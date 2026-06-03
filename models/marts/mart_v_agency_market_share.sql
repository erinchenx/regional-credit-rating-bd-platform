-- models/marts/mart_v_agency_market_share.sql
-- ============================================================
-- 【Marts · View】v_agency_market_share
-- 评级机构省内市场份额
--
-- 每行 = 某机构在某省的业务量统计。
-- 省内主体市占率用窗口函数在 PARTITION BY 省份 内计算，
-- 确保分母是该省所有机构的主体总数，而非全国总数。
--
-- 包含字段：
--   评级机构 | 省份 | 主体数 | 债项数 | 省内主体市占率_pct | 省内债项主体比
--
-- 依赖：v_bond（Staging）
-- 用途：Tab 0 机构排名表、饼图（业务广度）、柱状图（业务深度）
-- ============================================================


-- 如果需要展开多值字段来统计
CREATE OR REPLACE VIEW v_agency_market_share AS
WITH expanded_agencies AS (
    SELECT 
        TRIM(主体评级机构) AS 评级机构,
        省份,
        发行人中文名称
    FROM v_bond
    WHERE 主体评级机构 IS NOT NULL
      AND 主体评级机构 != ''
),
agency_stats AS (
    SELECT
        评级机构,
        省份,
        COUNT(DISTINCT 发行人中文名称) AS 主体数,
        COUNT(*) AS 债项数,
        ROUND(
            COUNT(DISTINCT 发行人中文名称) * 100.0 /
            NULLIF(
                SUM(COUNT(DISTINCT 发行人中文名称)) OVER (PARTITION BY 省份),
                0
            ),
            1
        ) AS 省内主体市占率_pct,
        ROUND(
            COUNT(*) * 1.0 /
            NULLIF(COUNT(DISTINCT 发行人中文名称), 0),
            2
        ) AS 省内债项主体比
    FROM expanded_agencies
    GROUP BY 评级机构, 省份
)
SELECT * FROM agency_stats
ORDER BY 省份, 主体数 DESC;