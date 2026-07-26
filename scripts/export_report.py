#!/usr/bin/env python3
"""⑦ 渲染 Markdown 体检报告 —— 唯一交付通道（只读，绝不碰账号）。

因为不写回网易云，报告本身就是产品：每首歌带 music.163.com/song?id= 深链，
用户点一下就跳到网易云对应歌曲。

组成：
  1. AI 写的诊断结论（--diagnosis 传入 markdown 片段）
  2. 脚本算的确定性事实（metrics.json）
  3. 灰歌 → 替代 的 diff 表（resolved.json，每条都已验证可听）
  4. 标注分布与置信度（classified.json，含 null 率/E3 率）
  5. unresolved 清单 —— 必须出现，不许静默丢弃

用法：
    export_report.py --metrics metrics.json
    export_report.py --metrics m.json --resolved r.json --diagnosis d.md
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

import ncm_http as N
import store


def link(name: str, sid: int) -> str:
    return f"[{name}]({N.song_url(sid)})"


def h(text: str, level: int = 2) -> str:
    return f"\n{'#' * level} {text}\n"


def render_playability(m: dict) -> str:
    p = m["playability"]
    out = h("可听性")
    out += "| 状态 | 首数 | 含义 |\n|---|---:|---|\n"
    rows = [
        ("可播放", p.get("ok", 0), "正常"),
        ("需会员", p.get("vip_limited", 0), "**歌还在**，只是当前身份放不了"),
        ("数字专辑", p.get("album_only", 0), "需购买专辑"),
        ("⚠️ 已下架", len(m["grey"]), "**歌没了，需要找替代**"),
        ("未知", m.get("unknown_count", 0), "地区限制等，未判定为下架"),
    ]
    for label, n, meaning in rows:
        if n:
            out += f"| {label} | {n} | {meaning} |\n"
    out += ("\n> 「需会员」与「已下架」是两件不同的事：前者歌还在，"
            "后者才需要替代。\n> 本报告只对「已下架」提名替代。\n")
    return out


def render_substitutions(resolved: list[dict], grey: list[dict]) -> str:
    """灰歌 → 替代 的 diff 表。每条替代都已过打假 + 可听性验证。"""
    if not resolved:
        return ""
    ok = [r for r in resolved if r.get("status") == "resolved"]
    hard = [r for r in resolved
            if r.get("status") in ("need_ai_fallback", "need_version")]
    bad = [r for r in resolved
           if r.get("status") in ("unresolved", "no_real_artist", "need_artist")]

    out = h("替代提名（diff）")
    out += ("这些是 AI 提名、**并已由脚本验证过真实存在且可听**的替代曲。\n"
            "AI 不能凭记忆写歌曲 id —— 每一条都经过歌手优先解析 + 打假过滤 + "
            "privileges 可听性检查。\n\n")

    if ok:
        out += "### ✅ 可直接采用\n\n"
        out += "| 原曲（已下架） | → 替代 | 专辑 | 可听 | 匹配 |\n"
        out += "|---|---|---|---|---|\n"
        for r in ok:
            src = f"{r['title']} — {r['artist']}"
            dst = link(r.get("resolved_name") or r["title"], r["song_id"])
            out += (f"| {src} | {dst} | {r.get('album') or '-'} "
                    f"| {r.get('playable')} | {r.get('match', '-')}"
                    f" {r.get('confidence', '')} |\n")

    if hard:
        out += "\n### 🔀 需要你定夺\n\n"
        for r in hard:
            out += f"- **{r['title']} — {r['artist']}** ({r['status']})\n"
            if r.get("hint"):
                out += f"  - {r['hint']}\n"
            for c in (r.get("candidates") or [])[:5]:
                out += (f"  - {link(c['name'], c['sid'])} 《{c.get('album') or '-'}》"
                        f" — {c.get('playable')} ({c.get('playable_reason')})\n")

    if bad:
        out += "\n### ❌ 无法解析（保留原样，不静默替换）\n\n"
        for r in bad:
            out += f"- **{r['title']} — {r['artist']}** — {r.get('reason') or r['status']}\n"
            if r.get("hint"):
                out += f"  - {r['hint']}\n"
    return out


def render_grey(m: dict) -> str:
    if not m["grey"]:
        return h("已下架曲目") + "✅ 没有已下架曲目。\n"
    out = h(f"已下架曲目（{len(m['grey'])} 首）")
    out += "| 歌曲 | 歌手 | 专辑 | 年份 | 判定依据 |\n|---|---|---|---|---|\n"
    for g in m["grey"]:
        out += (f"| {link(g['name'], g['sid'])} | {'/'.join(g['artists'])} "
                f"| {g.get('album') or '-'} | {g.get('year') or '-'} "
                f"| `{g['reason']}` |\n")
    return out


def render_cleanup(m: dict) -> str:
    out = ""
    if m["covers"]:
        out += h(f"伪装成原唱的翻唱 / DJ 版（{len(m['covers'])} 首）")
        out += ("网易云用 `originCoverType` 标记：2=翻唱 3=DJ/remix。\n"
                "这些不代表你的作品趣味，只是版本选择 —— "
                "画像时应与原作区分开。\n\n")
        out += "| 歌曲 | 歌手 | 类型 |\n|---|---|---|\n"
        for c in m["covers"]:
            out += (f"| {link(c['name'], c['sid'])} | {'/'.join(c['artists'])} "
                    f"| {c['kind']} |\n")

    if m["unregistered_artist"]:
        out += h(f"疑似冒名条目（{len(m['unregistered_artist'])} 首）")
        out += ("这些条目的歌手 `id=0`，即没有对应的歌手实体，"
                "是 UGC 上传的自由文本歌手名。\n"
                "网易云搜索里大量存在用形近字冒名的条目"
                "（如 `周杰伦-`、`周杰伦.`）。\n\n")
        out += "| 歌曲 | 标注的歌手 |\n|---|---|\n"
        for c in m["unregistered_artist"]:
            out += f"| {link(c['name'], c['sid'])} | {'/'.join(c['artists'])} |\n"

    if m["duplicates"]:
        out += h(f"重复版本（{len(m['duplicates'])} 组）")
        out += "| 主标题 | 歌手 | 版本 |\n|---|---|---|\n"
        for g in m["duplicates"]:
            vers = " ／ ".join(
                f"{link(i['name'], i['sid'])}"
                f"{'`[' + '+'.join(i['version']) + ']`' if i['version'] else ''}"
                f" ({i['playable']})"
                for i in g["items"])
            out += f"| {g['base_title']} | {g['artist']} | {vers} |\n"
    return out


def render_distributions(m: dict) -> str:
    d = m["duration"]
    out = h("结构分布（脚本计算，非估算）")
    out += (f"**总时长** {d['total_human']} ／ "
            f"中位 {d['median_s']}s ／ 均值 {d['mean_s']}s\n\n")
    out += "| 时长区间 | 首数 | 占比 |\n|---|---:|---:|\n"
    for b in d["histogram"]:
        if b["count"]:
            out += f"| {b['bucket']} | {b['count']} | {b['pct']}% |\n"
    if d.get("peak_pct", 0) >= 35:
        out += (f"\n> ⚠️ {d['peak_pct']}% 的曲目挤在 {d['peak_bucket']} —— "
                f"节奏缺少变化。\n")

    a = m["artists"]
    out += h("歌手集中度", 3)
    out += f"{a['distinct']} 位歌手"
    if a.get("top5_pct") is not None:
        out += f"，Top5 占 {a['top5_pct']}%"
    if a.get("solo_artist_pct") is not None:
        out += f"，{a['solo_artist_pct']}% 的歌手只出现 1 次"
    out += "\n\n"
    for t in a["top"][:10]:
        out += f"- {t['name']} ×{t['count']}\n"

    e = m["era"]
    if e["decades"]:
        out += h("年代分布", 3)
        out += f"中位年份 **{e['median_year']}**"
        if e["unknown"]:
            out += f"（{e['unknown']} 首年份未知）"
        out += "\n\n| 年代 | 首数 |\n|---|---:|\n"
        for x in e["decades"]:
            out += f"| {x['decade']} | {x['count']} |\n"
    return out


def render_labels(m: dict, labels: dict, songs: dict) -> str:
    """标注分布 + 置信度。sid 交集限定在本歌单内。"""
    sids = {str(g["sid"]) for g in m["grey"]}
    # 本歌单全部 sid：从 metrics 拿不到，改从 inventory 按 src_pids 反查
    pid = (m.get("playlist") or {}).get("pid")
    mine = {sid for sid, s in songs.items()
            if pid is None or pid in (s.get("src_pids") or [])}
    rel = {sid: labels[sid] for sid in mine if sid in labels}
    if not rel:
        return ""

    out = h(f"AI 标注分布（{len(rel)}/{len(mine)} 首已标注）")

    def dist(dim: str, multi: bool) -> str:
        c: collections.Counter = collections.Counter()
        for r in rel.values():
            v = r.get(dim)
            if v is None:
                c["(未定)"] += 1
            elif multi:
                if not v:
                    c["(未定)"] += 1
                for x in v:
                    c[x] += 1
            else:
                c[v] += 1
        total = len(rel)
        body = f"\n**{dim}**\n\n| 标签 | 首数 | 占比 |\n|---|---:|---:|\n"
        for k, n in c.most_common():
            body += f"| {k} | {n} | {100 * n / total:.1f}% |\n"
        return body

    out += dist("lang", False)
    out += dist("genre", True)
    out += dist("mood", True)
    out += dist("scene", True)

    energies = [r["energy"] for r in rel.values() if r.get("energy")]
    if energies:
        c = collections.Counter(energies)
        out += "\n**energy（1=极静 5=极烈）**\n\n| 等级 | 首数 |\n|---|---:|\n"
        for k in sorted(c):
            out += f"| {k} | {c[k]} |\n"

    # ---- 置信度：这一节是护栏 F1/F8 的对外呈现 ----
    n = len(rel)
    null_genre = sum(1 for r in rel.values() if not r.get("genre"))
    ev = collections.Counter(r.get("evidence") for r in rel.values())
    unc = sum(1 for r in rel.values() if r.get("uncertain"))
    out += h("标注置信度", 3)
    out += (f"| 指标 | 值 |\n|---|---:|\n"
            f"| E1 有可核验依据 | {ev.get('E1', 0)} ({100 * ev.get('E1', 0) / n:.1f}%) |\n"
            f"| E2 基于歌手推断 | {ev.get('E2', 0)} ({100 * ev.get('E2', 0) / n:.1f}%) |\n"
            f"| E3 纯先验猜测 | {ev.get('E3', 0)} ({100 * ev.get('E3', 0) / n:.1f}%) |\n"
            f"| 标记为不确定 | {unc} |\n"
            f"| genre 未定（null） | {null_genre} ({100 * null_genre / n:.1f}%) |\n")
    out += ("\n> 本次未抓歌词，所以标注只基于歌名/歌手/专辑/年份。\n"
            "> **E3 的标签不作为诊断结论的主依据。**\n")
    if null_genre / n < 0.05:
        out += ("> ⚠️ genre 未定率低于 5% —— 这是**过度自信警报**。"
                "真实歌单里必然有模型说不清的曲目。\n")

    low = [(sid, r) for sid, r in rel.items() if r.get("evidence") == "E3"]
    if low:
        out += h(f"低置信标注（{len(low)} 首，仅供参考）", 3)
        out += "| 歌曲 | 歌手 | 备注 |\n|---|---|---|\n"
        for sid, r in low[:30]:
            s = songs.get(sid, {})
            out += (f"| {link(s.get('name', sid), int(sid))} "
                    f"| {'/'.join(a['name'] for a in s.get('ar') or [])} "
                    f"| {r.get('note') or '无可靠依据'} |\n")
    return out


def render_unresolved(unres: list) -> str:
    if not unres:
        return ""
    out = h(f"未解析条目（{len(unres)} 条）")
    out += "**这些没有被静默丢弃。** 它们是已知的盲区：\n\n"
    out += "| 条目 | 原因 |\n|---|---|\n"
    for u in unres:
        who = u.get("key") or f"sid {u.get('sid')}"
        out += f"| {who} | `{u.get('reason')}` |\n"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 Markdown 体检报告")
    ap.add_argument("--metrics", required=True, help="diagnose.py 产出的 metrics.json")
    ap.add_argument("--resolved", default=None, help="resolve_song.py 产出的 resolved.json")
    ap.add_argument("--diagnosis", default=None,
                    help="AI 写的诊断结论 markdown 片段")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    store.ensure_dirs()
    m = store.read_json(args.metrics)
    if not m:
        raise SystemExit(f"读不到 {args.metrics}")
    resolved = store.read_json(args.resolved, []) if args.resolved else []
    inv = store.load_inventory()
    cls = store.load_classified()
    unres = store.read_json(store.UNRESOLVED, []) or []

    pl = m.get("playlist") or {}
    title = pl.get("name") or "（全部入库歌曲）"

    md = f"# 歌单体检报告：{title}\n\n"
    md += f"- 歌单 id：`{pl.get('pid')}`\n"
    if pl.get("pid"):
        md += f"- 歌单链接：{N.playlist_url(pl['pid'])}\n"
    if pl.get("creator"):
        md += f"- 创建者：{pl['creator']}\n"
    md += (f"- 曲目数：**{m['total']}** ／ 总时长 **{m['duration']['total_human']}**\n"
           f"- 生成时间：{m.get('generated_at')}\n")
    md += ("\n> 本报告**只读**：没有登录你的账号，没有创建或修改任何线上歌单。\n"
           "> 所有歌曲名都是可点击的深链，直接跳网易云。\n")

    if args.diagnosis:
        diag = Path(args.diagnosis).read_text("utf-8").strip()
        if diag:
            md += h("诊断结论")
            md += diag + "\n"

    md += render_playability(m)
    md += render_grey(m)
    md += render_substitutions(resolved, m["grey"])
    md += render_cleanup(m)
    md += render_distributions(m)
    md += render_labels(m, cls.get("labels", {}), inv.get("songs", {}))
    md += render_unresolved(unres)

    md += h("方法与边界")
    md += (
        "本报告的**事实部分**（首数、时长、占比、可听性、翻唱与重复判定）"
        "全部由脚本计算，可复现。\n\n"
        "本报告的**判断部分**（曲风/情绪/场景标注、诊断结论、替代提名）"
        "由 AI 给出，因为网易云**不提供任何歌曲级的曲风/情绪/语种标签**"
        "（`displayTags`/`songFeature` 等字段实测全为 null），也不提供 "
        "BPM/调性/能量值。这部分不存在可查的数据源。\n\n"
        "关键约束：\n"
        "- AI **不能**凭记忆写歌曲 id。所有替代提名都经过"
        "「歌手优先解析 → 冒名/翻唱过滤 → privileges 可听性检查」三道验证。\n"
        "- 「需会员」不等于「已下架」。只有 `privileges.st < 0` 才判为下架。\n"
        "- 未解析的条目全部列出，不静默丢弃。\n"
    )

    out = args.out or (store.OUT / store.today() /
                       f"report_{pl.get('pid') or 'all'}.md")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(md, encoding="utf-8")
    print(f"✅ 报告已生成 → {out}")
    print(f"   {len(md.splitlines())} 行 / {len(md)} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
