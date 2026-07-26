# 最小标签体系 v1 —— 唯一权威来源

机器版在 `assets/taxonomy.json`（由 `make_batches.py` 注入每个批次，
所以标注时**不需要**读本文；本文是给人看的，以及争议时的裁定依据）。

本体系是**为诊断服务的最小集**，不是通用音乐分类法。
判断「要不要加一个维度」的标准只有一条：
**它能不能让体检报告说出用户自己看不出来的问题？** 不能就不加。

---

## 维度

| 维度 | 取值 | 基数 | 说明 |
|---|---|---|---|
| `lang` | `zh yue en ja ko inst other` | 恰好 1 | `inst` = 器乐无人声；`yue` = 粤语 |
| `genre` | 见下 | 1–3，**可为 null** | 拿不准就 null，别硬填 |
| `mood` | 见下 | 1–3，**可为 null** | |
| `scene` | 见下 | 0–3 | 空数组合法 |
| `energy` | `1`–`5` 整数 | 恰好 1 | 1=极静 3=中等 5=极烈 |
| `evidence` | `E1 E2 E3` | 恰好 1 | 见下节 |
| `uncertain` | `true/false` | 恰好 1 | `E3` 必须为 `true` |

```
genre : pop rock folk r&b hiphop electronic classical jazz metal punk
        post-rock ost acg country ambient world other
mood  : happy sad nostalgic calm warm lonely angry epic tender restless bittersweet
scene : study workout commute late_night driving sleep party social focus
```

**越界标签会被 `merge_classification.py` 以 exit 3 拦下，且不写入任何数据**（红线 R7）。
需要新标签就改 `assets/taxonomy.json` 并升 `taxonomy_version`，不许临场发明。

### `lang` 的一条精确边界

中/日/韩/英可以靠字符集大致判断，但
**粤语 vs 国语代码判不了**（都是汉字），闽南语同理。
这一小块必须靠词法知识（唔/嘅/係/喺/乜 → 粤语）。
歌名看不出来时，宁可标 `zh` 并给 `E2`，不要猜 `yue`。

---

## 证据等级（本体系最重要的部分）

因为用户选择**不抓歌词**，模型只能靠歌名/歌手/专辑/年份来判断。
这意味着对小众歌、网络歌曲、新歌，模型**零真实知识却会流畅编造曲风**。
证据等级是唯一的护栏，必须硬执行。

| 等级 | 含义 | 例子 |
|---|---|---|
| **E1** | 有可核验的事实支撑，明确知道这首歌本身 | 周杰伦《晴天》2003《叶惠美》→ 华语流行 / R&B，E1 |
| **E2** | 基于该歌手其他作品、厂牌、年代的合理推断 | 万青的某首没听过的曲目 → 推断中文摇滚，E2 |
| **E3** | 纯先验猜测（只是"名字听起来像"） | `马也_Crabbit《海屿你》` → **E3 + uncertain=true** |

### 自动降级规则

`pop < 30` 或 歌手 `musicSize < 50` → **默认从 E3 起判**，
除非模型能说出关于这首歌的具体可核验事实。

`make_batches.py` 会在批次里给这类歌打上 `"hint": "low_confidence"`。

### 硬约束

* `E3` ⇒ `uncertain: true`
* `E3` 的标签**不得作为诊断结论的主依据**（报告里要单列，标注"低置信"）
* **允许 `genre: null` + `note: "无可靠依据"`** —— 这比编一个曲风好得多
* 情绪判断在无歌词时**最多给 E2**，不得给 E1
  （否则会跟随平台流行叙事，例如把《起风了》标成"治愈励志"，
   而歌词实际是近乡情怯与漂流失落 —— 护栏 F5）

### null 率是健康指标，不是缺陷

`merge_classification.py` 会统计 `genre` 的 null 率与 E3 占比。

> **null 率低于 5% 反而是过度自信警报**（护栏 F8）。
> 一个真实的华语歌单里必然有一批模型说不清的网络歌曲/小众独立。
> 全部填满说明模型在编。

---

## 禁止事项

1. **禁止 artist-level 标签继承**（护栏 F3）
   不许给周杰伦所有歌都标"中国风" —— 会误伤《以父之名》（暗黑说唱）、
   《不能说的秘密》（古典钢琴）。Radiohead 的《Creep》与
   《Everything In Its Right Place》同理。
   `merge_classification.py` 会检测：同一歌手全部歌曲拿到**完全相同**的标签集
   → 打印警报（这是过度泛化的指纹）。

2. **禁止用歌名指代歌曲**（护栏 F2）
   标注必须回引 `i` 索引与 `sid`。歌单里可能同时存在
   周杰伦《晴天》与空匪《晴天》(arid 30000975)，靠歌名会串。

3. **禁止把版本当作品趣味**（护栏 F4）
   `oct=2/3` 的翻唱/DJ 版已在批次里标出。
   《晴天(DJ版)》说明用户可能只是随手加的，不代表"喜欢 DJ"。

4. **禁止心算任何数字** —— 首数、时长、占比全部由脚本给出。
