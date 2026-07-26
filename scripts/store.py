"""状态目录读写 —— 与技能目录分离，技能可重装而数据不丢。

布局见 SKILL.md。所有路径可用 NCM_DOCTOR_HOME 覆盖（测试用）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

HOME = Path(os.environ.get("NCM_DOCTOR_HOME", Path.home() / ".ncm-doctor"))

RAW = HOME / "raw"
CATALOG = HOME / "catalog"
BATCHES = HOME / "batches"
OUT = HOME / "out"

INVENTORY = HOME / "inventory.json"
CLASSIFIED = HOME / "classified.json"
ARTISTS = HOME / "artists.json"
UNRESOLVED = HOME / "unresolved.json"

TAXONOMY_VERSION = "v1"


def ensure_dirs() -> None:
    for d in (HOME, RAW, CATALOG, BATCHES, OUT):
        d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def read_json(path: Path, default=None):
    if not Path(path).exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data) -> None:
    """原子写：先写 .tmp 再 replace，避免中断留下半个文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


# --------------------------------------------------------------------------
# inventory（事实层）
# --------------------------------------------------------------------------

def load_inventory() -> dict:
    return read_json(INVENTORY, {"version": 1, "playlists": {}, "songs": {}})


def save_inventory(inv: dict) -> None:
    inv["updated_at"] = now_iso()
    write_json(INVENTORY, inv)


def input_hash(song: dict) -> str:
    """标注缓存的失效依据。

    歌名/歌手/专辑/年份任一变化（官方改名、换专辑归属）→ hash 变 →
    该首自动重新标注。歌单增删歌不影响已有条目。
    """
    parts = [
        song.get("name") or "",
        "|".join(a.get("name", "") for a in (song.get("ar") or [])),
        (song.get("al") or {}).get("name") or "",
        str(song.get("year") or ""),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# classified（判断层）—— 与事实层物理分离，重拉歌单不损失标签
# --------------------------------------------------------------------------

def load_classified() -> dict:
    return read_json(CLASSIFIED, {
        "taxonomy_version": TAXONOMY_VERSION, "labels": {},
    })


def save_classified(cls: dict) -> None:
    cls["updated_at"] = now_iso()
    write_json(CLASSIFIED, cls)


def needs_labeling(sid: str, song: dict, labels: dict) -> bool:
    """增量命中三要素：sid 存在 且 input_hash 相同 且 taxonomy_version 相同。"""
    rec = labels.get(str(sid))
    if not rec:
        return True
    if rec.get("input_hash") != input_hash(song):
        return True
    if rec.get("taxonomy_version") != TAXONOMY_VERSION:
        return True
    return False


def load_artists() -> dict:
    return read_json(ARTISTS, {})


def save_artists(data: dict) -> None:
    write_json(ARTISTS, data)
