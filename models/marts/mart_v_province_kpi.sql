-- models/marts/mart_v_province_kpi.sql
-- ============================================================
-- 【Marts · View】v_province_kpi
--  省级 KPI 汇总
--
-- 包含字段：
--  省份 | 总发行主体_家 | 已评级主体_家 | 债项总数_条 | 发行总额_亿 | 
--  平均票面利率_pct | 覆盖城市_个 | 评级机构数_家

-- 依赖： v_bond（Staging）
-- 用途:为 Streamlit 顶部 6 个 KPI 卡片提供数据支撑

-- ============================================================

CREATE OR REPLACE VIEW v_province_kpi AS
SELECT
    省份,
    -- 1. 主体维度：区分总数与已获评级数
    COUNT(DISTINCT 发行人中文名称) AS 总发行主体_家,
    COUNT(DISTINCT CASE 
        WHEN 主体评级 IS NOT NULL AND TRIM(主体评级) != '' 
        THEN 发行人中文名称 
    END) AS 已评级主体_家,  -- 此指标与 Python sc_zt 长度对齐

    -- 2. 债项维度：放开过滤，统计全量规模
    COUNT(*) AS 债项总数_条, -- 包含无评级债项，对齐 Pandas len(df_prov)
    ROUND(SUM(发行总额), 2) AS 发行总额_亿,
    ROUND(AVG(票面利率), 4) AS 平均票面利率_pct,
    
    -- 3. 覆盖维度
    COUNT(DISTINCT 城市) AS 覆盖城市_个,
    COUNT(DISTINCT 主体评级机构) AS 评级机构数_家
FROM v_bond
WHERE 省份 IS NOT NULL
GROUP BY 省份
ORDER BY 发行总额_亿 DESC;