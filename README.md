# 交换学分预审 API（Exchange Credit Pre-evaluation）

交换学习前，用校外课程组合预判返校后的学分认定结果。Python 计算认定方案，
FastAPI + Pydantic 接收并校验输入，SQLite 保存**不可变预审版本**：同一输入
哈希重复提交始终返回同一结果。

## 能力

- **一对多 / 多对一认定**：一门校外课的换算学分可拆给多门校内课；多门校外课
  可合并凑满一门校内课。网络流（Dinic，整数“学分厘”）求全局最优分配。
- **同一门校外课不重复抵扣**：学分在 `全局认定上限 → 校外课 → 等价规则 →
  校内课缺口 → 类别名额` 的级联容量中只流过一次。
- **三阶段优先级**：① 必修缺口 → ② 选修缺口（含自由选修类别内具名课程）→
  ③ 自由选修类别吸收富余学分；后一阶段不会抢占前一阶段的分配。
- **规则**：
  - 学分换算 `host × rate`，受 `min/max` 截断，支持按课程或按合作院校匹配；
  - 等价规则可设 `min/max`；出现“非零但低于 min”的分配会整条剔除并报
    `RULE_MIN`；
  - 先修链：校外课声明的先修必须已修且修读学期早于交换开课学期；被认定的
    校内课自身先修也必须已修；
  - 必修类别 / 自由选修类别名额、**校内最低修读学分**（只按本校实际修读
    学分核算，交换认定学分不计入）、交换期课业负担上限（按校外实际学分）、
    总可认定学分上限、最大选课门数；
  - 开课时段：`2026秋(1-8周)` 形式，周次不相交不冲突；同学期周次相交且
    上课时段标签相交报 `SLOT_OVERLAP`；时段缺失按保守原则报风险。
- **锁定**：`must_include` 锁定必选（搜索时始终保留）；`do_not_replace`
  锁定校内课（不允许被校外课抵扣）。
- **搜索与排序**：按门数从少到多枚举，前缀学分担剪枝；评分依次为
  可行（无硬冲突）→ 必修缺口填补学分 → 可认定学分 → 未覆盖学习成果（少）
  → 课程门数（少），同分按选课 id 字典序决胜，结果确定。
- **响应**：逐课抵扣去向（`allocated` 为具名校内课抵扣，
  `free_elective_credits` 为被自由选修容量吸收的部分，二者与
  `leftover_credits` 之和等于该课换算学分）、逐课换算过程、未满足规则、
  分类别剩余毕业要求、本校预计修读学分与剩余毕业总学分、规范化输入 SHA-256。
- **不可变版本**：SQLite 中 `input_hash` 唯一，只 INSERT 不 UPDATE/DELETE；
  同哈希再次提交直接复用（`reused=true`）。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 数据库默认 data/preeval.db，可用 PREEVAL_DB_PATH 覆盖
```

交互式文档：<http://127.0.0.1:8000/docs>

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/preevaluations` | 提交预审请求，返回最优方案与版本号 |
| GET  | `/api/preevaluations/{version_id}` | 读取历史不可变版本 |
| GET  | `/health` | 健康检查 |

输入主体见 `examples/request.json`，关键字段：

```jsonc
{
  "program": {                 // 培养方案
    "degree_required_credits": 140,
    "min_home_credits": 90,    // 校内最低修读学分
    "categories": [{"id": "free", "required_credits": 20,
                    "free_elective": true}],
    "courses": [{"id": "CS201", "credits": 4, "category": "major",
                 "required": true, "prerequisites": ["CS101"],
                 "outcomes": ["O-ALGO"], "term": "2025秋"}]
  },
  "completed": [{"id": "CS101", "credits": 4, "term": "2025春"}],
  "external_courses": [{"id": "H-ALGO", "host_credits": 6,
                        "term": "2026秋(1-8周)", "slots": ["周一3-4"],
                        "prerequisites": ["CS101"], "outcomes": ["O-ALGO"]}],
  "rules": {
    "host_school": "伙伴大学",
    "max_workload_credits": 14,
    "max_courses": 3,
    "max_transferable_credits": 12,
    "conversions": [{"id": "r1", "rate": 0.67, "min_credits": 1,
                     "max_credits": 4, "host_school": "伙伴大学"}],
    "equivalences": [{"id": "eq1", "external_course_id": "H-ALGO",
                      "internal_course_id": "CS201", "min_credits": 0,
                      "outcomes_covered": ["O-ALGO"]}]
  },
  "must_include": ["H-ALGO"],
  "do_not_replace": []
}
```

违规代码：`PREREQ`、`SLOT_OVERLAP`、`WORKLOAD`（硬冲突，error）；
`RULE_MIN`、`TRANSFER_CAP`、`MIN_HOME`、`LOCKED_INELIGIBLE`（warning）。

## 测试

```bash
pytest -q
```

覆盖最大流的多对一合并 / 一对多拆分 / 全局上限、换算截断、先修链、周次冲突、
RULE_MIN 剔除、自由选修吸收、搜索排序、哈希稳定性与版本复用。

## 项目结构

```
app/
  models.py          Pydantic 模型与跨字段校验
  terms.py           开课时段/周次解析与冲突判断
  hashing.py         规范化输入哈希
  engine/
    flow.py          Dinic 最大流（整数单位）
    evaluate.py      三阶段认定核算、逐课去向、违规、类别余额、成果覆盖
    search.py        组合枚举、剪枝与排序
  storage.py         SQLite 不可变版本
  main.py            FastAPI 路由
tests/               pytest 用例
examples/            示例请求
```
