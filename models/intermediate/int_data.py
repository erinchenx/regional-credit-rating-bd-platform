# -*- coding: utf-8 -*-
"""
models/intermediate/int_province_bonds.py
==========================================
【Intermediate 层】多表关联（Join）逻辑

职责：
  - 接收 staging 层的标准化 DataFrame 作为输入
  - 将债券数据与财力数据按「城市」关联
  - 按省份切片，过滤出目标省份的债券记录
  - 生成「去重主体视图」（每个发行人只保留一条主记录）
  - 不做最终指标聚合，只做"让数据可关联、可分析"的工作

对应 dbt 层级：intermediate（int_）
"""

import numpy as np
import pandas as pd
import streamlit as st

import logging
logger = logging.getLogger(__name__)

# 财力列的统一业务列名（从 staging 层约定而来）
FISCAL_COL = "城市财力_亿元"

# ──────────────────────────────────────────────
# 【DQC】数据质量检查(Intermediate层）
# ──────────────────────────────────────────────
from app.config import (
    INT_JOIN_EXPANSION_THRESHOLD,
    INT_JOIN_MATCH_RATE_THRESHOLD,
)

def _dqc_int_join(df_before: pd.DataFrame, df_after: pd.DataFrame, province: str):
    """
    【DQC · Intermediate】关联完整性审计
    1. 检查数据膨胀（Join 是否引入了重复行）
    2. 检查关联覆盖率（有多少债项没匹配到财力）
    """
    row_before = len(df_before)
    row_after = len(df_after)
    
    if row_before == 0: 
        return df_after

    # 1. 膨胀检查 (Hard Check)
    expansion_rate = row_after / row_before
    if expansion_rate > INT_JOIN_EXPANSION_THRESHOLD:
        raise ValueError(
            f"[DQC · Join Error] {province} 数据关联后行数膨胀了 {expansion_rate:.2%}！\n"
            f"可能原因：财力数据或债券数据中存在重复的城市键。\n"
            f"请检查上游 Staging 层针对城市名称重复的DQC。"
        )

    # 2. 覆盖率检查 (Soft Warning)
    # 计算 FISCAL_COL 不为空的比例
    match_count = df_after[FISCAL_COL].notna().sum()
    match_rate = match_count / row_after

    if match_rate < INT_JOIN_MATCH_RATE_THRESHOLD:
        missing_cities = df_after[df_after[FISCAL_COL].isna()]["城市"].unique().tolist()
        msg = (
            f"[DQC · Join Warning] {province} 关联率 {match_rate:.1%}，"
            f"未匹配城市：{missing_cities}"
        )
        logger.warning(msg)
        st.warning(
            f"数据质量提示：{province} 债券与财力关联率仅为 {match_rate:.1%}。\n"
            f"未匹配成功的城市样本：{missing_cities}。"
            f"请检查这些城市的名字是否与债券数据中一致，以及这些城市是否有财力数据"
        )
    return df_after

# ──────────────────────────────────────────────
# 公开接口：Intermediate 层核心函数
# ──────────────────────────────────────────────

def int_filter_province(
    df_bond: pd.DataFrame,
    province: str,
) -> pd.DataFrame:
    """
    【Intermediate】从全国债券数据中切片出目标省份的记录。

    匹配规则：`省份` 字段包含 province 前两个字（如"四川省" → 匹配"四川"）。
    返回副本，避免修改上游数据。
    """
    mask = df_bond["省份"].str.contains(province[:2], na=False)
    return df_bond[mask].copy()


@st.cache_data(show_spinner=False)
def int_join_fiscal(
    df_province: pd.DataFrame,
    df_fiscal: pd.DataFrame,
) -> pd.DataFrame:
    """
    【Intermediate / 缓存层】将省份债券数据与财力数据按「城市」左连接。

    缓存策略：
      - st.cache_data 对 DataFrame 参数做内容哈希（基于 pickle），
        只有当上游 Staging 层输出真正变化时，此函数才重新执行。
      - show_spinner=False：由 main.py 的外层 spinner 统一展示进度，
        避免内层弹出重复的加载提示。

    - 财力数据取每个城市的最大值（去重聚合）
    - 连接结果中财力列重命名为统一业务列名 FISCAL_COL
    - 去除重复列，避免后续计算冲突
    返回合并后的宽表。
    """
    # 获取省份名称用于报错信息
    prov_name = df_province["省份"].iloc[0] if not df_province.empty else "未知省份"
    
    if "城市" not in df_fiscal.columns or "一般公共预算收入(亿元)" not in df_fiscal.columns:
        # 财力文件不存在或列不完整时，直接填充空列
        df_province[FISCAL_COL] = np.nan
        return df_province


    df_merged = df_province.merge(df_fiscal, on="城市", how="left")
    df_merged = df_merged.rename(columns={"一般公共预算收入(亿元)": FISCAL_COL})

    # 去除合并过程中可能引入的重复列
    df_merged = df_merged.loc[:, ~df_merged.columns.duplicated()]
    
    # 3. 【新增】执行 DQC 审计
    df_merged = _dqc_int_join(df_province, df_merged, prov_name)
    
    return df_merged


@st.cache_data(show_spinner=False)
def int_build_issuer_view(df_province_with_fiscal: pd.DataFrame) -> pd.DataFrame:
    """
    【Intermediate / 缓存层】构建「去重主体视图」。

    缓存策略：
      - 同 int_join_fiscal，依赖上游 DataFrame 内容哈希触发失效。
      - 当用户切换省份时，传入的 df_province_with_fiscal 内容不同，
        自动触发重算；同省份重复点击则直接命中缓存，无额外开销。
      - show_spinner=False：进度由 main.py 外层 spinner 统一管理。

    规则：
      - 以「发行人中文名称」为粒度去重（每家平台只保留一条记录）
      - 过滤掉主体评级为空的行（无评级主体不纳入分析）
      - 按财力 → 行政级别 → 主体评级 → 总资产 降序排列
      - 重置序号列

    该视图是 marts 层各指标计算的基础宽表。
    """
    df = df_province_with_fiscal.copy()
    df = df[df["主体评级"].notna() & (df["主体评级"].astype(str).str.strip() != "")] # 先删空
    df = df.drop_duplicates(subset=["发行人中文名称"]) # 再删重复


    # 排序：财力 → 行政级别量化 → 主体级别量化 → 总资产
    sort_cols = [
        c for c in [FISCAL_COL, "行政级别量化", "主体级别量化", "总资产"]
        if c in df.columns
    ]
    df = df.sort_values(sort_cols, ascending=False).reset_index(drop=True)
    df["序号"] = range(1, len(df) + 1)

    return df
