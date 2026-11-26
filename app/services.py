"""业务逻辑：采样段管理、增量窗谱索引、频带能量查询。

关键不变量
----------
1. 任意两个 segment 的半开样点区间 [start, start+n) 不重叠（换报排除自身）。
2. window_spectrum 中只保留 256 个样点全部齐全的窗；缺样窗无记录（撤下）。
3. segment 改动与受影响窗的谱索引在同一个 SQLite 事务内提交，
   任何一步抛错整体回滚，做到“采样和频谱一起生效，失败照旧”。
4. query_band_energy 只读取 window_spectrum，不访问 segment 原始采样。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from . import dsp


class ServiceError(Exception):
    """带 HTTP 状态码的业务异常。"""

    status_code = 400

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ServiceError):
    status_code = 400


class ConflictError(ServiceError):
    status_code = 409


class NotFoundError(ServiceError):
    status_code = 404


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- #
# 序列化工具
# --------------------------------------------------------------------------- #

def encode_samples(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype=np.float64).tobytes()


def decode_samples(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float64)


def encode_power(power: np.ndarray) -> bytes:
    return np.asarray(power, dtype=np.float64).tobytes()


def decode_power(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float64)


def segment_to_dict(row: sqlite3.Row) -> dict:
    start = int(row["start_sample"])
    n = int(row["n_samples"])
    return {
        "id": int(row["id"]),
        "start_sample": start,
        "n_samples": n,
        "fs": int(row["fs"]) if "fs" in row.keys() else dsp.FS,
        "end_sample_exclusive": start + n,
        "end_sample_inclusive": start + n - 1,
        "start_time_s": start / dsp.FS,
        "end_time_s": (start + n) / dsp.FS,
        "duration_s": n / dsp.FS,
        "name": row["name"],
        "source_file": row["source_file"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------- #
# 重叠校验
# --------------------------------------------------------------------------- #

def find_overlap(
    conn: sqlite3.Connection,
    start: int,
    n_samples: int,
    *,
    exclude_id: int | None = None,
) -> sqlite3.Row | None:
    """返回与 [start, start+n) 重叠的已存在段（若有）。"""
    end = start + n_samples
    return conn.execute(
        """
        SELECT *
        FROM segment
        WHERE (:exclude_id IS NULL OR id != :exclude_id)
          AND start_sample < :end
          AND start_sample + n_samples > :start
        ORDER BY start_sample
        LIMIT 1
        """,
        {"start": start, "end": end, "exclude_id": exclude_id},
    ).fetchone()


def _raise_if_overlap(
    conn: sqlite3.Connection, start: int, n_samples: int, *, exclude_id: int | None
) -> None:
    row = find_overlap(conn, start, n_samples, exclude_id=exclude_id)
    if row is not None:
        other = segment_to_dict(row)
        raise ConflictError(
            f"样点区间 [{start}, {start + n_samples}) 与已有段 "
            f"#{other['id']}（起始 {other['start_sample']}，"
            f"共 {other['n_samples']} 点，区间 "
            f"[{other['start_sample']}, {other['end_sample_exclusive']})）重叠，已拒收",
            details={"overlapping_segment": other},
        )


def _validate_start(start: int) -> int:
    try:
        start = int(start)
    except (TypeError, ValueError) as exc:
        raise ValidationError("起始样点必须是非负整数") from exc
    if start < 0:
        raise ValidationError("起始样点不能为负数")
    return start


# --------------------------------------------------------------------------- #
# 窗组装与增量索引
# --------------------------------------------------------------------------- #

def assemble_window(
    conn: sqlite3.Connection, window_index: int
) -> tuple[np.ndarray, bool]:
    """从 segment 表拼出一个窗的 256 点。

    返回 (buffer, complete)。complete=False 表示存在缺样，
    buffer 中缺样位置为 0（但不应被用于频谱）。
    """
    w0 = dsp.window_start(window_index)
    buf = np.zeros(dsp.WINDOW, dtype=np.float64)
    covered = np.zeros(dsp.WINDOW, dtype=bool)

    rows = conn.execute(
        """
        SELECT start_sample, n_samples, samples
        FROM segment
        WHERE start_sample < :w_end
          AND start_sample + n_samples > :w_start
        """,
        {"w_start": w0, "w_end": w0 + dsp.WINDOW},
    ).fetchall()

    for row in rows:
        seg_start = int(row["start_sample"])
        seg_len = int(row["n_samples"])
        x = decode_samples(row["samples"])

        lo = max(seg_start, w0)
        hi = min(seg_start + seg_len, w0 + dsp.WINDOW)
        if hi <= lo:
            continue

        dst_a, dst_b = lo - w0, hi - w0
        src_a, src_b = lo - seg_start, hi - seg_start
        buf[dst_a:dst_b] = x[src_a:src_b]
        covered[dst_a:dst_b] = True

    return buf, bool(covered.all())


@dataclass
class ReindexReport:
    """一次增量重算触及的窗（编号）。"""

    window_indices: list[int] = field(default_factory=list)
    stored: list[int] = field(default_factory=list)
    withdrawn: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        def starts(indices: list[int]) -> list[int]:
            return [dsp.window_start(k) for k in indices]

        return {
            "window_indices": list(self.window_indices),
            "window_starts": starts(self.window_indices),
            "stored_indices": list(self.stored),
            "stored_starts": starts(self.stored),
            "withdrawn_indices": list(self.withdrawn),
            "withdrawn_starts": starts(self.withdrawn),
            "considered_count": len(self.window_indices),
            "stored_count": len(self.stored),
            "withdrawn_count": len(self.withdrawn),
        }


def reindex_window_set(
    conn: sqlite3.Connection, window_indices: set[int] | list[int]
) -> ReindexReport:
    """重算给定窗集合：齐全则 upsert 谱，残缺则删除谱记录。"""
    report = ReindexReport(window_indices=sorted(set(int(k) for k in window_indices)))
    ts = now_iso()

    for k in report.window_indices:
        samples, complete = assemble_window(conn, k)
        w0 = dsp.window_start(k)

        if not complete:
            conn.execute("DELETE FROM window_spectrum WHERE window_start = ?", (w0,))
            report.withdrawn.append(k)
            continue

        power = dsp.one_sided_mean_square_power(samples)
        conn.execute(
            """
            INSERT INTO window_spectrum
                (window_start, n_samples, fs, n_bins, power, total_ms, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_start) DO UPDATE SET
                n_samples  = excluded.n_samples,
                fs         = excluded.fs,
                n_bins     = excluded.n_bins,
                power      = excluded.power,
                total_ms   = excluded.total_ms,
                updated_at = excluded.updated_at
            """,
            (
                w0,
                dsp.WINDOW,
                dsp.FS,
                power.shape[0],
                encode_power(power),
                float(power.sum()),
                ts,
            ),
        )
        report.stored.append(k)

    return report


def affected_indices_for_ranges(ranges: list[tuple[int, int]]) -> set[int]:
    """多个半开样点区间各自相交窗编号的并集（不取包围盒）。"""
    indices: set[int] = set()
    for start, end in ranges:
        k_lo, k_hi = dsp.affected_window_range(start, end)
        indices.update(range(k_lo, k_hi + 1))
    return indices


def reindex_ranges(
    conn: sqlite3.Connection, ranges: list[tuple[int, int]]
) -> ReindexReport:
    """只重算与给定样点区间集合相交的窗。"""
    return reindex_window_set(conn, affected_indices_for_ranges(ranges))


# --------------------------------------------------------------------------- #
# 段的增 / 换 / 撤
# --------------------------------------------------------------------------- #

def create_segment(
    conn: sqlite3.Connection,
    start_sample: int,
    samples: np.ndarray,
    *,
    name: str | None = None,
    source_file: str | None = None,
) -> tuple[dict, ReindexReport]:
    start = _validate_start(start_sample)
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim != 1 or samples.size == 0:
        raise ValidationError("采样数据必须是非空一维数组")
    n = int(samples.size)

    # with conn: 正常退出提交，异常回滚 —— 段与窗谱同生共死。
    with conn:
        _raise_if_overlap(conn, start, n, exclude_id=None)
        ts = now_iso()
        cur = conn.execute(
            """
            INSERT INTO segment
                (start_sample, n_samples, fs, name, source_file, samples,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (start, n, dsp.FS, name, source_file, encode_samples(samples), ts, ts),
        )
        seg_id = int(cur.lastrowid)
        report = reindex_ranges(conn, [(start, start + n)])
        row = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()

    return segment_to_dict(row), report


def get_segment_or_404(conn: sqlite3.Connection, segment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到段 #{segment_id}")
    return row


def find_segment_by_start(conn: sqlite3.Connection, start_sample: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM segment WHERE start_sample = ?", (int(start_sample),)
    ).fetchone()


def replace_segment(
    conn: sqlite3.Connection,
    segment_id: int,
    samples: np.ndarray,
    *,
    start_sample: int | None = None,
    name: str | None = None,
    source_file: str | None = None,
) -> tuple[dict, ReindexReport]:
    """整段换报：可用新 CSV 整体替换，并允许改变起始样点 / 长度。

    增量窗集合 = 旧段范围与新段范围各自相交窗的并集。
    """
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim != 1 or samples.size == 0:
        raise ValidationError("采样数据必须是非空一维数组")
    new_n = int(samples.size)

    with conn:
        old = get_segment_or_404(conn, segment_id)
        old_start = int(old["start_sample"])
        old_n = int(old["n_samples"])
        old_range = (old_start, old_start + old_n)

        new_start = old_start if start_sample is None else _validate_start(start_sample)
        _raise_if_overlap(conn, new_start, new_n, exclude_id=int(segment_id))
        new_range = (new_start, new_start + new_n)

        ts = now_iso()
        new_name = name if name is not None else old["name"]
        new_source = source_file if source_file is not None else old["source_file"]

        conn.execute(
            """
            UPDATE segment
               SET start_sample = ?, n_samples = ?, name = ?, source_file = ?,
                   samples = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                new_start,
                new_n,
                new_name,
                new_source,
                encode_samples(samples),
                ts,
                int(segment_id),
            ),
        )

        report = reindex_ranges(conn, [old_range, new_range])
        row = conn.execute(
            "SELECT * FROM segment WHERE id = ?", (int(segment_id),)
        ).fetchone()

    return segment_to_dict(row), report


def replace_segment_by_start(
    conn: sqlite3.Connection,
    locate_start: int,
    samples: np.ndarray,
    *,
    start_sample: int | None = None,
    **kwargs,
) -> tuple[dict, ReindexReport]:
    """以起始样点定位旧段的整段换报入口。

    ``start_sample`` 若给出，表示换报后采用的新起点；不给则沿用定位起点。
    """
    locate = _validate_start(locate_start)
    old = find_segment_by_start(conn, locate)
    if old is None:
        raise NotFoundError(f"起始样点 {locate} 处没有可换报的段")
    if start_sample is None:
        start_sample = locate
    return replace_segment(
        conn, int(old["id"]), samples, start_sample=start_sample, **kwargs
    )


def delete_segment(
    conn: sqlite3.Connection, segment_id: int
) -> tuple[dict, ReindexReport]:
    """撤段：删除段并把与其范围相交、因此变残缺的窗撤下。"""
    with conn:
        old = get_segment_or_404(conn, segment_id)
        info = segment_to_dict(old)
        old_range = (info["start_sample"], info["end_sample_exclusive"])

        conn.execute("DELETE FROM segment WHERE id = ?", (int(segment_id),))
        report = reindex_ranges(conn, [old_range])

    return info, report


def clear_all(conn: sqlite3.Connection) -> None:
    """清空全部段与窗谱（示例重置时使用）。"""
    with conn:
        conn.execute("DELETE FROM window_spectrum")
        conn.execute("DELETE FROM segment")


# --------------------------------------------------------------------------- #
# 能量查询：只读 window_spectrum，绝不回读 segment
# --------------------------------------------------------------------------- #

def query_band_energy(
    conn: sqlite3.Connection,
    interval_start: int,
    interval_end_inclusive: int,
    f_low: float,
    f_high: float,
) -> dict:
    """统计完整落入样点闭区间的有效窗，并平均其带内均方值。

    本函数内出现的唯一数据表是 window_spectrum（可用 SQLite authorizer
    禁止读取 segment 来验证这一点）。
    """
    try:
        interval_start = int(interval_start)
        interval_end_inclusive = int(interval_end_inclusive)
    except (TypeError, ValueError) as exc:
        raise ValidationError("样点区间端点必须是整数") from exc

    if interval_start < 0:
        raise ValidationError("区间起始样点不能为负")
    if interval_end_inclusive < interval_start:
        raise ValidationError("区间结束样点（含）不能早于起始样点")

    try:
        f_low, f_high = dsp.validate_band(f_low, f_high)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    bin_idx = dsp.band_bin_indices(f_low, f_high)
    freqs = dsp.bin_frequencies()
    bin_freqs = [float(freqs[i]) for i in bin_idx]

    w0_lo, w0_hi = dsp.windows_fully_inside(
        interval_start, interval_end_inclusive
    )

    windows: list[dict] = []
    if w0_lo <= w0_hi:
        rows = conn.execute(
            """
            SELECT window_start, power
            FROM window_spectrum
            WHERE window_start >= ? AND window_start <= ?
            ORDER BY window_start
            """,
            (w0_lo, w0_hi),
        ).fetchall()

        for row in rows:
            w0 = int(row["window_start"])
            power = decode_power(row["power"])
            band_ms = float(power[bin_idx].sum()) if bin_idx.size else 0.0
            windows.append(
                {
                    "window_index": dsp.window_index_of(w0),
                    "start_sample": w0,
                    "end_sample_inclusive": w0 + dsp.WINDOW - 1,
                    "start_time_s": w0 / dsp.FS,
                    "end_time_s": (w0 + dsp.WINDOW) / dsp.FS,
                    "band_ms": band_ms,
                    "band_rms": float(np.sqrt(max(band_ms, 0.0))),
                    "total_ms": float(power.sum()),
                }
            )

    count = len(windows)
    if count == 0:
        return {
            "status": "missing",
            "message": "所选时段内没有完整落入的有效窗（缺测）",
            "interval": {
                "start_sample": interval_start,
                "end_sample_inclusive": interval_end_inclusive,
                "end_exclusive": interval_end_inclusive + 1,
                "start_time_s": interval_start / dsp.FS,
                "end_time_s": (interval_end_inclusive + 1) / dsp.FS,
            },
            "band": {
                "f_low": f_low,
                "f_high": f_high,
                "bin_indices": [int(i) for i in bin_idx],
                "bin_frequencies_hz": bin_freqs,
            },
            "window_count": 0,
            "avg_band_ms": None,
            "band_rms": None,
            "windows": [],
        }

    avg_ms = float(sum(w["band_ms"] for w in windows) / count)
    return {
        "status": "ok",
        "message": f"共 {count} 个有效窗完整落入所选时段",
        "interval": {
            "start_sample": interval_start,
            "end_sample_inclusive": interval_end_inclusive,
            "end_exclusive": interval_end_inclusive + 1,
            "start_time_s": interval_start / dsp.FS,
            "end_time_s": (interval_end_inclusive + 1) / dsp.FS,
        },
        "band": {
            "f_low": f_low,
            "f_high": f_high,
            "bin_indices": [int(i) for i in bin_idx],
            "bin_frequencies_hz": bin_freqs,
        },
        "window_count": count,
        "avg_band_ms": avg_ms,
        "band_rms": float(np.sqrt(max(avg_ms, 0.0))),
        "windows": windows,
    }


# --------------------------------------------------------------------------- #
# 总览（供网页绘制段与窗覆盖情况，会读取段元数据但不读取 BLOB 采样）
# --------------------------------------------------------------------------- #

def get_overview(conn: sqlite3.Connection) -> dict:
    seg_rows = conn.execute(
        "SELECT * FROM segment ORDER BY start_sample"
    ).fetchall()
    segments = [segment_to_dict(r) for r in seg_rows]

    valid_rows = conn.execute(
        "SELECT window_start, total_ms, updated_at FROM window_spectrum "
        "ORDER BY window_start"
    ).fetchall()
    valid_windows = [
        {
            "window_index": dsp.window_index_of(int(r["window_start"])),
            "start_sample": int(r["window_start"]),
            "end_sample_inclusive": int(r["window_start"]) + dsp.WINDOW - 1,
            "total_ms": float(r["total_ms"]),
            "updated_at": r["updated_at"],
        }
        for r in valid_rows
    ]
    valid_starts = {w["start_sample"] for w in valid_windows}

    expected_indices = affected_indices_for_ranges(
        [(s["start_sample"], s["end_sample_exclusive"]) for s in segments]
    )
    expected_starts = sorted(dsp.window_start(k) for k in expected_indices)

    max_end = max(
        (s["end_sample_exclusive"] for s in segments), default=0
    )

    return {
        "constants": {
            "fs_hz": dsp.FS,
            "window_samples": dsp.WINDOW,
            "hop_samples": dsp.HOP,
            "nyquist_hz": dsp.NYQUIST,
            "n_bins": dsp.N_BINS,
        },
        "segments": segments,
        "valid_windows": valid_windows,
        "valid_window_starts": sorted(valid_starts),
        "expected_window_starts": expected_starts,
        "max_segment_end": max_end,
    }
