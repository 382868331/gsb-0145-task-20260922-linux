"""v2 具体执行语义（独立于掩码不动点的第二条实现路线）。

本模块给出一个**逐操作的具体解释器**：在给定布尔条件赋值下，沿一条显式
块游走（block walk，允许重复块）执行，维护

* 每个类型树节点的具体可用性：``ok`` / ``uninit`` / ``moved`` / ``dropped``；
* 活跃借用（shared/active-mut）与两阶段预约集合。

它与 :mod:`borrow_check.analyzer_v2` 的掩码 may-不动点是**两条独立路线**：
分析器只产出候选冲突，凡是要作为“确定冲突”输出的，都必须在本解释器上
用某个具体赋值 + 某条可执行游走重放出同一冲突种类——检查器不相信分析器
自己的布尔标志。

另外 :func:`certify` 在赋值固定后做有界游走搜索（回边计数 + 状态去重 +
展开预算），既用于把抽象证据还原为可执行路径，也在预算耗尽时如实产出
``unknown`` 原因，而不是放过或硬报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .model import (
    BorrowMut,
    BorrowShared,
    CFG,
    Decl,
    Drop,
    EndLoan,
    EndScope,
    Activate,
    Init,
    Move,
    Path,
    Read,
    RecordNode,
    Write,
)

# 可用性标签：ok 已初始化；None 尚未声明（支配校验保证访问点不会遇到）。
OK = "ok"
UNINIT = "uninit"
MOVED = "moved"
DROPPED = "dropped"
# *_exact：只压制节点自身（恢复子孙时由子树标签拆分而来）。
EXACT = {"uninit": "uninit_exact", "moved": "moved_exact",
         "dropped": "dropped_exact"}
EXACT_KIND = {"uninit_exact": "use_of_uninitialized",
              "moved_exact": "use_of_moved",
              "dropped_exact": "use_of_dropped"}
# 子树语义标签（祖先位置即压制子孙）。
SUBTREE_LABELS = (UNINIT, MOVED, DROPPED)

# 访问不可用时标签 -> 冲突 kind。
UNAVAILABLE_KIND = {
    UNINIT: "use_of_uninitialized",
    MOVED: "use_of_moved",
    DROPPED: "use_of_dropped",
    **EXACT_KIND,
}


@dataclass(frozen=True)
class ReplayEvent:
    """重放中的一个可打印步骤。"""

    block: int
    op: int
    text: str


@dataclass(frozen=True)
class ReplayResult:
    """一次具体重放的结果。

    :param reproduced: 停止点操作上确实观察到目标冲突 kind。
    :param kinds_at_stop: 停止点操作在本次执行中触发的全部冲突 kind
        （一次操作可能同时触发多类，如 move 已移动且被借用）。
    :param events: 从入口到停止点（含）的逐步事件。
    :param walk_valid: 游走边全部存在且在该赋值下守卫为真。
    """

    reproduced: bool
    kinds_at_stop: FrozenSet[str]
    events: Tuple[ReplayEvent, ...]
    walk_valid: bool


@dataclass(frozen=True)
class Evidence:
    """通过独立重放认证的可执行冲突证据。

    :param assignment: 条件赋值（按 CFG.conditions 顺序）。
    :param assignment_bits: 该赋值的整数编码（第 j 位为第 j 个条件）。
    :param walk: 实际执行的块下标序列（允许重复，含循环）。
    :param stop_block / stop_op: 冲突停止点。
    :param back_edges: 游走经过的回边（目标支配源的边）条数。
    :param events: 逐步重放事件（人类可读）。
    """

    assignment: Tuple[Tuple[str, bool], ...]
    assignment_bits: int
    walk: Tuple[int, ...]
    stop_block: int
    stop_op: int
    back_edges: int
    events: Tuple[ReplayEvent, ...]


@dataclass(frozen=True)
class CertifyOutcome:
    certified: Optional[Evidence]
    reason: Optional[str]
    detail: str


# --------------------------------------------------------------------------
# 类型树辅助
# --------------------------------------------------------------------------


def all_paths_of(types: Dict[str, RecordNode]) -> List[Path]:
    out: List[Path] = []

    def walk(node: RecordNode, prefix: Path) -> None:
        out.append(prefix)
        if isinstance(node, dict):
            for f, sub in node.items():
                walk(sub, (*prefix, f))

    for root, ty in types.items():
        walk(ty, (root,))
    return out


def _is_prefix(a: Path, b: Path) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def _overlaps(a: Path, b: Path) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


# --------------------------------------------------------------------------
# 具体解释器
# --------------------------------------------------------------------------


@dataclass
class _LoanMeta:
    mode: str  # "shared" / "mut"
    path: Path
    two_phase: bool


class ConcreteState:
    """一次具体执行的可变状态。"""

    def __init__(self, all_paths: Sequence[Path]) -> None:
        self.status: Dict[Path, Optional[str]] = {p: None for p in all_paths}
        self.active: Dict[str, str] = {}   # loan_id -> "shared"/"mut"
        self.reserved: set[str] = set()

    def snapshot(self) -> HashableState:
        return HashableState(
            status=tuple(sorted((p, s) for p, s in self.status.items())),
            active=tuple(sorted(self.active.items())),
            reserved=tuple(sorted(self.reserved)),
        )

    def copy(self) -> "ConcreteState":
        other = ConcreteState.__new__(ConcreteState)
        other.status = dict(self.status)
        other.active = dict(self.active)
        other.reserved = set(self.reserved)
        return other


@dataclass(frozen=True)
class HashableState:
    status: Tuple[Tuple[Path, Optional[str]], ...]
    active: Tuple[Tuple[str, str], ...]
    reserved: Tuple[str, ...]


class _Interpreter:
    def __init__(self, cfg: CFG, types: Dict[str, RecordNode],
                 loans: Dict[str, _LoanMeta],
                 scope_vars: Dict[str, List[Tuple[str, RecordNode]]],
                 tree_paths: Sequence[Path]) -> None:
        self.cfg = cfg
        self.types = types
        self.loans = loans
        self.scope_vars = scope_vars
        self.tree_paths = tree_paths

    def fresh(self) -> ConcreteState:
        return ConcreteState(self.tree_paths)

    def restore(self, snap: HashableState) -> ConcreteState:
        st = ConcreteState.__new__(ConcreteState)
        st.status = dict(snap.status)
        st.active = dict(snap.active)
        st.reserved = set(snap.reserved)
        return st

    # -- 区域 --------------------------------------------------------------

    def _set_subtree(self, st: ConcreteState, path: Path, label: str) -> None:
        for q in self.tree_paths:
            if _is_prefix(path, q):
                st.status[q] = label

    def _restore_subtree(self, st: ConcreteState, path: Path) -> None:
        """初始化/重组写：path 子树恢复为 OK；严格祖先的子树标签
        拆分为精确标签（只压祖先自身），兄弟标签保持不动。"""
        for q in self.tree_paths:
            if _is_prefix(path, q):
                st.status[q] = OK
        for depth in range(len(path) - 1):
            q = path[: depth + 1]
            lab = st.status.get(q)
            if lab in SUBTREE_LABELS:
                st.status[q] = EXACT[lab]

    def _unavailable(self, st: ConcreteState, path: Path
                      ) -> List[Tuple[Path, str]]:
        """方向性可用性：子树标签祖先（含自身）压制；精确标签只压自身。

        子孙的状态不使父路径不可用（部分移动的父路径其余字段仍可访问）。
        """
        out: List[Tuple[Path, str]] = []
        for q, label in st.status.items():
            if label in SUBTREE_LABELS and _is_prefix(q, path):
                out.append((q, label))
            elif label in EXACT_KIND and q == path:
                out.append((q, label))
        return out

    def _dead_nodes(self, st: ConcreteState, path: Path
                    ) -> List[Tuple[Path, str]]:
        """path 子树内所有非 OK 节点（显式 drop 的整 place 检查）。"""
        out: List[Tuple[Path, str]] = []
        for q in self.tree_paths:
            if _is_prefix(path, q):
                label = st.status.get(q)
                if label in SUBTREE_LABELS:
                    out.append((q, label))
                elif label in EXACT_KIND:
                    out.append((q, label))
        return out

    def _overlapping(self, st: ConcreteState, path: Path,
                     *, include_shared: bool, include_mut: bool,
                     include_reserved: bool
                     ) -> Tuple[List[str], List[str]]:
        hits_active: List[str] = []
        hits_reserved: List[str] = []
        for lid, mode in st.active.items():
            if not _overlaps(self.loans[lid].path, path):
                continue
            if mode == "shared" and not include_shared:
                continue
            if mode == "mut" and not include_mut:
                continue
            hits_active.append(lid)
        if include_reserved:
            for lid in st.reserved:
                if _overlaps(self.loans[lid].path, path):
                    hits_reserved.append(lid)
        return sorted(hits_active), sorted(hits_reserved)

    # -- 单步：返回该操作触发的冲突 kind 列表（效果照常应用）--------------

    def step(self, st: ConcreteState, bi: int, oi: int, op: object,
             events: List[ReplayEvent]) -> List[str]:
        kinds: List[str] = []

        def ev(text: str) -> None:
            events.append(ReplayEvent(block=bi, op=oi, text=text))

        if isinstance(op, Decl):
            if op.initialized:
                self._set_subtree(st, (op.var,), OK)
                ev(f"declare {op.var}：整块初始化为已初始化")
            else:
                self._set_subtree(st, (op.var,), UNINIT)
                ev(f"declare {op.var}（init=false）：整块未初始化")
            return []

        if isinstance(op, Init):
            active, reserved = self._overlapping(
                st, op.path, include_shared=True, include_mut=True,
                include_reserved=True)
            for lid in active:
                kinds.append("write_while_borrowed")
                ev(f"init {_fmt(op.path)} 与活跃借用 {lid} 重叠")
            for lid in reserved:
                kinds.append("write_while_reserved")
                ev(f"init {_fmt(op.path)} 与预约 {lid} 重叠")
            self._restore_subtree(st, op.path)
            if not kinds:
                ev(f"init {_fmt(op.path)}：子树初始化")
            return kinds

        if isinstance(op, Move):
            for q, label in self._unavailable(st, op.path):
                kinds.append(UNAVAILABLE_KIND[label])
                ev(f"move {_fmt(op.path)}：{_fmt(q)} 状态为 {label}")
            active, reserved = self._overlapping(
                st, op.path, include_shared=True, include_mut=True,
                include_reserved=True)
            for lid in active:
                kinds.append("move_while_borrowed")
                ev(f"move {_fmt(op.path)} 与活跃借用 {lid} 重叠")
            for lid in reserved:
                kinds.append("move_while_reserved")
                ev(f"move {_fmt(op.path)} 与两阶段预约 {lid} 重叠")
            self._set_subtree(st, op.path, MOVED)
            if not kinds:
                ev(f"move {_fmt(op.path)}：子树移出")
            return kinds

        if isinstance(op, BorrowShared):
            for q, label in self._unavailable(st, op.path):
                kinds.append(UNAVAILABLE_KIND[label])
            active, _ = self._overlapping(
                st, op.path, include_shared=False, include_mut=True,
                include_reserved=False)
            for lid in active:
                kinds.append("borrow_conflict")
                ev(f"共享借用 {op.loan_id} 与活跃可变借用 {lid} 重叠")
            # 关键：预约期间允许共享借用（两阶段）。
            st.active[op.loan_id] = "shared"
            ev(f"borrow_shared {op.loan_id} @ {_fmt(op.path)}（与预约可共存）")
            return kinds

        if isinstance(op, BorrowMut):
            for q, label in self._unavailable(st, op.path):
                kinds.append(UNAVAILABLE_KIND[label])
            if op.two_phase:
                active, reserved = self._overlapping(
                    st, op.path, include_shared=False, include_mut=True,
                    include_reserved=True)
                for lid in [*active, *reserved]:
                    kinds.append("reservation_conflict")
                    ev(f"预约 {op.loan_id} 与 {lid} 冲突（活跃mut/其他预约）")
                st.reserved.add(op.loan_id)
                ev(f"borrow_mut {op.loan_id} @ {_fmt(op.path)} 进入预约"
                   f"（重叠共享读取仍允许）")
                return kinds
            active, reserved = self._overlapping(
                st, op.path, include_shared=True, include_mut=True,
                include_reserved=True)
            for lid in active:
                kinds.append("borrow_conflict")
                ev(f"可变借用 {op.loan_id} 与活跃借用 {lid} 重叠")
            for lid in reserved:
                kinds.append("borrow_while_reserved")
                ev(f"可变借用 {op.loan_id} 与预约 {lid} 重叠")
            st.active[op.loan_id] = "mut"
            ev(f"borrow_mut {op.loan_id} @ {_fmt(op.path)}")
            return kinds

        if isinstance(op, Activate):
            lid = op.loan_id
            path = self.loans[lid].path
            if lid not in st.reserved:
                # 重入同一激活点（循环）时该 loan 已激活或已结束：no-op，
                # 不重复做可用性/独占检查（与独立穷举参考的具体语义一致）。
                ev(f"activate {lid}：预约不在（已激活/已结束），空操作")
                return kinds
            for q, label in self._unavailable(st, path):
                kinds.append(UNAVAILABLE_KIND[label])
            active, reserved = self._overlapping(
                st, path, include_shared=True, include_mut=True,
                include_reserved=True)
            for other in [*active, *reserved]:
                if other != lid:
                    kinds.append("activate_conflict")
                    ev(f"激活 {lid} 时仍有重叠借用/预约 {other}")
            st.reserved.discard(lid)
            st.active[lid] = "mut"
            if not any(k == "activate_conflict" for k in kinds):
                ev(f"activate {lid}：预约转为独占可变借用")
            return kinds

        if isinstance(op, EndLoan):
            st.active.pop(op.loan_id, None)
            st.reserved.discard(op.loan_id)
            ev(f"end_loan {op.loan_id}")
            return []

        if isinstance(op, Read):
            for q, label in self._unavailable(st, op.path):
                kinds.append(UNAVAILABLE_KIND[label])
                ev(f"read {_fmt(op.path)}：{_fmt(q)} 状态为 {label}")
            active, _ = self._overlapping(
                st, op.path, include_shared=False, include_mut=True,
                include_reserved=False)
            for lid in active:
                kinds.append("read_while_mut_borrowed")
                ev(f"read {_fmt(op.path)} 与活跃可变借用 {lid} 重叠")
            if not kinds:
                ev(f"read {_fmt(op.path)}（预约期共享读允许）")
            return kinds

        if isinstance(op, Write):
            active, reserved = self._overlapping(
                st, op.path, include_shared=True, include_mut=True,
                include_reserved=True)
            for lid in active:
                kinds.append("write_while_borrowed")
                ev(f"write {_fmt(op.path)} 与活跃借用 {lid} 重叠")
            for lid in reserved:
                kinds.append("write_while_reserved")
                ev(f"write {_fmt(op.path)} 与预约 {lid} 重叠")
            if op.reinit:
                self._restore_subtree(st, op.path)
                ev(f"write {_fmt(op.path)} reinit：子树恢复")
            else:
                for q, label in self._unavailable(st, op.path):
                    kinds.append(UNAVAILABLE_KIND[label])
                if not any(k.startswith("use_of") for k in kinds):
                    ev(f"write {_fmt(op.path)}")
            return kinds

        if isinstance(op, Drop):
            for q, label in self._dead_nodes(st, op.path):
                kinds.append(UNAVAILABLE_KIND[label])
                ev(f"drop {_fmt(op.path)}：{_fmt(q)} 状态为 {label}")
            active, reserved = self._overlapping(
                st, op.path, include_shared=True, include_mut=True,
                include_reserved=True)
            for lid in active:
                kinds.append("drop_while_borrowed")
                ev(f"drop {_fmt(op.path)} 与活跃借用 {lid} 重叠")
            for lid in reserved:
                kinds.append("drop_while_reserved")
                ev(f"drop {_fmt(op.path)} 与两阶段预约 {lid} 重叠")
            self._set_subtree(st, op.path, DROPPED)
            if not any(k in ("drop_while_borrowed", "drop_while_reserved")
                       for k in kinds):
                ev(f"drop {_fmt(op.path)}：析构消费")
            return kinds

        if isinstance(op, EndScope):
            kinds.extend(self._end_scope(st, bi, oi, op.scope, events))
            return kinds

        raise AssertionError(f"未知操作 {op!r}")  # pragma: no cover

    def _end_scope(self, st: ConcreteState, bi: int, oi: int,
                   scope: str, events: List[ReplayEvent]) -> List[str]:
        kinds: List[str] = []
        decls = self.scope_vars[scope]
        for var, ty in reversed(decls):  # 逆声明顺序
            regions = self._live_regions(st, (var,), ty)
            if not regions:
                events.append(ReplayEvent(
                    block=bi, op=oi,
                    text=f"end_scope：变量 {var} 无仍初始化字段，全部跳过"))
                self._set_subtree(st, (var,), DROPPED)
                continue
            # 逐字段记录被跳过的非初始化区域（部分移动 / 未初始化）。
            for q in self._skipped_regions(st, (var,), ty):
                label = st.status.get(q)
                events.append(ReplayEvent(
                    block=bi, op=oi,
                    text=f"end_scope：{_fmt(q)} 状态为 {label}，跳过不析构"))
            # 先按逆字段序记录实际析构访问的仍初始化区域，再报这些
            # 访问与借用/预约的冲突（执行顺序：访问 -> 冲突）。
            for r in reversed(regions):
                events.append(ReplayEvent(
                    block=bi, op=oi,
                    text=f"end_scope：析构仍初始化区域 {_fmt(r)}（变量 {var}）"))
            active, reserved = self._overlapping_regions(st, regions)
            for lid in active:
                kinds.append("drop_while_borrowed")
                events.append(ReplayEvent(
                    block=bi, op=oi,
                    text=f"end_scope：逆序析构 {var} 时与活跃借用 {lid} 重叠"))
            for lid in reserved:
                kinds.append("drop_while_reserved")
                events.append(ReplayEvent(
                    block=bi, op=oi,
                    text=f"end_scope：逆序析构 {var} 时与两阶段预约 {lid} 重叠"))
            self._set_subtree(st, (var,), DROPPED)
        return kinds

    def _live_regions(self, st: ConcreteState, path: Path,
                      node: RecordNode) -> List[Path]:
        """最大“仍初始化”区域：整体已初始化则整块；否则按字段逆序递归。

        词法析构只能访问仍初始化的字段；已移动/未初始化/已析构的区域跳过。
        """
        region_paths = [q for q in self.tree_paths if _is_prefix(path, q)]
        if all(st.status.get(q) == OK for q in region_paths):
            return [path]
        out: List[Path] = []
        if isinstance(node, dict):
            for f, sub in reversed(list(node.items())):
                out.extend(self._live_regions(st, (*path, f), sub))
        return out

    def _skipped_regions(self, st: ConcreteState, path: Path,
                         node: RecordNode) -> List[Path]:
        """最大“非初始化”区域（与 _live_regions 互补，混合时才有意义）。

        整棵子树全部非 OK 时由调用方的“全部跳过”分支处理；这里只在
        存在仍初始化兄弟的前提下，按字段逆序列出被跳过的最大区域。
        """
        region_paths = [q for q in self.tree_paths if _is_prefix(path, q)]
        labels = {st.status.get(q) for q in region_paths}
        if labels == {OK}:
            return []
        if OK not in labels:
            return [path]
        out: List[Path] = []
        if isinstance(node, dict):
            for f, sub in reversed(list(node.items())):
                out.extend(self._skipped_regions(st, (*path, f), sub))
        return out

    def _overlapping_regions(self, st: ConcreteState, regions: List[Path]
                             ) -> Tuple[List[str], List[str]]:
        act: set[str] = set()
        res: set[str] = set()
        for r in regions:
            a, z = self._overlapping(
                st, r, include_shared=True, include_mut=True,
                include_reserved=True)
            act.update(a)
            res.update(z)
        return sorted(act), sorted(res)


def _fmt(path: Path) -> str:
    return ".".join(path)


# --------------------------------------------------------------------------
# 显式游走重放
# --------------------------------------------------------------------------


def assignment_from_bits(cfg: CFG, bits: int) -> Tuple[Tuple[str, bool], ...]:
    return tuple((name, bool((bits >> j) & 1))
                 for j, name in enumerate(cfg.conditions))


def replay_walk(cfg: CFG, types: Dict[str, RecordNode],
                loans: Dict[str, _LoanMeta],
                scope_vars: Dict[str, List[Tuple[str, RecordNode]]],
                tree_paths: Sequence[Path],
                enabled_edges: FrozenSet[Tuple[int, int]],
                assignment_bits: int, walk: Sequence[int],
                stop: Tuple[int, int]) -> ReplayResult:
    """沿显式块游走逐步执行到停止点，返回具体结果。"""
    interp = _Interpreter(cfg, types, loans, scope_vars, tree_paths)
    st = interp.fresh()
    events: List[ReplayEvent] = []
    kinds_at_stop: FrozenSet[str] = frozenset()

    walk_valid = True
    for i in range(1, len(walk)):
        if (walk[i - 1], walk[i]) not in enabled_edges:
            walk_valid = False
            break
    if walk_valid and walk[0] != cfg.entry:
        walk_valid = False
    if not walk_valid:
        return ReplayResult(False, frozenset(), (), False)

    target_block, target_op = stop
    reached = False
    for step_idx, bi in enumerate(walk):
        last = step_idx == len(walk) - 1
        block = cfg.blocks[bi]
        for oi, op in enumerate(block.ops):
            kinds = interp.step(st, bi, oi, op, events)
            if last and bi == target_block and oi == target_op:
                kinds_at_stop = frozenset(kinds)
                reached = True
                break
        if reached:
            break
    return ReplayResult(
        reproduced=reached,
        kinds_at_stop=kinds_at_stop,
        events=tuple(events),
        walk_valid=True,
    )


# --------------------------------------------------------------------------
# 有界认证搜索
# --------------------------------------------------------------------------


def _dominator_back_edges(cfg: CFG) -> FrozenSet[Tuple[int, int]]:
    """目标块支配源块的边视为回边（不依赖守卫，保守计数）。"""
    n = len(cfg.blocks)
    succs = cfg.successors
    preds: Dict[int, List[int]] = {i: [] for i in range(n)}
    for u, vs in enumerate(succs):
        for v in vs:
            preds[v].append(u)
    reach = {cfg.entry}
    queue = [cfg.entry]
    while queue:
        b = queue.pop()
        for v in succs[b]:
            if v not in reach:
                reach.add(v)
                queue.append(v)
    dom: List[set[int]] = [set() for _ in range(n)]
    dom[cfg.entry] = {cfg.entry}
    changed = True
    while changed:
        changed = False
        for b in range(n):
            if b == cfg.entry or b not in reach:
                continue
            inc = [dom[p] for p in preds[b] if p in reach and dom[p]]
            if not inc:
                continue
            new = {b} | set.intersection(*inc)
            if new != dom[b]:
                dom[b] = new
                changed = True
    back: set[Tuple[int, int]] = set()
    for u, vs in enumerate(succs):
        for v in vs:
            if v in dom[u]:
                back.add((u, v))
    return frozenset(back)


def certify(cfg: CFG, types: Dict[str, RecordNode],
            loans: Dict[str, _LoanMeta],
            scope_vars: Dict[str, List[Tuple[str, RecordNode]]],
            tree_paths: Sequence[Path],
            enabled_edges: FrozenSet[Tuple[int, int]],
            assignment_bits: int, stop: Tuple[int, int],
            want_kind: str, *,
            hint_walk: Optional[Sequence[int]] = None,
            max_loop_unrolls: int = 4,
            max_search_states: int = 20_000) -> CertifyOutcome:
    """为候选冲突寻找可执行游走并逐步重放认证。两阶段：

    1. **有界阶段**：按回边计数展开（≤ ``max_loop_unrolls`` 轮），
       对 ``(块, 回边数, 具体状态)`` 去重；命中即在预算内给出可执行证据。
    2. **完备阶段**（阶段 1 未命中时）：在有限具体状态空间上按
       ``(块, 具体状态)`` 记忆化，**不设回边上界**——具体状态空间有限，
       重访即剪枝，搜索必然终止。它用于区分两种“未命中”：

       * 任何深度都触发不了：候选是抽象 may 过粗产生的伪候选，
         返回 ``reason="refuted"``（调用方丢弃，不产生冲突也不 unknown）；
       * 只在 > ``max_loop_unrolls`` 轮回边后触发：真冲突但超出演示预算，
         返回 ``reason="beyond_loop_budget"``（调用方判 unknown）；
       * 状态展开超过 ``max_search_states``：无法判定，
         ``reason="budget_exhausted"``（调用方判 unknown）。
    """
    interp = _Interpreter(cfg, types, loans, scope_vars, tree_paths)
    back_edges = _dominator_back_edges(cfg)
    assignment = assignment_from_bits(cfg, assignment_bits)
    target_block, target_op = stop

    def make_evidence(walk: Sequence[int], unroll: int,
                      events: Sequence[ReplayEvent]) -> Evidence:
        return Evidence(
            assignment=assignment, assignment_bits=assignment_bits,
            walk=tuple(walk), stop_block=target_block,
            stop_op=target_op, back_edges=unroll,
            events=tuple(events))

    def run(walk: Sequence[int]) -> Optional[ReplayResult]:
        res = replay_walk(
            cfg, types, loans, scope_vars, tree_paths, enabled_edges,
            assignment_bits, walk, stop)
        if res.walk_valid and res.reproduced and want_kind in res.kinds_at_stop:
            return res
        return None

    def execute_block(st: ConcreteState, bi: int) -> bool:
        local = st
        hit = False
        for oi, op in enumerate(cfg.blocks[bi].ops):
            kinds = interp.step(local, bi, oi, op, [])
            if bi == target_block and oi == target_op and want_kind in frozenset(kinds):
                hit = True
        return hit

    if hint_walk:
        res = run(hint_walk)
        if res is not None:
            unroll = sum(1 for i in range(1, len(hint_walk))
                         if (hint_walk[i - 1], hint_walk[i]) in back_edges)
            return CertifyOutcome(
                make_evidence(hint_walk, unroll, res.events),
                None, "提示游走重放成功")

    def successors_of(bi: int) -> List[int]:
        return [v for v in cfg.successors[bi] if (bi, v) in enabled_edges]

    # ---- 阶段 1：有界 DFS（回边计数 ≤ max_loop_unrolls）。----
    initial = interp.fresh()
    stack: List[Tuple[int, int, ConcreteState, Tuple[int, ...]]] = [
        (cfg.entry, 0, initial, (cfg.entry,))]
    seen: set = {(cfg.entry, 0, initial.snapshot())}
    expansions = 0
    while stack:
        bi, unroll, st, walk = stack.pop()
        expansions += 1
        if execute_block(st, bi):
            res = run(walk)
            if res is not None:
                return CertifyOutcome(
                    make_evidence(walk, unroll, res.events),
                    None, f"有界搜索第 {expansions} 个展开命中")
        for v in sorted(successors_of(bi), reverse=True):
            nu = unroll + (1 if (bi, v) in back_edges else 0)
            if nu > max_loop_unrolls:
                continue
            nst = st.copy()
            key = (v, nu, nst.snapshot())
            if key in seen:
                continue
            seen.add(key)
            stack.append((v, nu, nst, (*walk, v)))

    # ---- 阶段 2：有限状态空间上的完备记忆化搜索（不设回边上界）。----
    stack2: List[Tuple[int, ConcreteState, int]] = [
        (cfg.entry, interp.fresh(), 0)]
    closed2: Dict[Tuple[int, HashableState], int] = {}
    expansions2 = 0
    min_hit: Optional[int] = None
    while stack2:
        bi, st, unroll = stack2.pop()
        key = (bi, st.snapshot())
        prev = closed2.get(key)
        if prev is not None and prev <= unroll:
            continue
        closed2[key] = unroll
        expansions2 += 1
        if expansions2 > max_search_states:
            return CertifyOutcome(
                None, "budget_exhausted",
                f"完备认证状态预算 {max_search_states} 耗尽（已展开 "
                f"{expansions2}）；候选既未在 {max_loop_unrolls} 轮内证实，"
                f"也无法证伪")
        if execute_block(st, bi):
            if min_hit is None or unroll < min_hit:
                min_hit = unroll
        for v in successors_of(bi):
            nst = st.copy()
            nu = unroll + (1 if (bi, v) in back_edges else 0)
            stack2.append((v, nst, nu))

    if min_hit is not None and min_hit <= max_loop_unrolls:
        # 理论上阶段 1 应已命中；保守起见返回 unknown。
        return CertifyOutcome(
            None, "budget_exhausted",
            f"完备搜索在 {min_hit} 轮命中但有界阶段漏检（展开 {expansions2}）")
    if min_hit is not None:
        return CertifyOutcome(
            None, "beyond_loop_budget",
            f"候选为真但至少需要 {min_hit} 轮回边（预算 {max_loop_unrolls}，"
            f"完备展开 {expansions2}）")
    return CertifyOutcome(
        None, "refuted",
        f"完备搜索（{expansions2} 个可达具体状态）证明该候选在该赋值下"
        f"任何执行都不触发——抽象伪候选")
