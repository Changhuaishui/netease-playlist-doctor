#!/usr/bin/env python3
"""⑥a 歌手候选打分 —— 脚本只排序打分，**不自动选**（AI 决策点 1）。

为什么不自动选（见 references/api-endpoints.md §5 李志案例）：
  搜「李志」返回两个精确同名者，musicSize 分别是 1 和 2（随机路人），
  而真李志 2019 年已全面下架、在网易云归零。
  纯代码的"取 musicSize 最大者"会选中 `李志洲`(784 首) —— 完全不同的歌手；
  就算限定精确同名，也会选中那个 2 首歌的路人。
  **这不是效果不好，这是静默输出垃圾。**

所以脚本给证据，AI 做判断，然后调 confirm_artist.py 落库。

用法：
    resolve_artist.py --name 周杰伦
    resolve_artist.py --name 李志 --limit 8
    resolve_artist.py --names-from wanted.json     # 批量补齐缺失歌手
"""

from __future__ import annotations

import argparse
import re
import sys

import ncm_http as N
import store

# 冒名者常用的形近字/装饰后缀
HOMOGLYPH_SUFFIX = re.compile(r"[-.、。·，,！!~～^*_♚♛♬♪✿❤\s]+$")

TINY_CATALOG = 50      # musicSize 低于此值视为体量可疑
STRONG_CATALOG = 100   # 唯一候选且高于此值才允许 --auto-accept


def score(cand: dict, query: str) -> tuple[float, list[str]]:
    """给候选打分并列出 flags。分数只用于排序，不用于自动决策。"""
    name = (cand.get("name") or "").strip()
    alias = cand.get("alias") or []
    music = cand.get("musicSize") or 0
    album = cand.get("albumSize") or 0

    flags: list[str] = []

    exact = name == query
    stripped = HOMOGLYPH_SUFFIX.sub("", name)
    if not exact and stripped == query:
        flags.append("homoglyph_suffix")   # 如「周杰伦-」「周杰伦.」
    if music < TINY_CATALOG:
        flags.append("tiny_catalog")
    if not alias:
        flags.append("no_alias")
    if query in alias and not exact:
        # 「周杰伦♚」的 alias 精确等于真名 —— 名字匹配必败，只有体量能分
        flags.append("alias_impersonation")

    # 名字既不精确相等、也不是形近字变体、query 只是子串
    # → 这是**另一个歌手**（如查「李志」命中「李志洲」/「李志辉」）。
    # 必须重罚：否则大曲库的同姓歌手会盖过真身，把 AI 引向错误答案。
    substring_only = (not exact
                      and "homoglyph_suffix" not in flags
                      and query not in alias
                      and query in name)
    if substring_only:
        flags.append("different_name")

    s = 0.0
    if exact:
        s += 0.40
    elif query in alias:
        s += 0.20
    elif "homoglyph_suffix" in flags:
        s += 0.15
    # 体量：对数尺度，568 首 ≈ 0.35，20 首 ≈ 0.12
    s += min(0.35, 0.35 * (music / 600) ** 0.5)
    s += min(0.15, 0.15 * (album / 40) ** 0.5)
    if alias:
        s += 0.10
    s -= 0.15 * len(set(flags) & {"homoglyph_suffix", "alias_impersonation"})
    if substring_only:
        s -= 0.45
    return round(max(0.0, min(1.0, s)), 3), flags


def resolve(name: str, limit: int) -> dict:
    cands = N.search_artists(name, limit=limit)
    rows = []
    for c in cands:
        sc, flags = score(c, name)
        rows.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "alias": c.get("alias") or [],
            "musicSize": c.get("musicSize"),
            "albumSize": c.get("albumSize"),
            "score": sc,
            "flags": flags,
            "name_exact": (c.get("name") or "").strip() == name,
        })
    # 精确同名者一律排在前面，再按分数。
    # 理由：名字不同就是另一个歌手，曲库再大也不该排在真身候选之上。
    rows.sort(key=lambda r: (not r["name_exact"], -r["score"]))
    return {"query": name, "candidates": rows}


def print_block(res: dict) -> None:
    print(f"\n【歌手候选】query = {res['query']!r}")
    if not res["candidates"]:
        print("  （搜索无结果）")
        print("  → 该歌手可能已全面下架，或名字有误。这需要 AI 判断，不要硬选。")
        return
    print(f"  {'id':>11} {'score':>6} {'musicSize':>10} {'albumSize':>9}  "
          f"name / alias / flags")
    for r in res["candidates"]:
        alias = f" alias={r['alias']}" if r["alias"] else ""
        flags = f" ⚠️{','.join(r['flags'])}" if r["flags"] else ""
        print(f"  {r['id']:>11} {r['score']:>6.3f} {str(r['musicSize']):>10} "
              f"{str(r['albumSize']):>9}  {r['name']}{alias}{flags}")

    print("\n  ⚠️ 脚本不自动选。请由 AI 判断真身，并说出依据"
          "（musicSize / albumSize / alias），然后：")
    print(f"     confirm_artist.py --name {res['query']!r} --id <ID> --by ai")
    print("  ⚠️ 若所有候选体量都极小（如全部 <50 首），很可能真身已下架 —— "
          "此时应判定「无真身」而不是硬选一个。")


def main() -> int:
    ap = argparse.ArgumentParser(description="歌手候选打分（不自动决策）")
    ap.add_argument("--name", action="append", default=[], help="歌手名，可重复")
    ap.add_argument("--names-from", default=None,
                    help="wanted.json，自动取其中所有 artist 字段")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--out", default=None, help="候选 JSON 输出路径")
    ap.add_argument("--only-missing", action="store_true",
                    help="跳过 artists.json 里已确认的歌手")
    args = ap.parse_args()

    names: list[str] = list(args.name)
    if args.names_from:
        wanted = store.read_json(args.names_from) or []
        for w in wanted:
            if w.get("artist"):
                names.append(w["artist"])
    names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    if not names:
        ap.error("至少需要 --name 或 --names-from")

    store.ensure_dirs()
    known = store.load_artists()
    if args.only_missing:
        names = [n for n in names if n not in known]
        if not names:
            print("✅ 所有歌手都已确认过真身，无需解析")
            return 0

    out = {}
    for i, n in enumerate(names):
        if i:
            N.throttle()
        res = resolve(n, args.limit)
        out[n] = res
        print_block(res)

    if args.out:
        store.write_json(args.out, out)
        print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
