#!/usr/bin/env python3
"""⓪ 自检 —— 环境 / 端点探活 / 对账 / 回归测试。

出问题时先跑这个。四类检查：

  env        Python 版本与依赖（本 skill 只用 stdlib，不该缺任何东西）
  endpoints  4 个端点探活，缺字段时明确报「端点已变更」而不是让上层抛 KeyError
  counts     对账：Σ trackIds(去重) == inventory + unresolved（红线 R6）
  regression 三个已知案例必须复现 —— 这是防止红线被悄悄改坏的护栏

用法：
    doctor.py --check all
    doctor.py --check regression
"""

from __future__ import annotations

import argparse
import importlib
import sys

import ncm_http as N
import store

# 回归基准：全部来自 references/api-endpoints.md 的实测记录
REG_PLAYLIST = 3778678       # 热歌榜
REG_SONG_GREY = 186016       # 周杰伦《晴天》—— 已下架，priv.st=-200
REG_ARTIST = 6452            # 周杰伦
REG_ARTIST_MIN_SONGS = 566   # limit=100 分页应收满；若为 400 说明 page size 被改坏
REG_SEARCH_KW = "晴天 周杰伦"  # 前 30 条应被打假规则全部拦下

PASS, FAIL, WARN = "✅", "❌", "⚠️ "


class Result:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def ok(self, msg: str) -> None:
        print(f"  {PASS} {msg}")

    def bad(self, msg: str) -> None:
        print(f"  {FAIL} {msg}")
        self.failed += 1

    def warn(self, msg: str) -> None:
        print(f"  {WARN} {msg}")
        self.warned += 1


def check_env(r: Result) -> None:
    print("\n【env】")
    v = sys.version_info
    if v >= (3, 9):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        r.bad(f"Python {v.major}.{v.minor} 过旧，需要 ≥3.9")

    for mod in ("json", "urllib.request", "difflib", "unicodedata", "hashlib"):
        try:
            importlib.import_module(mod)
            r.ok(f"stdlib {mod}")
        except ImportError:
            r.bad(f"stdlib {mod} 缺失")

    # 本 skill 只读、不登录，所以不需要 requests / 加密库
    r.ok("无第三方依赖需求（只读模式不需要 requests / pycryptodome）")
    print(f"  状态目录 {store.HOME}"
          + ("（已存在）" if store.HOME.exists() else "（尚未创建）"))


def check_endpoints(r: Result) -> None:
    print("\n【endpoints】4 个只读端点探活")

    try:
        pl = N.playlist_detail(REG_PLAYLIST)
        r.ok(f"/api/v6/playlist/detail — 「{pl.get('name')}」"
             f" trackIds={len(pl.get('trackIds') or [])}")
    except N.EndpointChanged as e:
        r.bad(f"/api/v6/playlist/detail 结构已变更：{e}")
    except N.NcmError as e:
        r.bad(f"/api/v6/playlist/detail 不可用：{e}")

    try:
        songs, privs = N.song_detail([REG_SONG_GREY])
        if songs and privs:
            r.ok(f"/api/v3/song/detail — songs={len(songs)} privileges={len(privs)}")
        else:
            r.bad("/api/v3/song/detail 返回空 songs/privileges")
    except N.NcmError as e:
        r.bad(f"/api/v3/song/detail 不可用：{e}")

    try:
        arts = N.search_artists("周杰伦", limit=3)
        r.ok(f"/api/cloudsearch/pc (type=100) — {len(arts)} 位歌手候选")
    except N.NcmError as e:
        r.bad(f"/api/cloudsearch/pc 不可用：{e}")

    try:
        first = N.api_get("/api/v2/artist/songs", {
            "id": REG_ARTIST, "order": "hot",
            "limit": N.ARTIST_SONGS_PAGE, "offset": 0})
        if "songs" in first and "more" in first:
            r.ok(f"/api/v2/artist/songs — 首页 {len(first['songs'])} 首"
                 f" more={first['more']}")
        else:
            r.bad("/api/v2/artist/songs 缺少 songs/more 字段 —— 端点已变更")
    except N.NcmError as e:
        r.bad(f"/api/v2/artist/songs 不可用：{e}")


def check_counts(r: Result) -> None:
    print("\n【counts】对账（红线 R6）")
    inv = store.load_inventory()
    songs = inv.get("songs") or {}
    playlists = inv.get("playlists") or {}
    if not songs:
        r.warn("inventory 为空，跳过对账（先跑 fetch_playlist.py + enrich_songs.py）")
        return

    unres = store.read_json(store.UNRESOLVED, []) or []
    unres_sids = {u["sid"] for u in unres if u.get("sid")}

    all_wanted: set[int] = set()
    for pid in playlists:
        raw = store.read_json(store.RAW / f"playlist_{pid}.json")
        if raw:
            all_wanted |= {int(i) for i in raw.get("trackIds") or []}

    if not all_wanted:
        r.warn("找不到 raw/playlist_*.json，无法对账")
        return

    have = {int(s) for s in songs}
    missing = all_wanted - have - unres_sids
    if missing:
        r.bad(f"{len(missing)} 首在 trackIds 里但既不在 inventory 也不在 unresolved："
              f"{sorted(missing)[:10]}")
    else:
        r.ok(f"Σ trackIds(去重)={len(all_wanted)} == inventory∩ + unresolved "
             f"（inventory {len(have)} 首 / unresolved {len(unres_sids)} 首）")

    cls = store.load_classified()
    labels = cls.get("labels") or {}
    if len(labels) > len(have):
        r.bad(f"classified({len(labels)}) 多于 inventory({len(have)}) —— 有孤儿标注")
    else:
        r.ok(f"classified {len(labels)} 首 ≤ inventory {len(have)} 首")


def check_regression(r: Result) -> None:
    print("\n【regression】三个已知案例必须复现")

    # ① 真《晴天》必须判为 grey，依据是 priv.st=-200
    try:
        _, privs = N.song_detail([REG_SONG_GREY])
        priv = next((p for p in privs if int(p["id"]) == REG_SONG_GREY), None)
        verdict, reason = N.playability(priv)
        if verdict == "grey" and "st=-200" in reason:
            r.ok(f"① 186016《晴天》→ {verdict} ({reason})")
        else:
            r.bad(f"① 186016《晴天》应为 grey/priv.st=-200，"
                  f"实际 {verdict} ({reason}) —— playability() 规则表被改坏了？")
    except N.NcmError as e:
        r.bad(f"① 无法验证：{e}")

    # ② 搜「晴天 周杰伦」应被打假规则全部拦下（红线 R2）
    try:
        raw = N.search_songs(REG_SEARCH_KW, limit=30)
        survivors = [s for s in raw
                     if not N.is_impostor(s, REG_ARTIST) and not N.is_cover(s)]
        if not survivors:
            r.ok(f"② 搜「{REG_SEARCH_KW}」{len(raw)} 条 → 打假后 0 条存活（R2 有效）")
        else:
            r.bad(f"② 打假失效！{len(survivors)} 条假货漏网："
                  f"{[(s['id'], s['name']) for s in survivors[:5]]}")
    except N.NcmError as e:
        r.bad(f"② 无法验证：{e}")

    # ③ 歌手曲库必须收满，且能找到搜索里找不到的真歌（红线 R4）
    try:
        cat = N.artist_songs(REG_ARTIST)
        if len(cat) < REG_ARTIST_MIN_SONGS:
            r.bad(f"③ 歌手 {REG_ARTIST} 只收到 {len(cat)} 首"
                  f"（应 ≥{REG_ARTIST_MIN_SONGS}）—— "
                  f"page size 被改成 >100 了？实测 limit=300 只能收到 400 首。")
        elif not any(int(s["id"]) == REG_SONG_GREY for s in cat):
            r.bad(f"③ 曲库 {len(cat)} 首里找不到 186016 —— 歌手优先解析已失效")
        else:
            r.ok(f"③ 歌手 {REG_ARTIST} 曲库收满 {len(cat)} 首，"
                 f"且含搜索里找不到的 186016（R4 有效）")
    except N.NcmError as e:
        r.bad(f"③ 无法验证：{e}")

    # ④ norm_title 的关键行为（纯本地，无网络）
    cases = [
        ("晴天 (Live)", "晴天", "live"),
        ("晴天(DJ版)", "晴天", "remix"),
        ("晴天 (女声版))", "晴天", "revocal"),
    ]
    bad = [(t, N.norm_title(t)) for t, base, tag in cases
           if N.norm_title(t)[0] != base or tag not in N.norm_title(t)[1]]
    if bad:
        r.bad(f"④ norm_title 行为变化：{bad}")
    else:
        r.ok("④ norm_title 版本剥离正常（live/remix/revocal）")


def main() -> int:
    ap = argparse.ArgumentParser(description="自检：环境/端点/对账/回归")
    ap.add_argument("--check", default="all",
                    choices=["all", "env", "endpoints", "counts", "regression"])
    args = ap.parse_args()

    print("=" * 58)
    print("网易云歌单体检师 —— 自检")
    print("=" * 58)

    r = Result()
    if args.check in ("all", "env"):
        check_env(r)
    if args.check in ("all", "endpoints"):
        check_endpoints(r)
    if args.check in ("all", "counts"):
        check_counts(r)
    if args.check in ("all", "regression"):
        check_regression(r)

    print("\n" + "=" * 58)
    if r.failed:
        print(f"{FAIL} {r.failed} 项失败"
              + (f"，{r.warned} 项警告" if r.warned else ""))
        print("端点类失败 → 网易云可能改了接口，先读 references/api-endpoints.md。")
        print("回归类失败 → 有人改动了红线规则（打假/分页/可听性），必须先修复。")
        return 1
    print(f"{PASS} 全部通过"
          + (f"（{r.warned} 项警告）" if r.warned else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
