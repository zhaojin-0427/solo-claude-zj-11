from app.engine.flow import Dinic, to_credits, to_units


def test_simple_max_flow():
    d = Dinic(4)
    d.add_edge(0, 1, 10)
    d.add_edge(1, 2, 7)
    d.add_edge(1, 3, 5)
    d.add_edge(2, 3, 4)
    # 1→2→3 最多 4，1→3 最多 5，共 9
    assert d.max_flow(0, 3) == 9


def test_many_to_one_merge():
    # 两门 3 学分的课合并抵一门 5 学分课，只能凑满 5（sink 容量限制）
    d = Dinic(5)
    d.add_edge(0, 1, to_units(3))
    d.add_edge(0, 2, to_units(3))
    d.add_edge(1, 3, to_units(3))
    d.add_edge(2, 3, to_units(3))
    d.add_edge(3, 4, to_units(5))
    assert to_credits(d.max_flow(0, 4)) == 5.0


def test_one_to_many_split():
    # 一门 6 学分拆给两门 3 学分课
    d = Dinic(5)
    d.add_edge(0, 1, to_units(6))
    d.add_edge(1, 2, to_units(6))
    d.add_edge(1, 3, to_units(6))
    d.add_edge(2, 4, to_units(3))
    d.add_edge(3, 4, to_units(3))
    assert to_credits(d.max_flow(0, 4)) == 6.0


def test_global_cap():
    d = Dinic(3)
    idx = d.add_edge(0, 1, to_units(100))
    d.add_edge(1, 2, to_units(100))
    assert to_credits(d.max_flow(0, 2, limit=to_units(12))) == 12.0
    assert to_credits(d.used_on_edge(0, idx)) == 12.0
