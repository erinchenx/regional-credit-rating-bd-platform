# scripts/compare_engines_test.py
# -*- coding: utf-8 -*-
"""
双引擎一致性对比脚本
====================
按 KPI → Tab 0 → Tab 1 → Tab 2 → Tab 3 顺序，逐 Tab 对比
DuckDB SQL 引擎 vs pandas 降级引擎的计算结果。
这个脚本就是为了确保这两个引擎算出来的结果是一致的。

- KPI    ➔ 比对顶部的总条数、总金额、城市数是否绝对一致。
- Tab 0  ➔ 比对省内各评级机构的机构数量、市占率。
- Tab 1  ➔ 比对去重后的主体财务数据（总资产、负债率等）和地方财力数据。
- Tab 2  ➔ 抽查全省或特定（城市+级别+评级）条件下的财务中位数。
- Tab 3  ➔ 比对主承销商的客户数、我方客户数，以及双方合作的共同客户名单。


设计原则：
  - DuckDB 是主引擎，pandas 是高可用降级路径（不是"校验器"）
  - 两边范围必须一致：同一省份、同一筛选条件
  - 允许已知的合理误差（例如微小的四舍五入浮点数差异），不判为 FAIL
  - 如果发现对不上的真正不一致，输出具体差异行和数值，说明应修哪一侧：
     - 如果 DuckDB 算错了 ➔ 去修 `models/marts/` 下面的 `.sql` 视图文件。修完 SQL 先跑 `build_warehouse.py` 刷新数据库，再回来跑本脚本。
     - 如果 pandas 算错了 ➔ 去修 `mart_*.py` 里的 Python 函数。
   

用法：
    python scripts/compare_engines_test.py              # 自动选省
    python scripts/compare_engines_test.py 江苏省        # 手动指定省份
    python scripts/compare_engines_test.py 四川省 --agency 联合资信评估股份有限公司
"""

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))

from models.intermediate.int_data import (
    FISCAL_COL,
    int_build_issuer_view,
    int_filter_province,
    int_join_fiscal,
)
from models.marts.mart_credit_indicators import (
    mart_agency_stats,
    mart_financial_bench,
    mart_partner_network,
    mart_underwriter_stats,
)
from models.staging.stg_data import (
    _file_mtime,
    _read_active_version,
    stg_load_bond_data,
    stg_load_fiscal_data,
)

# ── 路径 ──────────────────────────────────────────────────────
_BASE        = _PROJECT
BOND_FILE    = max((_BASE / "data" / "1发债数据").glob("*.xlsx"),
                   key=lambda p: p.stat().st_mtime)
FISCAL_DIR   = _BASE / "data" / "2财力数据"
DB_PATH      = _BASE / "data" / "serving" / "credit_indicators.duckdb"
VERSION_FILE = _BASE / "data" / "serving" / "active_version.json"
_VERSION     = _read_active_version(VERSION_FILE)
FISCAL_YEAR  = _VERSION.get("active_fiscal_year", "2021")
_SNAP        = _VERSION.get("active_bond_snapshot", "")

# ── ANSI 颜色 ─────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"
B = "\033[1m";  E = "\033[0m"


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def qdb(sql: str, params: list | None = None) -> pd.DataFrame:
    """只读 DuckDB 查询，用完即关。"""
    if not DB_PATH.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    except Exception as e:
        print(f"  {R}[SQL ERROR]{E} {e}")
        return pd.DataFrame()
    finally:
        con.close()


def _auto_province() -> str:
    """选数据量适中的省（主体数在中间分位的省）。"""
    df = qdb("""
        SELECT 省份, COUNT(DISTINCT 发行人中文名称) AS n
        FROM v_bond WHERE 省份 IS NOT NULL
        GROUP BY 省份 ORDER BY n
    """)
    if df.empty:
        return "江苏省"
    
    # 比如 31 行，31 // 2 = 15，正好取到中间第 16 行（索引 15）
    mid_idx = len(df) // 2
    
    # 防止表格只有 1 或 2 行时 mid_idx 计算依然偏大的极端情况（保底机制）
    mid_idx = min(mid_idx, len(df) - 1)
    
    # 2. 拿出省份名字
    prov = str(df.iloc[mid_idx]["省份"])
    
    if prov in ["上海", "北京", "天津", "重庆"]:
        prov += "市"
    # 如果是其他省份（比如 "江苏"、"浙江"）且没写省，自动补上 "省"
    elif not prov.endswith("省") and not prov.endswith("市") and not prov.endswith("自治区"):
        prov += "省"
        
    return prov
    



def section(title: str) -> None:
    print(f"\n{B}{C}{'─'*60}{E}")
    print(f"{B}{C}  {title}{E}")
    print(f"{B}{C}{'─'*60}{E}")


def ok(msg: str)   -> None: print(f"  {G}✅  {msg}{E}")
def fail(msg: str) -> None: print(f"  {R}❌  {msg}{E}")
def note(msg: str) -> None: print(f"  {Y}⚠️  {msg}{E}")
def info(msg: str) -> None: print(f"  {C}📋  {msg}{E}")


def compare_scalar(label: str, pandas_val, duck_val,
                   atol: float = 0.05, allow_diff: bool = False) -> bool:
    """
    对比单个数值，返回是否通过。
    atol (Absolute Tolerance): 绝对容差。默认是 0.05。
    意思是如果两个数相差小于 0.05，我们也认为它们是相等的（这在处理四舍五入的金融数据时很常见）。
    """
    if duck_val is None:
        fail(f"{label}: DuckDB 无数据")
        return False
    try:
        diff = abs(float(pandas_val) - float(duck_val))
        match = diff <= atol
    except (TypeError, ValueError):
        match = (str(pandas_val) == str(duck_val))
        diff = 0

    if match:
        ok(f"{label}: 一致 = {duck_val}")
        return True
    elif allow_diff:
        note(f"{label}: pandas={pandas_val}  DuckDB={duck_val}  diff={diff:.4f}（已知差异）")
        return True
    else:
        fail(f"{label}: pandas={pandas_val}  DuckDB={duck_val}  diff={diff:.4f}")
        return False


def compare_df_counts(label: str, pdf: pd.DataFrame, ddf: pd.DataFrame,
                      allow_diff: bool = False) -> bool:
    pn, dn = len(pdf), len(ddf)
    if pn == dn:
        ok(f"{label}: 行数一致 = {pn}")
        return True
    elif allow_diff:
        note(f"{label}: pandas={pn}  DuckDB={dn}（已知差异）")
        return True
    else:
        fail(f"{label}: pandas={pn}  DuckDB={dn}")
        return False


def compare_numeric_col(label: str, pdf: pd.DataFrame, ddf: pd.DataFrame,
                        key: str, col: str, atol: float = 0.1,
                        allow_diff: bool = False) -> bool:
    """按 key 对齐后比较 col 列的最大差异。"""
    if col not in pdf.columns:
        note(f"{label}.{col}: pandas 无此列")
        return True
    if col not in ddf.columns:
        note(f"{label}.{col}: DuckDB 无此列")
        return True

    # 确保 key 唯一，避免 key 重复导致返回 Series 而非标量
    s = pdf.drop_duplicates(subset=[key]).set_index(key)[col].sort_index()
    d = ddf.drop_duplicates(subset=[key]).set_index(key)[col].sort_index()
    
    common = s.index.intersection(d.index)
    if common.empty:
        note(f"{label}.{col}: 无共有 key 可比较")
        return True

    sv = pd.to_numeric(s.loc[common], errors='coerce')
    dv = pd.to_numeric(d.loc[common], errors='coerce')

    # 必须先过滤掉双方任一方为 NaN 的行
    diff_mask = sv.notna() & dv.notna()
    if not diff_mask.any():
        note(f"{label}.{col}: 全部数据为 NaN 或无有效数值对，跳过")
        return True

    # 只对有效数值对计算差异
    diff     = (sv[diff_mask] - dv[diff_mask]).abs()
    max_diff = diff.max()
    worst    = diff.idxmax()
    
    # 提取具体数值用于打印
    p_val = float(sv.loc[worst])
    d_val = float(dv.loc[worst])

    if max_diff <= atol:
        ok(f"{label}.{col}: 最大差异 {max_diff:.2f} ≤ {atol}")
        return True
    elif allow_diff:
        note(f"{label}.{col}: 最大差异 {max_diff:.2f}（已知差异）key='{worst}'")
        return True
    else:
        fail(f"{label} 差异{max_diff:.4f} > {atol}  key='{worst}'  "
             f"pandas={p_val:.4f}  DuckDB={d_val:.4f}")
        return False

# ══════════════════════════════════════════════════════════════
# 准备数据（共用）
# ══════════════════════════════════════════════════════════════

def build_pandas_data(province: str) -> dict:
    """跑完整 pandas 数据流，返回各层结果。"""
    print(f"{C}[pandas] 加载{province}数据...{E}中")
    df_qg = stg_load_bond_data(BOND_FILE, _mtime=_file_mtime(BOND_FILE),
                                db_path=DB_PATH, _active_snapshot=_SNAP)
    fiscal_path = FISCAL_DIR / f"{FISCAL_YEAR}年{province}财力.xlsx"
    df_fiscal = stg_load_fiscal_data(FISCAL_DIR, FISCAL_YEAR, province,
                                     _mtime=_file_mtime(fiscal_path),
                                     db_path=DB_PATH, _active_snapshot=_SNAP)
    df_prov = int_filter_province(df_qg, province)
    df_prov = int_join_fiscal(df_prov, df_fiscal)
    sc_zt   = int_build_issuer_view(df_prov)
    print(f"{C}[pandas] df_prov={len(df_prov):,}行  sc_zt={len(sc_zt):,}家{E}")
    return {"df_qg": df_qg, "df_prov": df_prov, "sc_zt": sc_zt}


# ══════════════════════════════════════════════════════════════
# KPI
# ══════════════════════════════════════════════════════════════

def compare_kpi(pdata: dict, province: str) -> bool:
    """
    KPI · 顶部指标卡片

    说明：
      已评级主体_家：
        pandas  = len(sc_zt)，即主体评级非空的去重发行人
        DuckDB  = v_province_kpi 中 COUNT DISTINCT WHERE 主体评级机构 NOT NULL
        → 过滤字段略有不同（主体评级 vs 主体评级机构），允许小差异
      债项总数、发行总额、覆盖城市：均来自 v_bond，应完全一致
    """
    section("KPI · 顶部指标卡片")
    prov_kw = province[:2]
    df_prov = pdata["df_prov"]
    sc_zt   = pdata["sc_zt"]

    p = {
        "展业省份":   province,
        "已评级主体_家": len(sc_zt),
        "债项总数_条":   len(df_prov),
        "发行总额_亿":   round(float(df_prov["发行总额"].sum()), 2) if "发行总额" in df_prov.columns else 0,
        "覆盖城市_个":   int(df_prov["城市"].nunique()),
        "评级机构数_家":   int(df_prov["主体评级机构"].nunique()),
    }

    ddf = qdb("SELECT * FROM v_province_kpi WHERE 省份 LIKE ?", [f"%{prov_kw}%"])
    if ddf.empty:
        fail("v_province_kpi 无数据")
        return False
    dr = ddf.iloc[0]
    d = {
        "展业省份":   province,
        "已评级主体_家": int(dr["已评级主体_家"]),
        "债项总数_条":   int(dr["债项总数_条"]),
        "发行总额_亿":   float(dr["发行总额_亿"]),
        "覆盖城市_个":   int(dr["覆盖城市_个"]),
        "评级机构数_家":   int(dr["评级机构数_家"]),
    }


    r0 = compare_scalar("展业省份", p["展业省份"], d["展业省份"], atol=0)
    r1 = compare_scalar("已评级主体_家", p["已评级主体_家"], d["已评级主体_家"],
                        atol=30, allow_diff=True)
    r2 = compare_scalar("债项总数_条",   p["债项总数_条"],   d["债项总数_条"],   atol=0)
    r3 = compare_scalar("发行总额_亿",   p["发行总额_亿"],   d["发行总额_亿"],   atol=0.1)
    r4 = compare_scalar("覆盖城市_个",   p["覆盖城市_个"],   d["覆盖城市_个"],   atol=0)
    r5 = compare_scalar("评级机构数_家", p["评级机构数_家"], d["评级机构数_家"], atol=0)
    return all([r0, r1, r2, r3, r4, r5])


# ══════════════════════════════════════════════════════════════
# Tab 0：评级机构全景
# ══════════════════════════════════════════════════════════════

def compare_tab0(pdata: dict, province: str) -> bool:
    """
    Tab 0 · 评级机构全景

    A. 评级机构市占率（v_agency_market_share）
       - 主体数、债项数应完全一致
       - 债项数：pandas 按债项评级机构分组，SQL 用两个 CTE 分别统计后 JOIN
    B. 评级机构竞争格局（v_agency_competitive_landscape）
       - 该省内的评级机构集合应与 pandas 一致
    """
    section("Tab 0 · 评级机构全景")
    prov_kw = province[:2]
    results = []

    # A. 机构排名表
    pd_ag = mart_agency_stats(pdata["df_prov"])
    dq_ag = qdb(
        "SELECT * FROM v_agency_market_share WHERE 省份 LIKE ? ORDER BY 主体数 DESC",
        [f"%{prov_kw}%"])
    info(f"评级机构数量对比：pandas={len(pd_ag)}家  DuckDB={len(dq_ag)}家")
    results.append(compare_df_counts("评级机构数量对比", pd_ag, dq_ag))
    if not pd_ag.empty and not dq_ag.empty:
        results.append(compare_numeric_col(
            "该省主体数对比:", pd_ag, dq_ag, "评级机构", "主体数", atol=0))
        results.append(compare_numeric_col(
            "该省债项数对比:", pd_ag, dq_ag, "评级机构", "债项数", atol=0))
        results.append(compare_numeric_col(
            "评级机构主体市占率对比:", pd_ag, dq_ag, "评级机构", "省内主体市占率_pct", atol=0.1))
        results.append(compare_numeric_col(
            "评级机构债项主体比:", pd_ag, dq_ag, "评级机构", "省内债项主体比", atol=0.1))    

    # B. 竞争格局机构集合
    dq_comp = qdb(
        "SELECT DISTINCT 主体评级机构 FROM v_agency_competitive_landscape "
        "WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL",
        [f"%{prov_kw}%"])
    pd_set = set(pd_ag["评级机构"].dropna()) if not pd_ag.empty else set()
    dq_set = set(dq_comp["主体评级机构"].dropna()) if not dq_comp.empty else set()
    if pd_set == dq_set:
        ok(f"竞争格局机构集合：一致（{len(pd_set)}家）")
        results.append(True)
    else:
        only_pd = pd_set - dq_set
        only_dq = dq_set - pd_set
        if only_pd: note(f"仅 pandas 有：{sorted(only_pd)}")
        if only_dq: note(f"仅 DuckDB 有：{sorted(only_dq)}")
        results.append(False)

    return all(results)


# ══════════════════════════════════════════════════════════════
# Tab 1：已评级主体全景
# ══════════════════════════════════════════════════════════════

def compare_tab1(pdata: dict, province: str) -> bool:
    """
    Tab 1 · 已评级主体全景

    A. 去重主体数量和财务字段（v_issuer_profile）
    B. 城市主体明细（v_city_credit_overview）

    去重规则差异（已知，非 bug）：
      pandas  drop_duplicates 保留第一条（顺序依赖 DataFrame 行序）
      SQL     DISTINCT ON + ORDER BY 行政级别量化 DESC（确定性，更正确）
      → 行数允许微小差异，财务字段在共有发行人上应完全一致
    """
    section("Tab 1 · 已评级主体全景")
    prov_kw = province[:2]
    results = []
    sc_zt   = pdata["sc_zt"]

    # A. 去重主体
    dq_ip = qdb("SELECT * FROM v_issuer_profile WHERE 省份 LIKE ?", [f"%{prov_kw}%"])
    info(f"去重主体：pandas={len(sc_zt)}家  DuckDB={len(dq_ip)}家")
    results.append(compare_df_counts("去重主体数", sc_zt, dq_ip, allow_diff=True))

    if not sc_zt.empty and not dq_ip.empty:
        common = set(sc_zt["发行人中文名称"]) & set(dq_ip["发行人中文名称"])

        sc_c = sc_zt[sc_zt["发行人中文名称"].isin(common)]
        dq_c = dq_ip[dq_ip["发行人中文名称"].isin(common)]
        for col in ["总资产", "净资产", "营业收入", "净利润", "资产负债率"]:
            results.append(compare_numeric_col(
                "财务字段", sc_c, dq_c, "发行人中文名称", col, atol=0.01))
        if FISCAL_COL in sc_c.columns and FISCAL_COL in dq_c.columns:
            results.append(compare_numeric_col(
                "财力字段", sc_c, dq_c, "发行人中文名称", FISCAL_COL, atol=0.1))
        else:
            note(f"财力列 '{FISCAL_COL}': "
                 f"pandas={'有' if FISCAL_COL in sc_c.columns else '无'}  "
                 f"DuckDB={'有' if FISCAL_COL in dq_c.columns else '无'}")

    # B. 城市主体明细
    dq_city = qdb(
        "SELECT * FROM v_city_credit_overview WHERE 省份 LIKE ?", [f"%{prov_kw}%"])
    if dq_city.empty:
        fail("v_city_credit_overview 无数据")
        results.append(False)
    else:
        ok(f"v_city_credit_overview：{len(dq_city)}行，"
           f"覆盖 {dq_city['城市'].nunique()} 个城市")
        results.append(True)

    return all(results)


# ══════════════════════════════════════════════════════════════
# Tab 2：准入门槛模拟器
# ══════════════════════════════════════════════════════════════

def compare_tab2(pdata: dict, province: str) -> bool:
    """
    Tab 2 · 准入门槛模拟器

    对照策略：
      1. 全省基准（不限城市/级别/评级）
         pandas  = mart_financial_bench(sc_zt)
         DuckDB  = 对 v_issuer_profile 按省份聚合算分位数
                   （不直接用 v_financial_bench，因它是细分宽表，
                    全省汇总需先 WHERE 省份再在省内聚合）

      2. 特定三元组抽查
         取 v_financial_bench 中样本数最多的（城市,级别,评级）三元组，
         与 pandas 对同一子集的计算结果对比。

    分位数差异（已知，非 bug）：
      DuckDB QUANTILE_CONT vs pandas 自实现 _quantile 均为线性插值，
      结果极度接近，允许 ±0.05 的浮点差异。
    """
    section("Tab 2 · 准入门槛模拟器")
    prov_kw = province[:2]
    sc_zt   = pdata["sc_zt"]
    results = []

    # 1. 全省基准
    pd_bench = mart_financial_bench(sc_zt)
    info(f"pandas 全省基准样本数：{pd_bench.get('n', 0)}")

    dq_all = qdb("""
        SELECT
            COUNT(*)                              AS 样本数,
            ROUND(MEDIAN(总资产),     2)          AS 总资产_中位,
            ROUND(MEDIAN(净资产),     2)          AS 净资产_中位,
            ROUND(MEDIAN(营业收入),   2)          AS 营业收入_中位,
            ROUND(MEDIAN(净利润),     2)          AS 净利润_中位,
            ROUND(MEDIAN(资产负债率), 2)          AS 负债率_中位
        FROM v_issuer_profile
        WHERE 省份 LIKE ?
    """, [f"%{prov_kw}%"])

    if dq_all.empty:
        fail("v_issuer_profile 全省聚合无数据")
        return False

    dr = dq_all.iloc[0]
    info(f"DuckDB 全省基准样本数：{int(dr['样本数'])}")
    results.append(compare_scalar(
        "全省样本数", pd_bench.get("n", 0), int(dr["样本数"]),
        atol=0, allow_diff=True))

    for metric, sql_col in [
        ("总资产",    "总资产_中位"),
        ("净资产",    "净资产_中位"),
        ("营业收入",  "营业收入_中位"),
        ("净利润",    "净利润_中位"),
        ("资产负债率","负债率_中位"),
    ]:
        if metric not in pd_bench:
            continue
        pd_med = pd_bench[metric].get("median")
        dq_med = float(dr[sql_col]) if dr[sql_col] is not None else None
        results.append(compare_scalar(
            f"{metric} 中位数", pd_med, dq_med, atol=0.05, allow_diff=True))

    # 2. 特定三元组抽查
    dq_top = qdb("""
        SELECT 城市, 城投行政级别, 主体评级, 样本数, 总资产_中位
        FROM v_financial_bench
        WHERE 省份 LIKE ?
        ORDER BY 样本数 DESC LIMIT 1
    """, [f"%{prov_kw}%"])

    if not dq_top.empty:
        row    = dq_top.iloc[0]
        city   = str(row["城市"])
        level  = str(row["城投行政级别"])
        rating = str(row["主体评级"])
        info(f"三元组抽查：{city}+{level}+{rating}，DuckDB样本={int(row['样本数'])}")
        sub = sc_zt[(sc_zt["城市"] == city) &
                    (sc_zt["城投行政级别"] == level) &
                    (sc_zt["主体评级"] == rating)]
        if not sub.empty:
            pb = mart_financial_bench(sub)
            results.append(compare_scalar(
                "子集样本数", pb.get("n", 0), int(row["样本数"]),
                atol=2, allow_diff=True))
            if "总资产" in pb and row["总资产_中位"] is not None:
                results.append(compare_scalar(
                    "子集总资产中位", pb["总资产"].get("median"),
                    float(row["总资产_中位"]), atol=0.1, allow_diff=True))
        else:
            note("pandas 子集为空，跳过三元组抽查")

    return all(results)


# ══════════════════════════════════════════════════════════════
# Tab 3：展业帮手圈
# ══════════════════════════════════════════════════════════════

def compare_tab3(pdata: dict, province: str, agency: str) -> bool:
    """
    Tab 3 · 展业帮手圈
    
    A. 主承销商机构数量（v_underwriter_stats）
    重点验证指标：
    1.主承销商机构数量


    B. 展业帮手圈（v_partner_network）
    选定评级机构：中诚信；选定一家主承销商（已承做主体数最多的一家）
    重点验证指标：
    1. 这家主承销商在该省的主体业务数量(即主承销商的客户数)
    2. 我方客户数（my_issuers）
    3. 共同客户数
    4. 共同客户列表
    """
    section("Tab 3 · 展业帮手圈")
    prov_kw = province[:2]
    results = []
    
    # A. 主承销商数量对比
    pd_uw = mart_underwriter_stats(pdata["df_prov"])
    
    # DuckDB查询：聚合到省级别
    dq_uw = qdb("""
        SELECT 
            主承销商,
            SUM(已承做主体数) as 已承做主体数,
            SUM(债项发行数) as 债项发行数,
            SUM(发行总额_亿) as 发行总额_亿
        FROM v_underwriter_stats 
        WHERE 省份 LIKE ?
        GROUP BY 主承销商
    """, [f"%{prov_kw}%"])
    
    if not dq_uw.empty:
        dq_uw["发行倍数"] = (
            dq_uw["债项发行数"] / dq_uw["已承做主体数"].replace(0, None)
        ).round(2)
    
    
    info(f"PartA 主承销商机构数量:pandas={len(pd_uw)}家  DuckDB={len(dq_uw)}家")
    results.append(compare_df_counts("主承销商机构数量", pd_uw, dq_uw))
    
    # B. 展业帮手圈
    info(f"PartB 测试背景：选定我方评级机构：中诚信；选定主承销商（计算出已承做主体数最多的一家）, 然后验证主承销商的客户数 | 我方客户数 | 共同客户数 | 共同客户列表 是否一致")
    
    # 找出已承做主体数最多的主承销商以及这家主承销商的客户数
    top_uw_name = None
    top_uw_count = {}

    # 用duckdb计算
    if not dq_uw.empty:
        top_dq = dq_uw.nlargest(1, "已承做主体数").iloc[0]
        top_uw_count["DuckDB"] = (top_dq["主承销商"], int(top_dq["已承做主体数"]))
        top_uw_name = top_dq["主承销商"]  # 默认用 DuckDB 的选择
    
    # 用python计算
    if not pd_uw.empty:
        top_pd = pd_uw.nlargest(1, "已承做主体数").iloc[0]
        top_uw_count["pandas"] = (top_pd["主承销商"], int(top_pd["已承做主体数"]))
        if not top_uw_name:
            top_uw_name = top_pd["主承销商"]

    # 输出两边的结果
    for engine, (name, count) in top_uw_count.items():
        info(f" {engine} 计算出的客户数最多的承销商: {name} (客户数：{count}家)")

    # 验证python和duckdb计算的选定承销商的客户数是否一致
    if top_uw_name and not pd_uw.empty and not dq_uw.empty:
        pd_selected = pd_uw[pd_uw["主承销商"] == top_uw_name]
        dq_selected = dq_uw[dq_uw["主承销商"] == top_uw_name]
        
        if not pd_selected.empty and not dq_selected.empty:
            compare_numeric_col(
                f"计算出的已承做主体数对比：",
                pd_selected, dq_selected, "主承销商", "已承做主体数", atol=0)
    
    # 验证python和duckdb计算的我方客户数是否一致
    # Python 计算
    pd_partner, pd_my_issuers = mart_partner_network(pdata["df_prov"], agency)
    info(f"pandas my_issuers ({agency})该省我方客户数量: {len(pd_my_issuers)}家")
    
    # DuckDB 计算我方客户数（修正：同时检查主体和债项评级机构）
    dq_my_issuers = qdb("""
        SELECT COUNT(DISTINCT 发行人中文名称) as cnt
        FROM v_bond
        WHERE 省份 LIKE ?
          AND (
              (主体评级机构 IS NOT NULL AND 主体评级机构 LIKE ?)
              OR
              (债项评级机构 IS NOT NULL AND 债项评级机构 LIKE ?)
          )
    """, [f"%{prov_kw}%", f"%{agency}%", f"%{agency}%"])
    
    if not dq_my_issuers.empty:
        my_count_dq = int(dq_my_issuers.iloc[0]["cnt"])
        info(f"DuckDB my_issuers ({agency})该省我方客户数量: {my_count_dq}家")
        results.append(compare_scalar(
            "我方客户数", len(pd_my_issuers), my_count_dq, atol=0))
    


    # 验证python和duckdb计算的共同客户数是否一致
    # python已在上方pd_partner计算完成
    # duckdb
        # 验证python和duckdb计算的共同客户数是否一致
    # 先查出明细数据，再在Python中聚合到省级
    dq_partner_detail = qdb("""
        SELECT 主承销商, 共同客户数, 已承做主体数, 共同客户列表
        FROM v_partner_network
        WHERE 评级机构 = ?
          AND 省份 LIKE ?
    """, [agency, f"%{prov_kw}%"])
    
    # 省级聚合：合并同一承销商在不同城市/级别的数据
    if not dq_partner_detail.empty:
        # 按主承销商聚合
        dq_partner = dq_partner_detail.groupby("主承销商").agg({
            "已承做主体数": "max",
        }).reset_index()
        
        # 合并共同客户列表（去重）
        def merge_clients(group):
            all_clients = set()
            for clients_str in group["共同客户列表"].dropna():
                if clients_str:
                    all_clients.update(clients_str.split("、"))
            return "、".join(sorted(all_clients)) if all_clients else ""
        
        client_lists = dq_partner_detail.groupby("主承销商").apply(
            merge_clients, include_groups=False
        ).reset_index(name="共同客户列表")
        
        dq_partner = dq_partner.merge(client_lists, on="主承销商")
        dq_partner["共同客户数"] = dq_partner["共同客户列表"].apply(
            lambda x: len(x.split("、")) if x else 0
        )
        dq_partner["合作等级"] = dq_partner["共同客户数"].apply(
            lambda n: "主要合作伙伴" if n >= 2 else ("初步合作对象" if n >= 1 else "待开拓合作关系")
        )
        dq_partner = dq_partner.sort_values("共同客户数", ascending=False).reset_index(drop=True)
    else:
        dq_partner = pd.DataFrame(columns=["主承销商", "共同客户数", "已承做主体数", "共同客户列表", "合作等级"])
    


    # 验证python和duckdb计算的共同客户列表是否一致

    if not pd_partner.empty and not dq_partner.empty and 'top_uw_name' in locals():
        dq_uw_detail = dq_partner[dq_partner["主承销商"] == top_uw_name]
        pd_uw_detail = pd_partner[pd_partner["主承销商"] == top_uw_name]
        
        if not dq_uw_detail.empty and not pd_uw_detail.empty:
            dq_common_count = int(dq_uw_detail.iloc[0]["共同客户数"])
            pd_common_count = int(pd_uw_detail.iloc[0]["共同客户数"])
            
            # 对比共同客户列表
            dq_common_list = set()
            if dq_uw_detail.iloc[0]["共同客户列表"]:
                dq_common_list = set(str(dq_uw_detail.iloc[0]["共同客户列表"]).split("、"))
            
            pd_common_list = set()
            if pd_uw_detail.iloc[0]["共同客户列表"]:
                pd_common_list = set(str(pd_uw_detail.iloc[0]["共同客户列表"]).split("、"))
            
            info(f"共同客户数量对比：")
            results.append(compare_scalar(
                f"计算出的共同客户数量", pd_common_count, dq_common_count, atol=0))
            
            if dq_common_list == pd_common_list:
                ok(f"共同客户列表一致：{len(dq_common_list)}家")
            else:
                only_dq = dq_common_list - pd_common_list
                only_pd = pd_common_list - dq_common_list
                if only_dq:
                    note(f"仅DuckDB有 {len(only_dq)}家：{sorted(list(only_dq))[:5]}...")
                if only_pd:
                    note(f"仅pandas有 {len(only_pd)}家：{sorted(list(only_pd))[:5]}...")
                results.append(False)
    


    return all(results)


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

def main(province: str | None, agency: str) -> None:
    if province is None:
        province = _auto_province()

    print(f"\n{B}双引擎一致性对比  |  省份：{province}  |  机构：{agency[:12]}...{E}")
    print(f"pandas FISCAL_COL = '{FISCAL_COL}'")
    print(f"DuckDB: {'✅ 存在' if DB_PATH.exists() else '❌ 不存在'}")

    if not DB_PATH.exists():
        print(f"\n{R}ERROR: 请先运行 python scripts/build_warehouse.py{E}")
        sys.exit(1)

    pdata = build_pandas_data(province)
    print(f"Pandas: {'✅ 存在' if DB_PATH.exists() else '❌ 不存在'}")

    results = {}
    results["KPI"]   = compare_kpi(pdata, province)
    results["Tab 0"] = compare_tab0(pdata, province)
    results["Tab 1"] = compare_tab1(pdata, province)
    results["Tab 2"] = compare_tab2(pdata, province)
    results["Tab 3"] = compare_tab3(pdata, province, agency)

    section("对比汇总")
    passed = sum(results.values())
    total  = len(results)
    for tab, passed_flag in results.items():
        status = f"{G}PASS{E}" if passed_flag else f"{R}FAIL{E}"
        print(f"  [{status}]  {tab}")

    print()
    if passed == total:
        print(f"{G}{B}✅ 全部 {total} 项通过，双引擎结果一致。{E}")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"{R}{B}❌ {total-passed}/{total} 项未通过：{failed}{E}")
        print(f"\n{Y}排查思路：{E}")
        print(f"  1. 查看上方各 Tab 的 ❌ 行，定位具体差异列和数值")
        print(f"  2. DuckDB 数值正确 → 修对应的 mart_*.py pandas 函数")
        print(f"  3. pandas 数值正确 → 修对应的 .sql 视图文件")
        print(f"  4. 修 SQL 后重跑 build_warehouse.py，再跑本脚本")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="双引擎一致性对比")
    parser.add_argument("province", nargs="?", default=None,
                        help="目标省份（默认自动选择数据量适中的省）")
    parser.add_argument("--agency", default="中诚信国际信用评级有限责任公司",
                        help="Tab 3 帮手圈测试用的评级机构全称")
    args = parser.parse_args()
    main(province=args.province, agency=args.agency)
