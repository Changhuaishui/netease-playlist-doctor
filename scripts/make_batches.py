#!/usr/bin/env python3
"""④ 把待标注歌曲切成批次 + 注入 label_space。

三个设计决定：
  1. 按歌手聚类后再切批 —— 同一歌手的歌落在同批，标签一致性显著提高，
     也避免同歌手跨批标签漂移。
  2. label_space 直接注入每个批次 —— AI 不需要每批重读 taxonomy.md。
  3. 增量：只有 needs_labeling() 为真的歌进批次（sid 新增 / input_hash 变 /
     taxonomy_version 升级）。

用法：
    make_batches.py --playlist 3778678
    make_batches.py --playlist 3778678 --size 80
    make_batches.py --playlist 3778678 --force      # 忽略缓存全量重标
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import store

SKILL_DIR = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = SKILL_DIR / "assets" / "taxonomy.json"

DEFAULT_SIZE = 80

# 低置信提示阈值（见 references/taxonomy.md 自动降级规则）
LOW_POP = 30


def load_taxonomy() -> dict:
    tax = store.read_json(TAXONOMY_PATH)
    if not tax:
        raise SystemExit(f"读不到 {TAXONOMY_PATH}")
    return tax


def compact(song: dict) -> dict:
    """批次里只放标注需要的字段，压到最小以省 token。"""
    ar = song.get("ar") or []
    row = {
        "sid": song["sid"],
        "n": song.get("name"),
        "a": [a.get("name") for a in ar],
        "al": (song.get("al") or {}).get("name"),
        "y": song.get("year"),
        "d": (int(song["dt"]) // 1000) if song.get("dt") else None,
        "pop": song.get("pop"),
    }
    if song.get("oct") in (2, 3):
        row["oct"] = song["oct"]          # 翻唱/DJ版，护栏 F4
    if any((a.get("id") or 0) == 0 for a in ar):
        row["unregistered"] = True        # 疑似冒名条目
    if (song.get("pop") or 0) < LOW_POP:
        row["hint"] = "low_confidence"    # 自动降级到 E3 起判，护栏 F1
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="切分标注批次（按歌手聚类）")
    ap.add_argument("--playlist", type=int, default=None,
                    help="只处理该歌单（省略则处理全部入库歌曲）")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE,
                    help=f"每批歌曲数，默认 {DEFAULT_SIZE}")
    ap.add_argument("--force", action="store_true", help="忽略标注缓存")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    store.ensure_dirs()
    inv = store.load_inventory()
    if not inv.get("songs"):
        raise SystemExit("inventory 为空。请先跑 fetch_playlist.py + enrich_songs.py")

    cls = store.load_classified()
    labels = cls.get("labels", {})
    tax = load_taxonomy()

    songs = list(inv["songs"].values())
    if args.playlist is not None:
        songs = [s for s in songs
                 if args.playlist in (s.get("src_pids") or [])]
    if not songs:
        raise SystemExit(f"歌单 {args.playlist} 在 inventory 里没有歌曲")

    total = len(songs)
    todo = songs if args.force else [
        s for s in songs if store.needs_labeling(s["sid"], s, labels)
    ]
    print(f"歌单内 {total} 首 / 已标注 {total - len(todo)} / 待标注 {len(todo)}")

    if not todo:
        print("✅ 全部已标注，无需新批次。直接跑 diagnose.py 与 export_report.py")
        return 0

    # ---- 按主歌手聚类，再按聚类填批 ----
    by_artist: dict[str, list[dict]] = collections.defaultdict(list)
    for s in todo:
        primary = (s.get("ar") or [{}])[0].get("name") or "(unknown)"
        by_artist[primary].append(s)
    # 大歌手优先，让同歌手尽量不跨批
    clusters = sorted(by_artist.values(), key=len, reverse=True)

    batches: list[list[dict]] = []
    cur: list[dict] = []
    for cluster in clusters:
        for s in cluster:
            cur.append(s)
            if len(cur) >= args.size:
                batches.append(cur)
                cur = []
    if cur:
        batches.append(cur)

    out_dir = Path(args.out_dir) if args.out_dir else store.BATCHES
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一轮的批次，避免 index 与文件不一致
    for old in out_dir.glob("batch_*.json"):
        old.unlink()

    index = {"taxonomy_version": tax["taxonomy_version"],
             "playlist": args.playlist,
             "created_at": store.now_iso(),
             "batches": []}

    for i, rows in enumerate(batches, 1):
        bid = f"batch_{i:03d}"
        payload = {
            "batch_id": bid,
            "taxonomy_version": tax["taxonomy_version"],
            "label_space": tax["label_space"],
            "cardinality": tax["cardinality"],
            "evidence_grades": tax["evidence_grades"],
            "rules": [
                "对每首歌输出一条记录，必须回引 i 与 sid（禁止用歌名指代）",
                "标签必须取自 label_space，越界会被拒绝写入",
                "拿不准就 genre/mood 给 null + note，不要编",
                "hint=low_confidence 的歌默认从 E3 起判",
                "E3 必须 uncertain=true",
                "无歌词可读，情绪最多给 E2，不得给 E1",
                "禁止把歌手整体风格套到风格迥异的单曲上",
            ],
            "songs": [dict(i=j, **compact(s)) for j, s in enumerate(rows, 1)],
        }
        store.write_json(out_dir / f"{bid}.json", payload)
        index["batches"].append({"batch_id": bid, "count": len(rows),
                                 "status": "pending"})

    store.write_json(out_dir / "index.json", index)

    print(f"\n切出 {len(batches)} 批（每批 ≤{args.size} 首，按歌手聚类）")
    print(f"→ {out_dir}/batch_001.json … batch_{len(batches):03d}.json")
    print(f"→ {out_dir}/index.json")
    print("\n⚠️ 上下文纪律：每处理完一批立刻 merge，不要在上下文里累积多批答案。")
    print("   merge_classification.py --batch batch_001 --answer <file|->")
    return 0


if __name__ == "__main__":
    sys.exit(main())
