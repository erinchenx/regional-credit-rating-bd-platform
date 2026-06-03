# -*- coding: utf-8 -*-
"""
scripts/build_warehouse.py
===========================
【数据仓库初始化脚本】湖仓分离架构 · ETL 入库

职责：
  将原始 Excel 文件转换为 Parquet 格式（数据湖层），
  并在 DuckDB 中注册 SQL 视图（数仓服务层）。

架构层次：
  ┌─────────────────────────────────────────────────────────┐
  │  原始层 (Raw)     data/raw/                             │
  │  ─────────────────────────────────────────────────────  │
  │  Excel 文件，原封不动保留，不做任何修改                  │
  │  bond_20230216.xlsx  /  2021年四川省财力.xlsx            │
  └──────────────────────────┬──────────────────────────────┘
                             │ build_warehouse.py (本脚本)
                             │ 1. pd.read_excel
                             │ 2. 列名归一化 + DQC
                             │ 3. df.to_parquet
                             ▼
  ┌─────────────────────────────────────────────────────────┐
  │  标准层 (Standardized)   data/warehouse/                │
  │  ─────────────────────────────────────────────────────  │
  │  Parquet 文件，列式存储，列名已归一化，DQC 已通过        │
  │  bond/bond_20230216.parquet                             │
  │  fiscal/四川省_2021.parquet                             │
  │                                                         │
  │  特点：开放格式，任何引擎（Spark/Java/R）均可直接读取   │
  │        历史快照永久保留，不覆盖，支持版本溯源            │
  └──────────────────────────┬──────────────────────────────┘
                             │ build_warehouse.py (本脚本)
                             │ 4. CREATE OR REPLACE VIEW
                             ▼
  ┌─────────────────────────────────────────────────────────┐
  │  服务层 (Serving)        data/serving/                  │
  │  ─────────────────────────────────────────────────────  │
  │  DuckDB 文件，只存 SQL 视图定义，不存数据本身            │
  │  credit_indicators.duckdb                               │
  │                                                         │
  │  特点：可被 Tableau / Power BI / DBeaver 直接连接       │
  │        视图定义版本化，激活版本由 active_version.json   │
  │        管理，切换历史快照只需改 JSON，零 SQL 改动        │
  └─────────────────────────────────────────────────────────┘

使用场景：
  - 首次部署：python scripts/build_warehouse.py
  - 数据更新：收到新 Excel 后，放入 data/1发债数据/，重新运行本脚本


运行方式（从项目根目录）：
  python scripts/build_warehouse.py
  python scripts/build_warehouse.py --bond-file "全国城投债数据_WIND截止20240101.xlsx" #指定处理某期文件
  python scripts/build_warehouse.py --activate bond_20230216  # 激活指定快照版本
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ── 确保项目根目录在 Python 路径中（scripts/ 调用 models/ 时需要）──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 复用 staging 层的列名归一化和 DQC 逻辑（不重复造轮子）
from models.staging.stg_data import (
    _rename_bond_columns,
    _normalize_agency_name,
    _dqc_bond,
    _dqc_fiscal,
    LEVEL_ORDER,
    RATING_ORDER,
)

# ──────────────────────────────────────────────
# 日志配置（进阶彩色版）
# ──────────────────────────────────────────────

class ColorWarningFormatter(logging.Formatter):
    """自定义格式化器：将 WARNING 级别的日志染成红色"""
    def format(self, record):
        # 先获取标准格式的消息（时间 - 级别 - 内容）
        msg = super().format(record)
        # 如果是警告级别，则包裹 ANSI 转义序列（红色高亮）
        if record.levelno == logging.WARNING:
            return f"\033[31m{msg}\033[0m"  # \033[31m 是红色开始，\033[0m 是重置颜色
        return msg

def setup_logger():
    
    # 1. 彻底清除默认的 handler，防止日志重复打印
    logging.getLogger().handlers.clear()
    
    # 2. 创建一个终端处理器 (StreamHandler)
    console_handler = logging.StreamHandler()
    
    # 3. 设置格式：时间 [级别] 消息
    formatter = ColorWarningFormatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console_handler.setFormatter(formatter)
    
    # 4. 获取全局 logger 并挂载处理器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    
    return logging.getLogger("build_warehouse")


logger = setup_logger()

# ──────────────────────────────────────────────
# 路径常量
# ──────────────────────────────────────────────
RAW_BOND_DIR    = _PROJECT_ROOT / "data" / "1发债数据"
RAW_FISCAL_DIR  = _PROJECT_ROOT / "data" / "2财力数据"
WAREHOUSE_DIR   = _PROJECT_ROOT / "data" / "warehouse"
BOND_LAKE_DIR   = WAREHOUSE_DIR / "bond"
FISCAL_LAKE_DIR = WAREHOUSE_DIR / "fiscal"
SERVING_DIR     = _PROJECT_ROOT / "data" / "serving"
DB_PATH         = SERVING_DIR / "credit_indicators.duckdb"
VERSION_FILE    = SERVING_DIR / "active_version.json"
MODELS_DIR      = _PROJECT_ROOT / "models"      # SQL 模型文件根目录

FISCAL_YEAR = "2021"   # 财力数据年份，与 main.py 保持一致


# ══════════════════════════════════════════════
# Step 1：债券数据 Excel → Parquet
# ══════════════════════════════════════════════

def build_bond_parquet(bond_excel_path: Path) -> tuple[Path,list]:
    """
    将债券 Excel 转换为标准化 Parquet 文件，存入数据湖。

    文件命名规则：从 Excel 文件名中提取 8 位日期，作为快照版本号。
    例：全国城投债数据_WIND截止20230216.xlsx → bond_20230216.parquet

    这套命名规则确保：
    - 每期快照独立存放，不覆盖历史文件
    - 版本号直接从文件名读取，无需额外元数据
    - DuckDB 视图可以用通配符 bond/*.parquet 同时扫描多期数据

    返回：生成的 Parquet 文件路径
    """
   
    # ── 提取版本号（8位日期）──────────────────────────────────────
    date_match = re.search(r"20\d{6}", bond_excel_path.name)  # match year-prefixed dates like 20230216
    if not date_match:
        raise ValueError(
            f"无法从文件名「{bond_excel_path.name}」中提取日期版本号。\n"
            f"文件期望格式：文件名应包含 8 位数字日期，如 20230216。"
        )
    snapshot_date = date_match.group()
    parquet_path  = BOND_LAKE_DIR / f"bond_{snapshot_date}.parquet"

    if parquet_path.exists():
        logger.info(f"[Bond] {parquet_path.name}文件已存在，无需再次转换，跳过。")

    logger.info(f"[Bond] 读取 Excel：{bond_excel_path.name}")
    df = pd.read_excel(bond_excel_path, sheet_name=0)
    logger.info(f"[Bond] 原始数据：{len(df):,} 行 × {len(df.columns)} 列")

    # ── 列名归一化（复用 staging 层逻辑）────────────────────────────
    df = _rename_bond_columns(df)

    # ── 评级机构名称归并（复用 staging 层逻辑）──────────────────────
    for col in ["主体评级机构", "债项评级机构"]:
        if col in df.columns:
            df[col] = df[col].apply(_normalize_agency_name)

    # ── 辅助量化列（供下游 SQL ORDER BY 使用）──────────────────────
    df["行政级别量化"] = df["城投行政级别"].map(LEVEL_ORDER).fillna(0)
    df["主体级别量化"] = df["主体评级"].map(RATING_ORDER).fillna(0)

    # 截断时间戳，日期显示为 2022-09-30（例子）
    
    df["财务报告期"] = pd.to_datetime(df["财务报告期"]).dt.strftime('%Y-%m-%d')

    # ── 写入快照元数据列（供多版本查询时区分来源）──────────────────
    df["_snapshot_date"]   = snapshot_date        # 字符串，如 "20230216"
    df["_snapshot_source"] = bond_excel_path.name # 原始文件名，供审计追溯

    # ── DQC（复用 staging 层逻辑，在 build 阶段就拦截问题数据）─────
    # 注意：build_warehouse 是离线脚本，_dqc_bond 里的 st.warning 在此处
    # 不会触发 Streamlit UI，只会打印到终端（因为 logger 已接管）
    logger.info("[Bond] 执行 DQC 检查...")
    try:
        df, dqc_report = _dqc_bond(df, bond_excel_path)
    except Exception as e:
        # DQC 失败时打印清晰错误，让用户知道是哪个文件的问题
        logger.error(f"[Bond] DQC 检查失败，已中止写入 Parquet：\n{e}")
        raise

    # ── 写出 Parquet ─────────────────────────────────────────────
    BOND_LAKE_DIR.mkdir(parents=True, exist_ok=True) # parents=True：自动补全。exist_ok=True：防报错。如果文件夹已经有了，它就什么也不做。
    df.to_parquet(parquet_path, index=False, compression="snappy") #使用 Snappy 压缩算法。

    size_kb = parquet_path.stat().st_size / 1024 # 获取文件的原始字节（Bytes）大小。除以 1024 换算成大家能看懂的 KB。
    logger.info(
        f"[Bond] ✅ Parquet转换完成：{parquet_path.name}  "
        f"({len(df):,} 行，{size_kb:.0f} KB)"
    )
    return parquet_path, dqc_report


# ══════════════════════════════════════════════
# Step 2：财力数据 Excel → Parquet
# ══════════════════════════════════════════════

def build_fiscal_parquets(fiscal_dir: Path, fiscal_year: str) -> list[Path]:
    """
    将所有省份财力 Excel 批量转换为 Parquet 文件。

    财力数据体量小（每省几十行），统一存为单个 fiscal_<年份>.parquet，
    便于 DuckDB 一次扫描所有省份数据后在 SQL 层做省份筛选。

    文件命名规则：
      fiscal/四川省_2021.parquet
      fiscal/浙江省_2021.parquet

    返回：所有成功生成的 Parquet 文件路径列表
    """
    pattern = f"{fiscal_year}年*财力.xlsx"
    excel_files = list(fiscal_dir.glob(pattern))# glob是通配符搜索
    if not excel_files:
        logger.warning(f"[Fiscal] 未找到任何财力文件：{fiscal_dir / pattern}")
        return []

    logger.info(f"[Fiscal] 发现 {len(excel_files)} 个省份财力文件")
    FISCAL_LAKE_DIR.mkdir(parents=True, exist_ok=True)

    generated = []
    for excel_path in sorted(excel_files):
        # 从文件名提取省份名，如 "2021年四川省财力.xlsx" → "四川省"
        province = excel_path.stem.replace(f"{fiscal_year}年", "").replace("财力", "") # stem可获取文件名（不含扩展名）
        parquet_path = FISCAL_LAKE_DIR / f"{province}_{fiscal_year}.parquet"

        if parquet_path.exists():
            logger.info(f"[Fiscal] 已存在，跳过：{parquet_path.name}")
            generated.append(parquet_path)
            continue

        df = pd.read_excel(excel_path)

        # ── 列名模糊匹配（复用 stg_load_fiscal_data 的匹配逻辑）──
        # 注意：只映射第一个匹配到的列，避免多列映射到同一标准名导致重复列
        col_map: dict[str, str] = {}  #用来存映射关系。比如 { "地区名称": "城市" }
        city_mapped = fiscal_mapped = False #这是两个布尔开关（True/False）。初始都是 False，表示“我还没找到标准名对应的列”。
        for col in df.columns:
            if not city_mapped and ("地区" in str(col) or "城市" in str(col)):
                col_map[col] = "城市"; #这一列以后改名叫“城市”
                city_mapped = True    #后面哪怕再看到带“地区”的列也不管
            if not fiscal_mapped and "一般公共预算收入" in str(col):
                col_map[col] = "一般公共预算收入(亿元)"; fiscal_mapped = True
        df = df.rename(columns=col_map)
        df = df.loc[:, ~df.columns.duplicated()]   # 兜底：去除任何残留重复列

        # ── DQC ────────────────────────────────────────────────
        try:
            _dqc_fiscal(df, excel_path)
        except Exception as e:
            logger.error(f"[Fiscal] DQC失败，请检查：{excel_path.name}\n  {e}")
            continue

        # ── 写入省份和年份元数据列 ─────────────────────────────
        df["_province"]     = province
        df["_fiscal_year"]  = fiscal_year

        # 财力数值列强制转为 float（原始 Excel 中可能含 "-" 等非数值字符）
        if "一般公共预算收入(亿元)" in df.columns:
            df["一般公共预算收入(亿元)"] = pd.to_numeric(
                df["一般公共预算收入(亿元)"], 
                errors="coerce" #遇到真正的数字，它保留；遇到 "-" 或 "暂无"，它强行将其转为 NaN（空值）
            )
        keep = [c for c in ["城市", "一般公共预算收入(亿元)", "_province", "_fiscal_year"]
                if c in df.columns]
        df[keep].to_parquet(parquet_path, index=False, compression="snappy")
        logger.info(f"[Fiscal] ✅ {parquet_path.name}（{len(df)} 行）")
        generated.append(parquet_path)

    return generated


# ══════════════════════════════════════════════
# Step 3：在 DuckDB 注册 SQL 视图（服务层）
# ══════════════════════════════════════════════

def run_sql_file(con: duckdb.DuckDBPyConnection, sql_path: Path, params: dict | None = None) -> None:
    """
    读取单个 .sql 文件并在 DuckDB 连接上执行。

    支持通过 params 字典注入动态参数（Python str.format 风格）。
    主要用于将 Parquet 文件的绝对路径注入 Staging 层视图。

    示例：
      SQL 文件内容：SELECT * FROM read_parquet('{bond_parquet_path}')
      调用方式：   run_sql_file(con, path, {"bond_parquet_path": "/abs/path/bond.parquet"})

    参数：
      con       DuckDB 连接对象（由调用方管理生命周期）
      sql_path  .sql 文件的 Path 对象
      params    占位符替换字典，默认 None（不替换）
    """
    sql = sql_path.read_text(encoding="utf-8")
    if params:
        sql = sql.format(**params)
    con.execute(sql)
    logger.info(f"[DuckDB] ✅ {sql_path.name}")


def register_duckdb_views(snapshot_date: str, fiscal_year: str) -> None:
    """
    按 Staging → Intermediate → Marts 顺序读取并执行所有 .sql 模型文件，
    将视图和 Macro 持久化写入 DuckDB 服务文件。

    构建流：
      Staging 层    models/staging/stg_v_*.sql
                      ↓  读 Parquet，路径由 params 注入
      Intermediate 层  models/intermediate/int_*.sql
                      ↓  主体去重 + 财力 JOIN + 财力分段（唯一定义处）
      Marts 层      models/marts/mart_*.sql
                      ↓  Views + Macros，直接引用上层，无重复逻辑

    路径策略：
      使用 .resolve().as_posix() 生成绝对正斜杠路径，
      确保 Windows / macOS / Linux / Docker 环境全部兼容。
    """
    SERVING_DIR.mkdir(parents=True, exist_ok=True)

    # ── 动态路径参数（注入 Staging SQL 的占位符）──────────────────
    params = {
        "bond_parquet_path":   (BOND_LAKE_DIR / f"bond_{snapshot_date}.parquet").resolve().as_posix(),
        "fiscal_parquet_glob": (FISCAL_LAKE_DIR / "*.parquet").resolve().as_posix(),
    }

    logger.info(f"[DuckDB] Bond  路径：{params['bond_parquet_path']}")
    logger.info(f"[DuckDB] Fiscal路径：{params['fiscal_parquet_glob']}")


    # ── SQL 执行层级顺序（顺序不可打乱，下层依赖上层）────────────
    
    
    #原逻辑：会报错，因为mart_m_province_kpi.sql依赖mart_v_province_kpi.sql
    #但是按照字母顺序mart_m_province_kpi.sql在mart_v_province_kpi.sql前面，会先被读进duckdb
    
    SQL_LAYERS: list[tuple[str, list[Path]]] = [
        ("Staging",       sorted((MODELS_DIR / "staging").glob("stg_*.sql"))),
        ("Intermediate",  sorted((MODELS_DIR / "intermediate").glob("int_*.sql"))),
        ("Marts",         sorted((MODELS_DIR / "marts").glob("mart_*.sql"))),
    ]
    '''

    # Marts 层 ──mart_m_province_kpi.sql依赖mart_v_province_kpi.sql
    # 必须让mart_v_province_kpi.sql先于─mart_m_province_kpi.sql被读进duckdb
    # 

    # 生成一个sql列表，装的是每个sql文件的 Path 对象（即完整的路径信息），按字母顺序排序
    marts_sql_files = sorted((MODELS_DIR / "marts").glob("mart_*.sql"))
    
    # 将mart_v_province_kpi.sql的Path移动到列表最前面
    marts_sql_files.sort(key=lambda x: 0 if "province_summary" in x.name else 1)

    # 顺序不可打乱，下层依赖上层
    SQL_LAYERS: list[tuple[str, list[Path]]] = [
        ("Staging",      sorted((MODELS_DIR / "staging").glob("stg_*.sql"))),
        ("Intermediate", sorted((MODELS_DIR / "intermediate").glob("int_*.sql"))),
        ("Marts",        marts_sql_files), # 使用手动重排后的列表
    ]
    '''
    
    logger.info(f"[DuckDB] 连接：{DB_PATH}")
    
    # with 语句确保连接正常关闭，视图持久化写入磁盘
    with duckdb.connect(str(DB_PATH)) as con:
        for layer_name, sql_files in SQL_LAYERS:
            if not sql_files:
                logger.warning(f"[{layer_name}] 未找到任何 .sql 文件，跳过")
                continue
            logger.info(f"[{layer_name}] 开始注册（{len(sql_files)} 个文件）...")
            for sql_file in sql_files:
                # Staging 层需要注入路径参数，其他层直接执行
                file_params = params if layer_name == "Staging" else None
                run_sql_file(con, sql_file, file_params)

        # ── 验证核心视图可用 ──────────────────────────────────────
        bond_count   = con.execute("SELECT COUNT(*) FROM v_bond").fetchone()[0]
        fiscal_count = con.execute("SELECT COUNT(*) FROM v_fiscal").fetchone()[0]
        issuer_count = con.execute("SELECT COUNT(*) FROM v_issuer_profile").fetchone()[0]
        logger.info(
            f"[DuckDB] 视图验证通过："
            f"v_bond={bond_count:,}行  "
            f"v_fiscal={fiscal_count:,}行  "
            f"v_issuer_profile={issuer_count:,}行"
        )



# ══════════════════════════════════════════════
# Step 4：更新激活版本记录
# ══════════════════════════════════════════════

def update_active_version(snapshot_date: str, fiscal_year: str, dqc_report: list = None) -> None:
    """
    更新 active_version.json，记录当前激活的数据快照版本。

    这个文件的作用：
    - Streamlit 和 DuckDB 读取此文件，知道应该查询哪个 Parquet 快照
    - 切换历史快照时，只需改这个 JSON，不需要改任何代码或 SQL
    - 可以进行版本回滚：把 active_bond_snapshot 改为旧日期即可
    - list用来存放dqc的提示信息后续放在streamlit页面

    文件结构示例：
    {
        "active_bond_snapshot": "20230216",
        "active_fiscal_year": "2021",
        "updated_at": "2025-04-19T10:30:00",
        "bond_snapshots_available": ["20230216", "20240101"],
        "dqc_report": dqc_report or [],
        "note": "修改 active_bond_snapshot 可切换到任意历史快照版本"
    }
    """
    # 扫描已有快照，生成可用版本列表（列表推导式）
    available_snapshots = sorted([  #按日期顺序排好队
        re.search(r"\d{8}", p.name).group() #从文件名（如 bond_20230216.parquet）里精准抠出那 8 位日期。
        for p in BOND_LAKE_DIR.glob("bond_*.parquet") #去BOND_LAKE_DIR里找所有的债券Parquet文件
        if re.search(r"\d{8}", p.name) #质检过滤，只有文件名里包含 连续 8 位数字 的文件才准进入下一步。
    ])

    version_data = {
        "active_bond_snapshot":      snapshot_date,
        "active_fiscal_year":        fiscal_year,
        "updated_at":                datetime.now().isoformat(timespec="seconds"), #记录这套数据是哪秒钟生成的
        "bond_snapshots_available":  available_snapshots,
        "dqc_report": dqc_report or [],
        "note": "修改 active_bond_snapshot 的值可切换到任意历史快照版本，"
                "然后重新运行 build_warehouse.py --activate <版本号> 更新视图。",
    }

    SERVING_DIR.mkdir(parents=True, exist_ok=True)
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        json.dump(version_data, f, ensure_ascii=False, indent=2)

    logger.info(
        f"[Version] ✅ active_version.json 已更新：\n"
        f"  激活快照：{snapshot_date}  "
        f"  可用快照：{available_snapshots}"
    )


# ══════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════

def build(bond_excel_path: Path | None = None) -> str:
    """
    执行完整的数据仓库构建流程。

    返回：本次构建激活的快照版本号（如 "20230216"）
    """
    logger.info("=" * 60)
    logger.info("开始构建数据仓库（湖仓分离架构）")
    logger.info("=" * 60)

    # ── 确定目标债券 Excel ──────────────────────────────────────
    if bond_excel_path is None:
        
        # 扫描所有Excel，通过文件名中的日期字符串（如20230216）自动选取最新的 Excel 文件
        excel_files = list(RAW_BOND_DIR.glob("*.xlsx"))
        if not excel_files:
            raise FileNotFoundError(
                f"未在 {RAW_BOND_DIR} 找到任何 Excel 文件，"
                f"请将债券数据放入该目录后重新运行。"
            )

        bond_excel_path = max(
            excel_files,
            key=lambda p: re.search(r"20\d{6}", p.name).group() if re.search(r"20\d{6}", p.name) else ""
        )  

        logger.info(f"[Bond] 按照文件名日期自动选取最新文件：{bond_excel_path.name}")

    # ── Step 1：债券 Excel → Parquet ────────────────────────────
    bond_parquet , dqc_report = build_bond_parquet(bond_excel_path)

    # 从 Parquet 文件名提取版本号
    snapshot_date = re.search(r"\d{8}", bond_parquet.name).group()

    # ── Step 2：财力 Excel → Parquet ────────────────────────────
    fiscal_parquets = build_fiscal_parquets(RAW_FISCAL_DIR, FISCAL_YEAR)
    logger.info(f"[Fiscal] 共处理 {len(fiscal_parquets)} 个省份财力文件")

    # ── Step 3：DuckDB 注册视图 ──────────────────────────────────
    register_duckdb_views(snapshot_date, FISCAL_YEAR)

    # ── Step 4：更新版本记录 ─────────────────────────────────────
    update_active_version(snapshot_date, FISCAL_YEAR, dqc_report = dqc_report)

    logger.info("=" * 60)
    logger.info(f"✅数据仓库构建完成！激活版本：{snapshot_date}")
    logger.info(f"   DuckDB 服务文件：{DB_PATH}")
    logger.info(f"   Parquet 数据湖：{WAREHOUSE_DIR}")
    logger.info("=" * 60)

    return snapshot_date


def activate_snapshot(snapshot_date: str) -> None:
    """
    切换激活的债券数据快照版本（版本回滚 / 前滚）。

    不重新转换数据，只更新 DuckDB 视图指向和 active_version.json。
    用于：发现新数据有问题时秒级切换回旧版本。
    """
    parquet_path = BOND_LAKE_DIR / f"bond_{snapshot_date}.parquet"
    if not parquet_path.exists():
        available = [p.name for p in BOND_LAKE_DIR.glob("bond_*.parquet")]
        raise FileNotFoundError(
            f"快照 bond_{snapshot_date}.parquet 不存在。\n"
            f"可用快照：{available}"
        )

    logger.info(f"[Activate] 切换到快照版本：{snapshot_date}")
    register_duckdb_views(snapshot_date, FISCAL_YEAR)
    update_active_version(snapshot_date, FISCAL_YEAR)
    logger.info(f"[Activate] ✅ 已切换至版本 {snapshot_date}，无需重启应用")


# ──────────────────────────────────────────────
# CLI 入口（ Command Line Interface 命令行界面）
# 有了CLI，就在不改动任何一行代码的前提下，通过不同的指令让程序执行不同的任务。
# ──────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="区域信评展业引擎 · 数据仓库构建脚本（湖仓分离架构）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  # 自动选取最新 Excel，完整构建
  python scripts/build_warehouse.py

  # 指定特定 Excel 文件
  python scripts/build_warehouse.py --bond-file "全国城投债数据_WIND截止20240101.xlsx"

  # 切换到历史快照版本（不重新转换数据）
  python scripts/build_warehouse.py --activate 20230216
        """,
    )
    parser.add_argument(
        "--bond-file",
        type=str,
        default=None,
        help="指定债券 Excel 文件名（在 data/1发债数据/ 目录下）",
    )
    parser.add_argument(
        "--activate",
        type=str,
        default=None,
        metavar="SNAPSHOT_DATE",
        help="切换激活版本（格式：YYYYMMDD，如 20230216），不重新转换数据",
    )
    args = parser.parse_args()

    try:
        if args.activate:
            # 纯版本切换，不重新 ETL
            activate_snapshot(args.activate)
        else:
            bond_path = None
            if args.bond_file:
                bond_path = RAW_BOND_DIR / args.bond_file
                if not bond_path.exists():
                    logger.error(f"指定文件不存在：{bond_path}")
                    sys.exit(1)
            build(bond_path)
    except Exception as e:
        logger.error(f"构建失败：{e}")
        sys.exit(1)
