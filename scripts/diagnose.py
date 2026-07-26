#!/usr/bin/env python3
"""③ 确定性体检指标 —— 纯代码，无 AI。

这里算的每一个数字都是可复现的事实。AI 只负责**解读** metrics.json，
不负责计算。禁止 LLM 心算首数、总时长、占比（红线：一切计数与求和归脚本）。

输出 metrics.json，供 AI 阅读 + export_report.py 渲染。

用法：
    diagnose.py --playlist 3778678
    diagnose.py --playlist 3778678 --out /tmp/metrics.json
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys

import ncm_http as N
import store

# 时长直方图分桶（秒）
DURATION_BUCKETS = [(0, 120), (120, 180), (180, 210), (210, 240),
                    (240, 270), (270, 300), (300, 360), (360, 10 ** 9)]


def bucket_label(lo: int, hi: int) -> str:
    if hi >= 10 ** 9:
        return f"{lo // 60}:00+"
    return f"{lo // 60}:{lo % 60:02d}–{hi // 60}:{hi % 60:02d}"


def songs_of_playlist(inv: dict, pid: int | None) -> list[dict]:
    songs = list(inv.get("songs", {}).values())
    if pid is None:
        return songs
    return [s for s in songs if pid in (s.get("src_pids") or [])]


def analyze(songs: list[dict]) -> dict:
    total = len(songs)
    m: dict = {"total": total}

    # ---- 可听性 ----
    by_verdict = collections.Counter(s.get("playable") for s in songs)
    m["playability"] = dict(by_verdict)

    # grey = 真下架，需要 AI 提名等价替代
    m["grey"] = [
        {"sid": s["sid"], "name": s["name"],
         "artists": [a["name"] for a in s.get("ar") or []],
         "album": (s.get("al") or {}).get("name"),
         "year": s.get("year"),
         "reason": s.get("playable_reason"),
         "url": N.song_url(s["sid"])}
        for s in songs if s.get("playable") == "grey"
    ]
    # vip_limited = 歌还在，只是没开会员。**不触发替代提名。**
    m["vip_limited_count"] = by_verdict.get("vip_limited", 0)
    m["unknown_count"] = by_verdict.get("unknown", 0)

    # ---- 伪装成原唱的翻唱 / DJ 版（oct ∈ {2,3}）----
    m["covers"] = [
        {"sid": s["sid"], "name": s["name"],
         "artists": [a["name"] for a in s.get("ar") or []],
         "oct": s.get("oct"),
         "kind": "翻唱" if s.get("oct") == 2 else "DJ/remix",
         "url": N.song_url(s["sid"])}
        for s in songs if s.get("oct") in (2, 3)
    ]

    # ---- 疑似冒名条目：ar 里有 id=0 的歌手 ----
    m["unregistered_artist"] = [
        {"sid": s["sid"], "name": s["name"],
         "artists": [a["name"] for a in s.get("ar") or []],
         "url": N.song_url(s["sid"])}
        for s in songs
        if any((a.get("id") or 0) == 0 for a in s.get("ar") or [])
    ]

    # ---- 重复：归一化标题 + 主歌手 相同即视为同一首的不同版本 ----
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for s in songs:
        base, tags = N.norm_title(s.get("name") or "")
        primary = (s.get("ar") or [{}])[0].get("name") or ""
        groups[(base, primary)].append({
            "sid": s["sid"], "name": s["name"], "version": tags,
            "playable": s.get("playable"), "url": N.song_url(s["sid"]),
        })
    m["duplicates"] = [
        {"base_title": k[0], "artist": k[1], "count": len(v), "items": v}
        for k, v in groups.items() if len(v) > 1
    ]
    m["duplicates"].sort(key=lambda g: -g["count"])

    # ---- 时长分布 ----
    durations = [int(s["dt"]) // 1000 for s in songs if s.get("dt")]
    hist = []
    for lo, hi in DURATION_BUCKETS:
        n = sum(1 for d in durations if lo <= d < hi)
        hist.append({"bucket": bucket_label(lo, hi), "count": n,
                     "pct": round(100 * n / total, 1) if total else 0.0})
    m["duration"] = {
        "histogram": hist,
        "total_ms": sum(int(s.get("dt") or 0) for s in songs),
        "total_human": _hms(sum(int(s.get("dt") or 0) for s in songs)),
        "median_s": round(statistics.median(durations)) if durations else None,
        "mean_s": round(statistics.mean(durations)) if durations else None,
    }
    # 集中度：占比最高的那个时长桶
    if total:
        top = max(hist, key=lambda h: h["count"])
        m["duration"]["peak_bucket"] = top["bucket"]
        m["duration"]["peak_pct"] = top["pct"]

    # ---- 歌手集中度 ----
    artist_counter: collections.Counter = collections.Counter()
    for s in songs:
        for a in s.get("ar") or []:
            if a.get("name"):
                artist_counter[a["name"]] += 1
    m["artists"] = {
        "distinct": len(artist_counter),
        "top": [{"name": n, "count": c} for n, c in artist_counter.most_common(15)],
    }
    if total and artist_counter:
        top5 = sum(c for _, c in artist_counter.most_common(5))
        m["artists"]["top5_pct"] = round(100 * top5 / total, 1)
        m["artists"]["solo_artist_pct"] = round(
            100 * sum(1 for c in artist_counter.values() if c == 1)
            / len(artist_counter), 1)

    # ---- 年代分布 ----
    years = [s["year"] for s in songs if s.get("year")]
    decades: collections.Counter = collections.Counter(
        f"{y // 10 * 10}s" for y in years)
    m["era"] = {
        "known": len(years),
        "unknown": total - len(years),
        "decades": [{"decade": d, "count": c}
                    for d, c in sorted(decades.items())],
        "median_year": round(statistics.median(years)) if years else None,
    }

    # ---- 热度分布（仅供参考：pop 无法区分真假，见 references）----
    pops = [s["pop"] for s in songs if s.get("pop") is not None]
    m["pop"] = {
        "median": round(statistics.median(pops), 1) if pops else None,
        "low_pop_count": sum(1 for p in pops if p < 30),
    }

    return m


def _hms(ms: int) -> str:
    total = int(ms) // 1000
    h, rem = divmod(total, 3600)
    mnt, sec = divmod(rem, 60)
    return f"{h}小时{mnt}分" if h else f"{mnt}分{sec}秒"


def print_summary(m: dict, pl_name: str | None) -> None:
    total = m["total"]
    print(f"\n{'=' * 58}")
    print(f"体检对象：{pl_name or '(全部入库歌曲)'} — {total} 首"
          f" / 总时长 {m['duration']['total_human']}")
    print("=" * 58)

    p = m["playability"]
    print(f"\n【可听性】")
    print(f"  可播放      {p.get('ok', 0)}")
    print(f"  需会员      {p.get('vip_limited', 0)}   ← 歌还在，只是没开会员")
    print(f"  数字专辑    {p.get('album_only', 0)}")
    print(f"  ⚠️ 已下架    {len(m['grey'])}   ← 只有这些需要找替代")
    if m["unknown_count"]:
        print(f"  未知        {m['unknown_count']}")

    if m["grey"]:
        print(f"\n【已下架清单】（AI 将为这些提名等价替代）")
        for g in m["grey"][:20]:
            who = "/".join(g["artists"])
            album = f"《{g['album']}》" if g.get("album") else ""
            year = f" {g['year']}" if g.get("year") else ""
            print(f"  ⚠️  {g['name']} — {who}{album}{year}  [{g['reason']}]")
        if len(m["grey"]) > 20:
            print(f"  … 另有 {len(m['grey']) - 20} 首")

    if m["covers"]:
        print(f"\n【伪装成原唱的翻唱/DJ版】{len(m['covers'])} 首")
        for c in m["covers"][:10]:
            print(f"  {c['kind']}: {c['name']} — {'/'.join(c['artists'])}")
        if len(m["covers"]) > 10:
            print(f"  … 另有 {len(m['covers']) - 10} 首")

    if m["unregistered_artist"]:
        print(f"\n【疑似冒名条目（歌手 id=0）】{len(m['unregistered_artist'])} 首")
        for c in m["unregistered_artist"][:10]:
            print(f"  {c['name']} — {'/'.join(c['artists'])}")

    if m["duplicates"]:
        print(f"\n【重复版本】{len(m['duplicates'])} 组")
        for g in m["duplicates"][:10]:
            vers = ", ".join(
                f"{i['name']}{'[' + '+'.join(i['version']) + ']' if i['version'] else ''}"
                for i in g["items"])
            print(f"  {g['base_title']} — {g['artist']} ×{g['count']}: {vers}")

    d = m["duration"]
    print(f"\n【时长分布】中位 {d['median_s']}s / 均值 {d['mean_s']}s")
    for h in d["histogram"]:
        if h["count"]:
            bar = "█" * max(1, round(h["pct"] / 2))
            print(f"  {h['bucket']:>12} {h['count']:>4} ({h['pct']:>4.1f}%) {bar}")
    if d.get("peak_pct", 0) >= 35:
        print(f"  ⚠️ {d['peak_pct']}% 挤在 {d['peak_bucket']} —— 节奏单调")

    a = m["artists"]
    print(f"\n【歌手集中度】{a['distinct']} 位歌手"
          + (f" / Top5 占 {a.get('top5_pct')}%" if a.get("top5_pct") else ""))
    for t in a["top"][:8]:
        print(f"  {t['name']} ×{t['count']}")

    e = m["era"]
    if e["decades"]:
        print(f"\n【年代分布】中位 {e['median_year']}"
              + (f" / {e['unknown']} 首年份未知" if e["unknown"] else ""))
        for d2 in e["decades"]:
            bar = "█" * max(1, round(20 * d2["count"] / max(1, e["known"])))
            print(f"  {d2['decade']:>7} {d2['count']:>4} {bar}")

    print(f"\n{'=' * 58}")
    print("以上全部为脚本计算的确定性事实。")
    print("接下来由 AI 解读 metrics.json 并给出诊断结论 + 替代提名。")


def main() -> int:
    ap = argparse.ArgumentParser(description="歌单确定性体检（纯代码，无 AI）")
    ap.add_argument("--playlist", type=int, default=None,
                    help="只体检该歌单（省略则体检全部入库歌曲）")
    ap.add_argument("--inventory", default=None, help="inventory.json 路径")
    ap.add_argument("--out", default=None, help="metrics.json 输出路径")
    ap.add_argument("--quiet", action="store_true", help="不打印摘要")
    args = ap.parse_args()

    store.ensure_dirs()
    inv = (store.read_json(args.inventory) if args.inventory
           else store.load_inventory())
    if not inv or not inv.get("songs"):
        raise SystemExit("inventory 为空。请先跑 fetch_playlist.py + enrich_songs.py")

    songs = songs_of_playlist(inv, args.playlist)
    if not songs:
        raise SystemExit(
            f"歌单 {args.playlist} 在 inventory 里没有歌曲。"
            f"已入库的歌单：{list(inv.get('playlists', {}))}")

    pl_meta = (inv.get("playlists") or {}).get(str(args.playlist)) or {}
    m = analyze(songs)
    m["playlist"] = {"pid": args.playlist, "name": pl_meta.get("name"),
                     "creator": pl_meta.get("creator")}
    m["generated_at"] = store.now_iso()

    out = args.out or (store.OUT / store.today() / f"metrics_{args.playlist or 'all'}.json")
    store.write_json(out, m)

    if not args.quiet:
        print_summary(m, pl_meta.get("name"))
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
