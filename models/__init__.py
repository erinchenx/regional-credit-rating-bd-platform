# -*- coding: utf-8 -*-
"""
models/__init__.py
==================
顶层包入口，将三层模型的公开符号统一重导出。

用途：
  允许 main.py 通过扁平路径导入，例如：
    from models import stg_load_bond_data, int_join_fiscal, mart_agency_stats

  也可继续使用分层路径（推荐，语义更清晰）：
    from models.staging.stg_data import stg_load_bond_data
"""

# ── Staging ──────────────────────────────────
from models.staging.stg_data import (          # noqa: F401
    _file_mtime,
    stg_load_bond_data,
    stg_load_fiscal_data,
)

# ── Intermediate ─────────────────────────────
from models.intermediate.int_data import (     # noqa: F401
    FISCAL_COL,
    int_filter_province,
    int_join_fiscal,
    int_build_issuer_view,
)

# ── Marts ────────────────────────────────────
from models.marts.mart_credit_indicators import (   # noqa: F401
    FISCAL_BANDS,
    RATING_ORDER,
    ADMIN_ORDER,
    AGENCY_FULLNAME,
    mart_agency_stats,
    mart_underwriter_stats,
    mart_financial_bench,
    mart_partner_network,
    mart_competition_matrix,
)
