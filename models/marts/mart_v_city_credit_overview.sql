-- models/marts/mart_v_city_credit_overview.sql
-- ============================================================
-- 【Marts · View】v_city_credit_overview
-- 城市信用概览（省内财力排名、评级×级别主体分布）

-- 包含字段：
--  省份 | 城市 | 城市财力_亿元 | 省内财力排名 | 省内城市总数 | 
--  城市主体总数 | 主体评级 | 该评级主体数 | 城投行政级别 | 细分主体数

-- 依赖：int_v_issuer_enriched（Intermediate）
-- 用途：Tab 1 城市主体明细
-- ============================================================

CREATE OR REPLACE VIEW v_city_credit_overview AS
WITH

    -- Step 1：不再筛选，直接用全量数据（相比宏，去掉了 WHERE 参数条件）
    filtered AS (
        SELECT *
        FROM int_v_issuer_enriched
    ),

    -- Step 2：城市财力排名（相比宏，窗口函数加了 PARTITION BY 省份）
    city_rank AS (
        SELECT
            省份,
            城市,
            城市财力_亿元,
            DENSE_RANK() OVER (
                PARTITION BY 省份
                ORDER BY 城市财力_亿元 DESC NULLS LAST
            ) AS 省内财力排名,
            COUNT(*) OVER (PARTITION BY 省份) AS 省内城市总数
        FROM filtered
        GROUP BY 省份, 城市, 城市财力_亿元
    ),

    -- Step 3：城市 × 评级 汇总（相比宏，GROUP BY 和窗口函数加了省份）
    rating_summary AS (
        SELECT
            省份,
            城市,
            主体评级,
            SUM(COUNT(*)) OVER (PARTITION BY 省份, 城市) AS 城市主体总数,
            COUNT(*) AS 该评级主体数
        FROM filtered
        GROUP BY 省份, 城市, 主体评级
    ),

    -- Step 4：城市 × 评级 × 行政级别 明细（相比宏，GROUP BY 加了省份）
    detail AS (
        SELECT
            省份,
            城市,
            主体评级,
            城投行政级别,
            COUNT(*) AS 细分主体数
        FROM filtered
        GROUP BY 省份, 城市, 主体评级, 城投行政级别
    )

    -- Step 5：四表关联（相比宏，JOIN 条件加了省份）
    SELECT
        d.省份,
        d.城市,
        r.城市财力_亿元,
        r.省内财力排名,
        r.省内城市总数,
        rs.城市主体总数,
        d.主体评级,
        rs.该评级主体数,
        d.城投行政级别,
        d.细分主体数
    FROM detail d
    JOIN city_rank r     ON d.省份 = r.省份 AND d.城市 = r.城市
    JOIN rating_summary rs ON d.省份 = rs.省份 AND d.城市 = rs.城市 AND d.主体评级 = rs.主体评级
    ORDER BY d.省份, r.省内财力排名, d.主体评级 DESC, d.城投行政级别;