-- models/staging/stg_v_bond.sql
-- ============================================================
-- 【Staging】v_bond
-- 当前激活版本的债券原始宽表
--
-- 数据来源：data/warehouse/bond/bond_<snapshot_date>.parquet
-- 路径由 build_warehouse.py 的 run_sql_file() 在执行时注入。
--
-- 职责：
--   - 将 Parquet 文件暴露为可查询的 SQL 视图
--   - 不做任何业务逻辑，保持原始字段和行数
--   - 所有下游视图的原始数据来源
--
-- 依赖：无（直接读 Parquet）
-- 被引用：v_fiscal, int_v_issuer_enriched, v_province_kpi,
--          v_agency_market_share, m_underwriter_stats, m_partner_network
-- ============================================================

CREATE OR REPLACE VIEW v_bond AS
SELECT *
FROM read_parquet('{bond_parquet_path}');
