-- models/staging/stg_v_fiscal.sql
-- ============================================================
-- 【Staging】v_fiscal
-- 所有省份财力数据（一次扫描全部 Parquet）
--
-- 数据来源：data/warehouse/fiscal/*.parquet（每省一个文件）
-- 路径由 build_warehouse.py 的 run_sql_file() 在执行时注入。
--
-- 职责：
--   - 将所有省份的财力 Parquet 合并为一张可查询视图
--   - 字段：城市 | 一般公共预算收入(亿元) | _province | _fiscal_year
--
-- 依赖：无（直接读 Parquet glob）
-- 被引用：int_v_issuer_enriched（通过 MAX 聚合后 JOIN）
-- ============================================================

CREATE OR REPLACE VIEW v_fiscal AS
SELECT *
FROM read_parquet('{fiscal_parquet_glob}');
