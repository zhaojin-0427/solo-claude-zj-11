"""开课时段解析与比较。

支持形如 ``2025春`` / ``2025春(1-8周)`` / ``2025-SPRING`` / ``2025T1`` 的写法。
排序键为 (年份, 季节序)，春 < 暑 < 秋；``2025T1`` 视为秋季学年第一学期。
周次冲突通过 :func:`week_ranges` 解析括号内的周次区间判断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SEASON_RANK = {"SPRING": 0, "SUMMER": 1, "AUTUMN": 2, "FALL": 2, "WINTER": 3}
_CN_SEASON = {"春": "SPRING", "暑": "SUMMER", "夏": "SUMMER",
              "秋": "AUTUMN", "冬": "WINTER"}
_TERM_RE = re.compile(
    r"^\s*(\d{4})\s*(?:年)?\s*"
    r"(?:([春夏秋冬])|(?:[-/\s]?\s*)(SPRING|SUMMER|AUTUMN|FALL|WINTER)|T\s*([123]))\s*"
    r"(?:学期)?\s*(?:[\((（]([^)）]*)[\)）])?\s*$",
    re.IGNORECASE,
)
# 形如 “1-8周”“第3周”“1-8,10” 的周次片段
_WEEK_RE = re.compile(r"(\d+)\s*(?:[-~–至]\s*(\d+))?")


class TermError(ValueError):
    """无法解析的开课时段。"""


@dataclass(frozen=True)
class Term:
    raw: str
    year: int
    season_rank: int
    weeks: tuple[tuple[int, int], ...] = ()

    @property
    def order_key(self) -> tuple[int, int]:
        return (self.year, self.season_rank)

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return self.raw


def parse_term(text: str) -> Term:
    m = _TERM_RE.match(text or "")
    if not m:
        raise TermError(f"无法解析的开课时段: {text!r}")
    year = int(m.group(1))
    if m.group(2):
        rank = _SEASON_RANK[_CN_SEASON[m.group(2)]]
    elif m.group(3):
        rank = _SEASON_RANK[m.group(3).upper()]
    else:
        # T1 秋, T2 春（次年），T3 暑；排序时 T1 属于当年秋
        rank = {1: 2, 2: 0, 3: 1}[int(m.group(4))]
        if m.group(4) == "2":
            year += 1
    weeks = tuple(
        (int(a), int(b or a)) for a, b in _WEEK_RE.findall(m.group(5) or "")
    )
    return Term(raw=text.strip(), year=year, season_rank=rank, weeks=weeks)


def earlier_or_equal(left: Term, right: Term) -> bool:
    """left 的开课时段不晚于 right（用于先修链判断）。"""
    return left.order_key <= right.order_key


def slots_overlap(left_raw: str, right_raw: str) -> bool:
    """两门课开课时段是否冲突。

    不同学期/季节不冲突；同一学期内，未给周次信息则整学期占用（视为冲突），
    都给了周次则要求周次区间不相交。
    """
    left, right = parse_term(left_raw), parse_term(right_raw)
    if left.order_key != right.order_key:
        return False
    if not left.weeks or not right.weeks:
        return True
    return any(not (b1 < a2 or b2 < a1) for a1, b1 in left.weeks for a2, b2 in right.weeks)
