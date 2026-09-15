"""组合评估：给定一组已选校外课，计算认定方案与冲突。

两阶段、两张独立静态流网络：
  阶段 1 仅允许流向必修缺口校内课，先填补必修；
  阶段 2 以阶段 1 后的余量（校外课余量、总认定上限余量、类别名额余量）
  放开选修缺口与自由选修类别吸收富余学分——必修分配已冻结，不会被抢占。

每条等价规则是 ``external → rule 节点 → internal``；低于规则 min 但非零的
分配会被整条剔除并迭代重算，报 RULE_MIN 冲突。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    AllocationItem, CategoryRemaining, CategorySpec, CompletedCourse,
    ConversionStep, CreditConversion, EquivalenceRule, ExternalAllocation,
    ExternalCourse, PlanOutcome, Violation,
)
from .flow import Dinic, to_credits, to_units

_BIG = 10**12


@dataclass
class InternalState:
    course: InternalCourse
    required_credits: float           # 已考虑已修后的缺口
    completed_credits: float          # 已修（精确匹配，截断到课程学分）
    allocated: float = 0.0
    from_external: dict[str, float] = field(default_factory=dict)
    rule_ids: list[str] = field(default_factory=list)


@dataclass
class IndexedContext:
    req: PreevaluationRequest
    internal: dict[str, InternalState]
    conversions: dict[str, ConversionStep]
    converted: dict[str, float]
    rules_by_ext: dict[str, list[EquivalenceRule]]
    outcomes_required: set[str]
    completed_terms: dict[str, str | None]
    home_completed_credits: float
    category_specs: dict[str, CategorySpec]
    free_category: CategorySpec | None
    ext_map: dict[str, ExternalCourse]


# ---------------------------------------------------------------- 准备阶段

def _convert(course: ExternalCourse, rules: list[CreditConversion],
             host_school: str | None) -> ConversionStep:
    matched: CreditConversion | None = None
    for r in rules:
        if r.external_course_id == course.id:
            matched = r
            break
        if r.host_school is not None and r.host_school == host_school:
            matched = r
            break
    host = course.host_credits
    if matched is None:
        return ConversionStep(
            rule_id=None, host_credits=host, rate=1.0,
            converted_before_cap=host, min_credits=0.0, max_credits=None,
            converted_credits=host, transferable=True,
        )
    raw = host * matched.rate
    capped = max(raw, matched.min_credits)
    reason: str | None = None
    if matched.max_credits is not None and capped > matched.max_credits:
        capped = matched.max_credits
        reason = f"超过规则 {matched.id} 的上限 {matched.max_credits}，按上限认定"
    if raw < matched.min_credits:
        reason = f"低于规则 {matched.id} 的下限 {matched.min_credits}，按下限认定"
    return ConversionStep(
        rule_id=matched.id, host_credits=host, rate=matched.rate,
        converted_before_cap=round(raw, 4), min_credits=matched.min_credits,
        max_credits=matched.max_credits, converted_credits=round(capped, 4),
        transferable=True, reason=reason,
    )


def build_context(req: PreevaluationRequest) -> IndexedContext:
    completed_map: dict[str, CompletedCourse] = {c.id: c for c in req.completed}

    internal: dict[str, InternalState] = {}
    home_completed = 0.0
    for c in req.program.courses:
        done = completed_map.get(c.id)
        if done is not None:
            credits = done.credits if done.credits is not None else c.credits
            taken = min(credits, c.credits)
            gap = max(0.0, round(c.credits - credits, 4))
            state = InternalState(c, gap, round(taken, 4))
        else:
            state = InternalState(c, c.credits, 0.0)
        internal[c.id] = state
        home_completed += state.completed_credits
    for c in req.completed:
        if c.id not in internal and c.credits is not None:
            home_completed += c.credits

    conversions: dict[str, ConversionStep] = {}
    converted: dict[str, float] = {}
    for ec in req.external_courses:
        step = _convert(ec, req.rules.conversions, req.rules.host_school)
        conversions[ec.id] = step
        converted[ec.id] = step.converted_credits

    rules_by_ext: dict[str, list[EquivalenceRule]] = {}
    for r in req.rules.equivalences:
        rules_by_ext.setdefault(r.external_course_id, []).append(r)

    outcomes_required = {o for c in req.program.courses if c.required
                         for o in c.outcomes}
    completed_terms = {c.id: c.term for c in req.completed}
    specs = {s.id: s for s in req.program.categories}
    free = next((s for s in req.program.categories if s.free_elective), None)

    return IndexedContext(
        req=req, internal=internal, conversions=conversions,
        converted=converted, rules_by_ext=rules_by_ext,
        outcomes_required=outcomes_required, completed_terms=completed_terms,
        home_completed_credits=round(home_completed, 4),
        category_specs=specs, free_category=free,
        ext_map={ec.id: ec for ec in req.external_courses},
    )


# ---------------------------------------------------------------- 流网络

@dataclass
class _AR:
    """一条激活的等价规则边。"""
    ref: EquivalenceRule
    ext_id: str
    used: int = 0                          # 两阶段合计（学分厘）
    edge_refs: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class _FlowOutcome:
    rule_used: dict[str, int]             # rule id -> 流向校内课
    free_total: int                       # 流入自由选修类别总量
    free_direct_ext: dict[str, int]       # 无等价边的校外课直接计入自由选修
    total: int


def _active_rules(ctx: IndexedContext, selected: list[ExternalCourse],
                  disabled: set[str]) -> list[_AR]:
    do_not = set(ctx.req.do_not_replace)
    out: list[_AR] = []
    for ec in selected:
        for r in ctx.rules_by_ext.get(ec.id, []):
            if r.id in disabled or r.internal_course_id in do_not:
                continue
            out.append(_AR(r, ec.id))
    return out


def _run_stage(ext_left: dict[str, int], cap_left_units: int,
               active: list[_AR], demand: dict[str, float],
               ctx: IndexedContext
               ) -> tuple[dict[str, int], int, int]:
    """跑一段静态流。

    拓扑 source → cap(全局认定余量) → ext(校外课余量) → rule → internal → sink。
    返回 (rule_id→流量, 本段总认定量, 本段总认定量)（后两者相同，保留二元便于
    阅读）；调用方自行从 rule 流量汇总 ext 消耗。
    """
    target_ids = [cid for cid, v in demand.items() if v > 1e-9]
    rules_here = [a for a in active if a.ref.internal_course_id in set(target_ids)]
    n = 3 + len(ext_left) + len(rules_here) + len(target_ids)
    d = Dinic(n)
    source, cap_node, sink = 0, 1, 2
    base_ext = 3
    base_rule = base_ext + len(ext_left)
    base_int = base_rule + len(rules_here)
    ext_ids = list(ext_left.keys())
    ext_n = {cid: base_ext + i for i, cid in enumerate(ext_ids)}
    int_n = {cid: base_int + i for i, cid in enumerate(target_ids)}

    cap_edge = d.add_edge(source, cap_node, max(0, cap_left_units))
    for cid in ext_ids:
        d.add_edge(cap_node, ext_n[cid], max(0, ext_left[cid]))
    for cid in target_ids:
        d.add_edge(int_n[cid], sink, to_units(demand[cid]))
    for i, a in enumerate(rules_here):
        rnode = base_rule + i
        r = a.ref
        cap = min(to_credits(ext_left[a.ext_id]),
                  demand[r.internal_course_id])
        if r.max_credits is not None:
            cap = min(cap, r.max_credits)
        idx = d.add_edge(ext_n[a.ext_id], rnode, to_units(cap))
        a.edge_refs.append((ext_n[a.ext_id], idx))
        d.add_edge(rnode, int_n[r.internal_course_id], _BIG)

    d.max_flow(source, sink)
    used: dict[str, int] = {}
    for a in rules_here:
        node, idx = a.edge_refs[-1]
        u = d.used_on_edge(node, idx)
        if u:
            used[a.ref.id] = u
    return used, d.used_on_edge(source, cap_edge), d.used_on_edge(source, cap_edge)


def _run_free_stage(ext_left: dict[str, int], cap_left_units: int,
                    free_room: float) -> tuple[dict[str, int], int]:
    """自由选修吸收：任意校外课富余学分 → 自由选修类别名额。"""
    if free_room <= 1e-9:
        return {}, 0
    ext_ids = [cid for cid, left in ext_left.items() if left > 0]
    if not ext_ids or cap_left_units <= 0:
        return {}, 0
    n = 4 + len(ext_ids)
    d = Dinic(n)
    source, cap_node, sink, free_node = 0, 1, 2, 3
    cap_edge = d.add_edge(source, cap_node, max(0, cap_left_units))
    refs: dict[str, tuple[int, int]] = {}
    for i, cid in enumerate(ext_ids):
        enode = 4 + i
        idx = d.add_edge(cap_node, enode, ext_left[cid])
        d.add_edge(enode, free_node, ext_left[cid])
        refs[cid] = (cap_node, idx)
    d.add_edge(free_node, sink, to_units(free_room))
    d.max_flow(source, sink)
    used = {cid: d.used_on_edge(node, idx)
            for cid, (node, idx) in refs.items()}
    return ({cid: u for cid, u in used.items() if u},
            d.used_on_edge(source, cap_edge))


def _solve(ctx: IndexedContext, selected: list[ExternalCourse],
           disabled: set[str]) -> _FlowOutcome:
    req = ctx.req
    rules = _active_rules(ctx, selected, disabled)
    int_ids = list(ctx.internal.keys())
    ext_ids = [ec.id for ec in selected]

    ext_left = {cid: to_units(ctx.converted[cid]) for cid in ext_ids}
    cap_left = to_units(req.rules.max_transferable_credits)
    rule_used: dict[str, int] = {}

    def _consume(used: dict[str, int], total_units: int) -> None:
        nonlocal cap_left
        by_ext: dict[str, int] = {}
        for rid, u in used.items():
            rule = next(a for a in rules if a.ref.id == rid)
            by_ext[rule.ext_id] = by_ext.get(rule.ext_id, 0) + u
            rule.used += u
            rule_used[rid] = rule.used
        for cid, u in by_ext.items():
            ext_left[cid] -= u
        cap_left -= total_units

    # ---- 阶段 1：必修缺口 ----
    req_demand = {cid: st.required_credits for cid, st in ctx.internal.items()
                  if st.course.required}
    used1, total1, _ = _run_stage(ext_left, cap_left, rules, req_demand, ctx)
    _consume(used1, total1)

    # 必修课后仍剩余的缺口（多对一未必凑满）
    for rid, u in used1.items():
        target = next(a for a in rules if a.ref.id == rid).ref.internal_course_id
        req_demand[target] = max(0.0, req_demand[target] - to_credits(u))

    # ---- 阶段 2：选修（含自由选修类别内的具名课程）缺口 ----
    elec_demand = {cid: st.required_credits for cid, st in ctx.internal.items()
                   if not st.course.required}
    used2, total2, _ = _run_stage(ext_left, cap_left, rules, elec_demand, ctx)
    _consume(used2, total2)
    for rid, u in used2.items():
        target = next(a for a in rules if a.ref.id == rid).ref.internal_course_id
        elec_demand[target] = max(0.0, elec_demand[target] - to_credits(u))

    # ---- 阶段 3：自由选修类别吸收任意富余学分 ----
    free = ctx.free_category
    free_total = 0
    free_direct: dict[str, int] = {}
    if free is not None:
        committed = 0.0
        for cid, st in ctx.internal.items():
            if st.course.category != free.id:
                continue
            committed += st.completed_credits
            if st.course.required:
                committed += st.required_credits - req_demand[cid]
            else:
                committed += st.required_credits - elec_demand[cid]
        room = max(0.0, free.required_credits - committed)
        free_direct, free_total = _run_free_stage(ext_left, cap_left, room)
        # 自由选修不再细分规则，余量直接扣减
        for cid, u in free_direct.items():
            ext_left[cid] -= u
        cap_left -= free_total

    total = to_units(req.rules.max_transferable_credits) - cap_left
    return _FlowOutcome(rule_used, free_total, free_direct, total)


def _solve_with_min(ctx: IndexedContext, selected: list[ExternalCourse]
                    ) -> tuple[_FlowOutcome, list[str]]:
    """迭代剔除“分配量 > 0 但低于 min”的规则，直到稳定。

    返回最终流结果与被剔除（报 RULE_MIN）的规则 id 列表。
    """
    disabled: set[str] = set()
    dropped: list[str] = []
    rules = {r.id: r for r in ctx.req.rules.equivalences}
    while True:
        out = _solve(ctx, selected, disabled)
        bad: list[str] = []
        for rid, units in out.rule_used.items():
            r = rules[rid]
            if 0 < units < to_units(r.min_credits) - 1:
                bad.append(rid)
        if not bad:
            return out, dropped
        dropped.extend(rid for rid in bad if rid not in dropped)
        disabled.update(bad)


# ---------------------------------------------------------------- 冲突检测

def _term_before(ext_term_raw: str, prereq_term_raw: str | None) -> bool:
    """先修课已在交换开课之前修完。已修课没有 term 记录时默认已满足时序。"""
    if prereq_term_raw is None:
        return True
    from ..terms import parse_term
    return parse_term(prereq_term_raw).order_key < parse_term(ext_term_raw).order_key


def _collect_violations(ctx: IndexedContext, selected: list[ExternalCourse],
                        selected_ids: set[str], dropped: list[str],
                        outcome: _FlowOutcome) -> list[Violation]:
    req = ctx.req
    violations: list[Violation] = []
    rules_by_id = {r.id: r for r in req.rules.equivalences}

    # RULE_MIN：被剔除的规则
    for rid in dropped:
        r = rules_by_id[rid]
        violations.append(Violation(
            code="RULE_MIN", severity="warning",
            message=(f"等价规则 {rid}（{r.external_course_id}→"
                     f"{r.internal_course_id}）可分配学分低于下限 "
                     f"{r.min_credits}，该抵扣去向未被采纳"),
            external_course_id=r.external_course_id,
            internal_course_id=r.internal_course_id, rule_id=rid,
            detail={"min_credits": r.min_credits},
        ))

    # PREREQ：先修课未修或修读时间晚于交换开课
    completed = {c.id: c for c in req.completed}
    for ec in selected:
        for pid in ec.prerequisites:
            done = completed.get(pid)
            if done is None:
                violations.append(Violation(
                    code="PREREQ", severity="error",
                    message=f"校外课 {ec.id} 要求先修 {pid}，但未在已修课程中",
                    external_course_id=ec.id, internal_course_id=pid,
                ))
            elif not _term_before(ec.term, ctx.completed_terms.get(pid)):
                violations.append(Violation(
                    code="PREREQ", severity="error",
                    message=(f"先修课 {pid} 修读时间不早于 {ec.id} 的开课学期 "
                             f"{ec.term}"),
                    external_course_id=ec.id, internal_course_id=pid,
                ))

    # 校内课程先修链：被抵扣的校内课若要求先修，先修课必须已修（缺口课程之间
    # 不能互相满足）
    target_ids = {rules_by_id[rid].internal_course_id
                  for rid in outcome.rule_used}
    for cid in target_ids:
        st = ctx.internal[cid]
        for pid in st.course.prerequisites:
            if completed.get(pid) is None:
                violations.append(Violation(
                    code="PREREQ", severity="error",
                    message=f"拟认定课程 {cid} 的先修课 {pid} 尚未修读",
                    internal_course_id=cid,
                ))

    # SLOT_OVERLAP：同学期、周次相交时，上课时段标签相交即冲突；
    # 任一方未填时段标签则按“时间未知”保守判为冲突风险。
    from ..terms import slots_overlap
    for i, a in enumerate(selected):
        for b in selected[i + 1:]:
            if not slots_overlap(a.term, b.term):
                continue
            shared = sorted(set(a.slots) & set(b.slots))
            if shared:
                violations.append(Violation(
                    code="SLOT_OVERLAP", severity="error",
                    message=(f"{a.id} 与 {b.id} 在 {a.term} 上课时段冲突 "
                             f"({', '.join(shared)})"),
                    external_course_id=a.id,
                    detail={"other": b.id, "slots": shared},
                ))
            elif not a.slots or not b.slots:
                violations.append(Violation(
                    code="SLOT_OVERLAP", severity="error",
                    message=(f"{a.id} 与 {b.id} 同学期 {a.term} 且至少一方"
                             f"未填写上课时段，无法排除时间冲突"),
                    external_course_id=a.id,
                    detail={"other": b.id, "slots": []},
                ))

    # WORKLOAD：课业负担上限（校外实际学分）
    host_total = round(sum(ec.host_credits for ec in selected), 4)
    if host_total > req.rules.max_workload_credits + 1e-9:
        violations.append(Violation(
            code="WORKLOAD", severity="error",
            message=(f"校外实际学分 {host_total} 超过课业负担上限 "
                     f"{req.rules.max_workload_credits}"),
            detail={"host_credits": host_total,
                    "max_workload_credits": req.rules.max_workload_credits},
        ))

    # TRANSFER_CAP：理论可换算总量超过全局认定上限
    converted_total = round(sum(ctx.converted[ec.id] for ec in selected), 4)
    recognized = to_credits(outcome.total)
    if converted_total > req.rules.max_transferable_credits + 1e-9 \
            and converted_total - recognized > 1e-3:
        violations.append(Violation(
            code="TRANSFER_CAP", severity="warning",
            message=(f"换算学分合计 {converted_total}，超过可认定上限 "
                     f"{req.rules.max_transferable_credits}，"
                     f"{round(converted_total - recognized, 4)} 学分无法认定"),
            detail={"converted_credits": converted_total,
                    "cap": req.rules.max_transferable_credits,
                    "lost_credits": round(converted_total - recognized, 4)},
        ))

    # MIN_HOME：校内最低修读只按本校实际修读学分，交换认定学分不计入
    home_completed = ctx.home_completed_credits
    home_need = max(0.0, req.program.min_home_credits - home_completed)
    if home_need > 1e-9:
        violations.append(Violation(
            code="MIN_HOME", severity="warning",
            message=(f"本校实际修读 {round(home_completed, 4)} 学分，"
                     f"低于校内最低修读 {req.program.min_home_credits} 学分，"
                     f"返校后至少还需校内修读 {round(home_need, 4)} 学分"
                     f"（交换认定学分不计入此项）"),
            detail={"home_completed_credits": round(home_completed, 4),
                    "min_home_credits": req.program.min_home_credits,
                    "shortfall": round(home_need, 4)},
        ))

    # LOCKED_INELIGIBLE：锁定必选但没有任何可用抵扣去向
    do_not = set(req.do_not_replace)
    for ec in selected:
        if ec.id not in set(req.must_include):
            continue
        usable = [r for r in ctx.rules_by_ext.get(ec.id, [])
                  if r.id not in set(dropped)
                  and r.internal_course_id not in do_not
                  and ctx.internal[r.internal_course_id].required_credits > 1e-9]
        if not usable and ec.id not in outcome.free_direct_ext:
            violations.append(Violation(
                code="LOCKED_INELIGIBLE", severity="warning",
                message=f"锁定必选的 {ec.id} 没有可认定的抵扣去向",
                external_course_id=ec.id,
            ))
    return violations


# ---------------------------------------------------------------- 单方案核算

def _round(x: float) -> float:
    return round(x + 0.0, 4)


def evaluate_selection(ctx: IndexedContext,
                       selected: list[ExternalCourse]) -> PlanOutcome:
    """把一组已选校外课核算为完整的 :class:`PlanOutcome`。"""
    req = ctx.req
    selected_ids = {ec.id for ec in selected}
    outcome, dropped = _solve_with_min(ctx, selected)
    rules_by_id = {r.id: r for r in req.rules.equivalences}

    # ---- 校内课逐课抵扣去向 ----
    per_int: dict[str, dict[str, float]] = {}
    per_int_rules: dict[str, list[str]] = {}
    allocated_by_ext: dict[str, dict[str, float]] = {ec.id: {} for ec in selected}
    for rid, units in outcome.rule_used.items():
        r = rules_by_id[rid]
        credits = to_credits(units)
        per_int.setdefault(r.internal_course_id, {})
        per_int[r.internal_course_id][r.external_course_id] = \
            per_int[r.internal_course_id].get(r.external_course_id, 0) + credits
        per_int_rules.setdefault(r.internal_course_id, []).append(rid)
        d = allocated_by_ext.setdefault(r.external_course_id, {})
        d[r.internal_course_id] = d.get(r.internal_course_id, 0) + credits

    allocations: list[AllocationItem] = []
    for cid in ctx.internal:  # 保持培养方案中的顺序
        sources = per_int.get(cid)
        if not sources:
            continue
        st = ctx.internal[cid]
        total = _round(sum(sources.values()))
        allocations.append(AllocationItem(
            internal_course_id=cid,
            required_credits=st.required_credits,
            allocated_credits=total,
            satisfied=total + 1e-6 >= st.required_credits,
            rule_ids=per_int_rules.get(cid, []),
            from_external={k: _round(v) for k, v in sources.items()},
            category=st.course.category,
        ))

    recognized = to_credits(outcome.total)
    free_direct_credits = to_credits(sum(outcome.free_direct_ext.values()))

    # ---- 校外课逐课换算与去向 ----
    dropped_by_ext: dict[str, list[str]] = {}
    for rid in dropped:
        dropped_by_ext.setdefault(rules_by_id[rid].external_course_id, []).append(rid)
    ext_allocations: list[ExternalAllocation] = []
    for ec in selected:
        allocated = {k: _round(v) for k, v in allocated_by_ext.get(ec.id, {}).items()}
        allocated_total = _round(sum(allocated.values()))
        direct = to_credits(outcome.free_direct_ext.get(ec.id, 0))
        leftover = _round(max(0.0, ctx.converted[ec.id]
                              - allocated_total - direct))
        ext_allocations.append(ExternalAllocation(
            external_course_id=ec.id,
            host_credits=ec.host_credits,
            converted_credits=ctx.converted[ec.id],
            transferable=True,
            conversion=ctx.conversions[ec.id],
            allocated=allocated,
            allocated_total=allocated_total,
            free_elective_credits=direct,
            leftover_credits=leftover,
            rule_violations=sorted(dropped_by_ext.get(ec.id, [])),
        ))

    # ---- 未满足规则 / 冲突 ----
    violations = _collect_violations(
        ctx, selected, selected_ids, dropped, outcome)
    hard_conflict = any(v.severity == "error" for v in violations)

    # ---- 类别余额 ----
    cat_rows: list[CategoryRemaining] = []
    for spec in req.program.categories:
        members = [st for st in ctx.internal.values()
                   if st.course.category == spec.id]
        completed = _round(sum(st.completed_credits for st in members))
        recognized_in_cat = _round(sum(
            sum(per_int.get(st.course.id, {}).values()) for st in members))
        absorbed = free_direct_credits if spec.free_elective else 0.0
        remaining = _round(max(
            0.0, spec.required_credits - completed
            - recognized_in_cat - absorbed))
        unsatisfied = [
            st.course.id for st in members
            if st.course.required
            and st.required_credits
            - sum(per_int.get(st.course.id, {}).values()) > 1e-4
        ]
        cat_rows.append(CategoryRemaining(
            category_id=spec.id,
            required_credits=spec.required_credits,
            completed_credits=completed,
            recognized_credits=recognized_in_cat,
            leftover_absorbed=_round(absorbed),
            remaining_credits=remaining,
            unsatisfied_required=unsatisfied,
        ))

    # ---- 必修缺口 ----
    initial_required_gap = _round(sum(
        st.required_credits for st in ctx.internal.values()
        if st.course.required))
    required_gap = _round(sum(
        max(0.0, st.required_credits
            - sum(per_int.get(st.course.id, {}).values()))
        for st in ctx.internal.values() if st.course.required))
    gap_covered = _round(initial_required_gap - required_gap)

    # ---- 学习成果覆盖 ----
    # 只需覆盖“仍有缺口的必修课”所声明的成果；已修课程（缺口为 0）的成果
    # 视为已满足。
    covered: set[str] = set()
    contributing_ext = {ec.id for ec in selected
                        if allocated_by_ext.get(ec.id)
                        or ec.id in outcome.free_direct_ext}
    for ec in selected:
        if ec.id in contributing_ext:
            covered.update(ec.outcomes)
    for rid in outcome.rule_used:
        covered.update(rules_by_id[rid].outcomes_covered)
    need_outcomes: set[str] = set()
    for cid, st in ctx.internal.items():
        if st.course.required and st.required_credits > 1e-9:
            need_outcomes.update(st.course.outcomes)
    uncovered = sorted(need_outcomes - covered)

    # ---- 投影毕业要求 ----
    # 校内最低修读只按本校实际修读学分计算，交换认定学分不计入其中
    projected_home = _round(ctx.home_completed_credits)
    remaining_degree = _round(max(
        0.0, req.program.degree_required_credits
        - ctx.home_completed_credits - recognized))

    host_total = _round(sum(ec.host_credits for ec in selected))
    converted_total = _round(sum(ctx.converted[ec.id] for ec in selected))

    score = [
        0.0 if hard_conflict else 1.0,      # 可行方案优先
        gap_covered,                        # 先填补必修缺口
        recognized,                         # 再比可认定学分
        float(-len(uncovered)),             # 未覆盖成果越少越好
        float(-len(selected)),              # 课程门数越少越好
    ]

    return PlanOutcome(
        selected=sorted(selected_ids),
        host_credits=host_total,
        converted_credits=converted_total,
        recognized_credits=recognized,
        leftover_elective_credits=free_direct_credits,
        feasible=not hard_conflict,
        hard_conflict=hard_conflict,
        allocations=allocations,
        external_allocations=ext_allocations,
        violations=violations,
        category_remaining=cat_rows,
        required_gap_credits=required_gap,
        uncovered_outcomes=uncovered,
        projected_home_credits=projected_home,
        projected_remaining_degree_credits=remaining_degree,
        score=score,
    )
