"""v2 核心：条件感知的 may-借用检查（掩码不动点）+ 两阶段借用与析构。

与 v1（:mod:`borrow_check.analyzer`，冻结）并列的第二条引擎，由
:func:`borrow_check.check` 在 ``cfg.version == 2`` 时选用。

抽象状态
========
每个块边界维护一份 **条件限定（path-condition-qualified）** 状态：

* ``reach``：该点可到达的条件赋值位掩码（k 个条件 -> 2^k 个赋值，
  第 b 位代表整数编码 b 的赋值）；
* 三类不可用事实（``uninit`` / ``moved`` / ``dropped``），每类各有
  “子树事实”（压制自身及子孙）与“精确事实”（只压制自身，拆分祖先时
  产生），每条事实带一个条件掩码；
* 活跃借用 ``active``（shared/mut）与两阶段**预约** ``reserved``，
  每个 loan 带一个条件掩码。

边守卫把所有掩码与守卫真值表合取；块合流对掩码析取。因此“同一条件的
互斥分支分别移动不同字段”在合流后掩码不相交，读取点不会产生伪冲突——
条件直接参与不动点格，而不是事后筛报错。循环不动点与两阶段激活同样在
掩码格上迭代（预约沿回边传播、激活按独占规则检查）。

健全性流程
==========
掩码不动点只产出**候选冲突**及其条件掩码。每个候选必须由
:mod:`borrow_check.replay` 的独立具体解释器在某个赋值 + 某条有界
可执行块游走上**逐步重放**出来，才升级为确定冲突；重放预算（默认
4 轮回边）耗尽的候选只能让结果进入 ``unknown``——既不放过也不假报。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .errors import BudgetExhaustedError
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
    Operation,
    Path,
    RecordNode,
    Read,
    Write,
)
from . import replay as _replay
from .replay import (
    CertifyOutcome,
    Evidence,
    ReplayEvent,
    _LoanMeta,
    all_paths_of,
)

UNINIT = "uninit"
MOVED = "moved"
DROPPED = "dropped"
_LABELS = (UNINIT, MOVED, DROPPED)
# 正向 may 标签：节点“可能仍初始化”。与坏标签独立累积（同一赋值在不同
# 游走可能既可能 OK 又可能已移动），用于析构冲突的健全近似——坏标签的
# 方向性规则处理读/借，OK 标签处理“析构只访问仍初始化字段”。
OKLABEL = "__ok__"
_ALL_LABELS = (UNINIT, MOVED, DROPPED, OKLABEL)
_LABEL_KIND = {
    UNINIT: "use_of_uninitialized",
    MOVED: "use_of_moved",
    DROPPED: "use_of_dropped",
}

DEFAULT_MAX_LOOP_UNROLLS = 4
DEFAULT_MAX_CERT_STATES = 20_000


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WitnessV2:
    """抽象前驱见证（仅用于展示与认证提示；真凭实据看 Evidence）。"""

    kind: str
    origin_block: int
    origin_op: int
    block_chain: Tuple[int, ...]
    path: Optional[Path]
    loan_id: Optional[str] = None
    mode: Optional[str] = None
    condition_mask: int = 0


@dataclass(frozen=True)
class ConflictV2:
    """通过独立重放认证的确定冲突。"""

    kind: str
    block: int
    op: int
    op_name: str
    path: Optional[Path]
    detail: str
    witnesses: Tuple[WitnessV2, ...]
    evidence: Evidence


@dataclass(frozen=True)
class UnknownReason:
    kind: str
    block: int
    op: int
    mask: int
    reason: str
    detail: str


@dataclass
class CheckResultV2:
    """v2 检查结果。

    :param status: ``"clean"``（全部可执行路径均无冲突）/
        ``"conflict"``（存在经重放认证的确定冲突，且没有预算缺口）/
        ``"unknown"``（存在无法在预算内证实或排除的候选）。
        注意 ``unknown`` 与 ``clean`` 严格互斥。
    """

    status: str
    conflicts: List[ConflictV2]
    unknown_reasons: List[UnknownReason]
    iterations: int
    conditions: Tuple[str, ...]
    budgets: Dict[str, int]
    refuted_candidates: int = 0

    @property
    def clean(self) -> bool:
        return self.status == "clean"

    def explain(self) -> str:
        return "\n\n".join(_render_conflict(c) for c in self.conflicts)


# --------------------------------------------------------------------------
# 见证与事实
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _W:
    """内部见证：诞生点 + 块链。"""

    kind: str
    origin_block: int
    origin_op: int
    chain: Tuple[int, ...]
    path: Optional[Path]
    loan_id: Optional[str] = None
    mode: Optional[str] = None


def _born(kind: str, bi: int, oi: int, path: Optional[Path],
          loan_id: Optional[str] = None, mode: Optional[str] = None) -> _W:
    return _W(kind, bi, oi, (bi,), path, loan_id, mode)


def _lift_w(w: _W, nxt: int) -> _W:
    if nxt in w.chain:
        chain = w.chain[: w.chain.index(nxt) + 1]
    else:
        chain = (*w.chain, nxt)
    return _W(w.kind, w.origin_block, w.origin_op, chain, w.path,
              w.loan_id, w.mode)


@dataclass
class _Pair:
    """一个标签的（子树事实, 精确事实），键 -> (掩码, 见证)。"""

    sub: Dict[Path, List[object]] = field(default_factory=dict)
    exact: Dict[Path, List[object]] = field(default_factory=dict)

    def copy(self) -> "_Pair":
        p = _Pair()
        p.sub = {k: [v[0], v[1]] for k, v in self.sub.items()}
        p.exact = {k: [v[0], v[1]] for k, v in self.exact.items()}
        return p


@dataclass
class _State2:
    reach: int = 0
    pairs: Dict[str, _Pair] = field(default_factory=dict)
    active: Dict[str, List[object]] = field(default_factory=dict)   # [mask, _W, mode]
    reserved: Dict[str, List[object]] = field(default_factory=dict)  # [mask, _W]

    def copy(self) -> "_State2":
        s = _State2()
        s.reach = self.reach
        s.pairs = {k: v.copy() for k, v in self.pairs.items()}
        s.active = {k: [v[0], v[1], v[2]] for k, v in self.active.items()}
        s.reserved = {k: [v[0], v[1]] for k, v in self.reserved.items()}
        return s


# --------------------------------------------------------------------------
# 路径辅助
# --------------------------------------------------------------------------


def _is_prefix(a: Path, b: Path) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def _overlaps(a: Path, b: Path) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


def _node_at(types: Dict[str, RecordNode], path: Path) -> RecordNode:
    node: RecordNode = types.get(path[0], None)
    for seg in path[1:]:
        assert isinstance(node, dict)
        node = node[seg]
    return node


# --------------------------------------------------------------------------
# 标签事实的掩码格操作
# --------------------------------------------------------------------------


def _normalize(pair: _Pair) -> None:
    """子树反链化（掩码版）：子孙事实被祖先掩码覆盖的部分扣除；
    精确事实被任何子树祖先掩码覆盖的部分扣除。"""
    keys = sorted(pair.sub, key=lambda p: (len(p), p))
    for d in keys:
        if d not in pair.sub:
            continue
        anc = 0
        for m in keys:
            if m == d or m not in pair.sub:
                continue
            if _is_prefix(m, d):
                anc |= pair.sub[m][0]
        if anc:
            pair.sub[d][0] &= ~anc
            if pair.sub[d][0] == 0:
                del pair.sub[d]
    for q in [k for k in pair.exact]:
        cover = 0
        for m, v in pair.sub.items():
            if _is_prefix(m, q):
                cover |= v[0]
        if cover:
            pair.exact[q][0] &= ~cover
            if pair.exact[q][0] == 0:
                del pair.exact[q]


def _pair_add_subtree(pair: _Pair, path: Path, mask: int, w: _W) -> None:
    """在 mask 条件下加入 path 子树事实。"""
    if mask == 0:
        return
    # 已被（严格或相等的）祖先子树事实覆盖的部分不需要新键。
    covered = 0
    for m, v in pair.sub.items():
        if _is_prefix(m, path):
            covered |= v[0]
    effective = mask & ~covered
    # 新祖先覆盖子孙子树事实与子树内精确事实。
    for d in [k for k in pair.sub if k != path and _is_prefix(path, k)]:
        pair.sub[d][0] &= ~mask
        if pair.sub[d][0] == 0:
            del pair.sub[d]
    for q in [k for k in pair.exact if _is_prefix(path, k)]:
        pair.exact[q][0] &= ~mask
        if pair.exact[q][0] == 0:
            del pair.exact[q]
    if effective:
        if path in pair.sub:
            pair.sub[path][0] |= effective
        else:
            pair.sub[path] = [effective, w]
    _normalize(pair)


def _pair_restore(pair: _Pair, path: Path, mask: int,
                  types: Dict[str, RecordNode]) -> None:
    """在 mask 条件下把 path 子树恢复为可用（清除该标签事实）。

    严格祖先子树事实在交集条件上拆分：祖先本身转为精确事实，沿途兄弟
    字段转为子树事实（v1 同一规则的掩码版）。
    """
    if mask == 0:
        return
    new_sub: Dict[Path, List[object]] = {}
    for m, v in pair.sub.items():
        mw, w = v
        if m == path or _is_prefix(path, m):  # 子树内：扣除恢复条件
            resid = mw & ~mask
            if resid:
                new_sub[m] = [resid, w]
        elif _is_prefix(m, path):  # 严格祖先：拆分
            split = mw & mask
            resid = mw & ~mask
            if resid:
                new_sub[m] = [resid, w]
            if split:
                # 具体语义：恢复路径上从祖先 m 到 path 的每个前缀节点
                # 都变成“只压自身”的精确标签（含 m，不含 path——path
                # 自身已恢复）；沿途兄弟字段保持子树标签。
                for depth in range(len(m), len(path)):
                    prefix = path[:depth]
                    if depth > len(m):
                        old_pe = pair.exact.get(prefix)
                        pair.exact[prefix] = [
                            (old_pe[0] if old_pe else 0) | split,
                            old_pe[1] if old_pe else w]
                    node = _node_at(types, prefix)
                    if isinstance(node, dict):
                        nxt = path[depth]
                        for f in node:
                            if f != nxt:
                                sp = (*prefix, f)
                                if sp in new_sub:
                                    new_sub[sp][0] |= split
                                else:
                                    new_sub[sp] = [split, w]
                # 祖先 m 本身的精确事实。
                old_exact = pair.exact.get(m)
                pair.exact[m] = [(old_exact[0] if old_exact else 0) | split,
                                old_exact[1] if old_exact else w]
        else:
            new_sub[m] = [mw, w]
    pair.sub = new_sub
    for q in [k for k in pair.exact if _is_prefix(path, k)]:
        pair.exact[q][0] &= ~mask
        if pair.exact[q][0] == 0:
            del pair.exact[q]
    _normalize(pair)


def _pair_erase_subtree(pair: _Pair, path: Path, mask: int) -> None:
    """move/drop 覆盖：擦除 path 子树内（含 path）本标签事实，严格祖先保留。

    严格祖先子树事实保持不拆分（与 :func:`_pair_restore` 的区别）：
    覆盖操作不恢复兄弟区域。
    """
    for m in [k for k in pair.sub if _is_prefix(path, k)]:
        pair.sub[m][0] &= ~mask
        if pair.sub[m][0] == 0:
            del pair.sub[m]
    for q in [k for k in pair.exact if _is_prefix(path, k)]:
        pair.exact[q][0] &= ~mask
        if pair.exact[q][0] == 0:
            del pair.exact[q]
    _normalize(pair)


def _pair_unavailable(pair: _Pair, path: Path) -> int:
    mask = 0
    for m, v in pair.sub.items():
        if _is_prefix(m, path):
            mask |= v[0]
    if path in pair.exact:
        mask |= pair.exact[path][0]
    return mask


# --------------------------------------------------------------------------
# 块内传输
# --------------------------------------------------------------------------


@dataclass
class _Candidate:
    kind: str
    block: int
    op: int
    mask: int
    detail: str
    witnesses: List[_W]


class _Transfer2:
    def __init__(self, cfg: CFG, types: Dict[str, RecordNode],
                 loan_info: Dict[str, _LoanMeta],
                 scope_vars: Dict[str, List[Tuple[str, RecordNode]]],
                 tree_paths: Sequence[Path]) -> None:
        self.cfg = cfg
        self.types = types
        self.loan_info = loan_info
        self.scope_vars = scope_vars
        self.tree_paths = tree_paths
        self.candidates: List[_Candidate] = []

    def _emit(self, kind: str, bi: int, oi: int, mask: int,
              detail: str, witnesses: Sequence[_W]) -> None:
        if mask == 0:
            return
        # 同 (kind, 块, 操作) 的触发合并掩码与见证。
        for c in self.candidates:
            if c.kind == kind and c.block == bi and c.op == oi:
                c.mask |= mask
                known = {id(w) for w in c.witnesses}
                c.witnesses.extend(w for w in witnesses if id(w) not in known)
                return
        self.candidates.append(
            _Candidate(kind, bi, oi, mask, detail, list(witnesses)))

    def _unavail(self, st: _State2, path: Path
                 ) -> List[Tuple[str, int, List[_W]]]:
        out: List[Tuple[str, int, List[_W]]] = []
        for label in _LABELS:
            pair = st.pairs.get(label)
            if pair is None:
                continue
            hits: List[_W] = []
            mask = 0
            for m, v in pair.sub.items():
                if _is_prefix(m, path):
                    mask |= v[0]
                    hits.append(v[1])
            if path in pair.exact:
                mask |= pair.exact[path][0]
                hits.append(pair.exact[path][1])
            if mask:
                out.append((label, mask, hits))
        return out

    def _loans_overlap(self, st: _State2, path: Path, *,
                       shared: bool, mut: bool, reserved: bool
                       ) -> Tuple[List[Tuple[str, int, _W, str]],
                                  List[Tuple[str, int, _W]]]:
        act: List[Tuple[str, int, _W, str]] = []
        res: List[Tuple[str, int, _W]] = []
        for lid, v in st.active.items():
            amask, w, mode = v
            if (mode == "shared" and not shared) or (mode == "mut" and not mut):
                continue
            if _overlaps(self.loan_info[lid].path, path):
                act.append((lid, amask, w, mode))
        if reserved:
            for lid, v in st.reserved.items():
                if _overlaps(self.loan_info[lid].path, path):
                    res.append((lid, v[0], v[1]))
        act.sort(key=lambda x: x[0])
        res.sort(key=lambda x: x[0])
        return act, res

    def _region_bad(self, st: _State2, path: Path
                    ) -> List[Tuple[str, int, List[_W]]]:
        """path **整个区域**不可用的条件掩码（显式 drop 用）。

        与 :meth:`_unavail` 的方向性规则不同：析构要遍历整个 place，
        子路径被移动/未初始化也算——sub 事实与 path 对称重叠即命中，
        exact 事实落在 path 子树内即命中。
        """
        out: List[Tuple[str, int, List[_W]]] = []
        for label in _LABELS:
            pair = st.pairs.get(label)
            if pair is None:
                continue
            mask = 0
            hits: List[_W] = []
            for m, v in pair.sub.items():
                if _overlaps(m, path):
                    mask |= v[0]
                    hits.append(v[1])
            for q, v in pair.exact.items():
                if _is_prefix(path, q):
                    mask |= v[0]
                    hits.append(v[1])
            if mask:
                out.append((label, mask, hits))
        return out

    def _overwrite_effect(self, st: _State2, path: Path, new_label: str,
                          mask: int, w: _W) -> None:
        """move / drop / declare / end_scope 的节点子树覆盖效果。

        path 子树在 mask 条件下获得 new_label；其他标签在子树**内部**的
        事实擦除（具体解释器对子树节点重打标签），但其他标签在 path
        **严格祖先**处的子树事实保留且不拆分（祖先节点的标签仍在，
        读子孙时会与新标签一起被报告，与具体解释器“错误操作效果照常
        应用、完整枚举后续冲突”的口径一致）。
        """
        st.pairs.setdefault(new_label, _Pair())
        _pair_add_subtree(st.pairs[new_label], path, mask, w)
        for other in _ALL_LABELS:
            if other == new_label:
                continue
            pair = st.pairs.get(other)
            if pair is not None:
                _pair_erase_subtree(pair, path, mask)

    def _restore_effect(self, st: _State2, path: Path, mask: int,
                        w: Optional[_W] = None) -> None:
        """init / reinit：path 子树恢复为可能 OK，严格祖先拆分为精确。"""
        st.pairs.setdefault(OKLABEL, _Pair())
        _pair_add_subtree(st.pairs[OKLABEL], path, mask,
                          w or _born("ok", 0, 0, path))
        for other in _ALL_LABELS:
            if other == OKLABEL:
                continue
            pair = st.pairs.get(other)
            if pair is not None:
                _pair_restore(pair, path, mask, self.types)

    def _ok_mask(self, st: _State2, q: Path) -> int:
        """q 可能仍初始化的条件掩码（方向性：OK 祖先压制子孙，
        精确 OK 只算自身）。"""
        pair = st.pairs.get(OKLABEL)
        if pair is None:
            return 0
        return _pair_unavailable(pair, q)

    def run_block(self, bi: int, st: _State2) -> None:
        block = self.cfg.blocks[bi]
        for oi, op in enumerate(block.ops):
            self._step(st, bi, oi, op)

    def _step(self, st: _State2, bi: int, oi: int, op: Operation) -> None:
        reach = st.reach
        if isinstance(op, Decl):
            path = (op.var,)
            label = OKLABEL if op.initialized else UNINIT
            self._overwrite_effect(st, path, label, reach,
                               _born(label, bi, oi, path))
            return

        if isinstance(op, Init):
            act, res = self._loans_overlap(
                st, op.path, shared=True, mut=True, reserved=True)
            for lid, m, w, mode in act:
                self._emit("write_while_borrowed", bi, oi, m,
                           f"init {_fmt(op.path)} 与活跃{mode}借用 {lid} 重叠", [w])
            for lid, m, w in res:
                self._emit("write_while_reserved", bi, oi, m,
                           f"init {_fmt(op.path)} 与两阶段预约 {lid} 重叠", [w])
            self._restore_effect(st, op.path, reach)
            return

        if isinstance(op, Move):
            for label, mask, ws in self._unavail(st, op.path):
                self._emit(_LABEL_KIND[label], bi, oi, mask,
                           f"move {_fmt(op.path)} 命中 {label} 状态", ws)
            act, res = self._loans_overlap(
                st, op.path, shared=True, mut=True, reserved=True)
            for lid, m, w, _mode in act:
                self._emit("move_while_borrowed", bi, oi, m,
                           f"move {_fmt(op.path)} 与活跃借用 {lid} 重叠", [w])
            for lid, m, w in res:
                self._emit("move_while_reserved", bi, oi, m,
                           f"move {_fmt(op.path)} 与两阶段预约 {lid} 重叠", [w])
            self._overwrite_effect(
                st, op.path, MOVED, reach, _born("moved", bi, oi, op.path))
            return

        if isinstance(op, BorrowShared):
            for label, mask, ws in self._unavail(st, op.path):
                self._emit(_LABEL_KIND[label], bi, oi, mask,
                           f"共享借用 {op.loan_id} 命中 {label} 状态", ws)
            act, _ = self._loans_overlap(
                st, op.path, shared=False, mut=True, reserved=False)
            for lid, m, w, _mode in act:
                self._emit("borrow_conflict", bi, oi, m,
                           f"共享借用 {op.loan_id} 与活跃可变借用 {lid} 重叠", [w])
            # 预约期允许共享借用：不检查 reserved。
            st.active[op.loan_id] = [
                reach, _born("loan", bi, oi, op.path, op.loan_id, "shared"),
                "shared"]
            return

        if isinstance(op, BorrowMut):
            for label, mask, ws in self._unavail(st, op.path):
                self._emit(_LABEL_KIND[label], bi, oi, mask,
                           f"可变借用 {op.loan_id} 命中 {label} 状态", ws)
            if op.two_phase:
                act, res = self._loans_overlap(
                    st, op.path, shared=False, mut=True, reserved=True)
                for lid, m, w, _mode in act:
                    self._emit("reservation_conflict", bi, oi, m,
                               f"预约 {op.loan_id} 与活跃可变借用 {lid} 重叠", [w])
                for lid, m, w in res:
                    self._emit("reservation_conflict", bi, oi, m,
                               f"预约 {op.loan_id} 与既有预约 {lid} 重叠", [w])
                st.reserved[op.loan_id] = [
                    reach, _born("reserve", bi, oi, op.path, op.loan_id)]
                return
            act, res = self._loans_overlap(
                st, op.path, shared=True, mut=True, reserved=True)
            for lid, m, w, _mode in act:
                self._emit("borrow_conflict", bi, oi, m,
                           f"可变借用 {op.loan_id} 与活跃借用 {lid} 重叠", [w])
            for lid, m, w in res:
                self._emit("borrow_while_reserved", bi, oi, m,
                           f"可变借用 {op.loan_id} 与预约 {lid} 重叠", [w])
            st.active[op.loan_id] = [
                reach, _born("loan", bi, oi, op.path, op.loan_id, "mut"), "mut"]
            return

        if isinstance(op, Activate):
            lid = op.loan_id
            path = self.loan_info[lid].path
            rv = st.reserved.get(lid)
            rmask = rv[0] if rv else 0
            if rmask:
                for label, mask, ws in self._unavail_mask(st, path, rmask):
                    self._emit(_LABEL_KIND[label], bi, oi, mask,
                               f"激活 {lid} 命中 {label} 状态", ws)
                act, res = self._loans_overlap(
                    st, path, shared=True, mut=True, reserved=True)
                for olid, m, w, _mode in act:
                    if olid != lid:
                        self._emit("activate_conflict", bi, oi, m & rmask,
                                   f"激活 {lid} 时仍有重叠活跃借用 {olid}", [w])
                for olid, m, w in res:
                    if olid != lid:
                        self._emit("activate_conflict", bi, oi, m & rmask,
                                   f"激活 {lid} 时仍有重叠预约 {olid}", [w])
                # 效果：预约 -> 独占（仅在 rmask 条件上）。
                del st.reserved[lid]
                if lid in st.active:
                    st.active[lid][0] |= rmask
                else:
                    st.active[lid] = [
                        rmask, _born("loan", bi, oi, path, lid, "mut"), "mut"]
            return

        if isinstance(op, EndLoan):
            st.active.pop(op.loan_id, None)
            st.reserved.pop(op.loan_id, None)
            return

        if isinstance(op, Read):
            for label, mask, ws in self._unavail(st, op.path):
                self._emit(_LABEL_KIND[label], bi, oi, mask,
                           f"read {_fmt(op.path)} 命中 {label} 状态", ws)
            act, _ = self._loans_overlap(
                st, op.path, shared=False, mut=True, reserved=False)
            for lid, m, w, _mode in act:
                self._emit("read_while_mut_borrowed", bi, oi, m,
                           f"read {_fmt(op.path)} 与活跃可变借用 {lid} 重叠", [w])
            return

        if isinstance(op, Write):
            act, res = self._loans_overlap(
                st, op.path, shared=True, mut=True, reserved=True)
            for lid, m, w, _mode in act:
                self._emit("write_while_borrowed", bi, oi, m,
                           f"write {_fmt(op.path)} 与活跃借用 {lid} 重叠", [w])
            for lid, m, w in res:
                self._emit("write_while_reserved", bi, oi, m,
                           f"write {_fmt(op.path)} 与两阶段预约 {lid} 重叠", [w])
            if op.reinit:
                self._restore_effect(st, op.path, reach)
            else:
                for label, mask, ws in self._unavail(st, op.path):
                    self._emit(_LABEL_KIND[label], bi, oi, mask,
                               f"write {_fmt(op.path)} 命中 {label} 状态"
                               f"（恢复性写入需 reinit=true 或 init）", ws)
            return

        if isinstance(op, Drop):
            for label, mask, ws in self._region_bad(st, op.path):
                self._emit(_LABEL_KIND[label], bi, oi, mask,
                           f"drop {_fmt(op.path)} 要求整个 place 可用，"
                           f"但区域内命中 {label} 状态", ws)
            act, res = self._loans_overlap(
                st, op.path, shared=True, mut=True, reserved=True)
            for lid, m, w, _mode in act:
                self._emit("drop_while_borrowed", bi, oi, m,
                           f"drop {_fmt(op.path)} 与活跃借用 {lid} 重叠", [w])
            for lid, m, w in res:
                self._emit("drop_while_reserved", bi, oi, m,
                           f"drop {_fmt(op.path)} 与两阶段预约 {lid} 重叠", [w])
            self._overwrite_effect(
                st, op.path, DROPPED, reach,
                _born("dropped", bi, oi, op.path))
            return

        if isinstance(op, EndScope):
            self._end_scope(st, bi, oi, op.scope)
            return

    def _unavail_mask(self, st: _State2, path: Path, rmask: int
                      ) -> List[Tuple[str, int, List[_W]]]:
        out = []
        for label, mask, ws in self._unavail(st, path):
            m = mask & rmask
            if m:
                out.append((label, m, ws))
        return out

    def _end_scope(self, st: _State2, bi: int, oi: int, scope: str) -> None:
        decls = self.scope_vars[scope]
        for var, _ty in reversed(decls):
            root = (var,)
            subtree = [q for q in self.tree_paths if _is_prefix(root, q)]
            # 每个节点“可能仍初始化”的条件掩码；析构访问恰覆盖仍初始化区域，
            # 与借用冲突当且仅当区域中某个可能 OK 的节点与借用路径重叠。
            act, res = self._loans_overlap(
                st, root, shared=True, mut=True, reserved=True)
            for lid, _m, w, _mode in act:
                lpath = self.loan_info[lid].path
                m = 0
                for q in subtree:
                    if _overlaps(q, lpath):
                        m |= self._ok_mask(st, q)
                self._emit("drop_while_borrowed", bi, oi, m,
                           f"end_scope 逆序析构 {var} 时与活跃借用 {lid} 重叠",
                           [w])
            for lid, _m, w in res:
                lpath = self.loan_info[lid].path
                m = 0
                for q in subtree:
                    if _overlaps(q, lpath):
                        m |= self._ok_mask(st, q)
                self._emit("drop_while_reserved", bi, oi, m,
                           f"end_scope 逆序析构 {var} 时与两阶段预约 {lid} 重叠",
                           [w])
            # 效果：整个变量在可达条件上视为已析构（具体解释器无条件置 DROPPED）。
            self._overwrite_effect(
                st, root, DROPPED, st.reach, _born("dropped", bi, oi, root))


def _fmt(path: Path) -> str:
    return ".".join(path)


# --------------------------------------------------------------------------
# 状态提升与合流
# --------------------------------------------------------------------------


def _lift2(st: _State2, guard_mask: int, nxt: int) -> _State2:
    """沿守卫为 guard_mask 的边传播：全部条件掩码与守卫合取，链追加 nxt。"""
    out = _State2()
    out.reach = st.reach & guard_mask
    if out.reach == 0:
        return out
    for label, pair in st.pairs.items():
        np = _Pair()
        for k, (m, w) in pair.sub.items():
            m2 = m & guard_mask
            if m2:
                np.sub[k] = [m2, _lift_w(w, nxt)]
        for k, (m, w) in pair.exact.items():
            m2 = m & guard_mask
            if m2:
                np.exact[k] = [m2, _lift_w(w, nxt)]
        if np.sub or np.exact:
            out.pairs[label] = np
    for lid, (m, w, mode) in st.active.items():
        m2 = m & guard_mask
        if m2:
            out.active[lid] = [m2, _lift_w(w, nxt), mode]
    for lid, (m, w) in st.reserved.items():
        m2 = m & guard_mask
        if m2:
            out.reserved[lid] = [m2, _lift_w(w, nxt)]
    return out


def _join_pair(into: _Pair, other: _Pair) -> None:
    for k, (m, w) in other.sub.items():
        if k in into.sub:
            into.sub[k][0] |= m
        else:
            into.sub[k] = [m, w]
    for k, (m, w) in other.exact.items():
        if k in into.exact:
            into.exact[k][0] |= m
        else:
            into.exact[k] = [m, w]
    _normalize(into)


def _join2(into: _State2, other: _State2) -> None:
    into.reach |= other.reach
    for label, pair in other.pairs.items():
        if label not in into.pairs:
            into.pairs[label] = pair.copy()
        else:
            _join_pair(into.pairs[label], pair)
    for lid, (m, w, mode) in other.active.items():
        if lid in into.active:
            into.active[lid][0] |= m
        else:
            into.active[lid] = [m, w, mode]
    for lid, (m, w) in other.reserved.items():
        if lid in into.reserved:
            into.reserved[lid][0] |= m
        else:
            into.reserved[lid] = [m, w]


def _signature(st: _State2) -> object:
    parts = [("reach", st.reach)]
    for label in _ALL_LABELS:
        pair = st.pairs.get(label)
        if pair:
            parts.append((label, "s", sorted((k, v[0]) for k, v in pair.sub.items())))
            parts.append((label, "e", sorted((k, v[0]) for k, v in pair.exact.items())))
    parts.append(("A", sorted((k, v[0], v[2]) for k, v in st.active.items())))
    parts.append(("R", sorted((k, v[0]) for k, v in st.reserved.items())))
    return tuple(parts)


# --------------------------------------------------------------------------
# v2 检查入口
# --------------------------------------------------------------------------


def check_v2(cfg: CFG, *, max_iterations: Optional[int] = None,
             max_loop_unrolls: int = DEFAULT_MAX_LOOP_UNROLLS,
             max_cert_states: int = DEFAULT_MAX_CERT_STATES
             ) -> CheckResultV2:
    from .errors import checked_int
    from .validator import validate_cfg

    validate_cfg(cfg)
    if max_iterations is not None:
        checked_int("max_iterations", max_iterations, min_value=1)
    checked_int("max_loop_unrolls", max_loop_unrolls, min_value=0)
    checked_int("max_cert_states", max_cert_states, min_value=1)

    n = len(cfg.blocks)
    k = len(cfg.conditions)
    # 赋值空间 2^k 个赋值，真值表掩码占 2^k 位。
    full = (1 << (1 << k)) - 1

    # 收集类型、loan、作用域。
    types: Dict[str, RecordNode] = {}
    loan_info: Dict[str, _LoanMeta] = {}
    scope_vars: Dict[str, List[Tuple[str, RecordNode]]] = {}
    for bi, block in enumerate(cfg.blocks):
        for oi, op in enumerate(block.ops):
            if isinstance(op, Decl):
                types[op.var] = op.ty
                scope_vars.setdefault(op.scope, []).append((op.var, op.ty))
            elif isinstance(op, BorrowMut):
                loan_info[op.loan_id] = _LoanMeta(
                    "mut", op.path, op.two_phase)
            elif isinstance(op, BorrowShared):
                loan_info[op.loan_id] = _LoanMeta("shared", op.path, False)
    tree_paths = all_paths_of(types)

    # 边守卫真值表。
    cond_index = {name: i for i, name in enumerate(cfg.conditions)}
    edge_masks: Dict[Tuple[int, int], int] = {}
    for (u, v), formula in cfg.guards.items():
        edge_masks[(u, v)] = formula.compile(cond_index)

    def guard_of(u: int, v: int) -> int:
        return edge_masks.get((u, v), full)

    # 预算上界：每条边、每个赋值位、每类事实键至多增长一次。
    p_count = len(tree_paths)
    l_count = len(loan_info)
    edge_count = sum(len(s) for s in cfg.successors)
    bits = 1 << k
    bound = edge_count * bits * (6 * p_count + 2 * l_count) + n
    budget = bound if max_iterations is None else max_iterations

    predecessors: Dict[int, List[int]] = {i: [] for i in range(n)}
    for bi, succs in enumerate(cfg.successors):
        for s in succs:
            predecessors[s].append(bi)
    for preds in predecessors.values():
        preds.sort()

    ins: List[_State2] = [_State2() for _ in range(n)]
    outs: Dict[int, _State2] = {}
    sigs: Dict[int, object] = {}
    iterations = 0

    def process(idx: int) -> bool:
        nonlocal iterations
        iterations += 1
        if iterations > budget:
            raise BudgetExhaustedError(
                f"v2 不动点搜索预算耗尽：已处理 {budget} 个块次仍未收敛"
                f"（可证明上界为 {bound}）。这不是 clean 结论。")
        merged = _State2()
        if idx == cfg.entry:
            merged.reach = full  # 入口在所有赋值下可达
        for p in predecessors[idx]:
            if p in outs:
                _join2(merged, _lift2(outs[p], guard_of(p, idx), idx))
        ins[idx] = merged
        local = merged.copy()
        transfer = _Transfer2(cfg, types, loan_info, scope_vars, tree_paths)
        transfer.run_block(idx, local)
        outs[idx] = local
        sig = _signature(local)
        changed = sigs.get(idx) != sig
        sigs[idx] = sig
        return changed

    worklist: deque[int] = deque(range(n))
    seen: set[int] = set(range(n))
    while worklist:
        idx = worklist.popleft()
        seen.discard(idx)
        if process(idx):
            for s in cfg.successors[idx]:
                if s not in seen:
                    worklist.append(s)
                    seen.add(s)

    # 在不动点入口状态上有序收集候选。v2 只分析可达块：每条确定冲突必须
    # 能还原到从入口出发的可执行游走；不可达块在任何赋值下都不可达，
    # 没有可执行输入，故不产冲突（结构合法性仍由 validator 把关）。
    candidates: List[_Candidate] = []
    for bi in range(n):
        entry_state = ins[bi]
        if entry_state.reach == 0 and bi != cfg.entry:
            continue
        transfer = _Transfer2(cfg, types, loan_info, scope_vars, tree_paths)
        transfer.run_block(bi, entry_state.copy())
        candidates.extend(transfer.candidates)

    candidates.sort(key=lambda c: (c.block, c.op, c.kind))

    # 每个赋值的可用边集合（惰性缓存）。
    enabled_cache: Dict[int, FrozenSet[Tuple[int, int]]] = {}

    def enabled_for(bits_val: int) -> FrozenSet[Tuple[int, int]]:
        if bits_val not in enabled_cache:
            es = set()
            for u, succs in enumerate(cfg.successors):
                for v in succs:
                    if (guard_of(u, v) >> bits_val) & 1:
                        es.add((u, v))
            enabled_cache[bits_val] = frozenset(es)
        return enabled_cache[bits_val]

    conflicts: List[ConflictV2] = []
    unknowns: List[UnknownReason] = []
    refuted_count = [0]

    def make_evidence_witnesses(c: _Candidate) -> Tuple[WitnessV2, ...]:
        out_w: List[WitnessV2] = []
        seen_ids: set[int] = set()
        for w in c.witnesses:
            if id(w) in seen_ids:
                continue
            seen_ids.add(id(w))
            out_w.append(WitnessV2(
                kind=w.kind, origin_block=w.origin_block, origin_op=w.origin_op,
                block_chain=w.chain, path=w.path, loan_id=w.loan_id,
                mode=w.mode, condition_mask=c.mask))
        return tuple(out_w)

    def certify_reachable(c: _Candidate) -> CertifyOutcome:
        hint = tuple(c.witnesses[0].chain) if c.witnesses else None
        outcomes: List[CertifyOutcome] = []
        for b in range(bits):
            if not ((c.mask >> b) & 1):
                continue
            outcome = _replay.certify(
                cfg, types, loan_info, scope_vars, tree_paths,
                enabled_for(b), b, (c.block, c.op), c.kind,
                hint_walk=hint, max_loop_unrolls=max_loop_unrolls,
                max_search_states=max_cert_states)
            if outcome.certified is not None:
                return outcome
            outcomes.append(outcome)
        assert outcomes  # c.mask != 0
        # 每个可达赋值都未能在预算内证实。聚合原因：
        # * 全部 refuted -> 抽象伪候选，丢弃；
        # * 存在预算类未决（beyond_loop_budget / budget_exhausted）-> unknown。
        severity = {"refuted": 0, "not_reproduced": 1,
                    "beyond_loop_budget": 2, "budget_exhausted": 3}
        worst = max(outcomes, key=lambda o: severity.get(o.reason or "", 1))
        return worst

    for c in candidates:
        outcome = certify_reachable(c)
        wits = make_evidence_witnesses(c)
        if outcome.certified is not None:
            block = cfg.blocks[c.block]
            op_name = type(block.ops[c.op]).__name__.lower()
            path = getattr(block.ops[c.op], "path", None)
            conflicts.append(ConflictV2(
                kind=c.kind, block=c.block, op=c.op, op_name=op_name,
                path=path, detail=c.detail, witnesses=wits,
                evidence=outcome.certified))
        elif outcome.reason == "refuted":
            # 完备具体状态搜索证伪：抽象 may 过粗产生的伪候选，丢弃。
            refuted_count[0] += 1
        else:
            unknowns.append(UnknownReason(
                kind=c.kind, block=c.block, op=c.op, mask=c.mask,
                reason=outcome.reason or "not_reproduced",
                detail=outcome.detail))

    if unknowns:
        status = "unknown"
    elif conflicts:
        status = "conflict"
    else:
        status = "clean"

    return CheckResultV2(
        status=status, conflicts=conflicts, unknown_reasons=unknowns,
        iterations=iterations, conditions=cfg.conditions,
        budgets={"max_loop_unrolls": max_loop_unrolls,
                 "max_cert_states": max_cert_states,
                 "fixpoint_iteration_bound": bound},
        refuted_candidates=refuted_count[0])


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _render_conflict(c: ConflictV2) -> str:
    e = c.evidence
    head = (f"冲突[{c.kind}] @ blocks[{c.block}].ops[{c.op}] "
            f"({c.op_name}): {c.detail}")
    lines = [head]
    assign = ", ".join(f"{name}={int(v)}" for name, v in e.assignment) or "（无条件）"
    lines.append(f"    认证赋值: {assign}")
    lines.append(f"    可执行游走: {' -> '.join(f'blocks[{b}]' for b in e.walk)}"
                 f"（经回边 {e.back_edges} 条）")
    for w in c.witnesses:
        chain = " -> ".join(f"blocks[{b}]" for b in w.block_chain)
        lines.append(f"    抽象见证: {w.kind} {w.loan_id or _fmt(w.path or ())} "
                     f"诞生 blocks[{w.origin_block}].ops[{w.origin_op}]；{chain}")
    lines.append("    逐步重放:")
    for ev in e.events:
        lines.append(f"      · blocks[{ev.block}].ops[{ev.op}] {ev.text}")
    return "\n".join(lines)
