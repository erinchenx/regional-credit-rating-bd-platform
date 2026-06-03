-- models/marts/mart_v_issuer_profile.sql
-- ============================================================
-- 【Marts · View】v_issuer_profile
-- 去重主体视图（标准接口层）
--
-- 这是一个薄包装层（Thin Wrapper）。
-- 所有业务逻辑已封装在 int_v_issuer_enriched，
-- 此视图仅作为对外的稳定接口名称，
-- 确保 Streamlit、Tableau 等下游工具引用的名称不变。
--
-- 包含字段：
--   发行人中文名称 | 发行人中文简称 | 省份 | 城市 | 城投行政级别
--   主体评级 | 主体评级机构 | 总资产 | 净资产 | 营业收入 | 净利润
--   资产负债率 | 财务报告期 | 行政级别量化 | 主体级别量化
--   城市财力_亿元 | 城市财力区间
--
-- 依赖：int_v_issuer_enriched（Intermediate）
-- 用途：Tab 1 主体地图 / 名单, Tab 2 准入门槛分位数基础数据
-- ============================================================

CREATE OR REPLACE VIEW v_issuer_profile AS
SELECT *
FROM int_v_issuer_enriched;
