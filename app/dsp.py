"""数字信号处理核心：窗格定义、覆盖判定、矩形窗单边谱与频带均方值。

约定（与网页 / README 保持一致）
--------------------------------
- 采样率 FS = 256 Hz；
- 窗长 N = 256 点（1.0 s），步长 HOP = 128 点（0.5 s，50% 重叠）；
- 第 k 个窗（k 从 0 起）覆盖半开样点区间 [k*128, k*128 + 256)，
  即样点 k*128 ... k*128+255；
- 频谱：NumPy ``np.fft.rfft`` 的矩形窗（等价于直接截断）单边谱。
  各谱线对“均方值”的贡献按 Parseval 归一化：
      p[0]      = |X[0]|^2 / N^2          （直流）
      p[N/2]    = |X[N/2]|^2 / N^2        （Nyquist，偶数 N）
      p[1:N/2]  = 2 * |X[k]|^2 / N^2      （其余单边谱线，能量加倍）
  于是 sum(p) == mean(x^2)，频带内 sum(p[bin]) 即带内均方值。
"""

from __future__ import annotations

import math

import numpy as np

FS = 256
WINDOW = 256
HOP = 128
N_BINS = WINDOW // 2 + 1  # rfft 谱线数：129（0 .. 128 Hz，1 Hz 间隔）
NYQUIST = FS // 2


def window_start(window_index: int) -> int:
    """第 k 个窗的起始样点。"""
    return int(window_index) * HOP


def window_index_of(window_start_sample: int) -> int:
    """起始样点对应的窗编号（起始样点必须是 HOP 的整数倍）。"""
    if window_start_sample % HOP != 0:
        raise ValueError(f"窗起始样点必须是 {HOP} 的整数倍：{window_start_sample}")
    return window_start_sample // HOP


def iter_window_starts(lo_index: int, hi_index_inclusive: int) -> range:
    return range(lo_index, hi_index_inclusive + 1)


def affected_window_range(seg_start: int, seg_end_exclusive: int) -> tuple[int, int]:
    """返回与半开样点区间 ``[seg_start, seg_end)`` 相交的窗编号范围。

    返回 ``(k_lo, k_hi)``，两端均包含。推导：
    - 相交要求窗起点 < seg_end 且 窗末端(不含) > seg_start；
    - k_lo = max(0, floor((seg_start - WINDOW)/HOP) + 1)
      （最小的满足 128k + 256 > seg_start 的 k）；
    - k_hi = floor((seg_end - 1)/HOP)
      （最大的满足 128k <= seg_end - 1 的 k）。
    """
    if seg_end_exclusive <= seg_start:
        raise ValueError("段结束位置必须大于起始位置")
    if seg_start < 0:
        raise ValueError("样点区间起点不能为负")

    k_lo = max(0, (seg_start - WINDOW) // HOP + 1)
    k_hi = (seg_end_exclusive - 1) // HOP
    return k_lo, k_hi


def windows_fully_inside(interval_start: int, interval_end_inclusive: int) -> tuple[int, int]:
    """完整落入闭区间 ``[interval_start, interval_end_inclusive]`` 的窗起点上下界。

    窗 [w0, w0+WINDOW) 完整落入的条件：
        w0 >= interval_start 且 w0 + WINDOW - 1 <= interval_end_inclusive
        即 interval_start <= w0 <= interval_end_inclusive + 1 - WINDOW。
    返回起点样点的闭区间 ``(w0_lo, w0_hi)``；若不存在这样的窗，
    则 w0_lo > w0_hi，调用方据此得到 0 个窗。
    """
    w0_lo = max(0, interval_start)
    w0_hi = interval_end_inclusive + 1 - WINDOW
    return w0_lo, w0_hi


def one_sided_mean_square_power(samples: np.ndarray) -> np.ndarray:
    """计算矩形窗单边谱的逐谱线均方贡献，长度 N/2+1（float64）。"""
    x = np.asarray(samples, dtype=np.float64)
    n = x.shape[0]
    if n != WINDOW:
        raise ValueError(f"频谱计算要求 {WINDOW} 点，实际 {n} 点")

    rectangular = np.ones(n, dtype=np.float64)  # 矩形窗：不做锥化
    spectrum = np.fft.rfft(x * rectangular)

    power = (spectrum.real**2 + spectrum.imag**2) / float(n * n)
    if n % 2 == 0:
        power[1:-1] *= 2.0  # DC 与 Nyquist 不加倍
    else:
        power[1:] *= 2.0
    return power.astype(np.float64, copy=False)


def bin_frequencies() -> np.ndarray:
    """每条 rfft 谱线对应的频率（Hz）。FS=N=256 时恰为 0..128 的整数。"""
    return np.fft.rfftfreq(WINDOW, d=1.0 / FS)


def band_bin_indices(f_low: float, f_high: float) -> np.ndarray:
    """频带 [f_low, f_high]（Hz，闭区间，按谱线中心频率）覆盖的谱线索引。"""
    freqs = bin_frequencies()
    return np.nonzero((freqs >= f_low) & (freqs <= f_high))[0]


def validate_band(f_low: float, f_high: float) -> tuple[float, float]:
    """校验频带范围，返回 float 二元组；不合法抛 ValueError。"""
    f_low = float(f_low)
    f_high = float(f_high)
    if not (math.isfinite(f_low) and math.isfinite(f_high)):
        raise ValueError("频带上下限必须是有限数字")
    if f_low < 0.0:
        raise ValueError("频率下限不能小于 0 Hz")
    if f_high > NYQUIST + 1e-9:
        raise ValueError(f"频率上限不能超过 Nyquist 频率 {NYQUIST} Hz")
    if f_low > f_high:
        raise ValueError("频率下限不能大于上限")
    return f_low, f_high
