#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download.py — 用 yt_dlp 下载媒体与字幕到指定目录。

用法:
    python scripts/download.py --url <URL> --out-dir <目录>
        [--audio-only] [--max-height 720] [--no-captions]

行为:
  * 视频:  format = bestvideo[height<=N][ext=mp4]+bestaudio/best[height<=N]/best，
           合并输出 mp4（需要 ffmpeg，tools/ 优先其次 PATH）。
  * 音频:  --audio-only 时只下 bestaudio（兜底 best）并转 m4a。
  * 字幕:  手动字幕优先于自动生成；语言优先 zh-Hans/zh-Hant/zh/en（前缀匹配），
           一级都没命中则下载全部可用字幕兜底；统一存 .vtt。
           实现上分两趟：手动字幕随媒体一起下；自动字幕单独一趟(skip_download)，
           只为没有手动字幕的语言补自动字幕，避免同名 .vtt 互相覆盖。
  * 下载完成后扫描 out-dir 里实际生成的 .vtt 文件回填 captions 路径。

约定:
  * 日志打在 stdout 前面；最后一行 stdout 固定输出单行 JSON:  RESULT_JSON: {...}
  * 出错写 stderr、退出码 1，最后一行 RESULT_JSON: {"ok": false, "error": "..."}
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础环境：Windows 控制台强制 UTF-8（无法编码的字符替换而非崩溃）
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SKILL_ROOT = Path(__file__).resolve().parent.parent   # skill 根目录
SCRIPTS_DIR = Path(__file__).resolve().parent         # scripts/ 目录
RESULT_PREFIX = "RESULT_JSON: "

# 字幕语言优先档（前缀匹配，按此顺序排序）；一档都没命中则“全部可用”兜底
PREF_TIERS = ("zh-hans", "zh-hant", "zh", "en")


def log(msg: str) -> None:
    """普通日志，打在 RESULT_JSON 之前。"""
    print(msg, flush=True)


def emit(payload: dict) -> None:
    """最后一行 stdout：单行 JSON 结果（供调用方解析）。"""
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def fail(msg: str) -> None:
    """统一错误出口：stderr + RESULT_JSON(ok=false) + 退出码 1。"""
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    emit({"ok": False, "error": msg})
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 复用 common.find_tool（冻结契约）。common.py 缺失时启用最小兜底实现。
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from common import find_tool  # type: ignore
except Exception:  # pragma: no cover - 兜底分支

    def find_tool(name):
        """先 <SKILL>/tools/<name>.exe，再 PATH；找不到返回 None。"""
        exe = name if name.lower().endswith(".exe") else f"{name}.exe"
        cand = SKILL_ROOT / "tools" / exe
        if cand.is_file():
            return str(cand)
        return shutil.which(name) or shutil.which(exe)


def lang_rank(lang):
    """字幕语言排序键：zh-Hans > zh-Hant > 其他 zh* > en* > 其他（字母序）。"""
    ll = str(lang).lower()
    for i, p in enumerate(PREF_TIERS):
        if ll.startswith(p):
            return (i, ll)
    return (len(PREF_TIERS), ll)


def to_float(v):
    """宽松转 float；None/非法/NaN → None。"""
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # NaN 检查
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 字幕语言选择（返回可用语言标签的精确列表）
# ---------------------------------------------------------------------------
def choose_subtitle_langs(manual_langs: set, auto_langs: set) -> list:
    """优先 zh*/en*（前缀匹配，按优先档排序）；都没命中则全部可用字幕兜底。"""
    available = set(manual_langs) | set(auto_langs)
    if not available:
        return []
    preferred = sorted((l for l in available
                        if l.lower().startswith(("zh", "en"))), key=lang_rank)
    if preferred:
        return preferred
    return sorted(available, key=lang_rank)  # “全部兜底”


def build_media_opts(args, out_dir: Path, manual_chosen: list,
                     ffmpeg_location: str | None) -> dict:
    """第一趟：下载媒体本体（+ 手动字幕）。"""
    opts = {
        # 标题截断 100 字符，规避 Windows 长路径问题
        "outtmpl": str(out_dir / "%(title).100s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,          # 保证 stdout 干净，RESULT_JSON 恒为最后一行
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": False,         # 同目录重跑时幂等跳过
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    if args.audio_only:
        # bestaudio 兜底 best：无独立音轨的站点也能产出 m4a
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "192",
        }]
    else:
        mh = int(args.max_height)
        opts["format"] = (f"bestvideo[height<={mh}][ext=mp4]+bestaudio"
                          f"/best[height<={mh}]/best")
        opts["merge_output_format"] = "mp4"
    if manual_chosen:
        opts.update({
            "writesubtitles": True,
            "subtitleslangs": manual_chosen,   # 精确语言标签，无正则歧义
            "subtitlesformat": "vtt",
        })
    return opts


def build_auto_sub_opts(out_dir: Path, auto_chosen: list) -> dict:
    """第二趟：只补自动字幕（skip_download，不下媒体）。"""
    return {
        "outtmpl": str(out_dir / "%(title).100s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "writeautomaticsubs": True,
        "subtitleslangs": auto_chosen,
        "subtitlesformat": "vtt",
        "overwrites": False,
    }


def resolve_media_path(info: dict, out_dir: Path, audio_only: bool,
                       since: float) -> Path | None:
    """定位下载产物：先 requested_downloads，再按修改时间扫描目录兜底。"""
    for d in (info or {}).get("requested_downloads") or []:
        fp = d.get("filepath")
        if fp and Path(fp).is_file():
            return Path(fp)
    exts = {".m4a", ".mp3", ".wav", ".aac"} if audio_only else {".mp4", ".mkv", ".webm", ".m4a"}
    try:
        cands = [p for p in out_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in exts
                 and p.stat().st_mtime >= since - 5]
    except OSError:
        cands = []
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def scan_captions(out_dir: Path, media: Path | None, manual_langs: set,
                  since: float) -> list:
    """扫描 out-dir 里本次实际生成的 .vtt，回填 [{lang, kind, path}]。

    排序即选用优先级：manual 在前；同 kind 内 zh-Hans>zh-Hant>zh*>en*>其他。
    """
    try:
        vtts = [p for p in out_dir.glob("*.vtt")
                if p.is_file() and p.stat().st_mtime >= since - 5]
    except OSError:
        return []
    if media is not None:
        stem = media.stem
        related = [p for p in vtts if p.stem == stem or p.stem.startswith(stem + ".")]
        if related:
            vtts = related  # 只认与媒体同名前缀的字幕，排除目录里的旧文件

    caps = []
    for p in vtts:
        stem = p.stem  # yt-dlp 字幕命名: <媒体名>.<lang>.vtt
        if media is not None and stem.startswith(media.stem + "."):
            lang = stem[len(media.stem) + 1:]
        else:
            lang = stem.rsplit(".", 1)[-1] if "." in stem else "und"
        caps.append({
            "lang": lang,
            "kind": "manual" if lang in manual_langs else "auto",
            "path": str(p),
        })
    caps.sort(key=lambda c: (0 if c["kind"] == "manual" else 1,
                             lang_rank(c["lang"]), c["lang"]))
    return caps


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="用 yt_dlp 下载视频(mp4)/音频(m4a)与字幕(vtt)到指定目录。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--url", required=True, help="视频页面 URL（http/https）")
    ap.add_argument("--out-dir", required=True, help="输出目录（不存在则创建）")
    ap.add_argument("--audio-only", action="store_true",
                    help="只下载音轨并转换为 m4a（仍会按规则下载字幕）")
    ap.add_argument("--max-height", type=int, default=720,
                    help="视频流最大高度（像素），超限自动降档，最终兜底 best")
    ap.add_argument("--no-captions", action="store_true",
                    help="不下载任何字幕（captions 返回空列表）")
    args = ap.parse_args(argv)

    try:
        import yt_dlp  # 延迟导入：未安装不影响其他脚本
    except ImportError:
        fail("缺少 yt-dlp：请先运行 python scripts/setup.py --install（或 pip install yt-dlp）")

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fail(f"无法创建输出目录 {out_dir}: {e}")
    out_dir = out_dir.resolve()

    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        ffmpeg_location = str(Path(ffmpeg).parent)
        log(f"ffmpeg 位置: {ffmpeg_location}")
    else:
        ffmpeg_location = None
        log("警告: 未找到 ffmpeg，mp4 合并 / m4a 转码可能失败"
            "（可先运行 python scripts/setup.py --install）")

    # ---- 阶段 1：只取元数据，决定字幕语言 ------------------------------------
    log(f"读取元数据: {args.url}")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(args.url, download=False)
    except Exception as e:
        fail(f"元数据获取失败: {type(e).__name__}: {e}")
    if not info:
        fail("yt_dlp 未返回任何元数据")
    if info.get("entries") is not None:
        fail("暂不支持播放列表 URL，请提供单个视频链接")

    manual_langs = set((info.get("subtitles") or {}).keys())
    auto_langs = set((info.get("automatic_captions") or {}).keys())

    if args.no_captions:
        chosen: list = []
    else:
        chosen = choose_subtitle_langs(manual_langs, auto_langs)
    # 同一语言手动优先：自动字幕只补没有手动字幕的语言
    manual_chosen = [l for l in chosen if l in manual_langs]
    auto_chosen = [l for l in chosen if l not in manual_langs and l in auto_langs]
    if args.no_captions:
        log("按参数要求跳过字幕下载")
    elif not chosen:
        log("该视频没有可用字幕")
    else:
        log(f"字幕计划: 手动 {manual_chosen or '无'}；自动 {auto_chosen or '无'}"
            f"（可用: 手动 {len(manual_langs)} 种 / 自动 {len(auto_langs)} 种）")

    # ---- 阶段 2：下载媒体（+ 手动字幕） --------------------------------------
    since = time.time()
    media_opts = build_media_opts(args, out_dir, manual_chosen, ffmpeg_location)
    kind_desc = "音频(m4a)" if args.audio_only else f"视频(mp4, ≤{args.max_height}p)"
    log(f"开始下载{kind_desc} -> {out_dir}")
    try:
        with yt_dlp.YoutubeDL(media_opts) as ydl:
            try:
                # 复用阶段 1 的 info 直接下载，避免重复请求元数据
                info2 = ydl.process_ie_result(dict(info), download=True)
            except AttributeError:
                ydl.download([args.url])  # 兼容极老版本 yt-dlp
                info2 = info
    except Exception as e:
        fail(f"下载失败: {type(e).__name__}: {e}")

    media = resolve_media_path(info2 or info, out_dir, args.audio_only, since)
    if media is None:
        fail("下载结束但未在输出目录找到媒体文件")
    log(f"媒体文件: {media}")

    # ---- 阶段 3：补自动字幕（只下字幕，不下媒体） -----------------------------
    if auto_chosen and not args.no_captions:
        log(f"补下自动字幕: {auto_chosen}")
        try:
            with yt_dlp.YoutubeDL(build_auto_sub_opts(out_dir, auto_chosen)) as ydl:
                try:
                    ydl.process_ie_result(dict(info), download=True)
                except AttributeError:
                    ydl.download([args.url])
        except Exception as e:
            # 自动字幕补齐失败不致命：媒体已在手，降级为警告继续
            log(f"警告: 自动字幕下载失败（忽略）: {type(e).__name__}: {e}")

    # ---- 阶段 4：扫描实际生成的 .vtt，回填 captions --------------------------
    captions = [] if args.no_captions else scan_captions(out_dir, media, manual_langs, since)
    for c in captions:
        log(f"字幕[{c['kind']}] {c['lang']}: {c['path']}")

    duration = to_float((info2 or {}).get("duration"))
    if duration is None:
        duration = to_float(info.get("duration"))
    emit({
        "ok": True,
        "video_path": str(media),
        "title": (info2 or {}).get("title") or info.get("title") or "",
        "duration": round(duration, 3) if duration is not None else None,
        "captions": captions,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # 兜底：任何未预期异常都按契约输出 RESULT_JSON
        fail(f"未预期错误: {type(e).__name__}: {e}")
