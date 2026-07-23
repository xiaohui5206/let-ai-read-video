# 更新日志 / Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

All notable changes to this project will be documented here.
Format follows Keep a Changelog; versioning follows SemVer.

---

## [1.1.0] - 2026-07-23

### 新增 / Added
- **文稿—画面时间窗审查**（`scripts/review.py`）：把视频按时间窗生成"转写+帧"证据包，
  AI 逐窗判断 supports / contradicts / insufficient，证据不足的窗口生成补帧计划。
- **局部补帧**（`scripts/refine.py`）：按审查计划对指定窗口加密抽帧，最高 4fps；
  默认上限两轮、累计 120 帧；`review.py refresh` 只重置证据发生变化的窗口。
- 抽帧记录 `requested_t` 与真实解码帧时间 `actual_t`；选帧策略改为"均匀骨架 + 场景点"。
- **多P/播放列表选集**：`download.py` 与 `watch.py` 新增 `--item` 参数——`'3'` 单集、
  `'3-7'` 区间、`'all'` 全部；`--item all` 超 10 集时打印代价警告（实测 52 集课程选下第 1 集）。
- `probe.py` 输出合集清单 `playlist.items`（集数/标题/时长），选集前可查看。
- `watch.py` 多集编排：逐集产出独立 run 目录并聚合结果，单集失败降级跳过、不影响其余各集。
- B站缓存音视频流智能配对与时间轴对齐（`--source-offset`）；URL 凭据/跟踪参数脱敏；
  `setup.py --profile local` 本地安装模式；`tests/` 单元测试套件（61 项）。

### 修复 / Fixed
- B站缓存音视频流起点微差导致转写负时间戳，使 review 步骤失败、缓存路径中断。
- `watch.py` 超时降级路径的 NameError；review/refine 失败不再摧毁已完成的转写与抽帧成果。
- `review.py` 参数错误现遵循 RESULT_JSON 契约；refresh 改为"先校验后写入"。
- 无 manifest 目录下补帧轮数/累计帧数上限失效的问题。
- README 删除线渲染问题（中文"3~5"被 GFM 误解析）；数据边界表述统一。

---

## [1.0.0] - 2026-07-22

首个公开发布。Initial public release.

- 纯本地视频阅读 skill：faster-whisper GPU 转写（带 `t=MM:SS` 时间戳）+ ffmpeg 场景感知抽帧。
- 三种输入：视频 URL（yt-dlp）、本地文件、B站客户端缓存目录（免下载纯离线）。
- `setup.py` 环境自检与一键安装（便携版 ffmpeg/yt-dlp，不重分发二进制）。
- 中英双语 README、SKILL.md（任何具备命令行+读图能力的 AI Agent 可直接使用）。
