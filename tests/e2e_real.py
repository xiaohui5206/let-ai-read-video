#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_real.py — 真实端到端回归（不进 unittest discover，单独运行）。

    python tests/e2e_real.py

用 ffmpeg lavfi 现场造短媒体，起真实 webui 服务走 HTTP 全流程，覆盖 C01-C08
关键验收点：产物级状态（有声/无音轨/纯音频）、自定义间隔帧数、去重口径与恢复、
关键字补帧 manifest 同步与幂等、任务持久化（重启 interrupted）。
需要 tools/ 或 PATH 里的 ffmpeg/ffprobe 与可用的 faster-whisper（转写用 tiny 之外
的默认 small；无模型时会首次下载）。产物/历史库均在专用临时目录，跑完统一清理。
"""
from __future__ import annotations

import json
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import common  # noqa: E402
import webui  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"（{detail}）" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def ffmpeg(ff, args):
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=180)


def make_media(ff, tmp):
    av = tmp / "with_audio_20s.mp4"      # 有声 20s（间隔/关键字/去重样本）
    ffmpeg(ff, ["-f", "lavfi", "-i", "testsrc=duration=20:size=320x240:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(av)])
    na = tmp / "no_audio_8s.mp4"         # 无音轨
    ffmpeg(ff, ["-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10",
                "-an", "-pix_fmt", "yuv420p", str(na)])
    ao = tmp / "audio_only_8s.m4a"       # 纯音频
    ffmpeg(ff, ["-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                "-c:a", "aac", str(ao)])
    dup = tmp / "dup_4s.mp4"             # 纯红 4s（去重样本：帧完全相同）
    ffmpeg(ff, ["-f", "lavfi", "-i", "color=red:320x240:duration=4:rate=2",
                "-pix_fmt", "yuv420p", str(dup)])
    return av, na, ao, dup


def history_records(root):
    """只读查询日常历史，用于证明测试没有写入或标记它。"""
    db = root / ".webui_jobs.db"
    if not db.is_file():
        return []
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        return conn.execute("SELECT job_id,status FROM jobs ORDER BY job_id").fetchall()


def main():
    common.setup_stdio()   # Windows 控制台 UTF-8（✓/✗ 标记需要）
    ff = common.find_tool("ffmpeg")
    if not ff:
        print("SKIP: 未找到 ffmpeg")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="e2e_real_"))
    daily_root = common.runs_dir().resolve()
    before_history = history_records(daily_root)
    test_runs = tmp / "runs"
    service_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "VIDEO_WATCH_RUNS_DIR": str(test_runs)}
    created_runs = []
    proc = None
    proc2 = None
    try:
        av, na, ao, dup = make_media(ff, tmp)
        print(f"媒体就绪: {tmp}")

        # ---- 起真实服务 ----
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "webui.py"),
             "--no-browser", "--port", "0", "--runs-dir", str(test_runs)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=service_env)
        port = None
        for line in proc.stdout:
            if "http://127.0.0.1:" in line:
                port = int(line.split("http://127.0.0.1:")[1].split("/")[0])
                break
        if not port:
            raise RuntimeError("webui 未能启动")
        base = f"http://127.0.0.1:{port}"
        print(f"服务: {base}")

        def post(path, payload):
            req = urllib.request.Request(
                base + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())

        def get(path):
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return json.loads(r.read().decode())

        def wait_job(job_id, timeout=600):
            deadline = time.time() + timeout
            while time.time() < deadline:
                info = get(f"/api/jobs/{job_id}")
                if info["status"] not in ("queued", "running", "cancelling"):
                    return info
                time.sleep(1)
            raise TimeoutError(job_id)

        check("测试服务使用专用产物根", Path(get("/api/instance")["runs_root"]) == test_runs)
        outside = tmp / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            urllib.request.urlopen(base + "/api/files?path=" + urllib.parse.quote(str(outside)), timeout=10)
            check("隔离根之外的文件拒绝预览", False)
        except urllib.error.HTTPError as exc:
            check("隔离根之外的文件拒绝预览", exc.code == 403)

        # ---- A：有声 20s，同时生成 + 自定义间隔 5s ----
        print("任务 A：有声视频 + 间隔 5s + 同时生成")
        jid = post("/api/jobs", {
            "mode": "single", "url": str(av),
            "want_transcript": True, "want_frames": True,
            "frame_rule": {"type": "interval", "value": 5},
        })["job_id"]
        info = wait_job(jid)
        r = info["results"][0]
        created_runs.append(r["run_dir"])
        check("A 整体 done", info["status"] == "done", info["status"])
        check("A 文字稿 succeeded",
              r["artifacts"]["transcript"]["status"] == "succeeded",
              str(r["artifacts"]["transcript"]))
        check("A 关键帧 succeeded",
              r["artifacts"]["frames"]["status"] == "succeeded")
        fj = Path(r["frames_json"])
        entries = json.loads(fj.read_text(encoding="utf-8"))
        times = [e["actual_t"] for e in entries]
        check("A 间隔 5s → 恰好 0/5/10/15 四帧",
              r["frame_count"] == 4 and times == [0.0, 5.0, 10.0, 15.0], str(times))
        for per_page in (1, 2, 6):
            exported = post("/api/export_pdf", {"run_dir": r["run_dir"], "per_page": per_page})
            expected_pages = (4 + per_page - 1) // per_page
            check(f"PDF 每页{per_page}帧 HTTP 真实导出与页数",
                  exported.get("ok") and exported["pages"] == expected_pages
                  and exported["per_page"] == per_page, str(exported))
            with urllib.request.urlopen(base + "/api/files?path=" + urllib.parse.quote(exported["pdf"])) as resp:
                data = resp.read()
            check(f"PDF 每页{per_page}帧 各页页码存在",
                  all(f"Page {n} / {expected_pages}".encode() in data for n in range(1, expected_pages + 1)))

        # ---- A 续：关键字补帧（假 transcript 命中"你好"，真实 frames.py 管线）----
        print("任务 A 续：关键字补帧 + manifest 同步幂等")
        run_dir = Path(r["run_dir"])
        # 中点 7.0s 避开间隔网格 0/5/10/15（否则 frames.py 按时间去重，新增为 0）
        (run_dir / "transcript.json").write_text(json.dumps(
            [{"start": 6.5, "end": 7.5, "text": "你好世界"}]), encoding="utf-8")
        jid_a = jid
        kjob = webui.Job("single", [str(av)], True, True, keywords=["你好"])
        kjob.results = [dict(r)]
        webui.keyword_lock_frames(kjob)
        kw = kjob.results[0].get("keyword", {})
        check("关键字命中 1 处、新增 1 帧",
              kw.get("matched") == 1 and kw.get("added") == 1, str(kw))
        report = json.loads(Path(kjob.results[0]["keyword_report"]).read_text(encoding="utf-8"))
        check("命中报告关联真实新增帧", report["hits"][0]["reason"] == "added"
              and report["hits"][0]["frame_taken"])
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        kw_passes = [p for p in manifest["frames"]["passes"] if p.get("pass_id") == "kw1"]
        check("manifest kw1 pass 落账", len(kw_passes) == 1 and kw_passes[0]["count"] == 1)
        check("manifest count 同步为 5", manifest["frames"]["count"] == 5,
              str(manifest["frames"]["count"]))
        webui.keyword_lock_frames(kjob)   # 幂等重跑
        kw2 = kjob.results[1].get("keyword", {}) if len(kjob.results) > 1 else \
            kjob.results[0].get("keyword", {})
        report = json.loads(Path(kjob.results[0]["keyword_report"]).read_text(encoding="utf-8"))
        check("命中报告重跑显示复用帧", report["hits"][0]["reason"] == "reused")
        manifest2 = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        kw_passes2 = [p for p in manifest2["frames"]["passes"] if p.get("pass_id") == "kw1"]
        check("关键字重跑幂等（added=0、count 不变、kw1 不重复）",
              kw2.get("added") == 0 and manifest2["frames"]["count"] == 5
              and len(kw_passes2) == 1,
              f"added={kw2.get('added')} count={manifest2['frames']['count']} "
              f"passes={len(kw_passes2)}")

        # ---- B：无音轨 → 文字稿已跳过（C01/C02）----
        print("任务 B：无音轨视频")
        jid = post("/api/jobs", {"mode": "single", "url": str(na),
                                 "want_transcript": True, "want_frames": True, "frame_width": 1024})["job_id"]
        info = wait_job(jid)
        r = info["results"][0]
        created_runs.append(r["run_dir"])
        check("B 整体 done（产物级跳过不算失败）", info["status"] == "done", info["status"])
        check("B 文字稿 skipped:无音轨",
              r["artifacts"]["transcript"]["status"] == "skipped"
              and r["artifacts"]["transcript"]["reason"] == "无音轨",
              str(r["artifacts"]["transcript"]))
        check("B 关键帧 succeeded", r["artifacts"]["frames"]["status"] == "succeeded")
        import make_frames_pdf
        frame = json.loads(Path(r["frames_json"]).read_text(encoding="utf-8"))[0]
        jpeg_width, _ = make_frames_pdf.jpeg_size((Path(r["frames_json"]).parent / frame["file"]).read_bytes())
        check("1024清晰度传至真实 JPEG", jpeg_width == 1024)
        check("B 进度通道 transcript=skipped",
              info["progress"]["transcript"]["state"] == "skipped",
              str(info["progress"]["transcript"]))

        # ---- C：纯音频 → 关键帧已跳过 ----
        print("任务 C：纯音频")
        jid = post("/api/jobs", {"mode": "single", "url": str(ao),
                                 "want_transcript": True, "want_frames": True})["job_id"]
        info = wait_job(jid)
        r = info["results"][0]
        created_runs.append(r["run_dir"])
        check("C 文字稿 succeeded", r["artifacts"]["transcript"]["status"] == "succeeded",
              str(r["artifacts"]["transcript"]))
        check("C 关键帧 skipped:无视频流",
              r["artifacts"]["frames"]["status"] == "skipped"
              and r["artifacts"]["frames"]["reason"] == "无视频流",
              str(r["artifacts"]["frames"]))

        # ---- D：去重口径 + 恢复（纯红视频帧完全相同）----
        print("任务 D：去重 + 恢复")
        jid = post("/api/jobs", {"mode": "single", "url": str(dup),
                                 "want_transcript": False, "want_frames": True,
                                 "frame_rule": {"type": "interval", "value": 2},
                                 "dedup": {"enabled": True, "threshold": 0.95}})["job_id"]
        info = wait_job(jid)
        r = info["results"][0]
        created_runs.append(r["run_dir"])
        d = r.get("dedup", {})
        check("D 去重口径 total=kept+dropped",
              d.get("total_count") == d.get("kept_count", -1) + d.get("dropped_count", -1),
              str(d))
        check("D 纯红重复帧被剔除（dropped≥1）", d.get("dropped_count", 0) >= 1, str(d))
        fj = json.loads(Path(r["frames_json"]).read_text(encoding="utf-8"))
        dropped_files = [e["file"] for e in fj if e.get("dropped")]
        if dropped_files:
            res = post("/api/dedup_restore", {"run_dir": r["run_dir"],
                                              "file": dropped_files[0]})
            check("D 恢复后 kept+1 / dropped-1",
                  res.get("ok") and res["kept_count"] == d["kept_count"] + 1
                  and res["dropped_count"] == d["dropped_count"] - 1, str(res))
            fresh = get("/api/jobs/" + jid)["results"][0]
            check("D 刷新任务后恢复数量一致",
                  fresh["dedup"]["kept_count"] == res["kept_count"]
                  and fresh["friendly"]["frame_count"] == res["kept_count"], str(fresh.get("dedup")))
        else:
            check("D 有帧被标记 dropped", False, "frames.json 无 dropped")

        # ---- T01：重启后任务在、运行中任务被标 interrupted ----
        print("T01：服务重启语义")
        listing = get("/api/jobs?limit=10")
        check("任务列表包含本次任务", jid in [j["job_id"] for j in listing["jobs"]])
        db = test_runs / ".webui_jobs.db"
        check("sqlite 库落盘", db.is_file())
        # 模拟重启语义：直接往库里塞一个 running 再 mark_interrupted 已由单测覆盖，
        # 这里验证重启进程后历史仍可读
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None
        proc2 = subprocess.Popen(
            [sys.executable, str(REPO / "scripts" / "webui.py"),
             "--no-browser", "--port", "0", "--runs-dir", str(test_runs)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=service_env)
        port2 = None
        for line in proc2.stdout:
            if "http://127.0.0.1:" in line:
                port2 = int(line.split("http://127.0.0.1:")[1].split("/")[0])
                break
        listing2 = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port2}/api/jobs?limit=10", timeout=30).read().decode())
        row = next((j for j in listing2["jobs"] if j["job_id"] == jid), None)
        check("重启后任务仍可读且状态为 done", row is not None and row["status"] == "done")
        if dropped_files:
            check("重启后恢复数量仍一致",
                  row is not None and row["results"][0]["dedup"]["kept_count"] == res["kept_count"],
                  str(row and row["results"][0].get("dedup")))
        # 用任务 A 的行验证重启后产物仍可预览（真实文件存在性）
        row_a = next((j for j in listing2["jobs"] if j["job_id"] == jid_a), None)
        tt = row_a and row_a.get("results") and row_a["results"][0].get("transcript_txt")
        if tt and Path(tt).is_file():
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port2}/api/files?path="
                    + urllib.parse.quote(tt), timeout=10) as resp:
                check("重启后产物仍可预览", resp.status == 200)
        else:
            check("重启后产物仍可预览", False, "transcript 路径缺失")
        # 真实删除测试自身的产物文件，历史状态不回写，预览能力按当前事实刷新。
        if tt:
            Path(tt).unlink(missing_ok=True)
            friendly_tt = (row_a["results"][0].get("friendly") or {}).get("transcript_txt")
            if friendly_tt:
                Path(friendly_tt).unlink(missing_ok=True)
            changed = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port2}/api/jobs/{jid_a}", timeout=10).read().decode())
            check("历史文字稿移除后预览能力失效", changed["results"][0]["availability"]["transcript"] is False)
            check("文件失效保留原任务状态和路径", changed["status"] == "done"
                  and changed["results"][0]["transcript_txt"] == tt)
        check("所有测试产物位于专用根", all(Path(rd).resolve().is_relative_to(test_runs.resolve()) for rd in created_runs))
        check("日常任务数量与状态没有变化", history_records(daily_root) == before_history)
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
        proc2 = None

        if FAILURES:
            print(f"\n✗ {len(FAILURES)} 项失败: {FAILURES}")
            return 1
        print("\n✓ e2e_real 全部通过")
        return 0
    finally:
        for service in (proc, proc2):
            if service is None:
                continue
            service.terminate()
            try:
                service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=10)
        resolved_tmp = tmp.resolve()
        if resolved_tmp.is_relative_to(Path(tempfile.gettempdir()).resolve()) and resolved_tmp.name.startswith("e2e_real_"):
            shutil.rmtree(resolved_tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
