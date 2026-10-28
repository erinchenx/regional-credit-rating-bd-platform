# -*- coding: utf-8 -*-
"""
benchmark/run_all.py
====================
一键运行四项核心基准测试并汇总生成 benchmark/report.md。

用法：
    python benchmark/run_all.py                 # 跑全部四项 + 生成报告
    python benchmark/run_all.py --reps 10       # 自定义重复次数
    python benchmark/run_all.py --skip-apptest  # 测试4 只测数据层，跳过 AppTest
    python benchmark/run_all.py --report-only   # 不重跑，直接用已有 results/*.json 生成报告

每个子测试以独立子进程运行（避免 Streamlit 缓存 / AppTest 状态互相污染），
结果落盘 benchmark/results/benchN.json，本脚本再汇总成 report.md。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as C

SCRIPTS = [
    "bench1_parquet_vs_excel.py",
    "bench2_pandas_vs_duckdb.py",
    "bench3_old_vs_new_flow.py",
    "bench4_province_switch.py",
]


def _result_json(script: str) -> Path:
    """bench1_parquet_vs_excel.py -> results/bench1.json"""
    return C.RESULTS_DIR / f"{script.split('_', 1)[0]}.json"


# ──────────────────────────────────────────────
# Markdown 小工具
# ──────────────────────────────────────────────
def ms(x) -> str:
    return C.fmt_ms(x)


def _med_rng(d) -> str:
    """中位数（同批重复样本的最小–最大值）。"""
    if not d or d.get("median") is None:
        return "—"
    if d.get("min") is None or d.get("max") is None:
        return ms(d["median"])
    return f"{ms(d['median'])}（{ms(d['min'])}–{ms(d['max'])}）"


def _ms_band(x) -> str:
    """按中位耗时给出诚实的量级描述（不迎合「毫秒级」预期）。"""
    if x is None:
        return "—"
    if x < 1:
        return "亚毫秒级"
    if x < 10:
        return "个位数毫秒级"
    if x < 50:
        return "数十毫秒级"
    if x < 200:
        return "百余毫秒级"
    if x < 1000:
        return "亚秒级"
    return "秒级"


def _ms_claim_verdict(x) -> str:
    """针对「毫秒级」表述给出诚实判定。"""
    if x is None:
        return "无法判定"
    if x < 50:
        return "「毫秒级」表述基本成立"
    if x < 1000:
        return "「毫秒级」表述**不成立**——实为亚秒级"
    return "「毫秒级」表述**不成立**——实为秒级"


def tbl(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def _load(name: str) -> dict | None:
    try:
        return C.load_json(name)
    except Exception:
        return None


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────
def build_report(r1: dict | None, r2: dict | None, r3: dict | None,
                 r4: dict | None = None, r5: dict | None = None) -> str:
    """
    生成精简版 report.md。
    r1=bench1(文件格式读取), r2=bench2(纯计算对比),
    r3=bench3(单查询新旧流程对比), r4=bench4(省份切换),
    r5=end_to_end(全应用冷启动 + 首次分析完整数据层)。
    """
    env = (r1 or r2 or r3 or r4 or {}).get("env") or C.env_info()
    fiscal_provinces = C.available_provinces()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L: list[str] = []

    L.append("# 性能基准测试报告")
    L.append("")
    L.append(f"> 生成时间：{now} · 由 `benchmark/run_all.py` 自动生成，数字均来自实测。")
    L.append("")

    # ── 一、测试环境 ──
    L.append("## 一、测试环境")
    L.append("")
    mem = env.get("memory_gb")
    L.append(tbl(
        ["项目", "值"],
        [
            ["操作系统", env.get("os", "—")],
            ["CPU", f"{env.get('cpu','—')}（逻辑核心 {env.get('cpu_cores_logical','—')}）"],
            ["内存", f"{mem} GB" if mem else "—"],
            ["Python", env.get("python", "—")],
            ["pandas", env.get("pandas", "—")],
            ["DuckDB", env.get("duckdb", "—")],
            ["债券数据快照", C.active_snapshot() or "—"],
            ["财力数据年度", C.fiscal_year() or "—"],
            ["有财力数据的省份", f"{len(fiscal_provinces)} 个" if fiscal_provinces else "—"],
        ],
    ))
    L.append("")
    L.append("**数据版本**：以上版本读取自 `data/serving/active_version.json`，全部测试共用同一份数据。")
    L.append("")
    L.append("**测量波动**：表中括号内为同一次运行重复样本的最小–最大值；不同批次运行之间"
             "（尤其后台负载不同）可能相差数十个百分点，跨批次比较请以中位数为准。")
    L.append("")

    # ── 二、结论速览 ──
    L.append("## 二、结论速览")
    L.append("")
    L.append(_verdict_lines(r1, r2, r3, r4, r5))
    L.append("")

    # ── 三、测试 1 · 文件格式读取 ──
    L.append("## 三、测试 1 · 文件格式读取")
    L.append("")
    if not r1:
        L.append("_未获取到测试 1 结果（bench1.json 缺失或运行失败）。_")
    else:
        L.append(f"**方法**：取项目最大表（{r1['rows']:,} 行 × {r1['cols']} 列）导出为 `.xlsx` 与 `.csv`，"
                 f"分别用 `pandas.read_excel` / `pandas.read_parquet` / `DuckDB read_parquet` 读取；"
                 f"每组预热 {r1['config']['warmup']} 次、重复 {r1['config']['reps']} 次，取中位数。")
        L.append("")
        t = r1["timings_ms"]
        L.append(tbl(
            ["读取方式", "中位数（最小–最大）", "P95"],
            [
                ["pandas.read_excel(xlsx)", _med_rng(t["pandas_read_excel"]), ms(t["pandas_read_excel"]["p95"])],
                ["pandas.read_parquet", _med_rng(t["pandas_read_parquet"]), ms(t["pandas_read_parquet"]["p95"])],
                ["DuckDB read_parquet", _med_rng(t["duckdb_read_parquet"]), ms(t["duckdb_read_parquet"]["p95"])],
                ["pandas.read_csv（参照）", _med_rng(t["pandas_read_csv_ref"]), ms(t["pandas_read_csv_ref"]["p95"])],
            ],
        ))
        L.append("")
        rt = r1["ratios"]
        L.append(f"**结论**：本测试比较的是 **Excel 与 Parquet 两种文件格式**的读取耗时，"
                 f"pandas / DuckDB 只是读取 Parquet 的两种路径，不作为对比对象。"
                 f"同一份数据下，Parquet 读取比 Excel 快约 **{rt['excel_over_parquet_pandas']} 倍**"
                 f"（pandas 路径）/ **{rt['excel_over_parquet_duckdb']} 倍**（DuckDB 路径）。")
    L.append("")

    # ── 四、测试 2 · 计算引擎对比（纯计算） ──
    L.append("## 四、测试 2 · 计算引擎对比（纯计算）")
    L.append("")
    if not r2:
        L.append("_未获取到测试 2 结果（bench2.json 缺失或运行失败）。_")
    else:
        tq = r2["drill_triple"]
        L.append(f"**方法**：数据已预加载（同一份 Parquet），仅对比 DuckDB vs pandas 的纯计算用时。"
                 f"省份 = {r2['province']}，评级机构 = {r2['agency']}。"
                 f"下钻三元组（Q2 筛选参数）：城市={tq['城市']}、级别={tq['行政级别']}、评级={tq['主体评级']}。"
                 f"预热 {r2['config']['warmup']} 次、重复 {r2['config']['reps']} 次。")
        L.append("")
        L.append("**口径说明**：Q1 的 DuckDB 侧是 pandas 动态透视的等价 SQL 实现（app 当前仍走 pandas）；"
                 "Q2 是 app 在 DuckDB 模式下的真实路径；Q3 仅覆盖默认「全部城市 / 全部级别」。")
        L.append("")
        rows = []
        for name, q in r2["queries"].items():
            rows.append([
                name,
                _med_rng(q["pandas_pure_compute"]),
                _med_rng(q["duckdb_pure_compute"]),
                f"{q['speedup_pure_compute']}×",
            ])
        L.append(tbl(["查询", "pandas 中位（范围）", "DuckDB 中位（范围）", "加速比"], rows))
        L.append("")
        pure = [q["speedup_pure_compute"] for q in r2["queries"].values() if q.get("speedup_pure_compute")]
        all_ok = all(q.get("consistency", {}).get("ok") for q in r2["queries"].values())
        cons_note = "，三个查询输出结构一致" if all_ok else ""
        L.append(f"**结论**：DuckDB 纯计算比 pandas 快 **{min(pure)}~{max(pure)} 倍**，"
                 f"绝对耗时均在毫秒~百毫秒级{cons_note}。")
    L.append("")

    # ── 五、测试 3 · 单查询新旧流程对比 ──
    L.append("## 五、测试 3 · 单查询新旧流程对比")
    L.append("")
    if not r3:
        L.append("_未获取到测试 3 结果（bench3.json 缺失或运行失败）。_")
    else:
        L.append(f"**方法**：选取测试 2 的 Q1/Q2/Q3 三个下钻查询，使用完全相同的筛选参数"
                 f"（省份=`{r3['province']}`、评级机构=`{r3['agency']}`）。"
                 f"旧流程从原始 Excel 读起（pandas + staging 清洗），读取、处理分开计时，"
                 f"总耗时 = 读取 + 处理（逐次相加后统计）；"
                 f"新流程 DuckDB 连接服务文件执行 SQL，只记总耗时（读取+计算融合）。"
                 f"旧流程重复 {r3['config']['reps']} 次，新流程重复 {r3['config']['new_reps']} 次。")
        L.append("")
        rows = []
        for name, q in r3["queries"].items():
            rows.append([
                name,
                _med_rng(q["old_total"]),
                _med_rng(q["new_total"]),
                f"{q['speedup_total']}×",
                f"{q['read_share_pct']}%",
            ])
        L.append(tbl(["查询", "旧流程总耗时（范围）", "新流程总耗时（范围）", "加速比", "旧流程读取占比"], rows))
        L.append("")
        L.append("> ⚠️ 这是新旧流程整体对比（数据格式 + 读取方式 + 计算引擎一起变），非单一因素对比。")
        L.append(">")
        L.append("> **口径说明**：本测试比较的是技术栈的等价实现，不代表 app 每条查询都走 DuckDB。"
                 "Q1 在 app 中仍由 pandas 动态透视完成；Q3 只覆盖默认「全部城市 / 全部级别」；"
                 "Q2 是 app 的 DuckDB 真实路径。")
        L.append("")
        sps = [q["speedup_total"] for q in r3["queries"].values() if q.get("speedup_total")]
        shares = [q["read_share_pct"] for q in r3["queries"].values() if q.get("read_share_pct") is not None]
        all_ok = all(q.get("consistency", {}).get("ok") for q in r3["queries"].values())
        cons_note = "，三个查询输出结构一致" if all_ok else ""
        L.append(f"**结论**：新流程比旧流程快 **{min(sps)}~{max(sps)} 倍**，"
                 f"其中 **{min(shares)}%~{max(shares)}% 时间花在 Excel 解析**上"
                 f"{cons_note}。"
                 f'提速主因是"以 Parquet 列存取代运行时 Excel 解析"，而非 DuckDB 引擎本身。')
    L.append("")

    # ── 六、测试 4 · 省份切换响应 ──
    L.append("## 六、测试 4 · 省份切换响应")
    L.append("")
    if not r4:
        L.append("_未获取到测试 4 结果（bench4.json 缺失或运行失败）。_")
    else:
        dl = r4["data_layer"]
        ov = dl["overall"]
        L.append(f"**方法**：一次性载入全国债券宽表（模拟 App 缓存），遍历有财力数据的 "
                 f"{len(dl['provinces'])} 个省份，"
                 f"每省执行切换时真实的数据流（财力读取 + pandas 关联/去重 + DQC 审计 + 5 条 DuckDB 视图查询），"
                 f"每轮清缓存，预热 {dl['config']['warmup']} 次 + 重复 {dl['config']['reps']} 次。")
        L.append("")
        L.append(tbl(
            ["汇总", "中位数", "P95", "最小–最大"],
            [["有财力数据的省份×全部重复", ms(ov["median"]), ms(ov["p95"]),
              f"{ms(ov['min'])}–{ms(ov['max'])}"]],
        ))
        L.append("")
        full = dl.get("full_bond_cold_load_ms") or {}
        if full.get("median"):
            L.append(f"**全国债券宽表冷加载**（DuckDB 读取，模拟 App 首次加载）：中位 "
                     f"{ms(full['median'])}（{ms(full['min'])}–{ms(full['max'])}）。")
            L.append("")
        L.append(f"> 数据层每次切换会执行 5 条 DuckDB 视图查询，而项目的 `query_duckdb` 设计为"
                 f"每条查询新建并关闭一个只读连接（连接对象不可缓存），构成数百毫秒耗时的主要来源。")
        L.append("")
        L.append(f"**结论**：省份切换数据层耗时中位数约 **{ms(ov['median'])}**"
                 f"（{_ms_band(ov['median'])}），P95 约 {ms(ov['p95'])}。")
        at = r4.get("end_to_end_apptest", {})
        if at.get("ran"):
            cold = at.get("cold_run_ms")
            if cold:
                L.append(f"进程内首次脚本执行约 **{ms(cold)}**（bench4 进程已加载依赖，"
                         f"不含 Python import；含首次全脚本执行。全应用独立进程冷启动见第七节）。")
    L.append("")

    # ── 七、端到端口径补充 ──
    L.append("## 七、端到端口径补充（全应用冷启动 / 首次分析）")
    L.append("")
    if not r5:
        L.append("_未获取到端到端补充结果（end_to_end.json 缺失或运行失败）。_")
        L.append("_运行 `python benchmark/measure_end_to_end.py` 可生成该文件。_")
    else:
        app = r5.get("full_app_cold_start") or {}
        dl = r5.get("first_analysis_data_layer") or {}
        rows = []
        if app.get("old") and app.get("new"):
            ratio = app.get("ratio")
            rows.append([
                "全应用冷启动（含 Python import + 全脚本 + plotly 构建）",
                f"{ms(app['old']['min'])}–{ms(app['old']['max'])}（中位 {ms(app['old']['median'])}）",
                f"{ms(app['new']['min'])}–{ms(app['new']['max'])}（中位 {ms(app['new']['median'])}）",
                f"~{ratio:.1f}×" if ratio else "—",
            ])
        if dl.get("old") and dl.get("new"):
            ratio = dl.get("ratio")
            rows.append([
                "首次分析 · 完整数据层（不含 import/前端）",
                f"{ms(dl['old']['min'])}–{ms(dl['old']['max'])}（中位 {ms(dl['old']['median'])}）",
                f"{ms(dl['new']['min'])}–{ms(dl['new']['max'])}（中位 {ms(dl['new']['median'])}）",
                f"~{ratio:.1f}×" if ratio else "—",
            ])
        if r3:
            old_t = [q["old_total"]["median"] for q in r3["queries"].values()
                     if q.get("old_total", {}).get("median")]
            new_t = [q["new_total"]["median"] for q in r3["queries"].values()
                     if q.get("new_total", {}).get("median")]
            sps = [q["speedup_total"] for q in r3["queries"].values() if q.get("speedup_total")]
            if old_t and new_t:
                rows.append([
                    "单条核心查询（无应用缓存、重读源文件）",
                    f"{ms(min(old_t))}–{ms(max(old_t))}",
                    f"{ms(min(new_t))}–{ms(max(new_t))}",
                    f"{min(sps):.1f}–{max(sps):.1f}×" if sps else "—",
                ])
        if r4:
            ov = r4["data_layer"]["overall"]
            rows.append([
                "省份切换 · 数据层（债券宽表已加载）",
                "—（未测）",
                f"中位 {ms(ov['median'])} / P95 {ms(ov['p95'])}",
                "—",
            ])
        L.append(tbl(["场景", "旧流程（Excel 降级）", "新流程（DuckDB+Parquet）", "倍数"], rows))
        L.append("")
        L.append("**方法**：全应用冷启动由 `measure_cold_start.py` 在独立进程中用 AppTest 执行 "
                 "`app/main.py`；首次分析数据层由 `measure_end_to_end.py` 清空 `@st.cache_data` 后"
                 "从源文件读起，跑完所有 tab 的数据层。两者均不含浏览器 JS 绘制。")
        L.append("")
        old_t_all = [q["old_total"]["median"] for q in (r3 or {}).get("queries", {}).values()
                     if q.get("old_total", {}).get("median")]
        old_r_all = [q["old_read"]["median"] for q in (r3 or {}).get("queries", {}).values()
                     if q.get("old_read", {}).get("median")]
        sps_all = [q["speedup_total"] for q in (r3 or {}).get("queries", {}).values()
                   if q.get("speedup_total")]
        dl_old_med = (dl.get("old") or {}).get("median")
        app_ratio, dl_ratio = app.get("ratio"), dl.get("ratio")
        if old_t_all and old_r_all and dl_old_med:
            read_med = sorted(old_r_all)[len(old_r_all) // 2]
            speed_txt = (f"{min(sps_all):.1f}–{max(sps_all):.1f}×"
                         if sps_all else "单查询")
            L.append(f"> **口径说明**：单条查询的 {ms(min(old_t_all))}–{ms(max(old_t_all))} 与"
                     f"首次分析的 {ms(dl_old_med)} 都含同一笔约 {ms(read_med)} 的全国 Excel 解析。"
                     f"前者按每条查询重复计费，后者整次分析只解析一次，因此 {speed_txt} 是单查询口径，"
                     "不是整次分析口径。")
        else:
            L.append("> **口径说明**：单条查询与首次分析都含同一笔全国 Excel 解析；前者按每条查询"
                     "重复计费，后者整次分析只解析一次，因此加速比是单查询口径，不是整次分析口径。")
        if app_ratio and dl_ratio and app.get("old") and app.get("new") \
                and (dl.get("old") or {}).get("median") and (dl.get("new") or {}).get("median"):
            fixed_old = app["old"]["median"] - dl["old"]["median"]
            fixed_new = app["new"]["median"] - dl["new"]["median"]
            L.append(">")
            L.append(f"> 全应用约 {app_ratio:.1f}× 小于数据层约 {dl_ratio:.1f}×，是因为冷启动还包含"
                     f"与数据层无关的固定开销（Python import、Streamlit 脚本执行、plotly 构建等）。"
                     f"按中位数倒算，这部分旧流程约 {ms(fixed_old)}、新流程约 {ms(fixed_new)}，"
                     f"两者并不相同，本次未进一步拆分其构成；因此不能按「固定开销相同」来换算冷启动倍数。")
    L.append("")

    # ── 八、未覆盖项 ──
    L.append("## 八、未覆盖项")
    L.append("")
    L.append("- **浏览器渲染耗时**：项目未安装 playwright，无法测量。")
    L.append("- **冷启动（清 OS 文件缓存）**：Windows 非管理员无法清缓存，以热启动为主。")
    L.append("")

    # ── 九、如何复现 ──
    L.append("## 九、如何复现")
    L.append("")
    L.append("```bash")
    L.append("pip install -e .")
    L.append("python scripts/build_warehouse.py")
    L.append("python benchmark/measure_end_to_end.py   # 可选：生成端到端补充结果")
    L.append("python benchmark/run_all.py               # 跑 bench1-4 并汇总生成报告")
    L.append("```")
    L.append("")
    L.append("详见 `benchmark/README.md`。")
    L.append("")

    return "\n".join(L)


def _verdict_lines(r1, r2, r3, r4=None, r5=None) -> str:
    lines = []
    # 测试 1：文件格式读取
    if r1:
        rt = r1["ratios"]
        lines.append(f"1. **测试 1 · 文件格式读取（Excel vs Parquet）**："
                     f"Parquet 读取比 Excel 快 **{rt.get('excel_over_parquet_pandas')}×**（pandas 路径），"
                     f"**{rt.get('excel_over_parquet_duckdb')}×**（DuckDB 路径）。")
    else:
        lines.append("1. **测试 1 · 文件格式读取（Excel vs Parquet）**：未取得结果。")
    # 测试 2：纯计算对比
    if r2:
        pure = [q.get("speedup_pure_compute") for q in r2["queries"].values() if q.get("speedup_pure_compute")]
        lines.append(f"2. **测试 2 · 计算引擎对比（纯计算）**：DuckDB 加速比 **{min(pure) if pure else '—'}×~{max(pure) if pure else '—'}×**。")
    else:
        lines.append("2. **测试 2 · 计算引擎对比**：未取得结果。")
    # 测试 3：新旧流程对比
    if r3:
        sps = [q.get("speedup_total") for q in r3["queries"].values() if q.get("speedup_total")]
        shares = [q.get("read_share_pct") for q in r3["queries"].values() if q.get("read_share_pct") is not None]
        old_t = [q["old_total"]["median"] / 1000 for q in r3["queries"].values() if q.get("old_total", {}).get("median")]
        new_t = [q["new_total"]["median"] for q in r3["queries"].values() if q.get("new_total", {}).get("median")]
        lines.append(f"3. **测试 3 · 单查询新旧流程对比**：旧流程 **{ms(min(old_t)*1000 if old_t else 0)}~{ms(max(old_t)*1000 if old_t else 0)}**，"
                     f"新流程 **{ms(min(new_t) if new_t else 0)}~{ms(max(new_t) if new_t else 0)}**，"
                     f"新流程比旧流程快 **{min(sps) if sps else '—'}×~{max(sps) if sps else '—'}×**。"
                     f"其中 {min(shares) if shares else '—'}%~{max(shares) if shares else '—'}% 时间花在 Excel 解析，"
                     f"提速主因是 Parquet 取代 Excel，而非 DuckDB 引擎本身。")
    else:
        lines.append("3. **测试 3 · 单查询新旧流程对比**：未取得结果。")
    # 测试 4：省份切换响应
    if r4:
        ov = r4["data_layer"]["overall"]
        at = r4.get("end_to_end_apptest", {})
        parts = [f"4. **测试 4 · 省份切换响应**：数据层中位 **{ms(ov['median'])}**（{_ms_band(ov['median'])}）、"
                 f"P95 {ms(ov['p95'])}。"]
        if at.get("ran"):
            parts.append(f"AppTest 端到端（不含浏览器绘制）中位 {ms(at['summary']['median'])}。")
            cold = at.get("cold_run_ms")
            if cold:
                parts.append(f"进程内首次脚本 {ms(cold)}（不含 import）。")
        lines.append("".join(parts))
    else:
        lines.append("4. **测试 4 · 省份切换响应**：未取得结果。")
    # 端到端补充：全应用冷启动 + 首次分析完整数据层
    if r5:
        app = r5.get("full_app_cold_start") or {}
        dl = r5.get("first_analysis_data_layer") or {}
        parts = ["5. **端到端口径补充**："]
        wrote = False
        if app.get("old") and app.get("new") and app.get("ratio"):
            if wrote:
                parts.append("；")
            parts.append(f"全应用冷启动 **{ms(app['old']['median'])}→{ms(app['new']['median'])}**"
                         f"（约 {app['ratio']:.1f}×）")
            wrote = True
        if dl.get("old") and dl.get("new") and dl.get("ratio"):
            if wrote:
                parts.append("；")
            parts.append(f"首次分析完整数据层 **{ms(dl['old']['median'])}→{ms(dl['new']['median'])}**"
                         f"（约 {dl['ratio']:.1f}×）")
            wrote = True
        if wrote:
            parts.append("。")
            lines.append("".join(parts))
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 运行子测试
# ──────────────────────────────────────────────
def run_scripts(args) -> None:
    base_args = ["--warmup", str(args.warmup), "--reps", str(args.reps)]
    for script in SCRIPTS:
        cmd = [sys.executable, str(C.BENCH_DIR / script)]
        if script == "bench1_parquet_vs_excel.py":
            cmd += base_args + (["--force-export"] if args.force_export else [])
        elif script == "bench2_pandas_vs_duckdb.py":
            cmd += base_args
        elif script == "bench3_old_vs_new_flow.py":
            cmd += ["--warmup", str(args.warmup), "--reps", str(args.reps3),
                    "--new-reps", str(args.new_reps)]
        elif script == "bench4_province_switch.py":
            cmd += base_args + (["--skip-apptest"] if args.skip_apptest else [])
        print(f"\n{'#'*64}\n# 运行 {script}\n{'#'*64}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[警告] {script} 运行失败（返回码 {e.returncode}），已移除旧结果，报告中将标注缺失。")
            # 删除上一次成功运行遗留的 benchN.json，避免 _load 把旧数据当成本次结果
            _result_json(script).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="一键运行性能基准并生成 report.md")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=20)
    parser.add_argument("--reps3", type=int, default=10,
                        help="测试3 旧流程（含 Excel 读取，慢）重复次数，默认 10")
    parser.add_argument("--new-reps", type=int, default=20,
                        help="测试3 新流程（DuckDB，快）重复次数，默认 20")
    parser.add_argument("--skip-apptest", action="store_true",
                        help="测试4 只测数据层，跳过 AppTest")
    parser.add_argument("--force-export", action="store_true")
    parser.add_argument("--report-only", action="store_true", help="不重跑，仅用已有 JSON 生成报告")
    args = parser.parse_args()

    C.require_warehouse()
    if not args.report_only:
        run_scripts(args)

    r1, r2, r3 = _load("bench1.json"), _load("bench2.json"), _load("bench3.json")
    r4 = _load("bench4.json")
    r5 = _load("end_to_end.json")
    report = build_report(r1, r2, r3, r4, r5)
    out = C.BENCH_DIR / "report.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n报告已生成：{out}")


if __name__ == "__main__":
    main()
