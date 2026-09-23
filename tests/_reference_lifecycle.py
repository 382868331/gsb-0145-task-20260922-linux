"""v2 生命周期分析的独立有界穷举参考（仅用于测试对照）。

与 ``tests/_reference.py``（v1 DAG 全序列枚举）同级：本模块**不 import
被测核心**，语义在此独立重新编码，与
:mod:`borrow_check.lifecycle` 的受护事实表不动点是两条完全独立的路线。

参考做法（穷举）：

1. 枚举全部 ``2**k`` 个布尔条件原子赋值（小输入 k<=3 时 8 个，确定性
   大条件用例 k=8 时 256 个）；
2. 每个赋值下，沿只走"守卫为真"的边，在 **(块, 具体状态)** 状态图上
   BFS，深度（块步数）上限 ``(unroll+1)*n``（unroll 默认 4 轮）；
3. 逐操作具体执行，记录每个 (块,操作) 现场触发的冲突 kind；
4. 返回每个 (kind, block, op) 在哪些条件赋值行（按原子名字典序位编码）
   下、在深度预算内可达。

具体语义（独立编码）：

* 移动：子树移动（祖先压子孙，反向不成立）；reinit 只恢复子树并拆分祖先；
* 借用：每出生点产生 token（循环中同一 loan 可有多个存活 token）；
  reserve 态允许共享读，activate 才独占；end/drop/declare 清 token；
* drop：逆"本次游走中已执行且未被前一个 drop 析构"的声明顺序析构，
  只析构仍初始化路径；
* declare：整变量重新初始化。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

# (kind, block, op) -> 该冲突在预算内可达的条件赋值行集合
RefResult = Dict[Tuple[str, int, int], Set[int]]


def _all_paths(ty: Any, prefix: Tuple[str, ...]) -> List[Tuple[str, ...]]:
    out = [prefix]
    if isinstance(ty, dict):
        for f, sub in ty.items():
            out.extend(_all_paths(sub, (*prefix, f)))
    return out


def _is_prefix(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def _eval(expr: Any, assign: Dict[str, bool]) -> bool:
    if isinstance(expr, str):
        return assign[expr]
    (key, val), = expr.items()
    if key == "not":
        return not _eval(val, assign)
    vals = [_eval(p, assign) for p in val]
    return all(vals) if key == "and" else any(vals)


class _Ref:
    def __init__(self, cfg_dict: Dict[str, Any], unroll: int = 4,
                 state_cap: int = 100_000) -> None:
        self.blocks = cfg_dict["blocks"]
        self.succs = cfg_dict["successors"]
        self.entry = cfg_dict.get("entry", 0)
        self.n = len(self.blocks)
        self.unroll = unroll
        self.state_cap = state_cap

        self.types: Dict[str, Any] = {}
        self.loans: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
        self.decl_site: Dict[str, Tuple[int, int]] = {}
        for bi, b in enumerate(self.blocks):
            for oi, op in enumerate(b["ops"]):
                if op["op"] == "declare":
                    self.types[op["var"]] = op.get("ty")
                    self.decl_site[op["var"]] = (bi, oi)
                elif op["op"] in ("borrow_shared", "borrow_mut",
                                  "borrow_reserve"):
                    kind = {"borrow_shared": "shared",
                            "borrow_mut": "mut",
                            "borrow_reserve": "reserve"}[op["op"]]
                    self.loans[op["loan_id"]] = (kind, tuple(op["path"]))
        self.paths: List[Tuple[str, ...]] = []
        for root, ty in self.types.items():
            self.paths.extend(_all_paths(ty, (root,)))

    # -- 具体状态 ----------------------------------------------------------

    def _guard_ok(self, bi: int, edge_idx: int, s: int,
                  assign: Dict[str, bool]) -> bool:
        item = self.succs[bi][edge_idx]
        if isinstance(item, int):
            return True
        return _eval(item["when"], assign)

    def _edge_target(self, bi: int, edge_idx: int) -> int:
        item = self.succs[bi][edge_idx]
        return item if isinstance(item, int) else item["block"]

    def _disabled(self, sub: FrozenSet, exact: FrozenSet,
                  p: Tuple[str, ...]) -> bool:
        if p in exact:
            return True
        return any(_is_prefix(m, p) for m in sub)

    def _overlaps(self, tokens, p: Tuple[str, ...]
                  ) -> List[Tuple[str, str]]:
        out = []
        for lid, mode in tokens:
            q = self.loans[lid][1]
            if _is_prefix(q, p) or _is_prefix(p, q):
                out.append((lid, mode))
        return sorted(out)

    def _restore(self, sub: set, exact: set, p: Tuple[str, ...]) -> None:
        for m in list(sub):
            if p == m or _is_prefix(p, m):
                sub.discard(m)
        for m in list(exact):
            if p == m or _is_prefix(p, m):
                exact.discard(m)
        for m in list(sub):
            if _is_prefix(m, p) and m != p:
                sub.discard(m)
                exact.add(m)
                root_ty = self.types
                for depth in range(len(m), len(p)):
                    prefix = p[:depth]
                    node = self._node(root_ty, prefix)
                    if isinstance(node, dict):
                        nxt = p[depth]
                        for fld in node:
                            if fld != nxt:
                                sub.add((*prefix, fld))

    def _node(self, types, path: Tuple[str, ...]):
        node = types.get(path[0], None)
        for seg in path[1:]:
            node = node[seg]
        return node

    def _droppable(self, bi: int, oi: int, alive: FrozenSet) -> List[str]:
        out: List[str] = []
        for var in alive:
            db, do = self.decl_site[var]
            if db == bi:
                if do < oi:
                    out.append(var)
            else:
                out.append(var)
        out.sort(key=lambda v: self.decl_site[v], reverse=True)
        return out

    def _step(self, bi: int, oi: int, op: Dict[str, Any], state):
        sub, exact, tokens, alive = state
        sub, exact = set(sub), set(exact)
        tokens = set(tokens)
        alive = set(alive)
        kinds: List[str] = []
        kind_op = op["op"]

        def root_tokens(var):
            return {t for t in tokens if self.loans[t[0]][1][0] == var}

        if kind_op == "declare":
            var = op["var"]
            if root_tokens(var):
                kinds.append("write_while_borrowed")
            tokens -= root_tokens(var)
            sub = {m for m in sub if m[0] != var}
            exact = {m for m in exact if m[0] != var}
            alive.add(var)
        elif kind_op == "move":
            p = tuple(op["path"])
            if self._disabled(sub, exact, p):
                kinds.append("use_of_moved")
            if self._overlaps(tokens, p):
                kinds.append("move_while_borrowed")
            sub = {m for m in sub
                   if not (_is_prefix(p, m) and m != p)}
            exact = {m for m in exact if not _is_prefix(p, m)}
            sub.add(p)
        elif kind_op in ("borrow_shared", "borrow_mut", "borrow_reserve"):
            p = tuple(op["path"])
            mode = {"borrow_shared": "shared", "borrow_mut": "mut",
                    "borrow_reserve": "reserved"}[kind_op]
            if self._disabled(sub, exact, p):
                kinds.append("use_of_moved")
            bad_modes = {"shared": {"mut"},
                         "mut": {"shared", "mut", "reserved"},
                         "reserved": {"shared", "mut"}}[mode]
            if any(m in bad_modes for _, m in self._overlaps(tokens, p)):
                kinds.append("borrow_conflict")
            tokens.add((op["loan_id"], mode))
        elif kind_op == "borrow_activate":
            lid = op["loan_id"]
            p = self.loans[lid][1]
            if self._disabled(sub, exact, p):
                kinds.append("use_of_moved")
            others = [x for x in self._overlaps(tokens, p) if x[0] != lid]
            leftover = (lid, "mut") in tokens
            if others or leftover:
                kinds.append("borrow_conflict")
            # 仅当路径上确实存在该 loan 的预约实例时才转为激活；对从未
            # 预约（预约点在本路径不可达的互斥分支里）的 loan 是空操作。
            if (lid, "reserved") in tokens:
                tokens.discard((lid, "reserved"))
                tokens.add((lid, "mut"))
        elif kind_op == "end_loan":
            tokens = {t for t in tokens if t[0] != op["loan_id"]}
        elif kind_op == "read":
            p = tuple(op["path"])
            if self._disabled(sub, exact, p):
                kinds.append("use_of_moved")
            if any(m == "mut" for _, m in self._overlaps(tokens, p)):
                kinds.append("read_while_mut_borrowed")
        elif kind_op == "write":
            p = tuple(op["path"])
            if self._overlaps(tokens, p):
                kinds.append("write_while_borrowed")
            if op.get("reinit", False):
                self._restore(sub, exact, p)
            elif self._disabled(sub, exact, p):
                kinds.append("use_of_moved")
        elif kind_op == "drop":
            for var in self._droppable(bi, oi, frozenset(alive)):
                hit = False
                for lid, mode in tokens:
                    q = self.loans[lid][1]
                    if q[0] != var:
                        continue
                    for qp in self.paths:
                        if qp[0] != var:
                            continue
                        if not (_is_prefix(q, qp) or _is_prefix(qp, q)):
                            continue
                        if not self._disabled(sub, exact, qp):
                            hit = True
                if hit:
                    kinds.append("drop_while_borrowed")
                tokens -= root_tokens(var)
                sub.add((var,))
                alive.discard(var)
        return (frozenset(sub), frozenset(exact), frozenset(tokens),
                frozenset(alive)), kinds

    # -- 单赋值下的有界 BFS ------------------------------------------------

    def reference_for_assignment(self, assign: Dict[str, bool]
                                 ) -> Set[Tuple[str, int, int]]:
        cap = (self.unroll + 1) * self.n + 1
        start = (frozenset(), frozenset(), frozenset(), frozenset())
        # (块, 状态) -> 已到达的最小深度
        seen: Dict[Tuple[int, tuple], int] = {
            (self.entry, start): 0}
        queue: deque[Tuple[int, tuple, int]] = deque(
            [(self.entry, start, 0)])
        found: Set[Tuple[str, int, int]] = set()
        explored = 0
        while queue:
            bi, state, depth = queue.popleft()
            if seen.get((bi, state)) != depth:
                continue  # 同一(块,状态)的更浅副本已处理
            explored += 1
            if explored > self.state_cap:
                raise _RefBudget(f"参考状态数超过 {self.state_cap}")
            cur = state
            for oi, op in enumerate(self.blocks[bi]["ops"]):
                cur, kinds = self._step(bi, oi, op, cur)
                for k in kinds:
                    found.add((k, bi, oi))
            if depth + 1 > cap:
                continue
            for ei in range(len(self.succs[bi])):
                if not self._guard_ok(bi, ei, 0, assign):
                    continue
                s = self._edge_target(bi, ei)
                key = (s, cur)
                if key in seen:
                    continue
                seen[key] = depth + 1
                queue.append((s, cur, depth + 1))
        return found

    def reference(self, conditions: List[str]) -> RefResult:
        k = len(conditions)
        rows = 1 << k
        result: RefResult = {}
        for row in range(rows):
            assign = {name: bool((row >> i) & 1)
                      for i, name in enumerate(conditions)}
            for triple in self.reference_for_assignment(assign):
                result.setdefault(triple, set()).add(row)
        return result


class _RefBudget(Exception):
    """参考穷举自身触顶（测试据此缩小用例，而非放过不一致）。"""


def reference_lifecycle_conflicts(cfg_dict: Dict[str, Any],
                                  unroll: int = 4) -> RefResult:
    """公开入口：返回 {(kind, block, op): {可达条件赋值行}}。"""
    return _Ref(cfg_dict, unroll=unroll).reference(
        cfg_dict.get("conditions", []))
