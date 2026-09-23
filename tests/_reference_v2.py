"""v2 独立穷举参考语义（仅用于测试对照，**不 import 被测核心**）。

与被测实现（``borrow_check.analyzer_v2`` 的条件限定 may-不动点，以及
``borrow_check.replay`` 的认证器）完全独立的第三条路线：

1. 自己从纯数据解析类型树、loan、作用域、条件与守卫公式（真值表）；
2. 对 **2^k 个布尔赋值逐一**枚举，从入口做有界块游走 DFS；
3. 具体状态 = 每类型树节点状态（ok/uninit/moved/dropped）+ 活跃借用 +
   预约集合，确定性逐操作执行；
4. 用 ``(块, 具体状态)`` 记忆化剪枝：同一具体状态第二次到达同一块时，
   未来操作序列是已探索轨迹的重复，不可能产生新冲突——因此小参考域上
   的 clean 判定是**完整**的，不靠运气；
5. 同时记录每个冲突 ``(kind, block, op)`` 的**最小回边距离**（dominator
   回边计数）与一条样例 (赋值, 游走)，供核对被测的 4 轮认证预算。

参考自身也有状态访问预算；耗尽时 status="unknown"（调用方生成器保证
小输入不触发，触发则测试失败，而不是悄悄放过）。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

ConflictKey = Tuple[str, int, int]

OK = "ok"
UNINIT = "uninit"
MOVED = "moved"
DROPPED = "dropped"
EXACT = {UNINIT: "uninit_exact", MOVED: "moved_exact",
         DROPPED: "dropped_exact"}
BAD_KIND = {UNINIT: "use_of_uninitialized", MOVED: "use_of_moved",
            DROPPED: "use_of_dropped",
            "uninit_exact": "use_of_uninitialized",
            "moved_exact": "use_of_moved",
            "dropped_exact": "use_of_dropped"}
SUBTREE_LABELS = (UNINIT, MOVED, DROPPED)


def _all_paths(ty: Any, prefix: Tuple[str, ...]) -> List[Tuple[str, ...]]:
    out = [prefix]
    if isinstance(ty, dict):
        for f, sub in ty.items():
            out.extend(_all_paths(sub, (*prefix, f)))
    return out


def _is_prefix(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def _overlaps(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


def _formula_mask(tree: Any, cond_index: Dict[str, int], size: int) -> int:
    """把公式编译成赋值空间位掩码（独立实现）。"""
    if isinstance(tree, str):
        j = cond_index[tree]
        m = 0
        for b in range(size):
            if (b >> j) & 1:
                m |= 1 << b
        return m
    op = tree[0]
    if op == "const":
        return (1 << size) - 1 if tree[1] else 0
    if op == "not":
        return (~_formula_mask(tree[1], cond_index, size)) & ((1 << size) - 1)
    ms = [_formula_mask(a, cond_index, size) for a in tree[1:]]
    acc = ms[0]
    for m in ms[1:]:
        acc = (acc & m) if op == "and" else (acc | m)
    return acc


class _RefInterpreter:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.blocks = data["blocks"]
        self.succs = data["successors"]
        self.entry = data.get("entry", 0)
        self.conditions: Tuple[str, ...] = tuple(data.get("conditions", ()))
        self.k = len(self.conditions)
        self.size = 1 << self.k
        self.cond_index = {c: i for i, c in enumerate(self.conditions)}

        self.types: Dict[str, Any] = {}
        self.loan_path: Dict[str, Tuple[str, ...]] = {}
        self.loan_two_phase: Dict[str, bool] = {}
        self.loan_is_mut: Dict[str, bool] = {}
        self.scope_decls: Dict[str, List[Tuple[str, Any]]] = {}
        self.decl_order: List[str] = []
        for bi, b in enumerate(self.blocks):
            for op in b["ops"]:
                if op["op"] == "declare":
                    self.types[op["var"]] = op.get("ty")
                    self.decl_order.append(op["var"])
                    scope = op.get("scope", "0")
                    self.scope_decls.setdefault(scope, []).append(
                        (op["var"], op.get("ty")))
                elif op["op"] in ("borrow_shared", "borrow_mut"):
                    self.loan_path[op["loan_id"]] = tuple(op["path"])
                    self.loan_is_mut[op["loan_id"]] = op["op"] == "borrow_mut"
                    self.loan_two_phase[op["loan_id"]] = op.get("two_phase", False)

        self.paths: List[Tuple[str, ...]] = []
        for root, ty in self.types.items():
            self.paths.extend(_all_paths(ty, (root,)))

        # 边守卫。
        self.guards: Dict[Tuple[int, int], int] = {}
        for u, lst in enumerate(self.succs):
            for e in lst:
                if isinstance(e, dict):
                    self.guards[(u, e["block"])] = _formula_mask(
                        e["when"], self.cond_index, self.size)
        self.successor_idx = [
            [(e["block"] if isinstance(e, dict) else e) for e in lst]
            for lst in self.succs]

        # 支配集（用于回边判定）。
        self.back_edges = self._back_edges()

    def _back_edges(self) -> Set[Tuple[int, int]]:
        n = len(self.blocks)
        preds: Dict[int, List[int]] = {i: [] for i in range(n)}
        for u, vs in enumerate(self.successor_idx):
            for v in vs:
                preds[v].append(u)
        reach = {self.entry}
        stack = [self.entry]
        while stack:
            b = stack.pop()
            for v in self.successor_idx[b]:
                if v not in reach:
                    reach.add(v)
                    stack.append(v)
        dom: List[Set[int]] = [set() for _ in range(n)]
        dom[self.entry] = {self.entry}
        changed = True
        while changed:
            changed = False
            for b in range(n):
                if b == self.entry or b not in reach:
                    continue
                inc = [dom[p] for p in preds[b] if p in reach and dom[p]]
                if not inc:
                    continue
                new = {b} | set.intersection(*inc)
                if new != dom[b]:
                    dom[b] = new
                    changed = True
        back = set()
        for u, vs in enumerate(self.successor_idx):
            for v in vs:
                if v in dom[u]:
                    back.add((u, v))
        return back

    # -- 具体状态 ----------------------------------------------------------

    def fresh(self) -> Dict[Tuple[str, ...], str]:
        return {p: None for p in self.paths}  # type: ignore[dict-item]

    def snap(self, st: Dict[Tuple[str, ...], str],
             active: Dict[str, str], reserved: FrozenSet[str]
             ) -> Tuple[Any, ...]:
        return (tuple(sorted(st.items())),
                tuple(sorted(active.items())),
                tuple(sorted(reserved)))

    def _set_subtree(self, st, path, label):
        for q in self.paths:
            if _is_prefix(path, q):
                st[q] = label

    def _restore_subtree(self, st, path):
        """init / reinit：path 子树恢复 OK；严格祖先的子树标签转精确。"""
        for q in self.paths:
            if _is_prefix(path, q):
                st[q] = OK
        for depth in range(len(path) - 1):
            q = path[: depth + 1]
            lab = st.get(q)
            if lab in SUBTREE_LABELS:
                st[q] = EXACT[lab]

    def _unavailable(self, st, path):
        """方向性：子树标签祖先压制；精确标签只压自身；子孙不压父。"""
        hits = []
        for q, label in st.items():
            if label in SUBTREE_LABELS and _is_prefix(q, path):
                hits.append((q, label))
            elif label in BAD_KIND and label.endswith("_exact") and q == path:
                hits.append((q, label))
        return hits

    def _region_unavailable(self, st, path):
        """显式 drop：path 子树内任何非 OK 节点。"""
        return [(q, st[q]) for q in self.paths
                if _is_prefix(path, q) and st.get(q) in BAD_KIND]

    def _overlaps(self, active, reserved, path, *, want_shared, want_mut,
                  want_reserved):
        a, r = [], []
        for lid, mode in active.items():
            if _overlaps(self.loan_path[lid], path):
                if (mode == "shared" and want_shared) or (mode == "mut" and want_mut):
                    a.append(lid)
        if want_reserved:
            r = [lid for lid in reserved
                 if _overlaps(self.loan_path[lid], path)]
        return sorted(a), sorted(r)

    def _live_regions(self, st, path, ty):
        region_paths = [q for q in self.paths if _is_prefix(path, q)]
        if all(st.get(q) == OK for q in region_paths):
            return [path]
        out = []
        if isinstance(ty, dict):
            for f, sub in reversed(list(ty.items())):
                out.extend(self._live_regions(st, (*path, f), sub))
        return out

    def step(self, st, active, reserved, bi, oi):
        """执行一操作，返回触发的 kind 列表（效果照常应用）。"""
        op = self.blocks[bi]["ops"][oi]
        kind_op = op["op"]
        kinds: List[str] = []

        if kind_op == "declare":
            self._set_subtree(st, (op["var"],),
                              OK if op.get("init", False) else UNINIT)
        elif kind_op == "init":
            p = tuple(op["path"])
            a, r = self._overlaps(active, reserved, p, want_shared=True,
                                  want_mut=True, want_reserved=True)
            kinds += ["write_while_borrowed"] * len(a)
            kinds += ["write_while_reserved"] * len(r)
            self._restore_subtree(st, p)
        elif kind_op == "move":
            p = tuple(op["path"])
            kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
            a, r = self._overlaps(active, reserved, p, want_shared=True,
                                  want_mut=True, want_reserved=True)
            kinds += ["move_while_borrowed"] * len(a)
            kinds += ["move_while_reserved"] * len(r)
            self._set_subtree(st, p, MOVED)
        elif kind_op == "borrow_shared":
            p = tuple(op["path"])
            kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
            a, _ = self._overlaps(active, (), p, want_shared=False,
                                  want_mut=True, want_reserved=False)
            kinds += ["borrow_conflict"] * len(a)
            active[op["loan_id"]] = "shared"
        elif kind_op == "borrow_mut":
            p = tuple(op["path"])
            lid = op["loan_id"]
            kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
            if op.get("two_phase", False):
                a, r = self._overlaps(active, reserved, p, want_shared=False,
                                      want_mut=True, want_reserved=True)
                kinds += ["reservation_conflict"] * (len(a) + len(r))
                reserved = set(reserved) | {lid}
            else:
                a, r = self._overlaps(active, reserved, p, want_shared=True,
                                      want_mut=True, want_reserved=True)
                kinds += ["borrow_conflict"] * len(a)
                kinds += ["borrow_while_reserved"] * len(r)
                active[lid] = "mut"
        elif kind_op == "activate":
            lid = op["loan_id"]
            p = self.loan_path[lid]
            if lid in reserved:
                kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
                a, r = self._overlaps(active, reserved, p, want_shared=True,
                                      want_mut=True, want_reserved=True)
                kinds += ["activate_conflict"] * sum(
                    1 for x in [*a, *r] if x != lid)
                reserved = set(reserved) - {lid}
                active[lid] = "mut"
        elif kind_op == "end_loan":
            active.pop(op["loan_id"], None)
            reserved = set(reserved) - {op["loan_id"]}
        elif kind_op == "read":
            p = tuple(op["path"])
            kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
            a, _ = self._overlaps(active, (), p, want_shared=False,
                                  want_mut=True, want_reserved=False)
            kinds += ["read_while_mut_borrowed"] * len(a)
        elif kind_op == "write":
            p = tuple(op["path"])
            a, r = self._overlaps(active, reserved, p, want_shared=True,
                                  want_mut=True, want_reserved=True)
            kinds += ["write_while_borrowed"] * len(a)
            kinds += ["write_while_reserved"] * len(r)
            if op.get("reinit", False):
                self._restore_subtree(st, p)
            else:
                kinds += [BAD_KIND[label] for _q, label in self._unavailable(st, p)]
        elif kind_op == "drop":
            p = tuple(op["path"])
            kinds += [BAD_KIND[label] for _q, label in self._region_unavailable(st, p)]
            a, r = self._overlaps(active, reserved, p, want_shared=True,
                                  want_mut=True, want_reserved=True)
            kinds += ["drop_while_borrowed"] * len(a)
            kinds += ["drop_while_reserved"] * len(r)
            self._set_subtree(st, p, DROPPED)
        elif kind_op == "end_scope":
            scope = op.get("scope", "0")
            for var, ty in reversed(self.scope_decls[scope]):
                root = (var,)
                regions = self._live_regions(st, root, ty)
                hit_a, hit_r = set(), set()
                for reg in regions:
                    a, r = self._overlaps(active, reserved, reg,
                                          want_shared=True, want_mut=True,
                                          want_reserved=True)
                    hit_a.update(a)
                    hit_r.update(r)
                kinds += ["drop_while_borrowed"] * len(hit_a)
                kinds += ["drop_while_reserved"] * len(hit_r)
                self._set_subtree(st, root, DROPPED)
        else:  # pragma: no cover
            raise AssertionError(kind_op)
        return kinds, reserved

    # -- 穷举 --------------------------------------------------------------

    def enumerate_reference(self, *, max_states: int = 200_000
                            ) -> Dict[str, Any]:
        """返回 {status, conflicts: {key: min_back_edges}, evidence}。"""
        conflicts: Dict[ConflictKey, int] = {}
        evidence: Dict[ConflictKey, Tuple[int, Tuple[int, ...]]] = {}
        capped = False
        state_count = 0

        for bits in range(self.size):
            def edge_on(u, v):
                return bool((self.guards.get((u, v), (1 << self.size) - 1) >> bits) & 1)

            # DFS 栈：(块, 回边数, st(dict), active(dict), reserved(set), walk)
            st0 = self.fresh()
            stack: List[Tuple[int, int, Dict, Dict[str, str], set, Tuple[int, ...]]] = [
                (self.entry, 0, st0, {}, set(), (self.entry,))]
            # (块, 具体状态) -> 已探索的最小回边数；更小回边数再次到达要重开，
            # 以保证记录的冲突最小回边距离准确。
            closed: Dict[Tuple[int, Any], int] = {}
            while stack:
                bi, unroll, st, active, reserved, walk = stack.pop()
                key = (bi, self.snap(st, active, frozenset(reserved)))
                prev = closed.get(key)
                if prev is not None and prev <= unroll:
                    continue
                closed[key] = unroll
                state_count += 1
                if state_count > max_states:
                    capped = True
                    break
                block = self.blocks[bi]
                cur_st, cur_act, cur_res = st, active, reserved
                for oi in range(len(block["ops"])):
                    ks, cur_res = self.step(
                        cur_st, cur_act, cur_res, bi, oi)
                    for kd in ks:
                        ck = (kd, bi, oi)
                        if ck not in conflicts or unroll < conflicts[ck]:
                            conflicts[ck] = unroll
                            evidence[ck] = (bits, walk)
                nxts = []
                for v in self.successor_idx[bi]:
                    if edge_on(bi, v):
                        nxts.append(v)
                for v in sorted(nxts, reverse=True):
                    nu = unroll + (1 if (bi, v) in self.back_edges else 0)
                    stack.append((
                        v, nu,
                        {q: cur_st[q] for q in self.paths},
                        dict(cur_act), set(cur_res), (*walk, v)))
            if capped:
                break

        status = "unknown" if capped else ("conflict" if conflicts else "clean")
        return {"status": status, "conflicts": conflicts,
                "evidence": evidence, "state_count": state_count}
