# -*- coding: utf-8 -*-
"""transcribe.py — 视频/音频/字幕 → 转写三件套（transcript.srt / .txt / .json）

三条输入路径（互斥，必选其一）：
  --vtt F    解析 VTT/SRT 字幕（去 NOTE/样式块、去自动字幕滚动重复、相同文本连续块合并），
             直接产出三件套，RESULT_JSON 中 engine 报 "captions"。
  --video V  先用 ffmpeg 抽取 16kHz 单声道 audio.wav 到 out-dir，再做 ASR。
  --audio A  直接对音频文件做 ASR。

ASR 引擎：
  faster-whisper（默认）：WhisperModel(model, device="cpu", compute_type=...)，
      transcribe(vad_filter=not --no-vad, condition_on_previous_text=False,
      language=None if auto else --language)。
  sensevoice：延迟 import funasr（AutoModel, model="iic/SenseVoiceSmall"），
      未安装时报错并提示 `pip install funasr torch torchaudio`。

约定（与其他脚本拼装）：
  - 日志打 stdout 前面；最后一行 stdout 为单行 JSON：`RESULT_JSON: {...}`；
    出错写 stderr、退出码 1，且最后一行 `RESULT_JSON: {"ok": false, "error": "..."}`。
  - 任意 cwd 下以 `python scripts/transcribe.py ...` 运行；skill 根 = 本文件上级目录的上级。
  - 第三方库（faster_whisper / funasr）只在函数内延迟 import，未装依赖时
    `python -m py_compile` 与 --vtt 路径仍可用。
"""

import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础设施：skill 根、common 导入（带最小回退，保证 common 缺失时本文件仍可用）
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    # ffmpeg 查找与时间戳显示统一走 common
    from common import find_tool, fmt_ts, parse_time  # type: ignore
except ImportError:  # pragma: no cover - common 尚未就绪时的最小回退，语义与契约一致
    def find_tool(name):
        """工具查找顺序：<SKILL>/tools/<name>.exe → PATH。"""
        tools_dir = SKILL_ROOT / "tools"
        for cand in (tools_dir / f"{name}.exe", tools_dir / name):
            if cand.is_file():
                return str(cand)
        return shutil.which(name)

    def fmt_ts(t):
        """时间戳显示：MM:SS；≥1 小时用 HH:MM:SS。"""
        t = max(0, int(round(float(t))))
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def parse_time(value):
        """解析秒数、MM:SS 或 HH:MM:SS。"""
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            text = str(value).strip()
            if not text:
                raise ValueError("时间字符串为空")
            parts = text.split(":")
            if len(parts) == 1:
                result = float(parts[0])
            elif len(parts) == 2:
                result = float(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                result = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            else:
                raise ValueError(f"无法解析时间: {value!r}")
        if result < 0:
            raise ValueError(f"时间不能为负: {value}")
        return result


class Die(Exception):
    """可预期的致命错误：打印 stderr + RESULT_JSON(ok=false) 后退出码 1。"""


def die(msg):
    raise Die(str(msg))


def log(msg):
    print(f"[transcribe] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 字幕文件解析（VTT / SRT）
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]*>")  # <c>、</c>、<00:00:01.500> 等内联标签
_WS_RE = re.compile(r"\s+")
_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " ",
}


def _parse_caption_ts(s):
    """解析字幕时码：支持 HH:MM:SS.mmm / MM:SS.mmm，逗号或点号分隔毫秒。"""
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    try:
        sec = float(parts[-1])
        if len(parts) == 3:
            sec += int(parts[0]) * 3600 + int(parts[1]) * 60
        elif len(parts) == 2:
            sec += int(parts[0]) * 60
        else:
            raise ValueError
    except ValueError:
        die(f"无法解析字幕时码: {s!r}")
    return sec


def parse_caption_file(path):
    """解析 VTT/SRT 为原始 cue 列表 [(start, end, [文本行...])]。

    只响应含 "-->" 的行，因此天然跳过 WEBVTT 头、NOTE/STYLE/REGION 块、
    SRT 序号行与 VTT cue 标识符。
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        die(f"读取字幕文件失败 {path}: {e}")
    lines = text.splitlines()
    cues = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            start_s, end_s = line.split("-->", 1)
            # 结束时间后可能跟 VTT cue 设置（如 position:10%），只取第一个 token
            start = _parse_caption_ts(start_s)
            end = _parse_caption_ts(end_s.strip().split()[0])
            i += 1
            buf = []
            while i < len(lines) and lines[i].strip():
                buf.append(lines[i])
                i += 1
            cues.append((start, end, buf))
        else:
            i += 1
    if not cues:
        die(f"字幕文件中未找到任何 cue: {path}")
    return cues


def _clean_cue_lines(buf):
    """清理单个 cue 的文本行：去标签、解码 HTML 实体、折叠空白。"""
    out = []
    for ln in buf:
        ln = _TAG_RE.sub("", ln)
        for ent, ch in _HTML_ENTITIES.items():
            ln = ln.replace(ent, ch)
        ln = _WS_RE.sub(" ", ln).strip()
        if ln:
            out.append(ln)
    return out


def dedup_cues(cues):
    """cue 列表 → 规范化 segment 列表 [{start, end, text}]。

    去重规则：
      1) 自动字幕“滚动重复”：若本 cue 首行与上一保留 cue 末行相同，丢弃首行；
      2) 相同文本的连续块合并：文本与上一 segment 相同则只向后延长 end。
    """
    segs = []
    for start, end, buf in cues:
        lines = _clean_cue_lines(buf)
        if not lines:
            continue
        # 规则 1：滚动重复（YouTube 自动字幕典型形态：重复上一行 + 追加新行）
        if segs and lines and segs[-1]["_last_line"] == lines[0]:
            lines = lines[1:]
        if not lines:
            # 整个 cue 都是重复内容：仅延长上一段结束时间
            segs[-1]["end"] = max(segs[-1]["end"], end)
            continue
        text = " ".join(lines).strip()
        # 规则 2：相同文本连续块合并
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = max(segs[-1]["end"], end)
            segs[-1]["_last_line"] = lines[-1]
            continue
        segs.append({"start": start, "end": end, "text": text, "_last_line": lines[-1]})
    for s in segs:
        s.pop("_last_line", None)
    return segs


# ---------------------------------------------------------------------------
# 音频抽取与 ASR
# ---------------------------------------------------------------------------

def parse_window(start_value=None, end_value=None):
    """解析并校验可选时间窗，返回 ``(start, end, requested)``。

    start 在只给 end 时按 0 处理；未提供任何窗口参数时 start/end 都返回 None，
    让旧调用路径保持完整媒体处理行为。
    """
    requested = start_value is not None or end_value is not None
    if not requested:
        return None, None, False
    start = parse_time(start_value) if start_value is not None else 0.0
    end = parse_time(end_value) if end_value is not None else None
    if end is not None and end <= start:
        die(f"--end ({end:g}) 必须大于 --start ({start:g})")
    return float(start), float(end) if end is not None else None, True


def build_audio_extract_command(ffmpeg, media, wav, start=None, end=None):
    """构造媒体转 16kHz 单声道 WAV 的 ffmpeg 参数列表。"""
    cmd = [str(ffmpeg), "-y"]
    if start is not None:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(media)]
    if end is not None:
        begin = float(start or 0.0)
        cmd += ["-t", f"{float(end) - begin:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", str(wav)]
    return cmd


def extract_audio(video, out_dir, start=None, end=None):
    """ffmpeg 抽取可选窗口为 16kHz 单声道 wav 到 out_dir/audio.wav。"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        die("未找到 ffmpeg：请先运行 `python scripts/setup.py --install`，"
            "或将 ffmpeg.exe 放入 <skill>/tools/ 或加入 PATH")
    wav = out_dir / "audio.wav"
    cmd = build_audio_extract_command(ffmpeg, video, wav, start, end)
    if start is None and end is None:
        window_desc = "完整媒体"
    else:
        window_desc = f"{start or 0.0:.3f}s–{end:.3f}s" if end is not None else f"{start or 0.0:.3f}s–结尾"
    log(f"抽取音频（{window_desc}）: {wav.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not wav.is_file():
        tail = (proc.stderr or "")[-800:].strip()
        die(f"ffmpeg 抽取音频失败（退出码 {proc.returncode}）: {tail}")
    return wav


_ASR_CONFIDENCE_FIELDS = (
    "avg_logprob",
    "no_speech_prob",
    "compression_ratio",
    "temperature",
)


def _finite_float(value):
    """有限数值转 float；缺失、非法、NaN 与无穷返回 None。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def asr_confidence_fields(segment):
    """从 faster-whisper segment 读取可用的置信度诊断字段。"""
    fields = {}
    for name in _ASR_CONFIDENCE_FIELDS:
        value = _finite_float(getattr(segment, name, None))
        if value is not None:
            fields[name] = value
    avg_logprob = fields.get("avg_logprob")
    if avg_logprob is not None:
        # avg_logprob 是自然对数域分数；派生值仅用于排序/阈值提示，原值仍完整保留。
        fields["confidence"] = min(1.0, max(0.0, math.exp(avg_logprob)))
    return fields


def offset_segments(segs, offset):
    """把局部音频时间轴平移回源媒体时间轴，并保留附加元数据。"""
    delta = float(offset or 0.0)
    if delta == 0.0:
        return [dict(s) for s in segs]
    # 源偏移可能产生微负时间戳（如 B 站缓存 audio_minus_video_start=-0.023），
    # 钳到 >=0，使 transcript.json 与 txt/srt 的显示语义保持一致。
    return [
        {
            **s,
            "start": max(0.0, float(s["start"]) + delta),
            "end": max(0.0, float(s["end"]) + delta),
        }
        for s in segs
    ]


def filter_segments_by_window(segs, start=None, end=None):
    """保留与源媒体时间窗有交集的 segment，不改写原始时间戳。"""
    lower = float(start or 0.0)
    upper = float(end) if end is not None else None
    return [
        dict(s) for s in segs
        if float(s["end"]) > lower and (upper is None or float(s["start"]) < upper)
    ]


_DLL_DIR_COOKIES = []  # 必须存活到进程结束，否则目录会被移出 DLL 搜索路径


def _enable_nvidia_dlls():
    """Windows：把 pip 安装的 nvidia-*/bin 加入 DLL 搜索路径。

    必须在 import ctranslate2/faster_whisper 之前调用，否则 GPU 库加载失败。
    os.add_dll_directory 返回的句柄被回收后目录即失效，故存到模块级列表。
    非 Windows 或无安装时静默跳过。
    """
    if os.name != "nt":
        return
    try:
        import site
        bases = set(site.getsitepackages() + [site.getusersitepackages()])
    except Exception:
        return
    for base in bases:
        for dll_dir in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            if os.path.isdir(dll_dir):
                try:
                    _DLL_DIR_COOKIES.append(os.add_dll_directory(dll_dir))
                except OSError:
                    pass
                # 实测：ctranslate2 加载 cublas 时不走 add_dll_directory 的搜索域，
                # 必须同时前置到 PATH（进程内修改即刻对后续 LoadLibrary 生效）
                os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")


def _resolve_device(requested):
    """解析 ASR 运行设备：auto → 有 CUDA 用 cuda 否则 cpu；显式 cuda 不可用则报错。"""
    _enable_nvidia_dlls()
    try:
        import ctranslate2  # 延迟 import
        n_cuda = ctranslate2.get_cuda_device_count()
    except Exception:
        n_cuda = 0
    if requested == "auto":
        device = "cuda" if n_cuda > 0 else "cpu"
        log(f"设备自动选择: {device}（检测到 CUDA 设备 {n_cuda} 个）")
        return device
    if requested == "cuda" and n_cuda == 0:
        die("--device cuda 但没有可用 CUDA GPU；请确认已安装 "
            "`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` 且显卡驱动正常")
    return requested


def run_faster_whisper(audio_path, args):
    """faster-whisper ASR → (segments, 有效语言, device, compute_type)。"""
    device = _resolve_device(args.device)
    try:
        from faster_whisper import WhisperModel  # 延迟 import（须在 _enable_nvidia_dlls 之后）
    except ImportError:
        die("未安装 faster-whisper：请运行 `python scripts/setup.py --install`"
            "（或 `python -m pip install faster-whisper`）")
    # 未显式指定时按设备给默认精度：GPU 用 float16（快且省显存），CPU 用 int8
    compute = args.compute_type or ("float16" if device == "cuda" else "int8")
    lang = None if args.language == "auto" else args.language
    log(f"加载 faster-whisper 模型 {args.model}（device={device}, compute_type={compute}）…")
    model = WhisperModel(args.model, device=device, compute_type=compute)
    log(f"开始转写（vad_filter={not args.no_vad}, language={lang or 'auto'}）…")
    segments_iter, info = model.transcribe(
        str(audio_path),
        vad_filter=not args.no_vad,
        condition_on_previous_text=False,
        language=lang,
    )
    segs = []
    for s in segments_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        segs.append({
            "start": float(s.start),
            "end": float(s.end),
            "text": text,
            **asr_confidence_fields(s),
        })
    detected = getattr(info, "language", None)
    effective_lang = lang or detected or "auto"
    log(f"转写完成：{len(segs)} 个 segment，语言 {effective_lang}")
    return segs, effective_lang, device, compute


def _wav_duration(path):
    """stdlib wave 读取 wav 时长（秒）；失败返回 0.0。"""
    try:
        import wave
        with wave.open(str(path), "rb") as wf:
            frames, rate = wf.getnframes(), wf.getframerate()
            return frames / float(rate) if rate else 0.0
    except Exception:
        return 0.0


def run_sensevoice(audio_path, args):
    """SenseVoiceSmall ASR → (segments, 语言)。注意：此路径允许未实测。

    SenseVoiceSmall 不输出时间戳，这里整段音频产出一个 segment
    （end 取 wav 时长，无法获取时为 0.0），并剥离 <|...|> 控制标记。
    """
    try:
        from funasr import AutoModel  # 延迟 import
    except ImportError:
        die("sensevoice 引擎需要 funasr：请先运行 `pip install funasr torch torchaudio`")
    log("加载 SenseVoiceSmall（iic/SenseVoiceSmall）…")
    model = AutoModel(model="iic/SenseVoiceSmall")
    kwargs = {"input": str(audio_path), "cache": {}, "use_itn": True}
    if args.language != "auto":
        kwargs["language"] = args.language
    res = model.generate(**kwargs)
    text = ""
    if res:
        first = res[0]
        text = first.get("text", "") if isinstance(first, dict) else str(first)
    text = re.sub(r"<\|[^|]*\|>", "", text).strip()
    segs = [{"start": 0.0, "end": _wav_duration(audio_path), "text": text}] if text else []
    log(f"转写完成：{len(segs)} 个 segment")
    return segs, args.language


# ---------------------------------------------------------------------------
# 三件套输出
# ---------------------------------------------------------------------------

def _srt_ts(t):
    """SRT 时码：HH:MM:SS,mmm。"""
    ms = max(0, int(round(float(t) * 1000)))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segment_json_record(segment):
    """构造稳定的 transcript.json 记录，并保留可用 ASR 诊断字段。"""
    record = {
        "start": round(float(segment["start"]), 3),
        "end": round(float(segment["end"]), 3),
        "text": str(segment["text"]),
    }
    for name in (*_ASR_CONFIDENCE_FIELDS, "confidence"):
        value = _finite_float(segment.get(name))
        if value is not None:
            record[name] = round(value, 6)
    return record


def write_outputs(segs, out_dir):
    """写 transcript.srt / transcript.txt / transcript.json，返回三路径。"""
    srt_path = out_dir / "transcript.srt"
    txt_path = out_dir / "transcript.txt"
    json_path = out_dir / "transcript.json"

    # 标准 SRT：序号 + 时码 + 文本 + 空行
    blocks = []
    for i, s in enumerate(segs, 1):
        blocks.append(f"{i}\n{_srt_ts(s['start'])} --> {_srt_ts(s['end'])}\n{s['text']}\n")
    srt_path.write_text("\n".join(blocks), encoding="utf-8")

    # segment 级纯文本：每行 `[MM:SS] 文本`（≥1 小时 HH:MM:SS）
    txt_path.write_text(
        "".join(f"[{fmt_ts(s['start'])}] {s['text']}\n" for s in segs),
        encoding="utf-8",
    )

    json_path.write_text(
        json.dumps(
            [segment_json_record(s) for s in segs],
            ensure_ascii=False, indent=1,
        ) + "\n",
        encoding="utf-8",
    )
    log(f"已写出: {srt_path.name} / {txt_path.name} / {json_path.name}（{len(segs)} segments）")
    return srt_path, txt_path, json_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="视频/音频/字幕 → 转写三件套（transcript.srt/.txt/.json）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", metavar="V", help="本地视频文件：先 ffmpeg 抽 16kHz 单声道 wav 再 ASR")
    src.add_argument("--audio", metavar="A", help="本地音频文件：直接 ASR")
    src.add_argument("--vtt", metavar="F", help="VTT/SRT 字幕文件：解析去重后直接产三件套（无需 ASR）")
    p.add_argument("--out-dir", metavar="D", required=True, help="产物输出目录（不存在则创建）")
    p.add_argument("--engine", default="faster-whisper",
                   choices=["faster-whisper", "sensevoice"], help="ASR 引擎（--vtt 时忽略）")
    p.add_argument("--model", default="small", help="faster-whisper 模型名/路径（--vtt 时忽略）")
    p.add_argument("--language", default="auto",
                   help="语言代码（zh/en/… 或 auto 自动检测）")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                   help="ASR 运行设备：auto 自动检测（有 GPU 用 GPU），cuda 强制 GPU，cpu 强制 CPU")
    p.add_argument("--compute-type", default=None,
                   help="faster-whisper compute_type（缺省：GPU→float16，CPU→int8）")
    p.add_argument("--no-vad", action="store_true",
                   help="关闭 faster-whisper 的 VAD 过滤（默认开启 vad_filter）")
    p.add_argument("--start", default=None,
                   help="可选处理窗口起点：秒 / MM:SS / HH:MM:SS")
    p.add_argument("--end", default=None,
                   help="可选处理窗口终点：秒 / MM:SS / HH:MM:SS")
    p.add_argument(
        "--source-offset",
        type=float,
        default=0.0,
        help="输入音频/字幕的 t=0 对应源视频的秒数（用于分离流时间轴对齐）",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        window_start, window_end, window_requested = parse_window(args.start, args.end)
    except ValueError as exc:
        die(f"时间参数无效: {exc}")
    if not math.isfinite(args.source_offset):
        die("--source-offset 必须是有限数字")
    source_offset = float(args.source_offset)
    timeline_offset = source_offset
    media_seek_start = None
    media_seek_end = None
    if window_requested:
        # source_offset 定义 input-local t → source-video t。用户窗口使用源视频
        # 坐标，因此抽取分离音频时要先做逆变换，再在 ASR 后平移回来。
        requested_start = float(window_start or 0.0)
        media_seek_start = max(0.0, requested_start - source_offset)
        media_seek_end = (
            None if window_end is None else float(window_end) - source_offset
        )
        if media_seek_end is not None and media_seek_end <= media_seek_start:
            die("请求窗口与输入媒体时间轴没有有效交集")
        timeline_offset = media_seek_start + source_offset
    audio_path = None

    if args.vtt:
        # 路径 1：字幕文件 → 三件套
        # 字幕 cue 本身已经位于输入时间轴，只做可选 source_offset 平移；
        # --start/--end 在这里是过滤条件，不代表发生了媒体 seek。
        timeline_offset = source_offset
        media_seek_start = None
        media_seek_end = None
        vtt_path = Path(args.vtt).resolve()
        if not vtt_path.is_file():
            die(f"字幕文件不存在: {vtt_path}")
        log(f"解析字幕文件: {vtt_path}")
        segs = dedup_cues(parse_caption_file(vtt_path))
        if source_offset:
            segs = offset_segments(segs, source_offset)
        if window_requested:
            segs = filter_segments_by_window(segs, window_start, window_end)
        engine, model_name = "captions", None
        device_used, compute_used = None, None
        language = None if args.language == "auto" else args.language
        log(f"字幕解析完成：{len(segs)} 个 segment（已去重合并）")
    else:
        # 路径 2/3：视频抽音频 或 直接音频 → ASR
        if args.video:
            video_path = Path(args.video).resolve()
            if not video_path.is_file():
                die(f"视频文件不存在: {video_path}")
            audio_path = extract_audio(
                video_path,
                out_dir,
                media_seek_start if window_requested else None,
                media_seek_end if window_requested else None,
            )
        else:
            source_audio_path = Path(args.audio).resolve()
            if not source_audio_path.is_file():
                die(f"音频文件不存在: {source_audio_path}")
            if window_requested:
                audio_path = extract_audio(
                    source_audio_path,
                    out_dir,
                    media_seek_start,
                    media_seek_end,
                )
            else:
                audio_path = source_audio_path

        if args.engine == "sensevoice":
            segs, language = run_sensevoice(audio_path, args)
            model_name = "iic/SenseVoiceSmall"
            device_used, compute_used = None, None
        else:
            segs, language, device_used, compute_used = run_faster_whisper(audio_path, args)
            model_name = args.model
        engine = args.engine
        if timeline_offset:
            segs = offset_segments(segs, timeline_offset)
        if window_requested:
            segs = filter_segments_by_window(segs, window_start, window_end)

    srt_path, txt_path, json_path = write_outputs(segs, out_dir)
    result = {
        "ok": True,
        "engine": engine,
        "model": model_name,
        "language": language,
        "device": device_used,
        "compute_type": compute_used,
        "segments": len(segs),
        "srt": str(srt_path),
        "txt": str(txt_path),
        "json": str(json_path),
        "audio": str(audio_path) if audio_path is not None else None,
        "window": {
            "start": window_start if window_requested else None,
            "end": window_end if window_requested else None,
        },
        "timeline": {
            "unit": "seconds",
            "origin": "source",
            "offset": timeline_offset,
            "source_offset": source_offset,
            "media_seek_start": media_seek_start,
            "media_seek_end": media_seek_end,
        },
    }
    print("RESULT_JSON: " + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # Windows 下保证 stdout/stderr 以 UTF-8 输出（容忍非法字节）
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        exit_code = main()
    except Die as e:
        print(f"[transcribe] 错误: {e}", file=sys.stderr)
        print("RESULT_JSON: " + json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        exit_code = 1
    except SystemExit:
        raise  # argparse 自身的退出（--help / 参数错误）原样透传
    except Exception as e:  # 非预期错误：栈打到 stderr，契约行打 stdout
        traceback.print_exc(file=sys.stderr)
        print("RESULT_JSON: " + json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                           ensure_ascii=False))
        exit_code = 1
    sys.exit(exit_code)
