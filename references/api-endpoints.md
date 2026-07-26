# 网易云音乐只读接口 —— 实测记录与陷阱

> 全部结论均为 **2026-07-26 本机 curl 实测**，不是文档推测。
> 修改 `scripts/ncm_http.py` 前必读本文，尤其「反例」小节。

所有请求必须带：

```
User-Agent: Mozilla/5.0 (...)
Referer: https://music.163.com/
```

不带 Referer 时部分端点行为异常。**全部只读，不带 cookie。**

---

## 1. 歌单详情

```
GET /api/v6/playlist/detail?id=<pid>
```

免登录可用，`code: 200`。

**陷阱 ①：只返回前 10 首。**
实测热歌榜（pid 3778678）：`trackCount=200`，但 `playlist.tracks` 只有 **10** 条，
`playlist.trackIds` 有 **200** 条。

> 全量歌曲必须走 `trackIds` → 批量 `song/detail` 补全。
> 直接用 `tracks` 会静默只处理 5% 的歌单。

断言：`len(playlist.trackIds) == playlist.trackCount`，不等即中止。

歌单级 `tags` 字段存在（如 `华语` `摇滚` `伤感`），但**经常为空**——
连官方「热歌榜」的 `tags` 都是 `[]`。不可依赖。

---

## 2. 批量歌曲详情

```
POST /api/v3/song/detail
Content-Type: application/x-www-form-urlencoded
c=[{"id":186016},{"id":1973665667}]
```

返回 `{songs: [...], privileges: [...]}`，免登录可用。

**用 POST 而不是 GET**：2000 首歌分批时 GET 的 URL 会超长触发 414。
单批上限设为 200。

断言：`len(songs) + len(missing) == len(requested)`。

---

## 3. ⚠️ 歌曲级曲风/情绪/语种标签根本不存在

这是整个 skill 的立论基础。实测多首歌，以下字段**全部**为 `null` 或 `[]`：

| 字段 | 实测值 |
|---|---|
| `displayTags` | `None` |
| `awardTags` | `None` |
| `entertainmentTags` | `None` |
| `songFeature` | `None` |
| `tagPicList` | `None` |
| `markTags` | `[]` |

纯代码能拿到的全部有效字段，穷尽了就是：

```
name, ar[].name, ar[].id, al.name, dt(时长), pop(热度),
publishTime, fee, originCoverType
```

**没有曲风、没有情绪、没有语种、没有 BPM、没有调性、没有能量值。**
不像 Spotify，网易云不提供任何音频特征。

> 所以"按曲风分类"在纯代码方案里不是"实现得糙一点"，
> 而是**一个没有查询表的查表问题**。
> AI 标注不是锦上添花，是唯一可行解。

---

## 4. ⚠️⚠️ 歌曲搜索结果污染极严重

```
POST /api/cloudsearch/pc
s=晴天 周杰伦&type=1&limit=30
```

实测：**前 30 条里 0 条是真的。**

真身《晴天》id=186016（周杰伦 / 叶惠美 / 2003）**根本不在结果里**。
返回的全是：

| id | pop | ar 里的歌手 | 说明 |
|---|---:|---|---|
| 3339230677 | 95 | `周杰伦-`(id **0**), `A-LNK` | 形近字冒名 |
| 3334653818 | 95 | `周杰伦.`(id **0**), `Asasblue` | 形近字冒名 |
| 3324852509 | 70 | `B-KLl`, `周杰伦、`(id **0**) | 形近字冒名 |
| 2668397359 | 100 | `RyaVocal` | 翻唱（标题写"原唱 周杰伦"） |
| 2730151393 | 95 | `DJ阿罗` | DJ 版 |
| 1945894789 | 100 | `纪钧瀚` | 钢琴版 |

**`pop` 完全不能用来去伪** —— 假货 pop 也是 95~100，和真身同档甚至更高。

### 打假规则（两条必须并用）

```python
is_impostor(song, 6452)  # expect_artist_id 不在 ar[].id 里 → 冒名
is_cover(song)           # originCoverType ∈ {2,3} → 翻唱/DJ版
```

`originCoverType` 语义实测：

| oct | 含义 | 实例 |
|---:|---|---|
| 1 | 原唱 | 186016 真《晴天》 |
| 2 | 翻唱 | 2668397359 RyaVocal |
| 3 | DJ/remix | 2730151393 DJ阿罗 |
| **0** | **未标注** | **3339230677 冒名条目** |

> **关键陷阱**：冒名条目的 `oct` 是 **0**，不是 2。
> 所以 `is_cover` 单独用**抓不到冒名**，`is_impostor` 单独用抓不到 DJ阿罗
> （他是注册歌手 id=98666443，真实存在）。**两条规则必须并用。**

---

## 5. 歌手搜索（可靠）

```
POST /api/cloudsearch/pc
s=周杰伦&type=100&limit=5
```

实测：

| id | name | musicSize | albumSize | alias |
|---|---|---:|---:|---|
| **6452** | 周杰伦 | **568** | **41** | `["Jay Chou","周董"]` |
| 51128649 | 周杰伦. | 20 | 2 | `["周杰伦头号粉"]` |
| 122494823 | 周杰伦♚ | 12 | 5 | `["周杰伦"]` ← **别名与真名完全相同** |
| 52198464 | 周杰伦jay | 3 | 2 | `[]` |

真身可用 `musicSize`/`albumSize`/`alias` 区分。
注意 `周杰伦♚` 的 alias 精确等于真名，**名字匹配必败，只有体量能分**。

### 但「取体量最大者」这个启发式会输 —— 李志案例

搜「李志」实测：

| id | name | musicSize |
|---|---|---:|
| 33603544 | 李志 | **1** |
| 121462061 | 李志 | **2** |
| 4047 | 李志辉 | 290 |
| 4063 | 李志洲 | 784 |

真李志 2019 年全面下架，**在网易云已归零**。
剩下两个精确同名者是 musicSize 1 和 2 的随机路人。

> 纯代码的"取 musicSize 最大者"会选中 `李志洲`(784 首) —— 完全不同的歌手。
> 就算限定精确同名，也会选中那个 2 首歌的路人。
> **这不是"效果不好"，这是静默输出垃圾。**
>
> 需要知道"李志已被下架、这两个都不是他"才能正确放弃。这是世界知识。
> → 所以歌手真身认定是 **AI 决策点 1**，脚本只排序打分不自动选。

---

## 6. ⚠️⚠️ 歌手曲库 —— limit>100 会静默丢数据

```
GET /api/v2/artist/songs?id=6452&order=hot&limit=100&offset=<n>
```

**这是唯一能找到已下架真歌的通道。** 真《晴天》186016 在这里能命中，
而 `cloudsearch` 里完全找不到。

### 分页 bug 实测

| page size | 分页到 more=false 后收到 | 实际 total |
|---:|---:|---:|
| 300 | **400** 首（丢 166） | 566 |
| **100** | **566** 首 ✅ | 566 |

`limit=300` 时：第 1 页给 300 条，第 2 页只给 100 条就 `more=false`。
**静默丢掉 166 首，不报错。**

> 红线 R4：page size 恒为 100，写死在 `ncm_http.ARTIST_SONGS_PAGE`。
> 若回归测试发现只收到 400 首，说明有人改了这个常量。

### 字段名与别处不同

此端点用 `album` / `artists` / `duration`，
**不是** `song/detail` 的 `al` / `ar` / `dt`。
`ncm_http.artist_ids()` 已兼容两套命名。

---

## 7. ⚠️ 可听性真相在 privileges，歌曲字段会骗人

真《晴天》186016 实测：

```
歌曲级字段：  fee=0   st=0   copyright=1   pop=100     ← 看着完全正常
privileges[0]: st=-200  pl=0  dl=0  plLevel="none"     ← 实际已下架灰掉
```

> **绝不能用歌曲级 `fee`/`st` 判断能不能听。** 必须读 `privileges`。
> AI 不得自行解读"fee=0 所以免费能听"。

### ⚠️⚠️ 7b. 匿名访问下 `pl`/`dl` 不能用来判断下架

**这是一个差点让整个体检报告失真的坑，实测踩到过。**

我们不登录，所以**所有 VIP 曲目的 `pl`/`dl` 恒为 0**。
实测热歌榜 201 首的 privileges 组合只有 4 种：

| st | fee | pl>0 | dl>0 | n | 真实含义 |
|---:|---:|:---:|:---:|---:|---|
| 0 | 1 | ✗ | ✗ | **129** | **需要会员**（不是下架！） |
| 0 | 8 | ✓ | ✗ | 70 | 免费/试听可播 |
| 0 | 0 | ✓ | ✓ | 1 | 完全免费 |
| **-200** | 0 | ✗ | ✗ | **1** | **真下架**（《晴天》186016） |

初版规则把 `pl==0 and dl==0 → grey` 放在 `fee==1` 之前判，
结果 **129 首 VIP 歌全被误报成「已下架」**——
在一个 200 首的歌单里报出 130 首下架，报告完全没有可信度。

> **`st < 0` 是匿名访问下唯一可信的下架信号。**
> `pl==0` 只说明「当前身份放不了」，不说明「歌没了」。

`fee` 语义（实测）：`0`=完全免费 `1`=VIP会员 `4`=数字专辑需购买 `8`=免费/试听

### 裁定规则表（`playability()` 是唯一裁定源，顺序敏感）

| # | 条件 | verdict |
|---:|---|---|
| ① | `priv.st < 0` | `grey`（下架）—— **必须最先判** |
| ② | `priv.fee == 4` | `album_only`（数字专辑需购买） |
| ③ | `priv.fee == 1` | `vip_limited`（需会员）—— **必须在看 pl 之前判** |
| ④ | `priv.pl > 0` | `ok` |
| ⑤ | 其他 | `unknown`（地区限制等，**不当作下架**） |

体检报告里 `grey` 与 `vip_limited` 必须分开呈现：
前者是「歌没了，需要替代」，后者是「你没开会员，歌还在」。
**只有前者才触发 AI 等价替代提名。**

---

## 8. 分享链接解析

`[?&]id=(\d+)` 覆盖：

```
https://music.163.com/playlist?id=3778678
https://music.163.com/#/playlist?id=3778678
https://y.music.163.com/m/playlist?id=3778678
```

`163cn.tv` 短链返回 **302**，需跟随重定向后再解析（urllib 默认跟随）。

---

## 9. 歌词接口（本 MVP 不用，备查）

```
GET /api/song/lyric?id=186016&lv=1&kv=1&tv=-1
```

可用，返回 `lrc.lyric`（带时间轴）、`tlyric`（翻译）。
歌词头部含 credits：`作词 / 作曲 / 编曲 / 制作人 / 吉他 / 混音`。

**下架歌也照样能取到歌词**（186016 是 grey 但歌词正常返回）。

> 用户明确选择**不抓歌词**（省时间、避免风控）。
> 代价：失去 quote-then-label 硬证据链 →
> 情绪类标注只能给 E2/E3，不得标 E1。见 SKILL.md 护栏 F1/F5。

---

## 10. 不可依赖的东西

* **Binaryify/NeteaseCloudMusicApi 已停止维护** —— README 只剩
  「保护版权,此仓库不再维护」。不要依赖任何第三方封装，直连官方端点。
* **官方「一键导入外部音乐」只吃其他平台的分享链接**（QQ音乐/酷狗/Apple Music…），
  **不吃我们生成的文本清单，也不吃自建 URL**。
  → "生成一个文档给网易云识别成歌单"这条路是堵死的，不要反复尝试。
* 本机无 `requests` / `pycryptodome` / `cryptography`（Python 3.14.5）。
  只用 stdlib `urllib`。因为只读不登录，也不需要 weapi 的 AES 加密。

---

## 11. 端点变更风险

网易云随时可能给这些端点加签名校验。`doctor.py --check endpoints`
对 4 个端点探活，缺字段时抛 `EndpointChanged` 并给出明确提示，
而不是让上层脚本抛 `KeyError` 后不知所以。

监控这 4 个：

1. `/api/v6/playlist/detail` → 需有 `playlist.trackIds`
2. `/api/v3/song/detail` → 需有 `songs`
3. `/api/cloudsearch/pc` → 需有 `result`
4. `/api/v2/artist/songs` → 需有 `songs` + `more`
