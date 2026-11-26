"""单列采样 CSV 解析。

规则：
- UTF-8（可带 BOM）文本，每行一个采样值；
- 空行跳过；
- 第一条非空行若不是数字，视为表头跳过；之后出现非数字行报错；
- 容忍行尾多出的一个空字段（某些导出会带尾随逗号）；
- 不接受真正的多列 CSV；
- NaN / Inf 等非有限值拒收。
"""

from __future__ import annotations

import csv
import io
import math

import numpy as np


class CsvParseError(ValueError):
    """CSV 内容不合法。"""


def parse_single_column_csv(raw: bytes | str) -> np.ndarray:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CsvParseError("CSV 必须是 UTF-8 文本") from exc
    else:
        text = raw

    values: list[float] = []
    first_nonempty_seen = False

    for lineno, physical in enumerate(text.splitlines(), start=1):
        line = physical.strip()
        if not line:
            continue

        try:
            row = next(csv.reader(io.StringIO(line)))
        except csv.Error as exc:
            raise CsvParseError(f"第 {lineno} 行无法解析：{exc}") from exc

        # 容忍恰好一个尾随空字段，例如 "0.12,"
        if len(row) == 2 and row[1].strip() == "":
            row = row[:1]

        if len(row) != 1:
            # 第一条非空行无条件视为表头（即使表头文字里含逗号）
            if not first_nonempty_seen:
                first_nonempty_seen = True
                continue
            raise CsvParseError(
                f"第 {lineno} 行有 {len(row)} 列，本系统只接受单列采样 CSV"
            )

        token = row[0].strip()
        try:
            value = float(token)
        except (TypeError, ValueError):
            if not first_nonempty_seen:
                first_nonempty_seen = True
                continue  # 首条非空行当作表头
            raise CsvParseError(f"第 {lineno} 行不是数字：{token!r}")

        first_nonempty_seen = True

        if not math.isfinite(value):
            raise CsvParseError(f"第 {lineno} 行是非有限值（NaN/Inf）：{token!r}")

        values.append(value)

    if not values:
        raise CsvParseError("CSV 中没有解析到任何采样数据")

    return np.asarray(values, dtype=np.float64)
