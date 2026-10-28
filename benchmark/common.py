# -*- coding: utf-8 -*-
"""
benchmark/common.py
===================
性能基准测试套件的公共工具层。

职责（只读，不修改任何项目代码/数据）：
  - env_info()      采集测试环境（CPU / 内存 / OS / 各库版本）
  - 路径定位         从 active_version.json 定位 parquet / duckdb / fiscal / raw excel
  - timeit/summarize 统一的计时与统计（中位数 / 最小 / 最大 / P95）
  - qdb()           只读连接 DuckDB 服务文件（与 App 的 query_duckdb 语义一致）
  - clear_caches()  清除 @st.cache_data 缓存，避免缓存掩盖真实耗时
  - save/load_json  结果落盘

设计原则：
  本套件绝不 import app/main.py（那会触发整个 Streamlit 脚本执行），
  而是像 scripts/compare_engines_test.py 一样，直接复用 models/ 下的
  纯函数与 DuckDB 视图，保证基准跑的就是「项目真实使用的查询/计算逻辑」。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import warnings
from pathlib import Path

# ── 在 bare Python 模式下调用 @st.cache_data 函数会产生
#    "No runtime found" / "missing ScriptRunContext" 之类告警，这里统一静音（不影响功能）。
#    关键：必须「先 import streamlit 触发其 init_logging()，再设置级别」，
#    否则随后 models/ 导入 streamlit 时其日志初始化会把级别重置回 INFO，覆盖此处的静音。
warnings.filterwarnings("ignore")

# 关键：必须在 import streamlit「之前」用环境变量把 streamlit 的 logger.level 直接设为 error。
# streamlit 的 _update_logger()（经 config.on_config_parsed 注册）会在「配置解析」时把全局
# 日志级别重置为 config 的 logger.level（默认 info），从而覆盖任何提前的 set_log_level(ERROR)；
# 而 st.cache_data / st.markdown 触发的日志器往往是「延迟创建」的，会在重置之后才产生
# "No runtime found / missing ScriptRunContext / to view this Streamlit app..." 等 WARNING 噪声。
# 从环境变量把默认值改成 error，才能让 _update_logger 每次都设成 ERROR，彻底压掉噪声。
os.environ.setdefault("STREAMLIT_LOGGER_LEVEL", "error")

import duckdb
import pandas as pd
import streamlit as _st  # noqa: F401  仅为触发 streamlit 日志初始化，便于随后静音

# 双保险：显式把 config 选项与全局级别都设为 ERROR（应对配置已解析 / 再次解析的情形）。
try:
    from streamlit import config as _st_config
    _st_config.set_option("logger.level", "error")
except Exception:
    pass
try:
    import streamlit.logger as _st_logger
    _st_logger.set_log_level(logging.ERROR)
except Exception:
    pass

# bare 模式下调用 st.cache_data / st.markdown 等会产生大量「No runtime found /
# missing ScriptRunContext / to view this app...」噪声；项目 DQC 也会 logger.warning。
# 逐一显式静音（getLogger 会创建尚不存在的 logger，将其级别固定为 ERROR）。
for _lg in (
    "streamlit",
    "streamlit.runtime",
    "streamlit.runtime.caching",
    "streamlit.runtime.caching.cache_data_api",
    "streamlit.runtime.scriptrunner",
    "streamlit.runtime.scriptrunner_utils",
    "streamlit.runtime.scriptrunner_utils.script_run_context",
    "streamlit.runtime.state",
    "streamlit.web",
    "streamlit.web.bootstrap",
    "models",
    "models.intermediate.int_data",
    "models.staging.stg_data",
    "models.marts.mart_credit_indicators",
):
    logging.getLogger(_lg).setLevel(logging.ERROR)
# 兜底：把此刻已存在的所有 streamlit.* / models.* 子 logger 一并降到 ERROR
for _name in list(logging.Logger.manager.loggerDict):
    if _name.startswith(("streamlit", "models")):
        logging.getLogger(_name).setLevel(logging.ERROR)

# ──────────────────────────────────────────────
# 路径常量
# ──────────────────────────────────────────────
BENCH_DIR     = Path(__file__).resolve().parent
PROJECT_ROOT  = BENCH_DIR.parent
RESULTS_DIR   = BENCH_DIR / "results"

# 保证能 import models/ 与 app/config（项目已 pip install -e .，此处再兜底一次）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR        = PROJECT_ROOT / "data"
SERVING_DIR     = DATA_DIR / "serving"
DB_PATH         = SERVING_DIR / "credit_indicators.duckdb"
VERSION_FILE    = SERVING_DIR / "active_version.json"
WAREHOUSE_DIR   = DATA_DIR / "warehouse"
BOND_LAKE_DIR   = WAREHOUSE_DIR / "bond"
FISCAL_LAKE_DIR = WAREHOUSE_DIR / "fiscal"
RAW_BOND_DIR    = DATA_DIR / "raw" / "bond"
RAW_FISCAL_DIR  = DATA_DIR / "raw" / "fiscal"
MODELS_DIR      = PROJECT_ROOT / "models"
MAIN_PY         = PROJECT_ROOT / "app" / "main.py"


# ──────────────────────────────────────────────
# 版本 / 激活快照
# ──────────────────────────────────────────────
def read_active_version() -> dict:
    """读取 active_version.json（不存在时返回空字典）。"""
    if not VERSION_FILE.exists():
        return {}
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def active_snapshot() -> str:
    return read_active_version().get("active_bond_snapshot", "") or ""


def fiscal_year() -> str:
    return read_active_version().get("active_fiscal_year", "2021") or "2021"


def bond_parquet_path() -> Path:
    """返回当前激活快照对应的 bond parquet 路径（找不到则取最新一期）。"""
    snap = active_snapshot()
    if snap:
        p = BOND_LAKE_DIR / f"bond_{snap}.parquet"
        if p.exists():
            return p
    files = sorted(BOND_LAKE_DIR.glob("bond_*.parquet"))
    if not files:
        raise FileNotFoundError(f"{BOND_LAKE_DIR} 下没有任何 bond_*.parquet，请先运行 scripts/build_warehouse.py")
    return files[-1]


def bond_excel_path() -> Path | None:
    """原始债券 Excel（app「降级路径」的数据源）：取 raw/bond 下最新修改的 .xlsx。

    与 app/main.py 的 _latest_excel、scripts/compare_engines_test.py 的取法一致。
    找不到（未提供原始 Excel）时返回 None，调用方据此优雅跳过口径③。
    """
    if not RAW_BOND_DIR.exists():
        return None
    files = list(RAW_BOND_DIR.glob("*.xlsx"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def raw_fiscal_excel(province: str, fy: str | None = None) -> Path:
    """原始单省财力 Excel 路径（app「降级路径」的财力数据源）。"""
    return RAW_FISCAL_DIR / f"{fy or fiscal_year()}年{province}财力.xlsx"


def fiscal_parquet_glob() -> str:
    """财力 parquet 通配路径（posix 正斜杠，供 DuckDB read_parquet 使用）。"""
    return (FISCAL_LAKE_DIR / "*.parquet").resolve().as_posix()


def available_provinces() -> list[str]:
    """复刻 main.py 的 _available_provinces()：从 raw/fiscal 目录列出可切换省份。"""
    fy = fiscal_year()
    if not RAW_FISCAL_DIR.exists():
        return []
    return sorted(
        p.stem.replace(f"{fy}年", "").replace("财力", "")
        for p in RAW_FISCAL_DIR.glob(f"{fy}年*财力.xlsx")
    )


# ──────────────────────────────────────────────
# 环境信息
# ──────────────────────────────────────────────
def _cpu_name() -> str:
    """尽量取到 CPU 的市场名称；失败则退回 platform.processor()。"""
    # Windows：读注册表（只读）
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        if name and name.strip():
            return name.strip()
    except Exception:
        pass
    # Linux：/proc/cpuinfo
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _total_memory_gb() -> float | None:
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / (1024 ** 3), 2)
    except Exception:
        return None


def _pkg_version(name: str) -> str:
    try:
        mod = __import__(name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return "not-installed"


def env_info() -> dict:
    """采集测试环境信息，供 report.md 使用。"""
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "platform": platform.platform(),
        "cpu": _cpu_name(),
        "cpu_cores_logical": os.cpu_count(),
        "memory_gb": _total_memory_gb(),
        "python": sys.version.split()[0],
        "pandas": _pkg_version("pandas"),
        "duckdb": _pkg_version("duckdb"),
        "pyarrow": _pkg_version("pyarrow"),
        "streamlit": _pkg_version("streamlit"),
        "numpy": _pkg_version("numpy"),
        "openpyxl": _pkg_version("openpyxl"),
        "psutil": _pkg_version("psutil"),
    }


# ──────────────────────────────────────────────
# 计时与统计
# ──────────────────────────────────────────────
def _percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear' 一致）。"""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    idx = (n - 1) * p
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def summarize(samples_ms: list[float]) -> dict:
    """把一组毫秒耗时样本汇总为 median/min/max/p95/mean。"""
    s = sorted(samples_ms)
    n = len(s)
    return {
        "reps": n,
        "median": round(_percentile(s, 0.5), 3),
        "min": round(s[0], 3) if n else None,
        "max": round(s[-1], 3) if n else None,
        "p95": round(_percentile(s, 0.95), 3),
        "mean": round(sum(s) / n, 3) if n else None,
        "first": round(samples_ms[0], 3) if samples_ms else None,  # 首轮（相对"冷"）
        "all": [round(x, 3) for x in samples_ms],
    }


def timeit(fn, warmup: int = 1, reps: int = 10, before_each=None) -> dict:
    """
    预热 warmup 次后，重复 reps 次计时（毫秒）。

    before_each：每次「计时前」调用的钩子（如 clear_caches），
                 其耗时不计入样本，用于消除缓存影响。
    """
    for _ in range(warmup):
        if before_each:
            before_each()
        fn()
    samples = []
    for _ in range(reps):
        if before_each:
            before_each()
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return summarize(samples)


# ──────────────────────────────────────────────
# DuckDB 只读访问
# ──────────────────────────────────────────────
def qdb(sql: str, params: list | None = None) -> pd.DataFrame:
    """对 DuckDB 服务文件执行一次性只读查询，用完即关（等价 App 的 query_duckdb）。"""
    if not DB_PATH.exists():
        return pd.DataFrame()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def qdb_one(sql: str, params: list | None = None, default=None):
    """执行查询并返回第一行第一列的标量。"""
    if not DB_PATH.exists():
        return default
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        row = con.execute(sql, params or []).fetchone()
        return row[0] if row else default
    finally:
        con.close()


def get_bond_df() -> pd.DataFrame:
    """
    取全量债券宽表（等价 App 的 stg_load_bond_data DuckDB 路径）：
      SELECT * FROM v_bond，并丢弃 build_warehouse 写入的 '_' 前缀元数据列。
    """
    df = qdb("SELECT * FROM v_bond")
    meta_cols = [c for c in df.columns if c.startswith("_")]
    return df.drop(columns=meta_cols, errors="ignore")


def largest_province_with_fiscal() -> str:
    """在「有财力数据的省份」里挑债券行数最多的省（让下钻查询有足够数据量）。"""
    provs = available_provinces()
    if not provs:
        return "四川省"
    best, best_n = provs[0], -1
    for p in provs:
        n = qdb_one("SELECT COUNT(*) FROM v_bond WHERE 省份 LIKE ?", [f"%{p[:2]}%"], 0) or 0
        if int(n) > best_n:
            best, best_n = p, int(n)
    return best


# ──────────────────────────────────────────────
# 缓存清理
# ──────────────────────────────────────────────
def clear_caches() -> None:
    """
    清除项目里 @st.cache_data 装饰函数的缓存，避免"缓存命中"掩盖真实耗时。
    在 bare Python 模式下若缓存未启用，clear() 亦为安全 no-op。
    """
    targets = []
    try:
        from models.staging.stg_data import stg_load_bond_data, stg_load_fiscal_data
        from models.intermediate.int_data import int_join_fiscal, int_build_issuer_view
        targets = [stg_load_bond_data, stg_load_fiscal_data, int_join_fiscal, int_build_issuer_view]
    except Exception:
        return
    for fn in targets:
        try:
            fn.clear()
        except Exception:
            pass


# ──────────────────────────────────────────────
# 结果落盘
# ──────────────────────────────────────────────
def save_json(name: str, obj: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_json(name: str) -> dict:
    with open(RESULTS_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# 格式化 & 打印
# ──────────────────────────────────────────────
def fmt_bytes(n: float | int) -> str:
    n = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_ms(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1000:
        return f"{x/1000:.3f} s"
    return f"{x:.2f} ms"


def header(title: str) -> None:
    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)


def info(msg: str) -> None:
    print(f"  · {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def require_warehouse() -> None:
    """前置检查：DuckDB 与 parquet 必须已就绪。"""
    if not DB_PATH.exists():
        raise SystemExit(
            f"[前置检查失败] 未找到 {DB_PATH}\n请先运行：python scripts/build_warehouse.py"
        )
    try:
        bond_parquet_path()
    except FileNotFoundError as e:
        raise SystemExit(f"[前置检查失败] {e}\n请先运行：python scripts/build_warehouse.py")
