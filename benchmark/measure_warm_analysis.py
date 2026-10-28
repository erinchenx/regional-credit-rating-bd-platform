# -*- coding: utf-8 -*-
"""
benchmark/measure_warm_analysis.py
==================================
一次性测量：**非首次分析**（同一会话、缓存已预热）下新旧流程的完整数据层耗时。

背景：
  对外口径里的「3.2s → 0.03–0.07s」是冷读取口径（每轮清缓存、重新解析 Excel）。
  但真实使用时，用户在同一会话里第二次、第三次点击「开始分析」时，
  @st.cache_data 会命中 stg_load_* / int_* 缓存，Excel 不再重复解析。
  本脚本测的就是这个「稳态」口径。

测量范围（与 app/main.py 每次 rerun 的数据层一致）：
  · Staging   stg_load_bond_data / stg_load_fiscal_data（预热后，计时内为缓存命中）
  · Intermediate int_filter_province → int_join_fiscal → int_build_issuer_view
  · Marts     Tab0 机构表 + 主承销商表 + 竞争热力矩阵
              Tab2 财务准入基准
              Tab3 展业帮手圈
              Marts DQC 审计
  · 新流程额外执行 run_analysis 的 5 条 DuckDB 视图查询

只读运行，不修改项目文件；不接入 run_all.py。

用法：
  python benchmark/measure_warm_analysis.py [province]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C
import bench2_pandas_vs_duckdb as B2

from models.staging.stg_data import _file_mtime, stg_load_bond_data, stg_load_fiscal_data
from models.intermediate.int_data import (
    int_build_issuer_view,
    int_filter_province,
    int_join_fiscal,
)
from models.marts.mart_credit_indicators import (
    dqc_marts_platform_dismatch,
    mart_agency_stats,
    mart_competition_matrix,
    mart_partner_network,
    mart_underwriter_stats,
)


def _q(sql: str, params: list) -> object:
    return C.qdb(sql, params)


def old_pipeline(bond_xl, fiscal_xl, prov_name, agency, triple):
    prov = int_filter_province(bond_xl, prov_name)
    joined = int_join_fiscal(prov.copy(), fiscal_xl)
    iss = int_build_issuer_view(joined)

    sc_pj = mart_agency_stats(prov)                  # Tab0 机构表
    mart_underwriter_stats(prov)                     # Tab3 主承销商排名
    mart_competition_matrix(joined, agency, "fiscal")  # Tab0 热力矩阵（pandas）
    B2.q1_pandas_compute(iss, triple)                # Tab2 财务准入基准（pandas）
    mart_partner_network(joined, agency, "", "")      # Tab3
    dqc_marts_platform_dismatch(iss, prov_name)       # Step 4 DQC
    return sc_pj


def new_pipeline(df_qg, prov_name, agency, triple, like, bench_sql, bench_params, fy, snap):
    fiscal_xlsx = C.RAW_FISCAL_DIR / f"{fy}年{prov_name}财力.xlsx"
    fiscal = stg_load_fiscal_data(
        C.RAW_FISCAL_DIR, fy, prov_name,
        _mtime=_file_mtime(fiscal_xlsx),
        db_path=C.DB_PATH, _active_snapshot=snap,
    )
    prov = int_filter_province(df_qg, prov_name)
    joined = int_join_fiscal(prov.copy(), fiscal)
    iss = int_build_issuer_view(joined)

    # run_analysis 的 5 条 DuckDB 视图查询
    _q("SELECT * FROM v_province_kpi WHERE 省份 LIKE ?", [like])
    _q("SELECT * FROM v_agency_market_share WHERE 省份 LIKE ? ORDER BY 主体数 DESC", [like])
    _q("SELECT * FROM v_underwriter_stats WHERE 省份 LIKE ? ORDER BY 序号", [like])
    _q("SELECT DISTINCT 主体评级机构 FROM v_agency_competitive_landscape "
       "WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL", [like])
    _q("SELECT * FROM v_issuer_profile WHERE 省份 LIKE ? "
       "ORDER BY 城市财力_亿元 DESC NULLS LAST, 行政级别量化 DESC, 主体级别量化 DESC", [like])

    mart_competition_matrix(joined, agency, "fiscal")   # Tab0 矩阵仍走 pandas（与 App 一致）
    B2.q1_duck_to_bench(C.qdb(bench_sql, bench_params))  # Tab2 DuckDB
    C.qdb(B2._Q2_SQL, [like, agency])                    # Tab3 DuckDB
    dqc_marts_platform_dismatch(iss, prov_name)         # Step 4 DQC
    return iss


def run(province: str | None = None, warmup: int = 1, reps: int = 10) -> dict:
    C.require_warehouse()
    C.header("非首次分析（缓存预热）· 新旧流程数据层耗时")

    ctx = B2.Ctx(province)
    prov_name, agency, triple, like = ctx.province, ctx.agency, ctx.triple, ctx.like
    bench_sql, bench_params = ctx.bench_sql_params()
    try:
        ctx.mem.close()
    except Exception:
        pass
    C.info(f"省份={prov_name}  机构={agency}  三元组={triple}")

    fy, snap = C.fiscal_year(), C.active_snapshot()
    import bench3_old_vs_new_flow as B3
    bond_xlsx = B3._bond_excel_for_snapshot(snap)
    if bond_xlsx is None:
        raise SystemExit("未找到原始债券 Excel，无法测旧流程")

    # ── 旧流程：Excel 数据冷加载一次（计时外），随后缓存预热 ──
    C.clear_caches()
    bond_xl = stg_load_bond_data(bond_xlsx, _mtime=_file_mtime(bond_xlsx))
    fiscal_xl = stg_load_fiscal_data(
        C.RAW_FISCAL_DIR, fy, prov_name, _mtime=_file_mtime(C.RAW_FISCAL_DIR / f"{fy}年{prov_name}财力.xlsx")
    )
    old_pipeline(bond_xl, fiscal_xl, prov_name, agency, triple)  # 预热 int_* 缓存
    t_old = C.timeit(lambda: old_pipeline(bond_xl, fiscal_xl, prov_name, agency, triple),
                     warmup=warmup, reps=reps)

    # ── 新流程：清缓存后按 DuckDB 路径重新预热 ──
    C.clear_caches()
    df_qg = C.get_bond_df()
    new_pipeline(df_qg, prov_name, agency, triple, like, bench_sql, bench_params, fy, snap)
    t_new = C.timeit(lambda: new_pipeline(df_qg, prov_name, agency, triple, like,
                                          bench_sql, bench_params, fy, snap),
                     warmup=warmup, reps=reps)

    print("\n  ── 非首次分析 · 完整数据层（中位数）──")
    print(f"    旧流程（Excel 缓存 + pandas marts）: {C.fmt_ms(t_old['median'])}")
    print(f"    新流程（DuckDB 缓存 + 视图查询）    : {C.fmt_ms(t_new['median'])}")
    print(f"    倍数（旧/新）                       : {t_old['median']/t_new['median']:.2f}x")

    return {
        "province": prov_name, "agency": agency, "triple": triple,
        "old_warm": t_old, "new_warm": t_new,
        "ratio": round(t_old["median"] / t_new["median"], 2) if t_new["median"] else None,
        "config": {"warmup": warmup, "reps": reps},
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="非首次分析：新旧流程数据层耗时")
    ap.add_argument("province", nargs="?", default=None)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--reps", type=int, default=10)
    a = ap.parse_args()
    run(a.province, a.warmup, a.reps)
