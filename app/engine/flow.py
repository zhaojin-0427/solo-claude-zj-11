"""基于 Dinic 的学分流网络（内部以整数“学分厘”为单位，避免浮点误差）。

拓扑::

    source → cap[全局可认定上限] → external[已选校外课]
           → rule[等价边] → internal[缺口校内课] → sink

两阶段用法：
  1. 只接必修缺口校内课，先保必修；
  2. 再放开选修/自由选修吸收富余学分（已分配的流量不会被抢占）。
"""
from __future__ import annotations

from collections import deque

SCALE = 10000
_EPS_UNITS = 1  # 0.0001 学分，用于浮点回读容差


def to_units(credits: float) -> int:
    return int(round(credits * SCALE))


def to_credits(units: int) -> float:
    return round(units / SCALE, 4)


class _Edge:
    __slots__ = ("to", "rev", "cap", "init_cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.init_cap = cap


class Dinic:
    def __init__(self, n: int) -> None:
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, fr: int, to: int, cap: int) -> int:
        """添加有向边，返回正向边在 g[fr] 中的下标。"""
        i = len(self.g[fr])
        self.g[fr].append(_Edge(to, len(self.g[to]), cap))
        self.g[to].append(_Edge(fr, i, 0))
        return i

    def _bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and level[e.to] < 0:
                    level[e.to] = level[v] + 1
                    q.append(e.to)
        return level

    def _dfs(self, v: int, t: int, f: int, level: list[int], it: list[int]) -> int:
        if v == t:
            return f
        while it[v] < len(self.g[v]):
            e = self.g[v][it[v]]
            if e.cap > 0 and level[e.to] == level[v] + 1:
                d = self._dfs(e.to, t, min(f, e.cap), level, it)
                if d:
                    e.cap -= d
                    self.g[e.to][e.rev].cap += d
                    return d
            it[v] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: int | None = None) -> int:
        flow = 0
        while True:
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            while True:
                f = self._dfs(s, t, (limit - flow) if limit is not None else 10**18,
                              level, it)
                if f == 0:
                    break
                flow += f
                if limit is not None and flow >= limit:
                    return flow

    def used_on_edge(self, fr: int, edge_index: int) -> int:
        """正向边上已经走掉的流量（= 初始容量 - 剩余容量）。"""
        e = self.g[fr][edge_index]
        return e.init_cap - e.cap
