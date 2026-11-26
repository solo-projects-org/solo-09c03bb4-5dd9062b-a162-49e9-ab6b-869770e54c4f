from __future__ import annotations

import numpy as np
import pytest

from app import dsp


def test_constants():
    assert dsp.FS == 256
    assert dsp.WINDOW == 256
    assert dsp.HOP == 128
    assert dsp.N_BINS == 129
    assert list(dsp.bin_frequencies()[[0, 20, 55, 90, 128]]) == [0, 20, 55, 90, 128]


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (0, 1, (0, 0)),
        (0, 256, (0, 1)),
        (128, 256, (0, 1)),
        (256, 512, (1, 3)),
        (300, 650, (1, 5)),
        (700, 1024, (4, 7)),
        (650, 700, (4, 5)),
        (512, 768, (3, 5)),
    ],
)
def test_affected_window_range(start, end, expected):
    assert dsp.affected_window_range(start, end) == expected


def test_affected_range_rejects_bad_range():
    with pytest.raises(ValueError):
        dsp.affected_window_range(10, 10)
    with pytest.raises(ValueError):
        dsp.affected_window_range(-1, 10)


@pytest.mark.parametrize(
    "s,e,lo,hi",
    [
        (0, 255, 0, 0),        # 恰好窗 0
        (0, 254, 0, -1),       # 短一个点，无窗
        (128, 511, 128, 256),  # 窗 1、2
        (0, 1023, 0, 768),     # k=0..6
    ],
)
def test_windows_fully_inside(s, e, lo, hi):
    assert dsp.windows_fully_inside(s, e) == (lo, hi)


def test_spectrum_parseval_and_pure_tones():
    n = dsp.WINDOW
    t = np.arange(n) / dsp.FS
    x = (
        2.0 * np.sin(2 * np.pi * 20 * t)
        + 0.6 * np.sin(2 * np.pi * 55 * t + 0.7)
        + 0.3 * np.sin(2 * np.pi * 90 * t)
    )
    p = dsp.one_sided_mean_square_power(x)

    assert p.shape == (129,)
    assert p.dtype == np.float64
    assert p.sum() == pytest.approx(np.mean(x**2), rel=1e-12)

    assert p[dsp.band_bin_indices(15, 25)].sum() == pytest.approx(2.0, abs=1e-10)
    assert p[dsp.band_bin_indices(50, 60)].sum() == pytest.approx(0.18, abs=1e-10)
    assert p[dsp.band_bin_indices(85, 95)].sum() == pytest.approx(0.045, abs=1e-10)
    # 全频带（含直流 / Nyquist）
    assert p[dsp.band_bin_indices(0, 128)].sum() == pytest.approx(2.225, abs=1e-10)


def test_spectrum_rejects_wrong_length():
    with pytest.raises(ValueError):
        dsp.one_sided_mean_square_power(np.zeros(128))


def test_validate_band():
    assert dsp.validate_band(0, 128) == (0.0, 128.0)
    with pytest.raises(ValueError):
        dsp.validate_band(-1, 10)
    with pytest.raises(ValueError):
        dsp.validate_band(10, 9)
    with pytest.raises(ValueError):
        dsp.validate_band(0, 129)
