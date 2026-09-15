"""预审请求 / 响应的 Pydantic 模型与跨字段校验。"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .terms import parse_term

CourseId = str
CategoryId = str
RuleId = str

_NON_EMPTY = re.compile(r"\S")


def _require_id(value: str, label: str) -> str:
    if not _NON_EMPTY.search(value or ""):
        raise ValueError(f"{label}不能为空")
    return value.strip()


# ---------------------------------------------------------------- 培养方案侧

class CategorySpec(BaseModel):
    """必修类别（如：专业必修 30 学分，自由选修可吸收富余学分）。"""

    model_config = ConfigDict(extra="forbid")

    id: CategoryId
    name: str = ""
    required_credits: float = Field(0, ge=0)
    free_elective: bool = Field(
        False, description="自由选修类别：可吸收未分配到具体课程的富余学分"
    )

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "类别 id")


class InternalCourse(BaseModel):
    """本校培养方案中的课程（毕业要求的基本单位）。"""

    model_config = ConfigDict(extra="forbid")

    id: CourseId
    name: str = ""
    credits: float = Field(..., gt=0)
    category: CategoryId
    required: bool = True
    prerequisites: list[CourseId] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    term: Optional[str] = Field(None, description="正常开课学期，如 2025秋")

    @field_validator("id", "category")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "课程/类别 id")

    @field_validator("term")
    @classmethod
    def _term_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_term(v)
        return v


class Program(BaseModel):
    model_config = ConfigDict(extra="forbid")

    degree_required_credits: float = Field(..., ge=0, description="毕业总学分")
    min_home_credits: float = Field(..., ge=0, description="校内最低修读学分")
    categories: list[CategorySpec] = Field(default_factory=list)
    courses: list[InternalCourse] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_refs(self) -> "Program":
        cat_ids = {c.id for c in self.categories}
        if len(self.categories) != len(cat_ids):
            raise ValueError("培养方案类别 id 重复")
        known = cat_ids
        ids = {c.id for c in self.courses}
        if len(self.courses) != len(ids):
            raise ValueError("校内课程 id 重复")
        for c in self.courses:
            if c.category not in known:
                raise ValueError(f"课程 {c.id} 引用了不存在的类别 {c.category}")
            unknown = [p for p in c.prerequisites if p not in ids]
            if unknown:
                raise ValueError(f"课程 {c.id} 的先修课不存在: {unknown}")
        free = [c.id for c in self.categories if c.free_elective]
        if len(free) > 1:
            raise ValueError(f"自由选修类别最多一个: {free}")
        return self


class CompletedCourse(BaseModel):
    """已修课程：可按 id 精确匹配，或仅记录学分用于校内学分累计。"""

    model_config = ConfigDict(extra="forbid")

    id: CourseId
    credits: Optional[float] = Field(None, gt=0)
    term: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "已修课程 id")

    @field_validator("term")
    @classmethod
    def _term_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_term(v)
        return v


# ---------------------------------------------------------------- 校外课程侧

class ExternalCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CourseId
    name: str = ""
    host_credits: float = Field(..., gt=0, description="校外实际学分")
    term: str = Field(..., description="开课时段，如 2025秋(1-8周)")
    slots: list[str] = Field(default_factory=list, description="上课时段标签，如 周一第1节")
    outcomes: list[str] = Field(default_factory=list)
    prerequisites: list[CourseId] = Field(
        default_factory=list, description="校内课程先修要求（必须已修）"
    )

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "校外课程 id")

    @field_validator("term")
    @classmethod
    def _term_ok(cls, v: str) -> str:
        parse_term(v)
        return v.strip()


class CreditConversion(BaseModel):
    """学分换算规则：换算学分 = host_credits * rate，再受 min/max 截断。

    按 host_school / external course id 匹配，第一条命中的规则生效；
    未命中任何规则时 rate=1、不截断。
    """

    model_config = ConfigDict(extra="forbid")

    id: RuleId
    rate: float = Field(..., gt=0)
    min_credits: float = Field(0, ge=0)
    max_credits: Optional[float] = Field(None, gt=0)
    host_school: Optional[str] = None
    external_course_id: Optional[CourseId] = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "换算规则 id")

    @model_validator(mode="after")
    def _bounds(self) -> "CreditConversion":
        if self.max_credits is not None and self.max_credits < self.min_credits:
            raise ValueError(f"换算规则 {self.id} 的 min 大于 max")
        return self


class EquivalenceRule(BaseModel):
    """课程等价规则：一条边 = 某门校外课可向某门校内课输送学分。

    多对一：多条规则指向同一门 internal 课，学分可合并凑满。
    一对多：同一 external 课出现在多条规则中，其学分被拆分。
    min/max 限制该规则本身可贡献的换算学分；不允许出现“只用一部分且低于
    min”的抵扣（引擎会剔除这种分配并报 RULE_MIN 冲突）。
    """

    model_config = ConfigDict(extra="forbid")

    id: RuleId
    external_course_id: CourseId
    internal_course_id: CourseId
    min_credits: float = Field(0, ge=0)
    max_credits: Optional[float] = Field(
        None, gt=0, description="不填则最多到校外课换算学分与校内课缺口"
    )
    outcomes_covered: list[str] = Field(
        default_factory=list, description="按等价关系认定覆盖的学习成果"
    )

    @field_validator("id", "external_course_id", "internal_course_id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        return _require_id(v, "规则/课程 id")

    @model_validator(mode="after")
    def _bounds(self) -> "EquivalenceRule":
        if self.max_credits is not None and self.max_credits < self.min_credits:
            raise ValueError(f"等价规则 {self.id} 的 min 大于 max")
        return self


# ---------------------------------------------------------------- 整体请求

class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_school: Optional[str] = None
    max_workload_credits: float = Field(..., gt=0, description="交换期间课业负担上限（校外实际学分）")
    max_courses: Optional[int] = Field(None, ge=1)
    max_transferable_credits: float = Field(..., ge=0, description="交换总可认定学分上限")
    conversions: list[CreditConversion] = Field(default_factory=list)
    equivalences: list[EquivalenceRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "Rules":
        for name, items in (("换算规则", self.conversions),
                            ("等价规则", self.equivalences)):
            ids = [r.id for r in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} id 重复")
        return self


class PreevaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: Optional[str] = Field(
        None, description="可选幂等键；不影响内容哈希"
    )
    program: Program
    completed: list[CompletedCourse] = Field(default_factory=list)
    external_courses: list[ExternalCourse]
    rules: Rules
    must_include: list[CourseId] = Field(
        default_factory=list, description="锁定必选的校外课"
    )
    do_not_replace: list[CourseId] = Field(
        default_factory=list, description="锁定不愿替换的校内课（不允许被校外课抵扣）"
    )

    @model_validator(mode="after")
    def _cross_checks(self) -> "PreevaluationRequest":
        ext = {c.id for c in self.external_courses}
        if len(ext) != len(self.external_courses):
            raise ValueError("校外课程 id 重复")
        internal = {c.id for c in self.program.courses}
        if self.completed:
            comp_ids = [c.id for c in self.completed]
            if len(comp_ids) != len(set(comp_ids)):
                raise ValueError("已修课程 id 重复")
        missing_locked = [c for c in self.must_include if c not in ext]
        if missing_locked:
            raise ValueError(f"锁定必选的校外课不存在: {missing_locked}")
        unknown_keep = [c for c in self.do_not_replace if c not in internal]
        if unknown_keep:
            raise ValueError(f"不愿替换的校内课不存在: {unknown_keep}")
        conv_ids = {c.external_course_id for c in self.rules.conversions
                    if c.external_course_id}
        unknown_conv = [c for c in conv_ids if c not in ext]
        if unknown_conv:
            raise ValueError(f"换算规则引用了不存在的校外课: {sorted(unknown_conv)}")
        eq = self.rules.equivalences
        unknown_ext = {r.external_course_id for r in eq if r.external_course_id not in ext}
        unknown_int = {r.internal_course_id for r in eq
                       if r.internal_course_id not in internal}
        if unknown_ext:
            raise ValueError(f"等价规则引用了不存在的校外课: {sorted(unknown_ext)}")
        if unknown_int:
            raise ValueError(f"等价规则引用了不存在的校内课: {sorted(unknown_int)}")
        locked_host = sum(c.host_credits for c in self.external_courses
                          if c.id in set(self.must_include))
        if locked_host > self.rules.max_workload_credits + 1e-9:
            raise ValueError("锁定必选课程的学分合计已超过课业负担上限")
        return self


# ---------------------------------------------------------------- 响应

class ConversionStep(BaseModel):
    rule_id: Optional[RuleId]
    host_credits: float
    rate: float
    converted_before_cap: float
    min_credits: float
    max_credits: Optional[float]
    converted_credits: float
    transferable: bool
    reason: Optional[str] = None


class AllocationItem(BaseModel):
    """一门校内课的抵扣去向。"""

    internal_course_id: CourseId
    required_credits: float
    allocated_credits: float
    satisfied: bool
    rule_ids: list[RuleId]
    from_external: dict[CourseId, float]
    category: CategoryId


class ExternalAllocation(BaseModel):
    """一门已选校外课的换算与去向。"""

    external_course_id: CourseId
    host_credits: float
    converted_credits: float
    transferable: bool
    conversion: ConversionStep
    allocated: dict[CourseId, float]
    allocated_total: float
    free_elective_credits: float = Field(
        0.0, description="无具名校内课对应、被自由选修类别容量吸收的学分"
    )
    leftover_credits: float
    rule_violations: list[RuleId]


class Violation(BaseModel):
    code: Literal[
        "RULE_MIN", "PREREQ",
        "SLOT_OVERLAP", "MIN_HOME", "TRANSFER_CAP",
        "LOCKED_INELIGIBLE", "WORKLOAD",
    ]
    severity: Literal["error", "warning"]
    message: str
    external_course_id: Optional[CourseId] = None
    internal_course_id: Optional[CourseId] = None
    rule_id: Optional[RuleId] = None
    detail: Optional[dict] = None


class CategoryRemaining(BaseModel):
    category_id: CategoryId
    required_credits: float
    completed_credits: float
    recognized_credits: float
    leftover_absorbed: float
    remaining_credits: float
    unsatisfied_required: list[CourseId]


class PlanOutcome(BaseModel):
    selected: list[CourseId]
    host_credits: float
    converted_credits: float
    recognized_credits: float
    leftover_elective_credits: float
    feasible: bool
    hard_conflict: bool
    allocations: list[AllocationItem]
    external_allocations: list[ExternalAllocation]
    violations: list[Violation]
    category_remaining: list[CategoryRemaining]
    required_gap_credits: float
    uncovered_outcomes: list[str]
    projected_home_credits: float
    projected_remaining_degree_credits: float
    score: list[float]


class PreevaluationResult(BaseModel):
    input_hash: str
    best_plan: Optional[PlanOutcome]
    plans_evaluated: int
    enumeration_truncated: bool
    locked_must_include: list[CourseId]
    version_id: int
    created_at: str
    reused: bool
