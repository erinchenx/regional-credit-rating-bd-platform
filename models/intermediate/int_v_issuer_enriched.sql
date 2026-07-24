-- models/intermediate/int_v_issuer_enriched.sql
-- ============================================================
-- 【Intermediate】int_v_issuer_enriched
-- 去重主体 + 财力关联 + 财力分段
--
-- 这是整个 Marts 层的核心中间视图，封装三段复用逻辑：
--   1. 主体去重：每家城投平台保留一条记录（DISTINCT ON）
--   2. 财力关联：LEFT JOIN 城市财力，每城市取最大值
--   3. 财力分段：统一的 CASE WHEN 区间划分
--
-- 设计动机（DRY 原则）：
--   财力 JOIN 和财力分段 CASE WHEN 在原代码中出现了两次：
--     - v_agency_competitive_landscape
--     - m_underwriter_stats
--   将这段逻辑上移至此中间视图后，所有下游直接引用，
--   未来修改分段边界（如 100 → 150 亿）只需改这一处。
--
-- 依赖：v_bond（Staging）, v_fiscal（Staging）
-- 被引用：
--   v_issuer_profile                  → SELECT * FROM int_v_issuer_enriched
--   v_agency_competitive_landscape    → SELECT 竞争分析字段
--   m_financial_bench                 → WHERE 筛选后聚合分位数
--   m_city_credit_overview             → WHERE 筛选后按城市汇总
--   m_underwriter_stats               → 通过 v_bond JOIN 此视图获取财力分段
-- ============================================================

CREATE OR REPLACE VIEW int_v_issuer_enriched AS

WITH

-- ── Step 1：主体去重 ───────────────────────────────────────
-- 对 v_bond 按发行人去重，每家平台只保留一条代表性记录。
-- ORDER BY 行政级别量化 DESC 确保优先保留行政级别最高的那条，
-- 与 Streamlit 中 int_build_issuer_view() 的逻辑保持一致。
deduped_issuers AS (
    SELECT DISTINCT ON (发行人中文名称)
        发行人中文名称,
        发行人中文简称,
        实际控制人,
        省份,
        城市,
        城投行政级别,
        主体评级,
        主体评级机构,
        总资产,
        净资产,
        营业收入,
        净利润,
        资产负债率,
        财务报告期,
        行政级别量化,
        主体级别量化
    FROM v_bond
    WHERE 主体评级 IS NOT NULL
      AND TRIM(主体评级) != ''
    ORDER BY 发行人中文名称, 行政级别量化 DESC
),

-- ── Step 2：财力聚合 ───────────────────────────────────────
-- 每个城市取最大财力值，处理一城多行的情况。
-- 在 Staging 层，财力数据已按省份分文件存储，
-- 合并后同一城市可能出现多行（如多年数据），
-- 取 MAX 确保与 Streamlit 的 int_join_fiscal() 逻辑一致。
city_fiscal AS (
    SELECT
        城市,
        MAX("一般公共预算收入(亿元)") AS "一般公共预算收入(亿元)"
    FROM v_fiscal
    GROUP BY 城市
)

-- ── Step 3：关联 + 财力分段 ────────────────────────────────
-- 唯一的 CASE WHEN 财力分段定义，所有下游视图引用此处，
-- 不再各自重复计算。
SELECT
    i.*,
    f."一般公共预算收入(亿元)"                               AS "城市财力_亿元",
    CASE
        WHEN f."一般公共预算收入(亿元)" IS NULL  THEN '财力未知'
        WHEN f."一般公共预算收入(亿元)" <  100   THEN '0-100亿'
        WHEN f."一般公共预算收入(亿元)" <  200   THEN '100-200亿'
        WHEN f."一般公共预算收入(亿元)" <  300   THEN '200-300亿'
        WHEN f."一般公共预算收入(亿元)" <  500   THEN '300-500亿'
        WHEN f."一般公共预算收入(亿元)" <  700   THEN '500-700亿'
        WHEN f."一般公共预算收入(亿元)" < 1000   THEN '700-1000亿'
        ELSE                                          '1000亿以上'
    END                                                      AS 城市财力区间

FROM deduped_issuers i
LEFT JOIN city_fiscal f ON i.城市 = f.城市;
