# Apple Music Curator

用 Codex 在 Mac「音乐」中发现新歌、创建歌单，并用试听反馈改进推荐。

## 安装

需要 macOS、Python 3.10+，以及能运行本机命令和操作「音乐」窗口的 Codex。添加库外歌曲时需登录 Apple Music。

在下载后的技能目录运行，然后在 Codex 中调用 `$apple-music-curator`。 [Codex 安装文档](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)

```bash
skill_dir="$HOME/.agents/skills/apple-music-curator"
mkdir -p "$skill_dir"
cp -R README.md SKILL.md scripts tests "$skill_dir/"
```

## 使用

| 功能 | 示例 |
| --- | --- |
| 先看推荐 | 「参考『窗边阅读』，推荐 12 首类似风格的歌，换一批歌手。」 |
| 建单 | 「把第 2、5、9 首建成『纸页与午后』。」也可以直接指定场景和首数，让技能选曲并建单。 |
| 断点续跑 | 「继续上次没加完的歌单。」技能按原歌单 ID 回读，从缺失曲目继续。遇到同步延迟时先等待回读，避免重复添加。 |
| 偏好学习 | 「记住我的阅读偏好。第 2 首的木吉他很好，第 5 首鼓点太突出。」下次按场景调整排序。明确反馈优先，播放次数至少新增两次才作为弱信号。 |

示例歌单和反馈均为虚构。可在技能目录用 `python3 scripts/session.py resume` 查看续跑状态，或用 `python3 scripts/taste.py show --scope reading` 查看阅读偏好。

## 架构

- **选曲**　[SKILL.md](SKILL.md) 编排流程，[library.js](scripts/library.js) 读资料库，[catalog.py](scripts/catalog.py) 核验曲库版本，[shortlist.js](scripts/shortlist.js) 去重。
- **建单与播放**　[session.py](scripts/session.py) 保存顺序和进度，[reconcile.py](scripts/reconcile.py) 核对实际曲目。[playlist.py](scripts/playlist.py) 创建歌单并添加库内曲目，库外曲目通过「音乐」窗口加入。[playback.py](scripts/playback.py) 控制顺序循环。
- **偏好**　[taste.py](scripts/taste.py) 按批次和编号记录反馈，并按场景和曲目标签调整排序。

建单进度和偏好默认保存在本机 `~/.local/state/apple-music-curator/sessions.sqlite3`，位于技能目录之外。
