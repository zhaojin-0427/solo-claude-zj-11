import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("PREEVAL_DB_PATH", str(db))
    # 延迟导入，使全局 store 读取到测试用环境变量
    import importlib

    import app.main as main
    importlib.reload(main)
    main._store = main.VersionStore(str(db))
    with TestClient(main.app) as c:
        yield c, main._store


def payload():
    return {
        "program": {
            "degree_required_credits": 120,
            "min_home_credits": 90,
            "categories": [
                {"id": "major", "name": "专业必修", "required_credits": 40},
                {"id": "free", "name": "自由选修", "required_credits": 20,
                 "free_elective": True},
            ],
            "courses": [
                {"id": "CS101", "name": "数据结构", "credits": 4,
                 "category": "major", "outcomes": ["O1"]},
                {"id": "CS201", "name": "算法", "credits": 4,
                 "category": "major", "prerequisites": ["CS101"],
                 "outcomes": ["O2"]},
            ],
        },
        "completed": [
            {"id": "CS101", "credits": 4, "term": "2025秋"},
        ],
        "external_courses": [
            {"id": "A", "name": "伙伴校算法", "host_credits": 4,
             "term": "2026春", "slots": ["周一1"],
             "prerequisites": ["CS101"], "outcomes": ["O2"]},
        ],
        "rules": {
            "host_school": "伙伴大学",
            "max_workload_credits": 12,
            "max_transferable_credits": 10,
            "conversions": [
                {"id": "c1", "rate": 1.0, "host_school": "伙伴大学"},
            ],
            "equivalences": [
                {"id": "e1", "external_course_id": "A",
                 "internal_course_id": "CS201",
                 "outcomes_covered": ["O2"]},
            ],
        },
        "must_include": [],
        "do_not_replace": [],
    }


def test_full_flow_and_version_reuse(client):
    c, store = client
    r1 = c.post("/api/preevaluations", json=payload())
    assert r1.status_code == 200, r1.text
    data1 = r1.json()
    assert data1["reused"] is False
    assert data1["input_hash"].startswith("sha256:")
    assert data1["version_id"] >= 1
    best = data1["best_plan"]
    assert best["required_gap_credits"] == 0.0
    assert best["feasible"]
    # 逐课抵扣去向与换算过程
    alloc = best["allocations"][0]
    assert alloc["internal_course_id"] == "CS201"
    assert alloc["from_external"] == {"A": 4.0}
    ext = best["external_allocations"][0]
    assert ext["conversion"]["host_credits"] == 4
    assert ext["conversion"]["converted_credits"] == 4.0

    # 同输入再次提交 → 复用同一不可变版本
    r2 = c.post("/api/preevaluations", json=payload())
    data2 = r2.json()
    assert data2["reused"] is True
    assert data2["version_id"] == data1["version_id"]
    assert data2["input_hash"] == data1["input_hash"]
    assert data2["best_plan"] == data1["best_plan"]
    assert data2["plans_evaluated"] == data1["plans_evaluated"]

    # 按版本号取回
    r3 = c.get(f"/api/preevaluations/{data1['version_id']}")
    assert r3.status_code == 200
    assert r3.json()["input_hash"] == data1["input_hash"]

    # 不存在的版本
    assert c.get("/api/preevaluations/9999").status_code == 404


def test_key_order_does_not_change_hash(client):
    c, _ = client
    p1 = payload()
    import json
    canonical = json.dumps(p1, sort_keys=True)
    reversed_json = json.dumps(p1, sort_keys=False)
    d1 = c.post("/api/preevaluations", content=canonical,
                headers={"content-type": "application/json"}).json()
    d2 = c.post("/api/preevaluations", content=reversed_json,
                headers={"content-type": "application/json"}).json()
    assert d1["input_hash"] == d2["input_hash"]
    assert d2["reused"] is True


def test_request_id_ignored_by_hash(client):
    c, _ = client
    p1 = payload()
    p2 = payload()
    p1["request_id"] = "req-001"
    p2["request_id"] = "req-002"
    d1 = c.post("/api/preevaluations", json=p1).json()
    d2 = c.post("/api/preevaluations", json=p2).json()
    assert d1["input_hash"] == d2["input_hash"]
    assert d2["reused"] is True


def test_validation_error_payload(client):
    c, _ = client
    p = payload()
    p["rules"]["equivalences"][0]["internal_course_id"] = "GHOST"
    r = c.post("/api/preevaluations", json=p)
    assert r.status_code == 422
    assert "校验失败" in r.json()["error"]


def test_min_home_warning(client):
    c, _ = client
    p = payload()
    # 已修本校 4 学分；即使 A 认定 4 学分也不计入校内最低修读
    p["program"]["min_home_credits"] = 100
    d = c.post("/api/preevaluations", json=p).json()
    best = d["best_plan"]
    mh = [v for v in best["violations"] if v["code"] == "MIN_HOME"]
    assert mh
    assert mh[0]["detail"]["home_completed_credits"] == 4.0
    assert mh[0]["detail"]["shortfall"] == 96.0
    # projected_home_credits 只含本校实际修读，不含交换认定
    assert best["projected_home_credits"] == 4.0
    assert best["recognized_credits"] >= 4.0


def test_lock_exceeding_workload_rejected(client):
    c, _ = client
    p = payload()
    p["must_include"] = ["A"]
    p["rules"]["max_workload_credits"] = 3  # 锁定的 A 有 4 学分，超限
    r = c.post("/api/preevaluations", json=p)
    assert r.status_code == 422
    assert "课业负担" in r.text


def test_workload_conflict_in_searched_plan(client):
    """负担上限 3 但未锁定：空组合最优（不选课即不冲突），
    而三门以上组合会报 WORKLOAD；这里直接验证评估接口侧的行为。"""
    from app.engine.evaluate import build_context, evaluate_selection
    from app.models import PreevaluationRequest
    p = payload()
    p["rules"]["max_workload_credits"] = 3
    req = PreevaluationRequest(**p)
    ctx = build_context(req)
    plan = evaluate_selection(ctx, [req.external_courses[0]])  # A = 4 学分
    assert any(v.code == "WORKLOAD" for v in plan.violations)
    assert plan.hard_conflict


def test_uncovered_outcomes_reported(client):
    c, _ = client
    p = payload()
    # 去掉 A 对 O2 的成果覆盖：课程与规则都不覆盖
    p["external_courses"][0]["outcomes"] = []
    p["rules"]["equivalences"][0]["outcomes_covered"] = []
    d = c.post("/api/preevaluations", json=p).json()
    assert "O2" in d["best_plan"]["uncovered_outcomes"]
