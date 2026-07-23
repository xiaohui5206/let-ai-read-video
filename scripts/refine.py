#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compile a refinement plan and append the requested frames.

The script intentionally contains only standard-library dependencies.  It turns
interval requests into explicit timestamps, applies a global budget, writes a
versioned times file, and delegates image extraction to ``frames.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


RESULT_PREFIX = "RESULT_JSON: "
PLAN_VERSION = 1
DEFAULT_INTERVAL_FPS = 2.0
MAX_INTERVAL_FPS = 4.0
TARGETED_FRAMES_FPS = 4.0
DEFAULT_MAX_EXTRA = 60
DEFAULT_MAX_PASSES = 2
DEFAULT_MAX_TOTAL_EXTRA = 120
DEFAULT_WIDTH = 512
TIME_SCALE = 1_000_000
MAX_EXPANDED_POINTS = 250_000
# frames.py 单轮定向补帧的帧数硬上限（HARD_FRAMES）；超过会整体失败，需提前收敛
MAX_EXTRA_PER_PASS = 100
PASS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FRAMES_SCRIPT = SCRIPT_DIR / "frames.py"


class RefineError(Exception):
    """An expected validation or orchestration failure."""


class FramesProcessError(RefineError):
    """Failure reported while invoking frames.py."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.result = result


class ContractParser(argparse.ArgumentParser):
    """Convert argument errors into the same JSON error contract as runtime errors."""

    def error(self, message: str) -> None:
        raise RefineError(f"argument error: {message}")


def log(message: str) -> None:
    print(f"[refine] {message}", flush=True)


def emit_result(payload: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def _reject_json_constant(value: str) -> None:
    raise RefineError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RefineError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RefineError(f"cannot read {label} {path}: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except RefineError:
        raise
    except json.JSONDecodeError as exc:
        raise RefineError(
            f"invalid JSON in {label} {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def _check_keys(
    value: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise RefineError(f"{location} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise RefineError(f"{location} is missing field(s): {', '.join(missing)}")


def _finite_number(value: Any, location: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RefineError(f"{location} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise RefineError(f"{location} must be finite")
    if number < minimum:
        raise RefineError(f"{location} must be >= {minimum:g}")
    return number


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RefineError(f"{location} must be a positive integer")
    return value


def _nonempty_text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RefineError(f"{location} must be a non-empty string")
    return value.strip()


def _time_key(value: float) -> int:
    return int(round(value * TIME_SCALE))


def _normalise_time(value: float) -> float:
    return round(float(value), 6)


def _dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first item at each microsecond and return timestamp order."""
    seen: set[int] = set()
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _time_key(candidate["t"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(candidate)
    return sorted(kept, key=lambda item: item["t"])


def _evenly_select(
    candidates: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    ordered = sorted(candidates, key=lambda item: item["t"])
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    last = len(ordered) - 1
    indices = [round(index * last / (count - 1)) for index in range(count)]
    return [ordered[index] for index in indices]


def _interval_points(
    *,
    start: float,
    end: float,
    fps: float,
    reason: str,
) -> list[dict[str, Any]]:
    """Place frame requests at the centres of equal subdivisions."""
    span = end - start
    count = max(1, int(math.floor(span * fps + 1e-9)))
    if count > MAX_EXPANDED_POINTS:
        raise RefineError(
            f"interval expands to {count} points; limit is {MAX_EXPANDED_POINTS}"
        )
    step = span / count
    return [
        {
            "t": _normalise_time(start + (index + 0.5) * step),
            "reason": reason,
            "source": "interval",
        }
        for index in range(count)
    ]


def compile_plan(
    plan: Any,
    *,
    max_extra: int = DEFAULT_MAX_EXTRA,
    width: int = DEFAULT_WIDTH,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a plan and return budgeted, deduplicated explicit timestamps."""
    max_extra = _positive_int(max_extra, "--max-extra")
    width = _positive_int(width, "--width")
    if not isinstance(plan, dict):
        raise RefineError("plan root must be a JSON object")
    _check_keys(
        plan,
        allowed={"version", "intervals", "times"},
        required={"version"},
        location="plan",
    )
    version = plan["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise RefineError("plan.version must be an integer")
    if version != PLAN_VERSION:
        raise RefineError(
            f"unsupported plan.version {version!r}; supported version is {PLAN_VERSION}"
        )

    intervals = plan.get("intervals", [])
    times = plan.get("times", [])
    if not isinstance(intervals, list):
        raise RefineError("plan.intervals must be an array")
    if not isinstance(times, list):
        raise RefineError("plan.times must be an array")
    if not intervals and not times:
        raise RefineError("plan must contain at least one interval or explicit time")

    warnings: list[str] = []
    explicit_candidates: list[dict[str, Any]] = []
    for index, item in enumerate(times):
        location = f"plan.times[{index}]"
        if isinstance(item, bool):
            raise RefineError(f"{location} must be a number or object")
        if isinstance(item, (int, float)):
            timestamp = _finite_number(item, location)
            explicit_candidates.append(
                {
                    "t": _normalise_time(timestamp),
                    "reason": "explicit time",
                    "source": "explicit",
                }
            )
            continue
        if not isinstance(item, dict):
            raise RefineError(f"{location} must be a number or object")
        _check_keys(
            item,
            allowed={"t", "reason", "source"},
            required={"t", "reason"},
            location=location,
        )
        timestamp = _finite_number(item["t"], f"{location}.t")
        reason = _nonempty_text(item["reason"], f"{location}.reason")
        source = (
            _nonempty_text(item["source"], f"{location}.source")
            if "source" in item
            else "explicit"
        )
        explicit_candidates.append(
            {"t": _normalise_time(timestamp), "reason": reason, "source": source}
        )

    interval_candidates: list[dict[str, Any]] = []
    normalised_widths = 0
    expanded_total = 0
    for index, item in enumerate(intervals):
        location = f"plan.intervals[{index}]"
        if not isinstance(item, dict):
            raise RefineError(f"{location} must be an object")
        _check_keys(
            item,
            allowed={"start", "end", "reason", "fps", "width"},
            required={"start", "end", "reason"},
            location=location,
        )
        start = _finite_number(item["start"], f"{location}.start")
        end = _finite_number(item["end"], f"{location}.end")
        if end <= start:
            raise RefineError(f"{location}.end must be greater than start")
        reason = _nonempty_text(item["reason"], f"{location}.reason")
        fps = _finite_number(
            item.get("fps", DEFAULT_INTERVAL_FPS),
            f"{location}.fps",
            minimum=0.0,
        )
        if fps <= 0:
            raise RefineError(f"{location}.fps must be > 0")
        if fps > MAX_INTERVAL_FPS:
            raise RefineError(
                f"{location}.fps exceeds the local limit of {MAX_INTERVAL_FPS:g}"
            )
        if "width" in item:
            interval_width = _positive_int(item["width"], f"{location}.width")
            if interval_width != width:
                normalised_widths += 1
        points = _interval_points(start=start, end=end, fps=fps, reason=reason)
        expanded_total += len(points)
        if expanded_total > MAX_EXPANDED_POINTS:
            raise RefineError(
                "all intervals expand to more than "
                f"{MAX_EXPANDED_POINTS} points"
            )
        interval_candidates.extend(points)

    if normalised_widths:
        warnings.append(
            f"{normalised_widths} interval width override(s) were normalised "
            f"to --width {width}"
        )

    explicit = _dedupe(explicit_candidates)
    explicit_keys = {_time_key(item["t"]) for item in explicit}
    intervals_only = _dedupe(
        [
            item
            for item in interval_candidates
            if _time_key(item["t"]) not in explicit_keys
        ]
    )

    if len(explicit) >= max_extra:
        selected = _evenly_select(explicit, max_extra)
    else:
        selected = explicit + _evenly_select(
            intervals_only, max_extra - len(explicit)
        )
    selected = sorted(selected, key=lambda item: item["t"])

    total_unique = len(explicit) + len(intervals_only)
    if total_unique > len(selected):
        warnings.append(
            f"refinement requests were reduced from {total_unique} to "
            f"{len(selected)} by --max-extra"
        )
    if not selected:
        raise RefineError("plan produced no frame timestamps")
    return selected, warnings


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON in the destination directory and atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise RefineError(f"cannot write {path}: {exc}") from exc


def _extract_child_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError as exc:
            raise FramesProcessError(
                f"frames.py returned malformed RESULT_JSON: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise FramesProcessError("frames.py RESULT_JSON must be an object")
        return value
    return None


def invoke_frames(
    *,
    frames_script: Path,
    video: Path,
    out_dir: Path,
    times_json: Path,
    pass_id: str,
    width: int,
    max_fps: float,
) -> dict[str, Any]:
    if not frames_script.is_file():
        raise FramesProcessError(f"frames.py not found: {frames_script}")
    command = [
        sys.executable,
        str(frames_script),
        "--video",
        str(video),
        "--out-dir",
        str(out_dir),
        "--width",
        str(width),
        "--fps",
        f"{max_fps:g}",
        "--times-json",
        str(times_json),
        "--append",
        "--pass-id",
        pass_id,
    ]
    log(f"invoking frames.py for pass {pass_id!r}")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise FramesProcessError(f"cannot start frames.py: {exc}") from exc

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    for line in stdout.splitlines():
        if not line.startswith(RESULT_PREFIX):
            print(f"  [frames] {line}", flush=True)
    for line in stderr.splitlines():
        print(f"  [frames!] {line}", file=sys.stderr, flush=True)

    result = _extract_child_result(stdout)
    if result is None:
        tail = (stderr or stdout).strip()[-500:]
        message = (
            f"frames.py did not return RESULT_JSON (exit {completed.returncode})"
        )
        if tail:
            message += f": {tail}"
        raise FramesProcessError(message, returncode=completed.returncode)
    if completed.returncode != 0 or result.get("ok") is not True:
        message = str(result.get("error") or f"exit {completed.returncode}")
        raise FramesProcessError(
            f"frames.py failed: {message}",
            returncode=completed.returncode,
            result=result,
        )
    return result


def _result_count(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value >= 0:
        return value
    return fallback


def preflight_manifest(out_dir: Path) -> None:
    """Fail before extraction if an existing manifest cannot be safely extended."""
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = load_json(manifest_path, "manifest")
    if not isinstance(manifest, dict):
        raise RefineError("existing manifest root must be an object")
    frames = manifest.get("frames")
    if frames is not None and not isinstance(frames, dict):
        raise RefineError("existing manifest.frames must be an object")
    if isinstance(frames, dict):
        passes = frames.get("passes")
        if passes is not None and not isinstance(passes, list):
            raise RefineError("existing manifest.frames.passes must be an array")


def _frames_index_entries(frames_json: Path) -> list[dict[str, Any]]:
    """读取 frames.json 索引条目；文件缺失或顶层不是数组时按空索引处理。"""
    if not frames_json.is_file():
        return []
    data = load_json(frames_json, "frames index")
    if isinstance(data, dict):
        data = data.get("frames")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def ensure_minimal_manifest(out_dir: Path, *, pass_id: str) -> Path:
    """manifest 缺失时按 watch.py 的 schema 自建最小记账骨架。

    watch.py 流程中 refine 恰好运行在 manifest 首次写入之前；这里把
    frames.json 里非本 pass 的既有帧数记为 base_count（本次补帧前的存量），
    使本轮及后续轮次都能正常走累计额度记账，而不是直接放行全部预算。
    """
    manifest_path = out_dir / "manifest.json"
    entries = _frames_index_entries(out_dir / "frames" / "frames.json")
    base_count = sum(1 for item in entries if item.get("pass_id") != pass_id)
    manifest = {
        "schema_version": 2,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "frames": {
            "count": len(entries),
            "base_count": base_count,
            "passes": [],
        },
    }
    atomic_write_json(manifest_path, manifest)
    return manifest_path


def refinement_allowance(
    out_dir: Path,
    *,
    pass_id: str,
    max_passes: int,
    max_total_extra: int,
) -> int:
    """Return the remaining cumulative frame budget and enforce pass count."""
    max_passes = _positive_int(max_passes, "--max-passes")
    max_total_extra = _positive_int(
        max_total_extra,
        "--max-total-extra",
    )
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        # manifest 缺失（refine 跑在 watch.py 首次写入 manifest 之前）：
        # 自建最小 manifest 后继续走正常记账，而非直接放行全部累计额度。
        ensure_minimal_manifest(out_dir, pass_id=pass_id)
    manifest = load_json(manifest_path, "manifest")
    if not isinstance(manifest, dict):
        raise RefineError("existing manifest root must be an object")
    frames = manifest.get("frames") or {}
    if not isinstance(frames, dict):
        raise RefineError("existing manifest.frames must be an object")
    passes = frames.get("passes") or []
    if not isinstance(passes, list):
        raise RefineError("existing manifest.frames.passes must be an array")

    refinement_passes = [
        item
        for item in passes
        if isinstance(item, dict)
        and (
            item.get("plan") is not None
            or str(item.get("pass_id") or "") not in {"", "base"}
        )
    ]
    existing_ids = {
        str(item.get("pass_id"))
        for item in refinement_passes
        if item.get("pass_id") is not None
    }
    if pass_id not in existing_ids and len(existing_ids) >= max_passes:
        raise RefineError(
            f"refinement pass limit reached ({max_passes}); "
            "reassess existing evidence instead of adding another pass"
        )

    base_count = frames.get("base_count")
    total_count = frames.get("count")
    if (
        isinstance(base_count, int)
        and not isinstance(base_count, bool)
        and base_count >= 0
        and isinstance(total_count, int)
        and not isinstance(total_count, bool)
        and total_count >= base_count
    ):
        used = total_count - base_count
    else:
        used = 0
        for item in refinement_passes:
            value = item.get("added_count", item.get("count", 0))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                used += value

    remaining = max_total_extra - used
    if remaining <= 0:
        raise RefineError(
            f"cumulative refinement budget exhausted "
            f"({used}/{max_total_extra} frames)"
        )
    return remaining


def infer_existing_width(out_dir: Path) -> int:
    """Reuse the base run's resolution so targeted passes do not degrade it."""
    manifest_path = out_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path, "manifest")
        if isinstance(manifest, dict):
            params = manifest.get("params")
            if isinstance(params, dict):
                value = params.get("width")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value

    frames_path = out_dir / "frames" / "frames.json"
    if frames_path.is_file():
        frames = load_json(frames_path, "frames index")
        if isinstance(frames, dict):
            frames = frames.get("frames")
        if isinstance(frames, list):
            widths = [
                item.get("width")
                for item in frames
                if isinstance(item, dict)
                and isinstance(item.get("width"), int)
                and not isinstance(item.get("width"), bool)
                and item.get("width") > 0
            ]
            if widths:
                return max(widths)
    return DEFAULT_WIDTH


def update_manifest(
    *,
    out_dir: Path,
    pass_id: str,
    plan_path: Path,
    requested_count: int,
    frames_result: dict[str, Any],
) -> tuple[bool, Path | None, dict[str, Any]]:
    """Atomically upsert one pass record when a legacy or current manifest exists."""
    manifest_path = out_dir / "manifest.json"
    legacy_added = _result_count(
        frames_result.get("added_count"),
        requested_count,
    )
    added_count = _result_count(frames_result.get("added"), legacy_added)
    legacy_total = _result_count(
        frames_result.get("total_count"),
        added_count,
    )
    total_count = _result_count(frames_result.get("count"), legacy_total)
    frames_json_value = frames_result.get("frames_json")
    frames_json_path = (
        Path(str(frames_json_value)).expanduser()
        if frames_json_value
        else out_dir / "frames" / "frames.json"
    )
    if not frames_json_path.is_absolute():
        frames_json_path = out_dir / frames_json_path
    frames_json = str(frames_json_path.resolve())
    record = {
        "pass_id": pass_id,
        "plan": str(plan_path.resolve()),
        "added_count": added_count,
        "total_count": total_count,
        "frames_json": frames_json,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if not manifest_path.exists():
        return False, None, record

    manifest = load_json(manifest_path, "manifest")
    if not isinstance(manifest, dict):
        raise RefineError("existing manifest root must be an object")
    frames = manifest.get("frames")
    if frames is None:
        frames = {}
        manifest["frames"] = frames
    if not isinstance(frames, dict):
        raise RefineError("existing manifest.frames must be an object")
    passes = frames.get("passes")
    if passes is None:
        passes = []
        frames["passes"] = passes
    if not isinstance(passes, list):
        raise RefineError("existing manifest.frames.passes must be an array")

    replacement_index: int | None = None
    for index, existing in enumerate(passes):
        if isinstance(existing, dict) and existing.get("pass_id") == pass_id:
            replacement_index = index
            break
    if replacement_index is None:
        passes.append(record)
    else:
        passes[replacement_index] = record

    # Keep the manifest's summary in sync for callers that run refine.py after
    # watch.py has already completed.
    frames["count"] = total_count
    frames["json"] = frames_json
    frames["dir"] = str(Path(frames_json).parent)
    review = manifest.get("review")
    if isinstance(review, dict):
        review["status"] = "stale_after_refinement"
        review["last_refine_pass"] = pass_id

    atomic_write_json(manifest_path, manifest)
    return True, manifest_path, record


def build_parser() -> ContractParser:
    parser = ContractParser(
        description="Append frames selected by a versioned JSON refinement plan."
    )
    parser.add_argument("--video", required=True, help="input video file")
    parser.add_argument("--out-dir", required=True, help="existing or new run directory")
    parser.add_argument("--plan", required=True, help="refinement plan JSON file")
    parser.add_argument("--pass-id", required=True, help="safe identifier for this pass")
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help=(
            "output width; defaults to the existing run width, "
            f"or {DEFAULT_WIDTH} when it cannot be inferred"
        ),
    )
    parser.add_argument(
        "--max-extra",
        type=int,
        default=DEFAULT_MAX_EXTRA,
        help=f"maximum appended frame requests (default: {DEFAULT_MAX_EXTRA})",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=DEFAULT_MAX_PASSES,
        help=f"maximum distinct refinement passes (default: {DEFAULT_MAX_PASSES})",
    )
    parser.add_argument(
        "--max-total-extra",
        type=int,
        default=DEFAULT_MAX_TOTAL_EXTRA,
        help=(
            "maximum cumulative appended frames recorded in the run "
            f"(default: {DEFAULT_MAX_TOTAL_EXTRA})"
        ),
    )
    return parser


def run(
    argv: list[str] | None = None,
    *,
    frames_script: Path = DEFAULT_FRAMES_SCRIPT,
) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    pass_id = _nonempty_text(args.pass_id, "--pass-id")
    if not PASS_ID_RE.fullmatch(pass_id):
        raise RefineError(
            "--pass-id must be 1-64 ASCII letters, digits, dots, underscores, "
            "or hyphens, and must start with a letter or digit"
        )
    max_extra = _positive_int(args.max_extra, "--max-extra")
    # frames.py 对单轮定向补帧有 100 帧硬上限，超出会让整轮失败；这里提前收敛
    max_extra_clamped = max_extra > MAX_EXTRA_PER_PASS
    if max_extra_clamped:
        max_extra = MAX_EXTRA_PER_PASS
    max_passes = _positive_int(args.max_passes, "--max-passes")
    max_total_extra = _positive_int(
        args.max_total_extra,
        "--max-total-extra",
    )

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise RefineError(f"video file does not exist: {video}")
    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.is_file():
        raise RefineError(f"plan file does not exist: {plan_path}")
    out_dir = Path(args.out_dir).expanduser().resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RefineError(f"cannot create output directory {out_dir}: {exc}") from exc

    preflight_manifest(out_dir)
    remaining_total = refinement_allowance(
        out_dir,
        pass_id=pass_id,
        max_passes=max_passes,
        max_total_extra=max_total_extra,
    )
    effective_max_extra = min(max_extra, remaining_total)
    width = (
        _positive_int(args.width, "--width")
        if args.width is not None
        else infer_existing_width(out_dir)
    )
    if args.width is None:
        log(f"inferred output width {width}px from the existing run")
    plan = load_json(plan_path, "plan")
    times, warnings = compile_plan(
        plan,
        max_extra=effective_max_extra,
        width=width,
    )
    if max_extra_clamped:
        warnings.append(
            f"--max-extra exceeds the per-pass limit of {MAX_EXTRA_PER_PASS} "
            "frame(s) enforced by frames.py; clamped accordingly"
        )
    if effective_max_extra < max_extra:
        warnings.append(
            "this pass was capped to the remaining cumulative budget "
            f"of {effective_max_extra} frame(s)"
        )
    times_path = out_dir / f"refine_times_{pass_id}.json"
    times_document = {
        "version": PLAN_VERSION,
        "pass_id": pass_id,
        "width": width,
        "max_fps": TARGETED_FRAMES_FPS,
        "times": times,
    }
    atomic_write_json(times_path, times_document)
    log(f"compiled {len(times)} timestamp(s) into {times_path}")

    frames_result = invoke_frames(
        frames_script=Path(frames_script).resolve(),
        video=video,
        out_dir=out_dir,
        times_json=times_path,
        pass_id=pass_id,
        width=width,
        max_fps=TARGETED_FRAMES_FPS,
    )
    manifest_updated, manifest_path, pass_record = update_manifest(
        out_dir=out_dir,
        pass_id=pass_id,
        plan_path=plan_path,
        requested_count=len(times),
        frames_result=frames_result,
    )
    return {
        "ok": True,
        "pass_id": pass_id,
        "plan": str(plan_path),
        "times_json": str(times_path),
        "requested_count": len(times),
        "added_count": pass_record["added_count"],
        "total_count": pass_record["total_count"],
        "frames_json": pass_record["frames_json"],
        "manifest_updated": manifest_updated,
        "manifest": str(manifest_path) if manifest_path else None,
        "warnings": warnings,
        "limits": {
            "max_passes": max_passes,
            "max_total_extra": max_total_extra,
            "remaining_before_pass": remaining_total,
        },
        "frames_result": frames_result,
    }


def main(
    argv: list[str] | None = None,
    *,
    frames_script: Path = DEFAULT_FRAMES_SCRIPT,
) -> int:
    try:
        result = run(argv, frames_script=frames_script)
    except FramesProcessError as exc:
        payload: dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.returncode is not None:
            payload["returncode"] = exc.returncode
        if exc.result is not None:
            payload["frames_result"] = exc.result
        print(f"[refine][ERROR] {exc}", file=sys.stderr, flush=True)
        emit_result(payload)
        return 1
    except RefineError as exc:
        print(f"[refine][ERROR] {exc}", file=sys.stderr, flush=True)
        emit_result({"ok": False, "error": str(exc)})
        return 1
    except Exception as exc:  # Defensive contract boundary.
        message = f"{type(exc).__name__}: {exc}"
        print(f"[refine][ERROR] {message}", file=sys.stderr, flush=True)
        emit_result({"ok": False, "error": message})
        return 1
    emit_result(result)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    raise SystemExit(main())
