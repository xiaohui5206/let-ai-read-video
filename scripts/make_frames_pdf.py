#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_frames_pdf.py — 把 run 目录的关键帧导出为带时间标识的 PDF（零第三方依赖）。

读取 <run-dir>/frames/frames.json（条目含 file/t/actual_t；dropped:true 跳过），
JPEG 字节流以 DCTDecode 图像对象原样嵌入（不重压缩、无画质损失），caption 用
PDF 内置 Helvetica（Base-14，无需嵌字体），仅 ASCII：`#3  t=12:34`
（≥1 小时 `t=1:02:34`）。

排版：A4 竖版 595.28×841.89pt，页边距 36pt，默认 2 列 × 3 行/页，
支持 --per-page 1|2|6，首页标题条与每页页码，列间距 18pt；
图等比缩放水平居中，图下方 9pt caption。content stream 不压缩，便于测试断言。

用法（任意 cwd 下）：
    python scripts/make_frames_pdf.py --run-dir runs/<任务目录> [--out 输出.pdf]

stdout 最后一行：RESULT_JSON: {"ok": true, "pdf": ..., "pages": N,
"frames_included": N, "frames_skipped": N}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 优先复用同目录 common.py；导入失败时启用最小兜底实现，保证脚本被
# 单独分发时仍可运行。
# ---------------------------------------------------------------------------
try:
    import common  # type: ignore

    setup_stdio = common.setup_stdio
    print_result = common.print_result
except Exception:  # pragma: no cover - common 缺失时的最小兜底实现

    def setup_stdio():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def print_result(d):
        print("RESULT_JSON: " + json.dumps(d, ensure_ascii=False, default=str), flush=True)


# ---------------------------------------------------------------------------
# 输出与错误处理（契约：日志在前，stdout 最后一行单行 RESULT_JSON）
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[frames-pdf] {msg}", flush=True)


def fail(msg, code=1):
    print(f"[frames-pdf][ERROR] {msg}", file=sys.stderr, flush=True)
    print_result({"ok": False, "error": str(msg)})
    sys.exit(code)


class CliParser(argparse.ArgumentParser):
    """参数错误也遵守 RESULT_JSON 契约，便于 webui 编排消费。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        fail(f"参数错误: {message}")


# ---------------------------------------------------------------------------
# JPEG 尺寸解析（不重压缩嵌入，只需宽高）
# ---------------------------------------------------------------------------
def jpeg_size(data):
    """从 JPEG 头解析 (width, height)。

    SOI(FFD8) 后遍历 marker；SOF0-SOF2(FFC0-FFC2) 段内含
    precision(1B) + height(2B BE) + width(2B BE)。解析失败抛 ValueError。
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("不是 JPEG（缺 SOI 头）")
    pos = 2
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        while marker == 0xFF:  # 填充字节
            pos += 1
            marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:  # SOI/EOI/RSTn 无长度段
            continue
        if pos + 2 > len(data):
            break
        seg_len = (data[pos] << 8) | data[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF0-SOF2
            if pos + 7 > len(data):
                break
            height = (data[pos + 3] << 8) | data[pos + 4]
            width = (data[pos + 5] << 8) | data[pos + 6]
            if width <= 0 or height <= 0:
                raise ValueError("SOF 段宽高非法")
            return width, height
        if seg_len < 2:
            break
        pos += seg_len
    raise ValueError("未找到 SOF0-SOF2 段")


def fmt_caption_t(seconds):
    """caption 时间：秒截断取整，MM:SS；≥1 小时 H:MM:SS（全 ASCII）。"""
    total = max(0, int(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 帧清单加载
# ---------------------------------------------------------------------------
def load_frame_items(frames_json):
    """读取 frames.json → [{file, t}]：按 actual_t（缺省回退 t）排序。

    dropped:true 的条目跳过（为去重功能预留）；缺 file/时间字段的条目跳过。
    返回 (items, skipped)。
    """
    try:
        data = json.loads(frames_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 frames.json: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("frames.json 顶层必须是数组")
    items = []
    skipped = 0
    for raw in data:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        if raw.get("dropped"):
            skipped += 1
            continue
        fname = raw.get("file")
        t = raw.get("actual_t", raw.get("t"))
        if not fname or isinstance(t, bool) or not isinstance(t, (int, float)):
            log(f"跳过非法条目: {raw!r}")
            skipped += 1
            continue
        items.append({"file": fname, "t": float(t)})
    items.sort(key=lambda item: item["t"])
    return items, skipped


def read_frames(frames_json, items):
    """逐帧读 JPEG 字节并解析尺寸；单帧失败跳过记日志。返回 (frames, skipped)。"""
    frames = []
    skipped = 0
    base = frames_json.parent
    for item in items:
        # 文件名与 frames.json 同目录解析；拒绝越出该目录的条目
        path = (base / item["file"]).resolve()
        if base.resolve() not in path.parents:
            log(f"跳过越界条目: {item['file']}")
            skipped += 1
            continue
        try:
            data = path.read_bytes()
            width, height = jpeg_size(data)
        except OSError as exc:
            log(f"跳过 {item['file']}: 读取失败 {exc}")
            skipped += 1
            continue
        except ValueError as exc:
            log(f"跳过 {item['file']}: {exc}")
            skipped += 1
            continue
        frames.append({"data": data, "w": width, "h": height, "t": item["t"]})
    return frames, skipped


# ---------------------------------------------------------------------------
# PDF 生成（手写：Catalog → Pages → Page + content stream + Image XObject）
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 595.28, 841.89   # A4 竖版（pt）
MARGIN = 36.0
COL_GAP = 18.0
CAPTION_H = 14.0
FONT_SIZE = 9
TITLE_H = 28.0                    # 首页标题条高度（pt）
# U12 版式：每页 1 / 2 / 6 帧（课程 PPT 场景 1-2 帧/页更大更清晰）
_LAYOUTS = {1: (1, 1), 2: (1, 2), 6: (2, 3)}


def _ascii_title(raw, date_str):
    """PDF 标题只用 ASCII（内置 Helvetica 无 CJK 字形）：保留英数与基本符号，
    提取 BV 号；纯中文标题 → 'Video Frames' + 日期。"""
    raw_s = str(raw or "")
    text = re.sub(r"[^\x20-\x7e]", " ", raw_s)
    text = re.sub(r"\s+", " ", text).strip()
    bv = re.search(r"BV[0-9A-Za-z]{10}", raw_s)
    parts = []
    if text:
        parts.append(text[:80])
    if bv and bv.group(0) not in text:
        parts.append(bv.group(0))
    if not parts:
        parts.append("Video Frames")
    return " ".join(parts) + "  (" + date_str + ")"


def _num(v):
    """PDF 数值字面量：两位小数、去尾零。"""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_pdf(frames, per_page=6, title=None):
    """frames: [{data, w, h, t}]（已排序）→ 完整 PDF bytes。

    per_page ∈ {1, 2, 6}（U12 版式）；首页顶部带标题条（title 已是 ASCII），
    每页底部页码 "Page N / M"（ASCII，内置 Helvetica）。
    """
    if type(per_page) is not int or per_page not in _LAYOUTS:
        raise ValueError("per_page 必须是 1 / 2 / 6")
    cols, rows = _LAYOUTS[per_page]
    page_cap = cols * rows
    pages = [frames[i:i + page_cap] for i in range(0, len(frames), page_cap)]
    n_pages = len(pages)
    img_base = 4 + 2 * n_pages   # 图像 XObject 起始对象号

    cell_w = (PAGE_W - 2 * MARGIN - COL_GAP * (cols - 1)) / cols
    cell_h_full = (PAGE_H - 2 * MARGIN) / rows

    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii")
    objects[3] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                  b"/Encoding /WinAnsiEncoding >>")

    for pi, page in enumerate(pages):
        page_num = 4 + 2 * pi
        content_num = page_num + 1
        xobj_entries = []
        content = []
        # 首页顶部标题条（U12）
        top_off = 0.0
        if pi == 0 and title:
            top_off = TITLE_H
            safe = "".join(c if 32 <= ord(c) < 127 else " " for c in title)
            # 单行标题的保守宽度上限；PDF 字符串中的括号与反斜线必须转义。
            safe = safe if len(safe) <= 46 else safe[:43] + "..."
            safe = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append(
                f"BT /F1 11 Tf 0 g 1 0 0 1 {_num(MARGIN)} "
                f"{_num(PAGE_H - MARGIN - 14)} Tm ({safe}) Tj ET")
        cell_h = (PAGE_H - 2 * MARGIN - top_off) / rows
        box_w, box_h = cell_w, cell_h - CAPTION_H
        for ci, fr in enumerate(page):
            gi = pi * page_cap + ci
            name = f"Im{gi}"
            xobj_entries.append(f"/{name} {img_base + gi} 0 R")
            scale = min(box_w / fr["w"], box_h / fr["h"])
            dw, dh = fr["w"] * scale, fr["h"] * scale
            row, col = divmod(ci, cols)
            cell_x = MARGIN + col * (cell_w + COL_GAP)
            cell_top = PAGE_H - MARGIN - top_off - row * cell_h
            img_x = cell_x + (cell_w - dw) / 2      # 水平居中
            img_y = cell_top - dh                   # 顶部对齐
            cap = f"#{gi + 1}  t={fmt_caption_t(fr['t'])}"
            cap_w = len(cap) * FONT_SIZE * 0.52     # Helvetica 均宽近似，用于居中
            cap_x = cell_x + max(0.0, (cell_w - cap_w) / 2)
            cap_y = img_y - FONT_SIZE - 3
            content.append(
                f"q\n{_num(dw)} 0 0 {_num(dh)} {_num(img_x)} {_num(img_y)} cm\n"
                f"/{name} Do\nQ\n"
                f"BT /F1 {FONT_SIZE} Tf 0 g 1 0 0 1 {_num(cap_x)} {_num(cap_y)} Tm "
                f"({cap}) Tj ET"
            )
        # 页脚页码（ASCII）：Page N / M，底部居中
        footer = f"Page {pi + 1} / {n_pages}"
        fw = len(footer) * FONT_SIZE * 0.52
        content.append(
            f"BT /F1 {FONT_SIZE} Tf 0.4 g 1 0 0 1 "
            f"{_num((PAGE_W - fw) / 2)} {_num(MARGIN / 2)} Tm ({footer}) Tj ET")
        stream = "\n".join(content).encode("ascii")   # content stream 不压缩
        xobjs = " ".join(xobj_entries)
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(PAGE_W)} {_num(PAGE_H)}] "
            f"/Resources << /XObject << {xobjs} >> /Font << /F1 3 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("ascii")
        objects[content_num] = (b"<< /Length " + str(len(stream)).encode("ascii")
                                + b" >>\nstream\n" + stream + b"\nendstream")

    for gi, fr in enumerate(frames):
        objects[img_base + gi] = (
            f"<< /Type /XObject /Subtype /Image /Width {fr['w']} /Height {fr['h']} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(fr['data'])} >>\nstream\n"
        ).encode("ascii") + fr["data"] + b"\nendstream"

    return _serialize(objects)


def _serialize(objects):
    """按对象号升序写出，xref 表字节偏移精确，trailer + %%EOF 完整。"""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii")
        out += objects[num]
        out += b"\nendobj\n"
    xref_pos = len(out)
    size = max(objects) + 1
    lines = [f"xref\n0 {size}\n", "0000000000 65535 f \n"]
    for n in range(1, size):
        lines.append(f"{offsets[n]:010d} 00000 n \n")
    out += "".join(lines).encode("ascii")
    out += (f"trailer\n<< /Size {size} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode("ascii")
    return bytes(out)


# ---------------------------------------------------------------------------
# 顶层流程
# ---------------------------------------------------------------------------
def build_from_run_dir(run_dir, out_path, per_page=6, title=None):
    """读取 <run_dir>/frames/frames.json 并原子生成 PDF，返回结果 dict。

    per_page ∈ {1,2,6}；标题缺省取 manifest.json 的 title（ASCII 化）+ 日期。
    """
    run_dir = Path(run_dir)
    frames_json = run_dir / "frames" / "frames.json"
    if not frames_json.is_file():
        raise ValueError(f"找不到 frames.json: {frames_json}")
    items, skipped = load_frame_items(frames_json)
    if not items:
        raise ValueError("frames.json 中没有可用帧")
    frames, read_skipped = read_frames(frames_json, items)
    skipped += read_skipped
    if not frames:
        raise ValueError("所有帧均不可用（读取或解析失败）")
    if title is None:
        # 标题取 manifest 的 title（无则目录名），ASCII 化防字体嵌入问题（U12）
        raw_title = run_dir.name
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            raw_title = manifest.get("title") or raw_title
        except (OSError, json.JSONDecodeError):
            pass
        title = _ascii_title(raw_title, datetime.now().strftime("%Y-%m-%d"))
    pdf_bytes = build_pdf(frames, per_page=per_page, title=title)
    out_path = Path(out_path)
    tmp = out_path.with_name(out_path.name + ".tmp")   # 原子写：先 .tmp 再 replace
    try:
        tmp.write_bytes(pdf_bytes)
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    n_pages = (len(frames) + per_page - 1) // per_page
    log(f"已生成: {out_path}（{n_pages} 页，{len(frames)} 帧，跳过 {skipped}）")
    return {"ok": True, "pdf": str(out_path.resolve()), "pages": n_pages,
            "frames_included": len(frames), "frames_skipped": skipped,
            "per_page": per_page}


def main():
    setup_stdio()
    ap = CliParser(
        prog="make_frames_pdf.py",
        description="把 run 目录的关键帧导出为带时间标识的 PDF（A4，2列×3行/页，Helvetica caption）",
    )
    ap.add_argument("--run-dir", required=True, help="任务目录（其下应有 frames/frames.json）")
    ap.add_argument("--out", default=None, help="输出 PDF 路径（默认 <run-dir>/关键帧.pdf）")
    ap.add_argument("--per-page", type=int, choices=[1, 2, 6], default=6,
                    help="每页帧数版式：1/2/6（默认 6；PPT/录屏场景 1–2 帧/页更清晰）")
    ap.add_argument("--title", default=None,
                    help="PDF 首页标题（默认取 manifest.json 的 title，ASCII 化）")
    args = ap.parse_args()

    try:
        run_dir = Path(args.run_dir).expanduser()
        if not run_dir.is_dir():
            fail(f"任务目录不存在: {run_dir}")
        out_path = Path(args.out).expanduser() if args.out else run_dir / "关键帧.pdf"
        result = build_from_run_dir(run_dir, out_path,
                                    per_page=args.per_page, title=args.title)
        print_result(result)
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"{exc}")


if __name__ == "__main__":
    main()
