#!/usr/bin/env python3
"""生成“跨段样例”单列采样 CSV（256 Hz，确定性信号，无需随机数）。

在本文件所在目录写出：
- demo_seg1_start0.csv    样点 0..299
- demo_seg2_start300.csv  样点 300..649
- demo_seg3_start700.csv  样点 700..1023（650..699 故意留缺）

信号：20 Hz 振幅 2.0 + 55 Hz 振幅 0.6（初相 0.7）+ 90 Hz 振幅 0.3，
三个频率在 256 点窗内都是整周期，因此频带均方值可精确核对：
20 Hz 谱线带内均方 = 2.0^2 / 2 = 2.0。

用法：python3 samples/generate_samples.py
"""

from __future__ import annotations

import os

import numpy as np

FS = 256
N_TOTAL = 1024

# (文件名, 起始样点, 结束样点不含)
SEGMENTS = [
    ("demo_seg1_start0.csv", 0, 300),
    ("demo_seg2_start300.csv", 300, 650),
    ("demo_seg3_start700.csv", 700, 1024),
]


def build_signal(n: int = N_TOTAL) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / FS
    return (
        2.0 * np.sin(2.0 * np.pi * 20.0 * t)
        + 0.6 * np.sin(2.0 * np.pi * 55.0 * t + 0.7)
        + 0.3 * np.sin(2.0 * np.pi * 90.0 * t)
    )


def main() -> None:
    out_dir = os.path.dirname(os.path.abspath(__file__))
    x = build_signal()
    for filename, lo, hi in SEGMENTS:
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 256 Hz single-column samples; "
                     f"global sample indices [{lo} .. {hi})\n")
            for value in x[lo:hi]:
                fh.write(f"{value:.10f}\n")
        print(f"wrote {path} ({hi - lo} samples, start={lo})")


if __name__ == "__main__":
    main()
