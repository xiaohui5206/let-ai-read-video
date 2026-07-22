# 转写引擎与模型参考

本 skill 默认引擎为 faster-whisper，`--device auto`：检测到 CUDA GPU 自动用 GPU（float16），否则回退 CPU（int8）。

## 实测基准（本机 RTX 4060 Laptop 8GB，float16，5 分钟音频）

| 模型 | 总耗时（含抽音频+加载） | 速度 | 说明 |
|---|---|---|---|
| tiny | 5.7 s | 53x 实时 | 1 小时音频约 1 分钟 |
| small（默认） | 11.0 s | 27x 实时 | 1 小时音频约 2～3 分钟 |

全链路实测：5 分钟视频（探测 + small GPU 转写 + 80 帧抽取 + manifest）共 18 s。抽帧本身很快（80 帧约 7 s），无需额外加速。

GPU 建议：
- 8GB 显存：默认 small；要更准用 `--model medium`（float16 可放下）；最高质量 `--model large-v3 --compute-type int8_float16`（省显存）。
- 首次使用某模型会自动下载权重（tiny 75MB / small 465MB / medium 1.5GB / large-v3 约 3GB）。
- GPU 依赖 pip 包 nvidia-cublas-cu12 + nvidia-cudnn-cu12，用 `python scripts/setup.py --install --with-cuda` 安装；`setup.py --check` 的 `gpu` 字段可看就绪状态。

## CPU 参考表（无 GPU 或 --device cpu 时）

以 10 分钟中文视频、现代桌面 CPU（compute_type=int8）为基准的**量级参考**（实际速度取决于 CPU 核数与音频清晰度）：

| 模型 | 权重大小 | 内存占用 | 处理 10 分钟音频约需 | 中文质量 | 适用场景 |
|---|---|---|---|---|---|
| tiny | 75 MB | 1 GB | 1–2 分钟 | 较差：错字多，专有名词、数字易错 | 只要大意的快速预览 |
| base | 145 MB | 1 GB | 2–4 分钟 | 一般：日常口语可懂，长句易断错 | 时间紧、内容简单 |
| small（默认） | 465 MB | 2 GB | 5–10 分钟 | 较好：多数场景够用，偶有同音错字 | 默认选择，质量/速度平衡 |
| medium | 1.5 GB | 5 GB | 15–30 分钟 | 好：错字明显减少，术语更稳 | 正式转写、术语多、噪声大 |

建议：

- 默认用 `small`；用户明确要求"尽量准确"或内容专业（医学、法律、技术）时换 `--model medium`。
- 中文内容显式加 `--language zh`，避免 auto 把中文误判成其他语言。
- large-v3 在纯 CPU 上过慢（10 分钟音频常超过 1 小时），不推荐本环境使用。

## 模型权重下载说明

- setup.py **不预下载**模型；faster-whisper 在首次使用某模型时自动从 Hugging Face 拉取权重，缓存到 `%USERPROFILE%\.cache\huggingface`。
- 国内网络慢或失败时，先设置镜像再重跑转写：
  - CMD：`set HF_ENDPOINT=https://hf-mirror.com`
  - PowerShell：`$env:HF_ENDPOINT="https://hf-mirror.com"`
  - Bash：`HF_ENDPOINT=https://hf-mirror.com python scripts/watch.py ...`
- **实测坑（本机已验证）**：走镜像时若报 `CAS Client Error ... 401 Unauthorized`（xethub.hf.co 域名），说明模型走了 Xet 存储后端而镜像不代理它，追加禁用 Xet 即可：
  - Bash：`HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 python scripts/watch.py ...`
- 已缓存的模型离线可用，无需重复下载。

## SenseVoice（可选，中文专精、更快）

- 安装：`python -m pip install funasr torch torchaudio`（体积较大，按需安装）。
- 用法：
  - 单独转写：`python scripts/transcribe.py --video <视频> --out-dir <目录> --engine sensevoice`
  - 编排使用：`python scripts/watch.py <输入> --engine sensevoice --force-whisper`
- 特点：阿里 SenseVoiceSmall（权重经 ModelScope 下载，国内网络友好），中文识别准、速度快，自带 VAD 与标点恢复。
- 未安装时该路径会报错并提示上述安装命令；不影响 faster-whisper 路径。

## 常见排障

| 症状 | 处理 |
|---|---|
| 报错找不到 ffmpeg / ffprobe / yt-dlp | 运行 `python scripts/setup.py --install`（便携包装到 `<skill>/tools/`），再 `python scripts/setup.py --check` 确认 |
| `setup.py --install` 卡在 GitHub 下载（国内直连 GitHub 常接近 0 速） | pip 部分改用 `python scripts/setup.py --install --mirror cn`（清华镜像）；便携二进制改走 GitHub 代理手动下载：在原始 URL 前加 `https://gh-proxy.com/` 前缀，把 yt-dlp.exe、ffmpeg.exe、ffprobe.exe 放进 `<skill>/tools/` 后运行 `--check` 确认（本机即以此方式安装成功） |
| 转写出现幻觉（同一句话反复循环、无中生有） | 直接用 transcribe.py 重跑并加 `--no-vad` 关闭 VAD：`python scripts/transcribe.py --video <视频> --out-dir <run_dir> --model small --no-vad`；仍差则换 `--model medium`；背景噪声大时优先 medium |
| 平台字幕时轴错乱、内容缺失或机翻味重 | 重跑 watch.py 并加 `--force-whisper`，改用本地语音转写 |
| 中文识别明显差于预期 | 显式加 `--language zh`（watch.py 与 transcribe.py 均支持） |
| HF 模型下载超时 / 连接失败 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 后重试（见上文） |
| 报错 `Library cublas64_12.dll is not found` | CUDA 库未装：运行 `python scripts/setup.py --install --with-cuda`；不想用 GPU 可加 `--device cpu` 绕过 |
| URL 下载失败 | 检查网络连通性；确认视频公开可访问（会员专享、需登录、地区限制、已删除的内容 yt-dlp 无法直接下载）；B站部分内容需登录，改用公开链接，或用户自行下载后走本地文件路径 |
| 下载成功但没有字幕 | 属正常（部分视频无字幕），watch.py 会自动回退到语音转写；日志中会注明原因 |
