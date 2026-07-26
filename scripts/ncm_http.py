"""网易云音乐只读客户端 —— 确定性层（L1）共享底层。

设计约束（全部来自实测，见 references/api-endpoints.md）：
  * 只用 stdlib（本机无 requests / pycryptodome / cryptography）
  * 只读：不登录、不带 cookie、不写任何线上数据
  * 所有请求必须带 UA + Referer，否则部分端点返回异常

本模块不提供 CLI，由其他脚本 import。
"""

from __future__ import annotations

import json
import random
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

API = "https://music.163.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}

# artist/songs 端点在 limit>100 时会静默丢数据（实测 limit=300 只收 400/566 首）。
# 这是红线 R4，任何调用方不得覆盖。
ARTIST_SONGS_PAGE = 100

# 批量 song/detail 单次上限。用 POST 提交，避免 2000 首时 URL 超长 414。
SONG_DETAIL_CHUNK = 200


class NcmError(RuntimeError):
    """端点返回了非 200 的业务 code，或结构与预期不符。"""


class EndpointChanged(NcmError):
    """响应缺少预期字段 —— 通常意味着网易云改了接口（例如加了签名校验）。"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(url: str, data: bytes | None, timeout: int, retries: int) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        if attempt:
            # 指数退避 + 抖动，避免撞上风控
            time.sleep(min(2 ** attempt, 8) * (0.8 + 0.4 * random.random()))
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        except urllib.error.HTTPError as exc:  # pragma: no cover - 4xx/5xx
            last_err = exc
            if exc.code in (400, 404):
                break
    raise NcmError(f"请求失败（重试 {retries} 次）: {url} -> {last_err!r}") from last_err


def api_get(path: str, params: dict | None = None, *, timeout: int = 20,
            retries: int = 3) -> dict:
    """GET 一个 /api/... 端点，返回已解析的 JSON。"""
    url = f"{API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _check_code(_request(url, None, timeout, retries), path)


def api_post(path: str, form: dict, *, timeout: int = 20, retries: int = 3) -> dict:
    """POST 一个 /api/... 端点（application/x-www-form-urlencoded）。"""
    body = urllib.parse.urlencode(form).encode("utf-8")
    return _check_code(_request(f"{API}{path}", body, timeout, retries), path)


def _check_code(payload: dict, path: str) -> dict:
    code = payload.get("code")
    if code is not None and code != 200:
        msg = payload.get("message") or payload.get("msg") or ""
        raise NcmError(f"{path} 返回 code={code} {msg}".strip())
    return payload


def throttle(lo: float = 1.2, hi: float = 2.5) -> None:
    """请求之间的礼貌等待（带抖动）。批量循环里每次迭代都该调。"""
    time.sleep(random.uniform(lo, hi))


# --------------------------------------------------------------------------
# 分享链接 → 歌单 id
# --------------------------------------------------------------------------

_ID_RE = re.compile(r"[?&]id=(\d+)")


def resolve_share_link(url: str) -> int:
    """从各种形态的分享链接里取出歌单 id。

    实测覆盖：
      https://music.163.com/playlist?id=N
      https://music.163.com/#/playlist?id=N
      https://y.music.163.com/m/playlist?id=N
      http://163cn.tv/xxxx        （302 短链，需跟随重定向）
      纯数字                       （直接当 id）
    """
    url = url.strip()
    if url.isdigit():
        return int(url)

    m = _ID_RE.search(url)
    if m:
        return int(m.group(1))

    # 短链：跟随重定向后再解析。urllib 默认跟随 3xx。
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            final = resp.geturl()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NcmError(f"短链解析失败: {url} -> {exc!r}") from exc

    m = _ID_RE.search(final)
    if not m:
        raise NcmError(f"无法从链接中解析出歌单 id: {url}（最终地址 {final}）")
    return int(m.group(1))


# --------------------------------------------------------------------------
# 标题归一化
# --------------------------------------------------------------------------

# 版本标记：命中则剥离，并作为 tag 返回。顺序不敏感，全部小写匹配。
_VERSION_TAGS = [
    ("live", ["live", "演唱会", "现场", "unplugged"]),
    ("instrumental", ["伴奏", "instrumental", "纯音乐", "off vocal", "karaoke"]),
    ("piano", ["钢琴版", "钢琴曲", "piano version"]),
    ("guitar", ["吉他版", "guitar version"]),
    ("remix", ["remix", "dj版", "dj", "混音"]),
    ("cover", ["cover", "翻唱", "原唱"]),
    ("acoustic", ["acoustic", "不插电"]),
    ("remaster", ["remaster", "remastered", "重制", "复刻"]),
    ("sped", ["加速版", "sped up", "变速"]),
    ("slowed", ["慢速版", "slowed"]),
    ("official", ["正式版", "official"]),
    ("demo", ["demo", "试听版"]),
    ("edit", ["radio edit", "single version", "album version"]),
    # 换声别翻唱：网易云上极常见，且几乎都是 UGC 翻唱
    ("revocal", ["女声版", "女生版", "男声版", "男生版", "深情版", "伤感版", "对唱版"]),
]

_BRACKETS = re.compile(r"[（(\[【{][^）)\]】}]*[）)\]】}]")
_FEAT = re.compile(r"\s*(feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)


def norm_title(title: str) -> tuple[str, list[str]]:
    """把歌名归一成「主标题 + 版本标记」。

    >>> norm_title("晴天 (Live)")
    ('晴天', ['live'])
    >>> norm_title("晴天（钢琴版）(原唱：周杰伦)")
    ('晴天', ['cover', 'piano'])

    归一化清单写死在这里，禁止调用方（尤其 AI）临场发挥另一套规则。
    """
    if not title:
        return "", []

    # 全角 → 半角，兼容 NFKC 下的各种变体字符
    s = unicodedata.normalize("NFKC", title)

    tags: set[str] = set()

    # 先从括号内容里提取版本标记，再整体去掉括号
    for chunk in _BRACKETS.findall(s):
        low = chunk.lower()
        for tag, needles in _VERSION_TAGS:
            if any(n in low for n in needles):
                tags.add(tag)
    s = _BRACKETS.sub(" ", s)

    # 括号外裸写的版本标记（如 "晴天 - DJ版"）
    low = s.lower()
    for tag, needles in _VERSION_TAGS:
        for n in needles:
            if n in low:
                tags.add(tag)
                low = low.replace(n, " ")
    s = low

    s = _FEAT.sub("", s)
    # 去掉配对括号后可能残留的孤立括号（如 "晴天 (女声版))" 里多出的那个）
    s = re.sub(r"[（()\[\]【】{}）]+", " ", s)
    s = re.sub(r"[\s\-_/·,，、.。!！?？~～'\"“”‘’]+", " ", s)
    return s.strip(), sorted(tags)


def title_similarity(a: str, b: str) -> float:
    """归一化后的标题相似度，[0,1]。用于 resolve_song 的三级匹配第 3 级。"""
    na, _ = norm_title(a)
    nb, _ = norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# --------------------------------------------------------------------------
# 打假：冒名歌手 / 翻唱（红线 R2）
# --------------------------------------------------------------------------

def artist_ids(song: dict) -> list[int]:
    """取出歌曲的歌手 id 列表，兼容 cloudsearch(ar) 与 artist/songs(artists) 两套字段名。"""
    raw = song.get("ar")
    if raw is None:
        raw = song.get("artists") or []
    out = []
    for a in raw:
        try:
            out.append(int(a.get("id", 0)))
        except (TypeError, ValueError):
            out.append(0)
    return out


def is_impostor(song: dict, expect_artist_id: int) -> bool:
    """判断搜索结果是否为冒名条目。

    实测依据（references/api-endpoints.md 事实 7）：
    搜「晴天 周杰伦」返回的 `周杰伦-` `周杰伦.` `周杰伦、` 等形近字冒名歌手，
    其 ar[].id 恒为 0（UGC 自由文本，没有 artist 实体）；
    真身 186016 的 ar 是 [{6452, '周杰伦'}]。

    规则：期望的 artist_id 不在歌手 id 列表里 → 冒名。
    这条同时覆盖了 id==0 的情况（0 永远不等于任何真实 artist_id）。
    """
    return int(expect_artist_id) not in artist_ids(song)


def is_cover(song: dict) -> bool:
    """originCoverType: 1=原唱 2=翻唱 3=DJ/remix。

    注意（事实 8）：冒名条目的 oct 是 0，所以本规则**抓不到冒名**，
    必须与 is_impostor 并用。oct=0 表示"未标注"，不能据此判定。
    """
    return song.get("originCoverType") in (2, 3)


# --------------------------------------------------------------------------
# 可听性裁定（红线 R3）—— 唯一裁定源
# --------------------------------------------------------------------------

def playability(priv: dict | None) -> tuple[str, str]:
    """依据 privileges 判定可听性，返回 (verdict, reason)。

    verdict ∈ {ok, vip_limited, album_only, grey, unknown}

    为什么必须看 privileges 而不是歌曲字段（事实 12）：
    《晴天》186016 的歌曲级字段是 fee=0, st=0, copyright=1，看着完全正常，
    但 privileges[0] 是 st=-200, pl=0, plLevel="none" —— 实际已下架灰掉。
    AI 不得自行解读 fee=0 就当能听。

    ⚠️ 匿名访问的关键约束（事实 12b，实测热歌榜 201 首得出）：
    我们不登录，所以**所有 VIP 曲目的 pl/dl 恒为 0**。实测 129/201 首是
    `st=0, fee=1, pl=0, dl=0` —— 它们是「需要会员」而**不是**「已下架」。
    因此 `pl==0` 绝不能单独作为下架依据，否则会把整个歌单的 VIP 歌
    误报成下架（这会让体检报告完全失真）。

    → **`st < 0` 是匿名访问下唯一可信的下架信号。**

    fee 语义（实测）：0=完全免费  1=VIP会员  4=数字专辑需购买  8=免费/试听
    """
    if not priv:
        return "unknown", "无 privileges 数据"

    st = priv.get("st")
    pl = priv.get("pl")
    dl = priv.get("dl")
    fee = priv.get("fee")

    # ① 唯一可信的下架信号，必须最先判
    if isinstance(st, int) and st < 0:
        return "grey", f"priv.st={st}（已下架）"

    # ② 付费墙：匿名下 pl 必为 0，所以要在看 pl 之前判 fee
    if fee == 4:
        return "album_only", "priv.fee=4（数字专辑需购买）"
    if fee == 1:
        return "vip_limited", "priv.fee=1（需会员，匿名不可播）"

    # ③ 能拿到播放码率 → 可听
    if isinstance(pl, int) and pl > 0:
        return "ok", f"priv.st=0 且 priv.pl={pl}"

    # ④ st=0 且非付费墙却拿不到码率：地区限制等未知原因，不当作下架
    return "unknown", f"未覆盖组合 st={st} pl={pl} dl={dl} fee={fee}"


PLAYABLE_OK = {"ok", "vip_limited"}


# --------------------------------------------------------------------------
# 端点封装（只读）
# --------------------------------------------------------------------------

def playlist_detail(pid: int) -> dict:
    """歌单详情。注意只返回前 10 首 tracks，全量在 trackIds（事实 2）。"""
    data = api_get("/api/v6/playlist/detail", {"id": pid})
    pl = data.get("playlist")
    if not isinstance(pl, dict) or "trackIds" not in pl:
        raise EndpointChanged(
            "/api/v6/playlist/detail 响应缺少 playlist.trackIds —— 接口可能已变更"
        )
    return pl


def song_detail(ids: list[int]) -> tuple[list[dict], list[dict]]:
    """批量取歌曲详情，返回 (songs, privileges)。单批上限 SONG_DETAIL_CHUNK。

    用 POST 提交 c= 参数，避免大批量时 URL 414（事实 3）。
    """
    if not ids:
        return [], []
    if len(ids) > SONG_DETAIL_CHUNK:
        raise ValueError(f"单批最多 {SONG_DETAIL_CHUNK} 首，收到 {len(ids)}")
    c = json.dumps([{"id": int(i)} for i in ids], separators=(",", ":"))
    data = api_post("/api/v3/song/detail", {"c": c})
    if "songs" not in data:
        raise EndpointChanged("/api/v3/song/detail 响应缺少 songs —— 接口可能已变更")
    return data.get("songs") or [], data.get("privileges") or []


def search_artists(name: str, limit: int = 10) -> list[dict]:
    """歌手搜索（type=100）。真身可用 musicSize/albumSize/alias 与冒名者区分（事实 9）。"""
    data = api_post("/api/cloudsearch/pc", {"s": name, "type": 100, "limit": limit,
                                            "offset": 0})
    return (data.get("result") or {}).get("artists") or []


def search_songs(keyword: str, limit: int = 30) -> list[dict]:
    """歌曲搜索（type=1）。

    警告：结果污染极严重（事实 5）——搜「晴天 周杰伦」前 30 条 0 条是真的。
    调用方**必须**用 is_impostor + is_cover 过滤，这是红线 R2。
    """
    data = api_post("/api/cloudsearch/pc", {"s": keyword, "type": 1, "limit": limit,
                                            "offset": 0})
    return (data.get("result") or {}).get("songs") or []


def artist_songs(artist_id: int, *, order: str = "hot",
                 max_pages: int = 60) -> list[dict]:
    """拉取歌手全量曲库，自动分页到 more==false。

    红线 R4：page size 恒为 100。实测 limit=300 时分页会静默丢数据
    （周杰伦 566 首只收到 400 首），limit=100 循环才收满（事实 11）。

    返回的条目用 album/artists/duration 字段名（不是 al/ar/dt）。
    """
    out: list[dict] = []
    offset = 0
    for page in range(max_pages):
        if page:
            throttle(0.6, 1.4)
        data = api_get("/api/v2/artist/songs", {
            "id": artist_id, "order": order,
            "limit": ARTIST_SONGS_PAGE, "offset": offset,
        })
        if "songs" not in data:
            raise EndpointChanged(
                "/api/v2/artist/songs 响应缺少 songs —— 接口可能已变更"
            )
        batch = data.get("songs") or []
        out.extend(batch)
        if not data.get("more") or not batch:
            break
        offset += ARTIST_SONGS_PAGE
    return out


# --------------------------------------------------------------------------
# 杂项
# --------------------------------------------------------------------------

def chunked(seq, n: int):
    """把序列切成每 n 个一组。"""
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def song_url(sid: int) -> str:
    """歌曲深链。用户点它跳网易云 —— 这是本 skill 的唯一交付通道。"""
    return f"https://music.163.com/song?id={sid}"


def playlist_url(pid: int) -> str:
    return f"https://music.163.com/#/playlist?id={pid}"


def ms_to_mmss(ms: int | None) -> str:
    if not ms:
        return "-"
    total = int(ms) // 1000
    return f"{total // 60}:{total % 60:02d}"


def year_of(publish_time: int | None) -> int | None:
    """publishTime 是毫秒时间戳；0 表示未知。"""
    if not publish_time:
        return None
    try:
        return time.gmtime(int(publish_time) / 1000).tm_year
    except (ValueError, OSError, OverflowError):
        return None
