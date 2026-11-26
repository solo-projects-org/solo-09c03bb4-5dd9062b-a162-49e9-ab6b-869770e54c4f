from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from app import dsp, services
from app.demo import load_demo

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples"


def tone_signal(n: int = 1024) -> np.ndarray:
    t = np.arange(n) / dsp.FS
    return (
        2.0 * np.sin(2 * np.pi * 20 * t)
        + 0.6 * np.sin(2 * np.pi * 55 * t + 0.7)
        + 0.3 * np.sin(2 * np.pi * 90 * t)
    )


# --------------------------------------------------------------------------- #
# 段的创建 / 重叠 / 相邻
# --------------------------------------------------------------------------- #

def test_create_segment_builds_full_window(conn):
    x = tone_signal(300)
    seg, report = services.create_segment(conn, 0, x)
    assert seg["id"] == 1
    assert seg["end_sample_exclusive"] == 300
    # [0,300) 触及 k=0,1,2；只有 k=0 完整
    assert report.window_indices == [0, 1, 2]
    assert report.stored == [0]
    assert report.withdrawn == [1, 2]

    rows = conn.execute("SELECT window_start FROM window_spectrum").fetchall()
    assert [r[0] for r in rows] == [0]


def test_overlap_is_rejected(conn):
    services.create_segment(conn, 0, np.ones(300))
    with pytest.raises(services.ConflictError) as ei:
        services.create_segment(conn, 250, np.ones(100))  # [250,350) 与 [0,300) 重叠
    assert "重叠" in ei.value.message
    # 拒收后只有原来的一段
    assert conn.execute("SELECT COUNT(*) c FROM segment").fetchone()["c"] == 1


def test_adjacent_segments_allowed_and_cross_window(conn):
    # 第一段恰好 256 点 -> 窗 0 完整
    services.create_segment(conn, 0, tone_signal(256))
    # 相邻段从 256 开始；窗 1 [128,384) 由两段拼成
    services.create_segment(conn, 256, tone_signal(256)[0:128])
    result = services.query_band_energy(conn, 128, 383, 15, 25)
    assert result["status"] == "ok"
    assert result["window_count"] == 1
    # 拼接窗与直接对完整信号做谱一致
    full = tone_signal(512)
    p = dsp.one_sided_mean_square_power(full[128:384])
    expect = p[dsp.band_bin_indices(15, 25)].sum()
    assert result["windows"][0]["band_ms"] == pytest.approx(float(expect), rel=1e-12)


# --------------------------------------------------------------------------- #
# 跨段示例的端到端数值核对
# --------------------------------------------------------------------------- #

def test_demo_layout_and_energy(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)
    ov = services.get_overview(conn)
    assert [s["start_sample"] for s in ov["segments"]] == [0, 300, 700]
    assert ov["valid_window_starts"] == [0, 128, 256, 384, 768]
    assert ov["expected_window_starts"] == [0, 128, 256, 384, 512, 640, 768, 896]

    # 全程：5 个有效窗，20Hz 频带平均均方 = 2.0
    r = services.query_band_energy(conn, 0, 1023, 15, 25)
    assert r["status"] == "ok"
    assert r["window_count"] == 5
    assert r["avg_band_ms"] == pytest.approx(2.0, abs=1e-9)

    # 前四窗时段
    r2 = services.query_band_energy(conn, 0, 639, 15, 25)
    assert r2["window_count"] == 4

    # 跨段窗 k=1,2
    r3 = services.query_band_energy(conn, 128, 511, 15, 25)
    assert r3["window_count"] == 2
    assert {w["window_index"] for w in r3["windows"]} == {1, 2}

    # 55Hz / 90Hz 频带
    assert services.query_band_energy(conn, 0, 1023, 50, 60)["avg_band_ms"] == pytest.approx(0.18, abs=1e-9)
    assert services.query_band_energy(conn, 0, 1023, 85, 95)["avg_band_ms"] == pytest.approx(0.045, abs=1e-9)
    full = services.query_band_energy(conn, 0, 1023, 0, 128)
    assert full["avg_band_ms"] == pytest.approx(2.225, rel=1e-9)

    # 缺口时段：只有 k=4,5 完整落入且都残缺 -> 缺测
    miss = services.query_band_energy(conn, 512, 895, 0, 128)
    assert miss["status"] == "missing"
    assert miss["window_count"] == 0
    assert miss["avg_band_ms"] is None


def test_interval_shorter_than_window_is_missing(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)
    r = services.query_band_energy(conn, 10, 200, 0, 128)
    assert r["status"] == "missing"
    assert r["window_count"] == 0


# --------------------------------------------------------------------------- #
# 增量更新范围
# --------------------------------------------------------------------------- #

def test_replace_reindex_union_and_untouched_windows(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)

    before = {
        r["window_start"]: r["updated_at"]
        for r in conn.execute("SELECT window_start, updated_at FROM window_spectrum")
    }

    # 用相同数据换报 B [300,650)：只应触及 k=1..5
    x_b = tone_signal(650)[300:650]
    _, report = services.replace_segment(conn, 2, x_b)
    assert report.window_indices == [1, 2, 3, 4, 5]
    assert report.stored == [1, 2, 3]
    assert report.withdrawn == [4, 5]

    after = {
        r["window_start"]: r["updated_at"]
        for r in conn.execute("SELECT window_start, updated_at FROM window_spectrum")
    }
    # k=0 与 k=6 不在 B 的新旧范围内，时间戳必须原封不动
    assert after[0] == before[0]
    assert after[768] == before[768]


def test_replace_disjoint_move_uses_union_not_bounding_span(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)
    # 把 C [700,1024) 移到 [2000,2300)，中间的 k=8..13 不应被触及
    _, report = services.replace_segment(
        conn, 3, np.ones(300), start_sample=2000
    )
    assert report.window_indices == [4, 5, 6, 7, 14, 15, 16, 17]


def test_delete_withdraws_dependent_windows(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)
    info, report = services.delete_segment(conn, 2)  # 删 B [300,650)
    assert info["start_sample"] == 300
    assert report.window_indices == [1, 2, 3, 4, 5]
    # 删 B 后 k=1,2,3 都变残缺，谱记录删除
    starts = {
        r[0] for r in conn.execute("SELECT window_start FROM window_spectrum")
    }
    assert starts == {0, 768}
    r = services.query_band_energy(conn, 128, 511, 0, 128)
    assert r["status"] == "missing"


# --------------------------------------------------------------------------- #
# 原子性：失败照旧
# --------------------------------------------------------------------------- #

def test_spectrum_failure_rolls_back_segment_and_index(conn, monkeypatch):
    services.create_segment(conn, 0, tone_signal(300))
    blob_before = conn.execute(
        "SELECT power FROM window_spectrum WHERE window_start=0"
    ).fetchone()["power"]

    def boom(_x):
        raise RuntimeError("injected spectrum failure")

    monkeypatch.setattr("app.dsp.one_sided_mean_square_power", boom)

    # [512,768) 本身构成完整 k=4，会走到被注入的谱计算并抛错
    with pytest.raises(RuntimeError):
        services.create_segment(conn, 512, np.ones(256))

    # 段插入已回滚
    assert conn.execute("SELECT COUNT(*) c FROM segment").fetchone()["c"] == 1
    assert services.find_segment_by_start(conn, 512) is None
    # 旧窗谱原样保留
    row = conn.execute(
        "SELECT power FROM window_spectrum WHERE window_start=0"
    ).fetchone()
    assert row["power"] == blob_before
    assert conn.execute("SELECT COUNT(*) c FROM window_spectrum").fetchone()["c"] == 1


# --------------------------------------------------------------------------- #
# 能量查询不读 segment 表（authorizer 实证）
# --------------------------------------------------------------------------- #

def test_energy_query_never_reads_segment_table(conn):
    load_demo(conn, str(SAMPLE_DIR), reset=True)

    def deny_segment_reads(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ and arg1 == "segment":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_segment_reads)
    # 若能量查询试图读 segment，会立刻抛 sqlite3.DatabaseError
    r = services.query_band_energy(conn, 0, 1023, 15, 25)
    assert r["window_count"] == 5
    assert r["avg_band_ms"] == pytest.approx(2.0, abs=1e-9)
    conn.set_authorizer(None)


def test_invalid_query_interval(conn):
    with pytest.raises(services.ValidationError):
        services.query_band_energy(conn, 100, 99, 0, 10)
    with pytest.raises(services.ValidationError):
        services.query_band_energy(conn, 0, 100, -1, 10)
