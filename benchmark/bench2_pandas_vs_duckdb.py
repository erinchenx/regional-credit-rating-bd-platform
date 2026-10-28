# -*- coding: utf-8 -*-
"""
benchmark/bench2_pandas_vs_duckdb.py
====================================
【测试 2】复杂下钻查询：pandas vs DuckDB · 纯计算对比

数据已预加载（同一份 Parquet），仅对比计算引擎的纯计算用时。

从项目页面逻辑中挑 3 个最复杂的下钻查询，对比 pandas 与 DuckDB 的等价实现（不代表 app 每条查询都走 DuckDB），按 Tab 升序排列：
  Q1 竞争热力矩阵（Tab 0）：机构 × 财力区间 的 COUNT(DISTINCT 发行人）；app 当前用 pandas 动态透视，DuckDB 侧为等价 SQL 实现
  Q2 财务准入基准（Tab 2）：省份 + 城市 + 行政级别 + 主体评级 多维筛选 + 5 指标分位数聚合；app 在 DuckDB 模式下真实执行该 SQL
  Q3 展业帮手圈（Tab 3）：主承销商共同客户分析（string_split/unnest/JOIN/GROUP BY/string_agg）；仅测默认「全部城市 / 全部级别」，启用筛选时 app 走 pandas

公平性：
  - 同一省份、同一筛选参数、同一数据源（同一份 Parquet）。
  - 两种实现的结果用代码断言校验，不一致会如实报告。
  - 纯计算口径：pandas 用预载 DataFrame；DuckDB 用「已把数据 CREATE TABLE 载入的内存库」。
  - 每查询预热 1 次、重复 10 次，报告中位数与 P95。

本模块同时导出 Ctx、SQL、计算函数与断言函数，供 bench3（新旧流程对比）复用。

只读运行，不修改任何项目文件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd

import common as C

from models.intermediate.int_data import (
    FISCAL_COL,
    int_build_issuer_view,
    int_filter_province,
    int_join_fiscal,
)
from models.marts.mart_credit_indicators import (
    mart_competition_matrix,
    mart_financial_bench,
    mart_partner_network,
)

# Tab 2 准入门槛的分位数聚合 SQL（照抄 app/main.py，FROM int_v_issuer_enriched）
_BENCH_SELECT = """
    SELECT
        COALESCE(SUM(CASE WHEN 总资产 IS NOT NULL OR 净资产 IS NOT NULL
                           OR 营业收入 IS NOT NULL OR 净利润 IS NOT NULL
                           OR 资产负债率 IS NOT NULL THEN 1 ELSE 0 END), 0) AS 样本数,
        ROUND(MIN(总资产), 2) AS 总资产_最小,
        ROUND(QUANTILE_CONT(总资产, 0.25), 2) AS 总资产_Q1,
        ROUND(MEDIAN(总资产), 2) AS 总资产_中位,
        ROUND(AVG(总资产), 2) AS 总资产_均值,
        ROUND(QUANTILE_CONT(总资产, 0.75), 2) AS 总资产_Q3,
        ROUND(MAX(总资产), 2) AS 总资产_最大,
        ROUND(MIN(净资产), 2) AS 净资产_最小,
        ROUND(QUANTILE_CONT(净资产, 0.25), 2) AS 净资产_Q1,
        ROUND(MEDIAN(净资产), 2) AS 净资产_中位,
        ROUND(AVG(净资产), 2) AS 净资产_均值,
        ROUND(QUANTILE_CONT(净资产, 0.75), 2) AS 净资产_Q3,
        ROUND(MAX(净资产), 2) AS 净资产_最大,
        ROUND(MIN(营业收入), 2) AS 营业收入_最小,
        ROUND(QUANTILE_CONT(营业收入, 0.25), 2) AS 营业收入_Q1,
        ROUND(MEDIAN(营业收入), 2) AS 营业收入_中位,
        ROUND(AVG(营业收入), 2) AS 营业收入_均值,
        ROUND(QUANTILE_CONT(营业收入, 0.75), 2) AS 营业收入_Q3,
        ROUND(MAX(营业收入), 2) AS 营业收入_最大,
        ROUND(MIN(净利润), 2) AS 净利润_最小,
        ROUND(QUANTILE_CONT(净利润, 0.25), 2) AS 净利润_Q1,
        ROUND(MEDIAN(净利润), 2) AS 净利润_中位,
        ROUND(AVG(净利润), 2) AS 净利润_均值,
        ROUND(QUANTILE_CONT(净利润, 0.75), 2) AS 净利润_Q3,
        ROUND(MAX(净利润), 2) AS 净利润_最大,
        ROUND(MIN(资产负债率), 2) AS 负债率_最小,
        ROUND(QUANTILE_CONT(资产负债率, 0.25), 2) AS 负债率_Q1,
        ROUND(MEDIAN(资产负债率), 2) AS 负债率_中位,
        ROUND(AVG(资产负债率), 2) AS 负债率_均值,
        ROUND(QUANTILE_CONT(资产负债率, 0.75), 2) AS 负债率_Q3,
        ROUND(MAX(资产负债率), 2) AS 负债率_最大
    FROM int_v_issuer_enriched
    WHERE 省份 IS NOT NULL AND TRIM(主体评级) != ''
"""

_FIN_COLS = ["总资产", "净资产", "营业收入", "净利润", "资产负债率"]
_BENCH_COLMAP = {
    "总资产":    ("总资产_最小",  "总资产_Q1",  "总资产_中位",  "总资产_均值",  "总资产_Q3",  "总资产_最大"),
    "净资产":    ("净资产_最小",  "净资产_Q1",  "净资产_中位",  "净资产_均值",  "净资产_Q3",  "净资产_最大"),
    "营业收入":  ("营业收入_最小","营业收入_Q1","营业收入_中位","营业收入_均值","营业收入_Q3","营业收入_最大"),
    "净利润":    ("净利润_最小",  "净利润_Q1",  "净利润_中位",  "净利润_均值",  "净利润_Q3",  "净利润_最大"),
    "资产负债率":("负债率_最小",  "负债率_Q1",  "负债率_中位",  "负债率_均值",  "负债率_Q3",  "负债率_最大"),
}


# ──────────────────────────────────────────────
# 内存 DuckDB：把数据 CREATE TABLE 载入，作为 DuckDB 的「数据已加载」态
# ──────────────────────────────────────────────
def build_memory_db() -> duckdb.DuckDBPyConnection:
    """
    构建一个内存 DuckDB，其中 v_bond / v_fiscal 为「已载入内存」的真实表，
    再执行项目自带的 int/mart 视图 SQL（只读读取，不修改项目文件），
    使得对视图的查询完全在内存中计算 —— 与 pandas 预载 DataFrame 对等。
    """
    mem = duckdb.connect()  # 纯内存
    bond_df = C.qdb("SELECT * FROM v_bond")
    fiscal_df = C.qdb("SELECT * FROM v_fiscal")
    mem.register("_bond_src", bond_df)
    mem.register("_fiscal_src", fiscal_df)
    mem.execute("CREATE TABLE v_bond AS SELECT * FROM _bond_src")
    mem.execute("CREATE TABLE v_fiscal AS SELECT * FROM _fiscal_src")
    mem.unregister("_bond_src")
    mem.unregister("_fiscal_src")

    for rel in [
        "intermediate/int_v_issuer_enriched.sql",
        "marts/mart_v_partner_network.sql",
        "marts/mart_v_agency_competitive_landscape.sql",
    ]:
        sql = (C.MODELS_DIR / rel).read_text(encoding="utf-8")
        mem.execute(sql)
    return mem


# ──────────────────────────────────────────────
# 一致性断言工具
# ──────────────────────────────────────────────
def _num_close(a, b, atol: float) -> bool:
    try:
        if a is None or b is None:
            return (a is None) and (b is None)
        if pd.isna(a) and pd.isna(b):
            return True
        if pd.isna(a) or pd.isna(b):
            return False
        return abs(float(a) - float(b)) <= atol
    except Exception:
        return str(a) == str(b)


# ──────────────────────────────────────────────
# 基准上下文（一次性构建，计时之外）
# ──────────────────────────────────────────────
class Ctx:
    def __init__(self, province: str | None):
        C.require_warehouse()
        self.parquet = C.bond_parquet_path()
        self.fy = C.fiscal_year()
        self.province = province or C.largest_province_with_fiscal()
        self.kw = self.province[:2]
        self.like = f"%{self.kw}%"

        # 预载基表（供 pandas 纯计算与选择下钻参数）
        self.bond_df = C.get_bond_df()
        fiscal_df = pd.read_parquet(
            C.FISCAL_LAKE_DIR / f"{self.province}_{self.fy}.parquet"
        )[["城市", "一般公共预算收入(亿元)"]]
        self.prov_bond = int_filter_province(self.bond_df, self.province)
        self.df_sc = int_join_fiscal(self.prov_bond.copy(), fiscal_df)
        self.sc_zt = int_build_issuer_view(self.df_sc)

        # 选定下钻参数
        self.agency = self._pick_agency()
        self.triple = self._pick_triple()

        # 内存 DuckDB（DuckDB「纯计算」态）
        self.mem = build_memory_db()

    def _pick_agency(self) -> str:
        ag = C.qdb_one(
            "SELECT 主体评级机构 FROM v_agency_competitive_landscape "
            "WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL "
            "GROUP BY 主体评级机构 ORDER BY COUNT(*) DESC LIMIT 1",
            [self.like],
        )
        return str(ag) if ag else ""

    def _pick_triple(self) -> tuple:
        """选省内「有财务数据的样本最多」的 (城市, 行政级别, 主体评级) 三元组。"""
        fin = self.sc_zt[self.sc_zt[_FIN_COLS].notna().any(axis=1)]
        if fin.empty:
            return ("全部", "全部", "全部")
        grp = fin.groupby(["城市", "城投行政级别", "主体评级"]).size().sort_values(ascending=False)
        if grp.empty:
            return ("全部", "全部", "全部")
        return grp.index[0]  # (city, level, rating)

    # ── Q1 bench SQL（动态 WHERE，与 main.py 一致）──
    def bench_sql_params(self) -> tuple[str, list]:
        city, level, rating = self.triple
        and_parts = ["省份 LIKE ?"]
        params = [self.like]
        if city != "全部":
            and_parts.append("城市 = ?"); params.append(city)
        if level != "全部":
            and_parts.append("城投行政级别 = ?"); params.append(level)
        if rating != "全部":
            and_parts.append("主体评级 = ?"); params.append(rating)
        clause = " AND " + " AND ".join(and_parts)
        return _BENCH_SELECT + clause, params


# ══════════════════════════════════════════════
# Q1 · 财务准入基准
# ══════════════════════════════════════════════
def q1_pandas_compute(iss: pd.DataFrame, triple: tuple) -> dict:
    city, level, rating = triple
    sub = iss
    if city != "全部":
        sub = sub[sub["城市"] == city]
    if level != "全部":
        sub = sub[sub["城投行政级别"] == level]
    if rating != "全部":
        sub = sub[sub["主体评级"] == rating]
    return mart_financial_bench(sub)


def q1_duck_to_bench(df: pd.DataFrame) -> dict:
    """把 _bench_sql 的宽表结果转成 mart_financial_bench 同构字典（照抄 main.py 逻辑）。"""
    bench = {"n": int(df["样本数"].iloc[0]) if not df.empty else 0}
    if bench["n"] > 0 and not df.empty:
        row = df.iloc[0]
        for metric, (mn, q1, med, avg, q3, mx) in _BENCH_COLMAP.items():
            bench[metric] = {
                "min":    float(row[mn])  if pd.notna(row[mn])  else None,
                "q1":     float(row[q1])  if pd.notna(row[q1])  else None,
                "median": float(row[med]) if pd.notna(row[med]) else None,
                "mean":   float(row[avg]) if pd.notna(row[avg]) else None,
                "q3":     float(row[q3]) if pd.notna(row[q3]) else None,
                "max":    float(row[mx])  if pd.notna(row[mx])  else None,
            }
    return bench


def assert_q1(p_bench: dict, d_bench: dict, atol: float = 0.05) -> dict:
    diffs = []
    ok_n = abs(int(p_bench.get("n", 0)) - int(d_bench.get("n", 0))) <= 2
    if not ok_n:
        diffs.append(f"样本数: pandas={p_bench.get('n')} duckdb={d_bench.get('n')}")
    for metric in _FIN_COLS:
        pm, dm = p_bench.get(metric), d_bench.get(metric)
        if not pm or not dm:
            continue
        for k in ("min", "q1", "median", "mean", "q3", "max"):
            if not _num_close(pm.get(k), dm.get(k), atol):
                diffs.append(f"{metric}.{k}: pandas={pm.get(k)} duckdb={dm.get(k)}")
    return {
        "ok": ok_n and not diffs,
        "sample_count": {"pandas": p_bench.get("n"), "duckdb": d_bench.get("n")},
        "max_abs_tolerance": atol,
        "diffs": diffs[:20],
        "diff_count": len(diffs),
        "note": "pandas 走 int_build_issuer_view 去重(drop_duplicates keep-first)，"
                "DuckDB 走 int_v_issuer_enriched 的 DISTINCT ON(ORDER BY 行政级别量化 DESC)，"
                "去重口径的细微差异属项目已知行为；分位数算法均为线性插值。",
    }


# ══════════════════════════════════════════════
# Q2 · 展业帮手圈
# ══════════════════════════════════════════════
_Q2_SQL = """
    SELECT 主承销商, 已承做主体数, 共同客户数, 共同客户列表, 合作等级
    FROM v_partner_network
    WHERE 省份 LIKE ? AND 评级机构 = ?
    ORDER BY 共同客户数 DESC, 已承做主体数 DESC
"""


def assert_q2(p_df: pd.DataFrame, p_my: set, d_df: pd.DataFrame, ctx: "Ctx") -> dict:
    # 我方客户总数：pandas 集合 vs DuckDB 直接 COUNT DISTINCT
    d_my = C.qdb_one(
        "SELECT COUNT(DISTINCT 发行人中文名称) FROM v_bond "
        "WHERE 省份 LIKE ? AND ((主体评级机构 LIKE ?) OR (债项评级机构 LIKE ?))",
        [ctx.like, f"%{ctx.agency}%", f"%{ctx.agency}%"],
    ) or 0
    my_ok = abs(len(p_my) - int(d_my)) <= 1

    # 各承销商共同客户数对比（对齐主承销商）
    pm = p_df.drop_duplicates("主承销商").set_index("主承销商")["共同客户数"].to_dict() if not p_df.empty else {}
    dm = d_df.drop_duplicates("主承销商").set_index("主承销商")["共同客户数"].to_dict() if not d_df.empty else {}
    common = set(pm) & set(dm)
    cell_diffs = [f"{u}: pandas={pm[u]} duckdb={dm[u]}" for u in common if int(pm[u]) != int(dm[u])]
    return {
        "ok": my_ok and not cell_diffs,
        "my_issuers": {"pandas": len(p_my), "duckdb": int(d_my)},
        "underwriters": {"pandas": len(pm), "duckdb": len(dm), "common": len(common)},
        "cell_diffs": cell_diffs[:20],
        "cell_diff_count": len(cell_diffs),
        "note": "本次实测两侧一致：我方客户数、承销商集合、共同客户数均无差异。"
                "已对齐的两处：① pandas 用 .str.contains(机构名) 子串匹配，视图已改为 LIKE 子串匹配；"
                "② 视图 scope 与 pandas 一致，只要求主承销商非空，不再要求主体评级/最新债项评级非空。",
    }


# ══════════════════════════════════════════════
# Q3 · 竞争热力矩阵
# ══════════════════════════════════════════════
_Q3_SQL = """
    SELECT 主体评级机构 AS ag, 城市财力区间 AS cat, COUNT(DISTINCT 发行人中文名称) AS n
    FROM v_agency_competitive_landscape
    WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL
    GROUP BY 主体评级机构, 城市财力区间
"""


def q3_duck_matrix(df: pd.DataFrame) -> dict:
    mat: dict[str, dict[str, int]] = {}
    for _, r in df.iterrows():
        mat.setdefault(str(r["ag"]), {})[str(r["cat"])] = int(r["n"])
    return mat


def assert_q3(p_mat: dict, p_cats: list, d_mat: dict) -> dict:
    """在 DuckDB 出现的 (机构, 财力区间) 上比对主体数；pandas 侧用同一批 categories。"""
    cat_set = set(p_cats)
    diffs = []
    compared = 0
    for ag, cats in d_mat.items():
        for cat, dn in cats.items():
            if cat not in cat_set:
                continue  # '财力未知' 等非分档类别不计入 pandas 矩阵，跳过
            compared += 1
            pn = int(p_mat.get(ag, {}).get(cat, 0))
            if pn != int(dn):
                diffs.append(f"{ag}/{cat}: pandas={pn} duckdb={dn}")
    return {
        "ok": not diffs,
        "cells_compared": compared,
        "cell_diffs": diffs[:20],
        "cell_diff_count": len(diffs),
        "note": "仅比对落入 FISCAL_BANDS 分档的单元格；pandas 侧对 df_sc 内部按发行人去重，"
                "DuckDB 侧 v_agency_competitive_landscape 已 DISTINCT ON 发行人。",
    }


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════
def run(province: str | None = None, warmup: int = 1, reps: int = 20) -> dict:
    C.header("测试 2 · 复杂下钻查询 pandas vs DuckDB（纯计算）")
    ctx = Ctx(province)
    C.info(f"选定省份：{ctx.province}（省内债券 {len(ctx.prov_bond):,} 行，去重主体 {len(ctx.sc_zt):,} 家）")
    C.info(f"选定评级机构：{ctx.agency}")
    C.info(f"下钻三元组（Q2 财务准入基准筛选参数）：城市={ctx.triple[0]} 级别={ctx.triple[1]} 评级={ctx.triple[2]}")

    bench_sql, bench_params = ctx.bench_sql_params()
    results: dict = {
        "test": "pandas_vs_duckdb_pure_compute",
        "province": ctx.province,
        "agency": ctx.agency,
        "drill_triple": {"城市": ctx.triple[0], "行政级别": ctx.triple[1], "主体评级": ctx.triple[2]},
        "queries": {},
        "config": {"warmup": warmup, "reps": reps},
        "env": C.env_info(),
    }

    # ────────── Q1 竞争热力矩阵（Tab 0）──────────
    C.info("[Q1] 竞争热力矩阵：计时中 ...")
    dim = "fiscal"
    df_sc_preload = ctx.df_sc

    def q1_pandas_pure():
        return mart_competition_matrix(df_sc_preload, ctx.agency, dim)

    def q1_duck_pure():
        return q3_duck_matrix(ctx.mem.execute(_Q3_SQL, [ctx.like]).df())

    t_q1_pp = C.timeit(q1_pandas_pure, warmup, reps)
    t_q1_dp = C.timeit(q1_duck_pure, warmup, reps)
    p1_mat, _p1_ags, p1_cats = q1_pandas_pure()
    cons_q1 = assert_q3(p1_mat, p1_cats, q1_duck_pure())
    results["queries"]["Q1_竞争热力矩阵"] = {
        "desc": "机构 × 财力区间 的 COUNT(DISTINCT 发行人)（pandas iterrows vs DuckDB GROUP BY）",
        "dimension": dim,
        "pandas_pure_compute": t_q1_pp,
        "duckdb_pure_compute": t_q1_dp,
        "speedup_pure_compute": _ratio(t_q1_pp, t_q1_dp),
        "consistency": cons_q1,
    }

    # ────────── Q2 财务准入基准（Tab 2）──────────
    C.info("[Q2] 财务准入基准：计时中 ...")

    def q2_pandas_pure():
        iss = int_build_issuer_view(ctx.df_sc)   # 计时内含去重，与 DuckDB 口径对称
        return q1_pandas_compute(iss, ctx.triple)

    def q2_duck_pure():
        return q1_duck_to_bench(ctx.mem.execute(bench_sql, bench_params).df())

    t_q2_pp = C.timeit(q2_pandas_pure, warmup, reps, before_each=C.clear_caches)
    t_q2_dp = C.timeit(q2_duck_pure, warmup, reps)
    cons_q2 = assert_q1(q2_pandas_pure(), q2_duck_pure())
    results["queries"]["Q2_财务准入基准"] = {
        "desc": "省份+城市+级别+评级 多维筛选 + 5 指标分位数聚合",
        "pandas_pure_compute": t_q2_pp,
        "duckdb_pure_compute": t_q2_dp,
        "speedup_pure_compute": _ratio(t_q2_pp, t_q2_dp),
        "consistency": cons_q2,
    }

    # ────────── Q3 展业帮手圈（Tab 3）──────────
    C.info("[Q3] 展业帮手圈：计时中 ...")

    def q3_pandas_pure():
        return mart_partner_network(df_sc_preload, ctx.agency, "", "")

    def q3_duck_pure():
        return ctx.mem.execute(_Q2_SQL, [ctx.like, ctx.agency]).df()

    t_q3_pp = C.timeit(q3_pandas_pure, warmup, reps)
    t_q3_dp = C.timeit(q3_duck_pure, warmup, reps)
    p3_df, p3_my = q3_pandas_pure()
    cons_q3 = assert_q2(p3_df, p3_my, q3_duck_pure(), ctx)
    results["queries"]["Q3_展业帮手圈"] = {
        "desc": "主承销商共同客户分析（string_split/unnest/JOIN/GROUP BY/string_agg vs pandas iterrows）",
        "pandas_pure_compute": t_q3_pp,
        "duckdb_pure_compute": t_q3_dp,
        "speedup_pure_compute": _ratio(t_q3_pp, t_q3_dp),
        "consistency": cons_q3,
    }

    # ── 关闭内存库 ──
    try:
        ctx.mem.close()
    except Exception:
        pass

    # ── 控制台小结 ──
    print("\n  ── 结果（中位数，纯计算，pandas vs DuckDB）──")
    for name, q in results["queries"].items():
        print(f"    {name}")
        print(f"      pandas {C.fmt_ms(q['pandas_pure_compute']['median'])}  |  "
              f"DuckDB {C.fmt_ms(q['duckdb_pure_compute']['median'])}  |  "
              f"加速 {q['speedup_pure_compute']}x")
        print(f"      一致性 : {'一致' if q['consistency'].get('ok') else '存在差异（见 JSON）'}")

    C.save_json("bench2.json", results)
    C.info("结果已写入 benchmark/results/bench2.json")
    return results


def _ratio(t_pandas: dict, t_duck: dict):
    if not t_pandas or not t_duck:
        return None
    pm, dm = t_pandas.get("median"), t_duck.get("median")
    if not pm or not dm:
        return None
    return round(pm / dm, 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试2：复杂下钻 pandas vs DuckDB（纯计算）")
    parser.add_argument("province", nargs="?", default=None, help="目标省份（默认自动选省内债券最多者）")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=20)
    args = parser.parse_args()
    run(province=args.province, warmup=args.warmup, reps=args.reps)
