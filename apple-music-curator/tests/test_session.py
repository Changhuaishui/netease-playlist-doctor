"""Offline recovery and write-permission tests for the playlist workflow."""

import contextlib
import copy
import importlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
TARGET = "AABB000000000001"
NAMESAKE = "AABB000000000002"
PLAYLIST_NAME = "两张歌单的共同名称"
STATE_DIR = "APPLE_MUSIC_CURATOR_STATE_DIR"


def candidate(catalog_id, name):
    return {
        "catalog_id": catalog_id, "name": name, "artist": "测试艺人", "album": "测试专辑",
        "url": f"https://music.apple.com/cn/song/{catalog_id}",
        "album_id": "222", "album_track_url": f"https://music.apple.com/cn/album/222?i={catalog_id}",
    }


SONGS = [candidate("601", "第一首"), candidate("602", "第二首")]


def local_track(song, persistent_id):
    return {field: song[field] for field in ("name", "artist", "album")} | {"persistentID": persistent_id}


def snapshot(identifier=TARGET, tracks=()):
    return {"playlist": {"persistentID": identifier, "name": PLAYLIST_NAME}, "tracks": list(tracks)}


def invoke(session, *args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
            patch("reconcile.simplified_labels", return_value={}), \
            patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected external process")):
        code = session.main(list(map(str, args)))
    return code, json.loads(stdout.getvalue().strip() or stderr.getvalue().strip())


def claim_worker(directory, run, barrier, results):
    # Spawn imports the module in a fresh interpreter, as separate CLI invocations do.
    os.environ[STATE_DIR] = directory
    session = importlib.import_module("session")
    with patch.object(session, "read_playlist", return_value=snapshot()), \
            patch.object(session, "create_playlist", side_effect=AssertionError("Unexpected creation")):
        barrier.wait(timeout=20)
        results.put(invoke(session, "claim", "--run", run, "--track", "601"))


class SessionWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.input = Path(temp.name) / "selection.json"
        self.input.write_text(json.dumps({"country": "CN", "source": "Apple Music public song page",
                                          "tracks": SONGS}), encoding="utf-8")
        env = patch.dict(os.environ, {STATE_DIR: temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.session = importlib.reload(importlib.import_module("session"))
        self.actual = snapshot()
        self.read = Mock(side_effect=lambda _: copy.deepcopy(self.actual))
        self.create = Mock(return_value=TARGET)
        for name, mock in (("read_playlist", self.read), ("create_playlist", self.create)):
            change = patch.object(self.session, name, mock)
            change.start()
            self.addCleanup(change.stop)

    def ok(self, *args):
        code, data = invoke(self.session, *args)
        self.assertEqual(code, 0, data)
        return data

    def start(self, *target):
        return self.ok("start", "--tracks", self.input, *(target or ("--playlist-id", TARGET)))["run"]

    def test_resume_persists_plan_and_reconciles_current_snapshot(self):
        run = self.start()
        self.assertIsInstance(run, int)
        self.input.unlink()
        initial = self.ok("resume")
        self.assertEqual(initial["state"], "ready")
        self.assertEqual(initial["next"]["catalog_id"], "601")
        self.assertEqual([row["status"] for row in initial["tracks"]], ["missing", "missing"])
        self.actual["tracks"].append(local_track(SONGS[0], "C001000000000001"))
        advanced = self.ok("resume", "--run", run)
        self.assertEqual(advanced["next"]["catalog_id"], "602")
        self.assertEqual([row["status"] for row in advanced["tracks"]], ["present", "missing"])
        self.actual["tracks"].append(local_track(SONGS[1], "C001000000000002"))
        for _ in range(2):
            final = self.ok("resume", "--run", run)
            self.assertEqual(final["state"], "complete")
            self.assertIsNone(final["next"])
        self.assertFalse(invoke(self.session, "claim", "--run", run, "--track", "601")[1].get("permit"))

    def test_uncertain_send_blocks_replay_until_readback(self):
        run = self.start()
        claim = self.ok("claim", "--run", run, "--track", "601")
        UUID(claim["permit"])
        self.read.side_effect = RuntimeError("simulated Music read failure")
        code, data = invoke(self.session, "resume", "--run", run)
        self.assertFalse(data.get("permit"))
        self.assertTrue(code or data["state"] != "ready")
        self.read.side_effect = lambda _: copy.deepcopy(self.actual)
        waiting = self.ok("resume", "--run", run)
        self.assertEqual(waiting["state"], "awaiting_readback")
        self.assertIsNone(waiting["next"])
        self.assertFalse(invoke(self.session, "claim", "--run", run, "--track", "601")[1].get("permit"))
        self.actual["tracks"].append(local_track(SONGS[0], "C001000000000001"))
        self.assertEqual(self.ok("resume", "--run", run)["next"]["catalog_id"], "602")

    def test_unsubmitted_claim_can_be_released_by_its_owner(self):
        run = self.start()
        permit = self.ok("claim", "--run", run, "--track", "601")["permit"]
        invalid, _ = invoke(self.session, "release", "--run", run, "--permit",
                            "00000000-0000-0000-0000-000000000000", "--not-sent")
        self.assertNotEqual(invalid, 0)
        self.assertEqual(self.ok("resume", "--run", run)["state"], "awaiting_readback")
        self.ok("release", "--run", run, "--permit", permit, "--not-sent")
        next_permit = self.ok("claim", "--run", run, "--track", "601")["permit"]
        self.assertNotEqual(next_permit, permit)

    def test_same_name_with_wrong_id_cannot_confirm_or_authorize(self):
        run = self.start()
        self.actual = snapshot(NAMESAKE, [local_track(SONGS[0], "C001000000000001")])
        code, result = invoke(self.session, "resume", "--run", run)
        self.assertTrue(code or result["state"] in ("review", "target_uncertain"))
        self.assertFalse(result.get("next"))
        self.assertFalse(invoke(self.session, "claim", "--run", run, "--track", "602")[1].get("permit"))

    def test_new_target_with_lost_reply_is_not_created_twice(self):
        run = self.start("--new-playlist", PLAYLIST_NAME)
        self.assertEqual(self.ok("resume", "--run", run)["state"], "needs_target")
        self.create.side_effect = RuntimeError("created but response lost")
        invoke(self.session, "target", "--run", run)
        self.assertEqual(self.ok("resume", "--run", run)["state"], "target_uncertain")
        invoke(self.session, "target", "--run", run)
        self.create.assert_called_once_with(PLAYLIST_NAME)
        self.ok("bind", "--run", run, "--playlist-id", TARGET)
        self.assertEqual(self.ok("resume", "--run", run)["state"], "ready")

    def test_two_runs_on_same_track_publish_one_permit_across_processes(self):
        first = self.start()
        self.input.write_text(json.dumps({"country": "CN", "tracks": SONGS[:1]}))
        runs = [first, self.start()]
        self.assertNotEqual(*runs)
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=claim_worker, args=(self.directory, run, barrier, results))
                   for run in runs]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(25)
                self.assertEqual(worker.exitcode, 0)
            replies = [results.get(timeout=2) for _ in workers]
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
            results.close()
        permits = [data["permit"] for code, data in replies if code == 0 and data.get("permit")]
        self.assertEqual(len(permits), 1, replies)
        UUID(permits[0])


if __name__ == "__main__":
    unittest.main()
