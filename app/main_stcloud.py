# -*- coding: utf-8 -*-
# 本脚本专为 Streamlit Cloud 部署设计
# 提示：在 Streamlit Community Cloud 后台设置中，请将 "Main file path" 指定为本脚本 (app/main_stcloud.py)


import os
import sys
import subprocess
import runpy

DB_FILE_PATH = "data/serving/credit_indicators.duckdb"

if not os.path.exists(DB_FILE_PATH):
    print("="*50)
    print(f"[Cloud Init] 未检测到 {DB_FILE_PATH}，正在构建数据仓库...")
    print("="*50)
    try:
        result = subprocess.run(
            [sys.executable, "scripts/build_warehouse.py"],
            check=True,
            capture_output=True,
            text=True
        )
        print(result.stdout)
        print("[Cloud Init] 构建成功！")
    except subprocess.CalledProcessError as e:
        print(f"[Cloud Init] 构建失败：{e.stderr}")
        sys.exit(e.returncode)
else:
    print("[Cloud Init] DuckDB已存在，跳过构建。")

# 主进程
runpy.run_path("app/main.py", run_name="__main__")