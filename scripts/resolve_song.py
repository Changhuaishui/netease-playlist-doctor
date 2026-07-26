#!/usr/bin/env python3
"""⑥c 歌手优先的歌曲解析 —— AI 提名的验证通道。

这是「AI 提名 → 真实 songId」的唯一合法路径（红线 R1：AI 禁止凭记忆写 id）。

7 步算法，**前 6 步全确定性**：
  1. 读 wanted.json（AI 以 (歌名, 歌手, 专辑?, 年份?) 三元组提名）
  2. artists.json 查真身；miss → status=need_artist（AI 决策点 1）
  3. 拉该歌手全量曲库（limit=100 分页到 more==false，红线 R4）
  4. 曲库内三级匹配：原名 exact → norm_title exact → 相似度 ≥ fuzzy
  5. 命中 ≥2 → status=need_version（AI 决策点 2）
  6. 命中 0 → 回落 cloudsearch，**强制过 is_impostor + is_cover**（红线 R2）
  7. 候选过 playability；全部 grey → status=need_ai_fallback（AI 决策点 3）

为什么必须歌手优先（见 references/api-endpoints.md §4/§6）：
  直接搜「晴天 周杰伦」前 30 条 0 条是真的，真身 186016 根本不在结果里；
  而歌手曲库 /api/v2/artist/songs 能命中它。

用法：
    resolve_song.py --wanted wanted.json --out resolved.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ncm_http as N
import store

DEFAULT_FUZZY = 0.86
DURATION_TOL_MS = 3000


def catalog_for(artist_id: int, *, refresh: bool = False) -> list[dict]:
    """带磁盘缓存的歌手曲库。"""
    path = store.CATALOG / f"artist_{artist_id}.json"
    if not refresh:
        cached = store.read_json(path)
        if cached and cached.get("songs"):
            return cached["songs"]

    songs = N.artist_songs(artist_id)
    store.write_json(path, {"artist_id": artist_id, "count": len(songs),
                            "cached_at": store.now_iso(), "songs": songs})
    return songs


def _cat_row(s: dict) -> dict:
    """把 artist/songs 的条目归一成统一形状。

    注意该端点用 album/artists/duration，不是 al/ar/dt（references §6）。
    """
    al = s.get("album") or {}
    return {
        "sid": int(s["id"]),
        "name": s.get("name"),
        "album": al.get("name"),
        "duration_ms": s.get("duration"),
        "artist_ids": [a.get("id") for a in (s.get("artists") or [])],
    }


def match_in_catalog(want: dict, catalog: list[dict],
                     fuzzy: float) -> list[dict]:
    """曲库内三级匹配，返回带 match 类型与置信度的候选列表。"""
    title = want["title"]
    want_base, _ = N.norm_title(title)
    want_dur = want.get("duration_ms")
    hits: list[dict] = []

    for raw in catalog:
        row = _cat_row(raw)
        cname = row["name"] or ""
        base, tags = N.norm_title(cname)

        if cname == title:
            kind, conf = "exact", 1.0
        elif base and base == want_base:
            kind, conf = "normalized", 0.95
        else:
            ratio = N.title_similarity(cname, title)
            if ratio < fuzzy:
                continue
            kind, conf = "fuzzy", round(ratio, 3)

        # 时长吻合则加权，差得远则降权（用于同名多版本排序）
        if want_dur and row["duration_ms"]:
            delta = abs(int(row["duration_ms"]) - int(want_dur))
            conf = min(1.0, conf + 0.03) if delta <= DURATION_TOL_MS \
                else max(0.0, conf - 0.05)
            row["duration_delta_ms"] = delta

        # 专辑吻合则加权（AI 提名带专辑时能有效区分原版 vs 合辑/Live）
        if want.get("album") and row["album"]:
            if want["album"] in row["album"] or row["album"] in want["album"]:
                conf = min(1.0, conf + 0.04)

        row.update({"match": kind, "confidence": round(conf, 3),
                    "version": tags})
        hits.append(row)

    hits.sort(key=lambda h: (-h["confidence"], len(h["version"])))
    return hits


def search_fallback(want: dict, artist_id: int,
                    fuzzy: float) -> tuple[list[dict], dict]:
    """曲库没命中时回落全站搜索，返回 (存活候选, 各原因拦下计数)。

    红线 R2：必须过 is_impostor + is_cover 双重过滤。
    """
    kw = f"{want['title']} {want['artist']}"
    raw = N.search_songs(kw, limit=30)

    kept: list[dict] = []
    dropped = {"impostor": 0, "cover": 0, "title": 0}
    for s in raw:
        if N.is_impostor(s, artist_id):
            dropped["impostor"] += 1
            continue
        if N.is_cover(s):
            dropped["cover"] += 1
            continue
        if N.title_similarity(s.get("name") or "", want["title"]) < fuzzy:
            dropped["title"] += 1
            continue
        kept.append({
            "sid": int(s["id"]),
            "name": s.get("name"),
            "album": (s.get("al") or {}).get("name"),
            "duration_ms": s.get("dt"),
            "artist_ids": [a.get("id") for a in (s.get("ar") or [])],
            "match": "search",
            "confidence": 0.7,
            "version": N.norm_title(s.get("name") or "")[1],
        })
    return kept, dropped


def check_playable(cands: list[dict]) -> None:
    """就地补上可听性。红线 R3：不得跳过 privileges 检查。"""
    ids = [c["sid"] for c in cands]
    if not ids:
        return
    verdicts: dict[int, tuple[str, str]] = {}
    for chunk in N.chunked(ids, N.SONG_DETAIL_CHUNK):
        _, privs = N.song_detail(chunk)
        for p in privs:
            verdicts[int(p["id"])] = N.playability(p)
    for c in cands:
        v, r = verdicts.get(c["sid"], ("unknown", "未取到 privileges"))
        c["playable"] = v
        c["playable_reason"] = r


def resolve_one(want: dict, artists: dict, fuzzy: float,
                refresh: bool) -> dict:
    key = f"{want['title']}|{want['artist']}"
    out: dict = {"key": key, "title": want["title"], "artist": want["artist"]}

    # ---- 步骤 2：歌手真身 ----
    arec = artists.get(want["artist"])
    if not arec:
        out.update(status="need_artist",
                   hint=f"artists.json 无「{want['artist']}」。"
                        f"先跑 resolve_artist.py --name {want['artist']}")
        return out
    if arec.get("no_real_artist"):
        out.update(status="no_real_artist",
                   hint=f"「{want['artist']}」已确认无真身：{arec.get('reason', '')}")
        return out

    artist_id = arec["artist_id"]
    out["artist_id"] = artist_id

    # ---- 步骤 3+4：曲库内匹配 ----
    catalog = catalog_for(artist_id, refresh=refresh)
    out["catalog_size"] = len(catalog)
    hits = match_in_catalog(want, catalog, fuzzy)
    out["source"] = "catalog"

    # ---- 步骤 6：曲库 0 命中 → 回落搜索 ----
    if not hits:
        hits, dropped = search_fallback(want, artist_id, fuzzy)
        out["source"] = "search_fallback"
        out["search_dropped"] = dropped
        if not hits:
            out.update(status="unresolved",
                       reason="not_in_catalog_and_search_filtered_all",
                       hint="曲库无此曲，全站搜索结果全部被打假规则拦下。"
                            "这通常意味着该曲已下架且只剩翻唱/冒名条目。")
            return out

    # ---- 步骤 7：可听性 ----
    check_playable(hits)
    out["candidates"] = hits

    playable = [h for h in hits if h["playable"] in N.PLAYABLE_OK]
    if not playable:
        out.update(status="need_ai_fallback",
                   reason="all_candidates_unplayable",
                   hint="所有候选都不可听（下架/需购买）。"
                        "请由 AI 决定：接受某个替代版本，还是标记暂不可听并保留。")
        return out

    # ---- 步骤 5：同名多版本 → 交 AI 选 ----
    top = playable[0]
    distinct = {(p["name"], p["album"]) for p in playable}
    if len(distinct) > 1:
        out.update(status="need_version", best_guess=top["sid"],
                   hint=f"{len(distinct)} 个可听版本，请由 AI 选定"
                        f"（原版 vs Live vs 合辑重复 vs Remaster）")
        return out

    out.update(status="resolved", song_id=top["sid"],
               resolved_name=top["name"], album=top["album"],
               match=top["match"], confidence=top["confidence"],
               playable=top["playable"], url=N.song_url(top["sid"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="歌手优先的歌曲解析（AI 提名的验证通道）")
    ap.add_argument("--wanted", required=True,
                    help='JSON 数组：[{"title","artist","album?","duration_ms?"}]')
    ap.add_argument("--out", default=None, help="resolved.json 输出路径")
    ap.add_argument("--fuzzy", type=float, default=DEFAULT_FUZZY)
    ap.add_argument("--refresh-catalog", action="store_true")
    args = ap.parse_args()

    store.ensure_dirs()
    wanted = store.read_json(args.wanted)
    if not isinstance(wanted, list) or not wanted:
        raise SystemExit(f"{args.wanted} 必须是非空 JSON 数组")
    for w in wanted:
        if not w.get("title") or not w.get("artist"):
            raise SystemExit(f"每条提名必须含 title 与 artist：{w!r}")

    artists = store.load_artists()
    results = []
    for i, w in enumerate(wanted):
        if i:
            N.throttle(0.8, 1.6)
        r = resolve_one(w, artists, args.fuzzy, args.refresh_catalog)
        results.append(r)

        icon = {"resolved": "✅", "need_version": "🔀", "need_artist": "👤",
                "need_ai_fallback": "⚠️", "unresolved": "❌",
                "no_real_artist": "🚫"}.get(r["status"], "?")
        line = f"{icon} {w['title']} — {w['artist']}  [{r['status']}]"
        if r.get("song_id"):
            line += f"  → {r['song_id']} 《{r.get('album')}》 {r.get('playable')}"
        print(line)
        if r.get("hint"):
            print(f"     {r['hint']}")
        if r.get("search_dropped"):
            d = r["search_dropped"]
            print(f"     打假拦下：冒名 {d['impostor']} / 翻唱 {d['cover']} "
                  f"/ 标题不符 {d['title']}")

    out = args.out or (store.OUT / store.today() / "resolved.json")
    store.write_json(out, results)

    # 未解析的落 unresolved.json，报告里必须出现，不许静默丢弃
    bad = [r for r in results
           if r["status"] in ("unresolved", "no_real_artist")]
    if bad:
        unres = store.read_json(store.UNRESOLVED, []) or []
        keys = {u.get("key") for u in unres}
        for r in bad:
            if r["key"] not in keys:
                unres.append({"key": r["key"], "title": r["title"],
                              "artist": r["artist"],
                              "reason": r.get("reason") or r["status"],
                              "noted_at": store.now_iso()})
        store.write_json(store.UNRESOLVED, unres)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\n{'  '.join(f'{k}={v}' for k, v in counts.items())}")
    print(f"→ {out}")
    if bad:
        print(f"⚠️  {len(bad)} 条未解析 → {store.UNRESOLVED}（必须出现在报告里）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
