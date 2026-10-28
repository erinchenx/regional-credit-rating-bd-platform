# -*- coding: utf-8 -*-
"""
benchmark/bench4_province_switch.py
===================================
【测试 4】省份切换响应

验证表述：「省份切换响应很快（感觉是毫秒级）」

严格区分三类耗时，不混为一谈：
  ① 数据查询耗时（数据层，必测）：切换省份时真实运行的数据流——
       stg_load_fiscal_data(DuckDB) → int_filter_province → int_join_fiscal
       → int_build_issuer_view → 5 条 DuckDB 视图查询
       （v_province_kpi / v_agency_market_share / v_underwriter_stats /
         v_agency_competitive_landscape 机构列表 / v_issuer_profile）
     这与 app/main.py 的 run_analysis() 数据层完全一致。遍历全部省份，
     每省预热 1 次 + 重复 10 次，报告中位数 / P95 / 最大值。每轮清缓存。
  ② AppTest 全脚本耗时（端到端尽力项）：用 streamlit.testing.v1.AppTest 无头
     运行 app/main.py，切换省份并点击「开始分析」，测量服务端脚本执行耗时
     （含数据层 + Python 侧 plotly 图构建，但不含浏览器绘制）。
  ③ 浏览器渲染 / JS 绘制：本项目未安装 playwright，无法测量，报告中如实说明。

只读运行，不修改任何项目文件。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import common as C

from models.staging.stg_data import _file_mtime, stg_load_fiscal_data
from models.intermediate.int_data import (
    int_build_issuer_view,
    int_filter_province,
    int_join_fiscal,
)
from models.marts.mart_credit_indicators import dqc_marts_platform_dismatch


# ──────────────────────────────────────────────
# ① 数据层：复刻 run_analysis() 的查询/计算
# ──────────────────────────────────────────────
def datalayer_switch(province: str, df_qg: pd.DataFrame, fy: str, snap: str) -> dict:
    """切换到一个省份时，数据层真实执行的全部工作（与 main.py run_analysis 一致）。"""
    fiscal_path = C.RAW_FISCAL_DIR / f"{fy}年{province}财力.xlsx"
    df_fiscal = stg_load_fiscal_data(
        C.RAW_FISCAL_DIR, fy, province,
        _mtime=_file_mtime(fiscal_path),
        db_path=C.DB_PATH,
        _active_snapshot=snap,
    )

    df_prov = int_filter_province(df_qg, province)
    if df_prov.empty:
        return {"empty": True}
    df_prov = int_join_fiscal(df_prov, df_fiscal)
    sc_zt = int_build_issuer_view(df_prov)

    # ── Step 4：DQC 审计（与 App 启动一致）──
    t_dqc_start = time.perf_counter()
    dqc_marts_platform_dismatch(sc_zt, province)
    dqc_ms = (time.perf_counter() - t_dqc_start) * 1000.0

    like = f"%{province[:2]}%"
    province_kpi = C.qdb("SELECT * FROM v_province_kpi WHERE 省份 LIKE ?", [like])
    agencies = C.qdb(
        "SELECT * FROM v_agency_market_share WHERE 省份 LIKE ? ORDER BY 主体数 DESC", [like])
    underwriters = C.qdb(
        "SELECT * FROM v_underwriter_stats WHERE 省份 LIKE ? ORDER BY 序号", [like])
    ags_df = C.qdb(
        "SELECT DISTINCT 主体评级机构 FROM v_agency_competitive_landscape "
        "WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL", [like])
    sc_zt_db = C.qdb(
        "SELECT * FROM v_issuer_profile WHERE 省份 LIKE ? "
        "ORDER BY 城市财力_亿元 DESC NULLS LAST, 行政级别量化 DESC, 主体级别量化 DESC", [like])
    if not sc_zt_db.empty:
        sc_zt = sc_zt_db

    return {
        "empty": False,
        "rows_prov": len(df_prov),
        "issuers": len(sc_zt),
        "kpi": len(province_kpi),
        "agencies": len(agencies),
        "underwriters": len(underwriters),
        "ags": len(ags_df),
        "dqc_ms": round(dqc_ms, 2),
    }


def run_data_layer(warmup: int, reps: int) -> dict:
    C.header("测试 4 · 省份切换（① 数据层）")
    fy, snap = C.fiscal_year(), C.active_snapshot()
    provinces = C.available_provinces()
    C.info(f"可切换省份 {len(provinces)} 个：{provinces}")

    # 全量债券宽表在 App 里是缓存加载一次的，这里也一次性载入（计时之外），
    # 并单独记录这次「冷加载全国债券」的耗时作为参考。
    t_full_load = C.timeit(C.get_bond_df, warmup=0, reps=5)
    df_qg = C.get_bond_df()
    C.info(f"全国债券宽表：{len(df_qg):,} 行（冷加载中位数 {C.fmt_ms(t_full_load['median'])}）")

    per_province: dict[str, dict] = {}
    all_samples: list[float] = []
    for prov in provinces:
        stat = C.timeit(
            lambda p=prov: datalayer_switch(p, df_qg, fy, snap),
            warmup=warmup, reps=reps, before_each=C.clear_caches,
        )
        per_province[prov] = stat
        all_samples.extend(stat["all"])
        C.info(f"  {prov:<10} 中位 {C.fmt_ms(stat['median'])}  "
               f"P95 {C.fmt_ms(stat['p95'])}  最大 {C.fmt_ms(stat['max'])}")

    overall = C.summarize(all_samples)
    print("\n  ── 数据层汇总（全部省份 × 全部重复）──")
    print(f"    中位数 : {C.fmt_ms(overall['median'])}")
    print(f"    P95    : {C.fmt_ms(overall['p95'])}")
    print(f"    最大值 : {C.fmt_ms(overall['max'])}")

    return {
        "provinces": provinces,
        "full_bond_cold_load_ms": t_full_load,
        "overall": overall,
        "per_province": per_province,
        "config": {"warmup": warmup, "reps": reps},
    }


# ──────────────────────────────────────────────
# ② 端到端：streamlit.testing.v1.AppTest（尽力项）
# ──────────────────────────────────────────────
def _find_province_selectbox(at):
    try:
        cands = list(at.sidebar.selectbox)
    except Exception:
        cands = []
    if not cands:
        cands = list(at.selectbox)
    for sb in cands:
        try:
            opts = [str(o) for o in sb.options]
        except Exception:
            opts = []
        if len(opts) > 3 and any(o.endswith(("省", "市", "自治区")) for o in opts):
            return sb
    return cands[0] if cands else None


def _find_run_button(at):
    try:
        cands = list(at.sidebar.button)
    except Exception:
        cands = []
    if not cands:
        cands = list(at.button)
    for b in cands:
        if "开始分析" in str(getattr(b, "label", "")):
            return b
    return cands[0] if cands else None


def run_apptest(provinces: list[str]) -> dict:
    C.header("测试 4 · 省份切换（② 端到端 AppTest）")
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:  # pragma: no cover
        C.info(f"AppTest 不可用：{e!r}")
        return {"available": False, "reason": f"import 失败: {e!r}"}

    if not C.MAIN_PY.exists():
        return {"available": False, "reason": f"未找到 {C.MAIN_PY}"}

    result: dict = {"available": True, "ran": False}
    try:
        at = AppTest.from_file(str(C.MAIN_PY), default_timeout=180)
        t0 = time.perf_counter()
        at.run()  # 首次加载默认省份（冷启动，含全国数据加载 + 全部图构建）
        cold_ms = (time.perf_counter() - t0) * 1000.0
        if getattr(at, "exception", None):
            return {"available": True, "ran": False,
                    "reason": f"脚本运行抛出异常：{at.exception}",
                    "cold_run_ms": round(cold_ms, 2)}
        result["cold_run_ms"] = round(cold_ms, 2)
        C.info(f"首次冷启动全脚本执行：{C.fmt_ms(cold_ms)}")

        switches: dict[str, float] = {}
        for prov in provinces:
            try:
                # 每次 at.run() 都会重建 ElementTree，控件引用随即失效；
                # 必须在每次切换前重新定位下拉框与按钮，否则 set_value/click 会落空，
                # 导致除首省外的所有「切换」都变成对同一省的重复空转。
                sb = _find_province_selectbox(at)
                btn = _find_run_button(at)
                if sb is None or btn is None:
                    C.info(f"  {prov}: 未定位到省份下拉框或「开始分析」按钮，跳过")
                    continue
                sb.set_value(prov)
                btn.click()
                t0 = time.perf_counter()
                at.run()
                dt = (time.perf_counter() - t0) * 1000.0
                if getattr(at, "exception", None):
                    C.info(f"  {prov}: 运行异常，跳过（{at.exception}）")
                    continue
                switches[prov] = round(dt, 2)
                C.info(f"  {prov:<10} 切换端到端（全脚本）: {C.fmt_ms(dt)}")
            except Exception as e:
                C.info(f"  {prov}: 切换失败 {e!r}")
        if not switches:
            result["ran"] = False
            result["reason"] = "所有省份切换均未成功执行"
            return result

        samples = list(switches.values())
        result.update({
            "ran": True,
            "per_province_ms": switches,
            "summary": C.summarize(samples),
            "note": "AppTest 测的是服务端脚本执行耗时（数据层 + Python 侧 plotly 图构建），"
                    "不含浏览器 JS 绘制；每省 1 次（AppTest 开销较大，未做多次重复）。",
        })
        print("\n  ── AppTest 端到端汇总（切换，全脚本执行）──")
        s = result["summary"]
        print(f"    中位数 : {C.fmt_ms(s['median'])}")
        print(f"    P95    : {C.fmt_ms(s['p95'])}")
        print(f"    最大值 : {C.fmt_ms(s['max'])}")
        return result
    except Exception as e:  # pragma: no cover
        return {"available": True, "ran": False, "reason": f"AppTest 运行失败：{e!r}"}


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def run(warmup: int = 1, reps: int = 10, skip_apptest: bool = False) -> dict:
    data_layer = run_data_layer(warmup, reps)
    provinces = data_layer["provinces"]
    if skip_apptest:
        apptest = {"available": False, "reason": "已通过 --skip-apptest 跳过"}
    else:
        apptest = run_apptest(provinces)

    result = {
        "test": "province_switch",
        "data_layer": data_layer,
        "end_to_end_apptest": apptest,
        "browser_render": {
            "measured": False,
            "reason": "项目未安装 playwright，无法测量真实浏览器渲染/JS 绘制耗时；不编造数字。",
        },
        "env": C.env_info(),
    }
    C.save_json("bench4.json", result)
    C.info("结果已写入 benchmark/results/bench4.json")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试4：省份切换响应基准")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--skip-apptest", action="store_true", help="跳过 AppTest 端到端，仅测数据层")
    args = parser.parse_args()
    run(warmup=args.warmup, reps=args.reps, skip_apptest=args.skip_apptest)
