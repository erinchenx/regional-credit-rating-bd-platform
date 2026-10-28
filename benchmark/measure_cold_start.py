# -*- coding: utf-8 -*-
"""
benchmark/measure_cold_start.py
===============================
一次性测量：全应用冷启动耗时（Excel 降级路径 vs DuckDB 仓库路径）。

用途：为对外口径「全应用从 X 秒变 Y 秒」提供真实 before/after 数字。

用法（每次调用都在独立进程里跑一次冷启动，不做预热、不重复，确保无跨进程缓存残留）：
  python benchmark/measure_cold_start.py --mode duckdb   # 新流程（after）
  python benchmark/measure_cold_start.py --mode excel    # 旧流程（before，临时把 .duckdb 挪走以触发降级）

测量口径：
  - 用 streamlit.testing.v1.AppTest 无头执行 app/main.py，计时首次 at.run()（冷启动）。
  - 覆盖完整脚本执行：Staging 读取 + Intermediate 关联 + Marts 聚合 + 5 个 tab 的
    Python 侧 plotly 图构建；不含浏览器 JS 绘制。
  - excel 模式会临时把 data/serving/credit_indicators.duckdb 改名，测完在 finally 里还原。

注意：这不是基准套件的常驻第 5 项，不接入 run_all.py；仅供手工一次性取证。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "serving" / "credit_indicators.duckdb"
BAK = DB.with_suffix(".duckdb.bak")
MAIN = ROOT / "app" / "main.py"


def _measure_once() -> dict:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(MAIN), default_timeout=300)
    t0 = time.perf_counter()
    at.run()
    dt = (time.perf_counter() - t0) * 1000.0
    return {"ms": round(dt, 1), "exception": str(at.exception) if at.exception else None}


def main() -> None:
    ap = argparse.ArgumentParser(description="一次性测量全应用冷启动耗时")
    ap.add_argument("--mode", choices=["duckdb", "excel"], required=True)
    args = ap.parse_args()

    if args.mode == "excel":
        # 自愈：若上次异常中断留下了 .bak 且原文件缺失，先还原
        if BAK.exists() and not DB.exists():
            BAK.rename(DB)
        if not DB.exists():
            raise SystemExit(f"未找到 {DB}，无法测量 Excel 降级路径")
        DB.rename(BAK)

    try:
        r = _measure_once()
    finally:
        if args.mode == "excel" and BAK.exists() and not DB.exists():
            BAK.rename(DB)

    if r["exception"]:
        print(f"[{args.mode}] 运行异常：{r['exception']}", file=sys.stderr)
        sys.exit(1)
    print(f"[{args.mode}] 全应用冷启动 {r['ms']:.0f} ms")


if __name__ == "__main__":
    main()
