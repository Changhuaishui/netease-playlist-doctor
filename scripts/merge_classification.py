#!/usr/bin/env python3
"""⑤ 收 AI 的标注答案 → 校验 → 写入 classified.json（增量）。

这是红线 R7 的执行点：**标签越界一律 exit 3 且不写入任何数据。**
宁可让 AI 重答一次，也不能让自造标签污染缓存。

同时执行两个自动护栏检测：
  F3 —— 同一歌手全部歌曲拿到完全相同标签集（artist-level 标签继承的指纹）
  F8 —— genre null 率过低（过度自信警报）

用法：
    merge_classification.py --batch batch_001 --answer answer.json
    cat answer.json | merge_classification.py --batch batch_001 --answer -
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import store

EXIT_VALIDATION = 3

# genre null 率低于此值 → 过度自信警报（护栏 F8）
MIN_NULL_RATE = 0.05


def die(msg: str) -> None:
    print(f"\n❌ [校验失败] {msg}", file=sys.stderr)
    print("   未写入任何数据。请修正后重新提交本批。", file=sys.stderr)
    sys.exit(EXIT_VALIDATION)


def load_answer(spec: str):
    raw = sys.stdin.read() if spec == "-" else Path(spec).read_text("utf-8")
    raw = raw.strip()
    if not raw:
        die("答案为空")
    # 容忍 ```json 围栏
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"答案不是合法 JSON: {exc}")
    if isinstance(data, dict):
        data = data.get("labels") or data.get("songs") or data.get("results")
    if not isinstance(data, list):
        die("答案顶层必须是数组，或含 labels/songs/results 数组的对象")
    return data


def validate(answer: list, batch: dict) -> list[dict]:
    space = batch["label_space"]
    songs = {s["i"]: s for s in batch["songs"]}
    expect_i = set(songs)

    got_i, errors, rows = set(), [], []

    for rec in answer:
        if not isinstance(rec, dict):
            errors.append(f"记录不是对象: {rec!r}")
            continue

        i = rec.get("i")
        if not isinstance(i, int) or i not in expect_i:
            errors.append(f"i={rec.get('i')!r} 不在本批索引范围 1..{len(songs)}")
            continue
        if i in got_i:
            errors.append(f"i={i} 重复出现")
            continue
        got_i.add(i)

        src = songs[i]
        # sid 必须回引且与批次一致（护栏 F2：禁止用歌名指代）
        sid = rec.get("sid")
        if sid is None:
            errors.append(f"i={i} 缺少 sid 回引")
            continue
        if int(sid) != int(src["sid"]):
            errors.append(
                f"i={i} sid 不符：批次是 {src['sid']}，答案是 {sid}"
                f"（歌名「{src['n']}」）")
            continue

        # ---- 单值维度 ----
        lang = rec.get("lang")
        if lang not in space["lang"]:
            errors.append(f"i={i} lang={lang!r} 不在 {space['lang']}")

        energy = rec.get("energy")
        if not isinstance(energy, int) or not 1 <= energy <= 5:
            errors.append(f"i={i} energy={energy!r} 必须是 1-5 的整数")

        ev = rec.get("evidence")
        if ev not in space["evidence"]:
            errors.append(f"i={i} evidence={ev!r} 不在 {space['evidence']}")

        uncertain = rec.get("uncertain")
        if not isinstance(uncertain, bool):
            errors.append(f"i={i} uncertain 必须是布尔值")
        elif ev == "E3" and not uncertain:
            errors.append(f"i={i} evidence=E3 必须同时 uncertain=true")

        # ---- 多值维度（genre/mood 允许 null）----
        for dim, maxn in (("genre", 3), ("mood", 3), ("scene", 3)):
            val = rec.get(dim)
            if val is None:
                if dim == "scene":
                    errors.append(f"i={i} scene 不得为 null，无场景请给 []")
                continue
            if not isinstance(val, list):
                errors.append(f"i={i} {dim} 必须是数组或 null，收到 {val!r}")
                continue
            if len(val) > maxn:
                errors.append(f"i={i} {dim} 最多 {maxn} 个，收到 {len(val)} 个")
            bad = [v for v in val if v not in space[dim]]
            if bad:
                errors.append(f"i={i} {dim} 含非法标签 {bad} —— "
                              f"合法取值见 label_space")

        rows.append(rec)

    missing = sorted(expect_i - got_i)
    if missing:
        errors.append(f"缺少 {len(missing)} 首的标注，i = {missing[:20]}"
                      + (" …" if len(missing) > 20 else ""))

    if errors:
        print(f"\n共 {len(errors)} 处问题：", file=sys.stderr)
        for e in errors[:40]:
            print(f"  • {e}", file=sys.stderr)
        if len(errors) > 40:
            print(f"  … 另有 {len(errors) - 40} 处", file=sys.stderr)
        die("标注未通过校验")

    return rows


def guardrails(labels: dict, inv: dict) -> None:
    """写入后的自动护栏检测。只警告，不阻断。"""
    # ---- F3: artist-level 标签继承 ----
    by_artist: dict[str, list[tuple]] = collections.defaultdict(list)
    for sid, rec in labels.items():
        song = inv["songs"].get(str(sid))
        if not song:
            continue
        primary = (song.get("ar") or [{}])[0].get("name")
        if not primary:
            continue
        sig = (rec.get("lang"),
               tuple(sorted(rec.get("genre") or [])),
               tuple(sorted(rec.get("mood") or [])),
               rec.get("energy"))
        by_artist[primary].append(sig)

    suspects = [(a, sigs) for a, sigs in by_artist.items()
                if len(sigs) >= 4 and len(set(sigs)) == 1]
    if suspects:
        print(f"\n⚠️  护栏 F3：{len(suspects)} 位歌手的所有歌曲拿到了"
              f"**完全相同**的标签集 —— 这是把歌手整体风格下推到单曲的指纹：")
        for a, sigs in suspects[:8]:
            print(f"     {a} ×{len(sigs)} 全部标成 {sigs[0]}")
        print("     请复核这些歌手风格迥异的单曲（如周杰伦《以父之名》vs《晴天》）。")

    # ---- F8: null 率过低 ----
    n = len(labels)
    if n >= 20:
        null_genre = sum(1 for r in labels.values() if not r.get("genre"))
        e3 = sum(1 for r in labels.values() if r.get("evidence") == "E3")
        rate = null_genre / n
        print(f"\n置信度分布：genre null {null_genre}/{n} ({rate:.1%})，"
              f"E3 低置信 {e3}/{n} ({e3 / n:.1%})")
        if rate < MIN_NULL_RATE:
            print(f"⚠️  护栏 F8：genre null 率 {rate:.1%} 低于 {MIN_NULL_RATE:.0%} —— "
                  f"**这是过度自信警报，不是好事**。")
            print("     真实华语歌单里必然有一批模型说不清的网络歌曲/小众独立。")
            print("     全部填满通常说明在编。请收紧证据等级后复核低 pop 曲目。")


def main() -> int:
    ap = argparse.ArgumentParser(description="校验并写入 AI 标注（红线 R7 执行点）")
    ap.add_argument("--batch", required=True, help="批次 id，如 batch_001")
    ap.add_argument("--answer", required=True, help="答案 JSON 文件，或 - 读 stdin")
    ap.add_argument("--batch-dir", default=None)
    args = ap.parse_args()

    store.ensure_dirs()
    bdir = Path(args.batch_dir) if args.batch_dir else store.BATCHES
    bpath = bdir / f"{args.batch}.json"
    batch = store.read_json(bpath)
    if not batch:
        raise SystemExit(f"读不到批次 {bpath}")

    answer = load_answer(args.answer)
    rows = validate(answer, batch)

    inv = store.load_inventory()
    cls = store.load_classified()
    labels = cls.setdefault("labels", {})

    tv = batch["taxonomy_version"]
    for rec in rows:
        sid = str(int(rec["sid"]))
        song = inv["songs"].get(sid) or {}
        labels[sid] = {
            "lang": rec.get("lang"),
            "genre": rec.get("genre"),
            "mood": rec.get("mood"),
            "scene": rec.get("scene") or [],
            "energy": rec.get("energy"),
            "evidence": rec.get("evidence"),
            "uncertain": rec.get("uncertain"),
            "note": rec.get("note") or "",
            "batch_id": batch["batch_id"],
            "taxonomy_version": tv,
            "input_hash": song.get("input_hash"),
            "classified_at": store.now_iso(),
        }
    cls["taxonomy_version"] = tv
    store.save_classified(cls)

    # 更新批次进度
    idx = store.read_json(bdir / "index.json")
    if idx:
        for b in idx.get("batches", []):
            if b["batch_id"] == batch["batch_id"]:
                b["status"] = "done"
                b["merged_at"] = store.now_iso()
        store.write_json(bdir / "index.json", idx)
        done = sum(1 for b in idx["batches"] if b["status"] == "done")
        print(f"✅ {batch['batch_id']} 已写入 {len(rows)} 首"
              f"（进度 {done}/{len(idx['batches'])} 批）")
        pending = [b["batch_id"] for b in idx["batches"]
                   if b["status"] != "done"]
        if pending:
            print(f"   下一批：{pending[0]}")
        else:
            print("   ✅ 全部批次完成 → 跑 export_report.py")
    else:
        print(f"✅ {batch['batch_id']} 已写入 {len(rows)} 首")

    guardrails(labels, inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
