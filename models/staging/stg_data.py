# -*- coding: utf-8 -*-
"""
models/staging/stg_data.py
================================
【Staging 层】原始数据清洗与标准化 + 数据质量防御（DQC）

职责：
  - 读取 Excel 原始文件（债券数据 & 财力数据）
  - 列名归一化（多版本字段名 → 统一标准字段名）
  - 评级机构名称标准化
  - 辅助量化列（行政级别量化、主体级别量化）
  - 【DQC】Schema Check：核心列缺失时抛出异常，阻断后续流程
  - 【DQC】Value Check：关键字段空值率超限时抛出异常，部分空值记录警告

对应 dbt 层级：staging（stg_）
"""

import json
import warnings
import duckdb
import numpy as np
import pandas as pd
import streamlit as st
from pathlib import Path
import logging
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 缓存辅助：读取文件的修改时间（作为缓存 key 的一部分）
# ──────────────────────────────────────────────

def _file_mtime(path: Path) -> float:
    """
    返回文件的最后修改时间戳（秒）。
    文件不存在时返回 0.0。
    """
    return path.stat().st_mtime if path.exists() else 0.0


def _read_active_version(version_file: Path) -> dict:
    """
    读取 active_version.json，返回当前激活的快照版本信息。
    文件不存在时返回空字典（触发降级到 Excel 读取）。
    """
    if not version_file.exists():
        return {}
    try:
        with open(version_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ──────────────────────────────────────────────
# 常量：评级机构名称归并映射
# ──────────────────────────────────────────────
AGENCY_NORMALIZE: dict[str, str] = {
    # 联合资信
    "联合资信评估股份有限公司":      "联合资信评估股份有限公司",
    "联合信用评级有限公司":           "联合资信评估股份有限公司",
    "联合资信":                       "联合资信评估股份有限公司",
    # 大公
    "大公国际资信评估有限公司":       "大公国际资信评估有限公司",
    "大公国际":                        "大公国际资信评估有限公司",
    # 中诚信
    "中诚信国际信用评级有限责任公司": "中诚信国际信用评级有限责任公司",
    "中诚信国际信用评级有限公司":     "中诚信国际信用评级有限责任公司",
    "中诚信国际":                      "中诚信国际信用评级有限责任公司",
    # 鹏元
    "鹏元资信评估有限公司":           "鹏元资信评估有限公司",
    "中证鹏元资信评估股份有限公司":   "中证鹏元资信评估股份有限公司",
    "鹏元资信":                        "鹏元资信评估有限公司",
    # 东方金诚
    "东方金诚国际信用评估有限公司":   "东方金诚国际信用评估有限公司",
    "东方金诚":                        "东方金诚国际信用评估有限公司",
    # 上海新世纪
    "上海新世纪资信评估投资服务有限公司": "上海新世纪资信评估投资服务有限公司",
    "上海新世纪":                      "上海新世纪资信评估投资服务有限公司",
    # 远东
    "远东资信评估有限公司":           "远东资信评估有限公司",
    "远东资信":                        "远东资信评估有限公司",
    # 大普
    "大普信用评估有限公司":           "大普信用评估有限公司",
    "大普信用":                        "大普信用评估有限公司",
    # 惠誉博华
    "惠誉博华信用评级有限公司":       "惠誉博华信用评级有限公司",
    "惠誉博华":                        "惠誉博华信用评级有限公司",
}

# 行政级别 & 主体评级的量化映射（供后续排序使用）
LEVEL_ORDER: dict[str, int] = {
    "省级": 6, "地市级": 5, "国家新区": 4,
    "地市(开发区)": 3, "归属区县级开发区": 2, "区县级": 1,
}
RATING_ORDER: dict[str, int] = {
    "AAA": 5, "AA+": 4, "AA": 3, "AA-": 2, "A+": 1, "A": 0,
}

# Bond Excel 字段：标准列名 → 可能出现的原始列名（按优先级排列）
BOND_COLS: dict[str, list[str]] = {
    "证券代码":         ["证券代码"],
    "证券简称":         ["证券简称"],
    "发行人中文名称":   ["发行人中文名称"],
    "发行人中文简称":   ["发行人中文简称"],
    "省份":             ["省份"],
    "城市":             ["城市"],
    "城投行政级别":     ["城投行政级别(YY)", "城投行政级别(Wind)", "城投行政级别"],
    "主承销商":         ["主承销商"],
    "Wind债券二级分类": ["Wind债券二级分类"],
    "发行总额":         ["发行总额\n[单位] 亿元", "发行总额(亿元)", "发行总额 亿元"],
    "票面利率":         ["票面利率(发行时)\n[单位] %", "票面利率(发行时) %"],
    "主体评级": [
        "主体评级\n[交易日期] 最新收盘日\n[评级机构] 国内评级机构(除中债资信)\n[评级对象类型] 主体信用评级",
        "主体评级",
    ],
    "主体评级机构": [
        "主体评级评级机构\n[交易日期] 最新收盘日\n[评级对象类型] 主体信用评级\n[评级机构] 国内评级机构(除中债资信)",
        "主体评级机构",
    ],
    "最新债项评级": [
        "债项评级\n[交易日期] 最新收盘日\n[评级机构] 国内评级机构(除中债资信)",
        "最新债项评级",
    ],
    "债项评级机构": [
        "债项评级机构\n[交易日期] 最新收盘日\n[评级机构] 国内评级机构(除中债资信)",
        "债项评级机构",
    ],
    "总资产":         ["资产总计\n[报告期] 去年三季\n[报表类型] 合并报表\n[单位] 亿元", "总资产 亿元"],
    "净资产":         ["所有者权益合计\n[报告期] 去年三季\n[报表类型] 合并报表\n[单位] 亿元", "所有者权益 亿元"],
    "营业收入":       ["营业收入\n[报告期] 去年三季\n[报表类型] 合并报表\n[单位] 亿元", "营业收入 亿元"],
    "净利润":         ["净利润\n[报告期] 去年三季\n[报表类型] 合并报表\n[单位] 亿元", "净利润 亿元"],
    "资产负债率":     ["资产负债率\n[报告期] 去年三季\n[单位] %", "资产负债率 %"],
    "财务报告期":     ["最新报告期\n[交易日期] 最新收盘日", "财务最新报告期"],
    "实际控制人":     ["实际控制人名称\n[日期] 2022-12-31", "实际控制人"],
}



# ──────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────

def _normalize_agency_name(value) -> str:
    """将评级机构的各种别名统一为标准全称。"""
    if not isinstance(value, str):
        return value
    value = value.strip()
    return AGENCY_NORMALIZE.get(value, value)


def _rename_bond_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    将债券 DataFrame 的原始列名映射到标准列名。
    找不到对应列的标准字段会被补充为 NaN 列。
    """
    col_map: dict[str, str] = {}
    for std_name, candidates in BOND_COLS.items():
        for candidate in candidates:
            if candidate in df.columns:
                col_map[candidate] = std_name
                break
    df = df.rename(columns=col_map)
    # 补全缺失的标准列（补 NaN，后续 DQC 会做进一步检查）
    for std_name in BOND_COLS:
        if std_name not in df.columns:
            df[std_name] = np.nan
    return df


# ──────────────────────────────────────────────
# 【DQC】数据质量检查(Staging层)
# ──────────────────────────────────────────────
from app.config import(
    BOND_SCHEMA_REQUIRED,
    BOND_CRITICAL_NULL_TOLERANCE,
    BOND_VALUE_LOW_NULL,
    BOND_NULL_WARN_THRESHOLD,
    BOND_VALUE_WARN_NULL,
    FISCAL_SCHEMA_REQUIRED,
    FISCAL_STRICT_NO_NULL,
    DQC_STATUS_PASS, 
    DQC_STATUS_WARN,
    DQC_STATUS_FAIL,

) 





def _dqc_bond(df: pd.DataFrame, source_file: Path) -> tuple[pd.DataFrame, list]:
    """
    【DQC】数据质量防御
    1. Schema Check: 确保核心列存在且不全为空。
    2. Value Check: 
       - 关键维度（发行人/省份）: 缺失率 < 2% 则剔除行并警告，> 2% 则报错。
       - 财务/评级数据: 缺失率 > 80% 则发出软警告。
    """
    file_label = source_file.name
    total_rows = len(df)
    if total_rows == 0:
        return df

    dqc_report = [] # <--- 新增：用于收集本批次的警告

    # ── 层级 1：Schema Check (Hard Check)──────────────────────────────────────
    
    # 1.1 列名缺失：Excel 中完全没有对应的列名
    missing_cols = [col for col in BOND_SCHEMA_REQUIRED if col not in df.columns]
    
    # 1.2 数据缺失：列名存在，但内容全为空 (NaN)
    # _rename_bond_columns 对所有缺失列补了 np.nan，
    # 所以真正"找不到对应原始列"的字段，在 df 中会是全 NaN 列。
    # 用 df[col].isna().all() 来判断是否真正缺失。
    empty_cols = [
        col for col in BOND_SCHEMA_REQUIRED 
        if col in df.columns and df[col].isna().all()
    ]

    # 抛出具体的异常信息
    if missing_cols or empty_cols:
        error_msg = f"[DQC · Schema Check] 债券文件「{file_label}」校验未通过：\n"
        
        if missing_cols:
            error_msg += f"核心列缺失（列名不存在）：{missing_cols}\n"
        if empty_cols:
            error_msg += f"数据全空（列名存在但无有效值）：{empty_cols}\n"
            
        error_msg += f" 请检查 {file_label} 文件或 BOND_COLS 映射配置。"
        raise ValueError(error_msg)

    # ── 层级 2：Value Check (Hard Check + Soft Warning) ────────────────────
    
    # 2.1 关键维度空值检查 (BOND_VALUE_LOW_NULL)
    for col in BOND_VALUE_LOW_NULL:
        if col not in df.columns: continue
        
        # 安全获取空值总数，防止 Series 转换错误
        raw_null_sum = df[col].isna().sum()
        null_count = int(raw_null_sum) if np.isscalar(raw_null_sum) else int(raw_null_sum.iloc[0])
        null_rate = null_count / total_rows
        
        if null_count > 0:
            if null_rate <= BOND_CRITICAL_NULL_TOLERANCE:
                # 容忍范围内：剔除并 Streamlit 警告
                msg = (
                    f"数据质量提示：债券文件「{file_label}」中「{col}」缺失 {null_count} 条记录（缺失率 {null_rate:.1%}），已自动剔除。"
                )
                logger.warning(msg)
                dqc_report.append(msg)
                st.warning(f"数据质量提示：债券文件「{file_label}」中「{col}」缺失 {null_count} 条记录（缺失率 {null_rate:.1%}），已自动剔除。")
                df = df.dropna(subset=[col])
            else:
                # 超过 2%：硬拦截
                null_rows = df[df[col].isna()].index[:5].tolist()
                raise ValueError(
                    f"数据质量提示： 债券文件「{file_label}」中「{col}」缺失率为 {null_rate:.1%}，"
                    f"已超过 {BOND_CRITICAL_NULL_TOLERANCE:.0%} 的硬限制，请检查上面提到的文件。\n"
                    f"空值样本行号：{null_rows}"
                )

    # 2.2 高空值率预警 (BOND_VALUE_WARN_NULL)
    # 更新 total_rows (因为上面可能删除了行)
    current_total = len(df)
    for col in BOND_VALUE_WARN_NULL:
        if col not in df.columns: continue
        null_rate = df[col].isna().sum() / current_total if current_total > 0 else 0
        
        if null_rate > BOND_NULL_WARN_THRESHOLD:
            msg = (
                    f"数据质量提醒：债券文件「{file_label}」字段「{col}」空值率达 {null_rate:.1%}，请确认导出是否完整。"
                )
            logger.warning(msg)
            dqc_report.append(msg)
            st.warning(f"数据质量提醒：债券文件「{file_label}」字段「{col}」空值率达 {null_rate:.1%}，请确认导出是否完整。")

    return df, dqc_report


def _dqc_fiscal(df: pd.DataFrame, source_path: Path) -> None:
    """
    【DQC】财力数据质量防御检查。
    
    财力文件结构简单，只做三项：
      1. Schema Check：必须有「城市」和「一般公共预算收入(亿元)」
      2. No-Null Check：「城市」、「一般公共预算收入(亿元)」不允许空值
      「城市」是与债券数据 JOIN 的 key，「一般公共预算收入(亿元)」需要用来表示地方财力水平
      3. Uniqueness Check (重复行检查，以防有两个同一城市行，后续在join函数发生数据膨胀)

    """
    file_label = source_path.name


    # ── 层级 1：Schema Check (核心列必须存在) ──────────────────────────
    missing_cols = [col for col in FISCAL_SCHEMA_REQUIRED if col not in df.columns]
    if missing_cols:
        raise ValueError(
            f"[DQC · Schema Check] 财力文件「{file_label}」存在问题：\n"
            f"  缺失核心列：{missing_cols}\n"
            f"  请检查 {file_label} 文件 "
        )

    # ── 层级 2：Value Check (空值内容检查) ──────────────────
    # 遍历 FISCAL_SCHEMA_REQUIRED，确保“城市”没有空值，以免在债券数据与财力数据匹配时出错
    for col in FISCAL_STRICT_NO_NULL:
        if col not in df.columns:
            continue
            
        # 使用健壮求和方式
        null_count = int(df[col].isna().values.flatten().sum())
        if null_count > 0:
            # 提取前 5 行空值样本，方便用户回溯 Excel
            null_rows = df[df[col].isna()].index[:5].tolist()
            
            raise ValueError(
                f"[DQC · Value Check] 财力文件「{file_label}」质量校验不通过：\n"
                f"  字段「{col}」包含 {null_count} 个空值，该字段在财力库中严禁为空。\n"
                f"  空值所在行号（前5条）：{null_rows}\n"
                f"  建议：财力数据总量较小，请务必在原始 Excel 中补全后再重新运行。"
            )
    # ── 层级 3：Uniqueness Check (重复行检查，以防有两个同一城市行而导致后续在join时发生数据膨胀) ──────────────────
    duplicate_cities = df[df["城市"].duplicated()]["城市"].unique().tolist()
    
    if duplicate_cities:
        # 记录到看板状态

        raise ValueError(
            f"[DQC · Uniqueness Check] 财力文件「{file_label}\n"
            f"发现重复的城市记录：{duplicate_cities}"
            f"请在原始 Excel 中删除重复行，"
            f"确保每个城市只有一行唯一的财力数据。"
        )
    

# ──────────────────────────────────────────────
# 公开接口：Staging 层核心函数
# ──────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def stg_load_bond_data(
    bond_file: Path,
    _mtime: float = 0.0,
    db_path: Path | None = None,
    _active_snapshot: str = "",
) -> pd.DataFrame:
    """
    【Staging / 缓存层】读取全国城投债数据。

    读取策略（优先级从高到低）：
      1. DuckDB 优先：若 db_path 存在且 v_bond 视图可用，从 DuckDB 读取
         - 速度：~120ms（vs Excel 的 6500ms，快 50 倍）
         - 数据已在 build_warehouse.py 阶段完成归一化和 DQC
         - _active_snapshot 作为缓存 key，版本切换时自动失效
      2. Excel 降级：若 DuckDB 不可用，回退到直接读取 Excel
         - 适用场景：首次部署前、build_warehouse.py 尚未运行时
         - _mtime 作为缓存 key，文件替换时自动失效

    缓存策略：
      DuckDB 路径：stg_load_bond_data(BOND_FILE, db_path=DB_PATH,
                                       _active_snapshot=version["active_bond_snapshot"])
      Excel  路径：stg_load_bond_data(BOND_FILE, _mtime=_file_mtime(BOND_FILE))
    """
    # ── 优先路径：从 DuckDB 读取（数据已预处理，速度极快）──────────
    if db_path is not None and db_path.exists():
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            df  = con.execute("SELECT * FROM v_bond").df()
            con.close()

            # 删除 build_warehouse 写入的元数据辅助列（对上层透明）
            meta_cols = [c for c in df.columns if c.startswith("_")]
            df = df.drop(columns=meta_cols, errors="ignore")

            logger.info(
                f"[Staging] DuckDB 读取成功：{len(df):,} 行 "
                f"（快照版本：{_active_snapshot}）"
            )
            return df

        except Exception as e:
            logger.warning(
                f"[Staging] DuckDB 读取失败，降级到 Excel：{e}  提示：请先运行 python scripts/build_warehouse.py 初始化数据仓库。"
            )

    # ── 降级路径：直接读取 Excel（首次部署或仓库未初始化时）────────
    logger.info(f"[Staging] Excel 降级读取：{bond_file.name}")
    df = pd.read_excel(bond_file, sheet_name=0)
    df = _rename_bond_columns(df)

    for col in ["主体评级机构", "债项评级机构"]:
        if col in df.columns:
            df[col] = df[col].apply(_normalize_agency_name)

    df["行政级别量化"] = df["城投行政级别"].map(LEVEL_ORDER).fillna(0)
    df["主体级别量化"] = df["主体评级"].map(RATING_ORDER).fillna(0)

    df, _ = _dqc_bond(df, bond_file)
    return df


@st.cache_data(show_spinner=False)
def stg_load_fiscal_data(
    fiscal_dir: Path,
    fiscal_year: str,
    province: str,
    _mtime: float = 0.0,
    db_path: Path | None = None,
    _active_snapshot: str = "",
) -> pd.DataFrame:
    """
    【Staging / 缓存层】读取单省财力数据。

    读取策略（与 stg_load_bond_data 对称）：
      1. DuckDB 优先：从 v_fiscal 视图按省份筛选
      2. Excel 降级：直接读取原始 Excel 文件

    缓存策略：
      DuckDB 路径：_active_snapshot 作为 key（版本切换时失效）
      Excel  路径：_mtime 作为 key（文件替换时失效）
    """
    # ── 优先路径：从 DuckDB 读取 ────────────────────────────────
    if db_path is not None and db_path.exists():
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            df  = con.execute(
                "SELECT 城市, \"一般公共预算收入(亿元)\" FROM v_fiscal WHERE _province = ?",
                [province]
            ).df()
            con.close()
            if not df.empty:
                logger.info(f"[Staging] DuckDB 读取财力成功：{province}（{len(df)} 行）")
                return df
            # 查询结果为空（该省无财力数据）→ 返回空表，行为与 Excel 路径一致
            return pd.DataFrame(columns=["城市", "一般公共预算收入(亿元)"])

        except Exception as e:
            logger.warning(f"[Staging] DuckDB 财力读取失败，降级到 Excel：{e}")

    # ── 降级路径：直接读取 Excel ────────────────────────────────
    path = fiscal_dir / f"{fiscal_year}年{province}财力.xlsx"
    if not path.exists():
        return pd.DataFrame(columns=["城市", "一般公共预算收入(亿元)"])

    df = pd.read_excel(path)

    col_map: dict[str, str] = {}
    for col in df.columns:
        if "地区" in str(col) or "城市" in str(col):
            col_map[col] = "城市"
        if "一般公共预算收入" in str(col):
            col_map[col] = "一般公共预算收入(亿元)"
    df = df.rename(columns=col_map)

    _dqc_fiscal(df, path)
    keep = [c for c in ["城市", "一般公共预算收入(亿元)"] if c in df.columns]
    return df[keep]