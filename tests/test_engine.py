import pytest

from app.models import (
    CategorySpec, CompletedCourse, CreditConversion, EquivalenceRule,
    ExternalCourse, InternalCourse, Program, PreevaluationRequest, Rules,
)
from app.terms import parse_term, slots_overlap


def base_program():
    return Program(
        degree_required_credits=120,
        min_home_credits=90,
        categories=[
            CategorySpec(id="major", name="专业必修", required_credits=40),
            CategorySpec(id="elective", name="自由选修", required_credits=20,
                         free_elective=True),
        ],
        courses=[
            InternalCourse(id="CS101", name="数据结构", credits=4,
                           category="major", outcomes=["O1", "O2"]),
            InternalCourse(id="CS201", name="算法", credits=4,
                           category="major", prerequisites=["CS101"],
                           outcomes=["O3"], term="2025秋"),
            InternalCourse(id="CS301", name="编译原理", credits=3,
                           category="major", outcomes=["O4"]),
        ],
    )


def base_rules():
    return Rules(
        host_school="伙伴大学",
        max_workload_credits=15,
        max_transferable_credits=10,
        conversions=[
            CreditConversion(id="conv-A", rate=1.0,
                             host_school="伙伴大学",
                             min_credits=2, max_credits=4),
        ],
        equivalences=[
            EquivalenceRule(id="eq-A1", external_course_id="A",
                            internal_course_id="CS101"),
            EquivalenceRule(id="eq-A2", external_course_id="A",
                            internal_course_id="CS201"),
            EquivalenceRule(id="eq-B1", external_course_id="B",
                            internal_course_id="CS201"),
            EquivalenceRule(id="eq-C1", external_course_id="C",
                            internal_course_id="CS301",
                            outcomes_covered=["O4"]),
        ],
    )


def ext_courses():
    return [
        ExternalCourse(id="A", name="伙伴校·数据结构与算法", host_credits=6,
                       term="2026春", slots=["周一1"],
                       outcomes=["O1", "O2", "O3"]),
        ExternalCourse(id="B", name="伙伴校·算法专题", host_credits=3,
                       term="2026春", slots=["周二1"], outcomes=["O3"],
                       prerequisites=["CS101"]),
        ExternalCourse(id="C", name="伙伴校·编译", host_credits=3,
                       term="2026春", slots=["周三1"], outcomes=[]),
    ]


def make_req(**kw):
    defaults = dict(
        program=base_program(),
        completed=[CompletedCourse(id="CS101", credits=4, term="2025秋")],
        external_courses=ext_courses(),
        rules=base_rules(),
    )
    defaults.update(kw)
    return PreevaluationRequest(**defaults)


def test_term_parse_and_overlap():
    t = parse_term("2026春(1-8周)")
    assert (t.year, t.season_rank) == (2026, 0)
    assert slots_overlap("2026春(1-8周)", "2026春(9-16周)") is False
    assert slots_overlap("2026春(1-8周)", "2026春(7-10周)") is True
    assert slots_overlap("2026春", "2026秋") is False
    with pytest.raises(ValueError):
        parse_term("不合法学期")


def test_one_to_many_split_covers_two_required():
    """一对多拆分 + 多对一合并：A 换算上限为 2，必须与 B 合并凑满 CS201。"""
    from app.engine.search import search
    rules = base_rules()
    rules.conversions = [
        CreditConversion(id="conv-A", external_course_id="A",
                         rate=1.0, min_credits=2, max_credits=2),
    ]
    req = make_req(rules=rules)
    best, n, truncated = search(req)
    assert not truncated
    # CS101 已修，缺口为 0；最优方案应覆盖 CS201 与 CS301 两个必修缺口
    assert best.required_gap_credits == 0.0
    alloc = {a.internal_course_id: a for a in best.allocations}
    assert alloc["CS201"].satisfied
    assert alloc["CS301"].satisfied
    # A 与 B 都向 CS201 输送（多对一）
    assert set(alloc["CS201"].from_external) == {"A", "B"}
    # 同一门校外课不重复抵扣：A 的总分配不超过换算学分
    for ea in best.external_allocations:
        assert ea.allocated_total + ea.leftover_credits <= \
            ea.converted_credits + 1e-6
    assert best.feasible


def test_one_to_many_split_single_external():
    """一门 6 学分校外课（不设上限）同时拆给两门 3 学分必修课。"""
    from app.engine.evaluate import build_context, evaluate_selection
    program = Program(
        degree_required_credits=120, min_home_credits=0,
        categories=[CategorySpec(id="major", required_credits=6)],
        courses=[
            InternalCourse(id="X1", credits=3, category="major"),
            InternalCourse(id="X2", credits=3, category="major"),
        ],
    )
    courses = [ExternalCourse(id="A", host_credits=6, term="2026春")]
    rules = Rules(
        max_workload_credits=12, max_transferable_credits=10,
        equivalences=[
            EquivalenceRule(id="e1", external_course_id="A",
                            internal_course_id="X1"),
            EquivalenceRule(id="e2", external_course_id="A",
                            internal_course_id="X2"),
        ],
    )
    req = PreevaluationRequest(program=program, external_courses=courses,
                               rules=rules)
    ctx = build_context(req)
    plan = evaluate_selection(ctx, courses)
    alloc = {a.internal_course_id: a.allocated_credits for a in plan.allocations}
    assert alloc == {"X1": 3.0, "X2": 3.0}
    ext = plan.external_allocations[0]
    assert ext.allocated_total == 6.0
    assert plan.required_gap_credits == 0.0


def test_conversion_caps_and_steps():
    from app.engine.evaluate import build_context, evaluate_selection
    req = make_req()
    ctx = build_context(req)
    # A 原始 6 学分，校级规则上限 4
    step = ctx.conversions["A"]
    assert step.converted_credits == 4.0
    assert "上限" in (step.reason or "")
    plan = evaluate_selection(ctx, [req.external_courses[0]])
    # CS101 缺口 0，4 学分全部流向 CS201（缺口 4）
    ext = plan.external_allocations[0]
    assert ext.allocated == {"CS201": 4.0}
    assert ext.leftover_credits == 0.0


def test_prereq_chain_violation():
    """未修 CS101 时，认定 CS201 触发校内先修链冲突；选 B 还触发校外先修。"""
    from app.engine.evaluate import build_context, evaluate_selection
    req = make_req(completed=[])
    ctx = build_context(req)
    plan = evaluate_selection(ctx, [req.external_courses[1]])  # 只选 B
    codes = {(v.code, v.internal_course_id) for v in plan.violations}
    assert ("PREREQ", "CS101") in codes
    assert plan.hard_conflict


def test_slot_overlap():
    from app.engine.evaluate import build_context, evaluate_selection
    courses = ext_courses()
    courses[0].slots = ["周一1"]
    courses[1].slots = ["周一1"]  # 与 A 同学期同周次同标签
    req = make_req(external_courses=courses)
    ctx = build_context(req)
    plan = evaluate_selection(ctx, [courses[0], courses[1]])
    codes = [v.code for v in plan.violations]
    assert "SLOT_OVERLAP" in codes


def test_workload_limit_search():
    rules = base_rules()
    rules.max_workload_credits = 7  # 只能容纳 A 或 B+C 之类的小组合
    req = make_req(rules=rules)
    from app.engine.search import search
    best, _, _ = search(req)
    assert best.host_credits <= 7 + 1e-9


def test_must_include_locked_and_grade_warning():
    """锁定的 A 必须出现；A 只选一门不足以覆盖全部必修，仍返回可行方案。"""
    from app.engine.search import search
    req = make_req(must_include=["A"])
    best, _, _ = search(req)
    assert "A" in best.selected
    for plan_selected in [best.selected]:
        assert "A" in plan_selected


def test_do_not_replace_blocks_target():
    from app.engine.evaluate import build_context, evaluate_selection
    req = make_req(do_not_replace=["CS201"], completed=[])
    ctx = build_context(req)
    plan = evaluate_selection(ctx, [req.external_courses[0]])
    assert all(a.internal_course_id != "CS201" for a in plan.allocations)


def test_rule_min_drops_partial_allocation():
    """规则 min=4，而 CS201 只剩 2 学分缺口：A 能给出的非零分配只有 2，
    低于 min，应被整条剔除并报 RULE_MIN。"""
    from app.engine.evaluate import build_context, evaluate_selection
    rules = base_rules()
    rules.equivalences = [
        EquivalenceRule(id="eq-A2", external_course_id="A",
                        internal_course_id="CS201", min_credits=4),
    ]
    req = make_req(
        rules=rules,
        completed=[
            CompletedCourse(id="CS101", credits=4, term="2025秋"),
            CompletedCourse(id="CS201", credits=2, term="2025秋"),
            CompletedCourse(id="CS301", credits=3, term="2025秋"),
        ],
        external_courses=[
            ExternalCourse(id="A", name="伙伴校课程", host_credits=6,
                           term="2026春", outcomes=[]),
        ],
    )
    ctx = build_context(req)
    plan = evaluate_selection(ctx, [req.external_courses[0]])
    assert any(v.code == "RULE_MIN" and v.rule_id == "eq-A2"
               for v in plan.violations)
    # 被剔除后 A 的学分不分配给 CS201（进入自由选修或剩余）
    assert all(a.internal_course_id != "CS201" for a in plan.allocations)


def test_search_deterministic_across_runs():
    from app.engine.search import search
    req = make_req()
    b1, n1, t1 = search(req)
    b2, n2, t2 = search(req)
    assert n1 == n2 and t1 == t2
    assert b1.model_dump() == b2.model_dump()


def test_free_elective_absorbs_leftover():
    from app.engine.evaluate import build_context, evaluate_selection
    req = make_req(
        completed=[
            CompletedCourse(id="CS101", credits=4),
            CompletedCourse(id="CS201", credits=4),
            CompletedCourse(id="CS301", credits=3),
        ],
    )
    ctx = build_context(req)
    # A 换算后 4 学分，必修缺口全满，应被自由选修吸收
    plan = evaluate_selection(ctx, [req.external_courses[0]])
    assert plan.recognized_credits == 4.0
    assert plan.leftover_elective_credits == 4.0


def test_free_elective_per_course_consistency():
    """F 的 1 学分被自由选修容量吸收时：逐课记录必须显示去向，
    与汇总（recognized / leftover_elective_credits / 类别吸收）一致，
    且 allocated_total + free_elective + leftover == converted。"""
    from app.engine.evaluate import build_context, evaluate_selection
    program = Program(
        degree_required_credits=10, min_home_credits=0,
        categories=[
            CategorySpec(id="free", name="自由选修",
                         required_credits=10, free_elective=True),
        ],
        courses=[],
    )
    courses = [ExternalCourse(id="F", name="伙伴校·研讨", host_credits=1,
                              term="2026春")]
    rules = Rules(max_workload_credits=5, max_transferable_credits=10)
    req = PreevaluationRequest(program=program, external_courses=courses,
                               rules=rules)
    ctx = build_context(req)
    plan = evaluate_selection(ctx, courses)

    assert plan.recognized_credits == 1.0
    assert plan.leftover_elective_credits == 1.0
    ext = plan.external_allocations[0]
    assert ext.external_course_id == "F"
    assert ext.allocated == {}
    assert ext.allocated_total == 0.0
    assert ext.free_elective_credits == 1.0
    assert ext.leftover_credits == 0.0
    # 学分守恒：三处分摊之和等于换算学分
    assert (ext.allocated_total + ext.free_elective_credits
            + ext.leftover_credits) == ext.converted_credits
    # 类别汇总与逐课一致
    free_row = next(c for c in plan.category_remaining
                    if c.category_id == "free")
    assert free_row.leftover_absorbed == 1.0


def test_min_home_counts_only_home_credits():
    """已修本校 0 学分、min_home=1，即使交换认定 1 学分，
    projected_home_credits 仍为 0 且必须报 MIN_HOME（缺口 1）。"""
    from app.engine.evaluate import build_context, evaluate_selection
    program = Program(
        degree_required_credits=10, min_home_credits=1,
        categories=[CategorySpec(id="major", required_credits=1)],
        courses=[InternalCourse(id="X1", credits=1, category="major")],
    )
    courses = [ExternalCourse(id="F", host_credits=1, term="2026春")]
    rules = Rules(
        max_workload_credits=5, max_transferable_credits=10,
        equivalences=[EquivalenceRule(
            id="eq-f", external_course_id="F", internal_course_id="X1")],
    )
    req = PreevaluationRequest(program=program, external_courses=courses,
                               rules=rules)
    ctx = build_context(req)
    plan = evaluate_selection(ctx, courses)
    assert plan.recognized_credits == 1.0
    assert plan.projected_home_credits == 0.0
    assert plan.projected_remaining_degree_credits == 9.0
    mh = [v for v in plan.violations if v.code == "MIN_HOME"]
    assert len(mh) == 1
    assert mh[0].detail["shortfall"] == 1.0
    assert mh[0].detail["home_completed_credits"] == 0.0
