#!/usr/bin/env python3
"""Preview or apply a specific Apple Music playlist addition on this Mac."""

import argparse
import fcntl
import json
import re
import subprocess
import sys


CREATE_SCRIPT = """
on run argv
    set requestedName to item 1 of argv
    tell application id "com.apple.Music"
        if (exists (first playlist whose name is requestedName)) then error "已存在同名歌单"
        set createdPlaylist to make new user playlist with properties {name:requestedName}
        return persistent ID of createdPlaylist
    end tell
end run
"""


MUSIC_SCRIPT = r"""
function one(matches, label) {
    if (matches.length !== 1) throw new Error(label + " 匹配到 " + matches.length + " 项");
    return matches[0];
}

function ordinaryPlaylist(music, id) {
    const p = one(music.userPlaylists.whose({persistentID: id})(), "用户歌单 " + id);
    if (p.persistentID().toUpperCase() !== id || p.smart() !== false ||
        p.genius() !== false || p.specialKind() !== "none") {
        throw new Error("目标必须是普通用户歌单");
    }
    return p;
}

function libraryTrack(library, id) {
    const t = one(library.tracks.whose({persistentID: id})(), "资料库曲目 " + id);
    if (t.persistentID().toUpperCase() !== id) throw new Error("曲目 ID 不一致");
    return t;
}

function countTrack(playlist, id) {
    return playlist.tracks.whose({persistentID: id})().length;
}

function freshName(music, name) {
    const matches = music.playlists.whose({name: name})();
    if (matches.length) {
        throw new Error("已存在同名歌单，请用 --playlist-id 选择。匹配 ID " +
                        matches.map(p => p.persistentID()).join(", "));
    }
}

function run(argv) {
    const req = JSON.parse(argv[0]);
    const music = Application("com.apple.Music");
    const result = {mode: req.mode, playlist: {}, tracks: [], verified: false};
    let target = null;
    let library = null;
    try {
        if (req.mode === "check_name") {
            const matches = music.playlists.whose({name: req.new_playlist})();
            return JSON.stringify(matches.map(p => p.persistentID()));
        }
        if (req.playlist_id) {
            target = ordinaryPlaylist(music, req.playlist_id);
            result.playlist = {persistentID: req.playlist_id, name: target.name()};
        } else {
            freshName(music, req.new_playlist);
            result.playlist = {persistentID: null, name: req.new_playlist};
        }
        if (req.track_ids.length) {
            if (music.libraryPlaylists.length !== 1) throw new Error("无法唯一定位主资料库");
            library = music.libraryPlaylists[0];
        }
        // Resolve every selection before allowing the first write.
        for (const id of req.track_ids) {
            const t = libraryTrack(library, id);
            const before = target ? countTrack(target, id) : 0;
            result.tracks.push({persistentID: id, title: t.name(), artist: t.artist(),
                                album: t.album(), before: before,
                                status: before ? "already_present" : "to_add"});
        }
        if (req.mode === "preview") return JSON.stringify(result);
        if (!target) throw new Error("添加曲目前需要已核实的目标歌单 ID");
        for (const row of result.tracks) {
            if (countTrack(target, row.persistentID) > 0) {
                row.status = "already_present";
                continue;
            }
            row.status = "attempted";
            music.duplicate(libraryTrack(library, row.persistentID), {to: target});
        }
    } catch (error) {
        result.error = String(error);
    }

    // A Music event can fail after applying a change. Re-read the actual target,
    // including when some additions failed, so retrying uses its current content.
    if (req.mode === "apply" && result.playlist.persistentID) {
        try {
            target = ordinaryPlaylist(music, result.playlist.persistentID);
            result.playlist.name = target.name();
            for (const row of result.tracks) {
                row.after = countTrack(target, row.persistentID);
                if (row.after === 0) row.status = "missing";
                else if (row.after > Math.max(1, row.before)) row.status = "duplicate_conflict";
                else if (row.status === "attempted") row.status = "present_after_add";
                else row.status = "already_present";
            }
            result.verified = !result.error && result.tracks.length === req.track_ids.length &&
                result.tracks.every(row => ["already_present", "present_after_add"].includes(row.status));
            if (!result.error && !result.verified) result.error = "回读结果与选定清单不一致";
        } catch (error) {
            result.verify_error = String(error);
        }
    }
    return JSON.stringify(result);
}
"""


def persistent_id(value):
    if not re.fullmatch(r"[0-9a-fA-F]{1,16}", value):
        raise argparse.ArgumentTypeError("请使用 Music 返回的资料库 persistent ID（十六进制）")
    return value.upper()


def run_script(language, script, argument):
    process = subprocess.run(
        ["/usr/bin/osascript", "-l", language, "-", argument],
        input=script, capture_output=True, text=True, check=True
    )
    return process.stdout.strip()


def music(request):
    return json.loads(run_script("JavaScript", MUSIC_SCRIPT, json.dumps(request)))


def error_message(exc):
    return exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)


def run_request(request):
    if request["mode"] != "apply" or not request["new_playlist"]:
        return music(request)

    # Validate the entire selection before creating an empty playlist.
    result = music(dict(request, mode="preview"))
    result["mode"] = "apply"
    if result.get("error"):
        return result
    name = request["new_playlist"]
    try:
        identifier = run_script("AppleScript", CREATE_SCRIPT, name)
        if not re.fullmatch(r"[0-9a-fA-F]{1,16}", identifier):
            raise ValueError("新歌单的 persistent ID 未返回")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        result["error"] = error_message(exc)
        # A failed reply does not establish whether the creation was applied.
        # Report matching IDs for inspection without adopting a namesake.
        try:
            matches = music(dict(request, mode="check_name"))
            if not isinstance(matches, list):
                raise ValueError(matches.get("error", "无法核对同名歌单"))
            result["matching_playlist_ids"] = matches
        except (OSError, ValueError, subprocess.CalledProcessError) as check_exc:
            result["verify_error"] = error_message(check_exc)
        return result

    result["playlist"]["persistentID"] = identifier.upper()
    try:
        applied = music(dict(request, playlist_id=identifier.upper(), new_playlist=None))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        result["error"] = error_message(exc)
        return result
    if not applied.get("playlist", {}).get("persistentID"):
        applied["playlist"] = result["playlist"]
    return applied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--playlist-id", type=persistent_id, help="普通用户歌单的 persistent ID")
    target.add_argument("--new-playlist", help="新建歌单的完整名称")
    parser.add_argument("--track-id", action="append", type=persistent_id, default=[],
                        help="已选资料库曲目的 persistent ID，可以重复传参")
    args = parser.parse_args()
    if args.new_playlist is not None and not args.new_playlist.strip():
        parser.error("新歌单名称不能为空")
    if args.playlist_id and not args.track_id:
        parser.error("请选择至少一首资料库曲目")
    request = {"mode": args.mode, "playlist_id": args.playlist_id,
               "new_playlist": args.new_playlist, "track_ids": list(dict.fromkeys(args.track_id))}
    try:
        # Serialize concurrent applies using this installed copy without a lockfile.
        with open(__file__, "rb") as lock:
            if args.mode == "apply":
                fcntl.flock(lock, fcntl.LOCK_EX)
            result = run_request(request)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        message = error_message(exc)
        print(json.dumps({"error": message, "verified": False}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result.get("error") or result.get("verify_error") or
                    (args.mode == "apply" and not result["verified"])))


if __name__ == "__main__":
    raise SystemExit(main())
