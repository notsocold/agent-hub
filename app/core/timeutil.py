"""时间差计算的统一入口。

本机（以及很多 CI 容器）的 NTP 校正会让系统时钟出现毫秒级甚至数百毫秒的**回退**。
直接做 ``(now - started).total_seconds() * 1000`` 会算出负数，进而写出
``duration_ms=-742`` 这类脏数据，让「耗时 >= 0」这类断言随机失败。

耗时统计的语义下限就是 0，因此统一在这里夹紧，避免每个调用点各写一份 ``max(0, ...)``。
"""

from __future__ import annotations

from datetime import UTC, datetime


def elapsed_ms(start: datetime, end: datetime | None = None) -> int:
    """返回 ``start`` 到 ``end`` 的毫秒数；时钟回退时返回 0，不返回负数。"""
    finish = end if end is not None else datetime.now(UTC)
    return max(0, int((finish - start).total_seconds() * 1000))


__all__ = ["elapsed_ms"]
