# 性能基准测试套件（benchmark/）

对本项目（Parquet 数据湖 + DuckDB 服务层 + Streamlit 看板）做四项核心性能实测，
并提供一个一次性端到端补充测量：

| # | 测什么 | 对应脚本 |
| --- | --- | --- |
| 1 | 文件格式读取速度：Parquet vs Excel vs CSV | `bench1_parquet_vs_excel.py` |
| 2 | 计算引擎速度：DuckDB vs pandas（纯计算，同一份 Parquet） | `bench2_pandas_vs_duckdb.py` |
| 3 | 单查询新旧流程对比（读取+处理）：旧流程 Excel+pandas vs 新流程 DuckDB+Parquet | `bench3_old_vs_new_flow.py` |
| 4 | 省份切换响应速度 | `bench4_province_switch.py` |
| 5 | （补充）全应用冷启动 + 首次分析完整数据层 | `measure_end_to_end.py` |

> 第 5 项为一次性补充测量，**不接入 `run_all.py` 的常规子进程**；结果写入 `results/end_to_end.json`，由 `run_all.py` 汇总进报告。
>
> **口径说明**：bench2/bench3 的 DuckDB 侧是 pandas 路径的**等价 SQL 实现**，用于引擎/技术栈对比，**不代表 app 每条查询都走 DuckDB**。Q1（Tab 0）在 app 中仍是 pandas 动态透视；Q3（Tab 3）仅默认「全部城市 / 全部级别」时走 DuckDB 视图，选择具体城市/级别时走 pandas；Q2（Tab 2）才是 app 在 DuckDB 模式下的真实路径。

> **原则**：如实测量，不为迎合预期调整方法；与估算不符时在报告中直接写明差距，无法测量的项说明原因、绝不编造数字。
>
> **边界**：**本套件自身**只新增 `benchmark/` 目录下的文件，不修改项目运行代码；配套的 README 性能表述与展业帮手圈口径修正见同批提交。`.gitignore` 仅追加对 `benchmark/results/` 可再生产物的忽略规则。

---

## 一、前置条件

1. 已安装项目依赖（含 `psutil`、`openpyxl`、`streamlit`）：

   ```bash
   pip install -e .
   ```

2. 已初始化数据仓库（生成 Parquet 数据湖与 DuckDB 服务文件）：

   ```bash
   python scripts/build_warehouse.py
   ```

   运行前脚本会做前置检查：若找不到 `data/serving/credit_indicators.duckdb` 或 `data/warehouse/bond/*.parquet`，会直接报错并提示先运行上面的命令。

---

## 二、快速开始（一键运行四项核心测试 + 生成报告）

```bash
# 可选：先生成端到端补充结果（全应用冷启动 + 首次分析完整数据层）
python benchmark/measure_end_to_end.py

# 跑 bench1-4，并汇总 results/*.json（含 end_to_end.json）生成报告
python benchmark/run_all.py
```

- 四个子测试各自以**独立子进程**运行（隔离 Streamlit 缓存与 AppTest 状态），结果分别落盘到 `benchmark/results/bench1.json`、`bench2.json`、`bench3.json`、`bench4.json`。
- 全部跑完后，自动汇总生成 **`benchmark/report.md`**（测试环境、数据规模、每项方法与结果表格、一句话结论、无法测量项）。
- 首次运行会导出最大表为 `.xlsx`（约 4 MB，openpyxl 引擎），耗时较长，属正常。

---

## 三、单独运行某个测试

```bash
# 测试 1：Parquet vs Excel 读取
python benchmark/bench1_parquet_vs_excel.py

# 测试 2：复杂下钻 pandas vs DuckDB（纯计算，可指定省份）
python benchmark/bench2_pandas_vs_duckdb.py
python benchmark/bench2_pandas_vs_duckdb.py 四川省

# 测试 3：单查询新旧流程对比（Excel+pandas vs DuckDB+Parquet）
python benchmark/bench3_old_vs_new_flow.py
python benchmark/bench3_old_vs_new_flow.py 四川省

# 测试 4：省份切换响应
python benchmark/bench4_province_switch.py
```

每个脚本运行后都会把结果写入 `benchmark/results/benchN.json`，并在控制台打印小结。

---

## 四、命令行参数

**`run_all.py`**

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--warmup N` | 每项预热次数 | `1` |
| `--reps N` | 每项重复计时次数 | `20` |
| `--reps3 N` | 测试 3 旧流程（含 Excel 读取，慢）重复次数 | `10` |
| `--new-reps N` | 测试 3 新流程（DuckDB，快）重复次数 | `20` |
| `--skip-apptest` | 测试 4 只测数据层，跳过 AppTest 端到端 | 关 |
| `--force-export` | 强制重新导出测试 1 的 `.xlsx`/`.csv` | 关 |
| `--report-only` | 不重跑，直接用已有 `results/*.json` 重新生成 `report.md` | 关 |

**单个脚本**

| 脚本 | 参数 |
| --- | --- |
| `bench1_parquet_vs_excel.py` | `--warmup`、`--reps`、`--force-export` |
| `bench2_pandas_vs_duckdb.py` | `[province]`（位置参数，可选）、`--warmup`、`--reps` |
| `bench3_old_vs_new_flow.py` | `[province]`（位置参数，可选，默认与测试 2 一致）、`--warmup`、`--reps`（旧流程 Excel，默认 10）、`--new-reps`（新流程，默认 20） |
| `bench4_province_switch.py` | `--warmup`、`--reps`、`--skip-apptest` |
| `measure_end_to_end.py` | `[province]`（位置参数，可选）、`--reps`（数据层，默认 5）、`--app-runs`（冷启动轮数，默认 3）、`--skip-app` |

示例：`python benchmark/run_all.py --reps 20 --skip-apptest`

`run_all.py --report-only` 会自动加载 `results/end_to_end.json`（若存在）并写入报告第七节；没有该文件时，第七节会标注缺失。

---

## 五、产物说明

```
benchmark/
├── README.md                     # 本文件
├── common.py                     # 公共工具：环境信息 / 路径 / 计时 / DuckDB 只读连接 / 清缓存 / JSON 读写
├── bench1_parquet_vs_excel.py    # 测试 1
├── bench2_pandas_vs_duckdb.py    # 测试 2
├── bench3_old_vs_new_flow.py      # 测试 3（新旧流程整体对比）
├── bench4_province_switch.py      # 测试 4
├── measure_end_to_end.py          # 补充：全应用冷启动 + 首次分析完整数据层（一次性）
├── measure_cold_start.py          # 补充：单次全应用冷启动（被 measure_end_to_end.py 调用）
├── measure_warm_analysis.py       # 补充：非首次分析新旧流程对比（一次性）
├── run_all.py                     # 一键编排 + 生成 report.md
├── report.md                      # 【运行后生成】汇总报告（数字全部来自实测）
└── results/                       # 【运行后生成】运行产物
    ├── bench1.json / bench2.json / bench3.json / bench4.json   # 各测试原始结果
    ├── end_to_end.json                           # 端到端补充结果（run_all 会自动汇总）
    ├── largest_table.xlsx                        # 测试 1 导出的最大表（约 4 MB）
    └── largest_table.csv                         # 测试 1 导出的最大表（参照组）
```

- `results/` 下所有 JSON 与导出物均为**可再生的临时产物**，已整体写入项目 `.gitignore`；需要时重跑 `run_all.py` 或 `measure_end_to_end.py` 生成。`report.md` 作为交付文档随仓库提交，不被忽略。

---

## 六、方法与公平性

- **计时**：统一用 `time.perf_counter`；每项**预热 1 次 + 重复 20 次**（可调），报告中位数 / 最小 / 最大 / P95，并保留首轮（相对「冷」）耗时以体现冷/热差异。
- **清缓存**：对项目的 `@st.cache_data` 函数（`stg_load_bond_data`、`stg_load_fiscal_data`、`int_join_fiscal`、`int_build_issuer_view`）在每轮计时前调用 `.clear()`，避免缓存命中掩盖真实耗时。
- **复用真实逻辑**：不 `import app/main.py`（那会触发整个 Streamlit 脚本），而是像 `scripts/compare_engines_test.py` 一样，直接调用 `models/` 下的纯函数与 DuckDB 视图，业务口径取自项目代码。需注意：bench2/bench3 的 DuckDB 侧是**等价 SQL 实现**，不代表 app 每条查询都走 DuckDB——Q1 的 app 当前用 pandas 动态透视，Q3 只测默认「全部城市 / 全部级别」，Q2 才是 app 在 DuckDB 模式下的真实路径。
- **测试 2 的纯计算对比**：pandas 与 DuckDB 使用**同一省份、同一筛选参数、同一份 Parquet 数据**（数据已预加载）；两侧结果用**代码断言**校验一致性，不一致会如实写进报告。
- **测试 3 的整体对比**：与测试 2 共用 `Ctx` 自动选定的**同一省份 / 评级机构 / 下钻三元组**，并沿用测试 2 的一致性断言与容差。旧流程走项目真实的 **Excel 降级读取路径**（`stg_load_bond_data` / `stg_load_fiscal_data` 不传 `db_path`，即 `read_excel` + 列名归一化 + 评级归并 + DQC），新流程走 DuckDB 只读服务文件（视图读 Parquet）。旧流程**拆开记录读取 / 处理 / DQC / 总耗时**（总=逐次读+算相加），新流程只记总耗时；因 Excel 极慢，旧流程默认 10 次、新流程默认 20 次。**此为新旧技术栈整体对比（格式+读取+引擎一起变），非单一因素**。

---

## 七、测试覆盖范围

App 完整启动流程与各 bench 的测量覆盖对照：

| App 启动步骤 | 具体内容 | bench1 | bench2 | bench3 | bench4 |
|---|---|:---:|:---:|:---:|:---:|
| **Streamlit 冷启动** | Python import + 页面初始化 | — | — | — | ✅ ¹ |
| **Step 1 · 读取债券数据** | `stg_load_bond_data`（旧：read_excel / 新：DuckDB 查 Parquet） | ✅ | — | ✅ | — |
| **Step 2 · 读取财力数据** | `stg_load_fiscal_data`（旧：read_excel / 新：DuckDB 查 Parquet） | ✅ | — | ✅ | ✅ |
| **Step 3 · 关联 + 去重** | `int_filter_province` + `int_join_fiscal` + `int_build_issuer_view` | — | ✅ | ✅ | ✅ |
| **Step 4 · DQC 审计** | `dqc_marts_platform_dismatch` | — | — | ✅ | ✅ |
| **Tab 初始查询** | Tab 0/1/2/3 各跑一轮查询 | — | ✅ | ✅ | ✅ |
| **Streamlit 渲染** | 页面构建 + 组件加载 | — | — | — | — |

✅ = 该 bench 覆盖了此步骤；— = 未覆盖；✅ ¹ = bench4 AppTest 附录中附测（进程内首次脚本约 1.4 s，不含 Python import），非主测试项；全应用独立进程冷启动见 `measure_end_to_end.py`。

**未覆盖项说明**：

- **Streamlit 冷启动**：bench4 AppTest 实测约 1.4 s（进程内首次脚本执行；bench4 进程已加载依赖，不含 Python import，新旧流程相同，对比时抵消）。全应用独立进程冷启动约 2.1 s，见 `report.md` 第七节。
- **Streamlit 渲染**：项目未安装 playwright，无法测量浏览器真实渲染耗时；且渲染开销与数据管线无关，新旧流程相同，对比时抵消。

**各 bench 测量口径**：

- **bench1**：纯文件格式读取（Step 1 + Step 2 的 I/O 部分），不涉及计算。
- **bench2**：纯计算（Step 3），数据已预加载为同一份 Parquet，不含读取耗时。
- **bench3**：单查询新旧流程对比（读取+处理）。旧流程拆分计时：Step 1+2 读取 → Step 3 处理 → Step 4 DQC；新流程只记总耗时（DuckDB 读取与计算融合在一条 SQL 中无法拆分）。
- **bench4**：省份切换全流程。数据层包含 Step 2+3+4 + 5 条 DuckDB 查询；AppTest 端到端包含完整脚本执行（含首次脚本执行，不含 Python import）。全应用独立进程冷启动由 `measure_end_to_end.py` 补充测量。
- **Staging 读取不单独测量**：DuckDB 侧由 bench4 的 `full_bond_cold_load_ms` 覆盖（等价于 `SELECT * FROM v_bond` + 删元数据列）；Excel 侧由 bench3 的 Q3 `old_read` 覆盖（只读债券表；Q1/Q2 还多读财力表）。README 只保留定性描述，精确数字以 bench3 / bench4 为准。

---

## 八、无法测量 / 已知边界（如实说明）

- **浏览器真实渲染 / JS 绘制耗时**：项目未安装 `playwright`，无法测量端到端浏览器耗时。测试 4 用 `streamlit.testing.v1.AppTest` 无头运行 `app/main.py` 测得**服务端脚本执行耗时**（数据层 + Python 侧 plotly 图构建，**不含浏览器绘制**）作为最接近的替代，并明确标注。若 AppTest 运行失败，会优雅降级为「仅测数据层」并在报告注明。
- **真正的冷启动（清空操作系统文件缓存）**：Windows 非管理员无法清 OS 文件缓存，故以热启动为主，并单独报告首轮耗时作为「相对冷」参考。
- **进程级冷启动 vs OS 冷启动**：`measure_end_to_end.py` 每轮都在独立进程中启动 AppTest，属于「进程级冷启动」（Python import + 全脚本 + plotly 构建），但仍无法清空 OS 文件缓存，因此不是磁盘级冷启动。
- **测试 2 的绝对量级**：数据仅约 2.1 万行 / 1 MB 级，两个引擎的绝对耗时都在毫秒~百毫秒级，**不会出现「10 秒 → 2 秒」**；报告只呈现真实的相对倍数，并明确写出与主观估算的差距。
- **依赖缺失的替代**：未安装 `xlsxwriter`，测试 1 导出 `.xlsx` 使用 `openpyxl` 引擎。

---

## 九、常见问题

- **报告里某个测试显示「未取得结果」**：说明对应子脚本运行失败（`results/benchN.json` 未生成）。可单独运行该脚本查看报错，修复后 `python benchmark/run_all.py --report-only` 仅重建报告。
- **想换省份测下钻**：`python benchmark/bench2_pandas_vs_duckdb.py 江苏省`（省份名需存在于 `data/raw/fiscal/` 下）。
- **只想快速看报告结构**：先跑一次生成 JSON，之后用 `--report-only` 反复重建 `report.md` 而不重测。
