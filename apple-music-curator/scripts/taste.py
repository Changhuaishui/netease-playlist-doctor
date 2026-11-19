#!/usr/bin/env python3
"""Remember numbered selections and rank candidates from local listening feedback."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unicodedata

from session import database, select_run, selection, snapshot


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def load_tracks(path):
    tracks = selection(path)
    rows = json.loads(Path(path).read_text(encoding="utf-8"))["tracks"]
    for track, row in zip(tracks, rows):
        tags = row.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not normalize(tag) for tag in tags):
            raise ValueError("tags 必须是非空标签字符串的数组")
        track["tags"] = sorted({normalize(tag) for tag in tags})
    return tracks


def identity(track):
    if "catalog_id" in track:
        return "catalog:" + track["catalog_id"]
    return "library:" + track["library_id"]


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS taste_notes (
            scope TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY (scope, note)
        );
        CREATE TABLE IF NOT EXISTS taste_batches (
            name TEXT PRIMARY KEY, scope TEXT NOT NULL, tracks TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS taste_votes (
            scope TEXT NOT NULL, track_id TEXT NOT NULL, vote INTEGER NOT NULL,
            track TEXT NOT NULL, note TEXT, PRIMARY KEY (scope, track_id)
        );
        CREATE TABLE IF NOT EXISTS taste_plays (
            scope TEXT NOT NULL, track_id TEXT NOT NULL, track TEXT NOT NULL,
            baseline INTEGER NOT NULL, highwater INTEGER NOT NULL, PRIMARY KEY (scope, track_id)
        );
        CREATE TABLE IF NOT EXISTS taste_sources (
            batch TEXT PRIMARY KEY, run INTEGER NOT NULL
        );
    """)


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def profile(db, scope):
    notes = db.execute("SELECT scope, note FROM taste_notes WHERE scope IN ('all', ?) ORDER BY rowid", (scope,))
    votes = db.execute("SELECT scope, vote, track, note FROM taste_votes WHERE scope IN ('all', ?)", (scope,))
    plays = db.execute("""SELECT scope, track, highwater - baseline FROM taste_plays
                          WHERE scope IN ('all', ?) AND highwater - baseline >= 2""", (scope,))
    sources = db.execute("""SELECT name, run FROM taste_batches JOIN taste_sources ON name = batch
                            WHERE scope IN ('all', ?) ORDER BY taste_sources.rowid DESC""", (scope,))
    return {
        "notes": [dict(scope=kind, text=note) for kind, note in notes],
        "feedback": [dict(scope=kind, vote=vote, track=json.loads(track), note=note)
                     for kind, vote, track, note in votes],
        "replays": [dict(scope=kind, track=json.loads(track), additional_plays=count)
                    for kind, track, count in plays],
        "sources": [dict(batch=batch, run=run) for batch, run in sources],
    }


def observe(db, name, run):
    batch = db.execute("SELECT scope, tracks FROM taste_batches WHERE name=?", (name,)).fetchone()
    if batch is None:
        raise ValueError("未找到该批选曲")
    scope, encoded = batch
    tracks = json.loads(encoded)
    _, doc = select_run(db, run)
    if not doc["playlist_id"] or list(map(identity, tracks)) != list(map(identity, doc["tracks"])):
        raise ValueError("选曲批次与已绑定的播放列表不符")
    db.execute("INSERT OR IGNORE INTO taste_sources VALUES (?, ?)", (name, run))
    if db.execute("SELECT run FROM taste_sources WHERE batch=?", (name,)).fetchone() != (run,):
        raise ValueError("该批次已绑定其他播放列表记录")
    actual = snapshot(doc["playlist_id"])
    if "playedCount" in actual["unavailableFields"]:
        return {"observed": 0, "reason": "音乐未提供播放次数"}
    counts = {row["persistentID"]: row["playedCount"] for row in actual["tracks"]
              if type(row.get("playedCount")) is int and row["playedCount"] >= 0}
    entries = []
    for track, planned in zip(tracks, doc["tracks"]):
        count = counts.get(planned.get("library_id"))
        if count is not None:
            entries.append((scope, identity(track), dump(track), count, count))
    db.executemany("""INSERT INTO taste_plays VALUES (?, ?, ?, ?, ?)
                      ON CONFLICT (scope, track_id) DO UPDATE
                      SET highwater=max(taste_plays.highwater, excluded.highwater)""", entries)
    return {"observed": len(entries), "playlist": actual["playlist"]["name"], "scope": scope}


def rank(db, scope, tracks):
    saved = profile(db, scope)
    weights = {}
    exact = {}
    replay_weights = {}
    for row in saved["feedback"]:
        track = row["track"]
        vote = row["vote"]
        scoped = row["scope"] != "all"
        for tag in track["tags"]:
            weights[tag] = weights.get(tag, 0) + vote * (2 if scoped else 1)
        if scoped or identity(track) not in exact:
            exact[identity(track)] = vote
    for row in saved["replays"]:
        track = row["track"]
        if identity(track) not in exact:
            for tag in track["tags"]:
                replay_weights[tag] = replay_weights.get(tag, 0) + 0.25
    # Cap feature effects so repeated tags cannot crowd out taste discovery.
    weights = {tag: max(-4, min(4, score)) for tag, score in weights.items()}
    for tag, weight in replay_weights.items():
        if tag not in weights:
            weights[tag] = min(0.75, weight)
    for track in tracks:
        signals = {tag: weights[tag] for tag in track["tags"] if weights.get(tag)}
        track["taste_score"] = sum(signals.values()) / (len(track["tags"]) or 1)
        track["taste_match"] = signals
        track["previous_feedback"] = exact.get(identity(track))
    ranked = sorted(tracks, key=lambda t: (t["previous_feedback"] == -1, -t["taste_score"]))
    return {"scope": scope, "notes": saved["notes"], "tracks": ranked}


def execute(db, args):
    if args.command == "note":
        note = args.text.strip()
        if not note:
            raise ValueError("偏好说明不能为空")
        db.execute("INSERT OR IGNORE INTO taste_notes VALUES (?, ?)", (args.scope, note))
        return {"scope": args.scope, "note": note}
    if args.command == "show":
        return profile(db, args.scope)
    if args.command == "rank":
        return rank(db, args.scope, load_tracks(args.tracks))
    if args.command == "observe":
        return observe(db, args.batch, args.run)
    if args.command == "remember":
        tracks = dump(load_tracks(args.tracks))
        db.execute("INSERT OR IGNORE INTO taste_batches VALUES (?, ?, ?)", (args.batch, args.scope, tracks))
        existing = db.execute("SELECT scope, tracks FROM taste_batches WHERE name=?", (args.batch,)).fetchone()
        if existing != (args.scope, tracks):
            raise ValueError("批次名称已有不同的编号记录，请换名")
        return {"batch": args.batch, "scope": args.scope, "count": len(json.loads(tracks))}

    batch = db.execute("SELECT scope, tracks FROM taste_batches WHERE name=?", (args.batch,)).fetchone()
    if batch is None:
        raise ValueError("未找到该批选曲")
    scope, tracks = batch[0], json.loads(batch[1])
    if not 1 <= args.number <= len(tracks):
        raise ValueError("曲目编号超出选曲清单")
    track = tracks[args.number - 1]
    scope = args.scope or scope
    vote = {"like": 1, "dislike": -1, "clear": 0}[args.vote]
    if vote:
        db.execute("INSERT OR REPLACE INTO taste_votes VALUES (?, ?, ?, ?, ?)",
                   (scope, identity(track), vote, dump(track), args.note))
    else:
        db.execute("DELETE FROM taste_votes WHERE scope=? AND track_id=?", (scope, identity(track)))
    return {"scope": scope, "number": args.number, "vote": vote, "track": track}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "note", "rank", "remember"):
        command = commands.add_parser(name)
        command.add_argument("--scope", default="all")
        if name in ("rank", "remember"):
            command.add_argument("--tracks", required=True)
        if name == "remember":
            command.add_argument("--batch", required=True)
        if name == "note":
            command.add_argument("--text", required=True)
    feedback = commands.add_parser("feedback")
    feedback.add_argument("--batch", required=True)
    feedback.add_argument("--number", required=True, type=int)
    feedback.add_argument("--vote", choices=("like", "dislike", "clear"), required=True)
    feedback.add_argument("--scope")
    feedback.add_argument("--note")
    observe_parser = commands.add_parser("observe")
    observe_parser.add_argument("--batch", required=True)
    observe_parser.add_argument("--run", required=True, type=int)
    args = parser.parse_args()
    if getattr(args, "scope", None) is not None:
        args.scope = normalize(args.scope)
        if not args.scope:
            parser.error("--scope 不能为空")
    try:
        with closing(database()) as db:
            initialize(db)
            print(json.dumps(execute(db, args), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
