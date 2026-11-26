"""Flask 页面与 JSON API。"""

from __future__ import annotations

import sqlite3
import traceback

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from . import services
from .csvio import CsvParseError, parse_single_column_csv
from .db import connect
from .demo import list_demo_files, load_demo

bp = Blueprint("web", __name__)


# --------------------------------------------------------------------------- #
# 连接生命周期
# --------------------------------------------------------------------------- #

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


@bp.teardown_app_request
def close_db(exc):  # noqa: ANN001
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# --------------------------------------------------------------------------- #
# 错误处理
# --------------------------------------------------------------------------- #

def _error_response(exc: services.ServiceError):
    return jsonify({"error": exc.message, "details": exc.details}), exc.status_code


@bp.errorhandler(services.ServiceError)
def handle_service_error(exc: services.ServiceError):
    return _error_response(exc)


@bp.errorhandler(CsvParseError)
def handle_csv_error(exc: CsvParseError):
    return jsonify({"error": str(exc)}), 400


@bp.errorhandler(404)
def handle_404(exc):  # noqa: ANN001
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在"}), 404
    return ("Not Found", 404)


@bp.errorhandler(500)
def handle_500(exc):  # noqa: ANN001
    traceback.print_exc()
    if request.path.startswith("/api/"):
        return jsonify({"error": "服务器内部错误，数据库事务应已回滚"}), 500
    return ("Internal Server Error", 500)


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@bp.route("/api/config")
def api_config():
    return jsonify(
        {
            "db_path": current_app.config["DB_PATH"],
            "sample_dir": current_app.config["SAMPLE_DIR"],
        }
    )


# --------------------------------------------------------------------------- #
# 请求解析
# --------------------------------------------------------------------------- #

def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "reset"}


def _extract_samples_and_meta(*, start_required: bool):
    """从 multipart / JSON / 表单 / 原始请求体中取出 CSV 与元数据。"""
    start = request.values.get("start_sample")
    name = request.values.get("name") or None
    source_file = None
    raw: bytes | None = None

    uploaded = request.files.get("file")
    if uploaded is not None and uploaded.filename:
        raw = uploaded.read()
        source_file = uploaded.filename

    if raw is None:
        json_body = request.get_json(silent=True)
        if isinstance(json_body, dict):
            if "csv_text" in json_body and json_body["csv_text"] is not None:
                raw = str(json_body["csv_text"]).encode("utf-8")
            if start is None and json_body.get("start_sample") is not None:
                start = json_body.get("start_sample")
            if name is None:
                name = json_body.get("name")
            if source_file is None:
                source_file = json_body.get("source_file")

    if raw is None:
        csv_text = request.form.get("csv_text")
        if csv_text:
            raw = csv_text.encode("utf-8")

    if raw is None and request.data:
        # 例如 curl --data-binary @seg.csv，start_sample 放在 query/form
        raw = request.data

    if raw is None:
        raise services.ValidationError(
            "没有收到 CSV 内容：请用 file 字段上传文件，或提供 csv_text"
        )

    try:
        samples = parse_single_column_csv(raw)
    except CsvParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise services.ValidationError(f"CSV 解析失败：{exc}") from exc

    if start_required and (start is None or str(start).strip() == ""):
        raise services.ValidationError("请提供起始样点 start_sample")

    start_value: int | None = None
    if start is not None and str(start).strip() != "":
        try:
            start_value = int(start)
        except (TypeError, ValueError) as exc:
            raise services.ValidationError("起始样点必须是非负整数") from exc

    return samples, start_value, name, source_file


# --------------------------------------------------------------------------- #
# 总览 / 示例文件
# --------------------------------------------------------------------------- #

@bp.route("/api/overview")
def api_overview():
    return jsonify(services.get_overview(get_db()))


@bp.route("/api/demo/files")
def api_demo_files():
    sample_dir = current_app.config["SAMPLE_DIR"]
    return jsonify({"files": list_demo_files(sample_dir)})


@bp.route("/api/demo/raw/<path:filename>")
def api_demo_raw(filename: str):
    sample_dir = current_app.config["SAMPLE_DIR"]
    return send_from_directory(sample_dir, filename, mimetype="text/csv")


# --------------------------------------------------------------------------- #
# 段管理
# --------------------------------------------------------------------------- #

@bp.route("/api/segments", methods=["POST"])
def api_create_segment():
    samples, start, name, source_file = _extract_samples_and_meta(start_required=False)
    json_body = request.get_json(silent=True) or {}
    want_replace = _as_bool(
        request.values.get("replace", json_body.get("replace", False))
    )
    segment_id = request.values.get(
        "segment_id", json_body.get("segment_id")
    )

    # 同一个上传入口也支持整段换报：replace=1 时用 segment_id 或 start_sample 定位旧段。
    if want_replace:
        if segment_id not in (None, ""):
            try:
                segment_id_int = int(segment_id)
            except (TypeError, ValueError) as exc:
                raise services.ValidationError("segment_id 必须是整数") from exc
            seg, report = services.replace_segment(
                get_db(),
                segment_id_int,
                samples,
                start_sample=start,
                name=name,
                source_file=source_file,
            )
        else:
            if start is None:
                raise services.ValidationError(
                    "replace 模式需要 segment_id 或 start_sample 来定位要换报的段"
                )
            seg, report = services.replace_segment_by_start(
                get_db(),
                start,
                samples,
                name=name,
                source_file=source_file,
            )
        return jsonify({"segment": seg, "reindex": report.to_dict()}), 200

    if start is None:
        raise services.ValidationError("请提供起始样点 start_sample")

    seg, report = services.create_segment(
        get_db(),
        start,
        samples,
        name=name,
        source_file=source_file,
    )
    return jsonify({"segment": seg, "reindex": report.to_dict()}), 201


@bp.route("/api/segments/<int:segment_id>/replace", methods=["POST", "PUT"])
@bp.route("/api/segments/<int:segment_id>", methods=["PUT", "POST"])
def api_replace_segment(segment_id: int):
    # POST 到 /api/segments/<id> 与 /replace 都按整段换报处理。
    samples, start, name, source_file = _extract_samples_and_meta(start_required=False)
    seg, report = services.replace_segment(
        get_db(),
        segment_id,
        samples,
        start_sample=start,
        name=name,
        source_file=source_file,
    )
    return jsonify({"segment": seg, "reindex": report.to_dict()})


@bp.route("/api/segments/by-start/<int:start_sample>/replace", methods=["POST", "PUT"])
def api_replace_by_start(start_sample: int):
    samples, start, name, source_file = _extract_samples_and_meta(start_required=False)
    # 路径中的 start_sample 是定位键；payload 若另给 start_sample 则作为新范围起点。
    seg, report = services.replace_segment_by_start(
        get_db(),
        start_sample,
        samples,
        start_sample=start,
        name=name,
        source_file=source_file,
    )
    return jsonify({"segment": seg, "reindex": report.to_dict()})


@bp.route("/api/segments/<int:segment_id>", methods=["DELETE"])
def api_delete_segment(segment_id: int):
    seg, report = services.delete_segment(get_db(), segment_id)
    return jsonify({"deleted": seg, "reindex": report.to_dict()})


@bp.route("/api/segments", methods=["DELETE"])
def api_clear_segments():
    services.clear_all(get_db())
    return jsonify({"cleared": True})


# --------------------------------------------------------------------------- #
# 能量查询（只读窗谱表）
# --------------------------------------------------------------------------- #

@bp.route("/api/energy")
def api_energy():
    args = request.args
    try:
        start = args["start_sample"]
        end = args["end_sample"]
        f_low = args.get("f_low", 0.0)
        f_high = args.get("f_high", 128.0)
    except KeyError as exc:
        raise services.ValidationError(f"缺少查询参数：{exc.args[0]}") from exc

    return jsonify(
        services.query_band_energy(get_db(), start, end, f_low, f_high)
    )


# --------------------------------------------------------------------------- #
# 一键载入跨段示例
# --------------------------------------------------------------------------- #

@bp.route("/api/demo/load", methods=["POST"])
def api_demo_load():
    payload = request.get_json(silent=True) or {}
    reset = _as_bool(
        request.values.get("reset", payload.get("reset")), default=False
    )
    result = load_demo(
        get_db(), current_app.config["SAMPLE_DIR"], reset=bool(reset)
    )
    return jsonify(result), 201
