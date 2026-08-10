# 区域信用评级展业引擎

> **Regional Credit Rating BD Platform** — 基于湖仓分离架构的信评展业分析平台

面向信用评级机构的区域展业分析平台。基于 Wind 城投债与地方财力数据，覆盖评级机构市场格局、已评级主体概览、财务准入模拟、展业帮手圈、发债案例库（含 HTML 导出）五大核心模块，实现从底层数据到展业决策的端到端交付。

**An end-to-end analytics solution for credit rating agencies built on a modern Lakehouse architecture (Parquet + DuckDB). By standardizing and modeling LGFV bond and local fiscal data from Wind, the platform delivers five core business modules: Credit Rating Market Landscape, Rated Entity Distribution, Financial Admission Simulation, Partner Network, and Rating Case Studies with HTML export functionality for generating client-ready portable reports. It bridges the gap between raw financial data and strategic business development decisions.**

---

## 目录

- [项目定位](#项目定位)
- [技术架构](#技术架构)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [数据管理与版本控制](#数据管理与版本控制)
- [模块说明](#模块说明)
- [数据质量检查(DQC)](#数据质量检查dqc)
- [双引擎设计说明](#双引擎设计说明)
- [DuckDB SQL 视图](#duckdb-sql-视图)
- [技术选型说明](#技术选型说明)
- [交付方案说明](#交付方案说明)

---

## 项目定位

本项目是基于真实金融业务场景构建的端到端数据产品，旨在消除原始金融数据与业务决策间的鸿沟，为信评机构提供数据驱动的展业支持。

本项目采用了 Modern Data Stack 的轻量化实现。数据流分为三层：原始层保留 Wind 导出的 Excel 文件作为数据证据；标准层通过 ETL 脚本将 Excel 转为 Parquet， Parquet读取速度比 Excel 快20倍，且可以被 Python、Java、Spark 或云平台直接消费；服务层基于 DuckDB 构建，DuckDB 直接扫描 Parquet 文件，通过预定义 SQL 视图向下游提供标准化的数据接口，实现一次建模、多端交付。技术细节见下方[技术架构](#技术架构)。

- **数据源**：Wind 城投债全量导出 + 财汇/Wind数据库地方财力数据
- **交付物 1**：Streamlit 看板，供业务人员、分析师、管理层日常使用
- **交付物 2**：`credit_indicators.duckdb` 文件，客户可直接用 Tableau / Power BI 等连接做自定义分析

> 交付细节见[交付方案说明](#交付方案说明)

---

## 技术架构

本项目参考 **Modern Data Stack** 设计，实现了轻量化的湖仓分离架构：

```
原始层 (Raw)
  └── Excel 文件，原封不动保留，作为数据证据
       data/raw/bond/全国城投债数据_WIND截止20230216.xlsx
       data/raw/fiscal/2021年四川省财力.xlsx ...
            │
            │  scripts/build_warehouse.py（ETL，一次性运行）
            │  ① pd.read_excel
            │  ② 列名归一化 + DQC 检查
            │  ③ df.to_parquet
            ▼
标准层 (Data Lake · Parquet)
  └── 列式存储，列名已归一化，DQC 已通过，历史快照永久保留
       data/warehouse/bond/bond_20230216.parquet      (债券数据)
       data/warehouse/fiscal/四川省_2021.parquet   ...（财力数据）
            │
            │  ④ CREATE OR REPLACE VIEW（由 models/ 下的 .sql 文件定义）
            ▼
服务层 (Data Warehouse · DuckDB)
  └── 只存 SQL 视图定义，不存数据，体积极小
       data/serving/credit_indicators.duckdb        (268 KB)
       data/serving/active_version.json
            │
            ├──► Streamlit 看板（app/main.py）
            └──► Tableau / Power BI / DBeaver（直连 .duckdb 文件）
```

> 应用层采用双引擎架构（DuckDB 主引擎 + pandas 降级路径），确保仓库未初始化时应用不崩溃，自动降级运行。详见[双引擎设计说明](#双引擎设计说明)。

**设计原则：**

- **版本溯源**：`.py` 和 `.sql` 是 源代码，保存在 GitHub 里进行版本控制。每期快照独立保存，通过 `active_version.json` 控制激活版本，一行配置实现秒级回滚。每期城投债数据为时间点快照，历史版本独立保留，支持客户回溯任意时间节点的市场格局。
- **数据可移植性**：`.parquet`文件 是清洗后高性能、标准化的中间层数据资产，具有高拓展性，可被 Spark、Java、R、云平台直接消费。客户未来迁移至 Spark 或云平台时，无需重新清洗数据，保护数据资产的长期价值。
- **计算存储分离**：逻辑计算在`.duckdb` 文件，数据在`.parquet`，两者可独立演进。客户的 IT 团队可以单独升级分析逻辑而不影响已有数据资产，降低维护成本。
- **资产化交付**：这套方案中沉淀的 `.duckdb`资产不仅可以支撑 Streamlit 前端，还能通过内置的 **SQL 视图** 为下游（Tableau, PowerBI, API）提供统一、标准、资产化的数据服务接口——同一份数据资产多端复用，无需为不同受众重复建设数据管道。

---

## 项目结构

```
.
├── app/
│   ├── main.py                  # Streamlit 应用入口（UI 层）
│   └── config.py                # DQC 规则配置（阈值、必填字段）
│
├── scripts/
│   ├── build_warehouse.py       # ETL + SQL 加载器，Excel→Parquet→DuckDB 视图
│   └── compare_engines_test.py  # 双引擎一致性对照测试（SQL vs pandas）
│
├── models/                      # 数据建模层（参考 dbt 分层思维）
│   ├── staging/
│   │   ├── stg_data.py          # 原始数据读取、列名归一化、DQC
│   │   ├── stg_v_bond.sql       # v_bond 视图定义（含 Parquet 路径占位符）
│   │   └── stg_v_fiscal.sql     # v_fiscal 视图定义（含 Parquet 路径占位符）
│   │
│   ├── intermediate/
│   │   ├── int_data.py          # 多表关联、主体视图构建、DQC
│   │   └── int_v_issuer_enriched.sql  # 核心中间层：主体去重 + 财力 JOIN + 财力分段
│   │
│   └── marts/
│       ├── mart_credit_indicators.py        # pandas 降级路径聚合函数
│       ├── mart_v_province_kpi.sql          # 省份 KPI 汇总（6 个指标）
│       ├── mart_v_issuer_profile.sql        # 去重主体视图（含财力字段）
│       ├── mart_v_agency_market_share.sql   # 评级机构省内市场份额
│       ├── mart_v_agency_competitive_landscape.sql  # 评级机构竞争格局宽表
│       ├── mart_v_city_credit_overview.sql  # 城市已评级主体概览
│       ├── mart_v_financial_bench.sql       # 财务指标准入分位数（全组合宽表）
│       ├── mart_v_underwriter_stats.sql     # 主承销商排名（全维度宽表）
│       └── mart_v_partner_network.sql          # 展业伙伴网络（共同客户分析）
│
├── data/
│   ├── raw/                     # 【原始层】债券 Excel 及 各省财力 Excel
│   │   ├── bond/     
│   │   │    └── 全国城投债数据_WIND截止20230216.xlsx
│   │   └── fiscal/  
│   │        ├── 2021年四川省财力.xlsx  
│   │        └── ...（共 13 省）
│   │  
│   ├── warehouse/               # 【标准层】Parquet 数据湖
│   │   ├── bond/    
│   │   │   └── bond_20230216.parquet   #  由 build_warehouse.py 运行后生成
│   │   └── fiscal/
│   │       ├── 四川省_2021.parquet      #  由 build_warehouse.py 运行后生成
│   │       └── ...
│   └── serving/                      # 【服务层】DuckDB 文件
│       ├── credit_indicators.duckdb  #  由 build_warehouse.py 运行后生成
│       └── active_version.json       #  由 build_warehouse.py 运行后生成
│
│
├── .gitignore                   # Git忽略规则
├── pyproject.toml               # 包安装配置（pip install -e .）
├── requirements.txt             # 部署依赖清单
└── README.md
```

---

## 快速开始

### 环境要求

- Python 3.11+
- 操作系统：Windows / macOS / Linux

### 安装

```bash
# 1. 克隆项目
git clone <repo-url>
cd <project-dir>

# 2. 创建虚拟环境
python -m venv venv

# 3. 激活虚拟环境
# Windows:
.\venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 安装项目包（解决 models/ 跨目录 import 问题）
pip install -e .
```

### 初始化数据仓库（首次部署必须执行一次）

```bash
python scripts/build_warehouse.py
```

执行完成后会看到：

```
✅ Bond Parquet 写出完成：bond_20230216.parquet（21,156 行，1,183 KB）
✅ Fiscal Parquets：12 个省份文件
✅ DuckDB views 注册完成：v_bond / v_fiscal / v_issuer_profile / ... 共 10 个视图
✅ active_version.json：snapshot=20230216
```

### 双引擎一致性验证（可选，建议在首次及数据更新后运行）

```bash
python scripts/compare_engines_test.py            # 自动选择数据量适中的省份
python scripts/compare_engines_test.py 江苏省      # 手动指定省份
python scripts/compare_engines_test.py 四川省 --agency 中诚信国际信用评级有限责任公司 #默认设置使用中诚信来测试，可以修改
```

### 启动应用

```bash
# 从项目根目录运行（二选一，效果完全相同）
streamlit run app/main.py
python -m streamlit run app/main.py
```

侧边栏会显示当前数据读取模式：

- 🟢 **DuckDB 仓库模式**：`build_warehouse.py` 已运行，读取速度约 350ms
- 🟡 **Excel 降级模式**：仓库未初始化，自动回退读取 Excel，约 7,000ms

---

## 数据管理与版本控制

### 收到新期债券数据

```bash
# 1. 将新 Excel 放入 data/raw/bond/ 目录
#    文件名需包含 8 位日期，如：全国城投债数据_WIND截止20240101.xlsx

# 2. 重新运行 ETL 脚本
python scripts/build_warehouse.py
```

`build_warehouse.py`自动从文件名提取日期版本号，选取日期最新的Excel进行处理，生成带日期戳的 Parquet 快照并激活，旧快照永久保留：

```
data/warehouse/bond/
  bond_20230216.parquet   ← 第一期（保留）
  bond_20240101.parquet   ← 第二期（当前激活）
```

### 指定处理某期债券数据

```bash
python scripts/build_warehouse.py --bond-file "全国城投债数据_WIND截止20240101.xlsx"
```

### 版本回滚

```bash
python scripts/build_warehouse.py --activate 20230216
```

不重新处理 Excel，只更新 DuckDB 视图指向和 `active_version.json`，秒级生效。`v_bond` 视图立即指向对应 Parquet，Streamlit 缓存自动失效，刷新页面即可看到历史数据。

### 版本管理

`active_version.json` 由`build_warehouse.py`自动维护，记录当前激活版本与所有可用快照，无需手动修改。`active_version.json` 示例：

```json
{
  "active_bond_snapshot": "20240101",
  "active_fiscal_year": "2021",
  "updated_at": "2025-04-19T10:30:00",
  "bond_snapshots_available": ["20230216", "20240101"],
  "note": "此文件由 build_warehouse.py 自动维护，如需切换历史快照请运行 --activate <版本号>"
}
```

---

## 模块说明

### `models/staging/stg_data.py` — Staging 层

负责从存储层读取数据，完成列名归一化和 DQC 检查。

**读取策略（双模式自动切换）：**

| 条件                              | 读取方式                   | 速度     |
| --------------------------------- | -------------------------- | -------- |
| `credit_indicators.duckdb` 存在 | DuckDB 读取`v_bond` 视图 | ~350ms   |
| DuckDB 不存在或读取失败           | 直接读取 Excel             | ~7,000ms |

**主要函数：**

- `stg_load_bond_data()` — 读取全国城投债数据（DuckDB 优先）
- `stg_load_fiscal_data()` — 读取单省财力数据（DuckDB 优先）
- `_file_mtime()` — 文件修改时间（Excel 模式缓存 key）
- `_read_active_version()` — 读取激活版本配置

### `models/intermediate/int_data.py` — Intermediate 层

负责多表关联和数据重塑，不涉及数据读取。

**主要函数：**

- `int_filter_province()` — 按省份切片
- `int_join_fiscal()` — 债券 × 财力左连接，附 DQC 关联质量审计
- `int_build_issuer_view()` — 构建去重主体视图（pandas 降级路径）

### `models/marts/mart_credit_indicators.py` — Marts 层（pandas 降级路径）

**这是双引擎架构中的 pandas 降级路径**，在 DuckDB 不可用时自动启用。正常运行时，各 Tab 的数据由对应的 DuckDB SQL 视图提供，此文件的函数作为高可用兜底。

**主要函数：**

- `mart_agency_stats()` — 评级机构市场格局表
- `mart_competition_matrix()` — 竞争热力矩阵（**仅 pandas**，四维动态透视不适合固化为静态 SQL 视图）
- `mart_financial_bench()` — 财务基准准入门槛
- `mart_underwriter_stats()` — 主承销商排名
- `mart_partner_network()` — 展业帮手圈（共同客户分析）
- `dqc_marts_platform_dismatch()` — 省级平台财力适配审计

### `app/config.py` — DQC 规则配置

集中管理所有 DQC 阈值和字段规则，与列名映射逻辑分离。

```
BOND_SCHEMA_REQUIRED              # 必须存在的字段（Schema Check）
BOND_CRITICAL_NULL_TOLERANCE = 0.02   # 关键维度缺失率上限（>2% 报错）
BOND_NULL_WARN_THRESHOLD     = 0.80   # 财务字段空值率警告阈值
INT_JOIN_EXPANSION_THRESHOLD = 1.01   # 关联膨胀率上限
INT_JOIN_MATCH_RATE_THRESHOLD = 0.80  # 财力匹配率下限
```

---

## 数据质量检查(DQC)

每次数据流经各层时自动执行三级防御：

```
Staging 层
  ├── Schema Check（硬错误）
  │     列名归一化后核心列全为空 → raise ValueError，阻断流程
  ├── Value Check - 关键维度（硬错误）
  │     发行人/省份/城市 缺失率 ≤2%：自动剔除并警告（不中断）
  │                               >2%：raise ValueError，阻断流程
  └── Value Check - 财务字段（软警告）
  │     总资产/净利润等 空值率 >80% → 警告提示（不中断）
  └── City-name Check（软警告）
        财力数据城市名长度 < 2 字符 → 警告提示（不中断）

Intermediate 层
  ├── 关联膨胀检查（硬错误）
  │     Join 后行数 / 原行数 > 1.01 → raise ValueError
  └── 财力覆盖率检查（软警告）
        债券匹配到财力的比例 <80% → 显示未匹配城市列表

Marts 层
  └── 省级平台财力适配审计（软警告）
        省级平台所在城市财力低于全省中位线 → 标记为"平台行政级别与所属城市财力不匹配"
```

> **跳过策略**：`build_warehouse.py` 在 parquet 已存在时仍会执行 DQC 检查并输出质量警告，仅跳过 `to_parquet` 写入步骤，确保数据质量检查信息仍显示在终端及前端。

---

## 双引擎设计说明

### 设计动机

Streamlit 应用采用**双引擎架构**：DuckDB SQL 视图为主引擎，pandas 函数为高可用降级路径。

**降级路径存在的唯一理由是高可用性**：当 `build_warehouse.py` 尚未运行、`.duckdb` 文件不存在或损坏时，应用不崩溃，自动回退到 pandas 直接读 Excel 计算。

**降级路径不是"双重校验"**：如果 DuckDB 算错了，是 SQL 视图写错了，应该修 SQL，而不是切换到 pandas。两者的计算结果由 `scripts/compare_engines_test.py` 定期对照验证保证一致。

### 切换逻辑

`app/main.py` 中的 `_duckdb_available()` 函数在每次分析时检查 DuckDB 文件是否可用。`run_analysis()` 根据检查结果选择数据来源：

```python
if _duckdb_available():
    # 主引擎：DuckDB SQL 视图（~350ms）
    agencies = query_duckdb("SELECT * FROM v_agency_market_share WHERE ...")
else:
    # 降级路径：pandas 函数（~7,000ms，读 Excel）
    agencies = mart_agency_stats(df_prov)
```

---

### 各 Tab 双引擎对照

| Tab             | 功能模块            | DuckDB 主引擎                           | pandas 降级路径                  | 备注                                 |
| --------------- | ------------------- | --------------------------------------- | -------------------------------- | ------------------------------------ |
| **全局**  | 省份 KPI            | `v_province_kpi`                      | `len(sc_zt)` + `df_sc.sum()` |                                      |
| **Tab 0** | 评级机构排名表      | `v_agency_market_share`               | `mart_agency_stats()`          |                                      |
| **Tab 0** | 饼图 / 柱状图       | 从`v_agency_market_share` 派生        | 从排名表派生                     |                                      |
| **Tab 0** | 竞争热力矩阵（4维） | `v_agency_competitive_landscape`      | `mart_competition_matrix()`    | 动态透视仅 pandas，DuckDB 提供数据源 |
| **Tab 1** | 主体地图 / 名单表   | `v_issuer_profile`                    | `int_build_issuer_view()`      |                                      |
| **Tab 1** | 城市主体明细        | `v_city_credit_overview`              | 实时`groupby`                  |                                      |
| **Tab 2** | 准入门槛分位数      | `v_financial_bench`                   | `mart_financial_bench()`       |                                      |
| **Tab 3** | 主承销商排名        | `v_underwriter_stats`（先聚合至省级） | `mart_underwriter_stats()`     |                                      |
| **Tab 3** | 展业帮手圈          | `v_partner_network` JOIN 省份城市集合 | `mart_partner_network()`       |                                      |
| **Tab 4** | 发债案例明细        | `v_bond` WHERE 省份+机构+城市         | 筛选`df_sc`                    |                                      |

> **说明**：`mart_competition_matrix()` 的四维动态透视（城市 / 行政级别 / 评级 / 财力段）需要在运行时确定列结构，不适合固化为静态 SQL 视图，因此该模块始终使用 pandas 计算，DuckDB 仅提供原始数据。

---

## DuckDB SQL 视图

`data/serving/credit_indicators.duckdb` 内置 10 个业务视图，可直接用 Tableau / Power BI / DBeaver / Python 连接查询。所有视图均为全量宽表，自带维度列，BI 工具通过筛选器切片即可完成下钻分析。

| 视图名                             | 说明                                                   | 粒度                                         |
| ---------------------------------- | ------------------------------------------------------ | -------------------------------------------- |
| `v_bond`                         | 当前激活版本的完整债券数据                             | 一只债一行                                   |
| `v_fiscal`                       | 所有省份财力数据                                       | 一城市一行                                   |
| `v_province_kpi`                 | 省份 KPI 汇总（主体数、发行总额、覆盖城市等 6 个指标） | 一省一行                                     |
| `v_agency_market_share`          | 评级机构省内市场份额（省内市占率、债项主体比）         | 一省一机构一行                               |
| `v_agency_competitive_landscape` | 主体画像宽表（含城市、财力区间，供竞争热力矩阵使用）   | 一主体一行                                   |
| `v_issuer_profile`               | 去重主体视图（含财务指标、城市财力）                   | 一主体一行                                   |
| `v_city_credit_overview`         | 城市信用概览（省内财力排名、评级×级别主体分布）       | 一城市一主体评级一行政级别一行               |
| `v_financial_bench`              | 财务基准分位数（五指标六分位，全组合宽表）             | 一省一城市一行政级别一主体评级一行           |
| `v_underwriter_stats`            | 主承销商排名（含已承做主体数、发行倍数，全维度宽表）   | 一省一城市一级别一评级一财力区间一承销商一行 |
| `v_partner_network`              | 展业伙伴网络（共同客户数、合作等级）                   | 一省一城市一级别一评级机构一承销商一行       |

**Python 直接查询示例：**

```python
import duckdb
con = duckdb.connect("data/serving/credit_indicators.duckdb", read_only=True)

# 查询江苏省评级机构市场份额
df = con.execute("""
    SELECT 评级机构, 主体数, 债项数, 省内主体市占率_pct
    FROM v_agency_market_share
    WHERE 省份 LIKE '%江苏%'
    ORDER BY 主体数 DESC
""").df()
con.close()
```

**Tableau 连接方式：**
`连接 → 其他数据库 (JDBC) → 选择 DuckDB JDBC 驱动 → 路径填写 credit_indicators.duckdb`

---

## 技术选型说明

| 选型     | 选择                                       | 原因                                                                           |
| -------- | ------------------------------------------ | ------------------------------------------------------------------------------ |
| 存储格式 | Parquet                                    | 列式存储，读取比 Excel 快 20x；开放格式，与引擎无绑定；Snappy 压缩体积缩小 72% |
| 查询引擎 | DuckDB                                     | 嵌入式 OLAP，无需服务器；直接扫描 Parquet；支持标准 SQL；可被 Tableau 直连     |
| 数据建模 | dbt 分层思维（Staging/Intermediate/Marts） | 每层职责单一，换底层存储只改 Staging 层，上层零修改                            |
| 缓存策略 | `st.cache_data` + 快照版本号             | DuckDB 模式下缓存 key 为版本号，版本切换时精准失效；Excel 模式下为文件 mtime   |
| UI 框架  | Streamlit                                  | 轻量交付，适合数据咨询场景；`st.status` 实现分层进度展示                     |

---

## 交付方案说明

本项目针对金融信评场景设计了"交互工具+数据底座"的双轨制交付方案。

### 1. 演示版：基于 Streamlit Cloud 的交互网页

**定位：** 面向需求方或管理层，无需传数据，直接点击链接在浏览器使用。

- **最小 MVP：** 展示系统全貌与核心功能，以最快速度锁定业务逻辑与 UI 交互
- **会议演示：** 直接操作网页进行交互，根据客户反馈实时调整参数，替代传统 PPT

### 2. 资产版：源码包 + DuckDB 结构化数据底座

**定位：** 面向机构 IT 部门或高级分析师的可持续数字化资产。

- **数据主权与解耦：** 交付完整 Python 源码与标准 `.duckdb` 数据库文件，即便未来不使用 Streamlit，客户依然可以用 Tableau、Power BI 直接读取该底座
- **工程化可追溯：** 依托 dbt 分层建模思维，每一层指标加工逻辑透明、可审计

### 交付技术逻辑关联图

| 交付维度           | 技术支撑                     | 商业收益                                              |
| ------------------ | ---------------------------- | ----------------------------------------------------- |
| **快速交付** | Streamlit 轻量 UI 框架       | 极短上线周期，支持快速原型迭代与 MVP 验证             |
| **极致性能** | DuckDB 直接扫描 Parquet      | 读取速度比 Excel 快 20 倍，支持十万级债项数据秒级下钻 |
| **稳定扩展** | dbt 分层（Staging/Int/Mart） | 换底层存储只需修改接入层，逻辑层零成本维护            |
| **开放互通** | DuckDB 标准 SQL 接口         | 支持与机构现有 BI 系统（Tableau 等）无缝集成          |

---

*数据来源：Wind 金融终端（截至2023年02月16日），债券数据为公开市场城投债数据，财力数据为公开地方财政数据(2021年度），不涉及商业机密。*
