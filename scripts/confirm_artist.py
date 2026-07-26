#!/usr/bin/env python3
"""⑥b 把 AI 认定的歌手真身写入 artists.json 缓存。

也支持记录「无真身」（真身已全面下架），避免每次重复解析同一个死胡同。

用法：
    confirm_artist.py --name 周杰伦 --id 6452 --by ai \
        --reason "musicSize=568 albumSize=41 alias含官方英文名 Jay Chou"
    confirm_artist.py --name 李志 --none --by ai \
        --reason "仅两个同名者各1-2首，真身2019年已全面下架"
"""

from __future__ import annotations

import argparse
import sys

import ncm_http as N
import store


def main() -> int:
    ap = argparse.ArgumentParser(description="确认歌手真身（AI 决策点 1 的落库）")
    ap.add_argument("--name", required=True, help="歌手名（与解析时的 query 一致）")
    ap.add_argument("--id", type=int, default=None, help="认定的真身 artist id")
    ap.add_argument("--none", action="store_true",
                    help="认定「无真身」（已全面下架/查无此人）")
    ap.add_argument("--by", default="ai", choices=["ai", "user"])
    ap.add_argument("--reason", default="", help="判断依据，会存档以便复核")
    args = ap.parse_args()

    if not args.none and args.id is None:
        ap.error("需要 --id，或用 --none 记录「无真身」")
    if args.none and args.id is not None:
        ap.error("--id 与 --none 互斥")

    store.ensure_dirs()
    artists = store.load_artists()

    if args.none:
        artists[args.name] = {
            "artist_id": None, "no_real_artist": True,
            "confirmed_by": args.by, "reason": args.reason,
            "confirmed_at": store.now_iso(),
        }
        store.save_artists(artists)
        print(f"✅ 已记录「{args.name}」无真身"
              + (f" —— {args.reason}" if args.reason else ""))
        print("   后续解析会直接跳过该歌手，不再重复搜索。")
        return 0

    # 校验 id 真实存在，顺手回填体量作为存档证据
    cands = N.search_artists(args.name, limit=10)
    hit = next((c for c in cands if int(c.get("id", 0)) == args.id), None)

    rec = {
        "artist_id": args.id,
        "confirmed_by": args.by,
        "reason": args.reason,
        "confirmed_at": store.now_iso(),
    }
    if hit:
        rec.update({
            "resolved_name": hit.get("name"),
            "alias": hit.get("alias") or [],
            "musicSize": hit.get("musicSize"),
            "albumSize": hit.get("albumSize"),
        })
        print(f"✅ 「{args.name}」→ id {args.id}（{hit.get('name')}，"
              f"{hit.get('musicSize')} 首 / {hit.get('albumSize')} 张专辑）")
    else:
        rec["warning"] = "该 id 不在当前搜索结果中，未能回填体量证据"
        print(f"⚠️  「{args.name}」→ id {args.id}"
              f"（该 id 不在搜索结果里，已记录但无法回填体量）")

    if args.reason:
        print(f"   依据：{args.reason}")
    artists[args.name] = rec
    store.save_artists(artists)
    return 0


if __name__ == "__main__":
    sys.exit(main())
