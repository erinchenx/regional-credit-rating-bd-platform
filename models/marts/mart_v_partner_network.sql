-- models/marts/mart_v_partner_network.sql
-- ============================================================
-- 【Marts · View】v_partner_network
-- 主承销商共同客户分析（展业帮手圈）
--
-- 粒度：一省 × 一评级机构 × 一承销商 → 一行
-- 已承做主体数：该承销商在该省承做过的去重主体总数（COUNT DISTINCT，不按城市重复计算）
-- 共同客户数：  该承销商在该省承做过、且指定评级机构也评过的发行人去重数量
--
-- 包含字段：
--  省份 | 评级机构 | 主承销商 | 已承做主体数 | 共同客户数 | 共同客户列表 | 合作等级

-- 依赖：v_bond（Staging）
-- 用途：Tab 3 展业帮手圈
-- ============================================================

CREATE OR REPLACE VIEW v_partner_network AS
WITH

-- Step 1：基础范围（有评级 + 有承销商的债项）
scope AS (
    SELECT
        省份,
        城市,
        城投行政级别,
        发行人中文名称,
        主体评级机构,
        债项评级机构,
        主承销商
    FROM v_bond
    WHERE 省份          IS NOT NULL
      AND 主体评级       IS NOT NULL
      AND TRIM(主体评级) != ''
      AND 主承销商       IS NOT NULL
      AND 主承销商       != ''
),

-- Step 2：展开评级机构（主体评级机构 UNION 债项评级机构，两者都算我方客户）
agencies AS (
    SELECT DISTINCT
        省份,
        发行人中文名称,
        TRIM(unnest(string_split(主体评级机构, ','))) AS 评级机构
    FROM scope
    WHERE 主体评级机构 IS NOT NULL AND 主体评级机构 != ''

    UNION

    SELECT DISTINCT
        省份,
        发行人中文名称,
        TRIM(unnest(string_split(债项评级机构, ','))) AS 评级机构
    FROM scope
    WHERE 债项评级机构 IS NOT NULL AND 债项评级机构 != ''
),

-- Step 3：展开承销商（保留城市和级别列供下游筛选用）
underwriters AS (
    SELECT DISTINCT
        省份,
        城市,
        城投行政级别,
        发行人中文名称,
        TRIM(unnest(string_split(主承销商, ','))) AS 承销商
    FROM scope
),

-- Step 4：省级已承做主体数（COUNT DISTINCT，跨城市去重）
-- 业务口径 A：该承销商在该省承做过的去重主体总数
uw_stats AS (
    SELECT
        省份,
        承销商,
        COUNT(DISTINCT 发行人中文名称) AS 已承做主体数
    FROM underwriters
    GROUP BY 省份, 承销商
),

-- Step 5：省级共同客户（承销商承做过 AND 评级机构评过的发行人，省级去重）
cross_stats AS (
    SELECT
        u.省份,
        a.评级机构,
        u.承销商,
        COUNT(DISTINCT u.发行人中文名称)            AS 共同客户数,
        string_agg(DISTINCT u.发行人中文名称, '、') AS 共同客户列表
    FROM underwriters u
    INNER JOIN agencies a
        ON  u.发行人中文名称 = a.发行人中文名称
        AND u.省份           = a.省份
    GROUP BY u.省份, a.评级机构, u.承销商
)

-- Step 6：最终输出，粒度：省 × 评级机构 × 承销商
SELECT
    s.省份,
    COALESCE(c.评级机构, '未知评级机构')           AS 评级机构,
    s.承销商                                       AS 主承销商,
    s.已承做主体数,
    COALESCE(c.共同客户数,   0)                    AS 共同客户数,
    COALESCE(c.共同客户列表, '')                   AS 共同客户列表,
    CASE
        WHEN COALESCE(c.共同客户数, 0) >= 3 THEN '主要合作伙伴'
        WHEN COALESCE(c.共同客户数, 0) >= 1 THEN '初步合作对象'
        ELSE                                      '待开拓合作关系'
    END AS 合作等级
FROM uw_stats s
LEFT JOIN cross_stats c
    ON  s.省份   = c.省份
    AND s.承销商 = c.承销商
ORDER BY s.省份, 共同客户数 DESC, s.已承做主体数 DESC;