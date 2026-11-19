#!/usr/bin/env python3
"""Read Music playback status or start a playlist from the beginning on repeat."""

import argparse
import json
import re
import subprocess
import sys


MUSIC_SCRIPT = r"""
function currentItem(getItem, isTrack) {
    // Music can have no current playlist or track, including during a transition.
    try {
        const item = getItem();
        const result = {persistentID: item.persistentID(), name: item.name()};
        if (isTrack) result.artist = item.artist();
        return result;
    } catch (error) {
        return null;
    }
}

function status(music) {
    if (!music.running()) return {running: false};
    return {
        running: true,
        playerState: music.playerState(),
        songRepeat: music.songRepeat(),
        shuffleEnabled: music.shuffleEnabled(),
        currentPlaylist: currentItem(() => music.currentPlaylist(), false),
        currentTrack: currentItem(() => music.currentTrack(), true)
    };
}

function loop(music, req) {
    const matches = music.playlists.whose({persistentID: req.playlistID})();
    if (matches.length !== 1) throw new Error("歌单 ID 没有唯一匹配");
    const playlist = matches[0];
    if (playlist.persistentID() !== req.playlistID) throw new Error("歌单 ID 已改变");
    const size = playlist.tracks.length;
    if (size === 0) throw new Error("目标歌单为空");
    if (req.titles.length) {
        const titles = playlist.tracks.name();
        const actual = Array.isArray(titles) ? titles : [titles];
        if (JSON.stringify(actual.slice().sort()) !== JSON.stringify(req.titles.slice().sort())) {
            throw new Error("歌单内容与指定的完整曲目名单不同");
        }
    }
    if (music.shuffleEnabled()) music.shuffleEnabled = false;
    const first = playlist.tracks[0];
    const firstID = first.persistentID();
    if (!firstID) throw new Error("无法读取首曲 ID");
    if (music.songRepeat() !== "all") music.songRepeat = "all";
    music.play(first, {once: false});

    let readback;
    for (let attempt = 0; attempt < 24; attempt++) {
        readback = status(music);
        if (readback.playerState === "playing" && readback.songRepeat === "all" &&
            readback.shuffleEnabled === false && readback.currentPlaylist &&
            readback.currentPlaylist.persistentID === req.playlistID &&
            readback.currentTrack && readback.currentTrack.persistentID === firstID) {
            return {verified: true, targetTrackCount: size, status: readback};
        }
        if (attempt < 23) delay(0.5);
    }
    return {verified: false, error: "播放回读与预期不一致", targetTrackCount: size,
            expectedFirstTrackID: firstID, status: readback};
}

function run(argv) {
    const req = JSON.parse(argv[0]);
    const music = Application("com.apple.Music");
    if (req.mode === "status") return JSON.stringify(status(music));
    try {
        return JSON.stringify(loop(music, req));
    } catch (error) {
        const result = {verified: false, error: String(error)};
        try { result.status = status(music); } catch (readError) { result.readError = String(readError); }
        return JSON.stringify(result);
    }
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("status", help="读取当前播放状态")
    loop = commands.add_parser("loop", help="从首曲开始，按顺序循环整张歌单")
    loop.add_argument("--playlist-id", required=True, help="资料库歌单的 persistent ID")
    loop.add_argument("--expect-title", action="append", default=[],
                      help="可重复传入，播放前核对完整歌名清单及重复次数")
    args = parser.parse_args()
    request = {"mode": args.mode}
    if args.mode == "loop":
        if not re.fullmatch(r"[0-9A-Fa-f]{1,16}", args.playlist_id):
            loop.error("--playlist-id 必须是 Music 返回的十六进制 persistent ID")
        request.update(playlistID=args.playlist_id.upper(), titles=args.expect_title)
    try:
        outcome = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-", json.dumps(request)],
            input=MUSIC_SCRIPT, text=True, capture_output=True, timeout=90, check=True,
        )
        result = json.loads(outcome.stdout)
    except subprocess.CalledProcessError as exc:
        result = {"error": exc.stderr.strip() or str(exc)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int("error" in result)


if __name__ == "__main__":
    sys.exit(main())
