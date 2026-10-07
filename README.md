# Let AI Read Video!（video-watch）v2.0.0

把 B站视频（链接 / 本地文件 / 客户端缓存）在本机处理成**带时间戳的文字稿 + 关键帧 + 可导出的 PDF**。浏览器里点按钮即可完成，**不需要 AI Agent，数据不出机**。

*Give any AI agent the ability to truly watch videos — local media preprocessing, zero external audio/video APIs. One command turns a video into a timestamped transcript + keyframes. v2.0 adds a local Web UI so no agent is needed.*

---

## 安装（一次性，需联网）

要求 Windows + Python 3.10+（推荐 3.11/3.12）。

```bash
python scripts/setup.py --install              # 装齐 ffmpeg/yt-dlp 便携版 + Python 包
python scripts/setup.py --install --with-cuda  # 有 N 卡加上这条，提速 10～50 倍
python scripts/setup.py --check                # 随时体检，看还缺什么
```

- 只处理本地文件/B站缓存：`--profile local`（不装 yt-dlp）
- 中国大陆：`--mirror cn`；首次转写前设 `HF_ENDPOINT=https://hf-mirror.com` 和 `HF_HUB_DISABLE_XET=1`（详见 [references/engines.md](references/engines.md)）
- 首次转写自动下载模型权重（tiny 75MB / small 465MB / medium 1.5GB）

## 启动

双击 **`启动WebUI.bat`** → 浏览器自动打开 `http://127.0.0.1:8765`。

启动器自动：查找 Python（支持 `VIDEO_WATCH_PYTHON` 环境变量与项目内 venv）、识别端口冲突并复用已有实例、异常时给出中文诊断。也可命令行启动：`python scripts/launch_webui.py`。

## 界面用法

**来源三选一**：视频链接 / 本地文件（直接粘路径，支持中文、空格、引号）/ B站缓存目录（最快，纯离线）。

**模式三种**：单个视频 ｜ 多P生成（先「解析分P」再勾选集数，不会默认全跑）｜ 批量视频（每行一个，实时提示有效/重复/无效行）。

**三个按钮**：`生成文字稿` / `生成关键帧` / `同时生成`。两条进度条独立推进，点了一个可随时再点另一个；同源媒体自动复用，不重复下载。

**生成设置（右侧栏）**：

- 截取原则：正常（按时长自动分档，上限 100 帧）/ 目标帧数 / 严格间隔取帧（含起点）
- 清晰度：512 / 768 / 1024 宽度（录屏、PPT 建议 1024）
- 关键字定位：按关键字回归文字稿时刻定向补帧，命中明细可展开
- 关键帧去重：相似度阈值可调，只标记不删图、可一键恢复，按钮/预览/PDF 数量口径一致

**任务管理**：重型任务全局排队、可取消；刷新/重启不丢历史（重启后未完成任务如实标记"已中断"）；产物文件被移动会提示"文件不可用"并保留重试入口。

**阅读与导出**：文字稿阅读区（搜索高亮、点时间戳跳到最近关键帧、复制、下载 TXT/SRT）；关键帧大图查看（方向键切换）；结果卡片一键导出带时间标识的 `关键帧.pdf`（每页 1/2/6 帧可选）。

## 产物

每次任务落在 `runs/<视频标题>_<时间戳>/`：`文字稿.txt`、`文字稿.srt`、`关键帧/`、`关键帧.pdf`（导出后），以及 `transcript.json` / `frames.json` / `manifest.json` 等结构化数据。

## 隐私与合规

- 只监听 127.0.0.1；转写与抽帧不调用第三方云端音视频 API；URL 凭据全程脱敏
- 仓库不分发 ffmpeg/yt-dlp 二进制（`setup.py` 首次安装时从官方源下载，各自适用 LGPL/GPL/Unlicense）
- 本项目不调用 B站任何需要登录态的私有接口；请确保对所处理内容有合法使用权（详见 [NOTICE](NOTICE)）

## 排障

- 环境问题：先看界面右上角「环境状态」（显卡/CUDA 库/模型缓存分项展示，可重新检测）
- 转写幻觉、模型选择、镜像下载失败：见 [references/engines.md](references/engines.md)
- 下载失败会分类提示（连接/无数据/需登录/提取器）并给出建议；B站不稳定时可改用本地文件或缓存入口

## 回归验证（开发者）

```bash
python -m unittest discover -s tests -v   # 单元测试
node tests/test_webui_frontend.js         # 前端回归
python tests/e2e_real.py                  # 真实端到端（需 ffmpeg、转写包与已缓存模型）
```

## AI Agent / CLI 用法

[SKILL.md](SKILL.md) 的 Agent 工作流、`watch.py` 命令行、[review/refine 审查补帧协议](references/adaptive-review.md) 全部保留，与 Web UI 共存。

## 更多文档

- 更新记录：[CHANGELOG.md](CHANGELOG.md)
- 版本差异与交接：[docs/VERSION_NOTES.md](docs/VERSION_NOTES.md)、[docs/HANDOVER.md](docs/HANDOVER.md)
- 验证报告与后续清单：[docs/VALIDATION.md](docs/VALIDATION.md)、[docs/FOLLOWUP_REVIEW.md](docs/FOLLOWUP_REVIEW.md)

**作者 / Author**：[xiaohui5206](https://github.com/xiaohui5206) ｜ License: [MIT](LICENSE)
