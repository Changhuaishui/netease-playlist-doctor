# 网易云歌单体检师 · netease-playlist-doctor

一个 [Claude Code](https://claude.com/claude-code) Agent Skill：
读取网易云音乐歌单 → 查出**已下架的灰歌**、伪装成原唱的翻唱、重复版本 →
AI 打多维标签并诊断「为什么这歌单听着不对劲」→
为灰歌提名**功能等价**的替代曲 → 输出 Markdown 报告 + 可点击深链。

**只读**：不登录、不存 cookie、不创建或修改任何线上歌单，零封号风险。

---

## 为什么这件事需要 AI，而不是纯代码

这是本项目的立论基础，全部经实测验证：

**网易云不提供任何歌曲级的曲风/情绪/语种标签。**
`/api/v3/song/detail` 返回的 `displayTags`、`markTags`、`awardTags`、
`entertainmentTags`、`songFeature` **实测全部为 `null` 或 `[]`**，
也不提供 BPM / 调性 / 能量值（不像 Spotify）。

纯代码能拿到的字段穷尽了就是：

```
歌名、歌手名、歌手 id、专辑名、时长、pop(热度)、发行年、originCoverType
```

所以「按曲风分类」在纯代码方案里不是"实现得糙一点"，
而是**一个没有查询表的查表问题**。外接 Last.fm / MusicBrainz 也不行——
它们对华语独立、网络歌曲、翻唱的覆盖极差，而这恰恰是网易云曲库最密集的地方。

> **AI 是那个不存在的数据库**（世界知识补上全是 null 的字段）
> **AI 是那个不存在的查询语言**（"下雨天便利店的感觉"无法 SQL）
> **AI 是那个不存在的编排者**（歌单是序列，而没有 BPM 字段可排）

### 但同样重要：哪些事**不该**给 AI

打假、去重、可听性裁定、一切计数与求和，全部是确定性脚本。
比如「冒名歌手识别」我一开始以为需要 AI 做世界知识交叉验证——
**错了**：`ar[].id == 0` 一条规则就在实测中 100% 命中，零 token、可复现。

AI 在去伪上的真正战场是**残差**：当确定性过滤器返回**零条**的时候。

---

## 实测发现的几个坑

| # | 发现 | 影响 |
|---|---|---|
| 1 | 搜「晴天 周杰伦」**前 30 条 0 条是真的** | 全是翻唱、DJ 版，和形近字冒名歌手（`周杰伦-` `周杰伦.` `周杰伦♚`）。真身 186016 根本不在结果里 |
| 2 | `pop` 热度**无法**区分真假 | 假货 pop = 95~100，与真身同档甚至更高 |
| 3 | 冒名者 `ar[].id == 0` | 确定性打假规则。但**冒名者 `oct=0`**，所以 `oct` 单用抓不到冒名，两条规则必须并用 |
| 4 | 歌手曲库 `limit>100` **静默丢数据** | `limit=300` 分页只收到 400 首，`limit=100` 才收满 566 首。不报错 |
| 5 | 可听性真相在 `privileges`，歌曲字段会骗人 | 《晴天》歌曲级 `fee=0,st=0,copyright=1` 看着正常，实际 `privileges[0].st=-200` 已下架 |
| 6 | **匿名访问下 `pl` 恒为 0** | 曾把 129 首 VIP 歌误报成"已下架"。**只有 `st<0` 才是下架** |
| 7 | 官方「一键导入外部音乐」只吃**其他平台的分享链接** | 不吃自己生成的文本清单或自建 URL —— 这条回写路是堵死的 |

完整记录见 [`references/api-endpoints.md`](references/api-endpoints.md)。

---

## 安装

```bash
git clone https://github.com/Changhuaishui/netease-playlist-doctor.git
cp -r netease-playlist-doctor ~/.claude/skills/
```

零依赖（Python ≥3.9 stdlib only，不需要 requests / pycryptodome）。

## 使用

对 Claude Code 说：

> 我这个歌单最近听着不对劲：https://music.163.com/playlist?id=xxxxx

或手动跑：

```bash
cd ~/.claude/skills/netease-playlist-doctor/scripts
python3 doctor.py --check all                       # 自检
python3 fetch_playlist.py --url "<分享链接>"
python3 enrich_songs.py --ids-from ~/.ncm-doctor/raw/playlist_<pid>.json
python3 diagnose.py --playlist <pid>                # 确定性体检，无需 AI
```

`diagnose.py` 不需要 AI 就能给出下架清单、翻唱清单、重复分组、
时长直方图、歌手集中度、年代分布。

---

## 架构

```
L1 确定性层（Python）  HTTP / 分页 / 打假 / 可听性 / 归一化 / 计数
L2 AI 判断层           标注 / 诊断 / 等价替代提名 / 歌手真身认定
L3 交互层（SKILL.md）  意图澄清 → 只读诊断 → diff 呈现
```

### 七条红线

| | |
|---|---|
| R1 | 任何 `songId` 必须来自脚本输出。**AI 禁止凭记忆写 id**，只能以 `(歌名,歌手,专辑?,年份?)` 三元组提名 |
| R2 | 搜索结果未过 `is_impostor` + `is_cover` 一律不得采用 |
| R3 | 不得跳过 `privileges` 检查 |
| R4 | `artist/songs` page size 恒为 100 |
| R5 | 替代建议必须以 diff 呈现，用户确认前不产出最终清单 |
| R6 | 对账不平 → 停止写入并报差额 |
| R7 | 标签越界 → 脚本 exit 3，**不写入任何数据** |

红线由 `doctor.py --check regression` 守护——故意把 page size 改成 300，
回归测试会立刻报「只收到 400 首（应 ≥566）」。

### 证据等级（本项目最重要的护栏）

本 skill 默认**不抓歌词**，所以模型只能靠歌名/歌手/专辑/年份判断。
这意味着对小众歌、新歌，模型**零真实知识却会流畅编造曲风**。

| 等级 | 含义 |
|---|---|
| `E1` | 有可核验依据，明确知道这首歌本身 |
| `E2` | 基于该歌手其他作品/厂牌/年代的合理推断 |
| `E3` | 纯先验猜测 → **必须 `uncertain=true`，不作诊断主依据** |

- 允许 `genre: null` + `note: "无可靠依据"` —— 这比编一个曲风好得多
- **`genre` null 率低于 5% 反而是过度自信警报**，脚本会主动报警
- 同一歌手全部歌曲拿到完全相同标签集 → 报警（artist-level 标签继承的指纹）

---

## 目录

```
SKILL.md                      技能入口（职责边界 + 红线 + 工作流）
scripts/
  ncm_http.py                 只读客户端：打假 / 可听性 / 归一化
  doctor.py                   env / endpoints / counts / regression 自检
  fetch_playlist.py           分享链接 → 全量 trackIds
  enrich_songs.py             批量补全 → inventory（事实层）
  diagnose.py                 确定性体检指标（无 AI）
  make_batches.py             按歌手聚类切批 + 注入 label_space
  merge_classification.py     校验并写入标注（R7 执行点）
  resolve_artist.py           歌手候选打分（不自动决策）
  confirm_artist.py           落库 AI 认定的真身
  resolve_song.py             歌手优先解析（AI 提名的验证通道）
  export_report.py            渲染 Markdown 报告 + 深链
references/
  api-endpoints.md            全部实测事实与反例
  taxonomy.md                 标签体系与证据等级
assets/taxonomy.json          机器版标签空间
```

数据存 `~/.ncm-doctor/`（可用 `NCM_DOCTOR_HOME` 覆盖）。
`inventory.json`（事实层）与 `classified.json`（判断层）物理分离，
重拉歌单不会损失已有标注。

---

## 边界

- 不碰账号：不登录、不存 cookie、不创建/修改任何线上歌单
- 不抓歌词（可选能力，默认关）→ 代价是情绪判断只能给 E2/E3
- 不推荐歌单里没有的歌 —— 唯一例外是灰歌等价替代
- 不猜测无法解析的歌，一律进 `unresolved.json` 并出现在报告里
- 不依赖已停止维护的第三方封装（Binaryify/NeteaseCloudMusicApi 已因版权下架）

仅供个人学习与自己歌单的整理使用。

## License

MIT
