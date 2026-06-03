-- models/marts/mart_v_financial_bench.sql
-- ============================================================
-- 【Marts · View】v_financial_bench
-- 财务基准指标生成器（Tab 2 准入门槛模拟器）

-- 包含字段：
--  省份 | 城市 | 城投行政级别 | 主体评级 | 样本数 | 总资产_最小 | 总资产_Q1 |
--  总资产_中位 | 总资产_均值 | 总资产_Q3 | 总资产_最大 | 净资产_最小 | 
--  净资产_Q1 | 净资产_中位 | 净资产_均值 | 净资产_Q3 | 净资产_最大 | 
--  营业收入_最小 | 营业收入_Q1 | 营业收入_中位 | 营业收入_均值 | 营业收入_Q3 | 营业收入_最大 | 
--  净利润_最小 | 净利润_Q1 | 净利润_中位 | 净利润_均值 | 净利润_Q3 | 净利润_最大 | 
--  负债率_最小 | 负债率_Q1 | 负债率_中位 | 负债率_均值 | 负债率_Q3 | 负债率_最大

-- 依赖：int_v_issuer_enriched（Intermediate）
-- 用途：Tab 2 准入门槛模拟器，财务基准分位数展示
-- ============================================================

CREATE OR REPLACE VIEW v_financial_bench AS
SELECT
    省份,
    COALESCE(NULLIF(TRIM(城市), ''), '未知城市') AS 城市,
    COALESCE(NULLIF(TRIM(城投行政级别), ''), '未知级别') AS 城投行政级别,
    COALESCE(NULLIF(TRIM(主体评级), ''), '未知评级') AS 主体评级,
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
WHERE 省份 IS NOT NULL
  AND 省份 != ''
  AND 主体评级 IS NOT NULL
  AND TRIM(主体评级) != ''
GROUP BY 省份, 城市, 城投行政级别, 主体评级;