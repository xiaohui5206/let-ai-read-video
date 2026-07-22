---
name: video-watch
description: 纯本地、零外部 API 的视频阅读与问答——把视频处理成「带时间戳的语音转写 + 抽帧图片」，再对照阅读回答，效果等同于"看完并听完"整个视频。当用户要看视频、视频理解、视频总结、视频转写、提取视频文字，或对 YouTube、B站等视频链接做内容问答、分析本地视频文件、视频抽帧，并要求带时间戳（t=MM:SS）引用回答视频内容时使用。适用任何具备命令行与图片读取能力的 AI Agent。
---

# Let AI Read Video!（video-watch）：本地视频阅读（任何 AI 可用）

核心思路：用脚本一次性把视频变成「带时间戳的转写文本 + 抽帧图片」，再由你（AI）阅读文本与图片回答。全程本地，不调用任何外部视觉/语音 API。有 NVIDIA GPU 时，1 小时视频约 3～5 分钟处理完（实测 27～53 倍实时）；纯 CPU 也可运行，只是转写慢 10～50 倍。

## 首次使用（Bootstrap，必须按序执行）

在 skill 根目录（本文件所在目录）下执行：

```bash
# 1. 环境自检：看 RESULT_JSON 的 missing / gpu 字段
python scripts/setup.py --check

# 2. 有缺失则安装（ffmpeg/ffprobe/yt-dlp 便携版 + python 包）
python scripts/setup.py --install            # 中国大陆网络加 --mirror cn
python scripts/setup.py --install --with-cuda # 有 NVIDIA 显卡时追加 CUDA 库（提速 10～50 倍）
```

- `tools/` 已内置 Windows 版 ffmpeg/ffprobe/yt-dlp 时，第 2 步通常只需装 python 包。
- macOS/Linux：tools 目录的 Windows 二进制不可用，请用系统包管理器装 ffmpeg 与 yt-dlp（`brew install ffmpeg yt-dlp` / `apt install ffmpeg` + `pip install yt-dlp`），pip 包安装命令相同。
- 中国大陆网络：模型权重下载需镜像加速，运行转写前设置环境变量 `HF_ENDPOINT=https://hf-mirror.com` 与 `HF_HUB_DISABLE_XET=1`（详见 references/engines.md）。

## 标准工作流

1. 处理视频：运行 `python scripts/watch.py <URL或本地路径>`。
   - 解析最后一行 `RESULT_JSON:`，拿到 `run_dir`、`transcript_txt`、`frames_dir`、`frame_count`。
   - 转写默认 `--device auto`：有 CUDA 用 GPU，否则 CPU；可用 `--device cuda|cpu` 强制。
   - 任一子步骤失败 watch.py 会中止并在 stderr 给出原因，按提示修复后重跑即可。
2. 读转写：读取 `<run_dir>/transcript.txt`（每行 `[MM:SS] 文本`）。
   - 很长时先读 `<run_dir>/transcript.json` 定位相关时间段，再按需细读。
3. 读抽帧：先读 `<run_dir>/frames/frames.json` 拿每帧精确时间戳，再用你所平台的图片读取能力（多模态读图工具）分批读图，每批不超过 15 张。
   - 帧文件名自带时间（如 `0007_t0000123.5.jpg` 即 t=123.5s），可直接引用。
   - 若你所在平台不支持图片输入：仅用转写文本回答，并向用户说明画面维度缺失。
4. 作答：每条关键结论都标注来源时间戳，格式 `t=MM:SS`（视频超过 1 小时用 `t=HH:MM:SS`）。
   - 转写文本与画面信息相互印证；两者冲突时以画面为准并说明。
   - 字幕/转写可能有错别字（尤其 AI 字幕与 whisper 的同音错字）：按语境修正后再引用，不要照抄明显错误。
   - 总结类请求按模板输出：先一段 `## 概要`（3～5 句），再 `## 亮点`（5～10 条带时间戳的 bullet），长视频再加 `## 章节大纲`（按内容分段，每段一行起止时间 + 小标题）。
   - 超长转写（1 小时以上）：用 map-reduce 策略——按 10～15 分钟一段分批读 transcript.json 做分段摘要，最后汇总成总摘要，避免一次性塞入全文。

## B站客户端本地缓存（免下载，最快路径）

- 输入直接传 B站 PC 客户端的缓存目录（如 `C:\Users\<用户>\Videos\bilibili\<cid>\`），watch.py 自动进入缓存模式：剥掉 m4s 的 9 字节前缀生成修复副本（**原缓存文件只读不动**）、ffprobe 区分纯音频/无音视频、读 videoInfo.json 拿标题与 BV 号。
- 转写策略：**纯离线**——直接用**缓存里的纯音频**本地 GPU 转写（比从视频抽音频更快），全程无网络调用。
- 抽帧直接用**缓存里的无音视频流**，零下载。实测 11 分钟视频约 40 秒出全部产物。
- 缓存目录通常位于 `C:\Users\<用户>\Videos\bilibili\`；手机端缓存拷到电脑后同样可用（目录内含 .m4s 即识别）。

## 追问与复用

- 追问时复用同一个 run 目录，直接重读 `transcript.txt` 和 `frames/`，不要重跑 watch.py。
- 需要更清晰的截图（屏幕文字、代码、PPT）时，只重跑 frames.py 加大宽度：
  `python scripts/frames.py --video <run_dir内视频或原路径> --out-dir <run_dir> --width 1024 --budget 100`
- 需要某个时间点的特写帧时，用 `--start`/`--end` 缩窗重抽：
  `python scripts/frames.py --video <视频> --out-dir <run_dir> --start 12:30 --end 13:30 --budget 60`

## 参数选择

- 视频超过 10 分钟：先引导用户给出关注的时间段，用 `python scripts/watch.py <输入> --start 12:30 --end 18:00` 聚焦，避免把帧预算摊薄到全片。
- 播客、会议、讲座等纯音频内容：加 `--no-frames` 跳过抽帧，节省 token。
- 屏幕录制、代码讲解、PPT 演示等文字多的视频：加 `--width 1024` 抽更清晰的帧。
- 平台字幕缺失、错乱或质量差：加 `--force-whisper` 改用本地语音转写；引擎与模型权衡见 references/engines.md。
- 只要画面不要文字（如检查镜头内容）：加 `--no-transcribe`。
- 中文视频：加 `--language zh` 避免自动检测误判。
- 自定义产物位置：加 `--out-dir <目录>`；默认写到 `<skill>/runs/<标题slug>_<时间戳>/`，每次运行独立目录、互不覆盖。

## 目录结构

```
video-watch/
├── SKILL.md               # 本文件
├── references/engines.md  # 模型选型、实测速度、网络镜像、排障大全
├── scripts/               # watch.py(总编排) / probe / download / frames / transcribe / setup / common
├── tools/                 # 便携版 ffmpeg/ffprobe/yt-dlp（Windows；可被 setup.py 重建）
└── runs/                  # 运行期生成的处理产物（每个视频一个目录）
```

## 排障

- ffmpeg/ffprobe/yt-dlp 缺失、模型下载慢、转写幻觉、URL 下载失败、CUDA 库报错等：先读 references/engines.md 对照处理。
- 确认子脚本可用性：`python scripts/probe.py --input <输入>` 单独探测；`python scripts/transcribe.py --help`、`python scripts/frames.py --help` 查看子参数。
