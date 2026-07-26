#!/usr/bin/env python3
"""① 分享链接/歌单 id → 全量 trackIds。

红线断言：len(trackIds) == trackCount，不等则 exit 2 且不写文件。
理由：/api/v6/playlist/detail 只返回前 10 首 tracks，全量在 trackIds。
若两者不等说明分页/接口有变，此时继续往下跑会静默只处理一部分歌单。

用法：
    fetch_playlist.py --url "https://music.163.com/playlist?id=3778678"
    fetch_playlist.py --id 3778678 --id 123456
    fetch_playlist.py --url "http://163cn.tv/xxxxx"
"""

from __future__ import annotations

import argparse
import sys

import ncm_http as N
import store


def fetch_one(pid: int) -> dict:
    pl = N.playlist_detail(pid)

    track_ids = [int(t["id"]) for t in (pl.get("trackIds") or [])]
    track_count = pl.get("trackCount")

    # ---- 红线断言 R6 ----
    if track_count is not None and len(track_ids) != track_count:
        raise SystemExit(
            f"[对账失败] 歌单 {pid}「{pl.get('name')}」"
            f"trackCount={track_count} 但 trackIds={len(track_ids)} 条，差 "
            f"{abs(track_count - len(track_ids))} 首。\n"
            f"不写入任何文件。可能原因：接口分页有变，或歌单含不可见曲目。\n"
            f"请先跑 doctor.py --check endpoints。"
        )

    creator = (pl.get("creator") or {}).get("nickname")
    record = {
        "pid": pid,
        "name": pl.get("name"),
        "creator": creator,
        "trackCount": track_count,
        "tags": pl.get("tags") or [],
        "description": pl.get("description"),
        "updateTime": pl.get("updateTime"),
        "trackIds": track_ids,
        "fetched_at": store.now_iso(),
    }
    store.write_json(store.RAW / f"playlist_{pid}.json", record)
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取网易云歌单的全量 trackIds（只读）")
    ap.add_argument("--url", action="append", default=[],
                    help="歌单分享链接（支持 163cn.tv 短链），可重复")
    ap.add_argument("--id", action="append", type=int, default=[],
                    help="歌单 id，可重复")
    args = ap.parse_args()

    if not args.url and not args.id:
        ap.error("至少需要一个 --url 或 --id")

    store.ensure_dirs()

    pids: list[int] = list(args.id)
    for u in args.url:
        pids.append(N.resolve_share_link(u))

    inv = store.load_inventory()
    for i, pid in enumerate(dict.fromkeys(pids)):
        if i:
            N.throttle()
        rec = fetch_one(pid)
        inv["playlists"][str(pid)] = {
            k: rec[k] for k in
            ("name", "creator", "trackCount", "tags", "updateTime", "fetched_at")
        }
        print(f"✅ {pid}「{rec['name']}」by {rec['creator']} "
              f"— {len(rec['trackIds'])} 首（对账通过）")
        print(f"   → {store.RAW / f'playlist_{pid}.json'}")

    store.save_inventory(inv)
    print(f"\n下一步：enrich_songs.py --ids-from "
          f"{store.RAW}/playlist_<pid>.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
