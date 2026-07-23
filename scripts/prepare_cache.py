# -*- coding: utf-8 -*-
"""prepare_cache.py — B站客户端本地缓存 → 可处理的音视频文件 + 元数据。

B站 PC 客户端缓存（如 C:\\Users\\<u>\\Videos\\bilibili\\<cid>\\）包含：
  - <cid>-<p>-3XXXX.m4s：DASH 双流分离缓存（一个纯音频、一个无音频的视频流），
    文件头有 9 字节 ASCII '0' 前缀，需剥到 ftyp 起始才能被 ffmpeg 读取；
  - videoInfo.json：元数据（title/groupTitle/bvid/aid/cid/duration/p）；
  - .playurl：流归属信息（备用，主要靠 ffprobe 分类）。

本脚本（只读原缓存目录，改动副本写在 out-dir 内）：
  1. 扫描 *.m4s，剥离 ftyp 前缀前的字节，写修复副本 cache_fixed/<stem>.mp4；
  2. ffprobe 分类并按画质、时长和起点选择最合适的音视频流组合；
  3. 读 videoInfo.json 提取元数据。

CLI：python scripts/prepare_cache.py --input <缓存目录> --out-dir <run目录>
约定同其他脚本：日志在前，最后一行 RESULT_JSON；出错 exit 1。
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from common import find_tool, run  # type: ignore
except ImportError:
    import shutil
    import subprocess

    def find_tool(name):
        tools_dir = SKILL_ROOT / "tools"
        for cand in (tools_dir / f"{name}.exe", tools_dir / name):
            if cand.is_file():
                return str(cand)
        return shutil.which(name)

    def run(cmd, timeout=600, check=True):
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令失败 exit={proc.returncode}: {(proc.stderr or '')[-300:]}")
        return proc


def log(msg):
    print(f"[cache] {msg}", flush=True)


def die(msg):
    print(f"[cache] 错误: {msg}", file=sys.stderr)
    print("RESULT_JSON: " + json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(1)


def ftyp_offset(path, head=4096):
    """返回 ftyp box 起始偏移；找不到返回 -1。B站缓存通常是 9（9 个 ASCII '0'）。"""
    with open(path, "rb") as f:
        data = f.read(head)
    return data.find(b"ftyp") - 4 if data.find(b"ftyp") >= 4 else -1


def fix_m4s(src: Path, dst: Path):
    """从 ftyp 偏移处起写修复副本（流式，不整读内存）。ftyp 已在 0 偏移则返回原路径。"""
    off = ftyp_offset(src)
    if off < 0:
        die(f"未找到 ftyp box（不是有效的 mp4/m4s？）: {src}")
    if off == 0:
        return src  # 无需修复
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(off)
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            fout.write(chunk)
    return dst


def fixed_copy_path(src: Path, source_root: Path, fixed_root: Path) -> Path:
    """为修复副本保留源目录层级，避免递归扫描时同名 stem 相互覆盖。"""
    relative = src.resolve().relative_to(source_root.resolve())
    return (fixed_root / relative).with_suffix(".mp4")


def _to_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_stream(stream):
    """把 ffprobe 单条 stream 转成稳定、可 JSON 序列化的元数据。"""
    result = {
        "index": _to_int(stream.get("index")),
        "type": stream.get("codec_type"),
        "codec": stream.get("codec_name"),
        "duration": _to_float(stream.get("duration")),
        "start_time": _to_float(stream.get("start_time")),
        "bitrate": _to_int(stream.get("bit_rate")),
    }
    if result["type"] == "video":
        result.update({
            "width": _to_int(stream.get("width")),
            "height": _to_int(stream.get("height")),
            "fps": stream.get("avg_frame_rate") or stream.get("r_frame_rate"),
        })
    elif result["type"] == "audio":
        result.update({
            "sample_rate": _to_int(stream.get("sample_rate")),
            "channels": _to_int(stream.get("channels")),
        })
    return result


def parse_probe_payload(payload):
    """解析 ffprobe JSON，保留选择与时间轴对齐所需的流级元数据。"""
    streams = [_normalise_stream(s) for s in (payload.get("streams") or [])]
    videos = [s for s in streams if s.get("type") == "video"]
    audios = [s for s in streams if s.get("type") == "audio"]
    fmt = payload.get("format") or {}

    def video_key(stream):
        width = stream.get("width") or 0
        height = stream.get("height") or 0
        return (width * height, stream.get("bitrate") or 0)

    def audio_key(stream):
        return (stream.get("bitrate") or 0, stream.get("channels") or 0)

    primary_video = max(videos, key=video_key) if videos else None
    primary_audio = max(audios, key=audio_key) if audios else None

    duration = _to_float(fmt.get("duration"))
    if duration is None:
        durations = [s["duration"] for s in streams if s.get("duration") is not None]
        duration = max(durations) if durations else None
    start_time = _to_float(fmt.get("start_time"))
    if start_time is None:
        starts = [s["start_time"] for s in streams if s.get("start_time") is not None]
        start_time = min(starts) if starts else None

    return {
        "probe_ok": True,
        "has_video": bool(videos),
        "has_audio": bool(audios),
        "duration": duration,
        "start_time": start_time,
        "bitrate": _to_int(fmt.get("bit_rate")),
        "width": primary_video.get("width") if primary_video else None,
        "height": primary_video.get("height") if primary_video else None,
        "video": primary_video,
        "audio": primary_audio,
        "stream_details": streams,
    }


def probe_streams(ffprobe: str, path: Path):
    """ffprobe 返回包含流类型、质量、时长与起始时间的元数据字典。"""
    proc = run([ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], check=False)
    if proc.returncode != 0:
        return {
            "probe_ok": False, "has_video": False, "has_audio": False,
            "duration": None, "start_time": None, "bitrate": None,
            "width": None, "height": None, "video": None, "audio": None,
            "stream_details": [], "probe_error": (proc.stderr or "")[-300:],
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "probe_ok": False, "has_video": False, "has_audio": False,
            "duration": None, "start_time": None, "bitrate": None,
            "width": None, "height": None, "video": None, "audio": None,
            "stream_details": [], "probe_error": "ffprobe 输出不是合法 JSON",
        }
    return parse_probe_payload(payload)


def video_quality_key(candidate):
    """视频候选排序键：像素数优先，其次视频/容器码率与时长。"""
    video = candidate.get("video") or {}
    width = video.get("width") or candidate.get("width") or 0
    height = video.get("height") or candidate.get("height") or 0
    bitrate = video.get("bitrate") or candidate.get("bitrate") or 0
    return (width * height, bitrate, candidate.get("duration") or 0.0)


def audio_quality_key(candidate):
    """没有视频可配对时的音频候选排序键。"""
    audio = candidate.get("audio") or {}
    bitrate = audio.get("bitrate") or candidate.get("bitrate") or 0
    channels = audio.get("channels") or 0
    return (bitrate, channels, candidate.get("duration") or 0.0)


def _stream_start(candidate, kind):
    stream = candidate.get(kind) or {}
    value = stream.get("start_time")
    return value if value is not None else candidate.get("start_time")


def select_stream_pair(candidates):
    """选择最佳视频，并选取时长最接近的视频对应音频。

    同一文件若同时含 A/V，会同时进入两侧候选，因此也可以被选作两边输入。
    """
    videos = [c for c in candidates if c.get("has_video")]
    audios = [c for c in candidates if c.get("has_audio")]
    video = max(videos, key=video_quality_key) if videos else None

    if not audios:
        return video, None
    if video is None:
        return None, max(audios, key=audio_quality_key)

    video_duration = video.get("duration")
    video_start = _stream_start(video, "video")

    def pairing_key(audio):
        audio_duration = audio.get("duration")
        if video_duration is None or audio_duration is None:
            duration_delta = float("inf")
        else:
            duration_delta = abs(float(audio_duration) - float(video_duration))
        audio_start = _stream_start(audio, "audio")
        if video_start is None or audio_start is None:
            start_delta = float("inf")
        else:
            start_delta = abs(float(audio_start) - float(video_start))
        quality = audio_quality_key(audio)
        return (duration_delta, start_delta, -quality[0], -quality[1], str(audio.get("path") or ""))

    return video, min(audios, key=pairing_key)


def selected_stream_metadata(candidate, kind):
    """输出 watch/后续处理可直接消费的已选流摘要。"""
    if candidate is None:
        return None
    stream = candidate.get(kind) or {}
    result = {
        "path": candidate.get("path"),
        "source_path": candidate.get("source_path"),
        "stream_index": stream.get("index"),
        "codec": stream.get("codec"),
        "duration": candidate.get("duration"),
        "start_time": _stream_start(candidate, kind),
        "bitrate": stream.get("bitrate") or candidate.get("bitrate"),
    }
    if kind == "video":
        result.update({
            "width": stream.get("width") or candidate.get("width"),
            "height": stream.get("height") or candidate.get("height"),
        })
    else:
        result.update({
            "sample_rate": stream.get("sample_rate"),
            "channels": stream.get("channels"),
        })
    return result


def build_timeline_metadata(video, audio):
    """汇总已选 A/V 的源时间轴信息。"""
    video_start = _stream_start(video, "video") if video else None
    audio_start = _stream_start(audio, "audio") if audio else None
    start_delta = (
        float(audio_start) - float(video_start)
        if audio_start is not None and video_start is not None else None
    )
    video_duration = video.get("duration") if video else None
    audio_duration = audio.get("duration") if audio else None
    return {
        "unit": "seconds",
        "origin": "source_stream",
        "video_start_time": video_start,
        "audio_start_time": audio_start,
        "audio_minus_video_start": start_delta,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "duration": video_duration if video_duration is not None else audio_duration,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="B站客户端本地缓存 → 修复副本 + 元数据")
    ap.add_argument("--input", required=True, help="缓存目录（含 *.m4s 与 videoInfo.json）")
    ap.add_argument("--out-dir", required=True, help="修复副本与产物输出目录")
    args = ap.parse_args(argv)

    src_dir = Path(args.input).resolve()
    if not src_dir.is_dir():
        die(f"缓存目录不存在: {src_dir}")
    out_dir = Path(args.out_dir).resolve()
    fixed_dir = out_dir / "cache_fixed"

    m4s_files = sorted(src_dir.rglob("*.m4s"))
    if not m4s_files:
        die(f"目录内未找到 .m4s 缓存文件: {src_dir}")
    log(f"发现 {len(m4s_files)} 个 m4s 文件")

    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        die("未找到 ffprobe：请先运行 python scripts/setup.py --install")

    candidates = []
    for f in m4s_files:
        fixed = fix_m4s(f, fixed_copy_path(f, src_dir, fixed_dir))
        probed = probe_streams(ffprobe, fixed)
        candidate = {
            **probed,
            "path": str(Path(fixed).resolve()),
            "source_path": str(f.resolve()),
        }
        candidates.append(candidate)
        log(
            f"  {f.relative_to(src_dir)}: 视频流={candidate['has_video']} "
            f"音频流={candidate['has_audio']} 时长={candidate['duration']} "
            f"分辨率={candidate['width']}x{candidate['height']} "
            f"码率={candidate['bitrate']} 起点={candidate['start_time']} "
            f"→ {Path(fixed).relative_to(out_dir) if Path(fixed).is_relative_to(out_dir) else Path(fixed).name}"
        )

    videos = [c for c in candidates if c.get("has_video")]
    audios = [c for c in candidates if c.get("has_audio")]
    if len(videos) > 1 or len(audios) > 1:
        log(f"⚠️ 检测到多路流（视频 {len(videos)} / 音频 {len(audios)}），按质量与时长配对")

    selected_video, selected_audio = select_stream_pair(candidates)
    if selected_video is None and selected_audio is None:
        die("缓存文件均不含可解码的音频或视频流")
    video_path = selected_video.get("path") if selected_video else None
    audio_path = selected_audio.get("path") if selected_audio else None
    timeline = build_timeline_metadata(selected_video, selected_audio)
    duration = timeline.get("duration")
    if selected_video:
        log(
            f"选择视频: {Path(video_path).name} "
            f"({selected_video.get('width')}x{selected_video.get('height')}, "
            f"{selected_video.get('duration')}s)"
        )
    if selected_audio:
        log(f"配对音频: {Path(audio_path).name} ({selected_audio.get('duration')}s)")

    # 元数据（可选）
    meta = {}
    info_file = src_dir / "videoInfo.json"
    if info_file.is_file():
        try:
            meta = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            log(f"⚠️ videoInfo.json 解析失败（忽略）: {e}")
    title = meta.get("title") or meta.get("groupTitle") or src_dir.name
    if meta.get("groupTitle") and meta.get("title") and meta["groupTitle"] != meta["title"]:
        title = f"{meta['groupTitle']} - {meta['title']}"
    duration = duration or meta.get("duration")
    if timeline.get("duration") is None:
        timeline["duration"] = duration

    log(f"标题: {title} | bvid={meta.get('bvid')} | 时长={duration}")
    print("RESULT_JSON: " + json.dumps({
        "ok": True, "source": "bilibili-cache",
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "title": title, "duration": duration,
        "bvid": meta.get("bvid"), "aid": meta.get("aid"), "cid": meta.get("cid"),
        "page": meta.get("p") or 1,
        "streams": {"video": len(videos), "audio": len(audios)},
        "selected_streams": {
            "video": selected_stream_metadata(selected_video, "video"),
            "audio": selected_stream_metadata(selected_audio, "audio"),
        },
        "stream_metadata": candidates,
        "timeline": timeline,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
