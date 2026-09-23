"""v2 生命周期分析：初始化/部分移动/词法析构 + 条件命题 + 两阶段借用。

与 v1（``analyzer.py`` 的无条件 may 分析）并列的独立引擎，由
``check(cfg, lifecycle=True)`` 显式选择。共用数据模型、错误类型与
结果/见证数据结构，但传输函数独立实现。

抽象域
======
全局：到达条件表 pc（外部 k 个布尔原子，``R = 2**k`` 行）。状态按**根
变量独立分解**：每个根变量一个分量，分量内的事实表用 ``2R`` 行编码——
低 R 行表示“本次执行尚未经过该变量的词法析构（相位 0）”，高 R 行表示
“已经过析构（相位 1）”。相位位对用户条件不可见，它让“首次访问 drop
点”（真实冲突）与“循环重入同一 drop 点”（drop 已空操作）在不动点内
分开，而不是用 may 并集把首次冲突吞掉。事实键：子树移动 M/p、精确移动
E/p、借用 L/lid。

* 边传播：事实表与边守卫（展开到两相位）合取；合流按行析取。于是同一
  条件的互斥分支在汇合点为空 —— 路径条件直接参与循环不动点与两阶段
  激活，而不是事后筛报错。
* ``declare(v)``：两相位行都投影回相位 0 并清空（重新初始化；遗留借用
  先报 write_while_borrowed 再随变量死亡）。
* ``move`` 压子树；``write(reinit)`` 只恢复该子树并按行拆分祖先移动；
* ``drop(v)``：只作用于相位 0 的行，按声明逆序、只析构仍初始化路径；
  该变量借用 token 在相位 0 行死亡，移动事实映射到相位 1。
* ``borrow_reserve -> borrow_activate``：预约态允许共享读取，激活点
  才按独占规则检查；循环中同一 loan 的旧激活（mut）与新预约共存。

确定冲突 vs unknown
===================
抽象坏赋值表（投影掉相位位后）非空即存在候选；但还必须由**独立的有界
具体解释器**（不调用本引擎）在某条件赋值下搜到真正走到冲突点并现场
复现的块游走，才报确定冲突，并把游走（可执行输入）与逐步重放作为
证据；搜不到（预算/展开轮数受限或属抽象伪冲突）一律记 ``unknown``。

终止性
======
事实表只按位单调增长（kill 减去的行只依赖当前 pc/守卫，不依赖入口
状态，传输单调），每键事实条数有界（:data:`MAX_FACTS_PER_KEY`），
worklist 必达不动点并有显式整数迭代上界。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from .errors import BudgetExhaustedError
from .formula import TruthTables
from .model import (
    BorrowActivate,
    BorrowMut,
    BorrowReserve,
    BorrowShared,
    CFG,
    Decl,
    Drop,
    EndLoan,
    Move,
    Operation,
    Path,
    Read,
    RecordNode,
    Write,
)
from .analyzer import Conflict, Witness, all_type_paths, _is_prefix, _node_at

MAX_FACTS_PER_KEY = 64
MAX_LOOP_UNROLL = 4
MAX_SEARCH_STATES = 20_000


# --------------------------------------------------------------------------
# 可执行证据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayStep:
    """具体重放中的一步。"""

    block: int
    op: int
    op_name: str
    path: Optional[Path]
    moved_before: Tuple[str, ...]
    loans_before: Tuple[Tuple[str, str, int], ...]  # (loan_id, mode, tag)
    note: str


@dataclass(frozen=True)
class ExecutableEvidence:
    """确定冲突的可执行输入与逐步重放。"""

    kind: str
    block: int
    op: int
    assignment: Dict[str, bool]
    walk: Tuple[int, ...]
    steps: Tuple[ReplayStep, ...]
    origin: Tuple[int, int]
    reproduced: bool


# --------------------------------------------------------------------------
# 抽象状态（按根变量分分量，每分量带一个析构相位位）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fact:
    table: int               # 2R 行（低 R=相位0，高 R=相位1）
    origin_block: int
    origin_op: int
    mode: Optional[str]      # loan: shared/mut/reserved；移动事实: None
    chain: Tuple[int, ...]


_WitnessItem = Tuple[_Fact, Path, Optional[str]]


@dataclass
class _VState:
    """一个根变量的分量：M/E/L 事实键各自挂事实。"""

    facts: Dict[Tuple[str, object], List[_Fact]] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.facts is None:
            self.facts = {}

    def copy(self) -> "_VState":
        return _VState({k: [_Fact(f.table, f.origin_block, f.origin_op,
                                  f.mode, f.chain) for f in v]
                        for k, v in self.facts.items()})


@dataclass
class _LState:
    pc: int = 0                       # 外部条件到达表（R 行）
    vars: Dict[str, _VState] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.vars is None:
            self.vars = {}

    def comp(self, var: str) -> _VState:
        v = self.vars.get(var)
        if v is None:
            v = _VState()
            self.vars[var] = v
        return v

    def copy(self) -> "_LState":
        return _LState(self.pc, {v: c.copy() for v, c in self.vars.items()})


@dataclass
class _LoanStatic:
    birth_block: int
    birth_op: int
    kind: str
    path: Path


@dataclass
class _Context:
    cfg: CFG
    types: Dict[str, RecordNode]
    universe: List[Path]
    paths_by_var: Dict[str, List[Path]]
    loans: Dict[str, _LoanStatic]
    tt: TruthTables
    R: int
    RMASK: int
    edge_guards: Dict[Tuple[int, int], int]
    decl_order: Dict[str, Tuple[int, int]]
    in_scope_at: Dict[int, List[str]]


def _build_context(cfg: CFG) -> _Context:
    types: Dict[str, RecordNode] = {}
    loans: Dict[str, _LoanStatic] = {}
    decl_order: Dict[str, Tuple[int, int]] = {}
    for bi, block in enumerate(cfg.blocks):
        for oi, op in enumerate(block.ops):
            if isinstance(op, Decl):
                types[op.var] = op.ty
                decl_order[op.var] = (bi, oi)
            elif isinstance(op, (BorrowShared, BorrowMut, BorrowReserve)):
                kind = ("shared" if isinstance(op, BorrowShared)
                        else "mut" if isinstance(op, BorrowMut) else "reserve")
                loans[op.loan_id] = _LoanStatic(bi, oi, kind, op.path)

    from .validator import _reachable_blocks, _dominators

    reachable = _reachable_blocks(cfg.successors, cfg.entry)
    dom = _dominators(cfg.successors, cfg.entry, reachable)
    in_scope_at: Dict[int, List[str]] = {}
    for bi in range(len(cfg.blocks)):
        vs = [v for v, (db, _) in decl_order.items()
              if db == bi or (db in dom[bi] and db != bi)]
        vs.sort(key=lambda v: decl_order[v])
        in_scope_at[bi] = vs

    tt = TruthTables(sorted(cfg.conditions))
    R = tt.rows
    universe = all_type_paths(types)
    paths_by_var: Dict[str, List[Path]] = {}
    for q in universe:
        paths_by_var.setdefault(q[0], []).append(q)
    # 2R 行中低 R 位（相位0）与高 R 位（相位1）的掩码。
    lo_mask = (1 << R) - 1
    edge_guards: Dict[Tuple[int, int], int] = {}
    for bi, succs in enumerate(cfg.successors):
        for ki, s in enumerate(succs):
            gexpr = cfg.guards[bi][ki] if cfg.guards else None
            edge_guards[(bi, s)] = (tt.compile(gexpr)
                                    if gexpr is not None else tt.full)
    return _Context(cfg, types, universe, paths_by_var, loans, tt,
                    R, lo_mask, edge_guards, decl_order, in_scope_at)


# --------------------------------------------------------------------------
# 事实表代数（2R 行：低 R 相位0，高 R 相位1）
# --------------------------------------------------------------------------


def _k_sub(p: Path) -> Tuple[str, object]:
    return ("M", p)


def _k_exact(p: Path) -> Tuple[str, object]:
    return ("E", p)


def _k_loan(lid: str) -> Tuple[str, object]:
    return ("L", lid)


def _k_phase() -> Tuple[str, object]:
    """分量的相位/在域事实：低 R 行=未析构（相位0），高 R 行=已析构
    （相位1）。declare 后全部置为相位 0；drop 把相位 0 行移到相位 1。"""
    return ("P", None)


def _both_phase(ctx: _Context, ext_rows: int) -> int:
    """外部条件表 -> 展开到两相位的 2R 表。"""
    return ext_rows | (ext_rows << ctx.R)


def _project(ctx: _Context, table: int) -> int:
    """2R 表投影掉相位位：任一相位成立即外部成立。"""
    return (table & ctx.RMASK) | (table >> ctx.R)


def _covered(facts: Dict[Tuple[str, object], List[_Fact]],
             key: Tuple[str, object]) -> int:
    table = 0
    for f in facts.get(key, ()):
        table |= f.table
    return table


def _put_fact(comp: _VState, key: Tuple[str, object], fact: _Fact,
              unknown: List[str]) -> None:
    bucket = comp.facts.setdefault(key, [])
    for i, f in enumerate(bucket):
        if (f.origin_block, f.origin_op, f.mode) == (
                fact.origin_block, fact.origin_op, fact.mode):
            if fact.table & ~f.table:
                bucket[i] = _Fact(f.table | fact.table, f.origin_block,
                                  f.origin_op, f.mode, f.chain)
            return
    new_rows = fact.table
    for f in bucket:
        if (f.origin_block, f.origin_op, f.mode) == (
                fact.origin_block, fact.origin_op, fact.mode):
            new_rows &= ~f.table
    if not new_rows:
        return
    if len(bucket) >= MAX_FACTS_PER_KEY:
        catch = next((f for f in bucket if f.origin_block == -1), None)
        if catch is not None:
            idx = bucket.index(catch)
            bucket[idx] = _Fact(catch.table | fact.table, -1, -1,
                                fact.mode, (-1,))
        else:
            bucket.append(_Fact(fact.table, -1, -1, fact.mode, (-1,)))
        reason = ("fact_budget: 事实键 %s 的不同诞生点超过 %d，已合并为"
                  "捕获事实，结论降级 unknown" % (key, MAX_FACTS_PER_KEY))
        if reason not in unknown:
            unknown.append(reason)
        return
    bucket.append(_Fact(new_rows, fact.origin_block, fact.origin_op,
                        fact.mode, fact.chain))


def _kill(comp: _VState, key: Tuple[str, object], rows: int) -> None:
    bucket = comp.facts.get(key)
    if not bucket:
        return
    kept: List[_Fact] = []
    for f in bucket:
        remain = f.table & ~rows
        if remain:
            kept.append(_Fact(remain, f.origin_block, f.origin_op,
                              f.mode, f.chain))
    if kept:
        comp.facts[key] = kept
    else:
        comp.facts.pop(key, None)


def _disabled_2r(ctx: _Context, st: _LState, path: Path) -> int:
    var = path[0]
    comp = st.vars.get(var)
    if comp is None:
        return 0
    table = _covered(comp.facts, _k_exact(path))
    for q in ctx.paths_by_var[var]:
        if _is_prefix(q, path):
            table |= _covered(comp.facts, _k_sub(q))
    return table


def _move_witness_items(ctx: _Context, st: _LState, path: Path,
                        rows: int) -> List[_WitnessItem]:
    var = path[0]
    comp = st.vars.get(var)
    out: List[_WitnessItem] = []
    if comp is not None:
        for q in ctx.paths_by_var[var]:
            if _is_prefix(q, path):
                for f in comp.facts.get(_k_sub(q), ()):
                    if f.table & rows:
                        out.append((f, q, None))
        for f in comp.facts.get(_k_exact(path), ()):
            if f.table & rows:
                out.append((f, path, None))
    out.sort(key=lambda w: (w[0].origin_block, w[0].origin_op))
    return out


def _loan_witness_items(ctx: _Context, st: _LState, path: Path,
                        modes: Tuple[str, ...],
                        exclude: Optional[str] = None
                        ) -> Tuple[int, List[_WitnessItem]]:
    table = 0
    items: List[_WitnessItem] = []
    for lid, info in ctx.loans.items():
        if lid == exclude:
            continue
        if not (_is_prefix(info.path, path) or _is_prefix(path, info.path)):
            continue
        comp = st.vars.get(path[0])
        if comp is None:
            continue
        for f in comp.facts.get(_k_loan(lid), ()):
            if f.mode in modes:
                table |= f.table
                items.append((f, info.path, lid))
    items.sort(key=lambda w: (w[0].origin_block, w[0].origin_op,
                              w[2] or ""))
    return table, items


def _loan_comp(st: _LState, ctx: _Context, lid: str) -> Optional[_VState]:
    return st.vars.get(ctx.loans[lid].path[0])


def _phase_rows(ctx: _Context, st: _LState, var: str) -> int:
    """该变量在块内当前点的 2R 行集合（按相位）。

    无相位事实（从未 drop、未重声明）时全部执行都在相位 0（低 R 行）；
    drop 后相位 0 行移到相位 1；declare 复位回相位 0。
    """
    comp = st.vars.get(var)
    if comp is None:
        return st.pc
    table = _covered(comp.facts, _k_phase())
    return table if table else st.pc


# --------------------------------------------------------------------------
# 块内传输
# --------------------------------------------------------------------------


@dataclass
class _RawConflict:
    kind: str
    op_name: str
    path: Optional[Path]
    bad: int                  # 外部条件表（已投影掉相位位）
    items: List[_WitnessItem]


class _LTransfer:
    def __init__(self, ctx: _Context, unknown: List[str]) -> None:
        self.ctx = ctx
        self.unknown = unknown
        self.raw: List[_RawConflict] = []

    def _emit(self, kind: str, op_name: str, path: Optional[Path],
              bad_2r: int, items: List[_WitnessItem]) -> None:
        bad = _project(self.ctx, bad_2r)
        if bad:
            self.raw.append(_RawConflict(kind, op_name, path, bad, items))

    def _add_subtree_move(self, st: _LState, bi: int, oi: int,
                          p: Path, rows_2r: int) -> None:
        ctx = self.ctx
        comp = st.comp(p[0])
        c = rows_2r
        for q in ctx.paths_by_var[p[0]]:
            if _is_prefix(q, p):
                c &= ~_covered(comp.facts, _k_sub(q))
        if not c:
            return
        for q in ctx.paths_by_var[p[0]]:
            if q == p or _is_prefix(p, q):
                _kill(comp, _k_sub(q), c)
                _kill(comp, _k_exact(q), c)
        _put_fact(comp, _k_sub(p), _Fact(c, bi, oi, None, (bi,)),
                  self.unknown)

    def _restore(self, st: _LState, p: Path, rows_2r: int) -> None:
        ctx = self.ctx
        comp = st.comp(p[0])
        c = rows_2r
        for q in ctx.paths_by_var[p[0]]:
            if q == p or _is_prefix(p, q):
                _kill(comp, _k_sub(q), c)
                _kill(comp, _k_exact(q), c)
        for m in ctx.paths_by_var[p[0]]:
            if not (_is_prefix(m, p) and m != p):
                continue
            key = _k_sub(m)
            bucket = comp.facts.get(key)
            if not bucket:
                continue
            for f in list(bucket):
                rows = f.table & c
                if not rows:
                    continue
                if f.table == rows:
                    bucket.remove(f)
                else:
                    idx = bucket.index(f)
                    bucket[idx] = _Fact(
                        f.table & ~c, f.origin_block, f.origin_op,
                        f.mode, f.chain)
                ob, oo, ch = f.origin_block, f.origin_op, f.chain
                _put_fact(comp, _k_exact(m),
                          _Fact(rows, ob, oo, None, ch), self.unknown)
                for depth in range(len(m), len(p)):
                    prefix = p[:depth]
                    node = _node_at(ctx.types, prefix)
                    if isinstance(node, dict):
                        nxt = p[depth]
                        for fld in node:
                            if fld != nxt:
                                sib = (*prefix, fld)
                                _put_fact(comp, _k_sub(sib),
                                          _Fact(rows, ob, oo, None, ch),
                                          self.unknown)
            if not comp.facts.get(key):
                comp.facts.pop(key, None)

    def _declare(self, st: _LState, bi: int, oi: int, op: Decl) -> None:
        ctx = self.ctx
        c = _both_phase(ctx, st.pc)
        comp = st.comp(op.var)
        # 遗留借用（任一相位）：报 write_while_borrowed，然后随变量死亡。
        for lid, info in ctx.loans.items():
            if info.path[0] != op.var:
                continue
            items = [(f, info.path, lid)
                     for f in comp.facts.get(_k_loan(lid), ())
                     if f.table & c]
            bad = 0
            for f, _, _ in items:
                bad |= f.table & c
            if bad:
                self._emit("write_while_borrowed", "declare",
                           (op.var,), bad, items)
            comp.facts.pop(_k_loan(lid), None)
        for q in ctx.paths_by_var.get(op.var, ()):
            comp.facts.pop(_k_sub(q), None)
            comp.facts.pop(_k_exact(q), None)
        # 相位复位：两相位历史都投影回相位 0（全新局部变量）。
        comp.facts[_k_phase()] = [_Fact(st.pc, bi, oi, None, (bi,))]

    def _drop(self, st: _LState, bi: int, oi: int) -> None:
        ctx = self.ctx
        lo = st.pc                 # 相位 0（本次尚未析构）的低 R 位
        for var in self._droppable(bi, oi):
            comp = st.comp(var)
            phase_before = _phase_rows(ctx, st, var)
            active0 = phase_before & lo
            for lid, info in ctx.loans.items():
                if info.path[0] != var:
                    continue
                # may 分析无法可靠判定“仍初始化”（否定条件：合流后
                # may-moved 会掩盖部分路径上的初始化状态），因此候选只
                # 要求 loan 在相位 0 活跃；“仍初始化”由分级阶段的独立
                # 具体解释器权威确认（搜不到可执行游走即丢弃或降级）。
                items = [(f, info.path, lid)
                         for f in comp.facts.get(_k_loan(lid), ())
                         if f.table & active0]
                bad = 0
                for f, _, _ in items:
                    bad |= f.table & active0
                if bad:
                    self._emit("drop_while_borrowed", "drop", info.path,
                               bad, items)
                # token 只在相位 0 的析构中死亡；相位 1 行保持不变。
                _kill(comp, _k_loan(lid), active0)
            # 移动事实：相位 0 行映射到相位 1，相位 1 行保持。
            for key in list(comp.facts):
                kind, _k = key
                if kind not in ("M", "E"):
                    continue
                bucket = comp.facts[key]
                new_bucket: List[_Fact] = []
                for f in bucket:
                    shifted = (f.table & lo) << ctx.R
                    remain = f.table & ~lo
                    merged = remain | shifted
                    if merged:
                        new_bucket.append(_Fact(
                            merged, f.origin_block, f.origin_op,
                            f.mode, f.chain))
                if new_bucket:
                    comp.facts[key] = new_bucket
                else:
                    comp.facts.pop(key, None)
            fresh = active0 & ~_disabled_2r(ctx, st, (var,))
            if fresh:
                _put_fact(comp, _k_sub((var,)),
                          _Fact(fresh << ctx.R, bi, oi, None, (bi,)),
                          self.unknown)
            # 相位翻转：相位 0 -> 相位 1（原相位 1 行保留）。
            new_phase = (phase_before & ~lo) | ((phase_before & lo) << ctx.R)
            if new_phase:
                comp.facts[_k_phase()] = [
                    _Fact(new_phase, bi, oi, None, (bi,))]

    def _droppable(self, bi: int, oi: int) -> List[str]:
        out: List[str] = []
        for var in self.ctx.in_scope_at[bi]:
            db, do = self.ctx.decl_order[var]
            if db == bi and do >= oi:
                continue
            out.append(var)
        out.sort(key=lambda v: self.ctx.decl_order[v], reverse=True)
        return out

    def run(self, bi: int, state: _LState) -> _LState:
        st = state.copy()
        for oi, op in enumerate(self.ctx.cfg.blocks[bi].ops):
            self._step(st, bi, oi, op)
        # 出口归一化：作用域内每个变量都必须带显式相位事实 P，使跨块
        # 合流时“未 drop（相位0）”与“已 drop（相位1）”两类路径能按行
        # 并存而不互相覆盖。无 P 即本路径未析构 -> P=pc（低 R 位）。
        for var in self.ctx.in_scope_at[bi]:
            comp = st.comp(var)
            if not comp.facts.get(_k_phase()):
                comp.facts[_k_phase()] = [
                    _Fact(st.pc, bi, len(self.ctx.cfg.blocks[bi].ops),
                          None, (bi,))]
        return st

    def _step(self, st: _LState, bi: int, oi: int, op: Operation) -> None:
        if not st.pc:
            return

        if isinstance(op, Decl):
            self._declare(st, bi, oi, op)
            return
        if isinstance(op, Drop):
            self._drop(st, bi, oi)
            return

        # 路径相关操作只作用于所属变量当前相位的行；end_loan 无条件清除
        # 本 loan 的全部存活 token（两相位都清）。
        if isinstance(op, EndLoan):
            c = _both_phase(self.ctx, st.pc)
        elif isinstance(op, BorrowActivate):
            c = _phase_rows(
                self.ctx, st, self.ctx.loans[op.loan_id].path[0])
        else:
            c = _phase_rows(self.ctx, st, op.path[0])
        if not c:
            return

        if isinstance(op, Move):
            bad = _disabled_2r(self.ctx, st, op.path) & c
            self._emit("use_of_moved", "move", op.path, bad,
                       _move_witness_items(self.ctx, st, op.path, bad))
            lt, items = _loan_witness_items(
                self.ctx, st, op.path, ("shared", "mut", "reserved"))
            self._emit("move_while_borrowed", "move", op.path, lt & c, items)
            self._add_subtree_move(st, bi, oi, op.path, c)
            return

        if isinstance(op, (BorrowShared, BorrowMut, BorrowReserve)):
            if isinstance(op, BorrowShared):
                mode, tag, modes = "shared", "shared", ("mut",)
            elif isinstance(op, BorrowMut):
                mode, tag, modes = "mut", "mut", (
                    "shared", "mut", "reserved")
            else:
                mode, tag, modes = "reserved", "reserve", ("shared", "mut")
            bad = _disabled_2r(self.ctx, st, op.path) & c
            self._emit("use_of_moved", f"borrow_{tag}", op.path, bad,
                       _move_witness_items(self.ctx, st, op.path, bad))
            lt, items = _loan_witness_items(
                self.ctx, st, op.path, modes)
            self._emit("borrow_conflict", f"borrow_{tag}", op.path,
                       lt & c, items)
            comp = st.comp(op.path[0])
            _put_fact(comp, _k_loan(op.loan_id),
                      _Fact(c, bi, oi, mode, (bi,)), self.unknown)
            return

        if isinstance(op, BorrowActivate):
            self._activate(st, bi, oi, op)
            return

        if isinstance(op, EndLoan):
            comp = _loan_comp(st, self.ctx, op.loan_id)
            if comp is not None:
                _kill(comp, _k_loan(op.loan_id), c)
            return

        if isinstance(op, Read):
            bad = _disabled_2r(self.ctx, st, op.path) & c
            self._emit("use_of_moved", "read", op.path, bad,
                       _move_witness_items(self.ctx, st, op.path, bad))
            lt, items = _loan_witness_items(
                self.ctx, st, op.path, ("mut",))
            self._emit("read_while_mut_borrowed", "read", op.path,
                       lt & c, items)
            return

        if isinstance(op, Write):
            lt, items = _loan_witness_items(
                self.ctx, st, op.path, ("shared", "mut", "reserved"))
            self._emit("write_while_borrowed", "write", op.path,
                       lt & c, items)
            if op.reinit:
                self._restore(st, op.path, c)
            else:
                bad = _disabled_2r(self.ctx, st, op.path) & c
                self._emit("use_of_moved", "write", op.path, bad,
                           _move_witness_items(self.ctx, st, op.path, bad))
            return

    def _activate(self, st: _LState, bi: int, oi: int,
                  op: BorrowActivate) -> None:
        ctx = self.ctx
        c = _phase_rows(ctx, st, ctx.loans[op.loan_id].path[0])
        info = ctx.loans[op.loan_id]
        path = info.path
        bad = _disabled_2r(ctx, st, path) & c
        self._emit("use_of_moved", "borrow_activate", path, bad,
                   _move_witness_items(ctx, st, path, bad))
        lt, items = _loan_witness_items(
            ctx, st, path, ("shared", "mut", "reserved"),
            exclude=op.loan_id)
        comp = st.comp(path[0])
        self_items = [(f, path, op.loan_id)
                      for f in comp.facts.get(_k_loan(op.loan_id), ())
                      if f.mode == "mut" and f.table & c]
        for it in self_items:
            lt |= it[0].table & c
            items.append(it)
        items.sort(key=lambda w: (w[0].origin_block, w[0].origin_op,
                                  w[2] or ""))
        self._emit("borrow_conflict", "borrow_activate", path, lt, items)

        reserved = [f for f in comp.facts.get(_k_loan(op.loan_id), ())
                    if f.mode == "reserved"]
        rows = 0
        for f in reserved:
            rows |= f.table & c
        _kill(comp, _k_loan(op.loan_id), c)
        if rows:
            f0 = reserved[0]
            _put_fact(comp, _k_loan(op.loan_id),
                      _Fact(rows, f0.origin_block, f0.origin_op, "mut",
                            f0.chain), self.unknown)


# --------------------------------------------------------------------------
# 边传播 / 合流 / 不动点
# --------------------------------------------------------------------------


def _extend_chain(chain: Tuple[int, ...], nxt: int) -> Tuple[int, ...]:
    if nxt in chain:
        return chain[: chain.index(nxt) + 1]
    return (*chain, nxt)


def _propagate(st: _LState, guard: int, succ: int,
               ctx: _Context) -> _LState:
    g2 = _both_phase(ctx, guard)
    out = _LState(pc=st.pc & guard)
    for var, comp in st.vars.items():
        lifted_comp = _VState()
        for key, bucket in comp.facts.items():
            lifted = [_Fact(f.table & g2, f.origin_block, f.origin_op,
                            f.mode, _extend_chain(f.chain, succ))
                      for f in bucket if f.table & g2]
            if lifted:
                lifted_comp.facts[key] = lifted
        if lifted_comp.facts:
            out.vars[var] = lifted_comp
    return out


def _join(into: _LState, other: _LState, unknown: List[str]) -> bool:
    grown = (other.pc & ~into.pc) != 0
    into.pc |= other.pc
    for var, comp in other.vars.items():
        target = into.comp(var)
        for key, bucket in comp.facts.items():
            before = _covered(target.facts, key)
            for f in bucket:
                _put_fact(target, key,
                          _Fact(f.table, f.origin_block, f.origin_op,
                                f.mode, f.chain), unknown)
            if _covered(target.facts, key) & ~before:
                grown = True
    return grown


def _state_equal(a: _LState, b: _LState) -> bool:
    if a.pc != b.pc or set(a.vars) != set(b.vars):
        return False
    for var, ca in a.vars.items():
        cb = b.vars[var]
        if set(ca.facts) != set(cb.facts):
            return False
        for key in ca.facts:
            fa = sorted(ca.facts[key],
                        key=lambda f: (f.origin_block, f.origin_op,
                                       f.mode or ""))
            fb = sorted(cb.facts[key],
                        key=lambda f: (f.origin_block, f.origin_op,
                                       f.mode or ""))
            if len(fa) != len(fb):
                return False
            for x, y in zip(fa, fb):
                if (x.table, x.origin_block, x.origin_op, x.mode) != (
                        y.table, y.origin_block, y.origin_op, y.mode):
                    return False
    return True


def run_fixpoint(ctx: _Context, *, max_iterations: Optional[int] = None
                 ) -> Tuple[List[_LState], int, List[str]]:
    cfg = ctx.cfg
    n = len(cfg.blocks)
    preds: Dict[int, List[int]] = {i: [] for i in range(n)}
    for bi, succs in enumerate(cfg.successors):
        for s in succs:
            preds[s].append(bi)
    for plist in preds.values():
        plist.sort()

    # 增长事件：pc 至多 R 次；每个事实键（2P 个移动键 + L 个 loan 键）
    # 至多 F*2R 个新行 + F 条新增。每次出口增长至多使出边后继入队。
    key_count = 2 * len(ctx.universe) + len(ctx.loans)
    edge_count = sum(len(s) for s in cfg.successors)
    growth = ctx.R + key_count * (MAX_FACTS_PER_KEY * 2 * ctx.R + 1)
    bound = n + edge_count * (growth + 1)
    budget = bound if max_iterations is None else max_iterations

    ins: List[_LState] = [_LState() for _ in range(n)]
    outs: Dict[int, _LState] = {}
    iterations = 0
    unknown: List[str] = []

    def process(idx: int) -> bool:
        nonlocal iterations
        iterations += 1
        if iterations > budget:
            raise BudgetExhaustedError(
                f"生命周期不动点预算耗尽：{budget} 块次未收敛（上界 {bound}）。"
                "这不是“无冲突”证明。")
        merged = _LState()
        if idx == cfg.entry:
            merged.pc = ctx.tt.full
        for p in preds[idx]:
            if p in outs:
                _join(merged,
                      _propagate(outs[p], ctx.edge_guards[(p, idx)],
                                 idx, ctx), unknown)
        ins[idx] = merged
        new_out = _LTransfer(ctx, unknown).run(idx, merged)
        old = outs.get(idx)
        changed = old is None or not _state_equal(old, new_out)
        outs[idx] = new_out
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
    return ins, iterations, unknown


# --------------------------------------------------------------------------
# 独立具体解释器（证据搜索与重放；不调用抽象引擎）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    lid: str
    mode: str
    tag: int


@dataclass(frozen=True)
class _CState:
    sub: FrozenSet[Path]
    exact: FrozenSet[Path]
    loans: FrozenSet[_Token]
    dropped: FrozenSet[str] = frozenset()
    next_tag: int = 0


def _c_disabled(cs: _CState, p: Path) -> bool:
    if p in cs.exact:
        return True
    return any(_is_prefix(m, p) for m in cs.sub)


def _c_overlaps(ctx: _Context, cs: _CState, p: Path
                ) -> List[Tuple[str, str, int]]:
    out = []
    for tok in cs.loans:
        q = ctx.loans[tok.lid].path
        if _is_prefix(q, p) or _is_prefix(p, q):
            out.append((tok.lid, tok.mode, tok.tag))
    return sorted(out)


def _c_restore(cs: _CState, p: Path, ctx: _Context) -> _CState:
    sub = {m for m in cs.sub if not (p == m or _is_prefix(p, m))}
    exact = {m for m in cs.exact if not (p == m or _is_prefix(p, m))}
    for m in list(sub):
        if _is_prefix(m, p) and m != p:
            sub.discard(m)
            exact.add(m)
            for depth in range(len(m), len(p)):
                prefix = p[:depth]
                node = _node_at(ctx.types, prefix)
                if isinstance(node, dict):
                    nxt = p[depth]
                    for fld in node:
                        if fld != nxt:
                            sub.add((*prefix, fld))
    return _CState(frozenset(sub), frozenset(exact), cs.loans,
                   cs.dropped, cs.next_tag)


def _droppable_vars(ctx: _Context, bi: int, oi: int) -> List[str]:
    out: List[str] = []
    for var in ctx.in_scope_at[bi]:
        db, do = ctx.decl_order[var]
        if db == bi and do >= oi:
            continue
        out.append(var)
    out.sort(key=lambda v: ctx.decl_order[v], reverse=True)
    return out


def _c_step(ctx: _Context, bi: int, oi: int, op: Operation, cs0: _CState
            ) -> Tuple[_CState, List[str]]:
    """逐操作具体执行。借用是 token 多重集；dropped 记录本次已析构变量。"""
    kinds: List[str] = []
    sub, exact = set(cs0.sub), set(cs0.exact)
    tokens = set(cs0.loans)
    dropped = set(cs0.dropped)
    next_tag = cs0.next_tag

    def root_tokens(var: str):
        return {t for t in tokens
                if ctx.loans[t.lid].path[0] == var}

    if isinstance(op, Decl):
        if root_tokens(op.var):
            kinds.append("write_while_borrowed")
        tokens -= root_tokens(op.var)
        sub = {m for m in sub if m[0] != op.var}
        exact = {m for m in exact if m[0] != op.var}
        dropped.discard(op.var)

    elif isinstance(op, Move):
        if _c_disabled(cs0, op.path):
            kinds.append("use_of_moved")
        if _c_overlaps(ctx, cs0, op.path):
            kinds.append("move_while_borrowed")
        sub = {m for m in sub
               if not (_is_prefix(op.path, m) and m != op.path)}
        exact = {m for m in exact if not _is_prefix(op.path, m)}
        sub.add(op.path)

    elif isinstance(op, (BorrowShared, BorrowMut, BorrowReserve)):
        tag = ("shared" if isinstance(op, BorrowShared)
               else "mut" if isinstance(op, BorrowMut) else "reserve")
        mode = {"shared": "shared", "mut": "mut", "reserve": "reserved"}[tag]
        if _c_disabled(cs0, op.path):
            kinds.append("use_of_moved")
        conflict_modes = {"shared": {"mut"},
                          "mut": {"shared", "mut", "reserved"},
                          "reserve": {"shared", "mut"}}[tag]
        if any(m in conflict_modes for _, m, _ in
               _c_overlaps(ctx, cs0, op.path)):
            kinds.append("borrow_conflict")
        tokens.add(_Token(op.loan_id, mode, next_tag))
        next_tag += 1

    elif isinstance(op, BorrowActivate):
        p = ctx.loans[op.loan_id].path
        if _c_disabled(cs0, p):
            kinds.append("use_of_moved")
        others = [x for x in _c_overlaps(ctx, cs0, p) if x[0] != op.loan_id]
        leftover_mut = [t for t in tokens
                        if t.lid == op.loan_id and t.mode == "mut"]
        if others or leftover_mut:
            kinds.append("borrow_conflict")
        reserved = sorted(
            (t for t in tokens
             if t.lid == op.loan_id and t.mode == "reserved"),
            key=lambda t: t.tag)
        if reserved:
            target = reserved[-1]
            tokens.discard(target)
            tokens.add(_Token(op.loan_id, "mut", target.tag))

    elif isinstance(op, EndLoan):
        tokens = {t for t in tokens if t.lid != op.loan_id}

    elif isinstance(op, Read):
        if _c_disabled(cs0, op.path):
            kinds.append("use_of_moved")
        if any(m == "mut" for _, m, _ in
               _c_overlaps(ctx, cs0, op.path)):
            kinds.append("read_while_mut_borrowed")

    elif isinstance(op, Write):
        if _c_overlaps(ctx, cs0, op.path):
            kinds.append("write_while_borrowed")
        if op.reinit:
            new = _c_restore(cs0, op.path, ctx)
            sub, exact = set(new.sub), set(new.exact)
        elif _c_disabled(cs0, op.path):
            kinds.append("use_of_moved")

    elif isinstance(op, Drop):
        for var in _droppable_vars(ctx, bi, oi):
            if var in dropped:
                continue
            hit = False
            for tok in tokens:
                info = ctx.loans[tok.lid]
                if info.path[0] != var:
                    continue
                for q in ctx.paths_by_var.get(var, ()):
                    if not (_is_prefix(info.path, q)
                            or _is_prefix(q, info.path)):
                        continue
                    if not _c_disabled(cs0, q):
                        hit = True
            if hit:
                kinds.append("drop_while_borrowed")
            tokens -= root_tokens(var)
            sub.add((var,))
            dropped.add(var)

    return _CState(frozenset(sub), frozenset(exact), frozenset(tokens),
                   frozenset(dropped), next_tag), kinds


def _find_executable(ctx: _Context, kind: str, bi: int, oi: int,
                     bad_ext: int, search_budget: int
                     ) -> Tuple[str, Optional[Tuple[Tuple[int, ...], int]]]:
    """搜索在 (bi,oi) 现场复现 kind 的可执行游走。

    返回 ``("hit", (walk, row))``；穷举完预算内可达状态空间仍无命中为
    ``("exhausted", None)``（候选证伪，静默丢弃）；因搜索预算提前终止为
    ``("budget", None)``（保留 unknown）。
    """
    n = len(ctx.cfg.blocks)
    cap = (MAX_LOOP_UNROLL + 1) * n + 1
    start = _CState(frozenset(), frozenset(), frozenset())
    full = ctx.tt.full

    @dataclass
    class _Node:
        block: int
        state: _CState
        feasible: int
        parent: int
        depth: int

    masks: Dict[Tuple[int, _CState], List[int]] = {
        (ctx.cfg.entry, start): [full]}
    nodes: List[_Node] = [_Node(ctx.cfg.entry, start, full, -1, 1)]
    queue: deque[int] = deque([0])
    explored = 0
    truncated = False

    while queue:
        ni = queue.popleft()
        node = nodes[ni]
        explored += 1
        if explored > search_budget:
            truncated = True
            break
        b, cs = node.block, node.state

        cur = cs
        if b == bi:
            for k, op in enumerate(ctx.cfg.blocks[b].ops):
                new_cs, kinds = _c_step(ctx, b, k, op, cur)
                if k == oi and kind in kinds:
                    hit_rows = node.feasible & bad_ext
                    if hit_rows:
                        return ("hit",
                                (_backtrack(nodes, ni), _least_bit(hit_rows)))
                cur = new_cs
        else:
            for k, op in enumerate(ctx.cfg.blocks[b].ops):
                cur, _ = _c_step(ctx, b, k, op, cur)

        if node.depth >= cap:
            # 到达声明的 4 轮循环展开边界（与独立穷举参考同口径）：该节点
            # 不再扩展；全部预算内可达状态都检查过仍无命中即“有界不可达”。
            continue
        for s in ctx.cfg.successors[b]:
            feas = node.feasible & ctx.edge_guards[(b, s)]
            if not feas:
                continue
            key = (s, cur)
            existing = masks.setdefault(key, [])
            if any((feas & ~m) == 0 for m in existing):
                continue
            existing.append(feas)
            nodes.append(_Node(s, cur, feas, ni, node.depth + 1))
            queue.append(len(nodes) - 1)
    return ("budget", None) if truncated else ("exhausted", None)


def _least_bit(table: int) -> int:
    return (table & -table).bit_length() - 1


def _backtrack(nodes: List["_Node"], idx: int) -> Tuple[int, ...]:
    chain: List[int] = []
    cur = idx
    while cur != -1:
        chain.append(nodes[cur].block)
        cur = nodes[cur].parent
    return tuple(reversed(chain))


_OP_DISPLAY = {
    Decl: "declare", Move: "move", BorrowShared: "borrow_shared",
    BorrowMut: "borrow_mut", BorrowReserve: "borrow_reserve",
    BorrowActivate: "borrow_activate", EndLoan: "end_loan",
    Read: "read", Write: "write", Drop: "drop",
}


def _record_replay(ctx: _Context, walk: Tuple[int, ...],
                   stop: Tuple[int, int], stop_kind: str
                   ) -> Tuple[List[ReplayStep], bool]:
    cs = _CState(frozenset(), frozenset(), frozenset())
    steps: List[ReplayStep] = []
    reproduced = False
    last_pos = len(walk) - 1
    for pos, b in enumerate(walk):
        for oi, op in enumerate(ctx.cfg.blocks[b].ops):
            moved = tuple(sorted(".".join(m) for m in (cs.sub | cs.exact)))
            loans_before = tuple(sorted(
                (t.lid, t.mode, t.tag) for t in cs.loans))
            new_cs, kinds = _c_step(ctx, b, oi, op, cs)
            note = _note_for(ctx, b, oi, op, cs, kinds)
            path = getattr(op, "path", None)
            op_name = _OP_DISPLAY.get(type(op), type(op).__name__.lower())
            if pos == last_pos and oi == stop[1]:
                reproduced = stop_kind in kinds
                mark = "  >>> 现场复现: " + ", ".join(sorted(set(kinds)))
                steps.append(ReplayStep(
                    b, oi, op_name, path, moved, loans_before,
                    note + mark))
                return steps, reproduced
            steps.append(ReplayStep(
                b, oi, op_name, path, moved, loans_before, note))
            cs = new_cs
    return steps, reproduced


def _note_for(ctx: _Context, b: int, oi: int, op: Operation,
              cs: _CState, kinds: List[str]) -> str:
    if isinstance(op, Decl):
        return f"declare {op.var}：整个子树初始化"
    if isinstance(op, Drop):
        return f"drop：逆声明顺序析构 {_droppable_vars(ctx, b, oi)}"
    p = getattr(op, "path", None)
    if isinstance(op, Move):
        return (f"move {'.'.join(p)}：执行前 "
                f"{'已移动' if _c_disabled(cs, p) else '已初始化'}，"
                f"重叠借用 {_c_overlaps(ctx, cs, p) or '无'}")
    if isinstance(op, Read):
        return (f"read {'.'.join(p)}：执行前 "
                f"{'已移动' if _c_disabled(cs, p) else '已初始化'}，"
                f"重叠可变 "
                f"{[x for x in _c_overlaps(ctx, cs, p) if x[1]=='mut'] or '无'}")
    if isinstance(op, Write):
        return (f"write {'.'.join(p)}{' reinit' if op.reinit else ''}："
                f"重叠借用 {_c_overlaps(ctx, cs, p) or '无'}")
    if isinstance(op, BorrowReserve):
        return f"reserve {op.loan_id} on {'.'.join(p)}（预约，允许共享读）"
    if isinstance(op, BorrowActivate):
        q = ctx.loans[op.loan_id].path
        other = [x for x in _c_overlaps(ctx, cs, q) if x[0] != op.loan_id]
        leftover = [(t.lid, t.mode, t.tag) for t in cs.loans
                    if t.lid == op.loan_id and t.mode == "mut"]
        return (f"activate {op.loan_id} on {'.'.join(q)}：其他 loan 重叠 "
                f"{other or '无'}；本 loan 遗留激活 {leftover or '无'}")
    if isinstance(op, EndLoan):
        return f"end_loan {op.loan_id}"
    if isinstance(op, (BorrowShared, BorrowMut)):
        return f"{type(op).__name__.lower()} {op.loan_id} on {'.'.join(p)}"
    return ""


# --------------------------------------------------------------------------
# 分级与总入口
# --------------------------------------------------------------------------


def check_lifecycle(cfg: CFG, *, max_iterations: Optional[int] = None,
                    search_budget: int = MAX_SEARCH_STATES
                    ) -> Tuple[str, List[Conflict], int, List[str],
                               List[ExecutableEvidence]]:
    ctx = _build_context(cfg)
    ins, iterations, fp_unknown = run_fixpoint(
        ctx, max_iterations=max_iterations)
    conflicts, evidences, classify_unknown = _classify(
        ctx, ins, search_budget=search_budget)
    unknown = sorted(set(fp_unknown) | set(classify_unknown))
    status = "conflict" if conflicts else ("unknown" if unknown else "ok")
    return status, conflicts, iterations, unknown, evidences


def _classify(ctx: _Context, ins: List[_LState], *,
              search_budget: int
              ) -> Tuple[List[Conflict], List[ExecutableEvidence], List[str]]:
    conflicts: List[Conflict] = []
    evidences: List[ExecutableEvidence] = []
    unknown: List[str] = []
    reported: set[Tuple[str, int, int]] = set()

    for bi in range(len(ctx.cfg.blocks)):
        st0 = ins[bi]
        if not st0.pc:
            continue
        tr = _LTransfer(ctx, unknown)
        cur = st0.copy()
        merged_raw: Dict[Tuple[str, int, int], _RawConflict] = {}
        for oi, op in enumerate(ctx.cfg.blocks[bi].ops):
            n_before = len(tr.raw)
            tr._step(cur, bi, oi, op)
            for rc in tr.raw[n_before:]:
                key = (rc.kind, bi, oi)
                old = merged_raw.get(key)
                if old is None:
                    merged_raw[key] = _RawConflict(
                        rc.kind, rc.op_name, rc.path, rc.bad,
                        list(rc.items))
                else:
                    old.bad |= rc.bad
                    for item in rc.items:
                        if item not in old.items:
                            old.items.append(item)
        for (kind, _, oi), rc in sorted(merged_raw.items()):
            key = (rc.kind, bi, oi)
            if key in reported:
                continue
            verdict, found = _find_executable(
                ctx, rc.kind, bi, oi, rc.bad, search_budget)
            if verdict != "hit":
                if verdict == "budget":
                    unknown.append(
                        "unverified_conflict: %s @ blocks[%d].ops[%d]：抽象"
                        "状态提出候选（条件 %s），但证据搜索在预算（%d 轮"
                        "展开、%d 状态）内未完成，保留 unknown"
                        % (rc.kind, bi, oi,
                           ctx.tt.render_table(rc.bad),
                           MAX_LOOP_UNROLL, search_budget))
                # verdict == "exhausted"：有界具体解释器穷举完可达状态
                # 空间仍不能复现 -> 抽象候选被证伪（may 否定条件导致的
                # 伪冲突），静默丢弃，不报错也不记 unknown。
                continue
            walk, row = found
            assign = ctx.tt.assignment_of(row)
            steps, reproduced = _record_replay(
                ctx, walk, (bi, oi), rc.kind)
            origin = _pick_origin(rc)
            ev = ExecutableEvidence(
                kind=rc.kind, block=bi, op=oi, assignment=assign,
                walk=tuple(walk), steps=tuple(steps),
                origin=origin, reproduced=reproduced)
            evidences.append(ev)
            if not reproduced:
                unknown.append(
                    "replay_mismatch: %s @ blocks[%d].ops[%d] 重放未复现"
                    % (rc.kind, bi, oi))
                continue
            witnesses = tuple(
                Witness(
                    kind=("loan" if f.mode is not None else "moved"),
                    origin_block=max(f.origin_block, 0),
                    origin_op=max(f.origin_op, 0),
                    block_chain=(f.chain if f.chain != (-1,) else (bi,)),
                    path=wpath,
                    loan_id=lid,
                    mode=f.mode)
                for f, wpath, lid in rc.items
                if f.origin_block != -1)
            conflicts.append(Conflict(
                kind=rc.kind, block=bi, op=oi,
                op_name=rc.op_name, path=rc.path,
                witnesses=witnesses,
                detail=_detail(ctx, rc.kind, bi, oi, rc.bad)))
            reported.add(key)
    return conflicts, evidences, unknown


def _pick_origin(rc: _RawConflict) -> Tuple[int, int]:
    real = [f for f, _, _ in rc.items if f.origin_block >= 0]
    if not real:
        return (0, 0)
    f = min(real, key=lambda x: (x.origin_block, x.origin_op))
    return f.origin_block, f.origin_op


def _detail(ctx: _Context, kind: str, bi: int, oi: int, bad: int) -> str:
    if ctx.tt.k:
        n = len(ctx.tt.models(bad))
        return (f"{kind} at blocks[{bi}].ops[{oi}]：在 {n} 个条件赋值下"
                "可达并已有界具体解释器现场复现（证据取其中一个可执行"
                "输入）")
    return f"{kind} at blocks[{bi}].ops[{oi}]（无条件，已具体复现）"
