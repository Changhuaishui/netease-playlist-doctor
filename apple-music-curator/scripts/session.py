#!/usr/bin/env python3
"""Keep an ordered Apple Music selection and reconcile it with its original playlist."""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid

from catalog import apple_resource, validate_album_track_url
from playlist import run_request
from reconcile import reconcile


def read_playlist(identifier):
    script = Path(__file__).with_name("library.js")
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", str(script), "playlist", identifier],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def create_playlist(name):
    result = run_request(dict(mode="apply", playlist_id=None, new_playlist=name, track_ids=[]))
    # If Music returned an ID before the verification failed, keep that ID for recovery.
    identifier = result.get("playlist", {}).get("persistentID")
    if identifier:
        return identifier
    raise ValueError(result.get("error") or "音乐未返回新歌单的 ID")


def local_id(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9A-Fa-f]{1,16}", raw):
        raise ValueError("资料库 persistent ID 无效")
    return raw.upper()


def snapshot(identifier):
    data = read_playlist(identifier)
    if (local_id(data["playlist"]["persistentID"]) != identifier
            or not isinstance(data["tracks"], list)):
        raise ValueError("回读对象与记录的歌单 ID 不符")
    return data


def track_key(track):
    return track.get("catalog_id") or track["library_id"]


def selection(path):
    batch = json.loads(Path(path).read_text())
    rows = batch["tracks"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("选曲清单为空")
    result = []
    identities = set()
    for row in rows:
        track = {key: row[key] for key in ("name", "artist", "album")}
        if any(not isinstance(v, str) or not v.strip() for v in track.values()):
            raise ValueError("每首选曲需要曲名、艺人和专辑")
        if row.get("library_id"):
            track["library_id"] = local_id(row["library_id"])
        if row.get("catalog_id"):
            track["catalog_id"] = str(row["catalog_id"])
            if not re.fullmatch(r"[1-9][0-9]*", track["catalog_id"]):
                raise ValueError("目录曲目 ID 无效")
        if row.get("album_track_url") or not track.get("library_id"):
            url = row.get("album_track_url") or ""
            resource = apple_resource(url)
            if not resource or not row.get("album_id") or not track.get("catalog_id"):
                raise ValueError("库外选曲需要核实过的目录 ID、专辑 ID 和专辑曲目链接，请用 catalog.py page 核对")
            country = batch.get("country") or resource[0]
            track["album_id"] = str(row["album_id"])
            track["album_track_url"] = validate_album_track_url(url, country, track["album_id"], track["catalog_id"])
        if row.get("duration_ms"):
            track["duration_ms"] = row["duration_ms"]
        keys = {track[k] for k in ("catalog_id", "library_id") if k in track}
        if keys & identities:
            raise ValueError("选曲清单中有重复的曲目 ID")
        identities.update(keys)
        result.append(track)
    return result


def database():
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "apple-music-curator"
    folder = Path(os.environ.get("APPLE_MUSIC_CURATOR_STATE_DIR") or root)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "sessions.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(path, timeout=25, isolation_level=None)
    db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, document TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS active (slot INTEGER PRIMARY KEY CHECK(slot=1), run INTEGER NOT NULL)")
    return db


def activate(db, run):
    db.execute("INSERT OR REPLACE INTO active VALUES (1, ?)", (run,))


def select_run(db, run):
    if run is None:
        row = db.execute("SELECT run FROM active WHERE slot=1").fetchone()
        run = row[0] if row else None
    row = db.execute("SELECT document FROM runs WHERE id=?", (run,)).fetchone()
    if row is None:
        raise ValueError("找不到这次建单记录")
    return run, json.loads(row[0])


def save(db, run, doc):
    db.execute("UPDATE runs SET document=? WHERE id=?", (json.dumps(doc, ensure_ascii=False), run))


def start(db, args):
    tracks = selection(args.tracks)
    identifier = local_id(args.playlist_id) if args.playlist_id else None
    name = snapshot(identifier)["playlist"]["name"] if identifier else args.new_playlist.strip()
    if not name:
        raise ValueError("新歌单名称为空")
    # Repeating the same start after a lost response must reuse the saved intent.
    keys = [track_key(t) for t in tracks]
    for run, raw in db.execute("SELECT id, document FROM runs ORDER BY id DESC"):
        old = json.loads(raw)
        same = old.get("playlist_id") == identifier if identifier else old.get("new_name") == name
        if same and keys == [track_key(t) for t in old["tracks"]]:
            activate(db, run)
            return {"run": run, "playlist_id": old.get("playlist_id"), "name": name, "reused": True}
    doc = dict(name=name, new_name=None if identifier else name, playlist_id=identifier,
               creating=False, pending=None, tracks=tracks)
    run = db.execute("INSERT INTO runs(document) VALUES (?)", (json.dumps(doc, ensure_ascii=False),)).lastrowid
    activate(db, run)
    return {"run": run, "playlist_id": identifier, "name": name, "reused": False}


def inspect_run(db, run, doc):
    identifier = doc["playlist_id"]
    out = dict(run=run, playlist_id=identifier, name=doc["name"], tracks=[], next=None, permit=None)
    if not identifier:
        out["state"] = "target_uncertain" if doc["creating"] else "needs_target"
        return out
    actual = snapshot(identifier)
    doc["name"] = out["name"] = actual["playlist"]["name"]
    tracks = doc["tracks"]
    for planned, match in zip(tracks, reconcile(tracks, actual["tracks"])):
        key = track_key(planned)
        if match["status"] == "present":
            if match.get("persistentID"):
                planned["library_id"] = local_id(match["persistentID"])
            if doc["pending"] and doc["pending"]["track"] == key:
                doc["pending"] = None
        out["tracks"].append({**planned, **match})
    outstanding = next((t for t in out["tracks"] if t["status"] != "present"), None)
    if doc["pending"]:
        out.update(state="awaiting_readback", pending_track=doc["pending"]["track"])
    elif outstanding is None:
        out["state"] = "complete"
    elif outstanding["status"] == "review":
        out.update(state="review", review_track=track_key(outstanding))
    else:
        out.update(state="ready", next=outstanding)
    save(db, run, doc)
    return out


def ensure_target(db, run, doc):
    if doc["playlist_id"] or doc["creating"]:
        return inspect_run(db, run, doc)
    # Commit *before* sending Music the create event. An interrupted reply must
    # never cause a second create or an automatic bind to a namesake.
    doc["creating"] = True
    save(db, run, doc)
    db.commit()
    identifier = local_id(create_playlist(doc["new_name"]))
    db.execute("BEGIN IMMEDIATE")
    _, doc = select_run(db, run)
    if doc["playlist_id"] and doc["playlist_id"] != identifier:
        raise ValueError("建单操作期间目标绑定发生变化，请核对原歌单")
    doc.update(playlist_id=identifier, creating=False)
    save(db, run, doc)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    return inspect_run(db, run, doc)


def execute(db, args):
    db.execute("BEGIN IMMEDIATE")
    if args.command == "start":
        return start(db, args)
    run, doc = select_run(db, args.run)
    if args.command == "target":
        return ensure_target(db, run, doc)
    if args.command == "bind":
        identifier = local_id(args.playlist_id)
        if doc["playlist_id"] and identifier != doc["playlist_id"]:
            raise ValueError("已绑定的歌单 ID 不能替换")
        if not doc["playlist_id"] and snapshot(identifier)["playlist"]["name"] != doc["new_name"]:
            raise ValueError("歌单与原建单名称不符")
        doc.update(playlist_id=identifier, creating=False)
        save(db, run, doc)
    view = inspect_run(db, run, doc)
    if args.command == "resume":
        activate(db, run)
    if args.command == "release":
        pending = doc["pending"]
        if not pending or pending["permit"] != args.permit:
            raise ValueError("这不是当前未决操作的凭据")
        doc["pending"] = None
        save(db, run, doc)
        return inspect_run(db, run, doc)
    if args.command == "claim" and view["next"] and track_key(view["next"]) == args.track:
        for other_run, raw in db.execute("SELECT id, document FROM runs WHERE id!=?", (run,)):
            other = json.loads(raw)
            if other["playlist_id"] == doc["playlist_id"] and other["pending"]:
                view.update(state="awaiting_readback", next=None, blocking_run=other_run)
                return view
        permit = uuid.uuid4().hex
        doc["pending"] = {"track": args.track, "permit": permit}
        save(db, run, doc)
        view["permit"] = permit
    return view


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="把已选曲目及顺序保存到本机")
    begin.add_argument("--tracks", required=True)
    target = begin.add_mutually_exclusive_group(required=True)
    target.add_argument("--playlist-id")
    target.add_argument("--new-playlist")
    for name in ("target", "resume", "bind", "claim", "release"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run", type=int, required=name != "resume")
        if name == "bind":
            cmd.add_argument("--playlist-id", required=True)
        if name == "claim":
            cmd.add_argument("--track", required=True)
        if name == "release":
            cmd.add_argument("--permit", required=True)
            cmd.add_argument("--not-sent", action="store_true", required=True)
    args = parser.parse_args(argv)
    db = None
    try:
        db = database()
        result = execute(db, args)
        db.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(json.dumps({"error": detail}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
