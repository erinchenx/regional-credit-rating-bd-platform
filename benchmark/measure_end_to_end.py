# -*- coding: utf-8 -*-
"""
benchmark/measure_end_to_end.py
===============================
一次性补充测量：端到端口径（全应用冷启动 + 首次分析完整数据层）。

常规 `run_all.py` 只跑 bench1–bench4；本脚本补齐两个对外常用的「端到端」口径：

  1. **全应用冷启动**：独立进程、AppTest 无头执行 `app/main.py`，
     Excel 降级路径 vs DuckDB 仓库路径；含 Python import + 全脚本 + Python 侧
     plotly 图构建，不含浏览器 JS 绘制。
  2. **首次分析完整数据层**：清空 `@st.cache_data` 后从源文件读起，跑完所有 tab
     的数据层（Staging + Intermediate + Marts + DQC），不含 Python import 与前端。

结果写入 `benchmark/results/end_to_end.json`，由 `run_all.py --report-only`
汇总进 `benchmark/report.md`。不接入 run_all 的常规子进程（全应用冷启动需要
临时挪走 `.duckdb`，且 Excel 冷启动较慢）。

用法：
  python benchmark/measure_end_to_end.py
  python benchmark/measure_end_to_end.py --reps 5 --app-runs 3
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
import bench2_pandas_vs_duckdb as B2
import bench3_old_vs_new_flow as B3
from measure_warm_analysis import old_pipeline, new_pipeline

from models.staging.stg_data import (
    _file_mtime,
    stg_load_bond_data,
    stg_load_fiscal_data,
)


def _old_cold(bond_xlsx, fiscal_xlsx, prov_name, agency, triple, fy):
    """旧流程冷读取：每轮重新解析 Excel，再跑完整数据层。"""
    bond_xl = stg_load_bond_data(bond_xlsx, _mtime=_file_mtime(bond_xlsx))
    fiscal_xl = stg_load_fiscal_data(
        C.RAW_FISCAL_DIR, fy, prov_name, _mtime=_file_mtime(fiscal_xlsx)
    )
    return old_pipeline(bond_xl, fiscal_xl, prov_name, agency, triple)


def _new_cold(prov_name, agency, triple, like, bench_sql, bench_params, fy, snap):
    """新流程冷读取：每轮重新读 Parquet，再跑完整数据层。"""
    df_qg = C.get_bond_df()
    return new_pipeline(
        df_qg, prov_name, agency, triple, like,
        bench_sql, bench_params, fy, snap,
    )


def measure_data_layer(province: str | None, warmup: int, reps: int) -> dict:
    """首次分析完整数据层：清缓存，从源文件读起，跑完所有 tab 的数据层。"""
    C.header("端到端补充 ① 首次分析 · 完整数据层")
    ctx = B2.Ctx(province)
    prov_name, agency, triple, like = ctx.province, ctx.agency, ctx.triple, ctx.like
    bench_sql, bench_params = ctx.bench_sql_params()
    try:
        ctx.mem.close()
    except Exception:
        pass

    fy, snap = C.fiscal_year(), C.active_snapshot()
    bond_xlsx = B3._bond_excel_for_snapshot(snap)
    if bond_xlsx is None:
        raise SystemExit("未找到原始债券 Excel，无法测旧流程")
    fiscal_xlsx = C.RAW_FISCAL_DIR / f"{fy}年{prov_name}财力.xlsx"

    C.info(f"省份={prov_name}  机构={agency}  三元组={triple}")
    C.info(f"旧流程债券 Excel：{bond_xlsx.name}")

    t_old = C.timeit(
        lambda: _old_cold(bond_xlsx, fiscal_xlsx, prov_name, agency, triple, fy),
        warmup=warmup, reps=reps, before_each=C.clear_caches,
    )
    t_new = C.timeit(
        lambda: _new_cold(prov_name, agency, triple, like,
                          bench_sql, bench_params, fy, snap),
        warmup=warmup, reps=reps, before_each=C.clear_caches,
    )

    print("\n  ── 首次分析 · 完整数据层（清缓存，中位数）──")
    print(f"    旧流程（Excel + pandas）    : {C.fmt_ms(t_old['median'])}")
    print(f"    新流程（DuckDB + Parquet）  : {C.fmt_ms(t_new['median'])}")
    print(f"    倍数（旧/新）               : {t_old['median']/t_new['median']:.2f}x")

    return {
        "province": prov_name,
        "agency": agency,
        "drill_triple": {"城市": triple[0], "行政级别": triple[1], "主体评级": triple[2]},
        "old": t_old,
        "new": t_new,
        "ratio": round(t_old["median"] / t_new["median"], 2) if t_new["median"] else None,
        "config": {"warmup": warmup, "reps": reps},
    }


def _run_app_cold_once(mode: str) -> float:
    """在独立进程里跑一次全应用冷启动，返回毫秒。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, str(C.BENCH_DIR / "measure_cold_start.py"), "--mode", mode]
    proc = subprocess.run(
        cmd, cwd=str(C.PROJECT_ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"measure_cold_start.py --mode {mode} 失败：{proc.stderr.strip() or proc.stdout.strip()}"
        )
    m = re.search(r"全应用冷启动\s+([\d.]+)\s+ms", proc.stdout)
    if not m:
        raise RuntimeError(f"无法从输出解析冷启动耗时：{proc.stdout.strip()}")
    return float(m.group(1))


def measure_app_cold(runs: int) -> dict:
    """全应用冷启动：每次都在独立进程里跑，Excel 模式临时挪走 .duckdb。"""
    C.header("端到端补充 ② 全应用冷启动")
    old_samples, new_samples = [], []
    for i in range(runs):
        old_samples.append(_run_app_cold_once("excel"))
        new_samples.append(_run_app_cold_once("duckdb"))
        C.info(f"  第 {i+1}/{runs} 轮：Excel {old_samples[-1]:.0f} ms / "
               f"DuckDB {new_samples[-1]:.0f} ms")

    old, new = C.summarize(old_samples), C.summarize(new_samples)
    print("\n  ── 全应用冷启动（独立进程，中位数）──")
    print(f"    旧流程（Excel 降级）  : {C.fmt_ms(old['median'])}")
    print(f"    新流程（DuckDB）      : {C.fmt_ms(new['median'])}")
    print(f"    倍数（旧/新）         : {old['median']/new['median']:.2f}x")
    return {
        "old": old,
        "new": new,
        "ratio": round(old["median"] / new["median"], 2) if new["median"] else None,
        "config": {"runs": runs},
    }


def run(province: str | None = None, reps: int = 5, app_runs: int = 3,
        skip_app: bool = False) -> dict:
    C.require_warehouse()
    data_layer = measure_data_layer(province, warmup=1, reps=reps)
    app_cold = ({"available": False, "reason": "已通过 --skip-app 跳过"}
                if skip_app else measure_app_cold(app_runs))

    result = {
        "test": "end_to_end",
        "full_app_cold_start": app_cold,
        "first_analysis_data_layer": data_layer,
        "env": C.env_info(),
        "notes": [
            "全应用冷启动在独立进程中用 AppTest 执行 app/main.py，含 Python import + 全脚本 + "
            "Python 侧 plotly 构建，不含浏览器 JS 绘制。",
            "首次分析完整数据层清空 @st.cache_data 后从源文件读起，不含 Python import 与前端。",
            "单条查询的 3.2–3.4 s 与首次分析的 3.6 s 都含同一笔约 3.1 s 的全国 Excel 解析；"
            "前者按每条查询重复计费，后者整次分析只解析一次，因此 50–105× 是单查询口径，"
            "不是整次分析口径。",
        ],
    }
    C.save_json("end_to_end.json", result)
    C.info("结果已写入 benchmark/results/end_to_end.json")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="端到端补充测量：全应用冷启动 + 首次分析完整数据层")
    ap.add_argument("province", nargs="?", default=None, help="目标省份（默认与测试2一致）")
    ap.add_argument("--reps", type=int, default=5, help="数据层重复次数，默认 5")
    ap.add_argument("--app-runs", type=int, default=3, help="全应用冷启动轮数，默认 3")
    ap.add_argument("--skip-app", action="store_true", help="跳过全应用冷启动")
    args = ap.parse_args()
    run(args.province, args.reps, args.app_runs, args.skip_app)
