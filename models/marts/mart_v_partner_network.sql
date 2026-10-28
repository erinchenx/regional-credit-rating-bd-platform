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

-- Step 1：基础范围（有承销商的债项）
-- 不要求主体评级/最新债项评级非空，与 pandas 的 scope（df.dropna(subset=['主承销商'])）一致
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
      AND 主承销商       IS NOT NULL
      AND 主承销商       != ''
),

-- Step 2：展开承销商（从 v_bond 取，不要求债项有评级，与 pandas df_uw = scope.dropna(subset=['主承销商']) 一致）
underwriters AS (
    SELECT DISTINCT
        省份,
        城市,
        城投行政级别,
        发行人中文名称,
        TRIM(unnest(string_split(主承销商, ','))) AS 承销商
    FROM v_bond
    WHERE 省份          IS NOT NULL
      AND 主承销商       IS NOT NULL
      AND 主承销商       != ''
),

-- Step 3：省内所有评级机构（用于 CROSS JOIN，保证每个承销商对每个机构都出现）
distinct_agencies AS (
    SELECT DISTINCT 省份, TRIM(unnest(string_split(主体评级机构, ','))) AS 评级机构
    FROM scope WHERE 主体评级机构 IS NOT NULL AND 主体评级机构 != ''
    UNION
    SELECT DISTINCT 省份, TRIM(unnest(string_split(债项评级机构, ','))) AS 评级机构
    FROM scope WHERE 债项评级机构 IS NOT NULL AND 债项评级机构 != ''
),

-- Step 4：各评级机构覆盖的发行人（LIKE 子串匹配，与 pandas .str.contains 一致）
-- 业务口径：只要发行人的任一债项的主体/债项评级机构字段包含该机构名，即视为该机构的客户
my_issuers AS (
    SELECT DISTINCT
        s1.省份,
        ag.评级机构,
        s1.发行人中文名称
    FROM scope s1
    CROSS JOIN distinct_agencies ag
    WHERE s1.省份 = ag.省份
      AND (
          s1.主体评级机构 LIKE '%' || ag.评级机构 || '%'
       OR s1.债项评级机构 LIKE '%' || ag.评级机构 || '%'
      )
),

-- Step 5：省级已承做主体数（COUNT DISTINCT，跨城市去重）
uw_stats AS (
    SELECT
        省份,
        承销商,
        COUNT(DISTINCT 发行人中文名称) AS 已承做主体数,
        string_agg(DISTINCT 发行人中文名称, '、') AS 已承做主体列表
    FROM underwriters
    GROUP BY 省份, 承销商
),

-- Step 6：共同客户（承销商承做过的发行人 ∩ 评级机构覆盖的发行人）
cross_stats AS (
    SELECT
        u.省份,
        mi.评级机构,
        u.承销商,
        COUNT(DISTINCT u.发行人中文名称)            AS 共同客户数,
        string_agg(DISTINCT u.发行人中文名称, '、') AS 共同客户列表
    FROM underwriters u
    INNER JOIN my_issuers mi
        ON  u.省份           = mi.省份
        AND u.发行人中文名称 = mi.发行人中文名称
    GROUP BY u.省份, mi.评级机构, u.承销商
),

-- Step 7：全组合（省×承销商 × 省×评级机构），确保所有承销商对每个评级机构都出现
all_combos AS (
    SELECT
        uw.省份,
        ag.评级机构,
        uw.承销商
    FROM uw_stats uw
    INNER JOIN distinct_agencies ag ON uw.省份 = ag.省份
)

-- Step 8：最终输出，粒度：省 × 评级机构 × 承销商
SELECT
    ac.省份,
    ac.评级机构,
    ac.承销商                                        AS 主承销商,
    uw.已承做主体数,
    uw.已承做主体列表,
    COALESCE(cs.共同客户数,   0)                     AS 共同客户数,
    COALESCE(cs.共同客户列表, '')                    AS 共同客户列表,
    CASE
        WHEN COALESCE(cs.共同客户数, 0) >= 2 THEN '主要合作伙伴'
        WHEN COALESCE(cs.共同客户数, 0) >= 1 THEN '初步合作对象'
        ELSE                                         '待开拓合作关系'
    END AS 合作等级
FROM all_combos ac
LEFT JOIN uw_stats    uw ON ac.省份 = uw.省份 AND ac.承销商 = uw.承销商
LEFT JOIN cross_stats cs ON ac.省份 = cs.省份 AND ac.评级机构 = cs.评级机构 AND ac.承销商 = cs.承销商
ORDER BY ac.省份, 共同客户数 DESC, uw.已承做主体数 DESC;