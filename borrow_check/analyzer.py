"""核心：字段敏感 may-借用检查的单调不动点分析。

抽象状态（每个块的入口/出口各一份）：

* ``moved``：可能已移动路径，分两种条目：

  - *子树移动*（``move(p)`` 产生）：压制 ``p`` 及其全部子孙，集合保持反链；
  - *精确移动*：只压制路径本身、不压子孙。它只在 reinit 拆分祖先移动时
    产生——恢复 ``p`` 子树后，祖先 ``m`` 根对象本身仍可能已移动
    （即使该路径在类型树中已无其他兄弟字段），其余兄弟仍是子树移动。

  路径 ``q`` 因移动不可用当且仅当存在子树移动的祖先，或存在精确移动等于
  ``q``。方向性：移动子路径不使父路径不可用（不同字段独立）。
* ``loans``：可能仍活跃的 loan_id 集合（每个 id 的模式与路径在唯一 borrow 点固定）。
  借用冲突使用**对称重叠**（互为前缀即冲突），与移动可用性的方向规则区分。

分支合并 = 两者的并集（可能已移动 / 可能仍活跃），传输函数关于入口状态
的语义外延单调（reinit 的 kill 集合只依赖操作本身，不依赖入口状态），
因此在有限格上 worklist 迭代必达不动点，且不靠遍历深度截断。块处理次数
有可证明上界 ``E*(P+L) + n``（E 边数、P 类型树路径数、L loan 数、n 块数），
预算耗尽与“分析完成且无冲突”严格区分。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import BudgetExhaustedError
from .model import (
    BorrowMut,
    BorrowShared,
    CFG,
    Decl,
    EndLoan,
    Move,
    Operation,
    Path,
    Read,
    RecordNode,
    Write,
)

# --------------------------------------------------------------------------
# 见证（前驱链）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Witness:
    """一个抽象元素到达冲突点的前驱链。

    :param kind: ``"moved"``（可能已移动路径）或 ``"loan"``（可能仍活跃借用）。
    :param origin_block / origin_op: 元素诞生点（move 操作或 borrow 操作）。
    :param block_chain: 从诞生块到冲突块（均含）的块下标链，例如
        ``(0, 2, 3)`` 表示元素在块0产生，经 0→2→3 到达块3 的冲突点。
    :param path: 元素作用路径。
    :param loan_id / mode: loan 元素的 id 与 ``"shared"``/``"mut"``。
    """

    kind: str
    origin_block: int
    origin_op: int
    block_chain: Tuple[int, ...]
    path: Path
    loan_id: Optional[str] = None
    mode: Optional[str] = None


@dataclass(frozen=True)
class Conflict:
    """一次冲突。

    :param kind: 冲突种类（见模块文档与 README）：
        ``use_of_moved`` / ``move_while_borrowed`` / ``borrow_conflict`` /
        ``read_while_mut_borrowed`` / ``write_while_borrowed``。
    :param block / op: 冲突操作位置。
    :param witnesses: 导致冲突的前驱见证（可能多条，如同时可能已移动
        且存在活跃可变借用）。
    """

    kind: str
    block: int
    op: int
    op_name: str
    path: Optional[Path]
    witnesses: Tuple[Witness, ...]
    detail: str


@dataclass
class CheckResult:
    """check 的结果。

    :param clean: 无任何冲突为 ``True``（这是“证明无冲突”路径，
        与 :class:`BudgetExhaustedError` 的预算耗尽路径严格不同）。
    :param conflicts: 按 (块, 操作) 顺序排列的全部冲突。
    :param iterations: 到达不动点前的块处理次数（不含最终见证收集扫描）。
    """

    clean: bool
    conflicts: List[Conflict]
    iterations: int

    def explain(self) -> str:
        """渲染人类可读的冲突来源链（demo 使用）。"""
        return "\n".join(_render_conflict(c) for c in self.conflicts)


# --------------------------------------------------------------------------
# 抽象状态
# --------------------------------------------------------------------------


@dataclass
class _State:
    # 子树移动（move 产生，压制自身及子孙），保持反链。
    moved_subtree: Dict[Path, Witness] = field(default_factory=dict)
    # 精确移动（只压制自身；reinit 拆分祖先移动时产生）。
    moved_exact: Dict[Path, Witness] = field(default_factory=dict)
    # loans: loan_id -> 见证。
    loans: Dict[str, Witness] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(dict(self.moved_subtree), dict(self.moved_exact), dict(self.loans))


def _is_prefix(a: Path, b: Path) -> bool:
    """a 是 b 的前缀（含相等）。"""
    return len(a) <= len(b) and b[: len(a)] == a


def _overlaps(a: Path, b: Path) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


def _normalize_subtree(moved: Dict[Path, Witness]) -> Dict[Path, Witness]:
    """子树移动集合化为反链：祖先存在时丢弃子孙（祖先已压制子孙）。"""
    keys = list(moved)
    keep: Dict[Path, Witness] = {}
    for i, k in enumerate(keys):
        dominated = False
        for j, other in enumerate(keys):
            if i != j and _is_prefix(other, k) and other != k:
                dominated = True
                break
        if not dominated:
            keep[k] = moved[k]
    return keep


def _union_subtree(a: Dict[Path, Witness], b: Dict[Path, Witness]) -> Dict[Path, Witness]:
    """并集后反链化；键已存在时保留 a 的见证（前驱按确定序处理）。"""
    merged = dict(a)
    for k, w in b.items():
        merged.setdefault(k, w)
    return _normalize_subtree(merged)


# --------------------------------------------------------------------------
# 记录树辅助
# --------------------------------------------------------------------------


def _node_at(types: Dict[str, RecordNode], path: Path) -> RecordNode:
    node: RecordNode = types.get(path[0], None)
    for seg in path[1:]:
        assert isinstance(node, dict)
        node = node[seg]
    return node


def all_type_paths(types: Dict[str, RecordNode]) -> List[Path]:
    """枚举记录树中全部合法路径（根 + 每个字段节点）。"""
    result: List[Path] = []
    for root, ty in types.items():
        result.append((root,))
        if isinstance(ty, dict):
            for f, sub in ty.items():
                result.extend((root, *p) for p in _all_subpaths(sub, (f,)))
    return result


def _all_subpaths(node: RecordNode, prefix: Path) -> List[Path]:
    out: List[Path] = [prefix]
    if isinstance(node, dict):
        for f, sub in node.items():
            out.extend(_all_subpaths(sub, (*prefix, f)))
    return out


# --------------------------------------------------------------------------
# 块内传输
# --------------------------------------------------------------------------


@dataclass
class _LoanInfo:
    block: int
    op: int
    mode: str
    path: Path


class _Transfer:
    def __init__(
        self,
        cfg: CFG,
        types: Dict[str, RecordNode],
        loan_info: Dict[str, _LoanInfo],
        collect: bool,
    ) -> None:
        self.cfg = cfg
        self.types = types
        self.loan_info = loan_info
        self.collect = collect
        self.conflicts: List[Conflict] = []

    # -- 见证辅助 ----------------------------------------------------------

    def _born(self, kind: str, block: int, op: int, path: Path,
              loan_id: Optional[str] = None, mode: Optional[str] = None) -> Witness:
        return Witness(
            kind=kind,
            origin_block=block,
            origin_op=op,
            block_chain=(block,),
            path=path,
            loan_id=loan_id,
            mode=mode,
        )

    def _conflict(self, kind: str, block: int, op: int, op_name: str,
                  path: Optional[Path], detail: str,
                  witnesses: Sequence[Witness]) -> None:
        if self.collect:
            self.conflicts.append(
                Conflict(
                    kind=kind,
                    block=block,
                    op=op,
                    op_name=op_name,
                    path=path,
                    witnesses=tuple(witnesses),
                    detail=detail,
                )
            )

    # -- moved 集合操作 ----------------------------------------------------

    def _add_move(self, st: _State, path: Path, witness: Witness) -> None:
        """加入 move(path)（子树移动）。

        已有子树祖先移动则语义冗余；否则删除被新祖先覆盖的子孙子树移动
        与落在子树内的精确移动。
        """
        for m in st.moved_subtree:
            if _is_prefix(m, path):
                return
        for m in [k for k in st.moved_subtree if _is_prefix(path, k) and k != path]:
            del st.moved_subtree[m]
        for q in [k for k in st.moved_exact if _is_prefix(path, k)]:
            del st.moved_exact[q]
        st.moved_subtree[path] = witness

    def _restore(self, st: _State, path: Path) -> None:
        """reinit(path)：只恢复 path 子树。

        * 落在 path 子树内（含 path）的子树移动/精确移动删除；
        * 与 path 不交的条目保留；
        * path 的严格祖先子树移动被拆分：祖先对象本身记为**精确移动**
          （根对象仍可能已移动），通往 path 沿途的兄弟字段各记为子树移动；
        * path 的祖先精确移动只压制祖先自身，与恢复无关，保留。
        """
        new_sub: Dict[Path, Witness] = {}
        new_exact: Dict[Path, Witness] = dict(st.moved_exact)
        new_exact.pop(path, None)
        for q in [k for k in new_exact if _is_prefix(path, k) and k != path]:
            del new_exact[q]

        for m, w in st.moved_subtree.items():
            if m == path or _is_prefix(path, m):
                continue  # path 子树恢复。
            if _is_prefix(m, path):  # 严格祖先
                new_exact.setdefault(m, w)  # 祖先对象本身仍可能已移动。
                for depth in range(len(m), len(path)):
                    prefix = path[:depth]
                    node = _node_at(self.types, prefix)
                    if isinstance(node, dict):
                        nxt = path[depth]
                        for f in node:
                            if f != nxt:
                                new_sub.setdefault((*prefix, f), w)
            else:
                new_sub.setdefault(m, w)

        st.moved_subtree = _normalize_subtree(new_sub)
        # 被子树移动覆盖的精确移动是冗余的。
        redundant_exact = [
            k for k in new_exact
            if any(_is_prefix(m, k) for m in st.moved_subtree)
        ]
        for k in redundant_exact:
            del new_exact[k]
        st.moved_exact = new_exact

    # -- 移动可用性（有方向：祖先移动压制子孙；子孙移动不使祖先不可用）----

    def _disabled_by_move(self, st: _State, path: Path) -> List[Tuple[Path, Witness]]:
        """path 因可能已移动而不可用：存在 path 的子树移动祖先，
        或 path 本身的精确移动。

        题面契约：“移动父路径使所有子路径不可用”。移动子路径不使父路径
        不可用（部分移动的父路径其余字段仍可独立访问）。
        """
        hits = [(m, w) for m, w in st.moved_subtree.items() if _is_prefix(m, path)]
        if path in st.moved_exact:
            hits.append((path, st.moved_exact[path]))
        return sorted(hits)

    def _overlapping_loans(self, st: _State, path: Path,
                           *, mut_only: bool = False) -> List[Tuple[str, Witness]]:
        out = []
        for lid, w in st.loans.items():
            info = self.loan_info[lid]
            if _overlaps(info.path, path):
                if mut_only and info.mode != "mut":
                    continue
                out.append((lid, w))
        out.sort(key=lambda x: x[0])
        return out

    # -- 主循环 ------------------------------------------------------------

    def run_block(self, block_idx: int, in_state: _State) -> _State:
        st = in_state.copy()
        block = self.cfg.blocks[block_idx]
        for oi, op in enumerate(block.ops):
            self._step(st, block_idx, oi, op)
        return st

    def _step(self, st: _State, bi: int, oi: int, op: Operation) -> None:
        if isinstance(op, Decl):
            return

        if isinstance(op, Move):
            moved_hits = self._disabled_by_move(st, op.path)
            for _, w in moved_hits:
                self._conflict(
                    "use_of_moved", bi, oi, "move", op.path,
                    f"move 的路径 {_fmt(op.path)} 可能已被移动", [w])
            loan_hits = self._overlapping_loans(st, op.path)
            for _, w in loan_hits:
                self._conflict(
                    "move_while_borrowed", bi, oi, "move", op.path,
                    f"move 的路径 {_fmt(op.path)} 与活跃借用重叠", [w])
            self._add_move(st, op.path, self._born("moved", bi, oi, op.path))
            return

        if isinstance(op, (BorrowShared, BorrowMut)):
            mode = "mut" if isinstance(op, BorrowMut) else "shared"
            for _, w in self._disabled_by_move(st, op.path):
                self._conflict(
                    "use_of_moved", bi, oi, f"borrow_{mode}", op.path,
                    f"借用路径 {_fmt(op.path)} 可能已被移动", [w])
            mut_only = mode == "shared"  # shared 只与 mut 冲突；mut 与任何 loan 冲突
            hits = self._overlapping_loans(st, op.path, mut_only=mut_only)
            for lid, w in hits:
                other_mode = self.loan_info[lid].mode
                self._conflict(
                    "borrow_conflict", bi, oi, f"borrow_{mode}", op.path,
                    f"{mode} 借用 {_fmt(op.path)} 与活跃{('可变' if other_mode == 'mut' else '共享')}"
                    f"借用 {lid}@{_fmt(self.loan_info[lid].path)} 重叠", [w])
            st.loans[op.loan_id] = self._born(
                "loan", bi, oi, op.path, loan_id=op.loan_id, mode=mode)
            return

        if isinstance(op, EndLoan):
            st.loans.pop(op.loan_id, None)
            return

        if isinstance(op, Read):
            for _, w in self._disabled_by_move(st, op.path):
                self._conflict(
                    "use_of_moved", bi, oi, "read", op.path,
                    f"读取的路径 {_fmt(op.path)} 可能已被移动", [w])
            for _, w in self._overlapping_loans(st, op.path, mut_only=True):
                self._conflict(
                    "read_while_mut_borrowed", bi, oi, "read", op.path,
                    f"读取 {_fmt(op.path)} 时存在重叠的活跃可变借用", [w])
            return

        if isinstance(op, Write):
            for _, w in self._overlapping_loans(st, op.path):
                self._conflict(
                    "write_while_borrowed", bi, oi, "write", op.path,
                    f"写入 {_fmt(op.path)} 时存在重叠的活跃借用", [w])
            if op.reinit:
                # 即使借用冲突也按“错误恢复”应用声明的效果，见 README 设计取舍。
                self._restore(st, op.path)
            else:
                for _, w in self._disabled_by_move(st, op.path):
                    self._conflict(
                        "use_of_moved", bi, oi, "write", op.path,
                        f"写入的路径 {_fmt(op.path)} 可能已被移动（恢复性写入需 reinit=True）", [w])
            return


def _fmt(path: Path) -> str:
    return ".".join(path)


def _lift(state: _State, succ_block: int) -> _State:
    """块出口状态沿边传播：所有见证链追加后继块。

    追加后规范化为**简单路径**：may 分析中元素沿一条含环游走到达，删去环后
    仍是一条合法的前驱见证（环走零次也成立），因此输出不依赖迭代次序、
    也不靠深度截断来保证终止。
    """
    def extend(chain: Tuple[int, ...], nxt: int) -> Tuple[int, ...]:
        if nxt in chain:
            return chain[: chain.index(nxt) + 1]
        return (*chain, nxt)

    lifted = _State()
    for src, dst in ((state.moved_subtree, lifted.moved_subtree),
                     (state.moved_exact, lifted.moved_exact),
                     (state.loans, lifted.loans)):
        for key, w in src.items():
            dst[key] = Witness(
                kind=w.kind, origin_block=w.origin_block, origin_op=w.origin_op,
                block_chain=extend(w.block_chain, succ_block),
                path=w.path, loan_id=w.loan_id, mode=w.mode)
    return lifted


def _join(into: _State, other: _State) -> bool:
    """并集合并到 into（两类移动条目 + 活跃 loan）；返回外延键集合是否增长。"""
    grown = False
    old_sub = set(into.moved_subtree)
    old_exact = set(into.moved_exact)
    # 先并精确条目，再用子树移动并集反链化，最后剔除被子树覆盖的冗余精确条目。
    for q, w in other.moved_exact.items():
        into.moved_exact.setdefault(q, w)
    into.moved_subtree = _union_subtree(into.moved_subtree, other.moved_subtree)
    for q in [k for k in into.moved_exact
              if any(_is_prefix(m, k) for m in into.moved_subtree)]:
        del into.moved_exact[q]
    if set(into.moved_subtree) != old_sub or set(into.moved_exact) != old_exact:
        grown = True
    for lid, w in other.loans.items():
        if lid not in into.loans:
            into.loans[lid] = w
            grown = True
    return grown


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------


def check(cfg: CFG, *, max_iterations: Optional[int] = None) -> CheckResult:
    """对 CFG 运行字段敏感静态借用检查。

    :param cfg: 已通过结构构造的 :class:`CFG`（``check`` 会先做完整语义校验）。
    :param max_iterations: 块处理预算。默认取可证明上界
        ``n * (P + L) + n``（n 块数、P 类型树路径数、L loan 数），
        合法 CFG 不可能耗尽。传入更小的正数可在测试中演示
        :class:`BudgetExhaustedError` 与“无冲突”结论的区别。
    :raises CFGValidationError: 非法输入（携带 location）。
    :raises BudgetExhaustedError: 搜索预算耗尽。
    """
    from .validator import validate_cfg
    from .errors import checked_int

    validate_cfg(cfg)

    if max_iterations is not None:
        checked_int("max_iterations", max_iterations, min_value=1)

    n = len(cfg.blocks)
    types: Dict[str, RecordNode] = {}
    loan_info: Dict[str, _LoanInfo] = {}
    for bi, block in enumerate(cfg.blocks):
        for oi, op in enumerate(block.ops):
            if isinstance(op, Decl):
                types[op.var] = op.ty
            elif isinstance(op, (BorrowShared, BorrowMut)):
                mode = "mut" if isinstance(op, BorrowMut) else "shared"
                loan_info[op.loan_id] = _LoanInfo(bi, oi, mode, op.path)

    p_count = len(all_type_paths(types))
    l_count = len(loan_info)
    universe = all_type_paths(types)
    edge_count = sum(len(s) for s in cfg.successors)
    # 单调 worklist 的可证步数上界：每块出口外延至多严格增长 P+L 次；
    # 块 i 每次增长至多使 outdeg(i) 个后继入队，总入队数 ≤ E·(P+L)；
    # 再加初始 n 次（含不可达块的空状态分析）。
    bound = edge_count * (p_count + l_count) + n
    budget = bound if max_iterations is None else max_iterations

    predecessors: Dict[int, List[int]] = {i: [] for i in range(n)}
    for bi, succs in enumerate(cfg.successors):
        for s in succs:
            predecessors[s].append(bi)
    for preds in predecessors.values():
        preds.sort()

    # 记录每块抽象入口状态与每块出口的语义外延（用于单调变更判定）。
    ins: List[_State] = [_State() for _ in range(n)]
    outs: Dict[int, _State] = {}
    out_denot: Dict[int, Tuple[frozenset[Path], frozenset[str]]] = {}
    iterations = 0

    def process(idx: int) -> bool:
        nonlocal iterations
        iterations += 1
        if iterations > budget:
            raise BudgetExhaustedError(
                f"不动点搜索预算耗尽：已处理 {budget} 个块次仍未收敛"
                f"（可证明上界为 {bound}）。这不是“无冲突”证明。")
        merged = _State()
        for p in predecessors[idx]:
            if p in outs:
                _join(merged, _lift(outs[p], idx))
        ins[idx] = merged
        transfer = _Transfer(cfg, types, loan_info, collect=False)
        new_out = transfer.run_block(idx, merged)
        new_denot = _denotation(new_out, universe)
        changed = idx not in out_denot or out_denot[idx] != new_denot
        outs[idx] = new_out
        out_denot[idx] = new_denot
        return changed

    # 初始排入所有块：不可达块也以空入口状态被分析一次（之后不会被前驱更新）。
    worklist: deque[int] = deque(range(n))
    seen: set[int] = set(range(n))
    while worklist:
        idx = worklist.popleft()
        seen.discard(idx)
        changed = process(idx)
        if changed:
            for s in cfg.successors[idx]:
                if s not in seen:
                    worklist.append(s)
                    seen.add(s)

    # 不动点已到：用最终入口状态做一次有序扫描，权威地收集冲突与见证。
    conflicts: List[Conflict] = []
    for bi in range(n):
        transfer = _Transfer(cfg, types, loan_info, collect=True)
        transfer.run_block(bi, ins[bi])
        conflicts.extend(transfer.conflicts)

    return CheckResult(clean=not conflicts, conflicts=conflicts, iterations=iterations)


def _denotation(st: _State, universe: List[Path]) -> Tuple[frozenset[Path], frozenset[str]]:
    """抽象状态的语义外延：被压制的全部类型路径 + 活跃 loan 集合。

    传输函数在外延包含序下单调（反链键集合本身不单调：祖先移动会替换
    子孙键，但其覆盖集合只增不减）。worklist 的变更判定与步数上界都
    建立在外延上。
    """
    covered: set[Path] = set()
    for m in st.moved_subtree:
        for q in universe:
            if _is_prefix(m, q):  # 子树移动：祖先压制子孙（方向性）
                covered.add(q)
    for q in st.moved_exact:  # 精确移动：只压制自身（q 必为类型树路径）
        covered.add(q)
    return frozenset(covered), frozenset(st.loans)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _render_conflict(c: Conflict) -> str:
    head = f"冲突[{c.kind}] @ blocks[{c.block}].ops[{c.op}] ({c.op_name}): {c.detail}"
    lines = [head]
    for w in c.witnesses:
        chain = " -> ".join(f"blocks[{b}]" for b in w.block_chain)
        if w.kind == "moved":
            origin = (f"move {_fmt(w.path)} 诞生于 "
                      f"blocks[{w.origin_block}].ops[{w.origin_op}]")
        else:
            origin = (f"{w.mode} 借用 {w.loan_id}@{_fmt(w.path)} 开始于 "
                      f"blocks[{w.origin_block}].ops[{w.origin_op}]，之后未被 end_loan")
        lines.append(f"    前驱见证: {origin}")
        lines.append(f"              传播链: {chain} -> blocks[{c.block}].ops[{c.op}]")
    return "\n".join(lines)
