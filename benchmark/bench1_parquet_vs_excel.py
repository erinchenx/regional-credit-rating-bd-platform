# -*- coding: utf-8 -*-
"""
benchmark/bench1_parquet_vs_excel.py
====================================
【测试 1】Parquet 读取 vs Excel 读取（附 CSV 作参照）

验证表述：「Parquet 读取数据相比 Excel 更快（估算约 20 倍）」

方法：
  1. 取项目中最大的一张表（bond parquet，21,154 行 × 35 列），
     一次性导出为 .xlsx（openpyxl 引擎）与 .csv，保证三种格式数据完全一致。
  2. 分别计时：
       A. pandas.read_excel(xlsx)      —— openpyxl 解析
       B. pandas.read_parquet(parquet) —— 列式存储
       C. DuckDB read_parquet(...).df()—— 等价 App 的 v_bond 读取路径
       D. pandas.read_csv(csv)         —— 参照组
  3. 每组预热 1 次、重复 10 次，报告中位数 / 最小 / 最大 / P95。
  4. 计算 Excel 耗时 / Parquet 耗时 的倍数（pandas 与 DuckDB 两条 parquet 路径各算一次）。

只读运行，不修改任何项目文件；导出物写入 benchmark/results/。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd

import common as C


def ensure_exports(parquet_path: Path, force: bool = False) -> tuple[Path, Path]:
    """把最大表导出为 xlsx / csv（已存在则复用，--force-export 强制重建）。"""
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_path = C.RESULTS_DIR / "largest_table.xlsx"
    csv_path = C.RESULTS_DIR / "largest_table.csv"

    need_xlsx = force or not xlsx_path.exists()
    need_csv = force or not csv_path.exists()

    if need_xlsx or need_csv:
        C.info(f"读取源 parquet 以导出：{parquet_path.name}")
        df = pd.read_parquet(parquet_path)
        if need_xlsx:
            C.info(f"导出 xlsx（openpyxl，{len(df):,} 行）... 首次较慢，请稍候")
            # 项目未安装 xlsxwriter，使用 openpyxl 引擎
            df.to_excel(xlsx_path, index=False, engine="openpyxl")
        if need_csv:
            C.info(f"导出 csv（{len(df):,} 行）...")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    else:
        C.info("xlsx / csv 已存在，复用（--force-export 可强制重建）")

    return xlsx_path, csv_path


def _duckdb_read_parquet(parquet_path: Path) -> pd.DataFrame:
    """DuckDB 直接读 parquet → DataFrame（每次新建内存连接，模拟无缓存冷读）。"""
    posix = Path(parquet_path).resolve().as_posix()
    con = duckdb.connect()  # 内存库
    try:
        return con.execute(f"SELECT * FROM read_parquet('{posix}')").df()
    finally:
        con.close()


def run(warmup: int = 1, reps: int = 20, force_export: bool = False) -> dict:
    C.require_warehouse()
    C.header("测试 1 · Parquet vs Excel 读取")

    parquet_path = C.bond_parquet_path()
    xlsx_path, csv_path = ensure_exports(parquet_path, force=force_export)

    # 数据规模（以 parquet 为准）
    df_probe = pd.read_parquet(parquet_path)
    rows, cols = df_probe.shape
    col_names = list(df_probe.columns)
    del df_probe

    sizes = {
        "parquet": parquet_path.stat().st_size,
        "xlsx": xlsx_path.stat().st_size,
        "csv": csv_path.stat().st_size,
    }
    C.info(f"表规模：{rows:,} 行 × {cols} 列")
    C.info(f"文件大小：parquet={C.fmt_bytes(sizes['parquet'])}  "
           f"xlsx={C.fmt_bytes(sizes['xlsx'])}  csv={C.fmt_bytes(sizes['csv'])}")

    # ── 计时（每组预热 warmup 次 + 重复 reps 次）──
    C.info("计时 A：pandas.read_excel(xlsx) ...")
    t_excel = C.timeit(lambda: pd.read_excel(xlsx_path, engine="openpyxl"), warmup=warmup, reps=reps)

    C.info("计时 B：pandas.read_parquet(parquet) ...")
    t_pq_pd = C.timeit(lambda: pd.read_parquet(parquet_path), warmup=warmup, reps=reps)

    C.info("计时 C：DuckDB read_parquet(...).df() ...")
    t_pq_duck = C.timeit(lambda: _duckdb_read_parquet(parquet_path), warmup=warmup, reps=reps)

    C.info("计时 D（参照）：pandas.read_csv(csv) ...")
    t_csv = C.timeit(lambda: pd.read_csv(csv_path), warmup=warmup, reps=reps)

    # ── 倍数：Excel / Parquet ──
    ratio_pd = t_excel["median"] / t_pq_pd["median"] if t_pq_pd["median"] else None
    ratio_duck = t_excel["median"] / t_pq_duck["median"] if t_pq_duck["median"] else None

    result = {
        "test": "parquet_vs_excel",
        "table": parquet_path.name,
        "rows": rows,
        "cols": cols,
        "col_names": col_names,
        "file_sizes_bytes": sizes,
        "file_sizes_human": {k: C.fmt_bytes(v) for k, v in sizes.items()},
        "timings_ms": {
            "pandas_read_excel": t_excel,
            "pandas_read_parquet": t_pq_pd,
            "duckdb_read_parquet": t_pq_duck,
            "pandas_read_csv_ref": t_csv,
        },
        "ratios": {
            "excel_over_parquet_pandas": round(ratio_pd, 2) if ratio_pd else None,
            "excel_over_parquet_duckdb": round(ratio_duck, 2) if ratio_duck else None,
        },
        "config": {"warmup": warmup, "reps": reps},
        "env": C.env_info(),
    }

    # ── 控制台小结 ──
    print("\n  ── 结果（中位数）──")
    print(f"    pandas.read_excel   : {C.fmt_ms(t_excel['median'])}")
    print(f"    pandas.read_parquet : {C.fmt_ms(t_pq_pd['median'])}")
    print(f"    DuckDB read_parquet : {C.fmt_ms(t_pq_duck['median'])}")
    print(f"    pandas.read_csv(参照): {C.fmt_ms(t_csv['median'])}")
    print(f"    倍数 Excel/Parquet(pandas) : {ratio_pd:.1f}x" if ratio_pd else "    倍数：—")
    print(f"    倍数 Excel/Parquet(DuckDB) : {ratio_duck:.1f}x" if ratio_duck else "    倍数：—")

    C.save_json("bench1.json", result)
    C.info("结果已写入 benchmark/results/bench1.json")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试1：Parquet vs Excel 读取基准")
    parser.add_argument("--warmup", type=int, default=1, help="预热次数（默认 1）")
    parser.add_argument("--reps", type=int, default=20, help="重复次数（默认 20）")
    parser.add_argument("--force-export", action="store_true", help="强制重新导出 xlsx/csv")
    args = parser.parse_args()
    run(warmup=args.warmup, reps=args.reps, force_export=args.force_export)
