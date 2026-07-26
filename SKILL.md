---
name: netease-playlist-doctor
description: |
  网易云音乐歌单体检与 AI 分类。用户给出歌单分享链接或歌单 id 时使用，
  典型触发：「我这个歌单最近听着不对劲」「帮我看看这歌单怎么了」
  「给我的网易云歌单分类整理」「哪些歌下架了」「按语种/曲风分类我的歌单」。
  职责：拉歌单 → 脚本查出下架灰歌/翻唱/重复 → AI 打多维标签 → AI 诊断
  → AI 提名等价替代（脚本验证真实可听）→ 输出 Markdown 报告 + 深链清单。
  【只读】不登录、不存 cookie、不创建或修改任何线上歌单，零封号风险。
  网易云不提供任何歌曲级曲风/情绪/语种标签，所以 AI 标注是唯一可行解。
---

# 网易云歌单体检师

**核心事实**：网易云的 `/api/v3/song/detail` 里
`displayTags`/`markTags`/`awardTags`/`entertainmentTags`/`songFeature`
**实测全部为 null**，也不提供 BPM/调性/能量值。
纯代码能拿到的只有歌名、歌手、专辑、时长、pop、发行年。

所以本技能的分工不是「AI 当一个更聪明的分类器」，而是：
**脚本负责一切可确定的事实，AI 负责一切没有数据源的判断。**

---

## §0 职责边界（红线，不可协商）

| 步骤 | 谁做 |
|---|---|
| HTTP、UA/Referer、重试、限速 | **脚本**（不要直接 curl 端点） |
| 分页、对账断言 | **脚本** |
| 冒名/翻唱打假 | **脚本**（`ar[].id` + `oct`，纯确定性） |
| 可听性裁定 | **脚本**（只认 `privileges`） |
| 标题归一化、相似度、时长比对 | **脚本**（禁止心算相似度） |
| 首数、总时长、各类占比 | **脚本**（禁止心算任何数字） |
| **哪个候选是歌手真身** | **AI**（决策点 1，须说出依据） |
| **同名多版本选哪个** | **AI**（决策点 2） |
| **灰歌换成什么** | **AI**（决策点 3，功能等价而非同名） |
| **语种/曲风/情绪/场景/精力标注** | **AI**（官方零标签，核心增值） |
| 诊断结论与报告文案 | **AI** |

### 七条红线

- **R1** 任何 `songId` 必须来自脚本输出的 JSON。**禁止凭记忆写 id。**
  需要新歌时只能以 `(歌名, 歌手, 专辑?, 年份?)` 三元组提名，交
  `resolve_song.py` 换取真实 id。
- **R2** 搜索结果未过 `is_impostor` + `is_cover` 一律不得采用。
- **R3** 不得跳过 `privileges` 检查，不得自行解读 `fee=0` 就当能听。
- **R4** `artist/songs` 的 page size 恒为 100（写死在 `ncm_http.py`，不许改）。
- **R5** 替代建议必须以 diff 呈现，用户确认前不产出最终清单。
- **R6** `doctor.py --check counts` 对账不平 → 停止写入并报差额。
- **R7** 标签必须 ∈ `label_space`，越界由脚本 exit 3 拦下。

### 两个最容易犯的错

1. **「需会员」≠「已下架」。** 我们不登录，所以所有 VIP 曲目的 `pl` 都是 0。
   只有 `privileges.st < 0` 才是下架。曾把 129 首 VIP 歌误报成下架——
   在 200 首歌单里报出 130 首下架，报告完全失去可信度。
   **只有 `grey` 触发替代提名，`vip_limited` 不触发。**

2. **直接搜歌名几乎全是假货。** 搜「晴天 周杰伦」前 30 条 0 条是真的，
   真身 186016 根本不在结果里，而歌手曲库能命中它。
   解析必须**歌手优先**，绝不能用搜索第一条。

---

## 主工作流 W1「修好我这个歌单」

用户说「我这个歌单最近听着不对劲」时走这条。

### 1. 事实层（全脚本，无 AI）

```bash
cd <skill>/scripts
python3 doctor.py --check endpoints          # 端点探活，失败先读 references
python3 fetch_playlist.py --url "<分享链接>"  # 或 --id <pid>
python3 enrich_songs.py --ids-from ~/.ncm-doctor/raw/playlist_<pid>.json
python3 doctor.py --check counts             # 红线 R6
python3 diagnose.py --playlist <pid>
```

`diagnose.py` 直接给出：下架灰歌清单、`oct∈{2,3}` 的翻唱/DJ 版、
歌手 id=0 的冒名条目、重复版本分组、时长直方图、歌手集中度、年代分布、总时长。
**这些数字全部是脚本算的，不要重算，也不要口头估算。**

### 2. 标注层（AI，逐批）

```bash
python3 make_batches.py --playlist <pid>     # 按歌手聚类，每批 80 首
```

然后**逐批**处理 `~/.ncm-doctor/batches/batch_XXX.json`：

- 批次里已注入 `label_space`，**不需要**去读 `references/taxonomy.md`
- 每首输出：`i`、`sid`（必须回引）、`lang`、`genre`、`mood`、`scene`、
  `energy`、`evidence`、`uncertain`、`note`
- 处理完一批**立刻**落盘，**不要在上下文里累积多批答案**：

```bash
python3 merge_classification.py --batch batch_001 --answer /tmp/a1.json
```

> 25 批若累积在上下文会到 200k+；逐批落盘则峰值恒定 ~15k。
> 中断可续跑，`index.json` 记录进度，merge 是幂等的。

标注纪律见 `references/taxonomy.md`。最关键的三条：

- **拿不准就给 `genre: null` + `note`，不要编。**
  `hint: low_confidence` 的歌默认从 `E3` 起判。
- **本次不抓歌词**，所以情绪判断最多给 `E2`，不得给 `E1`。
- **禁止把歌手整体风格套到单曲上**（周杰伦不是每首都「中国风」，
  《以父之名》是暗黑说唱）。脚本会自动检测并警告。

### 3. 诊断（AI）

读 `metrics.json`，写一段**说人话的**诊断，回答「为什么听着不对劲」。
好的诊断长这样：

> 23 首下架意味着平均每 7 首断一次；剩下 40% 是 2015 年电音，
> 和你近一年加进来的华语独立在打架；60% 曲目挤在 3:30–4:00，
> 情绪全程高位没有喘息段。

引用脚本给的数字，不要自己算。E3 标注不作为结论主依据。

### 4. 等价替代提名（AI + 脚本验证）

只对 `grey` 的歌提名。要**功能等价**，不是标题相同：

> `李志《热河》`（已消失）→ `万青《杀死那个石家庄人》`
> 同为中文摇滚 + 县城叙事 + 时代失落感，且在架可听。

反例：代码唯一的「等价」概念是标题字符串相同，会选出
`晴天(DJ版) pop=95` 或 `RyaVocal 晴天(原唱周杰伦) pop=100`——
热度都比真身高，但用 DJ 版替换 2003 年 R&B ballad 会当场炸掉歌单。

**超额提名**（要 1 首提 2~3 个）以吸收解析损耗，然后：

```bash
# wanted.json: [{"title","artist","album?","duration_ms?"}]
python3 resolve_artist.py --names-from /tmp/wanted.json --only-missing
python3 confirm_artist.py --name <歌手> --id <id> --by ai --reason "<依据>"
#   若所有候选体量都极小 → 用 --none 记录「真身已下架」，不要硬选
python3 resolve_song.py --wanted /tmp/wanted.json --out /tmp/resolved.json
```

`resolve_song.py` 的 status 决定下一步：

| status | 含义 | 你要做什么 |
|---|---|---|
| `resolved` | 已验证真实且可听 | 可采用 |
| `need_version` | 多个可听版本 | 选一个（原版 vs Live vs 合辑） |
| `need_artist` | 歌手未确认 | 跑 `resolve_artist.py` |
| `need_ai_fallback` | 全部候选不可听 | 换一首歌，或标「暂不可听」保留 |
| `unresolved` | 曲库无 + 搜索全被打假拦下 | **放弃，不要硬塞**，进报告 |
| `no_real_artist` | 歌手已全面下架 | 放弃该歌手 |

### 5. 出报告

```bash
python3 export_report.py --metrics <metrics.json> \
    --resolved /tmp/resolved.json --diagnosis /tmp/diagnosis.md
```

报告里每首歌都是 `music.163.com/song?id=` 深链，用户点一下就跳网易云。
**因为我们不碰账号，报告本身就是交付物**——把替换清单以 diff 呈现，
让用户自己决定要不要动手。

---

## 附带能力 W2「自创维度」

用户可以要求任何轴，成本几乎为零（换个 prompt 跑标注）：

> 「按『能不能在地铁上戴耳机听』分」→ 需推理：地铁噪音掩蔽低频，
> 编曲稀疏的歌会「消失」，故 `Bon Iver《Holocene》` 判否、`五月天《倔强》` 判可。

这类轴在任何 API 的 schema 里都不存在。若要长期使用，
把新维度加进 `assets/taxonomy.json` 并升 `taxonomy_version`。

---

## 排障

```bash
python3 doctor.py --check all
```

- **端点类失败** → 网易云可能加了签名校验，读 `references/api-endpoints.md`
- **回归类失败** → 有人改坏了红线规则（打假/分页/可听性），先修复再继续
- **`merge_classification.py` exit 3** → 标注越界，看报错改答案重提交，
  此时**没有任何数据被写入**，安全

状态目录 `~/.ncm-doctor/`（可用 `NCM_DOCTOR_HOME` 覆盖）。
`inventory.json`（事实层）与 `classified.json`（判断层）物理分离，
重拉歌单不会损失已有标注。

---

## 不做清单

- **不碰账号**：不登录、不存 cookie、不创建/修改任何线上歌单
- **不抓歌词**（用户决定）→ 代价是情绪判断只能给 E2/E3
- **不试图把生成的文本/链接导回网易云**：官方「一键导入外部音乐」只接受
  *其他平台*的分享链接，不接受我们生成的清单或自建 URL。这条路是堵死的，
  不要反复尝试。
- **不推荐歌单里没有的歌** —— 唯一例外是灰歌等价替代
- **不猜测无法解析的歌**：一律进 `unresolved.json` 并出现在报告里
- **不评价用户的音乐品味**
- **不为凑数把灰色歌当可听**
- 不依赖 Binaryify/NeteaseCloudMusicApi 等已停止维护的第三方
