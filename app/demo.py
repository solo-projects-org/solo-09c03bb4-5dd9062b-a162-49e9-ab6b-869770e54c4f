"""跨段示例数据的定位与一键载入。

示例信号总长 1024 点（4 s），由 20 Hz / 55 Hz / 90 Hz 三个整周期纯音组成，
切成三段（边界故意不对齐窗格）：

    A: start=0,   n=300  -> [0, 300)
    B: start=300, n=350  -> [300, 650)   （A/B 相邻，窗 1、2 跨两段）
    C: start=700, n=324  -> [700, 1024)  （650..699 留 50 点缺口）

有效窗：0 [0,256)、1 [128,384)、2 [256,512)、3 [384,640)、6 [768,1024)；
窗 4、5 因缺口残缺被撤下，窗 7 长度不足。
"""

from __future__ import annotations

import os
import sqlite3

from .csvio import parse_single_column_csv
from .services import (
    ConflictError,
    ValidationError,
    encode_samples,
    now_iso,
    reindex_ranges,
    segment_to_dict,
)

DEMO_SPECS = [
    {"file": "demo_seg1_start0.csv", "start_sample": 0, "name": "示例段A [0,300)"},
    {"file": "demo_seg2_start300.csv", "start_sample": 300, "name": "示例段B [300,650)"},
    {"file": "demo_seg3_start700.csv", "start_sample": 700, "name": "示例段C [700,1024)"},
]


def demo_dir(sample_dir: str) -> str:
    return sample_dir


def list_demo_files(sample_dir: str) -> list[dict]:
    out = []
    for spec in DEMO_SPECS:
        path = os.path.join(sample_dir, spec["file"])
        out.append({**spec, "exists": os.path.isfile(path)})
    return out


def load_demo(
    conn: sqlite3.Connection, sample_dir: str, *, reset: bool = False
) -> dict:
    """从附带 CSV 载入跨段示例。reset=True 时先清空全部数据。"""
    if not os.path.isdir(sample_dir):
        raise ValidationError(f"示例目录不存在：{sample_dir}")

    # 先把三个文件全部解析成功，再动数据库。
    parsed: list[dict] = []
    for spec in DEMO_SPECS:
        path = os.path.join(sample_dir, spec["file"])
        if not os.path.isfile(path):
            raise ValidationError(f"缺少示例文件：{spec['file']}")
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            samples = parse_single_column_csv(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"示例文件 {spec['file']} 解析失败：{exc}") from exc
        parsed.append({**spec, "samples": samples})

    existing = conn.execute("SELECT COUNT(*) AS c FROM segment").fetchone()["c"]
    if existing and not reset:
        raise ConflictError(
            "数据库中已有采样段；如需用跨段示例替换全部数据，请带 reset=1",
            details={"existing_segments": existing},
        )

    # 段写入 + 全范围窗索引放在同一个事务里：示例要么整体生效，要么照旧。
    segments = []
    with conn:
        if reset and existing:
            conn.execute("DELETE FROM window_spectrum")
            conn.execute("DELETE FROM segment")

        ts = now_iso()
        ranges = []
        for item in parsed:
            start = int(item["start_sample"])
            samples = item["samples"]
            n = int(samples.size)
            cur = conn.execute(
                """
                INSERT INTO segment
                    (start_sample, n_samples, fs, name, source_file, samples,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    start,
                    n,
                    256,
                    item["name"],
                    item["file"],
                    encode_samples(samples),
                    ts,
                    ts,
                ),
            )
            seg_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM segment WHERE id = ?", (seg_id,)
            ).fetchone()
            segments.append(segment_to_dict(row))
            ranges.append((start, start + n))

        report = reindex_ranges(conn, ranges)

    return {
        "reset": bool(reset),
        "segments": segments,
        "reindex": report.to_dict(),
    }
