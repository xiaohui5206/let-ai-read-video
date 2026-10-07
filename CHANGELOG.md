# 更新日志 / Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

All notable changes to this project will be documented here.
Format follows Keep a Changelog; versioning follows SemVer.

---

## [Unreleased] - 二次检查

- README10：按已选产物汇总总任务与通道状态，后处理失败不再显示全部完成。
- 历史快照检查文件可用性；失效预览/PDF禁用，保留路径与重新生成，文字稿可回退原始文件。
- 刷新恢复 cancelling 的轮询与通道占用，取消结束后解锁。
- 支持 `--runs-dir` / `VIDEO_WATCH_RUNS_DIR`，e2e 自动隔离产物/数据库并检查日常历史未变化；实例复用也匹配产物根目录。
- 当前验证：245 项 Python、7 项 JS 回归、38 项真实端到端断言通过；更新本地 GitHub 提交版，详见 README10。
- 修复 B站 URL 分P、批量逐条媒体缓存隔离；范围/全部选集不再错误复用单个文件，旧缓存键隔离。
- 恢复隐藏帧后的计数在刷新、服务重启后保持一致，并发恢复不会覆盖索引。
- 纯文字稿忽略关键帧设置；修复单视频重试参数、重试通道锁；最新结果置顶。
- 修复移动端长错误路径撑宽工作区的问题；390px 视口实测无横向溢出。
- 间隔/关键词/去重后处理支持取消，修复进程注册竞态与取消被覆盖成完成的问题。
- 新增 10 项 Python、4 项 JS 回归及 2 项真实恢复/重启断言；详见 `docs/FOLLOWUP_REVIEW.md`。

## [2.0.0] - 2026-10-07

本地源码版本；未创建远程 Release。与历史 v1.2.0 的功能差异见 `docs/VERSION_NOTES.md`。

### 完善 / Completed（第 4 批）
- PDF 前端可选每页 1/2/6 帧，后端含标题、时间戳、页码；标题字符串转义与长度保护。
- 抽帧宽度 512/768/1024 前后端贯通，偏好持久化。
- 去重增加亮度与 64×64 局部变化保护，保留关键字帧；固定真 JPEG 回归覆盖黑白切换、PPT 新增 NOTE 与完全重复。
- `keyword_report.json` 保留全部命中句、关键词、前后文、句子中点、取样/取帧结果与跳过原因；超过 60 命中展示总数和取样数，前端按需展开分页。
- 启动器识别 Python、版本、端口及内部异常；同项目实例复用，独占端口绑定，绑定成功后才更改历史任务状态。
- 版本核对、离线前提、标准库范围与去重恢复说明；公开交接文档不包含私人原始聊天记录。
- 扩充 PDF/去重/关键字/启动回归；修复真实端到端脚本对已设置 PYTHONIOENCODING 环境变量的兼容性。

### 新增 / Added
- **本地 Web 界面**（`scripts/webui.py` + `webui/index.html` + `启动WebUI.bat`）：
  给命令行流水线套上脱离 AI Agent 的网页壳，零新增依赖（标准库 `http.server`，
  仅监听 `127.0.0.1`）。支持单个视频 / 多P选集 / 批量链接三种模式，实时滚动任务日志，
  产物（文字稿 / 帧图）可在线预览；`/api/files` 仅限 `runs/` 目录内文件（防路径穿越）。
- Web UI 双通道进度条（文字稿 / 关键帧）：由 watch.py/transcribe.py/frames.py
  日志锚点驱动，按钮按通道独立禁用（可分头为同一链接补生成另一产物）。
- **关键帧导出 PDF**（`scripts/make_frames_pdf.py`）：手写 PDF 生成器（零第三方依赖），
  JPEG 以 DCTDecode 原样嵌入、Helvetica caption 标注 `#N  t=MM:SS`，A4 可选每页 1/2/6 帧；
  Web UI 结果卡片一键导出 `关键帧.pdf` 到任务目录。
- **Web UI 侧栏三功能做实**：
  - 关键帧截取原则：正常 / 目标帧数（`--budget`）/ 严格间隔（显式时间点）；
  - 关键字定位关键帧：按关键字回归文字稿时间位置，postprocess 调
    `frames.py --times-json --append --pass-id kw1` 定向补帧；
  - 关键帧去重：aHash（16×16 灰度）相似度阈值 0.80–0.99，剔除帧只在
    frames.json 标记 `dropped:true` + `dedup_similarity`（原子写），不删文件，
    并生成 `dedup_report.json`；帧图浏览浮层过滤 dropped 帧并显示时间戳。
- `tests/test_webui.py` / `tests/test_frames_pdf.py`：Web UI 与 PDF 导出单元测试
  （命令拼装、RESULT_JSON 解析、进度锚点、关键字定位、aHash 去重、路径安全、
  mock 子进程任务生命周期、PDF xref 结构自检）。

### 修复 / Fixed（第 1 批正确性修复，对应实测报告 C01–C08）
- **C01 下载后媒体复检**：`probe.py` 区分「codec 明确为 none」（确定无轨道）与
  「字段缺失」（未知→保守按有），formats 缺 codec 字段不再误判 has_video=False；
  `watch.py` 下载完成后用 ffprobe 对实际文件复检 duration/has_video/has_audio
  并覆盖 probe 元数据；多集模式每集独立复检，不再用首集布尔值兜底后续集。
- **C02 产物级状态**：result 增加 `artifacts.transcript/frames`（succeeded/skipped/
  empty/failed + reason，如"无音轨""无视频流"）；批量整体状态新增 `partial`
  （部分失败不再被任一成功吞掉）；results 的 url 统一 `redact_url` 脱敏（T05）。
- **C03 抽帧设置语义**：自定义帧数改传 `--budget N`（目标 N 帧，仍受 2fps/100
  硬上限），前后端严格拒绝小数/bool；自定义间隔改为显式 `start + k×X` 时间点
  走 `frames.py --times-json` 基础轮（禁场景点混入），20 秒 5 秒间隔恰好 0/5/10/15。
- **C04 实时日志**：`watch.py` `run_step` 从 `subprocess.run(capture_output=True)`
  改为 Popen 流式逐行转发（stderr 并入 stdout 防管道死锁），保留 timeout 杀进程、
  退出码检查、末行 RESULT_JSON 倒序解析与 fatal/raise_on_fail 三级语义。
- **C05 批次进度**：通道 percent = (已完成项数 + 当前项内进度)/总项数；multi 总数
  从 `第 N 集（i/total）` 日志或聚合 RESULT 推断，未知显示不定态；100% 只在含
  关键字补帧/去重等后处理全部结束后置位（后处理期间显示"整理结果…"）。
- **C06 去重数量口径**：result 统一 `total_count / kept_count / dropped_count`；
  `关键帧/` 中文副本只复制保留帧（预览、PDF、副本数量一致，原 frames/ 全量保留）；
  ffmpeg 哈希读取失败的帧计入 kept。
- **C07 关键字补帧同步 manifest**：补帧后原子更新 manifest.json 的 frames.count
  与 passes（upsert pass_id kw1，相同参数重跑幂等不重复计数）。
- **C08 关键字通道并发冲突**：前端统一 `requiredChannels(action, options)`，
  勾选关键字时「生成关键帧」在文字稿通道在跑时禁用并显示原因；提交瞬间进入
  submitting 态防快速双击建两个任务；通道有活动 job 时禁止新 job 接管。

### 新增 / Added（第 2 批界面主流程改版，对应实测报告 U01–U11）
- **U01 布局**：三列改为主工作区 + 320px「生成设置」侧栏（内容上限 1280px）；
  <1100px 设置折叠可展开；<768px 单列按 输入→操作→进度→结果→设置 排序。
- **U02 视觉体系**：落地报告 5.1 CSS token（#F4F6F9/#FFF、#172033/#526079、
  字级、间距档位、圆角 8/12、控件高 40–44px、`:focus-visible`）；状态改用 CSS
  圆点不再依赖 emoji；长路径缩短显示 + 悬停全量 + 复制；环境横幅可收起。
- **U03 主操作**：按钮下方一行当前设置摘要（来源 · 截取原则 · 去重 · 关键字）。
- **U04 设置联动**：未勾选的关键字输入/去重滑块/未选中截取原则输入项全部禁用；
  纯文字稿不校验抽帧设置；滑块标注"阈值越低，合并越积极"。
- **U05 来源入口**：新增 视频链接 / 本地文件 / B站缓存目录 来源切换；
  本地路径 Windows 引号清理 + `POST /api/check_path` 存在性提示；仅接受路径粘贴。
- **U06 选集与批量检查**：新增 `POST /api/probe` 只探测不下载（60s 超时）；
  多P「解析分P」→ 勾选清单（序号/标题/时长，默认不全选），连续区间合并为
  `--item` 表达式，多段选集走批量逐条选集（batch urls 支持 `{"url", "item"}`）；
  批量输入实时显示有效/重复/无效行数。
- **U09 环境能力视图**：/api/health 区分 检测到显卡 / CUDA 运行库与可见设备 /
  faster-whisper / 模型缓存；缺转写包不阻断纯抽帧提示；前端分项展示 +
  「重新检测」+ 安装命令复制；检测失败显示"未知"。
- **U10 行内反馈**：全面移除 `alert()`——表单错误落在字段旁、复制成功轻量
  toast、轮询 404 立即终结等待、断网显示"连接中断，后台任务可能仍在运行"
  并指数退避重连；错误不清空用户输入。
- **U11 无障碍**：label/for 配对；tablist/tab/tabpanel 角色 + 方向键切换；
  进度条 role=progressbar + aria-valuenow；帧图浮层 role=dialog、Escape 关闭、
  焦点约束、关闭后焦点还原；尊重 prefers-reduced-motion。

### 新增 / Added（第 3 批任务体验与工程补强，对应实测报告 T01–T06、U07、U08）
- **T01 任务持久化**：标准库 sqlite3 落库 `runs/.webui_jobs.db`（jobs 表含
  参数/状态/进度/结果/日志尾部摘要，运行中 0.5s 节流）；新端点 `GET /api/jobs`
  （倒序 + limit/offset 分页），`GET /api/jobs/<id>` 内存 miss 时查库；
  服务重启后库里 running/queued 一律标 `interrupted`（诚实提示可重试，不伪装续跑）；
  页面加载恢复在跑任务与最近结果；localStorage 只存表单偏好。
- **T02 排队与取消**：调度器（`scripts/webui_jobs.py`）——含转写的重型任务全局
  并发 1、纯抽帧并发 2，队列位置可见（任务卡"排队中 #N"）；状态机
  queued/running/cancelling/cancelled/done/partial/error/interrupted；
  `POST /api/jobs/<id>/cancel`：排队直接取消，运行中 `taskkill /PID /T /F`
  杀本任务整棵进程树（含 watch.py 子孙的 ffmpeg/whisper，不碰其他进程）。
- **T03 同源复用与归组**：`media_key`（B站=BV+分P；普通 URL 去跟踪参数保留内容
  参数——不用脱敏 URL 作键；本地文件=路径+mtime+size；缓存目录=路径+指纹）；
  media_cache 表 + 同源写锁（600s 等待超时记日志）；纯抽帧任务复用已下载媒体
  免重复下载（转写任务仍需平台字幕总是下载），manifest 记 `reused_from`；
  前端结果按同源媒体键归组为组卡。
- **T04 下载失败可诊断**：`classify_download_error` 把 yt-dlp 错误归为
  连接/无数据/需登录或地区限制/提取器四类 + 中文建议（指引本地文件/缓存入口）；
  连接与无数据类自动重试一次（可取消）；失败结果卡有「重试」（按原参数新建任务）。
- **T05 严格校验收尾**：want_*/dedup.enabled 必须真 bool，整数拒绝 bool/小数，
  字符串字段拒绝非字符串，urls 元素必须字符串或 {url,item} 对象；
  结果 error 统一 `redact_text_urls` 脱敏。
- **T06 拆分与真实回归**：前端拆为 `webui/index.html` + `app.css` + `app.js`，
  新增 `/static/<file>` 路由（白名单后缀 + 目录边界防穿越）；新增 `.gitattributes`
  （py/html/css/js/md → LF，bat → CRLF）；新增 `tests/e2e_real.py` 真实端到端
  回归（不进 unittest discover）：ffmpeg lavfi 造有声/无音轨/纯音频/纯红媒体，
  走真实 HTTP 全流程覆盖 C01–C08 关键验收点。
- **U07 文字稿阅读区**：任务卡内「阅读文字稿」——结构化 segments 渲染（时间戳 +
  文本，textContent 防注入），固定工具栏（搜索高亮 + 计数 + 上下跳转、带/不带
  时间戳复制、下载 TXT/SRT），长稿每 100 段分页加载，点时间戳定位最近关键帧；
  无文字稿时显示产物状态原因。
- **U08 帧图查看器**：缩略图固定比例占位；点击进大图（前后切换、方向键、Escape
  逐级退出、复制时间戳）；「仅看保留 / 查看已隐藏」筛选；已隐藏帧可「恢复此帧」
  （新端点 `POST /api/dedup_restore`：去掉 dropped 标记、原子写、同步
  dedup_report 与卡片/PDF 口径）；frames.json 损坏明确报错、不再无提示回退全量目录。

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
