#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动诊断入口；版本检查先于导入 WebUI，低版本也能给出明确提示。"""
import sys


def main(argv=None):
    if sys.version_info < (3, 10):
        print("[版本过低] 当前 Python %s，需要 Python 3.10+（推荐 3.11/3.12）。"
              % sys.version.split()[0], file=sys.stderr)
        return 3
    from pathlib import Path
    import traceback
    try:
        import webui
        return webui.main(argv)
    except Exception:
        log_root = getattr(sys.modules.get("webui"), "RUNS_ROOT", Path(__file__).resolve().parents[1] / "runs")
        log = log_root / "webui-startup.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            detail = "详细日志：%s" % log
        except OSError:
            detail = traceback.format_exc()
        print("[启动异常] 已找到 Python，但服务内部启动失败。\n" + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
