# Let AI Read Video!（video-watch）

**让任何 AI 真正"看"视频：本地媒体预处理、零外部音视频 API 的视频阅读 skill。**
把视频一次性处理成「带时间戳的语音转写 + 抽帧图片」，AI 对照阅读后，像看完并听完整个视频一样回答你的问题。

**Give any AI agent the ability to truly watch videos — local media preprocessing, zero external audio/video APIs.**
One command turns a video into a timestamped transcript + keyframes; the agent reads both and answers like someone who actually watched it.

[中文文档](#中文文档) · [English](#english)

---

<a name="中文文档"></a>
# 中文文档

## 为什么做这个项目

大模型能读网页、读文件，但原生读不了视频。云方案（如各类转写 SaaS）要么收费、要么数据出机、要么只有音频维度。video-watch 把**媒体处理链路**全部搬到本机：**画面和语音双通道**，转写与抽帧不经过任何云端；有 NVIDIA GPU 时 1 小时视频约 3～5 分钟处理完（实测 27～53 倍实时）。

## 特性

- 🎬 **双通道理解**：语音转写（faster-whisper 本地推理）+ 自适应抽帧（场景检测 + 均匀补点）；`frames.json` 同时保留请求时间与实际解码 PTS，回答可带 `t=MM:SS` 引用
- 🔎 **证据驱动补帧**：自动生成时间对齐的 `review.json`；人或任意多模态模型判断证据是否充足，再用中立 JSON 计划只补关键时间窗
- ⚡ **双档速度通道**：
  1. B站客户端本地缓存 → 纯离线免下载，11 分钟视频实测 40 秒
  2. 常规链路（任意 URL/本地文件）→ GPU 下 1 小时视频 3～5 分钟
- 🔒 **本地媒体管线**：下载后的转写与抽帧不调用第三方音视频 API；最终读图的数据边界取决于你选用的 Agent，完全离线时请搭配本地多模态模型
- 🧩 **AI 无关**：任何能跑命令行 + 读图的 Agent（Kimi / Claude Code / Codex / ...）读 `SKILL.md` 即可上手
- 💰 **成本感知**：首轮按时长分配预算（2fps/100 帧上限），局部补帧最高 4fps 且有独立总量限制，避免全片暴力加密
- 📚 **多P/合集选集**：probe 输出合集清单（集数/标题/时长），`--item` 支持单集（`'3'`）、区间（`'3-7'`）与全部（`'all'`）；多集逐集产出独立 run 目录并聚合结果，单集失败自动降级

## 快速开始

需要 Python 3.10 或更高版本（推荐 3.11/3.12）。本地文件不需要 yt-dlp；运行安装脚本时会按所选功能补齐缺失组件。

```bash
# 1. 环境自检 + 安装（ffmpeg/yt-dlp 便携版 + python 包，首次约 2～5 分钟）
python scripts/setup.py --check              # 仅本地工作流可加 --profile local
python scripts/setup.py --install            # 中国大陆加 --mirror cn
python scripts/setup.py --install --profile local # 只处理本地文件/B站缓存，不装 yt-dlp
python scripts/setup.py --install --with-cuda # 有 N 卡时追加 CUDA 库，提速 10～50 倍

# 2. 处理视频（三选一）
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx"   # B站链接
python scripts/watch.py "C:\Users\<你>\Videos\bilibili\<cid>"      # B站客户端缓存目录（最快）
python scripts/watch.py "meeting.mp4"                              # 本地文件

# 多P/合集：先 probe 看清单，再用 --item 选集（'3' 单集 · '3-7' 区间 · 'all' 全部）
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx" --item 3

# 3. 让 AI 读产物回答
#    产物在 runs/<标题>_<时间戳>/：transcript.txt + frames/ + review.json + manifest.json
```

常用参数：`--start 12:30 --end 18:00`（聚焦时段）· `--no-frames`（纯音频内容）· `--width 1024`（看清屏幕文字）· `--language zh`（中文）· `--model medium`（更准）· `--force-whisper`（弃用平台字幕）

需要细看时，先由人或任意多模态模型填写 `review.json` 中的 `assessment`，再执行：

```bash
python scripts/review.py plan --review runs/<本次任务>/review.json --out runs/<本次任务>/refine_plan.json
python scripts/refine.py --video <manifest中的video_path> --out-dir runs/<本次任务> --plan runs/<本次任务>/refine_plan.json --pass-id r1
python scripts/review.py refresh --review runs/<本次任务>/review.json
```

`refine.py` 会沿用首轮分辨率，只追加新帧而不删除首轮结果；`refresh` 仅重置新增证据影响到的窗口。默认最多两轮、累计最多 120 张补帧。“语音和画面不同”本身不会触发补帧，只有证据不足或需要排除转场/同步误差时才细化。格式见 [自适应视听审查协议](references/adaptive-review.md)。

首次转写会自动下载模型权重（tiny 75MB / small 465MB / medium 1.5GB）；中国大陆请先设置 `HF_ENDPOINT=https://hf-mirror.com` 和 `HF_HUB_DISABLE_XET=1`（详见 [references/engines.md](references/engines.md)）。

## 首次使用会下载什么

本项目不打包任何二进制，以下内容由脚本在首次使用时从**官方源**自动下载，全程透明可审计：

| 内容 | 大小 | 来源 | 时机 |
|---|---|---|---|
| ffmpeg + ffprobe 便携版 | 约 90 MB | gyan.dev（ffmpeg 官方推荐 Windows 构建） | `setup.py --install` |
| yt-dlp.exe 便携版 | 约 15 MB | GitHub yt-dlp 官方 releases | `setup.py --install` |
| Python 包（faster-whisper 等） | 约 100 MB | PyPI | `setup.py --install` |
| CUDA 库（可选，N 卡加速） | 约 1 GB | PyPI | 加 `--with-cuda` 时 |
| Whisper 模型权重 | 75 MB / 465 MB / 1.5 GB（tiny/small/medium） | HuggingFace | 首次转写时 |

工具与产物全部落在项目目录内（`tools/`、`runs/`）；Python 包装入你的 Python 环境，模型权重存入 HuggingFace 标准缓存目录（`~/.cache/huggingface`）。除此之外不碰系统、不碰个人文件。

## 给 AI Agent 使用

让 Agent 读 [SKILL.md](SKILL.md) 并照做即可——里面是完整工作流：环境自检 → 跑 `watch.py` → 读转写与帧 → 填写审查包 → 必要时定向补帧 → 带时间戳作答，含缓存模式、追问复用、长视频策略与排障指引。

## 工作原理

```
输入（URL / 本地文件 / B站缓存目录）
  │
  ├─ B站缓存 ──→ 剥前缀修复 m4s ──→ 纯音频/无音频视频流直接使用
  └─ 其他 ──→ yt-dlp 下载（字幕优先）
                │
                ▼
   faster-whisper 本地转写（GPU 加速，VAD 防幻觉）
                │
                ▼
   ffmpeg 自适应抽帧（均匀骨架 + 场景点，帧帧记录实际解码 PTS）
                │
                ▼
   transcript + frames → 时间窗 review.json
                │
                ├─ 证据充分 ──→ 带 t=MM:SS 引用作答
                └─ 证据不足 ──→ refine_plan.json → 增量补帧 → 复查
```

## 实测性能（RTX 4060 Laptop 8GB，float16）

| 任务 | 耗时 |
|---|---|
| 5 分钟视频全链路（转写 small + 80 帧） | 18 秒 |
| 11 分钟 B站缓存视频全链路（转写 + 100 帧） | 40 秒 |
| 1 小时视频转写（small 模型） | 约 2～3 分钟 |
| 抽帧 | 80 帧约 7 秒 |

## 合规说明

- 本项目代码为原创，采用 [MIT License](LICENSE)；设计思路致谢见 [NOTICE](NOTICE)。
- 仓库不分发 ffmpeg/yt-dlp 二进制（它们由 setup.py 首次运行时从官方渠道下载，各自适用 LGPL/GPL/Unlicense）。
- 本项目不调用 B站任何需要登录态的私有接口（如字幕 API）。B站视频有两种合规途径：yt-dlp 下载公开视频页，或读取用户本机官方客户端的缓存（纯离线、更快）。请确保你对所处理内容有合法使用权（详见 NOTICE 免责声明）。

**作者 / Author**：[xiaohui5206](https://github.com/xiaohui5206)

---

<a name="english"></a>
# English

## Why this project

LLMs can read webpages and files, but they can't watch videos natively. Cloud solutions (transcription SaaS) either cost money, exfiltrate your data, or only cover the audio track. video-watch brings the entire **media-processing pipeline** local: **dual-channel understanding (visuals + speech)** — no cloud involved in transcription or frame extraction — and with an NVIDIA GPU a 1-hour video is processed in about 3–5 minutes (measured 27–53× realtime).

## Features

- 🎬 **Dual-channel understanding**: local speech transcription (faster-whisper) + adaptive frame extraction (scene detection + uniform backbone); `frames.json` keeps both requested times and decoded-frame PTS for grounded `t=MM:SS` citations
- 🔎 **Evidence-driven refinement**: produces a timestamp-aligned `review.json`; a human or any vision-capable model can assess evidence quality and request frames only for uncertain intervals through a neutral JSON protocol
- ⚡ **Two speed tiers**:
  1. Bilibili client local cache → fully offline, zero download; an 11-minute video fully processed in 40 seconds (measured)
  2. Standard pipeline (any URL / local file) → 1-hour video in 3–5 minutes on GPU
- 🔒 **Local media pipeline**: transcription and frame extraction use no third-party audio/video API after download; final image-review privacy follows the agent you choose, so use a local multimodal model for a fully offline workflow
- 🧩 **Agent-agnostic**: any agent that can run shell commands and read images (Kimi / Claude Code / Codex / ...) works by reading `SKILL.md`
- 💰 **Cost-aware**: base-pass caps stay at 2 fps / 100 frames; targeted passes may use up to 4 fps under a separate extra-frame budget
- 📚 **Multi-part / playlist selection**: probe returns a `playlist.items` listing (index/title/duration); `--item` accepts a single episode (`'3'`), a range (`'3-7'`), or `'all'`; multi-episode runs produce one run directory per episode with aggregated results and per-episode failure fallback

## Quick start

Requires Python 3.10 or newer (3.11/3.12 recommended). Local files do not require yt-dlp; the installer fills only the components needed for the selected workflow.

```bash
# 1. Check + install (portable ffmpeg/yt-dlp + python packages; ~2–5 min first time)
python scripts/setup.py --check               # add --profile local for local-only use
python scripts/setup.py --install             # mainland China: add --mirror cn
python scripts/setup.py --install --profile local # local files/Bilibili cache; skip yt-dlp
python scripts/setup.py --install --with-cuda  # NVIDIA GPU: CUDA libs, 10–50× faster

# 2. Process a video (pick one)
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx"   # Bilibili URL
python scripts/watch.py "C:\Users\<you>\Videos\bilibili\<cid>"     # Bilibili client cache dir (fastest)
python scripts/watch.py "meeting.mp4"                              # local file

# Multi-part/playlist: probe first for the item list, then pick with --item ('3' single · '3-7' range · 'all' everything)
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx" --item 3

# 3. Let the agent read the artifacts and answer
#    Artifacts in runs/<title>_<timestamp>/: transcript.txt + frames/ + review.json + manifest.json
```

Common flags: `--start 12:30 --end 18:00` (focus window) · `--no-frames` (audio-only content) · `--width 1024` (read on-screen text) · `--language zh` (Chinese) · `--model medium` (higher accuracy) · `--force-whisper` (skip platform captions)

For a closer look, let a human or any vision-capable model fill the `assessment` fields in `review.json`, then run:

```bash
python scripts/review.py plan --review runs/<run>/review.json --out runs/<run>/refine_plan.json
python scripts/refine.py --video <video_path from manifest> --out-dir runs/<run> --plan runs/<run>/refine_plan.json --pass-id r1
python scripts/review.py refresh --review runs/<run>/review.json
```

The refinement pass inherits the base resolution and appends evidence without deleting base frames. `refresh` resets only windows whose evidence changed. Defaults enforce at most two passes and 120 cumulative extra frames. Audio/visual differences alone do not trigger more sampling; refinement is reserved for insufficient evidence or diagnosing possible transition/sync errors. See the [adaptive review protocol](references/adaptive-review.md).

On first transcription the model weights download automatically (tiny 75MB / small 465MB / medium 1.5GB). In mainland China, set `HF_ENDPOINT=https://hf-mirror.com` and `HF_HUB_DISABLE_XET=1` first (see [references/engines.md](references/engines.md)).

## What gets downloaded on first run

This repository ships no binaries. The items below are fetched automatically from **official sources** on first use — fully transparent and auditable:

| Item | Size | Source | When |
|---|---|---|---|
| ffmpeg + ffprobe (portable) | 约 90 MB | gyan.dev (officially recommended Windows build) | `setup.py --install` |
| yt-dlp.exe (portable) | 约 15 MB | Official yt-dlp GitHub releases | `setup.py --install` |
| Python packages (faster-whisper etc.) | 约 100 MB | PyPI | `setup.py --install` |
| CUDA libraries (optional, NVIDIA GPU boost) | 约 1 GB | PyPI | with `--with-cuda` |
| Whisper model weights | 75 MB / 465 MB / 1.5 GB (tiny/small/medium) | HuggingFace | first transcription |

Tools and outputs stay inside the project directory (`tools/`, `runs/`); Python packages go into your Python environment, and model weights go to the standard HuggingFace cache (`~/.cache/huggingface`). Nothing else is touched — no system settings, no personal files.

## For AI agents

Point the agent at [SKILL.md](SKILL.md) and let it follow the workflow: environment check → run `watch.py` → inspect transcript and frames → assess the review packet → refine uncertain intervals when needed → answer with timestamps. It also covers cache mode, run reuse, long-video strategy, and troubleshooting.

## How it works

```
Input (URL / local file / Bilibili cache dir)
  │
  ├─ Bilibili cache ──→ strip m4s prefix ──→ use audio-only / video-only streams directly
  └─ Others ──→ yt-dlp download (platform captions preferred)
                │
                ▼
   faster-whisper local transcription (GPU-accelerated, VAD anti-hallucination)
                │
                ▼
   ffmpeg adaptive frame extraction (uniform backbone + scene points, decoded-frame PTS)
                │
                ▼
   transcript + frames → timestamp-window review.json
                │
                ├─ sufficient evidence → answer with t=MM:SS citations
                └─ insufficient evidence → refine_plan.json → append frames → reassess
```

## Measured performance (RTX 4060 Laptop 8GB, float16)

| Task | Time |
|---|---|
| 5-min video, full pipeline (small transcription + 80 frames) | 18 s |
| 11-min Bilibili cache video, full pipeline (transcription + 100 frames) | 40 s |
| 1-hour video transcription (small model) | 2–3 min |
| Frame extraction | 80 frames in 7 s |

## Compliance

- All code in this project is original work, licensed under the [MIT License](LICENSE); design inspirations are acknowledged in [NOTICE](NOTICE).
- This repository does not redistribute ffmpeg/yt-dlp binaries — `setup.py` downloads them from official sources on first run (they are covered by LGPL/GPL/Unlicense respectively).
- This project makes no calls to any Bilibili private endpoints that require login state (such as the caption API). Bilibili videos can be handled two compliant ways: yt-dlp downloading of public video pages, or reading the local cache of the user's own official client (fully offline, faster). Please make sure you have the legal right to process the content (see the disclaimer in NOTICE).

**Author / 作者**：[xiaohui5206](https://github.com/xiaohui5206)
