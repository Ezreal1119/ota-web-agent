from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ValidationError

_STANDARD = re.compile(r"^[A-Za-z0-9]{8}[0-9]{6}$")
_SPECIAL = re.compile(r"^[A-Za-z0-9]{15,23}$")


@dataclass(frozen=True)
class Interval:
    prefix: str
    start: int
    end: int

    def text(self) -> str:
        start, end = f"{self.prefix}{self.start:06d}", f"{self.prefix}{self.end:06d}"
        return start if self.start == self.end else f"{start}-{end}"


def _parse(value: str) -> Interval | str:
    if not isinstance(value, str):
        raise ValidationError("SN 必须使用字符串，以保留前导零。")
    if value != value.strip() or not value:
        raise ValidationError("SN 不能为空或含首尾空白。")
    parts = value.split("-")
    if len(parts) == 2:
        start, end = parts
        if (
            not _STANDARD.fullmatch(start)
            or not _STANDARD.fullmatch(end)
            or start[:8] != end[:8]
            or start > end
        ):
            raise ValidationError("SN 区间必须为同前缀的 14 位 SN，起点不大于终点。")
        return Interval(start[:8], int(start[8:]), int(end[8:]))
    if len(parts) != 1:
        raise ValidationError("SN 格式不正确。")
    if _STANDARD.fullmatch(value):
        return Interval(value[:8], int(value[8:]), int(value[8:]))
    if _SPECIAL.fullmatch(value) and (len(value) != 23 or value.startswith("Y3AT582")):
        return value
    raise ValidationError("标准 SN 应为 14 位；特殊 SN 为 15–23 位，23 位需以 Y3AT582 开头。")


def _merge(intervals: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for item in sorted(intervals, key=lambda x: (x.prefix, x.start, x.end)):
        if result and result[-1].prefix == item.prefix and item.start <= result[-1].end + 1:
            previous = result.pop()
            result.append(Interval(item.prefix, previous.start, max(previous.end, item.end)))
        else:
            result.append(item)
    return result


def canonical(entries: list[str]) -> list[str]:
    items = [_parse(v) for v in entries]
    return [v.text() for v in _merge([i for i in items if isinstance(i, Interval)])] + sorted(
        {i for i in items if isinstance(i, str)}
    )


def change(entries: list[str], requested: list[str], *, remove: bool = False) -> list[str]:
    if not requested:
        raise ValidationError("请提供至少一个 SN 或区间。")
    existing = [_parse(v) for v in canonical(entries)]
    wanted = [_parse(v) for v in requested]
    if not remove:
        return canonical(entries + requested)
    intervals = [i for i in existing if isinstance(i, Interval)]
    specials = {i for i in existing if isinstance(i, str)}
    for item in wanted:
        if isinstance(item, str):
            specials.discard(item)
            continue
        remaining = []
        for old in intervals:
            if old.prefix != item.prefix or item.end < old.start or item.start > old.end:
                remaining.append(old)
                continue
            if old.start < item.start:
                remaining.append(Interval(old.prefix, old.start, item.start - 1))
            if item.end < old.end:
                remaining.append(Interval(old.prefix, item.end + 1, old.end))
        intervals = remaining
    return [i.text() for i in _merge(intervals)] + sorted(specials)
