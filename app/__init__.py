"""频带能量核对系统 —— 应用包。

模块划分：
- dsp:      窗参数、覆盖判定、矩形窗单边谱、频带均方值
- csvio:    单列采样 CSV 解析
- db:       SQLite 连接与表结构
- services: 段的增 / 换报 / 撤段，增量窗索引，能量查询（只读取谱表）
- web:      Flask 页面与 JSON API
"""

from __future__ import annotations

import os

from flask import Flask

from .db import init_db
from .web import bp as web_bp


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root_dir, "data")

    app.config.update(
        DB_PATH=os.environ.get("DB_PATH", os.path.join(data_dir, "vibration.db")),
        SAMPLE_DIR=os.environ.get("SAMPLE_DIR", os.path.join(root_dir, "samples")),
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)

    # 让 JSON 直接输出中文（Flask 3 的 JSON Provider）
    try:
        app.json.ensure_ascii = False
    except AttributeError:
        app.config["JSON_AS_ASCII"] = False

    db_path = app.config["DB_PATH"]
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)

    init_db(db_path)

    app.register_blueprint(web_bp)

    return app
