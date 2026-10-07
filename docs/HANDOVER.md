# 可分享的项目交接 / Shareable handover

更新：2026-10-07。本文件是技术交接摘要，不包含私人原始聊天、令牌或用户测试媒体。

## 入口与边界

- 公开说明：根目录 README；版本差异：`docs/VERSION_NOTES.md`；更新记录：CHANGELOG。
- 最新交接与 GitHub 提交版：`README10.md`；四项可靠性修复已完成，历史 README9 保留原记录。
- 用户界面：`启动WebUI.bat` → `scripts/launch_webui.py` → `scripts/webui.py`，仅本机 `127.0.0.1`。
- CLI：`scripts/watch.py`；Agent：SKILL 的环境检查、处理、阅读、审查、补帧、回答步骤。
- Web 生成文字稿和帧图，不自动做语义阅读总结。当前没有 DOCX 导出。

## 结构

| 文件 | 职责 |
|---|---|
| `scripts/webui.py` | 校验/API/任务处理/关键字补帧/去重/中文副本 |
| `scripts/webui_jobs.py` | sqlite 历史、调度、取消、媒体复用 |
| `webui/index.html` / `app.css` / `app.js` | 页面结构、视觉、任务和阅读交互 |
| `scripts/watch.py` / `probe.py` / `download.py` | 输入探测、逐集编排、下载 |
| `scripts/transcribe.py` / `frames.py` | 本地 ASR、实际帧时间索引 |
| `scripts/review.py` / `refine.py` | 证据包与追加补帧 |
| `scripts/make_frames_pdf.py` | 标准库 PDF，每页 1/2/6 帧 |

## 验证

```bash
python -m unittest discover -s tests -v
node tests/test_webui_frontend.js
node --check webui/app.js
python tests/e2e_real.py
python scripts/setup.py --check --profile local
```

单测使用标准库；真实回归需要 ffmpeg/ffprobe、转写包、small 模型。安装脚本联网；本地媒体和缓存可在所需组件/模型就绪后离线处理。不要把纯正弦波的转写管线测试当作识别准确率评测。

当前完整验证为 245 项 Python、7 项 JS、38 项真实端到端断言。e2e 自动隔离临时产物/数据库，重启只影响测试库；手工隔离可用 Web `--runs-dir`，或设置 `VIDEO_WATCH_RUNS_DIR`。日常默认 `runs/` 不变，文件预览边界及实例复用随产物根目录检查。

`runs/` 保存每视频产物、关键字/去重报告和任务 sqlite；`tools/` 保存本地工具，二者不进 Git。原帧保留与 UI 恢复是两项不同保证；恢复后需重新导出 PDF。关键词定位是句子中点估计，不是逐词时间。去重样本通过不能推导为所有视频都不会误判。

## 后续方向

在线 B站下载专项诊断；转写路径下载复用；SenseVoice 实测；干净环境安装与可选 EXE。桌面 v1.2.0 三大件按 VERSION_NOTES 的独立模块方案处理。保留用户现有改动与原始产物，避免发布目录混入私人历史。

## English

This is a shareable technical handover without private conversations, credentials or user media. README and VERSION_NOTES define the current scope. The browser generates processing artifacts; CLI/Agent review and refine remain available. Word export and automatic semantic summaries are not included.

Run the commands above. Real regression needs ffmpeg/ffprobe, ASR packages and cached small weights. Installation needs network access; prepared local media can run offline. Preserve existing changes and artifacts. runs/tools are excluded from Git. Re-export PDFs after restoring hidden frames, treat keyword times as segment midpoints, and do not infer universal dedup accuracy from fixed regressions.
