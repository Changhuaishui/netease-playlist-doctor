#!/usr/bin/env python3
"""② 批量补全歌曲详情 + 可听性 → inventory.json（事实层）。

只存客观字段。曲风/情绪/语种等判断性标签一律不进这个文件
（它们在 classified.json，物理分离，重拉歌单不损失标签）。

红线断言：每批 len(songs) + len(missing) == len(requested)。

用法：
    enrich_songs.py --ids-from ~/.ncm-doctor/raw/playlist_3778678.json
    enrich_songs.py --ids 186016,1973665667
    enrich_songs.py --ids-from a.json --ids-from b.json --force
"""

from __future__ import annotations

import argparse
import sys

import ncm_http as N
import store


def collect_ids(args) -> tuple[list[int], dict[int, list[int]]]:
    """返回 (去重后的 id 列表, {sid: [来源 pid,...]})。"""
    ids: list[int] = []
    origin: dict[int, list[int]] = {}

    for path in args.ids_from:
        rec = store.read_json(path)
        if rec is None:
            raise SystemExit(f"读不到 {path}")
        pid = rec.get("pid")
        for sid in rec.get("trackIds") or []:
            ids.append(int(sid))
            origin.setdefault(int(sid), [])
            if pid and pid not in origin[int(sid)]:
                origin[int(sid)].append(pid)

    for raw in args.ids:
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                ids.append(int(part))
                origin.setdefault(int(part), [])

    return list(dict.fromkeys(ids)), origin


def main() -> int:
    ap = argparse.ArgumentParser(description="批量补全歌曲详情与可听性（只读）")
    ap.add_argument("--ids-from", action="append", default=[],
                    help="fetch_playlist.py 产出的 playlist_<pid>.json，可重复")
    ap.add_argument("--ids", action="append", default=[],
                    help="逗号分隔的歌曲 id，可重复")
    ap.add_argument("--chunk", type=int, default=N.SONG_DETAIL_CHUNK,
                    help=f"单批歌曲数，默认 {N.SONG_DETAIL_CHUNK}")
    ap.add_argument("--force", action="store_true",
                    help="忽略缓存，全部重新拉取")
    args = ap.parse_args()

    if not args.ids_from and not args.ids:
        ap.error("至少需要一个 --ids-from 或 --ids")
    if args.chunk > N.SONG_DETAIL_CHUNK:
        ap.error(f"--chunk 不得超过 {N.SONG_DETAIL_CHUNK}（URL/请求体上限）")

    store.ensure_dirs()
    inv = store.load_inventory()
    songs_db: dict = inv.setdefault("songs", {})

    requested, origin = collect_ids(args)
    if not requested:
        print("没有歌曲 id 可处理")
        return 0

    todo = requested if args.force else [
        s for s in requested if str(s) not in songs_db
    ]
    cached = len(requested) - len(todo)

    print(f"请求 {len(requested)} 首 / 缓存命中 {cached} / 待拉取 {len(todo)}")

    missing_all: list[int] = []
    stats = {"ok": 0, "vip_limited": 0, "album_only": 0, "grey": 0, "unknown": 0}

    for i, batch in enumerate(N.chunked(todo, args.chunk)):
        if i:
            N.throttle()
        songs, privs = N.song_detail(batch)
        priv_by_id = {int(p["id"]): p for p in privs if "id" in p}

        got = {int(s["id"]) for s in songs}
        missing = [b for b in batch if b not in got]

        # ---- 红线断言 ----
        if len(songs) + len(missing) != len(batch):
            raise SystemExit(
                f"[对账失败] 第 {i + 1} 批：请求 {len(batch)} 首，"
                f"返回 {len(songs)} 首 + 缺失 {len(missing)} 首，合计不符。\n"
                f"不写入任何数据。请跑 doctor.py --check endpoints。"
            )
        missing_all.extend(missing)

        for s in songs:
            sid = int(s["id"])
            priv = priv_by_id.get(sid)
            verdict, reason = N.playability(priv)
            stats[verdict] = stats.get(verdict, 0) + 1

            rec = {
                "sid": sid,
                "name": s.get("name"),
                "alia": s.get("alia") or [],
                "ar": [{"id": a.get("id"), "name": a.get("name")}
                       for a in (s.get("ar") or [])],
                "al": {"id": (s.get("al") or {}).get("id"),
                       "name": (s.get("al") or {}).get("name")},
                "dt": s.get("dt"),
                "pop": s.get("pop"),
                "fee": s.get("fee"),
                "oct": s.get("originCoverType"),
                "publishTime": s.get("publishTime"),
                "year": N.year_of(s.get("publishTime")),
                "priv": {k: priv.get(k) for k in ("st", "pl", "dl", "fee", "plLevel")}
                        if priv else None,
                "playable": verdict,
                "playable_reason": reason,
                "src_pids": origin.get(sid, []),
                "checked_at": store.now_iso(),
            }
            rec["input_hash"] = store.input_hash(rec)
            songs_db[str(sid)] = rec

        print(f"  批 {i + 1}: +{len(songs)} 首"
              + (f"，缺失 {len(missing)}" if missing else ""))

    # 补记来源歌单（缓存命中的歌也要记，否则换歌单跑会丢来源）
    for sid, pids in origin.items():
        rec = songs_db.get(str(sid))
        if rec is not None:
            merged = list(dict.fromkeys((rec.get("src_pids") or []) + pids))
            rec["src_pids"] = merged

    store.save_inventory(inv)

    if missing_all:
        unres = store.read_json(store.UNRESOLVED, []) or []
        known = {u.get("sid") for u in unres}
        for sid in missing_all:
            if sid not in known:
                unres.append({"sid": sid, "reason": "song_detail_missing",
                              "noted_at": store.now_iso()})
        store.write_json(store.UNRESOLVED, unres)

    print(f"\ninventory: {len(songs_db)} 首")
    if todo:
        print("本次可听性分布: " + "  ".join(
            f"{k}={v}" for k, v in stats.items() if v))
    if missing_all:
        print(f"⚠️  {len(missing_all)} 首接口查无此曲 → {store.UNRESOLVED}")
    print(f"\n下一步：diagnose.py --playlist <pid>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
