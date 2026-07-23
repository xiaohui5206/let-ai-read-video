#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build timestamp-aligned review packets and refinement plans.

This module deliberately does not depend on a particular AI provider.  ``prepare``
groups transcript segments and frames into compact time windows.  A human or any
multimodal model can fill each window's ``assessment`` object.  ``plan`` then turns
the structured assessments into the neutral JSON format consumed by refine.py.

Every command follows the repository contract: logs first, one RESULT_JSON line
last, and a non-zero exit code on failure.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path


RESULT_PREFIX = "RESULT_JSON: "
RELATIONS = ("supports", "complements", "contradicts", "unrelated", "insufficient")
VISUAL_CUE_RE = re.compile(
    r"(看这里|如图|图中|屏幕|画面|代码|公式|表格|曲线|左边|右边|上方|下方|"
    r"点击|拖动|演示|look at|as shown|on screen|this (?:chart|figure|code)|"
    r"left side|right side|click|drag)",
    flags=re.IGNORECASE,
)


def log(message: str) -> None:
    print(f"[review] {message}", flush=True)


def emit(payload: dict) -> None:
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def fail(message: str) -> None:
    print(f"[review][ERROR] {message}", file=sys.stderr, flush=True)
    emit({"ok": False, "error": str(message)})
    raise SystemExit(1)


class ContractParser(argparse.ArgumentParser):
    """参数错误也遵守 RESULT_JSON 契约，与运行时错误一致。"""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        fail(f"参数错误: {message}")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        fail(f"文件不存在: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"读取 JSON 失败 {path}: {exc}")


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def as_number(value, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail(f"{field} 必须是数字: {value!r}")
    if not math.isfinite(number):
        fail(f"{field} 必须是有限数字: {value!r}")
    return number


def normalize_transcript(raw) -> list[dict]:
    if not isinstance(raw, list):
        fail("transcript.json 顶层必须是数组")
    items = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            fail(f"transcript[{index}] 必须是对象")
        start = as_number(item.get("start"), f"transcript[{index}].start")
        end = as_number(item.get("end"), f"transcript[{index}].end")
        text = str(item.get("text") or "").strip()
        # 源偏移可能产生微负 start（如 B 站缓存偏移 -0.023s），
        # 容差内归一化为 0.0；超出容差或 end<start 仍视为非法。
        if -1.0 <= start < 0:
            start = 0.0
        if start < 0 or end < start:
            fail(f"transcript[{index}] 时间范围非法")
        if text:
            items.append({"start": start, "end": end, "text": text})
    return sorted(items, key=lambda item: (item["start"], item["end"]))


def normalize_frames(raw, frames_dir: Path) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("frames")
    if not isinstance(raw, list):
        fail("frames.json 顶层必须是数组，或包含 frames 数组的对象")
    items = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            fail(f"frames[{index}] 必须是对象")
        actual = item.get("actual_t", item.get("t"))
        t = as_number(actual, f"frames[{index}].t")
        if t < 0:
            fail(f"frames[{index}].t 不能为负")
        file_name = str(item.get("file") or "")
        items.append({
            "file": file_name,
            "path": str((frames_dir / file_name).resolve()) if file_name else None,
            "t": t,
            "source": item.get("source"),
            "pass_id": item.get("pass_id", "base"),
        })
    return sorted(items, key=lambda item: item["t"])


def maximum_gap(start: float, end: float, frame_times: list[float]) -> float:
    points = [start] + [t for t in frame_times if start <= t <= end] + [end]
    return max((b - a for a, b in zip(points, points[1:])), default=end - start)


def prepare_units(transcript: list[dict], frames: list[dict], window: float,
                  duration: float | None, attention_gap: float,
                  range_start: float = 0.0,
                  range_end: float | None = None) -> list[dict]:
    inferred_end = max(
        [item["end"] for item in transcript] + [item["t"] for item in frames] + [0.0]
    )
    total = inferred_end if duration is None else max(duration, inferred_end)
    review_start = max(0.0, float(range_start))
    review_end = total if range_end is None else min(total, float(range_end))
    if review_end <= review_start:
        return []
    count = max(1, int(math.ceil((review_end - review_start) / window)))
    units = []
    for index in range(count):
        start = review_start + index * window
        end = min(review_end, start + window)
        segments = [
            item for item in transcript
            if item["end"] > start and item["start"] < end
        ]
        # Include a small boundary margin so an assessor can see the nearest evidence.
        nearby = [
            item for item in frames
            if start - 1.0 <= item["t"] <= end + 1.0
        ]
        in_window_times = [item["t"] for item in nearby if start <= item["t"] <= end]
        text = " ".join(item["text"] for item in segments).strip()
        cue_matches = sorted(set(match.group(0) for match in VISUAL_CUE_RE.finditer(text)))
        gap = maximum_gap(start, end, in_window_times)
        needs_attention = (
            not in_window_times
            or gap > attention_gap
            or (bool(cue_matches) and not any(
                min(abs(frame_t - segment["start"]) for segment in segments) <= 2.0
                for frame_t in in_window_times
            ))
        )
        units.append({
            "id": f"w{index:04d}",
            "start": round(start, 3),
            "end": round(end, 3),
            "transcript": text,
            "segments": segments,
            "frames": nearby,
            "signals": {
                "frame_count": len(in_window_times),
                "max_coverage_gap": round(gap, 3),
                "visual_cues": cue_matches,
                "needs_attention": needs_attention,
            },
            "assessment": {
                "relation": None,
                "confidence": None,
                "notes": "",
                "refine": None,
            },
        })
    return units


def _assessment_requests_refine(unit: dict, min_confidence: float,
                                include_heuristics: bool) -> tuple[bool, str]:
    assessment = unit.get("assessment") or {}
    relation = assessment.get("relation")
    confidence_value = assessment.get("confidence")
    confidence = 0.0 if confidence_value is None else as_number(
        confidence_value, f"{unit.get('id')}.assessment.confidence"
    )
    explicit = assessment.get("refine")
    if explicit is True:
        return True, str(assessment.get("notes") or relation or "requested")
    if relation == "insufficient" and confidence >= min_confidence:
        return True, str(assessment.get("notes") or "insufficient_visual_evidence")
    if relation == "contradicts" and confidence >= min_confidence:
        return True, str(assessment.get("notes") or "diagnose_possible_contradiction")
    if include_heuristics and (unit.get("signals") or {}).get("needs_attention"):
        return True, "coverage_or_visual_cue"
    return False, ""


def merge_intervals(intervals: list[dict]) -> list[dict]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: (item["start"], item["end"]))
    merged = [dict(ordered[0])]
    for item in ordered[1:]:
        previous = merged[-1]
        if item["start"] <= previous["end"] + 1e-9:
            previous["end"] = max(previous["end"], item["end"])
            reasons = [part for part in (previous.get("reason"), item.get("reason")) if part]
            previous["reason"] = " | ".join(dict.fromkeys(reasons))
            previous["fps"] = max(previous.get("fps", 2.0), item.get("fps", 2.0))
        else:
            merged.append(dict(item))
    return merged


def build_plan(review: dict, min_confidence: float, padding: float, fps: float,
               include_heuristics: bool) -> dict:
    units = review.get("units")
    if not isinstance(units, list):
        fail("review JSON 缺少 units 数组")
    duration = review.get("duration")
    duration_value = as_number(duration, "duration") if duration is not None else None
    intervals = []
    for unit in units:
        if not isinstance(unit, dict):
            fail("review.units 中存在非对象条目")
        relation = (unit.get("assessment") or {}).get("relation")
        if relation is not None and relation not in RELATIONS:
            fail(f"{unit.get('id')} relation 非法: {relation!r}")
        requested, reason = _assessment_requests_refine(
            unit, min_confidence, include_heuristics
        )
        if not requested:
            continue
        start = max(0.0, as_number(unit.get("start"), "unit.start") - padding)
        end = as_number(unit.get("end"), "unit.end") + padding
        if duration_value is not None:
            end = min(duration_value, end)
        if end > start:
            intervals.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "reason": reason,
                "fps": fps,
            })
    return {
        "version": 1,
        "intervals": merge_intervals(intervals),
        "times": [],
    }


def unit_evidence_signature(unit: dict) -> tuple:
    """Stable comparison key used to preserve assessments during refresh."""
    frames = unit.get("frames") or []
    frame_key = tuple(sorted(
        (
            str(item.get("file") or ""),
            round(float(item.get("t", 0.0)), 6),
            str(item.get("pass_id") or ""),
        )
        for item in frames
        if isinstance(item, dict)
    ))
    segment_key = tuple(
        (
            round(float(item.get("start", 0.0)), 6),
            round(float(item.get("end", 0.0)), 6),
            str(item.get("text") or ""),
        )
        for item in (unit.get("segments") or [])
        if isinstance(item, dict)
    )
    return frame_key, segment_key


def command_prepare(args) -> None:
    transcript_path = (
        Path(args.transcript_json).resolve()
        if args.transcript_json
        else None
    )
    frames_path = Path(args.frames_json).resolve()
    transcript = (
        normalize_transcript(load_json(transcript_path))
        if transcript_path is not None
        else []
    )
    frames = normalize_frames(load_json(frames_path), frames_path.parent)
    duration = None if args.duration is None else as_number(args.duration, "--duration")
    range_start = as_number(args.start, "--start")
    range_end = None if args.end is None else as_number(args.end, "--end")
    units = prepare_units(
        transcript,
        frames,
        args.window,
        duration,
        args.attention_gap,
        range_start,
        range_end,
    )
    effective_duration = max(
        [item["end"] for item in transcript] + [item["t"] for item in frames] + [duration or 0.0]
    )
    output = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "transcript_json": str(transcript_path) if transcript_path else None,
            "frames_json": str(frames_path),
        },
        "duration": round(effective_duration, 3),
        "range": {
            "start": round(range_start, 3),
            "end": round(
                min(effective_duration, range_end)
                if range_end is not None
                else effective_duration,
                3,
            ),
        },
        "window_seconds": args.window,
        "attention_gap_seconds": args.attention_gap,
        "relation_labels": list(RELATIONS),
        "instructions": (
            "Inspect the listed frames and transcript for each unit. "
            "Set assessment.relation, confidence (0..1), notes, and refine. "
            "Treat complementary audio/visual evidence as complements, not a conflict."
        ),
        "units": units,
    }
    out_path = Path(args.out).resolve()
    write_json_atomic(out_path, output)
    log(f"已生成 {len(units)} 个审查窗口: {out_path}")
    emit({"ok": True, "review_json": str(out_path), "units": len(units)})


def command_refresh(args) -> None:
    review_path = Path(args.review).resolve()
    previous = load_json(review_path)
    if not isinstance(previous, dict):
        fail("review JSON 顶层必须是对象")
    source = previous.get("source")
    if not isinstance(source, dict):
        fail("review.source 必须是对象")
    transcript_value = source.get("transcript_json")
    transcript_path = (
        Path(str(transcript_value)).resolve()
        if transcript_value
        else None
    )
    frames_value = args.frames_json or source.get("frames_json")
    if not frames_value:
        fail("review.source.frames_json 缺失，且未提供 --frames-json")
    frames_path = Path(str(frames_value)).resolve()

    transcript = (
        normalize_transcript(load_json(transcript_path))
        if transcript_path is not None
        else []
    )
    frames = normalize_frames(load_json(frames_path), frames_path.parent)
    duration_value = previous.get("duration")
    duration = (
        None if duration_value is None else as_number(duration_value, "duration")
    )
    range_value = previous.get("range") or {}
    if not isinstance(range_value, dict):
        fail("review.range 必须是对象")
    range_start = as_number(range_value.get("start", 0.0), "range.start")
    range_end_raw = range_value.get("end")
    range_end = (
        None if range_end_raw is None else as_number(range_end_raw, "range.end")
    )
    window = as_number(previous.get("window_seconds", 10.0), "window_seconds")
    attention_gap = as_number(
        previous.get("attention_gap_seconds", 5.0),
        "attention_gap_seconds",
    )
    units = prepare_units(
        transcript,
        frames,
        window,
        duration,
        attention_gap,
        range_start,
        range_end,
    )

    old_units = {
        item.get("id"): item
        for item in (previous.get("units") or [])
        if isinstance(item, dict) and item.get("id")
    }
    preserved = 0
    reset = 0
    for unit in units:
        old = old_units.get(unit["id"])
        if old is not None and unit_evidence_signature(old) == unit_evidence_signature(unit):
            assessment = old.get("assessment")
            if isinstance(assessment, dict):
                unit["assessment"] = dict(assessment)
                preserved += 1
                continue
        reset += 1

    effective_duration = max(
        [item["end"] for item in transcript]
        + [item["t"] for item in frames]
        + [duration or 0.0]
    )
    output = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "refreshed_from": str(review_path),
        "source": {
            "transcript_json": str(transcript_path) if transcript_path else None,
            "frames_json": str(frames_path),
        },
        "duration": round(effective_duration, 3),
        "range": {
            "start": round(range_start, 3),
            "end": round(
                min(effective_duration, range_end)
                if range_end is not None
                else effective_duration,
                3,
            ),
        },
        "window_seconds": window,
        "attention_gap_seconds": attention_gap,
        "relation_labels": list(RELATIONS),
        "instructions": previous.get("instructions") or (
            "Inspect the listed frames and transcript for each unit. "
            "Set assessment.relation, confidence (0..1), notes, and refine."
        ),
        "units": units,
    }
    out_path = Path(args.out).resolve() if args.out else review_path
    manifest_path = out_path.parent / "manifest.json"
    # 先读取并校验 manifest：损坏即 fail，此时 review.json 尚未被改动，
    # 保证“报错即未生效”；全部校验通过后再原子写 review.json，最后更新 manifest。
    manifest = None
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            fail("manifest.json 顶层必须是对象")
    write_json_atomic(out_path, output)
    manifest_updated = False
    if manifest is not None:
        manifest_review = manifest.get("review")
        if not isinstance(manifest_review, dict):
            manifest_review = {}
            manifest["review"] = manifest_review
        manifest_review.update({
            "status": "pending_reassessment",
            "json": str(out_path),
            "units": len(units),
        })
        write_json_atomic(manifest_path, manifest)
        manifest_updated = True
    log(
        f"已刷新审查包: {out_path}；保留 {preserved} 个判断，"
        f"重置 {reset} 个证据变化窗口"
    )
    emit({
        "ok": True,
        "review_json": str(out_path),
        "units": len(units),
        "preserved_assessments": preserved,
        "reset_assessments": reset,
        "manifest_updated": manifest_updated,
    })


def command_plan(args) -> None:
    review_path = Path(args.review).resolve()
    review = load_json(review_path)
    plan = build_plan(
        review,
        min_confidence=args.min_confidence,
        padding=args.padding,
        fps=args.fps,
        include_heuristics=args.include_heuristics,
    )
    out_path = Path(args.out).resolve()
    write_json_atomic(out_path, plan)
    log(f"已生成 {len(plan['intervals'])} 个补帧区间: {out_path}")
    emit({
        "ok": True,
        "refine_plan": str(out_path),
        "intervals": len(plan["intervals"]),
    })


def build_parser() -> argparse.ArgumentParser:
    parser = ContractParser(
        description="把转写与帧对齐成通用审查包，并从结构化判断生成补帧计划"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="生成待人工或多模态模型填写的审查包")
    prepare.add_argument(
        "--transcript-json",
        default=None,
        help="可选 transcript.json；无语音视频可仅提供帧索引",
    )
    prepare.add_argument("--frames-json", required=True)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--duration", type=float, default=None)
    prepare.add_argument("--start", type=float, default=0.0)
    prepare.add_argument("--end", type=float, default=None)
    prepare.add_argument("--window", type=float, default=10.0)
    prepare.add_argument("--attention-gap", type=float, default=5.0)
    prepare.set_defaults(handler=command_prepare)

    refresh = sub.add_parser(
        "refresh",
        help="补帧后刷新审查包；仅重置证据发生变化的窗口",
    )
    refresh.add_argument("--review", required=True)
    refresh.add_argument("--frames-json", default=None)
    refresh.add_argument("--out", default=None)
    refresh.set_defaults(handler=command_refresh)

    plan = sub.add_parser("plan", help="从已填写 assessment 的审查包生成 refine plan")
    plan.add_argument("--review", required=True)
    plan.add_argument("--out", required=True)
    plan.add_argument("--min-confidence", type=float, default=0.6)
    plan.add_argument("--padding", type=float, default=1.0)
    plan.add_argument(
        "--fps",
        type=float,
        default=4.0,
        help="补证据窗口的采样率；默认 4fps，仍受 refine 总预算限制",
    )
    plan.add_argument("--include-heuristics", action="store_true")
    plan.set_defaults(handler=command_plan)
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if getattr(args, "window", 1.0) <= 0:
        fail("--window 必须 > 0")
    if getattr(args, "attention_gap", 1.0) <= 0:
        fail("--attention-gap 必须 > 0")
    if getattr(args, "start", 0.0) < 0:
        fail("--start 不能为负")
    if getattr(args, "end", None) is not None and args.end <= args.start:
        fail("--end 必须大于 --start")
    if not 0 <= getattr(args, "min_confidence", 0.6) <= 1:
        fail("--min-confidence 必须位于 0..1")
    if getattr(args, "padding", 0.0) < 0:
        fail("--padding 不能为负")
    if not 0 < getattr(args, "fps", 2.0) <= 4:
        fail("--fps 必须位于 0..4")
    args.handler(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"未预期错误: {type(exc).__name__}: {exc}")
