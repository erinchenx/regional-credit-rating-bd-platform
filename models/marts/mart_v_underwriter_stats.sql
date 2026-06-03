-- models/marts/mart_v_underwriter_stats.sql
-- ============================================================
-- 【Marts · View】v_underwriter_stats
-- 主承销商排名表

-- 包含字段：
--  省份 | 城市 | 城投行政级别 | 主体评级 | 城市财力区间 | 主承销商 |
--  已承做主体数 | 债项发行数 | 发行总额_亿 | 发行倍数

-- 依赖：v_bond（Staging）
-- 用途：Tab 3 主承销商排名表
-- ============================================================
CREATE OR REPLACE VIEW v_underwriter_stats AS
WITH
    -- 直接从 v_bond 展开承销商（不经过 int_v_issuer_enriched，避免评级过滤）
    exploded AS (
        SELECT
            TRIM(unnest(string_split(b.主承销商, ','))) AS 承销商,
            b.发行人中文名称,
            b.发行总额,
            b.省份,
            b.城市,
            b.城投行政级别,
            b.主体评级,
            COALESCE(e.城市财力区间, '财力未知') AS 城市财力区间
        FROM v_bond b
        LEFT JOIN int_v_issuer_enriched e ON b.发行人中文名称 = e.发行人中文名称
        WHERE b.主承销商 IS NOT NULL
          AND b.主承销商 != ''
    ),
    metrics AS (
        SELECT
            COALESCE(省份, '未知') AS 省份,
            城市, 城投行政级别, 主体评级, 城市财力区间,
            承销商,
            COUNT(DISTINCT 发行人中文名称) AS 已承做主体数,
            COUNT(*)                        AS 债项发行数,
            ROUND(SUM(发行总额), 2)         AS 发行总额_亿
        FROM exploded
        GROUP BY 省份, 城市, 城投行政级别, 主体评级, 城市财力区间, 承销商
    )
SELECT
    省份, 城市, 城投行政级别, 主体评级, 城市财力区间,
    ROW_NUMBER() OVER (
        PARTITION BY 省份, 城市, 城投行政级别, 主体评级, 城市财力区间
        ORDER BY 债项发行数 DESC
    ) AS 序号,
    承销商                                                      AS 主承销商,
    已承做主体数,
    债项发行数,
    发行总额_亿,
    ROUND(债项发行数::FLOAT / NULLIF(已承做主体数, 0), 2)       AS 发行倍数
FROM metrics;



