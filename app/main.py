# -*- coding: utf-8 -*-
"""
区域信评指标分析平台 v4.0  —  Streamlit Web App（dbt 分层重构版）
运行：streamlit run main.py
依赖：pip install streamlit pandas numpy openpyxl plotly xlsxwriter

架构说明（参考 dbt 分层思维）：
  models/staging/      → 原始 Excel 清洗与标准化
  models/intermediate/ → 多表关联（债券 × 财力）
  models/marts/        → 看板指标计算（聚合 / 格式化）
  main.py              → Streamlit UI 层，只调用 marts 的结果做渲染
"""

import io as _io
import math
import re
import json
from datetime import datetime
from pathlib import Path

import duckdb

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import logging
class ColorWarningFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        if record.levelno == logging.WARNING:
            return f"\033[31m{msg}\033[0m"  # 红色
        return msg
logging.getLogger().handlers.clear() #删掉之前的handler
#Streamlit 每次热重载都会重新执行main.py，每次执行都会追加一个新handler，时间长了还是会出现重复打印。
#留着 handlers.clear() 可以防止这个问题，没有副作用，保留更稳妥。

handler = logging.StreamHandler()
handler.setFormatter(ColorWarningFormatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.WARNING)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 导入三层模型 ──────────────────────────────
from models.staging.stg_data import (
    _file_mtime,
    _read_active_version,
    stg_load_bond_data,
    stg_load_fiscal_data,
)
from models.intermediate.int_data import (
    FISCAL_COL,
    int_filter_province,
    int_join_fiscal,          # 保留：DQC 检查仍在 Python 层执行
    int_build_issuer_view,    # 保留：sc_zt 供 Tab 1 地图 / Tab 2 筛选用
)
from models.marts.mart_credit_indicators import (
    FISCAL_BANDS,
    RATING_ORDER,
    LEVEL_ORDER,
    AGENCY_FULLNAME,
    mart_competition_matrix,  # 保留：四维热力矩阵，动态透视，不适合固化成 SQL
    mart_financial_bench,     # 保留：Tab 2 pandas 路径（DuckDB 降级时用）
    mart_partner_network,        # 保留：Tab 3 pandas 路径（DuckDB 降级时用）
    dqc_marts_platform_dismatch,  # 保留：省级平台财力适配审计
)

# ──────────────────────────────────────────────
# 路径配置
# ──────────────────────────────────────────────
_BASE       = Path(__file__).resolve().parent.parent

# ── 原始数据路径（Raw 层）────────────────────────────────────
def _latest_excel(directory: Path) -> Path:
    """返回目录下修改时间最新的 .xlsx 文件（DuckDB 不可用时的降级路径）。"""
    files = list(directory.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"目录 {directory} 下没有找到任何 xlsx 文件")
    return max(files, key=lambda p: p.stat().st_mtime)

BOND_FILE  = _latest_excel(_BASE / "data" / "1发债数据")
FISCAL_DIR = _BASE / "data" / "2财力数据"

# ── 数仓路径（Serving 层）────────────────────────────────────
DB_PATH      = _BASE / "data" / "serving" / "credit_indicators.duckdb"
VERSION_FILE = _BASE / "data" / "serving" / "active_version.json"

# ── 读取激活版本信息 ─────────────────────────────────────────
# active_version.json 由 build_warehouse.py 维护
# 不存在时（仓库未初始化）降级到 Excel 读取
_VERSION     = _read_active_version(VERSION_FILE)
FISCAL_YEAR  = _VERSION.get("active_fiscal_year", "2021")

# ── 数据截止日期（用于侧边栏显示）──────────────────────────
# DuckDB 模式：从版本号提取日期；Excel 降级模式：从文件名提取
_active_snap = _VERSION.get("active_bond_snapshot", "")
if _active_snap:
    BOND_DATA_DUE = datetime.strptime(_active_snap, "%Y%m%d").strftime("%Y年%m月%d日")
else:
    _date_str     = re.search(r"\d{8}", BOND_FILE.name)
    BOND_DATA_DUE = (datetime.strptime(_date_str.group(), "%Y%m%d").strftime("%Y年%m月%d日")
                     if _date_str else "未知")

FISCAL_DISPLAY = f"{FISCAL_YEAR}年一般公共预算收入(亿元)"

# ──────────────────────────────────────────────
# Plotly 工具栏配置
# ──────────────────────────────────────────────
PLOTLY_CFG = {
    "displayModeBar": True,
    "displaylogo": False,
    "modeBarButtonsToRemove": [
        "select2d", "lasso2d", "autoScale2d",
        "hoverClosestCartesian", "hoverCompareCartesian",
    ],
    "toImageButtonOptions": {"format": "png", "filename": "credit_chart"},
}

# ──────────────────────────────────────────────
# 页面配置 & 样式
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="区域信评分析平台",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    color: #2c3e50;
}
.top-bar {
    position: relative; margin-top: -50px !important;
    padding: 25px 28px; margin-bottom: 18px;
    background: #2d5a5e; border-radius: 10px; text-align: center;
}
.top-bar-title { font-size: 22px; font-weight: 700; color: #ffffff; letter-spacing: .8px; display: block; }
.top-bar-sub   { font-size: 14px; color: rgba(255,255,255,0.75); margin-top: 4px; display: block; }
.top-bar-tag   {
    position: absolute; right: 15px; bottom: 10px;
    font-size: 11px; padding: 2px 10px; border-radius: 4px;
    background: rgba(255,255,255,0.1); color: rgba(255,255,255,0.6);
    border: 1px solid rgba(255,255,255,0.2); pointer-events: none;
}
[data-testid="metric-container"] {
    background: #f4f8f7; border-radius: 8px; padding: 9px 14px;
    border: 1px solid #d1e0de; transition: all 0.3s ease;
}
[data-testid="metric-container"]:hover { border-color: #2d5a5e; box-shadow: 0 2px 8px rgba(45,90,94,0.1); }
[data-testid="stMetricLabel"] { font-size: 11px; color: #5e7e7c; }
[data-testid="stMetricValue"] { font-size: 15px; color: #6d6c66; font-weight: 550; }

/* 修改标签 (Label)：穿透到内部的 p 标签 */
[data-testid="stMetricLabel"] p {
    font-size: 17px !important;
    color: #5e7e7c !important;
    font-weight: 500 !important; 
    margin-bottom: 0.5px !important; /* 消除不必要的间距 */
}
/* 修改数值 (Value)：穿透到内部的 div */
[data-testid="stMetricValue"] > div {
    font-size: 17px !important;   /* 调大一点，15px 在大屏幕上偏小 */
    color: #6d6c66 !important;
    font-weight: 600 !important;   /* 稍微加粗提高可读性 */
}


[data-testid="column"]  { display: flex !important; justify-content: center !important; flex: 1 !important; width: 100% !important; }
[data-testid="stMetric"] { width: 100% !important; display: flex !important; justify-content: center !important; }
[data-testid="metric-container"] { width: 90% !important; text-align: center !important; margin: 0 auto !important; }
[data-testid="stMetricLabel"], [data-testid="stMetricValue"] { width: 100% !important; display: flex !important; justify-content: center !important; }
[data-testid="stTabs"] { width: 100% !important; }
.stTabs [data-baseweb="tab-list"] { display: flex !important; width: 100% !important; justify-content: space-between !important; gap: 0px !important; border-bottom: 2px solid #d1e0de; }
.stTabs [data-baseweb="tab"] { flex: 1 !important; text-align: center !important; justify-content: center !important; padding: 10px 0px !important; margin: 0px !important; font-size: 20px !important; font-weight: 600 !important; border-radius: 6px 6px 0 0; color: #1a3a3a; border-bottom: 2px solid transparent !important; }
.stTabs [aria-selected="true"] { color: #1a3a3a !important; border-bottom: 2px solid #1a3a3a !important; }
.stTabs button[data-baseweb="tab"] div {
    font-size: 17px !important; 
    font-weight: 600 !important;
}
/* 为选中的 Tab 增加边框效果 */
.stTabs [aria-selected="true"] {
    /* 设置边框：颜色建议和顶部的墨绿色 #1a3a3a 一致 */
    border: 1.5px solid #1a3a3a !important; 
    
    /* 修正圆角：保持顶部圆润，底部平齐以连接下划线 */
    border-radius: 6px 6px 0 0 !important; 
    
    /* 内部间距微调：防止边框挤压文字 */
    padding-top: 20px !important;
    padding-bottom: 15px !important;
    padding-left: 15px !important;
    padding-right: 15px !important;
    
    
    /* 背景色微调（可选）：让选中的 Tab 稍微有个色差 */
    background-color: rgba(26, 58, 58, 0.05) !important; 
    
    /* 消除底部边框（可选）：如果你希望它和下面的横线融为一体 */
    border-bottom: none !important; 
}

/* 为了让框框更明显，可以给未选中的 Tab 设置透明边框，防止抖动 */
.stTabs [data-baseweb="tab"] {
    border: 1.5px solid transparent !important;
}
/* 重要：未选中的 Tab 必须也要撑大，否则布局会错位 */
.stTabs [data-baseweb="tab"] {
    padding-top: 20px !important;
    padding-bottom: 15px !important;
    padding-left: 15px !important;
    padding-right: 15px !important;
}


div[data-baseweb="tab-border"]    { background-color: #d1e0de !important; }
[data-testid="stAlert"] { padding: 0.4rem 1rem !important; }
[data-testid="stAlert"] p { margin: 0 !important; }
.sec-title { font-size: 18px; font-weight: 600; color: #1a3a3a; margin: 5px 0 5px; }
.sec-note  { font-size: 15px; color: #6a8a88; margin-bottom: 8px; line-height: 1.5; }
.rec-g { background: #f1f8f1; border-left: 4px solid #3d8c40; padding: 10px 15px; border-radius: 6px; margin-bottom: 8px; color: #1a3a3a; }
.rec-a { background: #fdf9f0; border-left: 4px solid #d4a574; padding: 10px 15px; border-radius: 6px; margin-bottom: 8px; color: #8b6f4e; }
.rec-r { background: #fff5f5; border-left: 4px solid #cc785c; padding: 10px 15px; border-radius: 6px; margin-bottom: 8px; color: #3d2b1f; }
.rec-n { background: #f9f7f3; border-left: 4px solid #a0846a; padding: 10px 15px; border-radius: 6px; margin-bottom: 8px; color: #3d2b1f; }
hr { border: none; border-top: 1px solid #d1e0de; margin: 10px 0; }
[data-testid="stSidebar"] { background-color: #f8faf9; }
div.stButton > button:first-child { background-color: #E0E6E5; color: #000000; border: none; border-radius: 6px; }
div.stButton > button:hover       { background-color: #E0E6E5; border: none; }
div[data-baseweb="select"] > div:focus-within, input:focus, textarea:focus {
    border-color: #2d5a5e !important; box-shadow: 0 0 0 1px #2d5a5e !important;
}
[data-baseweb="radio"] input:checked ~ div { border-color: #1a3a3a !important; background-color: #1a3a3a !important; }
div[data-baseweb="slider"] > div > div { background-color: #2d5a5e !important; }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────
# 颜色常量（UI 层专用）
# ──────────────────────────────────────────────
PIE_COLORS = [
    "#8B6F4E", "#2D5A5E", "#D4A574", "#6D6C66", "#8DA39F",
    "#7CA4A2", "#A3C1BF", "#C1D3D2", "#D1E0DE", "#344E4D", "#8DA39F",
]
RATING_COLORS = {
    "AAA": "#388e3c", "AA+": "#1565c0", "AA": "#e65100",
    "AA-": "#b71c1c", "A+": "#6a1b9a", "A": "#37474f",
}

# ──────────────────────────────────────────────
# 城市坐标库（UI 层专用，供地图渲染）
# ──────────────────────────────────────────────
CITY_COORDS = {
    "成都市": (104.07, 30.67), "绵阳市": (104.68, 31.47), "宜宾市": (104.62, 28.77),
    "泸州市": (105.43, 28.87), "南充市": (106.08, 30.79), "达州市": (107.47, 31.22),
    "乐山市": (103.76, 29.58), "遂宁市": (105.57, 30.52), "德阳市": (104.40, 31.13),
    "自贡市": (104.77, 29.35), "内江市": (105.07, 29.59), "眉山市": (103.83, 30.05),
    "广安市": (106.63, 30.47), "广元市": (105.84, 32.43), "攀枝花市": (101.72, 26.58),
    "雅安市": (103.01, 30.01), "资阳市": (104.63, 30.13), "凉山彝族自治州": (102.27, 27.90),
    "巴中市": (106.74, 31.85), "甘孜藏族自治州": (101.97, 30.05), "阿坝藏族羌族自治州": (102.22, 31.90),
    "济南市": (117.00, 36.67), "青岛市": (120.38, 36.07), "烟台市": (121.39, 37.54),
    "潍坊市": (119.16, 36.71), "临沂市": (118.36, 35.06), "济宁市": (116.59, 35.41),
    "淄博市": (118.05, 36.81), "泰安市": (117.09, 36.19), "威海市": (122.12, 37.51),
    "菏泽市": (115.46, 35.23), "聊城市": (115.97, 36.46), "日照市": (119.46, 35.42),
    "枣庄市": (117.32, 34.81), "东营市": (118.67, 37.43), "滨州市": (117.97, 37.38),
    "德州市": (116.29, 37.45),
    "南京市": (118.78, 32.04), "苏州市": (120.62, 31.32), "无锡市": (120.30, 31.57),
    "南通市": (120.86, 32.01), "常州市": (119.97, 31.79), "徐州市": (117.18, 34.27),
    "扬州市": (119.41, 32.39), "泰州市": (119.90, 32.46), "镇江市": (119.44, 32.20),
    "淮安市": (119.02, 33.60), "盐城市": (120.16, 33.35), "连云港市": (119.22, 34.60),
    "宿迁市": (118.28, 33.96),
    "杭州市": (120.15, 30.28), "宁波市": (121.55, 29.87), "温州市": (120.67, 28.00),
    "绍兴市": (120.58, 30.03), "金华市": (119.65, 29.08), "台州市": (121.42, 28.66),
    "嘉兴市": (120.76, 30.77), "湖州市": (120.10, 30.87), "衢州市": (118.87, 28.97),
    "舟山市": (122.21, 29.99), "丽水市": (119.92, 28.47),
    "广州市": (113.26, 23.13), "深圳市": (114.06, 22.54), "东莞市": (113.75, 23.05),
    "佛山市": (113.12, 23.02), "中山市": (113.39, 22.52), "珠海市": (113.58, 22.27),
    "惠州市": (114.41, 23.11), "汕头市": (116.68, 23.35), "韶关市": (113.59, 24.80),
    "江门市": (113.08, 22.58), "湛江市": (110.36, 21.27), "茂名市": (110.93, 21.66),
    "昆明市": (102.71, 25.04), "曲靖市": (103.80, 25.49), "玉溪市": (102.55, 24.35),
    "保山市": (99.17, 25.11),  "昭通市": (103.72, 27.34), "丽江市": (100.23, 26.87),
    "普洱市": (100.97, 22.79), "临沧市": (100.08, 23.89),
    "楚雄彝族自治州": (101.55, 25.04), "红河哈尼族彝族自治州": (103.38, 23.37),
    "文山壮族苗族自治州": (104.24, 23.37), "西双版纳傣族自治州": (100.80, 22.01),
    "大理白族自治州": (100.23, 25.59), "德宏傣族景颇族自治州": (98.58, 24.44),
    "怒江傈僳族自治州": (98.85, 25.85), "迪庆藏族自治州": (99.71, 27.83),
    "上海市": (121.47, 31.23), "北京市": (116.40, 39.90),
    "天津市": (117.20, 39.13), "重庆市": (106.55, 29.56),
}


# ══════════════════════════════════════════════
# UI 辅助函数（纯渲染逻辑，不含业务计算）
# ══════════════════════════════════════════════

def _available_provinces() -> list[str]:
    if not FISCAL_DIR.exists():
        return []
    return sorted(
        p.stem.replace(f"{FISCAL_YEAR}年", "").replace("财力", "")
        for p in FISCAL_DIR.glob(f"{FISCAL_YEAR}年*财力.xlsx")
    )


def _cities_by_fiscal(sc_zt: pd.DataFrame) -> list[str]:
    if FISCAL_COL not in sc_zt.columns or "城市" not in sc_zt.columns:
        return sc_zt["城市"].dropna().unique().tolist()
    fiscal_map = (
        sc_zt[["城市", FISCAL_COL]].dropna(subset=[FISCAL_COL])
        .drop_duplicates("城市").set_index("城市")[FISCAL_COL].to_dict()
    )
    return sorted(sc_zt["城市"].dropna().unique().tolist(), key=lambda c: fiscal_map.get(c, 0), reverse=True)


def _score_val(val: float, bench: dict, col: str) -> int:
    if col == "资产负债率":
        return 3 if val <= bench["q1"] else (2 if val <= bench["median"] else (1 if val <= bench["q3"] else 0))
    return 3 if val >= bench["q3"] else (2 if val >= bench["median"] else (1 if val >= bench["q1"] else 0))


# ──────────────────────────────────────────────
# 图表渲染函数（UI 层）
# ──────────────────────────────────────────────

def fig_pie(sc_pj: pd.DataFrame) -> go.Figure:
    df = sc_pj[sc_pj["主体数"] > 0].copy()
    fig = go.Figure(go.Pie(
        labels=df["评级机构"], values=df["主体数"], hole=0.42,
        marker=dict(colors=PIE_COLORS[:len(df)], line=dict(color="#fff", width=1.5)),
        textinfo="percent", textfont_size=13,
        hovertemplate="%{label}<br>主体数：%{value}<br>占比：%{percent}<extra></extra>",
    ))
    fig.update_layout(height=280, showlegend=True,
                      legend=dict(orientation="v", font=dict(size=13), x=1.02, y=0.5),
                      margin=dict(t=10, b=10, l=10, r=100))
    return fig


def fig_bar_ratio(sc_pj: pd.DataFrame) -> go.Figure:
    df = sc_pj[sc_pj["主体数"] > 0].copy()
    if df.empty:
        return go.Figure()
    df["ratio"] = df["债项数"] / df["主体数"]
    inv_map = {v: k for k, v in AGENCY_FULLNAME.items()}
    df["简称"] = df["评级机构"].map(inv_map).fillna(df["评级机构"].str[:4])
    fig = go.Figure(go.Bar(
        x=df["简称"], y=df["ratio"], width=0.35,
        marker_color="#2D5A5E", marker_line_width=0,
        text=df["ratio"].map("{:.2f}".format), textposition="outside",
        textfont=dict(color="#1A3A3A", size=13),
        hovertemplate="<b>%{x}</b><br>业务深度: %{y:.2f}<extra></extra>",
    ))
    fig.update_layout(
        height=280, margin=dict(t=40, b=40, l=10, r=10),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",# 将背景设为透明
        yaxis=dict(visible=False, range=[0, df["ratio"].max() * 1.25]),
        xaxis=dict(showgrid=False, tickfont=dict(size=13, color="#5E7E7C"), linecolor="#D1E0DE"),
        dragmode=False, hoverlabel=dict(bgcolor="#F4F8F7", bordercolor="#2D5A5E"),
    )
    return fig


def fig_comp_heatmap(df_sc: pd.DataFrame, my_agency: str, dimension: str):
    matrix, ordered_agencies, categories = mart_competition_matrix(df_sc, my_agency, dimension)
    if not ordered_agencies:
        return None, {}, [], []

    y_labels = (
        ([my_agency + " ◀"] if my_agency in ordered_agencies else []) +
        [ag for ag in ordered_agencies if ag != my_agency]
    )
    z_matrix = [[matrix[ag][cat] for cat in categories] for ag in ordered_agencies]
    max_v = max((v for row in z_matrix for v in row), default=1) or 1

    col_max_idx = {}
    for ci in range(len(categories)):
        col_vals = [z_matrix[ri][ci] for ri in range(len(ordered_agencies))]
        max_val = max(col_vals)
        if max_val > 0:
            col_max_idx[ci] = ([i for i, v in enumerate(col_vals) if v == max_val], max_val)

    fig = go.Figure(go.Heatmap(
        z=z_matrix, x=categories, y=y_labels,
        colorscale=[[0, "#F4F8F7"], [0.3, "#A3C1BF"], [1, "#1A3A3A"]],
        showscale=False, zmin=0, zmax=max_v, xgap=2, ygap=2,
        hovertemplate="%{y}<br>%{x}: %{z}家<extra></extra>",
    ))

    annotations = []
    for ri, ag in enumerate(ordered_agencies):
        for ci, cat in enumerate(categories):
            v = z_matrix[ri][ci]
            is_col_max = (ci in col_max_idx and ri in col_max_idx[ci][0] and v > 0)
            fc = "#ffffff" if v > max_v * 0.3 else "#8b6f4e"
            annotations.append(dict(
                x=cat, y=y_labels[ri],
                text=f"<b>{v}</b>" if is_col_max else (str(v) if v > 0 else ""),
                showarrow=False, font=dict(size=12, color=fc),
                xref="x", yref="y", xanchor="center", yanchor="middle",
            ))

    shapes = [
        dict(type="rect", xref="x", yref="y",
             x0=categories[ci], x1=categories[ci],
             y0=y_labels[ri_max], y1=y_labels[ri_max],
             fillcolor="rgba(204,120,92,0.88)", line=dict(width=0), layer="below")
        for ci, (ris_max, _) in col_max_idx.items()
        for ri_max in ris_max
    ]

    fig.update_layout(
        annotations=annotations, shapes=shapes,
        height=max(300, len(ordered_agencies) * 36 + 80),
        margin=dict(t=80, b=10, l=10, r=10),
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=False, side="top", tickangle=0, tickfont=dict(size=13)),
        yaxis=dict(showgrid=False, autorange="reversed", tickfont=dict(size=13)),
    )
    return fig, matrix, ordered_agencies, categories


def build_map_fig(sc_zt: pd.DataFrame, filter_lv="", filter_rt="", fiscal_cities=None) -> go.Figure:
    df = sc_zt.copy()
    if filter_lv: df = df[df["城投行政级别"] == filter_lv]
    if filter_rt: df = df[df["主体评级"] == filter_rt]

    all_cities    = set(sc_zt["城市"].dropna().unique()) | (set(fiscal_cities) if fiscal_cities else set())
    active_cities = set(df["城市"].dropna().unique()) if not df.empty else set()
    empty_cities  = all_cities - active_cities

    def simplify(name):
        for s in ["攀枝花", "西双版纳", "呼和浩特", "大兴安岭", "鄂尔多斯"]:
            if s in name: return s
        return name[:2]
    
    # 提取全省所有城市的财力（去重），并算好排名字典
    city_fiscal = df.groupby("城市")[FISCAL_COL].first().dropna() #在分组之后，取每一组出现的第一个财力数据非空值。
    rank_dict = city_fiscal.rank(ascending=False, method="min").to_dict()
    total_count = len(city_fiscal)
    fig = go.Figure()

    if not df.empty:
        rows = []
        for city, sub in df.groupby("城市"):
            if city not in CITY_COORDS: continue
            rk = int(rank_dict.get(city, 0))
            rk_str = f"{rk}/{total_count}" if rk > 0 else "NaN"
            fs = sub[FISCAL_COL].iloc[0] if FISCAL_COL in sub.columns else None
            top_rt = sorted(sub["主体评级"].dropna().unique(), key=lambda r: RATING_ORDER.get(r, -1), reverse=True)
            top_rt = top_rt[0] if top_rt else "未知"
            fs_str = f"{fs:.0f}亿" if fs and not math.isnan(float(fs)) else "—"

            lines = [f"<b>{city}</b>", 
                    f"一般公共预算收入：{fs_str}", 
                    f"地区财力排名：全省第{rk_str}", 
                    f"主体总数：{len(sub)}", 
                    "──────────"]
            for rt in sorted(sub["主体评级"].dropna().unique(), key=lambda r: RATING_ORDER.get(r, -1), reverse=True):
                rt_s = sub[sub["主体评级"] == rt]
                lines.append(f"<b>{rt}</b> {len(rt_s)}家：")
                for lv in sorted(rt_s["城投行政级别"].dropna().unique(), key=lambda l: LEVEL_ORDER.get(l, -1), reverse=True):
                    lines.append(f"  其中{lv} {rt_s[rt_s['城投行政级别']==lv].shape[0]}家")
            lon, lat = CITY_COORDS[city]
            rows.append({"城市": city, "lon": lon, "lat": lat, "n": len(sub), "top_rt": top_rt, "hover": "<br>".join(lines)})
        if rows:
            map_df = pd.DataFrame(rows)
            for rt, color in RATING_COLORS.items():
                sub_m = map_df[map_df["top_rt"] == rt]
                if sub_m.empty: continue
                fig.add_trace(go.Scattergeo(
                    lon=sub_m["lon"], lat=sub_m["lat"], text=sub_m["hover"],
                    hovertemplate="%{text}<extra></extra>", hoverlabel=dict(align="left"),
                    mode="markers",
                    marker=dict(size=sub_m["n"].apply(lambda n: max(8, min(40, 8 + n * 3.5))),
                                color=color, opacity=0.78, line=dict(width=1.2, color="white"), sizemode="diameter"),
                    name=rt, showlegend=True,
                ))
            fig.add_trace(go.Scattergeo(lon=map_df["lon"], lat=map_df["lat"],
                                        text=map_df["城市"].apply(simplify),
                                        mode="text", textfont=dict(size=9, color="#333"),
                                        showlegend=False, hoverinfo="skip"))
    emp_rows = [{"城市": c, "lon": CITY_COORDS[c][0], "lat": CITY_COORDS[c][1]} for c in empty_cities if c in CITY_COORDS]
    if emp_rows:
        emp_df = pd.DataFrame(emp_rows)
        fig.add_trace(go.Scattergeo(lon=emp_df["lon"], lat=emp_df["lat"], text=emp_df["城市"],
                                    hovertemplate="%{text}<br>暂无公开发债的被评主体<extra></extra>",
                                    mode="markers",
                                    marker=dict(size=10, color="#bdbdbd", opacity=0.5, line=dict(width=1, color="white"), sizemode="diameter"),
                                    name="暂无被评主体", showlegend=True))
        fig.add_trace(go.Scattergeo(lon=emp_df["lon"], lat=emp_df["lat"],
                                    text=emp_df["城市"].apply(simplify),
                                    mode="text", textfont=dict(size=8, color="#9e9e9e"),
                                    showlegend=False, hoverinfo="skip"))

    all_coords = {c: CITY_COORDS[c] for c in all_cities if c in CITY_COORDS}
    #地图中心定位（Auto-centering）
    cx = sum(v[0] for v in all_coords.values()) / len(all_coords) if all_coords else 104
    cy = sum(v[1] for v in all_coords.values()) / len(all_coords) if all_coords else 30
    fig.update_geos(visible=True, resolution=50, showland=True, landcolor="#f5f5f0",
                    showcountries=False, showcoastlines=False, showlakes=False, showrivers=False,
                    showsubunits=True, subunitcolor="#ccc", subunitwidth=0.5,
                    center=dict(lon=cx, lat=cy), projection_scale=8)
    fig.update_layout(height=500, margin=dict(t=10, b=10, l=0, r=0),
                      legend=dict(title="最高信用级别", orientation="v", x=0.01, y=0.98,
                                  bgcolor="rgba(255,255,255,.88)", bordercolor="#ddd", borderwidth=0.5),
                      paper_bgcolor="rgba(0,0,0,0)")
    return fig


# ──────────────────────────────────────────────
# 导出工具
# ──────────────────────────────────────────────

def build_html_report(case_disp, agency, province, city, lv, rt, fiscal_year) -> str:
    filters = []
    if city and city != "全部城市":       filters.append(f"城市：{city}")
    if lv   and lv   != "全部级别":       filters.append(f"行政级别：{lv}")
    if rt   and rt   != "全部信用级别":   filters.append(f"主体评级：{rt}")
    filter_str = "　·　".join(filters) if filters else "全部（无筛选）"
    cols = list(case_disp.columns)
    th_html = "".join(f"<th>{c}</th>" for c in cols)
    rows_html = []
    for i, row in case_disp.iterrows():
        tds = ["<td>—</td>" if pd.isna(row[c]) else (f"<td>{row[c]:.2f}</td>" if isinstance(row[c], float) else f"<td>{row[c]}</td>") for c in cols]
        rows_html.append(f"<tr{' class=\"alt\"' if i%2==1 else ''}>{''.join(tds)}</tr>")
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>{agency} — {province}发债案例报告</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f8f7;color:#1a3a3a;font-size:13px}}.header{{background:linear-gradient(90deg,#1a3a3a,#2d5a5e 55%,#4a7c7a);padding:24px 36px;color:#fff}}.header h1{{font-size:20px;font-weight:700}}.header p{{font-size:12px;color:rgba(255,255,255,.72);margin-top:5px}}.meta{{display:flex;gap:20px;flex-wrap:wrap;padding:14px 36px;background:#fff;border-bottom:1px solid #d1e0de;font-size:12px}}.meta-item{{color:#5e7e7c}}.meta-item b{{color:#1a3a3a}}.wrap{{padding:20px 36px 40px}}.table-title{{font-size:14px;font-weight:600;margin-bottom:10px;padding-bottom:6px;border-bottom:2px solid #1a3a3a}}table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:12px}}thead tr{{background:#1a3a3a;color:#fff}}th{{padding:9px 10px;text-align:left;font-weight:600;white-space:nowrap}}td{{padding:8px 10px;border-bottom:1px solid #edf2f2;white-space:nowrap}}tr.alt td{{background:#f7faf9}}tr:hover td{{background:#edf5f4}}.footer{{padding:14px 36px;font-size:11px;color:#9ab8b6;border-top:1px solid #d1e0de;text-align:right}}</style></head><body>
<div class="header"><h1>{agency} — {province}发债案例报告</h1><p>区域信评展业引擎 · 数据仅供内部使用</p></div>
<div class="meta"><div class="meta-item">评级机构：<b>{agency}</b></div><div class="meta-item">展业省份：<b>{province}</b></div><div class="meta-item">财力数据年份：<b>{fiscal_year}年</b></div><div class="meta-item">筛选条件：<b>{filter_str}</b></div><div class="meta-item">记录总数：<b>{len(case_disp)} 条</b></div></div>
<div class="wrap"><div class="table-title">发债明细</div><table><thead><tr>{th_html}</tr></thead><tbody>{"".join(rows_html)}</tbody></table></div>
<div class="footer">生成时间：导出自区域信评展业引擎 · 仅供内部使用，请勿对外传播</div></body></html>"""

# ══════════════════════════════════════════════
# DuckDB 查询工具函数
# ══════════════════════════════════════════════

def query_duckdb(sql: str, params: list | None = None) -> pd.DataFrame:
    """
    对 DuckDB 服务文件执行一次性只读查询，返回 DataFrame。

    每次调用都新建连接、查完立即关闭。
    设计原因：
      - DuckDB 连接对象不可序列化，无法存入 @st.cache_data
      - read_only=True 允许多个进程同时读取同一个 .duckdb 文件
      - 每次查询都是独立事务，无连接状态污染风险

    参数：
      sql     待执行的 SQL 字符串（可含 ? 占位符）
      params  对应 ? 占位符的参数列表，默认 None

    返回：
      查询结果 DataFrame；若 DuckDB 文件不存在则返回空 DataFrame
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()

# 写一个通用的动态 WHERE 拼接函数       
def _build_where(conditions: dict) -> tuple[str, list]:
    """
    构建参数化 WHERE 子句。

    参数：
        conditions: {"列名": "值", ...}，值为空字符串或 None 表示跳过该条件

    返回：
        (where_clause, params_list)

    示例：
        _build_where({"城市": "成都", "主体评级": ""})
        返回: (" WHERE 城市 = ?", ["成都"])
    """
    parts = []
    params = []
    for col, val in conditions.items():
        if val:  # 空字符串或 None 跳过
            parts.append(f"{col} = ?")
            params.append(val)
    
    where_clause = " AND ".join(parts)
    return (f" WHERE {where_clause}" if where_clause else "", params)


def _duckdb_available() -> bool:
    """检查 DuckDB 仓库是否已初始化（v_issuer_profile 视图是否可用）。"""
    if not DB_PATH.exists():
        return False
    try:
        con = duckdb.connect(str(DB_PATH), read_only=True)
        con.execute("SELECT 1 FROM v_issuer_profile LIMIT 1")
        con.close()
        return True
    except Exception:
        return False




# ══════════════════════════════════════════════
# 数据编排：调用三层模型
# ══════════════════════════════════════════════

def load_bond_data() -> pd.DataFrame:
    """
    Staging 层入口：读取全国城投债。

    读取策略（由 stg_load_bond_data 内部决定）：
      - DuckDB 可用（build_warehouse.py 已运行）→ 从 DuckDB 视图读取，~120ms
      - DuckDB 不可用 → 降级到 Excel 读取，~6500ms

    缓存 key 设计：
      - DuckDB 路径：_active_snapshot 版本号变化时失效
      - Excel  路径：_mtime 文件修改时间变化时失效
    """
    return stg_load_bond_data(
        BOND_FILE,
        _mtime=_file_mtime(BOND_FILE),          # Excel 降级路径的缓存 key
        db_path=DB_PATH,                         # DuckDB 服务文件路径
        _active_snapshot=_active_snap,           # DuckDB 路径的缓存 key（快照版本号）
    )


def run_analysis(df_qg: pd.DataFrame, province: str) -> dict | None:
    """
    编排三层调用，生成单省完整分析结果。

    数据流（思路 A：SQL 视图驱动）：
    ┌─────────────────────────────────────────────────────┐
    │  Staging   stg_load_bond_data / stg_load_fiscal     │
    │            DuckDB 优先，Excel 自动降级               │
    ├─────────────────────────────────────────────────────┤
    │  Intermediate  int_filter_province（pandas 切片）   │
    │                int_join_fiscal（pandas JOIN + DQC）  │
    │                int_build_issuer_view（pandas 去重）  │
    │                → sc_zt 供 Tab 1 地图 / Tab 2 筛选   │
    ├─────────────────────────────────────────────────────┤
    │  Marts（DuckDB 视图优先，pandas 自动降级）           │
    │    agencies    ← v_agency_market_share WHERE 省份   │
    │    underwriters← m_underwriter_stats(p_province)   │
    │    actual_ags  ← v_agency_competitive_landscape    │
    │    sc_zt       ← v_issuer_profile WHERE 省份        │
    └─────────────────────────────────────────────────────┘

    DQC 策略：
      - Staging DQC：在 stg_* 函数内部执行（不变）
      - Intermediate DQC：int_join_fiscal 内部执行（不变）
      - Marts DQC：dqc_marts_platform_dismatch 由调用方（trigger block）执行（不变）
    """
    prov_keyword = province[:2]   # 用于 SQL LIKE 筛选，如 "四川"

    # ── ① Staging：财力数据（双模式，缓存由 stg_* 管理）─────────
    fiscal_path = FISCAL_DIR / f"{FISCAL_YEAR}年{province}财力.xlsx"
    df_fiscal = stg_load_fiscal_data(
        FISCAL_DIR, FISCAL_YEAR, province,
        _mtime=_file_mtime(fiscal_path),
        db_path=DB_PATH,
        _active_snapshot=_active_snap,
    )

    # ── ② Intermediate：DQC 保留在 Python 层 ────────────────────
    # int_filter_province / int_join_fiscal 负责关联完整性审计（膨胀率 + 覆盖率）
    # sc_zt 同时作为 Tab 1 地图、Tab 2 准入门槛的 pandas 数据源
    df_province = int_filter_province(df_qg, province)
    if df_province.empty:
        return None
    df_province = int_join_fiscal(df_province, df_fiscal)
    sc_zt       = int_build_issuer_view(df_province)

    # ── ③ Marts：DuckDB 视图优先，pandas 自动降级 ────────────────

    if _using_duckdb:

        # ── 省份 KPI（v_province_kpi）──
        province_kpi = query_duckdb(
            "SELECT * FROM v_province_kpi WHERE 省份 LIKE ?",
            [f"%{prov_keyword}%"]
        )

        # ── 评级机构市场格局（v_agency_market_share）──
        agencies = query_duckdb(
            "SELECT * FROM v_agency_market_share WHERE 省份 LIKE ? ORDER BY 主体数 DESC",
            [f"%{prov_keyword}%"]
        )

        # ── 主承销商排名（v_underwriter_stats）──
        underwriters = query_duckdb(
            "SELECT * FROM v_underwriter_stats WHERE 省份 LIKE ? ORDER BY 序号 ",
            [f"%{prov_keyword}%"]
        )

        # ── 竞争热力矩阵可用机构列表（v_agency_competitive_landscape）──
        # mart_competition_matrix 仍用 pandas（四维动态透视，实时性要求高）
        # 但机构列表从 DuckDB 视图取，避免重复扫描 df_province
        ags_df = query_duckdb(
            "SELECT DISTINCT 主体评级机构 FROM v_agency_competitive_landscape "
            "WHERE 省份 LIKE ? AND 主体评级机构 IS NOT NULL",
            [f"%{prov_keyword}%"]
        )
        actual_ags = ags_df["主体评级机构"].tolist() if not ags_df.empty else []

        # ── sc_zt 升级为 DuckDB 版本（含财力列）──
        # 替换 int_build_issuer_view 的 pandas 结果，
        # v_issuer_profile = SELECT * FROM int_v_issuer_enriched，含城市财力_亿元和城市财力区间
        sc_zt_db = query_duckdb(
            "SELECT * FROM v_issuer_profile WHERE 省份 LIKE ? "
            "ORDER BY 城市财力_亿元 DESC NULLS LAST, 行政级别量化 DESC, 主体级别量化 DESC",
            [f"%{prov_keyword}%"]
        )
        # 若 DuckDB 查询有结果则用 DuckDB 版本，否则回退到 pandas 版本
        if not sc_zt_db.empty:
            sc_zt = sc_zt_db

    else:
        # ── pandas 降级路径（DuckDB 未初始化时自动切换）──
        from models.marts.mart_credit_indicators import mart_agency_stats, mart_underwriter_stats
        province_kpi = pd.DataFrame()  # 降级时 KPI 走 pandas 分支
        agencies     = mart_agency_stats(df_province)
        underwriters = mart_underwriter_stats(df_province)
        actual_ags   = mart_competition_matrix(df_province, "", "fiscal")[1]

    return {
        "province":      province,
        "province_kpi":  province_kpi,
        "df_raw":        df_province,   # 全量省份债项宽表（Tab 0 热力矩阵 / Tab 4 发债案例）
        "sc_zt":         sc_zt,         # 去重主体视图（Tab 1 地图 / Tab 2 准入门槛）
        "agencies":      agencies,      # 评级机构格局（Tab 0 排名表 / 饼图 / 柱图）
        "underwriters":  underwriters,  # 主承销商排名（Tab 3）
        "actual_ags":    actual_ags,    # 机构列表（Tab 0 热力矩阵 / Tab 3 帮手圈机构选择器）
    }


# ══════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════
with st.sidebar:
    st.markdown("### ⚙️ 分析设置")
    provs = _available_provinces()
    province_input = st.selectbox("选择展业省份", provs, index=0) if provs else st.text_input("目标省份（精确名称）", value="四川省")
    run_btn = st.button("▶开始分析", type="primary", use_container_width=True)
    st.markdown("---")
    # ── 数据源模式指示 ────────────────────────────────────
    _using_duckdb = DB_PATH.exists() and bool(_active_snap)
    _mode_icon    = "" if _using_duckdb else ""
    _mode_label   = "DuckDB 仓库模式" if _using_duckdb else "Excel 降级模式"
    st.caption(f"{_mode_icon} 数据读取模式：{_mode_label}")
    if _using_duckdb:
        st.caption(f"激活快照版本：`{_active_snap}`")
        _avail = _VERSION.get("bond_snapshots_available", [])
        if len(_avail) > 1:
            st.caption(f"可用快照：{', '.join(_avail)}")
    else:
        st.caption(f"债券数据：`{BOND_FILE.name}`")
        st.caption("⚠️ 提示：运行 `python scripts/build_warehouse.py`")
        st.caption("可切换至 DuckDB 高性能模式")
    st.caption(" ")
    st.caption("数据来源说明")
    st.caption(f"债券市场数据来源：`WIND 截止 {BOND_DATA_DUE}`")
    st.caption(f"地方财力数据来源：`财汇数据库 {FISCAL_YEAR}年度数据`")

# ══════════════════════════════════════════════
# 顶部横幅
# ══════════════════════════════════════════════
st.markdown("""
<div class="top-bar">
  <div>
    <div class="top-bar-title">区域信评展业引擎</div>
    <div class="top-bar-sub">信评展业分析 · 评级机构格局 · 准入门槛模拟 · 展业帮手圈</div>
  </div>
  <span class="top-bar-tag">内部使用</span>
</div>
""", unsafe_allow_html=True)

if not BOND_FILE.exists():
    st.error(f"❌ 未找到债券数据文件：`{BOND_FILE}`")
    st.stop()

# ══════════════════════════════════════════════
# 数据加载触发区（分层进度展示）
# ══════════════════════════════════════════════
#
# 进度层次设计：
#
#   st.status（可折叠的多步进度容器）
#   ├── Step 1  [Staging]      读取全国债券原始数据
#   │           缓存命中时此步骤瞬间完成，status 直接跳到下一步
#   ├── Step 2  [Staging]      读取省份财力数据（同上）
#   ├── Step 3  [Intermediate] 多表关联 + 主体视图构建（该视图是 marts 层各指标计算的基础宽表）
#   │            @st.cache_data(show_spinner=False)，进度文案由此处 update() 控制
#   └── Step 4  [Marts]        实时聚合（无缓存，毫秒级，无单独 step）
#
# 缓存全部命中时：status 容器以"完成"态折叠，用户几乎感知不到延迟。
# 冷启动或 Excel 替换后：每一层的实际耗时都有对应文案，体验清晰。

needs_reload = (
    run_btn
    or "result" not in st.session_state
    or st.session_state.get("last_prov") != province_input
)

# 确保读vesion的dqc的最新信息能被感知（加入时间戳作为参数）
def get_data_version_info():
    version_file = VERSION_FILE
    if version_file.exists():
        # 获取文件最后修改时间，确保文件一变，缓存就失效
        mtime = version_file.stat().st_mtime
        with open(version_file, "r", encoding="utf-8") as f:
            return json.load(f), mtime
    return {}, 0


if needs_reload:
    with st.status(f"正在加载「{province_input}」分析数据...", expanded=True) as status:
        

        # ── Step 1：Staging — 全国债券数据 ──────────────────
        status.write("Step 1 / 4 · 读取全国城投债数据（Staging）")
        df_qg = load_bond_data()

        # --- [新增]：第一时间读取 JSON 并展示加工历史 ---
        version_info, _ = get_data_version_info()
        history_dqc = version_info.get("dqc_report", [])
        if history_dqc:
            #status.write("#### 数据质量检查报告")
            for note in history_dqc:
                st.warning(f"{note}")
        else:
            status.write("")

        # ── Step 2：Staging — 省份财力数据 ──────────────────
        status.write("Step 2 / 4 · 读取省份财力数据（Staging）")
        _fiscal_path_pre = FISCAL_DIR / f"{FISCAL_YEAR}年{province_input}财力.xlsx"
        stg_load_fiscal_data(
            FISCAL_DIR, FISCAL_YEAR, province_input,
            _mtime=_file_mtime(_fiscal_path_pre),
        )

        # ── Step 3：Intermediate — 关联 债券数据和财力数据 ──────────
        status.write("Step 3 / 4 · 关联数据，构建大宽表（Intermediate）")
        result = run_analysis(df_qg, province_input)

        if result is None:
            status.update(label=f" 未找到「{province_input}」的发债数据", state="error")
            st.error(f"提示：未找到省份「{province_input}」的发债数据，请确认名称与 Wind 数据一致。")
            st.stop()

        # ── Step 4：Marts — DQC 业务逻辑审计 ──────────────
        status.write("Step 4 / 4 · 信息分析（Marts）")
        suspicious_df, warning_msg ,total_cities= dqc_marts_platform_dismatch(
            result["sc_zt"], province_input
        )

        if warning_msg:
            st.warning(warning_msg+"具体信息如下，请关注：")
        
            # 预处理展示用的 DataFrame
            display_df = (
                suspicious_df[["发行人中文名称", "城投行政级别", "城市", FISCAL_COL, "_fiscal_rank"]]
                .drop_duplicates("发行人中文名称")
                .copy()
            )
            
            # 转换排名列格式为 "排名/总数"
            display_df["_fiscal_rank"] = display_df["_fiscal_rank"].apply(
                lambda x: f"{int(x)}/{total_cities}"
            )
            
            # 重命名并展示
            st.dataframe(
                display_df.rename(columns={
                    "_fiscal_rank": "财力省内排名", 
                }),
                width="stretch",
                hide_index=True #去掉左侧 index
            )
            
            status.write(
                '<p style="color: gray; font-size: 0.9rem;">备注：这里的财力为地方全级一般公共预算收入。</p>', 
                unsafe_allow_html=True
            )
        status.update(
            label=f"「{province_input}」数据加载完成（点击此处可展开/关闭「数据质量检查提示」）",
            state="complete",
            expanded=False,
        )

    st.session_state["result"]    = result
    st.session_state["last_prov"] = province_input



else:
    # 缓存命中：直接从 session_state 读取，不触发任何 IO 或 spinner
    df_qg = load_bond_data()   # 必须保证 df_qg 变量存在（供后续 TAB 使用）

res        = st.session_state["result"]
prov_name  = res["province"]
prov_kpi = res.get("province_kpi")
df_sc      = res["df_raw"] #全量债项明细宽表（DataFrame）一行一只债。Tab 0 热力矩阵、Tab 4 发债案例用。
sc_zt      = res["sc_zt"]  #去重的发行人主体视图（DataFrame）一行一个主体。Tab 1 地图气泡、Tab 1 主体名单、Tab 2 准入门槛筛选、Tab 2 的 pandas 降级路径都会用。
sc_pj      = res["agencies"] #评级机构市占率统计表（DataFrame），一行一家评级机构在某省的数据。Tab 0 的排行榜、饼图、柱图用。
sc_uw      = res["underwriters"] #主承销商排名表（DataFrame），一行一家承销商。目前没有 Tab 直接用到它，但作为分析结果的一部分保留。如果后面加承销商排名的 Tab 可以立刻用上。
actual_ags = res["actual_ags"] #该省内实际展业的评级机构名单（list）。Tab 0 热力矩阵的"我方机构"下拉框、Tab 3 展业帮手圈的机构选择器、Tab 4 发债案例的机构选择器，都需要知道该省有哪些机构可以选。

# ══════════════════════════════════════════════
# KPI 行
# ══════════════════════════════════════════════

k1, k2, k3, k4, k5, k6 = st.columns(6)

if _using_duckdb and not prov_kpi.empty:
    row = prov_kpi.iloc[0]
    k1.metric("展业省份",     prov_name)
    k2.metric("已评级主体",   f"{int(row['已评级主体_家'])}家")
    k3.metric("债项总数",     f"{int(row['债项总数_条'])}条")
    k4.metric("发行总额",     f"{row['发行总额_亿']:,.0f}亿")
    k5.metric("覆盖城市",     f"{int(row['覆盖城市_个'])}个")
    k6.metric("评级机构数",   f"{int(row['评级机构数_家'])}家")
else:
    # pandas 降级
    k1.metric("展业省份",   prov_name)
    k2.metric("已评级主体", f"{len(sc_zt)}家")
    k3.metric("债项总数",   f"{len(df_sc)}条")
    k4.metric("发行总额",   f"{df_sc['发行总额'].sum() if '发行总额' in df_sc.columns else 0:,.0f}亿")
    k5.metric("覆盖城市",   f"{df_sc['城市'].nunique()}个")
    k6.metric("评级机构数", f"{sc_pj[sc_pj['主体数'] > 0].shape[0]}家")

# ══════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════
tabs = st.tabs(["评级机构全景", "已评级主体全景", "准入门槛模拟器", "展业帮手圈", "我方评级发债案例"])

# ────────────────────────────────────────────
# TAB 0  评级机构全景
# ────────────────────────────────────────────
with tabs[0]:
    st.markdown('<div class="sec-note">本部分展示各评级机构的展业数据、排名以及竞争格局。</div>', unsafe_allow_html=True)
    st.markdown('<div class="sec-title">评级业务市场数据</div>', unsafe_allow_html=True)
    st.dataframe(sc_pj, use_container_width=True, height=320, hide_index=True,
                 column_config={
                     "主体数": st.column_config.ProgressColumn(min_value=0, max_value=int(sc_pj["主体数"].max() or 1), format="%d", color="#5E7E7C"),
                     "债项数": st.column_config.ProgressColumn(min_value=0, max_value=int(sc_pj["债项数"].max() or 1), format="%d", color="#8B6F4E"),
                 })
    col_pie, col_bar = st.columns(2)
    with col_pie:
        st.markdown('<div class="sec-title" style="text-align:left;">主体市场占有率（业务广度）</div>', unsafe_allow_html=True)
        st.plotly_chart(fig_pie(sc_pj), use_container_width=True, config=PLOTLY_CFG)
    with col_bar:
        st.markdown('<div class="sec-title" style="text-align:left;">债项/主体比值（业务深度）</div>', unsafe_allow_html=True)
        st.plotly_chart(fig_bar_ratio(sc_pj), use_container_width=True, config=PLOTLY_CFG)

    st.markdown("---")
    st.markdown('<div class="sec-title">评级机构竞争格局</div>', unsafe_allow_html=True)
   
    dim_options = {f"按{FISCAL_YEAR}年财力水平": "fiscal", "按行政级别": "level", "按主体评级": "rating", "按地区位置": "city"}
    _dc, _ac = st.columns(2)
    with _dc: dim_lbl = st.selectbox("筛选分析维度", list(dim_options.keys()), key="comp_dim_tab0")
    with _ac: my_ag_comp = st.selectbox("我方评级机构（第一行）", actual_ags if actual_ags else [""], key="comp_ag_tab0")
    dim = dim_options[dim_lbl]
    st.markdown('<div class="sec-note">热力图中：数字 = 各机构在该分析维度的主体客户数（去重）；财力指地方一般公共预算收入，均采用全口径</div>', unsafe_allow_html=True)
    fig_heat, matrix, all_ags_list, cats = fig_comp_heatmap(df_sc, my_ag_comp, dim)
    if fig_heat:
        if dim == "city" and len(cats) > 1:
            fig_heat.update_xaxes(tickangle=-45, tickfont=dict(size=10, color="#1a3a3a"))
            fig_heat.update_layout(margin=dict(b=80))


        st.plotly_chart(fig_heat, use_container_width=True, config=PLOTLY_CFG)
        
        my_scores = {c: matrix.get(my_ag_comp, {}).get(c, 0) for c in cats}
        rank_rows = []
        for c in cats:
            total_c = sum(matrix.get(ag, {}).get(c, 0) for ag in all_ags_list)
            if total_c == 0: continue
            sorted_ags = sorted(all_ags_list, key=lambda a: matrix.get(a, {}).get(c, 0), reverse=True)
            my_rank = sorted_ags.index(my_ag_comp) + 1 if my_ag_comp in sorted_ags else "—"
            rank_rows.append({"维度": c, "第一名": f"{sorted_ags[0]}({matrix.get(sorted_ags[0],{}).get(c,0)})",
                               "我方主体数": my_scores[c], "我方排名": f"No.{my_rank}",
                               "我方市占率": f"{my_scores[c]/total_c:.0%}" if total_c else "0%"})
        if rank_rows: st.dataframe(pd.DataFrame(rank_rows), use_container_width=True, hide_index=True)
        
        strong = [c for c in cats if my_scores[c]>0 and all(my_scores[c]>=matrix.get(ag,{}).get(c,0) for ag in all_ags_list if ag!=my_ag_comp)]
        weak   = [c for c in cats if my_scores[c]>0 and sum(1 for ag in all_ags_list if ag!=my_ag_comp and matrix.get(ag,{}).get(c,0)>my_scores[c])>=2]
        blank  = [c for c in cats if my_scores[c]==0]
        if strong: st.markdown(f'<div class="rec-g"><b>优势阵地</b>：{" / ".join(strong)}</div>', unsafe_allow_html=True)
        if weak:   st.markdown(f'<div class="rec-a"><b>待提升区域</b>：{" / ".join(weak)}</div>', unsafe_allow_html=True)
        if blank:  st.markdown(f'<div class="rec-r"><b>空白区域</b>：{" / ".join(blank[:8])}</div>', unsafe_allow_html=True)

# ────────────────────────────────────────────
# TAB 1  已评级主体全景
# ────────────────────────────────────────────
with tabs[1]:
    st.markdown('<div class="sec-note">本部分展示已评级主体地理分布及名单。注：这些主体均已公开发债。</div>', unsafe_allow_html=True)
    st.markdown('<div class="sec-title">已评级主体地图分布</div>', unsafe_allow_html=True)
    mc1, mc2 = st.columns(2)
    with mc1:
        map_lv_opts = ["全部行政级别"] + sorted(df_sc["城投行政级别"].dropna().unique().tolist(), key=lambda l: LEVEL_ORDER.get(l,-1), reverse=True)
        map_lv = st.selectbox("筛选行政级别", map_lv_opts, key="map_lv")
    with mc2:
        map_rt_opts = ["全部信用级别"] + [r for r in RATING_ORDER if r in sc_zt["主体评级"].values]
        map_rt = st.selectbox("筛选主体评级", map_rt_opts, key="map_rt")

    # 复用 staging 缓存（mtime 不变则直接命中，无二次磁盘读取）
    _fiscal_path   = FISCAL_DIR / f"{FISCAL_YEAR}年{prov_name}财力.xlsx"
    _fiscal_df     = stg_load_fiscal_data(FISCAL_DIR, FISCAL_YEAR, prov_name,
                                          _mtime=_file_mtime(_fiscal_path))
    _fiscal_cities = _fiscal_df["城市"].dropna().tolist() if "城市" in _fiscal_df.columns else []
    map_fig = build_map_fig(sc_zt, "" if map_lv=="全部行政级别" else map_lv, "" if map_rt=="全部信用级别" else map_rt, fiscal_cities=_fiscal_cities)
    map_fig.update_geos(fitbounds="locations", visible=False, resolution=50, showcountries=True, countrycolor="#d1e0de")
    st.plotly_chart(map_fig, use_container_width=True, config=PLOTLY_CFG)
    st.markdown('<div class="sec-note">气泡大小=主体数 | 颜色=最高信用级别 | 灰色=该城市暂无公开发债的被评主体 | 悬停查看详情</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f'<div class="sec-title">{prov_name}已评级主体名单</div>', unsafe_allow_html=True)
    f1, f2, f3 = st.columns(3)
    with f1: sel_city = st.selectbox("筛选城市", ["全部城市"] + _cities_by_fiscal(sc_zt), key="zt_city")
    with f2: sel_lv = st.selectbox("筛选行政级别", ["全部级别"] + sorted(df_sc["城投行政级别"].dropna().unique().tolist(), key=lambda l: LEVEL_ORDER.get(l,-1), reverse=True), key="zt_lv")
    with f3: sel_rt = st.selectbox("筛选主体评级", ["全部评级"] + [r for r in RATING_ORDER if r in sc_zt["主体评级"].values], key="zt_rt")

    df_show = sc_zt.copy()
    if sel_city != "全部城市": df_show = df_show[df_show["城市"]==sel_city]
    if sel_lv   != "全部级别": df_show = df_show[df_show["城投行政级别"]==sel_lv]
    if sel_rt   != "全部评级": df_show = df_show[df_show["主体评级"]==sel_rt]
    df_show["序号"] = range(1, len(df_show)+1)

    disp_cols = [c for c in ["序号","发行人中文名称","实际控制人","城市",FISCAL_COL,"城投行政级别","主体评级","主体评级机构","总资产","净资产","营业收入","净利润","资产负债率","财务报告期"] if c in df_show.columns]
    df_disp = df_show[disp_cols].rename(columns={FISCAL_COL: FISCAL_DISPLAY})

    total_issuers = len(df_show)
    valid_fiscal  = df_show[df_show["总资产"].notna() & (df_show["总资产"] > 0)].shape[0]
    coverage_rate = (valid_fiscal / total_issuers * 100) if total_issuers > 0 else 0

    st.metric("当前筛选主体数", f"{len(df_show)}家")
    st.dataframe(df_disp, use_container_width=True, height=480, hide_index=True,
                 column_config={
                     "总资产":       st.column_config.NumberColumn(format="%.1f 亿"),
                     "净资产":       st.column_config.NumberColumn(format="%.1f 亿"),
                     "营业收入":     st.column_config.NumberColumn(format="%.1f 亿"),
                     "净利润":       st.column_config.NumberColumn(format="%.1f 亿"),
                     "资产负债率":   st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%", color="#E8D9C8"),
                     FISCAL_DISPLAY: st.column_config.NumberColumn(format="%.0f 亿"),
                 })
    st.markdown(f'''<div class="sec-note" style="border-left:4px solid #2F4F4F;padding:12px;background:#F0F5F5;border-radius:5px;line-height:1.6;">
        <strong>数据说明：</strong>私募债发行人可以不披露完整报表，导致 Wind 财务数据暂缺。保留它们旨在呈现区域内已评级平台的全貌。<br>
        <strong>统计汇总：</strong>本次共检索 {total_issuers} 家主体，其中 {valid_fiscal} 家具有核心财务指标，财务数据覆盖率 {coverage_rate:.1f}%。
    </div>''', unsafe_allow_html=True)

# ────────────────────────────────────────────
# TAB 2  准入门槛模拟器
# ────────────────────────────────────────────
with tabs[2]:
    st.markdown('<div class="sec-note">根据已公开发债的受评主体数据，模拟特定城市+行政级别+信用级别下的财务门槛；输入新主体数据后自动打分并给出承做建议。</div>', unsafe_allow_html=True)
    sim_c1, sim_c2 = st.columns([1, 1])
    with sim_c1:
        st.markdown("**📍 定位目标市场**")
        sim_city = st.selectbox("目标城市", ["全省（不限城市）"] + _cities_by_fiscal(sc_zt), key="sim_city")
        sim_lv   = st.selectbox("目标行政级别", ["全部行政级别"] + sorted(df_sc["城投行政级别"].dropna().unique().tolist(), key=lambda l: LEVEL_ORDER.get(l,-1), reverse=True), key="sim_lv")
        sim_rt   = st.selectbox("目标主体评级", ["全部主体级别"] + [r for r in RATING_ORDER if r in sc_zt["主体评级"].values], key="sim_rt")

        # ── 准入门槛：DuckDB View 优先，pandas 降级 ──────────────
        _city_param   = "全部" if sim_city == "全省（不限城市）" else sim_city
        _level_param  = "全部" if sim_lv   == "全部行政级别"     else sim_lv
        _rating_param = "全部" if sim_rt   == "全部主体级别"     else sim_rt


        if _using_duckdb:
            # DuckDB 路径：从原始主体视图直接计算分位数（支持"全部"聚合）
            _and_parts = ["省份 = ?"]
            _params = [prov_name]
            if _city_param != "全部":
                _and_parts.append("城市 = ?"); _params.append(_city_param)
            if _level_param != "全部":
                _and_parts.append("城投行政级别 = ?"); _params.append(_level_param)
            if _rating_param != "全部":
                _and_parts.append("主体评级 = ?"); _params.append(_rating_param)
            _and_clause = (" AND " + " AND ".join(_and_parts)) if _and_parts else ""
            _bench_sql = f"""
                SELECT
                    COUNT(*) AS 样本数,
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
                WHERE 省份 IS NOT NULL AND TRIM(主体评级) != ''{_and_clause}
            """
            _bench_df = query_duckdb(_bench_sql, _params if _params else None)


            # 将 Macro 宽表结果转换为 mart_financial_bench 返回的字典格式
            # 确保下游打分逻辑（_score_val）无需修改
            bench = {"n": int(_bench_df["样本数"].iloc[0]) if not _bench_df.empty else 0}
            _col_map = {
                "总资产":    ("总资产_最小",  "总资产_Q1",  "总资产_中位",  "总资产_均值",  "总资产_Q3",  "总资产_最大"),
                "净资产":    ("净资产_最小",  "净资产_Q1",  "净资产_中位",  "净资产_均值",  "净资产_Q3",  "净资产_最大"),
                "营业收入":  ("营业收入_最小","营业收入_Q1","营业收入_中位","营业收入_均值","营业收入_Q3","营业收入_最大"),
                "净利润":    ("净利润_最小",  "净利润_Q1",  "净利润_中位",  "净利润_均值",  "净利润_Q3",  "净利润_最大"),
                "资产负债率":("负债率_最小",  "负债率_Q1",  "负债率_中位",  "负债率_均值",  "负债率_Q3",  "负债率_最大"),
            }
            if bench["n"] > 0 and not _bench_df.empty:
                row = _bench_df.iloc[0]
                for metric, (mn, q1, med, avg, q3, mx) in _col_map.items():
                    bench[metric] = {
                        "min":    float(row[mn])  if row[mn]  is not None else None,
                        "q1":     float(row[q1])  if row[q1]  is not None else None,
                        "median": float(row[med]) if row[med] is not None else None,
                        "mean":   float(row[avg]) if row[avg] is not None else None,
                        "q3":     float(row[q3])  if row[q3]  is not None else None,
                        "max":    float(row[mx])  if row[mx]  is not None else None,
                    }
        else:
            # pandas 降级路径（DuckDB 未初始化时）
            bench_df = sc_zt.copy()
            if sim_city != "全省（不限城市）": bench_df = bench_df[bench_df["城市"]==sim_city]
            if sim_lv   != "全部行政级别":     bench_df = bench_df[bench_df["城投行政级别"]==sim_lv]
            if sim_rt   != "全部信用级别":     bench_df = bench_df[bench_df["主体评级"]==sim_rt]
            bench = mart_financial_bench(bench_df)

        st.markdown(f"**📊 市场财务基准**（基于 **{bench['n']}** 家受评主体）")
        if bench["n"] > 0:
            fn = {"总资产": "总资产(亿元)", "净资产": "净资产(亿元)", "营业收入": "营业收入(亿元)", "净利润": "净利润(亿元)", "资产负债率": "资产负债率(%)"}
            br = [{"指标": lbl, "最小值": bench[col]["min"], "Q1(25%)": bench[col]["q1"], "中位数": bench[col]["median"], "均值": bench[col]["mean"], "Q3(75%)": bench[col]["q3"], "最大值": bench[col]["max"]} for col, lbl in fn.items() if col in bench]
            if br:
                bt = pd.DataFrame(br)
                st.dataframe(bt, use_container_width=True, hide_index=True,
                             column_config={c: st.column_config.NumberColumn(format="%.1f") for c in bt.columns if c != "指标"})
                st.caption("★★★ >Q3(75%) | ★★ >中位 | ★ >Q1(25%) | ✗ <Q1")
        else:
            st.warning("当前筛选组合暂无历史数据，请调整条件。")

    with sim_c2:
        st.markdown("**✏️ 输入待评估主体财务数据**")
        inp_as  = st.number_input("总资产（亿元）",   min_value=0.0, step=0.1, format="%.1f", value=None, key="inp_as")
        inp_eq  = st.number_input("净资产（亿元）",   min_value=0.0, step=0.1, format="%.1f", value=None, key="inp_eq")
        inp_rv  = st.number_input("营业收入（亿元）", min_value=0.0, step=0.1, format="%.1f", value=None, key="inp_rv")
        inp_pf  = st.number_input("净利润（亿元）",   min_value=0.0, step=0.1, format="%.1f", value=None, key="inp_pf")
        auto_lev = round((inp_as - inp_eq) / inp_as * 100, 1) if (inp_as and inp_as > 0 and inp_eq is not None) else None
        if auto_lev is not None:
            st.session_state["inp_lev"] = auto_lev
        inp_lev = st.number_input("资产负债率（%）— 自动计算，可手动覆盖", min_value=0.0, max_value=100.0, step=0.1, format="%.1f", value=None, key="inp_lev")

        eval_btn = st.button("▶ 开始评估财务指标", type="primary", use_container_width=True)
        if eval_btn and bench["n"] > 0:
            inp_vals = {"总资产": inp_as or None, "净资产": inp_eq or None, "营业收入": inp_rv or None, "净利润": inp_pf or None, "资产负债率": inp_lev or None}
            scored = [{"指标": col, "输入值": val, "市场中位": bench[col]["median"], "得分": _score_val(val, bench[col], col)} for col, val in inp_vals.items() if val and col in bench]
            if not scored:
                st.markdown(
                    """
                    <div style="
                        padding: 10px 6px;
                        background-color: #F5F2F0;
                        color: #607D8B;
                        line-height: 1.4;
                        border-left: 4px solid #607D8B;
                        border-radius: 4px;
                        font-size: 14px;
                        margin: 10px 0;
                    ">
                        <b>提示</b>：请填写财务指标后再发起评估。
                    </div>
                    """,
                    unsafe_allow_html=True
                )
            else:
                ratio = sum(r["得分"] for r in scored) / (len(scored) * 3)
                sm = {3: "★★★", 2: "★★", 1: "★", 0: "✗"}
                cm = {3: "超75%分位，领先市场", 2: "超中位，稳健", 1: "超25%分位，偏弱", 0: "低于Q1，差距较大"}
                res_rows = [{"指标": r["指标"], "输入值": f"{r['输入值']}{'%' if r['指标']=='资产负债率' else '亿'}", "市场中位": f"{r['市场中位']}{'%' if r['指标']=='资产负债率' else '亿'}", "竞争力": sm[r["得分"]], "评价": cm[r["得分"]]} for r in scored]
                st.markdown("**📋 财务指标评估结果**")
                st.dataframe(pd.DataFrame(res_rows), use_container_width=True, hide_index=True)
                st.progress(ratio)
                score_pct = int(ratio * 100)
                lbl = f"{sim_city if sim_city!='全省（不限城市）' else '全省'} · {sim_lv if sim_lv!='全部行政级别' else '全级别'} · {sim_rt if sim_rt!='全部信用级别' else '全信用级别'}"
                if ratio >= 0.65:   st.markdown(f'<div class="rec-g"><b>✅ 建议承做（{score_pct}分）</b><br>主体财务优于{lbl}历史主体中位，竞争力较强。</div>', unsafe_allow_html=True)
                elif ratio >= 0.35: st.markdown(f'<div class="rec-a"><b>⚠️ 谨慎评估（{score_pct}分）</b><br>部分指标低于{lbl}中位水平，建议重点评估薄弱项后决策。</div>', unsafe_allow_html=True)
                else:               st.markdown(f'<div class="rec-r"><b>❌ 暂不建议（{score_pct}分）</b><br>主要指标明显偏弱，与{lbl}准入门槛差距较大。</div>', unsafe_allow_html=True)

# ────────────────────────────────────────────
# TAB 3  展业帮手圈
# ────────────────────────────────────────────
with tabs[3]:
    st.markdown('<div class="sec-note">通过分析主承销商情况，判断哪家券商/银行适合作为合作伙伴，共同展业增加胜率。</div>', unsafe_allow_html=True)
    pc1, pc2, pc3 = st.columns(3)
    with pc1: partner_ag   = st.selectbox("我方评级机构", actual_ags if actual_ags else ["—"], key="partner_ag")
    with pc2: partner_city = st.selectbox("目标城市", ["全部城市"] + _cities_by_fiscal(sc_zt), key="partner_city")
    with pc3: partner_lv   = st.selectbox("行政级别", ["全部级别"] + sorted(df_sc["城投行政级别"].dropna().unique().tolist(), key=lambda l: LEVEL_ORDER.get(l,-1), reverse=True), key="partner_lv")

    # ── 展业帮手圈：DuckDB View 优先，pandas 降级 ──────────────
    _partner_city  = "" if partner_city == "全部城市" else partner_city
    _partner_level = "" if partner_lv   == "全部级别"  else partner_lv

    
    if _using_duckdb:
        # DuckDB 路径：查询 v_partner_network
        _where, _params = _build_where({
            "评级机构":     partner_ag,
            "城市":         _partner_city,
            "城投行政级别": _partner_level,
        })

        partner_df = query_duckdb("""
            SELECT
                主承销商,
                已承做主体数,
                已承做主体列表,
                共同客户数,
                共同客户列表,
                合作等级
            FROM v_partner_network
            WHERE 省份    LIKE ?
            AND 评级机构 = ?
            ORDER BY 共同客户数 DESC, 已承做主体数 DESC
        """, [f"%{prov_name[:2]}%", partner_ag])

        # 从共同客户列表推导我方客户总数
        all_common = set()
        for clients in partner_df["共同客户列表"].dropna():
            for c in clients.split("、"):
                c = c.strip()
                if c:
                    all_common.add(c)
        my_issuers = all_common
    
    else:
        # pandas 降级路径
        partner_df, my_issuers = mart_partner_network(
            df_sc, partner_ag, _partner_city, _partner_level
        )

    st.markdown(f'<div class="sec-title">主承销商共同客户分析</div><div class="sec-note" style="margin-top:-5px;">— {partner_ag} 在当前选择范围内的主体客户共有<b>{len(my_issuers)}</b> 家</div>', unsafe_allow_html=True)
    if not partner_df.empty and "共同客户数" in partner_df.columns:
        st.dataframe(partner_df[["主承销商","已承做主体数","已承做主体列表","共同客户数","合作等级","共同客户列表"]], use_container_width=True, height=360, hide_index=True,
                     column_config={"共同客户数": st.column_config.ProgressColumn(min_value=0, max_value=max(int(partner_df["共同客户数"].max()), 1), format="%d", color="#2D5A5E")})
    else:
        st.info("当前筛选条件下无主承销商数据。")

    st.markdown("---")
    st.markdown("**💡 同业交流建议**")
    if not partner_df.empty and "合作等级" in partner_df.columns:
        high = partner_df[partner_df["合作等级"]=="主要合作伙伴"]
        mid  = partner_df[partner_df["合作等级"]=="初步合作对象"]
        low  = partner_df[partner_df["合作等级"]=="待开拓合作关系"]
        adv1, adv2, adv3 = st.columns(3)
        with adv1:
            st.markdown("**① 主要合作伙伴**（共同客户≥3）")
            for _, r in high.iterrows(): st.markdown(f'<div class="rec-g"><b>{r["主承销商"]}</b><br>共同客户：{r["共同客户数"]}家<br><span style="font-size:11px">{r["共同客户列表"]}</span></div>', unsafe_allow_html=True)
            if high.empty: st.caption("暂无")
        with adv2:
            st.markdown("**② 初步合作对象**（1-2家）")
            for _, r in mid.iterrows():  st.markdown(f'<div class="rec-a"><b>{r["主承销商"]}</b><br>共同客户：{r["共同客户数"]}家<br><span style="font-size:11px">{r["共同客户列表"]}</span></div>', unsafe_allow_html=True)
            if mid.empty: st.caption("暂无")
        with adv3:
            st.markdown("**③ 待开拓合作关系**（无共同客户）")
            if not low.empty: st.markdown(f'<div class="rec-n"><span style="font-size:12px">{"、".join(low["主承销商"].head(8).tolist())}{"等" if len(low)>8 else ""}</span></div>', unsafe_allow_html=True)
            else: st.caption("暂无")

# ────────────────────────────────────────────
# TAB 4  我方评级发债案例
# ────────────────────────────────────────────
with tabs[4]:
    st.markdown('<div class="sec-note">查看指定评级机构在当前省份的受评主体列表及核心财务数据，支持按城市、行政级别、信用级别筛选。</div>', unsafe_allow_html=True)
    my_ags_case = st.selectbox("选择评级机构", actual_ags if actual_ags else ["—"], key="case_ag")
    c1c, c2c, c3c = st.columns(3)
    with c1c: case_city = st.selectbox("筛选城市", ["全部城市"] + _cities_by_fiscal(sc_zt), key="case_city")
    with c2c: case_lv   = st.selectbox("筛选行政级别", ["全部级别"] + sorted(df_sc["城投行政级别"].dropna().unique().tolist(), key=lambda l: LEVEL_ORDER.get(l,-1), reverse=True), key="case_lv")
    with c3c: case_rt   = st.selectbox("筛选主体评级", ["全部信用级别"] + [r for r in RATING_ORDER if r in df_sc["主体评级"].values], key="case_rt")

    case_df = df_sc[df_sc["主体评级机构"].fillna("").str.contains(my_ags_case, regex=False)].copy()
    if case_city != "全部城市":     case_df = case_df[case_df["城市"]==case_city]
    if case_lv   != "全部级别":     case_df = case_df[case_df["城投行政级别"]==case_lv]
    if case_rt   != "全部信用级别": case_df = case_df[case_df["主体评级"]==case_rt]
    case_df = case_df.sort_values([FISCAL_COL, "行政级别量化", "主体级别量化", "发行总额"], ascending=False).reset_index(drop=True)
    case_df["序号"] = range(1, len(case_df)+1)

    CASE_COL_MAP = {"序号": "序号", "发行人中文名称": "发行人中文名称", "城市": "城市",
                    FISCAL_COL: "一般公共预算收入(亿元)", "城投行政级别": "行政级别", "主体评级": "主体评级",
                    "证券代码": "证券代码", "证券简称": "证券简称", "主承销商": "主承销商","Wind债券二级分类": "Wind债券二级分类",
                    "发行总额": "发行总额(亿元)", "票面利率": "票面利率(%)"}
    case_disp = case_df[[c for c in CASE_COL_MAP if c in case_df.columns]].rename(columns=CASE_COL_MAP)

    st.metric(f"{my_ags_case} 债项评级数（当前筛选）", f"{len(case_df)}项")
    st.dataframe(case_disp, use_container_width=True, height=540, hide_index=True,
                 column_config={
                     "发行总额(亿元)":        st.column_config.NumberColumn(format="%.2f 亿"),
                     "一般公共预算收入(亿元)": st.column_config.NumberColumn(format="%.0f 亿"),
                     "票面利率(%)":            st.column_config.NumberColumn(format="%.2f%%"),
                 })

    st.markdown("---")
    _html_bytes = build_html_report(case_disp, my_ags_case, prov_name,
                                    case_city if case_city!="全部城市" else "",
                                    case_lv   if case_lv!="全部级别" else "",
                                    case_rt   if case_rt!="全部信用级别" else "",
                                    FISCAL_YEAR).encode("utf-8")
    _fname_parts = [p for p in [my_ags_case, prov_name,
                                 case_city if case_city!="全部城市" else "",
                                 case_lv   if case_lv!="全部级别" else "",
                                 case_rt   if case_rt!="全部信用级别" else ""] if p]
    _dl1, _dl2 = st.columns([4, 1])
    with _dl1: st.caption(f"点击右侧按钮下载当前筛选结果（{len(case_df)} 条记录），输出为 HTML 格式，可直接用浏览器打开或打印。")
    with _dl2: st.download_button("⬇️ 导出 HTML 报告", data=_html_bytes,
                                   file_name="_".join(_fname_parts)+"_发债案例.html",
                                   mime="text/html; charset=utf-8", use_container_width=True)
