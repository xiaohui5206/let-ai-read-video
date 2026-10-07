# 版本核对与本次范围 / Version comparison and scope

核对日期：2026-10-07。当前目录 `video-watch-release/` 的源码版本为 2.0.0；这是本地开发交付，版本号不代表已创建远程 tag 或 Release。

## 基线证据

- 当前副本的改版前 HEAD：`88a6de4`，本地原有标签仅 `v1.0.0`、`v1.1.0`。
- 工作区根目录 `release-notes-v1.2.0.md` 记录三大件、`deliver.py`、详细总结模板。
- 只读核对桌面历史发布副本（本机用户目录路径不纳入公开文档）：存在 `scripts/deliver.py`、`tests/test_deliver.py`、`references/summary-template.md`；CHANGELOG 有 `[1.2.0] - 2026-07-25`，SKILL 第 7 步调用 deliver，setup 安装 reportlab/python-docx，本地有 `v1.2.0` 标签。
- 桌面 `deliver.py` SHA-256：`238067cf810589cc9bfb6f16c98d54d4eb53066ecccbc0c05ecc99922361edb6`。
- 当前副本没有 deliver、详细总结模板及 SKILL 第 7 步；并非上述历史 v1.2.0 的完整直接升级副本。此次没有访问远程仓库来核验线上发布状态。

## 功能差异

| 能力 | 桌面历史 v1.2.0 / 工作区发布说明 | 当前 2.0.0 副本 |
|---|---|---|
| URL / 文件 / B站缓存、转写与抽帧 | 有 | 保留；下载后逐集复检轨道 |
| review / refine 证据审查和补帧 | 有 | CLI/Agent 流程保留；Web 不自动判断语义 |
| 自动语义总结 Markdown | Agent 按详细模板撰写 | 需 Agent 阅读后另行撰写；Web 只生成处理产物 |
| Word 文字稿 | deliver + python-docx | 未提供；TXT/SRT/结构化 JSON |
| 关键帧 PDF | deliver + reportlab，封面/CJK 字体/每页 2 帧 | 标准库 make_frames_pdf，原 JPEG，每页 1/2/6 帧，时间戳/页码，ASCII 标题回落 |
| 三大件醒目命名 | `【阅读总结】*.md` / `【文字稿】*.docx` / `【关键帧】*.pdf` | `文字稿.txt` / `文字稿.srt` / `关键帧.pdf` / `关键帧/` |
| 浏览器交互、持久任务、排队取消 | 历史版本无本轮界面 | 有；刷新恢复，重启标 interrupted，可重试 |
| 关键字报告、去重恢复、清晰度 | 历史说明未提供 | 有；明确取样上限与恢复入口 |
| 交付依赖 | reportlab、python-docx | Web/PDF 零新增第三方依赖；媒体处理依赖仍需安装 |

## deliver.py 不移植的决定

本轮按 README8 收尾，保留标准库 Web/PDF 路径，不把桌面 Agent 三大件脚本直接移入当前副本。原因是两套交付契约、依赖、文件名及 PDF 字体策略不同，直接移植会让界面安装范围和文档承诺发生变化。桌面原版保留，当前 README 不承诺 DOCX 或自动语义总结。

后续若需要 Word 或中文 PDF 封面，应作为独立可选导出模块：先确定依赖安装方式，处理 dropped 帧和实际时间戳，再做固定回归；不能仅复制 deliver 就宣布完整兼容。

## 验证与发布边界

- 单元/固定 JPEG：`python -m unittest discover -s tests -v`。
- 真实管线：`python tests/e2e_real.py`。使用已缓存 small 模型可离线运行；关键词回归采用明确的固定文字稿，取帧子进程真实执行，不代表 ASR 识别内容精度评测。
- PDF 的 ASCII 标题回落是当前实现限制；去重保护通过固定 PPT 样本，不保证所有课程绝无误判。
- 在线 B站下载根因、SenseVoice、完整干净环境安装、EXE 与转写下载复用仍未排期。原始私人聊天与测试产物不进入发布目录。
- 当前仅整理本地源码与提交；远程推送、tag、Release 和桌面同步另按明确发布请求执行。

## English

This workspace uses version 2.0.0 for the local source delivery. Its pre-change HEAD was `88a6de4`, with local tags v1.0.0/v1.1.0. A read-only comparison confirmed that the desktop historical v1.2.0 copy includes deliver.py, a detailed summary template, SKILL step 7, reportlab/python-docx and the v1.2.0 tag. This workspace does not contain that complete delivery layer.

The current scope preserves the CLI review/refine workflow and adds the browser, persistent/cancellable jobs, TXT/SRT, configurable frame width, keyword reports, reversible dedup and standard-library PDF export (1/2/6 frames per page, ASCII title fallback). It does not promise automatic semantic summaries or Word documents. deliver.py was deliberately left in the historical copy because its dependencies, filenames and PDF contract differ. Importing it would require a separately scoped optional export module and regression checks.

Use the unit and real-pipeline commands above. Online Bilibili failures, SenseVoice, clean first installation, EXE packaging and transcript-download reuse remain outside this batch. No remote release/tag or desktop synchronization is implied by the local version number.
