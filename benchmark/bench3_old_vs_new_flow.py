# -*- coding: utf-8 -*-
"""
benchmark/bench3_old_vs_new_flow.py
===================================
【测试 3】单查询新旧流程对比（读取+处理）：旧流程（pandas + Excel） vs 新流程（DuckDB + Parquet）

目标：对「完成同一分析任务」的总耗时做整体对比，而非单一因素对比。
      沿用测试 2 的 Q1 / Q2 / Q3 三个下钻查询与**完全相同的筛选参数**
      （同省份、同评级机构、同下钻三元组，均由测试 2 的 Ctx 自动选定）。

口径提醒（重要）：
  - 本测试比较的是「Excel + pandas 旧技术栈」与「Parquet + DuckDB 新技术栈」的等价实现，
    不代表 app 每条查询都走 DuckDB。
  - Q1 在 app 中仍由 pandas 动态透视完成；本测试的 DuckDB 侧是等价 SQL。
  - Q3 只覆盖默认「全部城市 / 全部级别」；选择具体城市/级别时 app 走 pandas。
  - Q2 是 app 在 DuckDB 模式下的真实路径。

两条流程：
  · 旧流程（pandas + Excel）：从项目**原始 Excel** 出发，走项目自带的
    「Excel 降级读取路径」——`stg_load_bond_data` / `stg_load_fiscal_data`
    （read_excel + 列名归一化 + 评级机构归并 + DQC），再用 pandas 完成
    与测试 2 相同的过滤 / 关联 / 去重 / 聚合。
      → 拆开记录：读取耗时（Excel 载入 + staging 清洗）、处理耗时（pandas 分析管线）、
        总耗时（= 读取 + 处理，逐次相加后再统计）。
  · 新流程（DuckDB + Parquet）：DuckDB 只读连接服务文件（视图指向 Parquet 数据湖），
    直接执行与测试 2 相同的 SQL。
      → 只记录总耗时（连接 + 查询 + 转 DataFrame，即端到端）。

公平性与诚实性：
  - 同一省份、同一筛选参数、同一份底层数据（Excel 与 Parquet 同源）。
  - 结果一致性沿用测试 2 的容差规则与断言函数（assert_q1/q2/q3），有差异如实报告。
  - 每轮计时前清 @st.cache_data 缓存，避免缓存命中掩盖真实耗时。
  - Excel 读取极慢，旧流程默认重复 5 次；新流程（很快）默认重复 10 次。
  - 报告中位数 / 最小 / 最大 / P95，并给出总耗时倍数、读取与处理各占比。

只读运行，不修改任何项目文件；结果写入 benchmark/results/bench3.json。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import common as C

# 复用测试 2 的参数选定、DuckDB SQL、pandas 计算与一致性断言，保证「同参数、同口径」
import bench2_pandas_vs_duckdb as B2

from models.staging.stg_data import stg_load_bond_data, stg_load_fiscal_data
from models.intermediate.int_data import (
    int_build_issuer_view,
    int_filter_province,
    int_join_fiscal,
)
from models.marts.mart_credit_indicators import (
    mart_competition_matrix,
    mart_partner_network,
    dqc_marts_platform_dismatch,
)


# ──────────────────────────────────────────────
# 原始 Excel 定位（复刻 app/main.py 的降级路径取数方式）
# ──────────────────────────────────────────────
def _latest_excel(directory: Path) -> Path:
    """返回目录下修改时间最新的 .xlsx（与 main.py 的 _latest_excel 一致）。"""
    files = list(directory.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"目录 {directory} 下没有找到任何 xlsx 文件")
    return max(files, key=lambda p: p.stat().st_mtime)


def _bond_excel_for_snapshot(snapshot: str) -> Path:
    """返回与激活快照同源的债券 Excel（文件名含相同 8 位日期）。

    新流程读的是 active_version.json 里激活快照对应的 parquet；旧流程必须读同一份源 Excel，
    否则「新旧流程加速比」会建立在两份不同数据上。找不到同源 Excel 时回退 mtime 最新并告警。
    """
    if not C.RAW_BOND_DIR.exists():
        raise FileNotFoundError(f"目录 {C.RAW_BOND_DIR} 不存在")
    for p in C.RAW_BOND_DIR.glob("*.xlsx"):
        m = re.search(r"20\d{6}", p.name)
        if m and m.group() == snapshot:
            return p
    latest = _latest_excel(C.RAW_BOND_DIR)
    C.warn(f"未找到与激活快照 {snapshot} 同源的 Excel（文件名含 {snapshot}），"
           f"回退到 mtime 最新 {latest.name}；新旧流程可能读不同数据，加速比仅供参考。")
    return latest


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def _summarize_pair(read_t: dict, proc_t: dict) -> dict:
    """把「读取」与「处理」两组样本逐次相加，再统计总耗时分布（不额外重读 Excel）。"""
    ra, pa = read_t.get("all", []), proc_t.get("all", [])
    n = min(len(ra), len(pa))
    if n == 0:
        return C.summarize([])
    return C.summarize([ra[i] + pa[i] for i in range(n)])


def _ratio(a: dict, b: dict):
    am, bm = a.get("median"), b.get("median")
    if not am or not bm:
        return None
    return round(am / bm, 2)


def _share(part: dict, whole: dict) -> float | None:
    pm, wm = part.get("median"), whole.get("median")
    if not wm:
        return None
    return round((pm or 0.0) / wm * 100.0, 1)


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def run(province: str | None = None, warmup: int = 1, reps: int = 10, new_reps: int = 20) -> dict:
    C.require_warehouse()
    C.header("测试 3 · 单查询新旧流程对比（Excel+pandas vs DuckDB+Parquet）")

    # ── 用测试 2 的 Ctx 选定「同参数」（省份 / 评级机构 / 下钻三元组）──
    ctx = B2.Ctx(province)
    prov_name = ctx.province
    agency = ctx.agency
    triple = ctx.triple
    like = ctx.like
    bench_sql, bench_params = ctx.bench_sql_params()
    # 测试 3 的新流程直连服务文件（C.qdb），不需要 Ctx 的内存库，尽早释放
    try:
        ctx.mem.close()
    except Exception:
        pass

    C.info(f"选定省份：{prov_name}　评级机构：{agency}")
    C.info(f"下钻三元组（Q2 财务准入基准筛选参数）：城市={triple[0]} 级别={triple[1]} 评级={triple[2]}")

    # ── 旧流程数据源：原始 Excel（bond 全国表 + 该省财力表）──
    bond_snap = C.active_snapshot()
    bond_xlsx = _bond_excel_for_snapshot(bond_snap)
    _m = re.search(r"20\d{6}", bond_xlsx.name)
    src_consistent = bool(_m and _m.group() == bond_snap)
    fy = C.fiscal_year()
    fiscal_xlsx = C.RAW_FISCAL_DIR / f"{fy}年{prov_name}财力.xlsx"
    bond_mtime = _mtime(bond_xlsx)
    fiscal_mtime = _mtime(fiscal_xlsx)
    C.info(f"旧流程债券 Excel：{bond_xlsx.name}（{C.fmt_bytes(bond_xlsx.stat().st_size)}）")
    C.info(f"旧流程财力 Excel：{fiscal_xlsx.name if fiscal_xlsx.exists() else '（缺失，按空财力处理）'}")

    # ── 预载一次 Excel 数据（计时之外），供「处理耗时」重复使用 ──
    C.clear_caches()
    bond_xl = stg_load_bond_data(bond_xlsx, _mtime=bond_mtime)  # db_path=None → 走 Excel 降级路径
    fiscal_xl = stg_load_fiscal_data(C.RAW_FISCAL_DIR, fy, prov_name, _mtime=fiscal_mtime)
    C.info(f"Excel 载入完成：债券 {len(bond_xl):,} 行，财力 {len(fiscal_xl):,} 行（预载，用于处理计时）")

    results: dict = {
        "test": "old_vs_new_flow",
        "province": prov_name,
        "agency": agency,
        "drill_triple": {"城市": triple[0], "行政级别": triple[1], "主体评级": triple[2]},
        "old_flow": {
            "engine": "pandas",
            "source": "项目自带的 Excel 降级读取路径 stg_load_*（read_excel + 列名归一化 + 评级归并 + DQC）",
            "bond_excel": bond_xlsx.name,
            "bond_excel_bytes": bond_xlsx.stat().st_size,
            "fiscal_excel": fiscal_xlsx.name if fiscal_xlsx.exists() else None,
            "source_consistent": src_consistent,
            "active_snapshot": bond_snap,
        },
        "new_flow": {
            "engine": "DuckDB",
            "source": "只读连接 credit_indicators.duckdb（视图指向 Parquet 数据湖），直接执行 SQL",
        },
        "queries": {},
        "config": {"warmup": warmup, "reps": reps, "new_reps": new_reps},
        "env": C.env_info(),
    }

    # ── 旧流程读取函数（Excel，昂贵）──
    def read_bond_fiscal():
        b = stg_load_bond_data(bond_xlsx, _mtime=bond_mtime)
        f = stg_load_fiscal_data(C.RAW_FISCAL_DIR, fy, prov_name, _mtime=fiscal_mtime)
        return b, f

    def read_bond_only():
        return stg_load_bond_data(bond_xlsx, _mtime=bond_mtime)

    def _record(name: str, desc: str, read_t: dict, proc_t: dict, new_t: dict, cons: dict,
                dqc_t: dict | None = None) -> None:
        total_t = _summarize_pair(read_t, proc_t)
        results["queries"][name] = {
            "desc": desc,
            "old_read": read_t,
            "old_process": proc_t,
            "old_total": total_t,
            "old_dqc": dqc_t or C.summarize([]),
            "new_total": new_t,
            "speedup_total": _ratio(total_t, new_t),
            "read_share_pct": _share(read_t, total_t),
            "process_share_pct": _share(proc_t, total_t),
            "consistency": cons,
        }

    # ══════════════ Q1 · 竞争热力矩阵（Tab 0）══════════════
    C.info("[Q1] 竞争热力矩阵：旧流程读 Excel + pandas(iterrows) vs 新流程 DuckDB(GROUP BY) ...")
    dim = "fiscal"

    def q1_process():
        prov = int_filter_province(bond_xl, prov_name)
        joined = int_join_fiscal(prov.copy(), fiscal_xl)
        mat, ags, cats = mart_competition_matrix(joined, agency, dim)
        return mat, joined, cats

    def q1_new():
        return B2.q3_duck_matrix(C.qdb(B2._Q3_SQL, [like]))

    q1_read_t = C.timeit(read_bond_fiscal, warmup, reps, before_each=C.clear_caches)
    q1_proc_t = C.timeit(lambda: q1_process()[0], warmup, reps, before_each=C.clear_caches)
    q1_dqc_t = C.timeit(lambda: dqc_marts_platform_dismatch(q1_process()[1], prov_name), warmup, reps)
    q1_new_t = C.timeit(q1_new, warmup, new_reps)
    C.clear_caches()
    q1_mat, q1_joined, q1_cats = q1_process()
    q1_cons = B2.assert_q3(q1_mat, q1_cats, q1_new())
    _record("Q1_竞争热力矩阵",
            "机构 × 财力区间 的 COUNT(DISTINCT 发行人)（旧流程含 Excel 读取与财力关联）",
            q1_read_t, q1_proc_t, q1_new_t, q1_cons, q1_dqc_t)

    # ══════════════ Q2 · 财务准入基准（Tab 2）══════════════
    C.info("[Q2] 财务准入基准：旧流程读 Excel + pandas 分位聚合 vs 新流程 DuckDB SQL ...")

    def q2_process():
        prov = int_filter_province(bond_xl, prov_name)
        joined = int_join_fiscal(prov.copy(), fiscal_xl)
        iss = int_build_issuer_view(joined)
        return B2.q1_pandas_compute(iss, triple), iss

    def q2_new():
        return B2.q1_duck_to_bench(C.qdb(bench_sql, bench_params))

    q2_read_t = C.timeit(read_bond_fiscal, warmup, reps, before_each=C.clear_caches)
    q2_proc_t = C.timeit(lambda: q2_process()[0], warmup, reps, before_each=C.clear_caches)
    q2_dqc_t = C.timeit(lambda: dqc_marts_platform_dismatch(q2_process()[1], prov_name), warmup, reps)
    q2_new_t = C.timeit(q2_new, warmup, new_reps)
    C.clear_caches()
    q2_result, q2_iss = q2_process()
    q2_cons = B2.assert_q1(q2_result, q2_new())
    _record("Q2_财务准入基准",
            "省份+城市+级别+评级 多维筛选 + 5 指标分位数聚合（旧流程含 Excel 读取与 staging 清洗）",
            q2_read_t, q2_proc_t, q2_new_t, q2_cons, q2_dqc_t)

    # ══════════════ Q3 · 展业帮手圈（Tab 3）══════════════
    C.info("[Q3] 展业帮手圈：旧流程读 Excel + pandas(iterrows) vs 新流程 DuckDB(string_agg) ...")

    def q3_process():
        prov = int_filter_province(bond_xl, prov_name)
        mart_df = int_join_fiscal(prov.copy(), fiscal_xl)
        return mart_partner_network(mart_df, agency, "", ""), mart_df

    def q3_new():
        return C.qdb(B2._Q2_SQL, [like, agency])

    q3_read_t = C.timeit(read_bond_only, warmup, reps, before_each=C.clear_caches)
    q3_proc_t = C.timeit(lambda: q3_process()[0], warmup, reps, before_each=C.clear_caches)
    q3_dqc_t = C.timeit(lambda: dqc_marts_platform_dismatch(q3_process()[1], prov_name), warmup, reps)
    q3_new_t = C.timeit(q3_new, warmup, new_reps)
    C.clear_caches()
    (q3_old_df, q3_old_my), q3_mart = q3_process()
    q3_cons = B2.assert_q2(q3_old_df, q3_old_my, q3_new(), ctx)
    _record("Q3_展业帮手圈",
            "主承销商共同客户分析（旧流程仅需债券 Excel；pandas iterrows vs DuckDB unnest/string_agg）",
            q3_read_t, q3_proc_t, q3_new_t, q3_cons, q3_dqc_t)

    # ── 控制台小结 ──
    print("\n  ── 结果（中位数：旧流程 vs 新流程）──")
    for name, q in results["queries"].items():
        print(f"    {name}")
        print(f"      旧流程 读取 {C.fmt_ms(q['old_read']['median'])}  +  处理 {C.fmt_ms(q['old_process']['median'])}"
              f"  +  DQC {C.fmt_ms(q['old_dqc']['median'])}"
              f"  =  总 {C.fmt_ms(q['old_total']['median'])}"
              f"（读取占 {q['read_share_pct']}% / 处理占 {q['process_share_pct']}%）")
        print(f"      新流程 总 {C.fmt_ms(q['new_total']['median'])}"
              f"  →  总耗时倍数 {q['speedup_total']}×")
        print(f"      一致性：{'一致' if q['consistency'].get('ok') else '存在差异（见 JSON）'}")

    C.save_json("bench3.json", results)
    C.info("结果已写入 benchmark/results/bench3.json")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试3：单查询新旧流程对比（Excel+pandas vs DuckDB+Parquet）")
    parser.add_argument("province", nargs="?", default=None, help="目标省份（默认与测试2一致，自动选省内债券最多者）")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=10, help="旧流程（含 Excel 读取）重复次数，默认 10")
    parser.add_argument("--new-reps", type=int, default=20, help="新流程（DuckDB）重复次数，默认 20")
    args = parser.parse_args()
    run(province=args.province, warmup=args.warmup, reps=args.reps, new_reps=args.new_reps)
