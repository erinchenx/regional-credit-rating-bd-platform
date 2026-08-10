# -*- coding: utf-8 -*-
"""
models/marts/mart_credit_indicators.py
========================================
【Marts 层】看板指标计算逻辑

职责：
  - 接收 intermediate 层的关联宽表作为输入
  - 计算各类看板所需的最终聚合指标：
      · mart_agency_stats      → 评级机构市场格局表
      · mart_competition_matrix→ 评级机构竞争热力矩阵
      · mart_financial_bench   → 财务基准（准入门槛模拟器）
      · mart_underwriter_stats → 主承销商排名表
      · mart_partner_network      → 展业帮手圈（承销商共同客户分析）
  - 所有函数只做「聚合 / 计算 / 格式化」，不做数据读取

对应 dbt 层级：marts
"""

import math
from typing import Optional
import pandas as pd
from models.intermediate.int_data import FISCAL_COL
import streamlit as st
import logging
logger = logging.getLogger(__name__)
from app.config import MART_PROV_PLATFORM_FISCAL_QUANTILE  
# ──────────────────────────────────────────────
# 常量（看板层专用）
# ──────────────────────────────────────────────
RATING_ORDER: dict[str, int] = {
    "AAA": 5, "AA+": 4, "AA": 3, "AA-": 2, "A+": 1, "A": 0,
}
LEVEL_ORDER: dict[str, int] = {
    "省级": 7,
    "省(开发区)级": 6,
    "地市级": 5,
    "国家新区级": 4,
    "地市(开发区)级": 3,
    "区县(开发区)级": 2,
    "区县级": 1,
}
FISCAL_BANDS: list[tuple[str, float, float]] = [
    ("0-100亿",    0,    100),
    ("100-200亿",  100,  200),
    ("200-300亿",  200,  300),
    ("300-500亿",  300,  500),
    ("500-700亿",  500,  700),
    ("700-1000亿", 700,  1000),
    ("1000亿以上", 1000, 99999),
]
AGENCY_FULLNAME: dict[str, str] = {
    "联合资信":   "联合资信评估股份有限公司",
    "大公国际":   "大公国际资信评估有限公司",
    "中诚信国际": "中诚信国际信用评级有限责任公司",
    "鹏元资信":   "鹏元资信评估有限公司",
    "东方金诚":   "东方金诚国际信用评估有限公司",
    "上海新世纪": "上海新世纪资信评估投资服务有限公司",
    "远东资信":   "远东资信评估有限公司",
    "大普信用":   "大普信用评估有限公司",
    "中证鹏元":   "中证鹏元资信评估股份有限公司",
    "惠誉博华":   "惠誉博华信用评级有限公司",
}


# ──────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────

def _quantile(values: list[float], q: float) -> Optional[float]:
    """计算列表的分位数（不依赖 numpy，兼容 None/NaN 过滤）。"""
    cleaned = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not cleaned:
        return None
    s = sorted(cleaned)
    pos = (len(s) - 1) * q
    #lo (Lower)：索引的整数部分（向下取整）。
    #hi (Higher)：索引的下一个位置（向上取整），并用 min 确保不越界。
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (pos - lo) * (s[hi] - s[lo])


def _mean(values: list[float]) -> Optional[float]:
    """计算列表均值（过滤 None/NaN）。"""
    cleaned = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return sum(cleaned) / len(cleaned) if cleaned else None


def _collect_agencies(df: pd.DataFrame) -> list[str]:
    """
    从债券数据中收集所有实际出现的评级机构，
    按主体评级数量降序排列。
    """
    agencies: set[str] = set()
    for col in ["主体评级机构", "债项评级机构"]:
        if col in df.columns:
            for v in df[col].dropna().unique():
                v = str(v).strip()
                if v:
                    agencies.add(v)

    subject_counts = {
    ag: (df["主体评级机构"] == ag).sum() 
    for ag in agencies
    }
    return sorted(agencies, key=lambda a: subject_counts.get(a, 0), reverse=True)

# ──────────────────────────────────────────────
# 【DQC】数据质量检查(Marts层）
# ──────────────────────────────────────────────

def dqc_marts_platform_dismatch(df_marts: pd.DataFrame, province_name: str):
    """
    【DQC · Marts Check】省级平台与财力水平匹配度审计

    返回：(suspicious_df, warning_msg)
      suspicious_df - 异常主体 DataFrame（空则无异常）
      warning_msg   - 警告文案字符串（空则无异常），由调用方决定展示时机
      total_cities  - 全省总城市数，后续在main.py调用，用于排名格式的优化，{row['_fiscal_rank']}/{total_cities}，
    """
    # 1. 建立省内城市财力字典与排名参考（以城市为唯一键）
    city_stats = (
        df_marts.drop_duplicates("城市")[["城市", FISCAL_COL]]
        .dropna()
        .copy()
    )

    if city_stats.empty:
        return pd.DataFrame(), "", 0

    city_stats["_fiscal_rank"] = (
        city_stats[FISCAL_COL]
        .rank(ascending=False, method="min")
        .astype(int)
    )
    total_cities = len(city_stats)

    # 2. 动态计算阈值 (p)
    p_threshold = city_stats[FISCAL_COL].quantile(MART_PROV_PLATFORM_FISCAL_QUANTILE)

    # 3. 关联排名信息并锁定"高配低挂"样本
    df_audit = df_marts.merge(
        city_stats[["城市", "_fiscal_rank"]],
        on="城市",
        how="left",
    )
    df_audit["_fiscal_rank"] = df_audit["_fiscal_rank"].fillna(0).astype(int) #排名不要小数位

    mask = (
        (df_audit["城投行政级别"] == "省级") &
        (df_audit[FISCAL_COL] < p_threshold)
    )
    suspicious_df = df_audit[mask].copy()

    if suspicious_df.empty:
        return pd.DataFrame(), "", 0

    # 4. 整理异常信息
    summary_list = []
    for _, row in suspicious_df.drop_duplicates("发行人中文名称").iterrows():
        summary_list.append(
            f"主体：{row['发行人中文名称']} | 平台行政级别：{row['城投行政级别']} | "
            f"城市：{row['城市']} | 财力：{row[FISCAL_COL]}亿 | "
            f"排名：{row['_fiscal_rank']}/{total_cities}"
        )

    detail_msg = "\n".join(summary_list)
    warning_msg = (
        f"{province_name} 存在 {len(summary_list)} 个省级平台"
        f"位于省内低财力城市（排名位于靠后的{MART_PROV_PLATFORM_FISCAL_QUANTILE:.0%}区间）。"
    )

    # 5. terminal 输出
    logger.warning(
        f"\n[DQC · 业务数据提示] {province_name} 存在「省级平台与城市财力不匹配」的情况：\n"
        f"异常样本详情：\n{detail_msg}\n"
    )

    return suspicious_df, warning_msg, total_cities
# ──────────────────────────────────────────────
# 公开接口：Marts 层核心函数
# ──────────────────────────────────────────────
def mart_agency_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    【Mart】评级机构市场格局表。

    输入：省份债券宽表（含财力，含重复主体记录）
    输出列：序号 | 评级机构 | 主体数 | 债项数 | 主体占比 | 债项/主体

    业务口径：
      - 主体数：按主体评级机构分组，统计唯一主体名称数量（去重）。
      - 债项数：按主体评级机构分组，统计债项数量（不去重，一只债一行）。
      即：主体数 = COUNT(DISTINCT 发行人中文名称)，债项数 = COUNT(*)，
      均以主体评级机构为分组维度。
    """
    actual_agencies = _collect_agencies(df)

    # ── 主体数（按发行人+机构去重）──

    df1 = df.copy()
    df1["主体评级机构"] = df1["主体评级机构"].astype(str).str.strip()
    df1 = df1[(df1["主体评级机构"] != "") & (df1["主体评级机构"] != "nan")]

    deduped = df1.drop_duplicates(subset=["发行人中文名称", "主体评级机构"])

    subject_cnt = (
        deduped.groupby("主体评级机构")["发行人中文名称"]
        .count()
        .rename("主体数")
    )

    # ── 债项数：按主体评级机构计数（和 SQL View 一致）──
    bond_cnt = (
        df1.groupby("主体评级机构")["发行人中文名称"]
        .count()
        .rename("债项数")
    )

    result = pd.merge(
        subject_cnt.reset_index().rename(columns={"主体评级机构": "评级机构"}),
        bond_cnt.reset_index().rename(columns={"主体评级机构": "评级机构"}),
        on="评级机构", how="outer",
    ).fillna(0)

    '''
    ### 补全实际出现但数据为 0 的机构
    existing = set(result["评级机构"])
    extras = [{"评级机构": ag, "主体数": 0, "债项数": 0} for ag in actual_agencies if ag not in existing]
    if extras:
        result = pd.concat([result, pd.DataFrame(extras)], ignore_index=True)
    '''
    
    result["主体数"] = result["主体数"].astype(int)
    result["债项数"] = result["债项数"].astype(int)

    total_subjects = result["主体数"].sum()
    
    #total_bonds = result["债项数"].sum()    
    #result["债项占比"] = result["债项数"].apply(lambda x: f"{x / total_bonds:.1%}" if total_bonds else "0%")

    
    result["省内主体市占率_pct"] = result["主体数"].apply(
        lambda x: round(x * 100.0 / total_subjects, 1) if total_subjects else 0.0)
    
    result["省内债项主体比"] = result.apply(
        lambda r: round(r["债项数"] / r["主体数"], 2) if r["主体数"] > 0 else None, axis=1)

    result = result.sort_values("主体数", ascending=False).reset_index(drop=True)
    result.insert(0, "序号", range(1, len(result) + 1))
    return result


def mart_competition_matrix(
    df: pd.DataFrame,
    my_agency: str,
    dimension: str,
) -> tuple[object, dict, list[str], list[str]]:
    """
    【Mart】评级机构竞争热力矩阵数据。

    输入：
      df        - 省份债券宽表
      my_agency - 我方评级机构（在热力图中标注 ◀）
      dimension - 分析维度，可选值：
                    "fiscal"  按财力分段
                    "level"   按行政级别
                    "rating"  按主体评级
                    "city"    按城市

    输出：(matrix_dict, actual_agencies, categories)
      matrix_dict      - {agency: {category: count}} 的嵌套字典
      actual_agencies  - 按主体数降序排列的机构列表（my_agency 排首位）
      categories       - 横轴分类标签列表
    """
    actual_agencies = _collect_agencies(df)
    if not actual_agencies:
        return {}, [], []

    # 构建横轴分类
    if dimension == "fiscal":
        categories = [band[0] for band in FISCAL_BANDS]

        def get_category(row: dict) -> Optional[str]:
            fs = row.get(FISCAL_COL)
            if fs is None or (isinstance(fs, float) and math.isnan(fs)):
                return None
            for label, lo, hi in FISCAL_BANDS:
                if lo <= fs < hi:
                    return label
            return None

    elif dimension == "level":
        actual_levels = df["城投行政级别"].dropna().unique().tolist()
        categories = sorted(actual_levels, key=lambda lv: LEVEL_ORDER.get(lv, -1), reverse=True)

        def get_category(row: dict) -> Optional[str]:
            return row.get("城投行政级别")

    elif dimension == "rating":
        categories = [r for r in RATING_ORDER if r in df["主体评级"].values]

        def get_category(row: dict) -> Optional[str]:
            return row.get("主体评级")

    else:  # "city"
        categories = df["城市"].dropna().unique().tolist()

        def get_category(row: dict) -> Optional[str]:
            return row.get("城市")

    # 统计每个机构在每个分类中的主体数
    deduped = df.drop_duplicates(subset=["发行人中文名称"]).copy()
    matrix: dict[str, dict[str, int]] = {
        ag: {cat: 0 for cat in categories} for ag in actual_agencies
    }
    for _, row in deduped.iterrows():
        ag = row.get("主体评级机构")
        if not ag or ag not in matrix:
            continue
        cat = get_category(row.to_dict())
        if cat and cat in matrix[ag]:
            matrix[ag][cat] += 1

    # 我方机构排首位
    ordered_agencies = (
        ([my_agency] if my_agency in actual_agencies else []) +
        [ag for ag in actual_agencies if ag != my_agency]
    )

    return matrix, ordered_agencies, categories

def mart_financial_bench(df_issuer_view: pd.DataFrame) -> dict:
    """
    【Mart】财务基准统计（用于准入门槛模拟器）。

    输入：去重主体视图（来自 intermediate 层）
    输出：{
        "n": 主体数,
        "总资产":  {"min","max","mean","median","q1","q3"},
        "净资产":  {...},
        "营业收入": {...},
        "净利润":  {...},
        "资产负债率": {...},
    }
    """
    result: dict = {"n": len(df_issuer_view)}
    for col in ["总资产", "净资产", "营业收入", "净利润", "资产负债率"]:
        if col not in df_issuer_view.columns:
            continue
        values = df_issuer_view[col].dropna().tolist()
        if not values:
            continue
        result[col] = {
            "min":    round(min(values), 2),
            "max":    round(max(values), 2),
            "mean":   round(_mean(values), 2),
            "median": round(_quantile(values, 0.5), 2),
            "q1":     round(_quantile(values, 0.25), 2),
            "q3":     round(_quantile(values, 0.75), 2),
        }
    return result


def mart_underwriter_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    【Mart】主承销商业务排名表。

    输入：省份债券宽表
    输出列：序号 | 主承销商 | 已承做主体数 | 债项发行数 | 发行总额_亿 | 发行倍数
    """
    df2 = df.dropna(subset=["主承销商"]).copy()
    df2["主承销商"] = df2["主承销商"].str.split(",")
    df2 = df2.explode("主承销商")
    df2["主承销商"] = df2["主承销商"].str.strip()
    df2 = df2[df2["主承销商"] != ""]

    deduped = df2.drop_duplicates(subset=["发行人中文名称", "主承销商"])
    subject_cnt = deduped.groupby("主承销商")["发行人中文名称"].count().rename("已承做主体数")
    bond_cnt = df2.groupby("主承销商")["发行人中文名称"].count().rename("债项发行数")

    result = subject_cnt.reset_index().merge(bond_cnt.reset_index(), on="主承销商")

    if "发行总额" in df2.columns:
        amount_sum = df2.groupby("主承销商")["发行总额"].sum().rename("发行总额(亿元)")
        result = result.merge(amount_sum.reset_index(), on="主承销商", how="left")
        result["发行总额(亿元)"] = result["发行总额(亿元)"].round(2)

    result["发行倍数"] = (result["债项发行数"] / result["已承做主体数"]).round(2)
    result = result.sort_values("债项发行数", ascending=False).reset_index(drop=True)
    result.insert(0, "序号", range(1, len(result) + 1))
    return result



def mart_partner_network(
    df: pd.DataFrame,
    my_agency: str,
    filter_city: str = "",
    filter_level: str = "",
) -> tuple[pd.DataFrame, set[str]]:
    """
    【Mart】展业帮手圈：主承销商共同客户分析。

    输入：
      df           - 省份债券宽表
      my_agency    - 我方评级机构名称
      filter_city  - 筛选城市（空字符串表示不筛选）
      filter_level - 筛选行政级别（空字符串表示不筛选）

    输出：
      (result_df, my_issuers_set)
        result_df 列：主承销商 | 已承做主体数 | 已承做主体列表 | 共同客户数 |共同客户列表 | 合作等级
        my_issuers_set：我方评级机构的发行人集合
    """
    scope = df.loc[:, ~df.columns.duplicated()].copy()
    if filter_city:
        scope = scope[scope["城市"] == filter_city]
    if filter_level:
        scope = scope[scope["城投行政级别"] == filter_level]

    # 我方已覆盖的发行人
    mask_subject = scope["主体评级机构"].fillna("").str.contains(my_agency, regex=False)
    mask_bond = (
        scope["债项评级机构"].fillna("").str.contains(my_agency, regex=False)
        if "债项评级机构" in scope.columns
        else pd.Series(False, index=scope.index)
    )
    my_issuers: set[str] = set(scope[mask_subject | mask_bond]["发行人中文名称"].dropna())

    # 构建承销商 → {全部主体, 共同主体} 映射
    df_uw = scope.dropna(subset=["主承销商"]).copy()
    df_uw["主承销商"] = df_uw["主承销商"].str.split(",")
    df_uw = df_uw.explode("主承销商")
    df_uw["主承销商"] = df_uw["主承销商"].str.strip()
    df_uw = df_uw[df_uw["主承销商"] != ""]

    uw_map: dict[str, dict] = {}
    for _, row in df_uw.iterrows():
        uw = row["主承销商"]
        issuer = row["发行人中文名称"]
        if uw not in uw_map:
            uw_map[uw] = {"total": set(), "mine": set()}
        uw_map[uw]["total"].add(issuer)
        if issuer in my_issuers:
            uw_map[uw]["mine"].add(issuer)

    if not uw_map:
        empty_cols = ["主承销商", "共同客户数", "已承做主体数", "共同客户列表", "合作等级"]
        return pd.DataFrame(columns=empty_cols), my_issuers

    rows = []
    for uw, data in uw_map.items():
        n = len(data["mine"])
        rows.append({
            "主承销商":     uw,
            "已承做主体数": len(data["total"]),
            "共同客户数":   n,
            "共同客户列表": "、".join(sorted(data["mine"])),
            "合作等级":     "主要合作伙伴" if n >= 2 else ("初步合作对象" if n >= 1 else "待开拓合作关系"),
        })

    result_df = (
        pd.DataFrame(rows)
        .sort_values(["共同客户数", "已承做主体数"], ascending=False)
        .reset_index(drop=True)
    )
    return result_df, my_issuers



