"""Match an ordered catalog plan against one target playlist snapshot."""

from __future__ import annotations

import json
import math
import re
import subprocess
import unicodedata
from collections import defaultdict
from dataclasses import dataclass


CJK = re.compile(r"[\u3400-\u9fff]")
SUFFIX = re.compile(
    r"\s*[（(\[【].*$|\s+[-–—]\s+.*$|"
    r"\s+(?:(?:\d{4}\s+)?(?:live|remaster(?:ed)?|re-release|remix|acoustic|demo)\b.*)$|"
    r"\s*(?:现场版?|現場版?|重录版?|重錄版?|重制版|重製版)$", re.I,
)


def clean(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def simplified_labels(labels):
    """Use the same macOS Foundation transform as shortlist.js. No Music events."""
    labels = sorted({label for label in labels if CJK.search(label)})
    if not labels:
        return {}
    source = """
        ObjC.import("Foundation");
        const bytes = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
        const input = ObjC.unwrap($.NSString.alloc.initWithDataEncoding(bytes, $.NSUTF8StringEncoding));
        const converted = JSON.parse(input).map(s =>
            ObjC.unwrap($(s).stringByApplyingTransformReverse("Traditional-Simplified", false)));
        JSON.stringify(converted);
    """
    process = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", source],
        input=json.dumps(labels), text=True, capture_output=True, check=True, timeout=30,
    )
    converted = json.loads(process.stdout)
    if len(converted) != len(labels) or any(not isinstance(s, str) for s in converted):
        raise ValueError("无法核对曲目文字的简繁变体")
    return dict(zip(labels, converted))


@dataclass(frozen=True)
class MatchKeys:
    fields: tuple
    loose: tuple
    base: str
    seconds: float | None
    catalog_id: str
    local_id: str


def match_keys(track, seconds_key, folds):
    """Keep full keys for identity. Loose keys only flag possible collisions."""
    fields = tuple(clean(track.get(k)) for k in ("name", "artist", "album"))
    loose = tuple("".join(c for c in unicodedata.normalize("NFKC", folds.get(s, s))
                          if c.isalnum()) for s in fields)
    title = folds.get(fields[0], fields[0])
    base = "".join(c for c in SUFFIX.sub("", unicodedata.normalize("NFKC", title)) if c.isalnum())
    duration = track.get(seconds_key)
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        duration = None
    elif seconds_key == "duration_ms":
        duration /= 1000
    catalog_id = str(track.get("catalog_id", ""))
    if not re.fullmatch(r"[1-9]\d*", catalog_id, re.ASCII):
        catalog_id = ""
    local_key = "library_id" if seconds_key == "duration_ms" else "persistentID"
    local_id = (track.get(local_key) or "").strip().upper()
    return MatchKeys(fields, loose, base, duration, catalog_id, local_id)


def evidence(plan, row):
    same_local = bool(plan.local_id and plan.local_id == row.local_id)
    if plan.catalog_id and row.catalog_id:
        if plan.catalog_id == row.catalog_id:
            return "id"
        return "conflict" if same_local else None
    if same_local:
        return "id"

    close = None if plan.seconds is None or row.seconds is None else abs(plan.seconds - row.seconds) <= 2
    if all(plan.fields) and close is not False and row.local_id:
        title, artist, album = plan.fields
        row_title, row_artist, row_album = row.fields
        # Apple Music's song page can join multiple artist links with commas
        # while the Mac client displays the same credits with an ampersand.
        artists_match = artist == row_artist or (
            (", " in artist and " & " in row_artist or " & " in artist and ", " in row_artist)
            and re.split(r", | & ", artist) == re.split(r", | & ", row_artist)
        )
        if title == row_title and album == row_album and artists_match:
            return "metadata"
    if not plan.fields[0] or not row.fields[0] or (plan.base and plan.base == row.base):
        return "possible"
    # A translated title can share no characters with the storefront title.
    artist_matches = bool(plan.loose[1] and plan.loose[1] == row.loose[1])
    album_matches = bool(plan.loose[2] and plan.loose[2] == row.loose[2])
    cross_script = bool(CJK.search(plan.fields[0])) != bool(CJK.search(row.fields[0]))
    if close and (artist_matches and album_matches or cross_script and (artist_matches or album_matches)):
        return "possible"
    if artist_matches and album_matches and cross_script:
        return "possible"
    return None


def reconcile(tracks, snapshot_tracks):
    """Return present/missing/review in plan order. Match IDs before text."""
    labels = (clean(row.get(key)) for rows in (tracks, snapshot_tracks)
              for row in rows for key in ("name", "artist", "album"))
    folds = simplified_labels(labels) if tracks and snapshot_tracks else {}
    plans = [match_keys(track, "duration_ms", folds) for track in tracks]
    rows = [match_keys(row, "duration", folds) for row in snapshot_tracks]
    choices = []
    claimants = defaultdict(list)
    for plan in plans:
        groups = defaultdict(list)
        for j, row in enumerate(rows):
            kind = evidence(plan, row)
            if kind:
                groups[kind].append(j)
        # A stale saved local ID that now points to another catalog ID needs review,
        # even if this snapshot also contains the intended catalog recording.
        kind = next((key for key in ("conflict", "id", "metadata", "possible") if groups[key]), None)
        matches = groups[kind]
        choices.append((kind, matches))
        for j in matches:
            claimants[rows[j].local_id or j].append(kind)

    results = []
    for track, plan, (kind, matches) in zip(tracks, plans, choices):
        result = {"catalog_id": track.get("catalog_id"), "status": "review"}
        if kind == "conflict":
            result["reason"] = "已保存的资料库 ID 与快照中的目录 ID 冲突"
        elif kind is None and all(plan.fields):
            result.update(status="missing", reason="目标歌单未找到这首录音")
        elif kind is None:
            result["reason"] = "计划缺少曲名、艺人或专辑"
        elif kind == "possible":
            result["reason"] = "疑似同曲，需人工核对曲名、艺人、专辑版本或时长"
        elif len(matches) != 1:
            result["reason"] = "有多条记录匹配同一首计划曲目"
        else:
            j = matches[0]
            local_id = rows[j].local_id
            claims = claimants[local_id or j]
            if len(claims) > 1 and (kind != "id" or claims.count("id") > 1 or "conflict" in claims):
                result["reason"] = "同一条资料库记录可能属于多首计划曲目"
            else:
                result.update(status="present", reason="ID 匹配" if kind == "id" else "曲名、艺人和专辑匹配")
                if local_id:
                    result["persistentID"] = local_id
        results.append(result)
    return results
