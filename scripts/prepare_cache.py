# -*- coding: utf-8 -*-
"""prepare_cache.py — B站客户端本地缓存 → 可处理的音视频文件 + 元数据。

B站 PC 客户端缓存（如 C:\\Users\\<u>\\Videos\\bilibili\\<cid>\\）包含：
  - <cid>-<p>-3XXXX.m4s：DASH 双流分离缓存（一个纯音频、一个无音视频），
    文件头有 9 字节 ASCII '0' 前缀，需剥到 ftyp 起始才能被 ffmpeg 读取；
  - videoInfo.json：元数据（title/groupTitle/bvid/aid/cid/duration/p）；
  - .playurl：流归属信息（备用，主要靠 ffprobe 分类）。

本脚本（只读原缓存目录，改动副本写在 out-dir 内）：
  1. 扫描 *.m4s，剥离 ftyp 前缀前的字节，写修复副本 cache_fixed/<stem>.mp4；
  2. ffprobe 分类：含视频流 → 视频文件；仅音频 → 音频文件（多对取第一对并告警）；
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


def probe_streams(ffprobe: str, path: Path):
    """ffprobe 返回 (has_video, has_audio, duration)。"""
    proc = run([ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], check=False)
    if proc.returncode != 0:
        return False, False, None
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, False, None
    has_v = any(s.get("codec_type") == "video" for s in d.get("streams", []))
    has_a = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
    try:
        dur = float(d.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        dur = None
    return has_v, has_a, dur


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

    videos, audios = [], []
    for f in m4s_files:
        fixed = fix_m4s(f, fixed_dir / (f.stem + ".mp4"))
        has_v, has_a, dur = probe_streams(ffprobe, fixed)
        log(f"  {f.name}: 视频流={has_v} 音频流={has_a} 时长={dur} → {Path(fixed).name}")
        if has_v:
            videos.append((fixed, dur))
        elif has_a:
            audios.append((fixed, dur))
    if len(videos) > 1 or len(audios) > 1:
        log(f"⚠️ 检测到多路流（视频 {len(videos)} / 音频 {len(audios)}），取第一路")

    video_path = videos[0][0] if videos else None
    audio_path = audios[0][0] if audios else None
    duration = (videos or audios or [(None, None)])[0][1]

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

    log(f"标题: {title} | bvid={meta.get('bvid')} | 时长={duration}")
    print("RESULT_JSON: " + json.dumps({
        "ok": True, "source": "bilibili-cache",
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "title": title, "duration": duration,
        "bvid": meta.get("bvid"), "aid": meta.get("aid"), "cid": meta.get("cid"),
        "page": meta.get("p") or 1,
        "streams": {"video": len(videos), "audio": len(audios)},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
