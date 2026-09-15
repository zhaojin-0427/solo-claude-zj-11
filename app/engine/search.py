"""在课业负担上限内枚举校外课组合并选出最优方案。

枚举按课程门数从少到多（锁定必选课程始终在内）。候选课按 (学分, id) 排序，
同一门数层内一旦前缀学分超过负担上限，后续组合必然也超重，直接截断。
评分由 :mod:`evaluate` 给出：先无硬冲突，再必修缺口覆盖、可认定学分、
未覆盖成果数、课程门数；同分按选课 id 字典序决胜。
"""
from __future__ import annotations

from itertools import combinations

from ..models import ExternalCourse, PlanOutcome, PreevaluationRequest
from .evaluate import build_context, evaluate_selection

# 单次请求最多评估的组合数；超出后置 enumeration_truncated
MAX_PLANS = 50_000


def search(req: PreevaluationRequest) -> tuple[PlanOutcome | None, int, bool]:
    ctx = build_context(req)
    by_id = {ec.id: ec for ec in req.external_courses}
    locked_ids = list(req.must_include)
    locked = [by_id[cid] for cid in locked_ids]
    optional = [ec for ec in req.external_courses if ec.id not in set(locked_ids)]
    # 学分升序：超重截断时前面的小组合都已评估过
    optional.sort(key=lambda c: (c.host_credits, c.id))

    workload = req.rules.max_workload_credits
    max_courses = req.rules.max_courses
    locked_host = round(sum(c.host_credits for c in locked), 4)
    upper = len(optional)
    if max_courses is not None:
        upper = min(upper, max(0, max_courses - len(locked)))

    best: PlanOutcome | None = None
    evaluated = 0
    truncated = False

    def consider(combo: list[ExternalCourse]) -> None:
        nonlocal best, evaluated
        plan = evaluate_selection(ctx, combo)
        evaluated += 1
        if best is None or plan.score > best.score or (
                plan.score == best.score and plan.selected < best.selected):
            best = plan

    # 锁定课单独超负担也要评估（WORKLOAD 作为硬冲突体现在方案里）
    if locked_host > workload + 1e-9:
        consider(locked)
        return best, evaluated, truncated

    for k in range(0, upper + 1):
        # 下界剪枝：最轻的 k 门可选课都超重，则本层及更大层不可能合法
        lightest = round(sum(c.host_credits for c in optional[:k]), 4)
        if locked_host + lightest > workload + 1e-9:
            if evaluated == 0 and locked:
                consider(locked)  # 保留一个可评估的冲突方案
            break
        for chosen_idx in combinations(range(len(optional)), k):
            host = locked_host + round(
                sum(optional[i].host_credits for i in chosen_idx), 4)
            if host > workload + 1e-9:
                continue
            consider(locked + [optional[i] for i in chosen_idx])
            if evaluated >= MAX_PLANS:
                truncated = True
                break
        if truncated:
            break

    if best is None:  # external_courses 允许为空时
        consider(locked)
    return best, evaluated, truncated
