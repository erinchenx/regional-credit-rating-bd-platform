-- models/marts/mart_v_agency_competitive_landscape.sql
-- ============================================================
-- 【Marts · View】v_agency_competitive_landscape
-- 评级机构省内竞争格局（四维分析基础宽表）
--
-- 包含字段：
--   发行人中文名称 | 省份 | 城市 | 城投行政级别 | 主体评级
--   主体评级机构 | 城市财力_亿元 | 城市财力区间
--      
--   在 Tableau 中的使用方式：
--    行     = 主体评级机构
--    列     = 城市 / 城投行政级别 / 主体评级 / 城市财力区间（四选一，可用参数做动态切换）
--    标记   = 矩形 (Square)
--    颜色   = COUNTD(发行人中文名称)  -- 呈现热力深浅
--    标签   = COUNTD(发行人中文名称)  -- 显示具体主体家数
--    筛选器 = 省份

-- 依赖：int_v_issuer_enriched（Intermediate）
-- 用途：Tab 0 竞争热力矩阵
-- ============================================================

CREATE OR REPLACE VIEW v_agency_competitive_landscape AS
SELECT
    发行人中文名称,
    省份,
    城市,
    城投行政级别,
    主体评级,
    主体评级机构,
    城市财力_亿元,
    城市财力区间
FROM int_v_issuer_enriched;
