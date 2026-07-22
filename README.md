# Let AI Read Video!（video-watch）

**让任何 AI 真正"看"视频：纯本地、零外部 API 的视频阅读 skill。**
把视频一次性处理成「带时间戳的语音转写 + 抽帧图片」，AI 对照阅读后，像看完并听完整个视频一样回答你的问题。

**Give any AI agent the ability to truly watch videos — 100% local, zero external APIs.**
One command turns a video into a timestamped transcript + keyframes; the agent reads both and answers like someone who actually watched it.

[中文文档](#中文文档) · [English](#english)

---

<a name="中文文档"></a>
# 中文文档

## 为什么做这个项目

大模型能读网页、读文件，但原生读不了视频。云方案（如各类转写 SaaS）要么收费、要么数据出机、要么只有音频维度。video-watch 把整条链路搬到本地：**画面和语音双通道**，数据不出机，有 NVIDIA GPU 时 1 小时视频约 3~5 分钟处理完（实测 27~53 倍实时）。

## 特性

- 🎬 **双通道理解**：语音转写（faster-whisper 本地推理）+ 自适应抽帧（场景检测 + 均匀补点），回答全部带 `t=MM:SS` 时间戳引用
- ⚡ **双档速度通道**：
  1. B站客户端本地缓存 → 纯离线免下载，11 分钟视频实测 40 秒
  2. 常规链路（任意 URL/本地文件）→ GPU 下 1 小时视频 3~5 分钟
- 🔒 **纯本地**：转写、抽帧、理解全在本机；不上传任何内容到第三方 API
- 🧩 **AI 无关**：任何能跑命令行 + 读图的 Agent（Kimi / Claude Code / Codex / ...）读 `SKILL.md` 即可上手
- 💰 **成本感知**：按时长自适应帧预算（硬上限 2fps/100 帧），`--start/--end` 聚焦模式，字幕优先白嫖

## 快速开始

```bash
# 1. 环境自检 + 安装（ffmpeg/yt-dlp 便携版 + python 包，首次约 2~5 分钟）
python scripts/setup.py --check
python scripts/setup.py --install            # 中国大陆加 --mirror cn
python scripts/setup.py --install --with-cuda # 有 N 卡时追加 CUDA 库，提速 10~50 倍

# 2. 处理视频（三选一）
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx"   # B站链接
python scripts/watch.py "C:\Users\<你>\Videos\bilibili\<cid>"      # B站客户端缓存目录（最快）
python scripts/watch.py "meeting.mp4"                              # 本地文件

# 3. 让 AI 读产物回答
#    产物在 runs/<标题>_<时间戳>/：transcript.txt（带时间戳全文）+ frames/（抽帧）+ manifest.json
```

常用参数：`--start 12:30 --end 18:00`（聚焦时段）· `--no-frames`（纯音频内容）· `--width 1024`（看清屏幕文字）· `--language zh`（中文）· `--model medium`（更准）· `--force-whisper`（弃用平台字幕）

首次转写会自动下载模型权重（tiny 75MB / small 465MB / medium 1.5GB）；中国大陆请先设置 `HF_ENDPOINT=https://hf-mirror.com` 和 `HF_HUB_DISABLE_XET=1`（详见 [references/engines.md](references/engines.md)）。

## 给 AI Agent 使用

让 Agent 读 [SKILL.md](SKILL.md) 并照做即可——里面是给 AI 的完整工作流：环境自检 → 跑 `watch.py` → 读转写 → 分批读帧 → 带时间戳作答，含缓存模式、追问复用、map-reduce 长视频策略与排障指引。

## 工作原理

```
输入（URL / 本地文件 / B站缓存目录）
  │
  ├─ B站缓存 ──→ 剥前缀修复 m4s ──→ 纯音频/无音视频直接使用
  └─ 其他 ──→ yt-dlp 下载（字幕优先）
                │
                ▼
   faster-whisper 本地转写（GPU 加速，VAD 防幻觉）
                │
                ▼
   ffmpeg 自适应抽帧（场景检测 + 均匀补点，帧帧带时间戳）
                │
                ▼
   runs/<标题>/：transcript.srt/.txt/.json + frames/ + manifest.json
                │
                ▼
        AI 对照阅读，带 t=MM:SS 引用作答
```

## 实测性能（RTX 4060 Laptop 8GB，float16）

| 任务 | 耗时 |
|---|---|
| 5 分钟视频全链路（转写 small + 80 帧） | 18 秒 |
| 11 分钟 B站缓存视频全链路（转写 + 100 帧） | 40 秒 |
| 1 小时视频转写（small 模型） | 约 2~3 分钟 |
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

LLMs can read webpages and files, but they can't watch videos natively. Cloud solutions (transcription SaaS) either cost money, exfiltrate your data, or only cover the audio track. video-watch brings the whole pipeline local: **dual-channel understanding (visuals + speech)**, data never leaves your machine, and with an NVIDIA GPU a 1-hour video is processed in about 3–5 minutes (measured 27–53× realtime).

## Features

- 🎬 **Dual-channel understanding**: local speech transcription (faster-whisper) + adaptive frame extraction (scene detection + uniform fill); every answer is grounded with `t=MM:SS` timestamp citations
- ⚡ **Two speed tiers**:
  1. Bilibili client local cache → fully offline, zero download; an 11-minute video fully processed in 40 seconds (measured)
  2. Standard pipeline (any URL / local file) → 1-hour video in 3–5 minutes on GPU
- 🔒 **Fully local**: transcription, frame extraction, and understanding all happen on your machine; nothing is uploaded to any third-party API
- 🧩 **Agent-agnostic**: any agent that can run shell commands and read images (Kimi / Claude Code / Codex / ...) works by reading `SKILL.md`
- 💰 **Cost-aware**: duration-adaptive frame budget (hard caps: 2 fps / 100 frames), `--start/--end` focused mode, free platform captions preferred over ASR

## Quick start

```bash
# 1. Check + install (portable ffmpeg/yt-dlp + python packages; ~2–5 min first time)
python scripts/setup.py --check
python scripts/setup.py --install             # mainland China: add --mirror cn
python scripts/setup.py --install --with-cuda  # NVIDIA GPU: CUDA libs, 10–50× faster

# 2. Process a video (pick one)
python scripts/watch.py "https://www.bilibili.com/video/BVxxxx"   # Bilibili URL
python scripts/watch.py "C:\Users\<you>\Videos\bilibili\<cid>"     # Bilibili client cache dir (fastest)
python scripts/watch.py "meeting.mp4"                              # local file

# 3. Let the agent read the artifacts and answer
#    Artifacts in runs/<title>_<timestamp>/: transcript.txt (timestamped) + frames/ + manifest.json
```

Common flags: `--start 12:30 --end 18:00` (focus window) · `--no-frames` (audio-only content) · `--width 1024` (read on-screen text) · `--language zh` (Chinese) · `--model medium` (higher accuracy) · `--force-whisper` (skip platform captions)

On first transcription the model weights download automatically (tiny 75MB / small 465MB / medium 1.5GB). In mainland China, set `HF_ENDPOINT=https://hf-mirror.com` and `HF_HUB_DISABLE_XET=1` first (see [references/engines.md](references/engines.md)).

## For AI agents

Point the agent at [SKILL.md](SKILL.md) and let it follow the workflow: environment check → run `watch.py` → read the transcript → read frames in batches → answer with timestamps. It covers cache mode, run-dir reuse for follow-ups, map-reduce strategy for long videos, and troubleshooting.

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
   ffmpeg adaptive frame extraction (scene detection + uniform fill, every frame timestamped)
                │
                ▼
   runs/<title>/: transcript.srt/.txt/.json + frames/ + manifest.json
                │
                ▼
        Agent reads both channels, answers with t=MM:SS citations
```

## Measured performance (RTX 4060 Laptop 8GB, float16)

| Task | Time |
|---|---|
| 5-min video, full pipeline (small transcription + 80 frames) | 18 s |
| 11-min Bilibili cache video, full pipeline (transcription + 100 frames) | 40 s |
| 1-hour video transcription (small model) | ~2–3 min |
| Frame extraction | 80 frames in ~7 s |

## Compliance

- All code in this project is original work, licensed under the [MIT License](LICENSE); design inspirations are acknowledged in [NOTICE](NOTICE).
- This repository does not redistribute ffmpeg/yt-dlp binaries — `setup.py` downloads them from official sources on first run (they are covered by LGPL/GPL/Unlicense respectively).
- This project makes no calls to any Bilibili private endpoints that require login state (such as the caption API). Bilibili videos can be handled two compliant ways: yt-dlp downloading of public video pages, or reading the local cache of the user's own official client (fully offline, faster). Please make sure you have the legal right to process the content (see the disclaimer in NOTICE).

**Author / 作者**：[xiaohui5206](https://github.com/xiaohui5206)
