# Let AI Read Video!（video-watch）v2.1.0

把 B站视频（链接 / 本地文件 / 客户端缓存）在本机处理成**带时间戳的文字稿 + 关键帧 + 可导出的 PDF**。浏览器里点按钮即可完成，**不需要 AI Agent，数据不出机**。

## 功能特点

- **三种来源 × 三种模式**：B站链接 / 本地文件 / B站缓存目录（最快，纯离线）；单个视频、多P生成（先解析再勾选集数）、批量视频（多行输入，实时校验）
- **文字稿**：本地 Whisper 语音转写，带逐句时间戳；转写模型三档可选（快速 tiny / 均衡 small / 精准 medium，仅影响文字稿质量），TXT / SRT / JSON 全出
- **关键帧**：正常截取（自动分档 ≤100 帧）/ 自定义帧数 / 自定义间隔三种原则；清晰度 512/768/1024 三档；每张带时间戳
- **关键字定位关键帧**：输入关键字，自动回归文字稿命中时刻、定向补截对应画面，命中明细可展开
- **关键帧去重**：相似帧自动标记剔除（不删文件、可一键恢复），预览/PDF/副本数量口径一致
- **PDF 自动导出**：关键帧生成后自动产出带时间戳的 PDF（每页 1/2/6 帧版式可换），像一本图解讲义
- **产物按视频标题组织**：`runs/视频标题_时间戳/` 内含 `视频标题_文字稿.txt`、`视频标题_关键帧/`、`视频标题_关键帧.pdf`，批量产出好辨认
- **双通道进度条 + 任务管理**：文字稿/关键帧独立跑条、可先后补生成；全局排队、可取消、历史持久化，刷新/重启不丢
- **本地与隐私**：只监听 127.0.0.1，不调用云端音视频 API，URL 凭据全程脱敏；有 N 卡转写提速 10～50 倍

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

**阅读与导出**：文字稿阅读区（搜索高亮、点时间戳跳到最近关键帧、复制、下载 TXT/SRT）；关键帧大图查看（方向键切换）；关键帧 PDF 默认完成后自动导出，结果卡片也可手动导出并换版式（每页 1/2/6 帧可选）。

## 产物

每次任务落在 `runs/<视频标题>_<时间戳>/`：`<标题>_文字稿.txt`、`<标题>_文字稿.srt`、`<标题>_关键帧/`、`<标题>_关键帧.pdf`，以及 `transcript.json` / `frames.json` / `manifest.json` 等结构化数据（原始英文名产物始终保留，供 AI 工作流引用）。

## v2.1.0 更新与升级指引（2026-10-20）

- 界面新增**转写模型**三档：快速 tiny / 均衡 small（默认）/ 精准 medium——仅影响文字稿质量，与关键帧无关；首次选用未缓存的模型会自动下载权重（75MB / 465MB / 1.5GB）
- 关键帧 **PDF 改为默认自动产出**（设置栏可关），手动「导出PDF」按钮保留，可随时换版式重导
- 中文产物命名带视频标题：`视频标题_文字稿.txt`、`视频标题_关键帧/`、`视频标题_关键帧.pdf`，批量产出好辨认（历史任务的老名字不受影响）

**特别注意（模型档位与网络）**：新档位**初次使用必须联网下载模型权重**，网络连不上 HuggingFace 时转写会失败（报 `ConnectTimeout` / `LocalEntryNotFoundError`），切换回已缓存的 small 档不受影响。国内用户可先在命令行设置镜像再启动/重试（CMD：`set HF_ENDPOINT=https://hf-mirror.com` 和 `set HF_HUB_DISABLE_XET=1`），下载一次后永久离线可用；详见 [references/engines.md](references/engines.md)。

**老用户升级（v2.0.0 → v2.1.0，不用重装环境）**

1. 补丁位置：本仓库 **Releases → v2.1.0 → Assets（资产列表）**，下载其中的 **`v2.1.0-patch.zip`**（与完整包 `release.zip` 并列，别下错）。
2. 关闭正在运行的 Web UI，把补丁 zip **先解压**，再进入解压后的文件夹，把里面全部内容**按目录结构复制到项目根目录**——就是含 `启动WebUI.bat`、`scripts/`、`webui/` 的那一层，提示重复选「全部替换」。
3. 双击 `启动WebUI.bat` 即完成升级。历史产物、环境组件、个人设置均不受影响；旧任务的老文件名仍可正常查看。

**新用户**：直接下载 Releases 里 v2.1.0 的完整包（已含上述更新），无需再打补丁。

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
