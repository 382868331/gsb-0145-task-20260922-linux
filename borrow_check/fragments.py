"""片段模板、摘要与实例化（本次新增功能，复用主分析的数据流原语）。

片段是由已有指令组成的**无环**小 CFG（边只连向更大下标），只含显式路径
形参与局部 loan，不含 ``declare``；片段调用图也无环（不支持递归）。

自底向上把每个片段归约成一个 *模板* :class:`Template`：其全部传递调用都
已按形参命名空间内联展开，仍是前向 DAG；每个模板操作 :class:`TOp` 带
*label 链* ``((片段名, 块, 操作), ...)``，嵌套调用向外增长；模板内 loan
用 ``LoanKey``（诞生 label 链 + 片段内局部 loan_id）静态标识。

摘要 :class:`FragmentSummary` 在**形参命名空间**给出：

* 每个片段内操作点对入口状态的前置条件（入口可能已移动 / 入口活跃借用
  重叠），摘要本身**不假设形参互不重叠**；
* 与入口状态无关的形参命名空间内部冲突（may 语义）；
* 出口状态变化：片段体自身新增的移动（子树/精确），以及入口移动条目
  穿过片段（含分支并集与 reinit 拆分）后的存活表；
* 局部 loan 在片段出口一律结束，不逃逸到调用方。

调用点的**权威**检查由 :func:`instantiate` 完成：按实参路径把模板替换成
主命名空间下的具体无环 CFG，以调用方状态为入口跑单趟数据流。因此两个
形参绑定到同一记录的不同字段、或互相别名时，冲突判定与“真正展开”完全
一致，不依赖形参互不重叠的假设。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .analyzer import (
    _join,
    _State,
    _Transfer,
    _LoanInfo,
    all_type_paths,
)
from .errors import CFGValidationError
from .model import (
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    EndLoan,
    Fragment,
    Invoke,
    Move,
    Operation,
    Path,
    Program,
    Read,
    RecordNode,
    Write,
)

# 片段内定位三元组（片段名, 片段内块下标, 片段内操作下标）。
FragPoint = Tuple[str, int, int]
# label 链：外层被调用片段在前，向内增长。
Label = Tuple[FragPoint, ...]
# 模板内 loan 的静态键：(诞生点 label 链, 片段内原始 loan_id)。
LoanKey = Tuple[Label, str]

# 实例化规模上限（接口边界，README 中写明）。
MAX_TEMPLATE_OPS = 100_000


# --------------------------------------------------------------------------
# 模板中间表示
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TOp:
    """模板内的一条基本指令（invoke 已内联，故无 declare/invoke）。"""

    kind: str  # move / borrow_shared / borrow_mut / end_loan / read / write
    label: Label
    path: Optional[Path] = None
    reinit: bool = False
    loan: Optional[LoanKey] = None


@dataclass
class Template:
    """完全内联的片段模板：前向 DAG，路径在**顶层片段形参**命名空间。"""

    fragment: str
    param_tys: Dict[str, Optional[Dict[str, RecordNode]]]
    blocks: List[List[TOp]]
    successors: List[List[int]]
    entry: int
    loan_modes: Dict[LoanKey, Tuple[str, Path, Label]]


def loan_key_str(key: LoanKey) -> str:
    """模板 loan 键的确定性字符串化（测试/渲染用）。"""
    return "|".join(f"{n}@{b}.{o}" for n, b, o in key[0]) + "#" + key[1]


def _subst_path(path: Path, binding: Dict[str, Path]) -> Path:
    return (*binding[path[0]], *path[1:])


# --------------------------------------------------------------------------
# 自底向上构造模板（调用图无环已由 validator 保证）
# --------------------------------------------------------------------------


def build_templates(program: Program) -> Dict[str, Template]:
    order = _bottom_up_order(program)
    templates: Dict[str, Template] = {}
    total_ops = 0
    for name in order:
        tpl = _build_one(program.fragments[name], program, templates)
        total_ops += sum(len(b) for b in tpl.blocks)
        if total_ops > MAX_TEMPLATE_OPS:
            raise CFGValidationError(
                f"片段完全内联后的模板操作总数超过上限 {MAX_TEMPLATE_OPS}",
                f"fragment {name!r}")
        templates[name] = tpl
    return templates


def _bottom_up_order(program: Program) -> List[str]:
    color: Dict[str, int] = {n: 0 for n in program.fragments}
    order: List[str] = []

    def visit(name: str, stack: List[str]) -> None:
        if color[name] == 2:
            return
        if color[name] == 1:  # 防御：validator 应已拦截环
            raise CFGValidationError(
                f"片段调用图存在环：{' -> '.join(stack + [name])}", "fragment")
        color[name] = 1
        for block in program.fragments[name].blocks:
            for op in block.ops:
                if isinstance(op, Invoke):
                    visit(op.fragment, stack + [name])
        color[name] = 2
        order.append(name)

    for n in program.fragments:
        visit(n, [])
    return order


def _build_one(frag: Fragment, program: Program,
               templates: Dict[str, Template]) -> Template:
    param_tys = {p.name: p.ty for p in frag.params}

    out_blocks: List[List[TOp]] = []
    edges: List[Tuple[int, int]] = []
    region_first: List[int] = [0] * len(frag.blocks)
    region_last: List[int] = [0] * len(frag.blocks)
    loan_modes: Dict[LoanKey, Tuple[str, Path, Label]] = {}

    for bi, block in enumerate(frag.blocks):
        region_first[bi] = len(out_blocks)
        cur: List[TOp] = []
        # pending：等待连到“下一个落盘块”的前驱出口（上一个 invoke 的 callee
        # 出口集合）。invoke 前缀块 -> callee 入口的边另显式添加。
        pending: List[int] = []

        def emit(ops_list: List[TOp]) -> int:
            idx = len(out_blocks)
            out_blocks.append(ops_list)
            for e in pending:
                edges.append((e, idx))
            pending.clear()
            return idx

        for oi, op in enumerate(block.ops):
            anchor: FragPoint = (frag.name, bi, oi)
            label: Label = (anchor,)
            if isinstance(op, Invoke):
                # 落盘调用前缀（可能为空），并把它连到 callee 入口。
                pre_idx = emit(cur)
                cur = []
                sub = templates[op.fragment]
                callee = program.fragments[op.fragment]
                binding = {p.name: op.args[p.name] for p in callee.params}
                offset = len(out_blocks)
                sub_exits: List[int] = []
                for sbi, stops in enumerate(sub.blocks):
                    lowered: List[TOp] = []
                    for t in stops:
                        np = (_subst_path(t.path, binding)
                              if t.path is not None else None)
                        nlabel = (anchor, *t.label)
                        nloan: Optional[LoanKey] = None
                        if t.loan is not None:
                            lchain, local = t.loan
                            nloan = ((anchor, *lchain), local)
                            mode, lpath, _blabel = sub.loan_modes[t.loan]
                            loan_modes[nloan] = (
                                mode, _subst_path(lpath, binding), nloan[0])
                        lowered.append(TOp(t.kind, nlabel, np, t.reinit, nloan))
                    idx = len(out_blocks)
                    out_blocks.append(lowered)
                    if not sub.successors[sbi]:
                        sub_exits.append(idx)
                    for s in sub.successors[sbi]:
                        edges.append((idx, offset + s))
                edges.append((pre_idx, offset + sub.entry))
                # callee 出口等待连到调用后缀块。
                pending.extend(sub_exits)
            elif isinstance(op, Move):
                cur.append(TOp("move", label, path=op.path))
            elif isinstance(op, Read):
                cur.append(TOp("read", label, path=op.path))
            elif isinstance(op, Write):
                cur.append(TOp("write", label, path=op.path, reinit=op.reinit))
            elif isinstance(op, (BorrowShared, BorrowMut)):
                mode = "mut" if isinstance(op, BorrowMut) else "shared"
                key: LoanKey = (label, op.loan_id)
                loan_modes[key] = (mode, op.path, label)
                cur.append(TOp(f"borrow_{mode}", label, path=op.path, loan=key))
            elif isinstance(op, EndLoan):
                # 局部 loan_id 在片段内唯一；匹配诞生于本片段（label 链长度 1）
                # 且同片段名的 borrow 键。
                match = [k for k in loan_modes if k[1] == op.loan_id
                         and len(k[0]) == 1 and k[0][0][0] == frag.name]
                assert match, f"局部 loan {op.loan_id} 未找到（校验应已拦截）"
                cur.append(TOp("end_loan", label, loan=match[0]))
            else:  # pragma: no cover - Decl 已被 validator 拦截
                raise CFGValidationError(
                    f"片段内出现非法操作 {type(op).__name__}",
                    f"fragment {frag.name!r} blocks[{bi}].ops[{oi}]")
        # 块尾落盘（invoke 是最后一条时为空后缀块，接收 callee 出口）。
        emit(cur)
        region_last[bi] = len(out_blocks) - 1

    successors: List[List[int]] = [[] for _ in out_blocks]
    for u, v in edges:
        successors[u].append(v)
    for u, vs in enumerate(frag.successors):
        for v in vs:
            successors[region_last[u]].append(region_first[v])
    for lst in successors:
        lst.sort()

    return Template(
        fragment=frag.name,
        param_tys=param_tys,
        blocks=out_blocks,
        successors=successors,
        entry=region_first[frag.entry],
        loan_modes=loan_modes,
    )


# --------------------------------------------------------------------------
# 实例化：按实参路径把模板降成主命名空间下的具体无环 CFG
# --------------------------------------------------------------------------


@dataclass
class Instantiation:
    """一次调用点的具体展开。

    :param cfg: 降维后的真实 :class:`CFG`（可直接喂给既有 ``_Transfer``）。
    :param pos_label: 每个 (块, 操作) 对应的片段 label 链。
    :param fresh_loans: 本次实例化的运行时 loan_id ->
        ``(模式, 具体路径, label 链, 模板 LoanKey)``。每次调用都新鲜。
    """

    cfg: CFG
    pos_label: Dict[Tuple[int, int], Label]
    fresh_loans: Dict[str, Tuple[str, Path, Label, LoanKey]]


def instantiate(template: Template, binding: Dict[str, Path],
                id_prefix: str) -> Instantiation:
    """把形参命名空间模板替换为具体路径；loan_id 加调用点前缀（每次新鲜）。"""
    blocks: List[Block] = []
    pos_label: Dict[Tuple[int, int], Label] = {}
    fresh_loans: Dict[str, Tuple[str, Path, Label]] = {}

    for bi, tops in enumerate(template.blocks):
        ops: List[Operation] = []
        for oi, t in enumerate(tops):
            cpath = _subst_path(t.path, binding) if t.path is not None else None
            pos_label[(bi, oi)] = t.label
            runtime_id: Optional[str] = None
            if t.loan is not None:
                runtime_id = f"{id_prefix}::{loan_key_str(t.loan)}"
                mode, lpath, blabel = template.loan_modes[t.loan]
                fresh_loans[runtime_id] = (mode, _subst_path(lpath, binding),
                                           blabel, t.loan)
            if t.kind == "move":
                ops.append(Move(cpath))
            elif t.kind == "read":
                ops.append(Read(cpath))
            elif t.kind == "write":
                ops.append(Write(cpath, reinit=t.reinit))
            elif t.kind == "borrow_shared":
                ops.append(BorrowShared(runtime_id, cpath))
            elif t.kind == "borrow_mut":
                ops.append(BorrowMut(runtime_id, cpath))
            elif t.kind == "end_loan":
                ops.append(EndLoan(runtime_id))
            else:  # pragma: no cover
                raise AssertionError(t.kind)
        blocks.append(Block(ops=ops))

    cfg = CFG(blocks=blocks, successors=[list(s) for s in template.successors],
              entry=template.entry)
    return Instantiation(cfg=cfg, pos_label=pos_label, fresh_loans=fresh_loans)


# --------------------------------------------------------------------------
# 在调用点解释一次实例化（供主不动点分析调用）
# --------------------------------------------------------------------------


def _neg(block: int) -> int:
    """模板块下标的见证编码：-1-block，与主 CFG 的非负块下标永不混淆。"""
    return -1 - block


def _lift_frag(state: _State, succ_block: int) -> _State:
    """与 analyzer._lift 同语义，但追加的块下标用负数（片段内）编码。"""
    from .analyzer import Witness

    nxt = _neg(succ_block)

    def extend(chain: Tuple[int, ...]) -> Tuple[int, ...]:
        if nxt in chain:
            return chain[: chain.index(nxt) + 1]
        return (*chain, nxt)

    lifted = _State()
    for src, dst in ((state.moved_subtree, lifted.moved_subtree),
                     (state.moved_exact, lifted.moved_exact),
                     (state.loans, lifted.loans)):
        for key, w in src.items():
            dst[key] = Witness(
                kind=w.kind, origin_block=w.origin_block,
                origin_op=w.origin_op, block_chain=extend(w.block_chain),
                path=w.path, loan_id=w.loan_id, mode=w.mode,
                frag_chain=w.frag_chain)
    return lifted


def run_invocation(inst: Instantiation, entry: _State,
                   types: Dict[str, RecordNode],
                   caller_loan_info: Dict[str, _LoanInfo],
                   site: Tuple[int, int], *, collect: bool = True
                   ) -> Tuple[_State, list]:
    """以调用方状态 ``entry`` 为入口单趟解释实例化的无环 CFG。

    :returns: ``(出口状态, 原始冲突)``。出口状态中：
        * 局部 loan 已全部移除，只保留调用方逃逸 loan；
        * 片段内诞生的移动见证已重写到调用位置 ``site``，并带 ``frag_chain``；
        * 调用方来源见证的链被清洗为只含主 CFG 块（去掉片段内负编码段）。
        原始冲突的位置是实例化 CFG 的 (块, 操作)，由调用方做位置翻译。
    """
    from .analyzer import Witness, Conflict

    bi, oi = site
    n = len(inst.cfg.blocks)
    loan_info: Dict[str, _LoanInfo] = dict(caller_loan_info)
    for rid, (mode, cpath, _blabel, _key) in inst.fresh_loans.items():
        loan_info[rid] = _LoanInfo(block=-1, op=-1, mode=mode, path=cpath)

    reachable = _reachable_from(inst.cfg.successors, inst.cfg.entry, n)
    transfer = _Transfer(inst.cfg, types, loan_info, collect=collect)
    states: List[Optional[_State]] = [None] * n
    for idx in range(n):
        if idx not in reachable:
            continue
        if idx == inst.cfg.entry:
            st = entry.copy()
        else:
            st = _State()
            for p in range(idx):
                if idx in inst.cfg.successors[p] and states[p] is not None:
                    _join(st, _lift_frag(states[p], idx))  # type: ignore[arg-type]
        for opi in range(len(inst.cfg.blocks[idx].ops)):
            _step_neg(transfer, st, idx, opi)
        states[idx] = st

    # 出口：可达汇点块并集。
    out = _State()
    for idx in range(n):
        if idx in reachable and not inst.cfg.successors[idx] \
                and states[idx] is not None:
            _join(out, states[idx])  # type: ignore[arg-type]

    # loan：局部 loan 不逃逸；只留调用方 loan。
    caller_ids = set(caller_loan_info)
    out.loans = {lid: w for lid, w in out.loans.items() if lid in caller_ids}

    def clean_caller_chain(w: Witness) -> Witness:
        chain = tuple(x for x in w.block_chain if x >= 0)
        if not chain or chain[-1] != bi:
            chain = (*chain, bi)
        return Witness(
            kind=w.kind, origin_block=w.origin_block,
            origin_op=w.origin_op, block_chain=chain,
            path=w.path, loan_id=w.loan_id, mode=w.mode)

    # 片段内诞生（负 origin，模板块 0 编码为 -1）的移动见证重写到调用位置；
    # 其余（调用方来源，非负 origin）清洗链。
    def rewrite(w: Witness) -> Witness:
        if w.origin_block < 0:
            tb = -1 - w.origin_block
            label = inst.pos_label.get((tb, w.origin_op))
            return Witness(
                kind=w.kind, origin_block=bi, origin_op=oi,
                block_chain=(bi,), path=w.path, frag_chain=label)
        return clean_caller_chain(w)

    for d in (out.moved_subtree, out.moved_exact):
        for k in list(d):
            d[k] = rewrite(d[k])
    for lid in list(out.loans):
        out.loans[lid] = clean_caller_chain(out.loans[lid])

    return out, transfer.conflicts


def _reachable_from(successors: List[List[int]], entry: int, n: int) -> set:
    seen = {entry}
    stack = [entry]
    while stack:
        u = stack.pop()
        for v in successors[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def _step_neg(transfer: _Transfer, st: _State, tb: int, opi: int) -> None:
    """跑一个模板操作，但把诞生见证的块下标改成负数编码。"""
    from .analyzer import Witness

    real = transfer._born

    def neg_born(kind, block, op, path, loan_id=None, mode=None):
        w = real(kind, block, op, path, loan_id, mode)
        return Witness(kind=kind, origin_block=_neg(block), origin_op=op,
                       block_chain=(_neg(block),), path=path,
                       loan_id=loan_id, mode=mode)

    transfer._born = neg_born  # type: ignore[method-assign]
    try:
        op = transfer.cfg.blocks[tb].ops[opi]
        transfer._step(st, tb, opi, op)
    finally:
        transfer._born = real  # type: ignore[method-assign]


def translate_conflict(conflict, inst: Instantiation,
                       site: Tuple[int, int]) -> "object":
    """把实例化 CFG 内的冲突翻译为锚定调用位置、带片段 label 的 Conflict。"""
    from .analyzer import Witness, Conflict

    bi, oi = site
    label = inst.pos_label[(conflict.block, conflict.op)]
    witnesses = []
    for w in conflict.witnesses:
        if w.origin_block < 0:
            tb = -1 - w.origin_block
            flabel = inst.pos_label.get((tb, w.origin_op))
            frag_loan = None
            if w.loan_id is not None and w.loan_id in inst.fresh_loans:
                mode, _p, blabel, key = inst.fresh_loans[w.loan_id]
                frag_loan = (blabel, key[1], mode)
            witnesses.append(Witness(
                kind=w.kind, origin_block=bi, origin_op=oi,
                block_chain=(bi,), path=w.path,
                loan_id=None if frag_loan else w.loan_id, mode=w.mode,
                frag_chain=flabel, frag_loan=frag_loan))
        else:
            # 调用方来源：剥掉片段内（负）链段，调用块作为链尾。
            chain = tuple(x for x in w.block_chain if x >= 0)
            if not chain or chain[-1] != bi:
                chain = (*chain, bi)
            witnesses.append(Witness(
                kind=w.kind, origin_block=w.origin_block,
                origin_op=w.origin_op, block_chain=chain,
                path=w.path, loan_id=w.loan_id, mode=w.mode))
    # detail 文本中若引用了片段局部 loan 的运行时 id，替换为局部 id 展示。
    detail = conflict.detail
    for w in witnesses:
        if w.frag_loan is not None:
            local_id = w.frag_loan[1]
            # 运行时 id 形如 "c0_1#2::Touch@0.0#lp"：去掉调用点前缀。
            runtime_ids = [rid for rid in inst.fresh_loans]
            for rid in runtime_ids:
                if rid.endswith("#" + local_id):
                    detail = detail.replace(rid, local_id)
    return Conflict(
        kind=conflict.kind, block=bi, op=oi,
        op_name=f"invoke {label[-1][0]}",
        path=conflict.path, witnesses=tuple(witnesses),
        detail=detail + f"（片段内位置 "
               f"{' -> '.join(f'{n}[{b}.{o}]' for n, b, o in label)}）")


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Precondition:
    """片段内 ``label`` 处对入口状态的一个前置条件。

    :param moved: 真时要求入口移动状态使 path 不可用（方向性祖先/精确）。
    :param loan_mode: ``"mut_any"``（任何活跃借用重叠：move/write/borrow_mut）
        或 ``"mut_only"``（仅活跃可变借用：read/borrow_shared）；None=无。
    """

    label: Label
    op_name: str
    path: Path
    moved: bool
    loan_mode: Optional[str]


@dataclass(frozen=True)
class InternalConflict:
    kind: str
    label: Label
    path: Path


@dataclass
class FragmentSummary:
    fragment: str
    params: Tuple[str, ...]
    preconditions: Tuple[Precondition, ...]
    internal: Tuple[InternalConflict, ...]
    effect_subtree: Tuple[Path, ...]
    effect_exact: Tuple[Path, ...]
    survival_subtree: Dict[Path, Tuple[Tuple[str, Path], ...]]
    survival_exact: Dict[Path, Tuple[Tuple[str, Path], ...]]

    def describe(self) -> str:
        lines = [f"片段 {self.fragment} 摘要（形参 {', '.join(self.params)}）："]
        lines.append(f"  前置条件 {len(self.preconditions)} 条；"
                     f"形参命名空间内部冲突 {len(self.internal)} 个；"
                     f"出口新增子树移动 {len(self.effect_subtree)} 条、"
                     f"精确移动 {len(self.effect_exact)} 条；"
                     f"局部 loan 出口全部结束。")
        for pc in self.preconditions:
            where = " -> ".join(f"{n}[{b}.{o}]" for n, b, o in pc.label)
            cond = []
            if pc.moved:
                cond.append("入口可能已移动")
            if pc.loan_mode:
                cond.append("入口活跃借用重叠"
                            + ("（仅可变）" if pc.loan_mode == "mut_only" else ""))
            lines.append(f"    前置 @ {where} {pc.op_name} "
                         f"{'.'.join(pc.path)}：{' 且 '.join(cond)}")
        for ic in self.internal:
            where = " -> ".join(f"{n}[{b}.{o}]" for n, b, o in ic.label)
            lines.append(f"    内部冲突 {ic.kind} @ {where}：{'.'.join(ic.path)}")
        sub = ", ".join(map(_fmt, self.effect_subtree)) or "（无）"
        exact = ", ".join(map(_fmt, self.effect_exact)) or "（无）"
        lines.append(f"  出口新增移动：子树 [{sub}]；精确 [{exact}]")
        return "\n".join(lines)


def summarize_template(template: Template) -> FragmentSummary:
    """从模板形成形参命名空间摘要：前置条件 + 内部冲突 + 状态变化。"""
    types: Dict[str, RecordNode] = dict(template.param_tys)  # type: ignore
    params = tuple(template.param_tys)
    preconditions: List[Precondition] = []
    for tops in template.blocks:
        for t in tops:
            if t.kind == "end_loan":
                continue  # end_loan 不访问路径、无前置条件。
            assert t.path is not None
            preconditions.append(_precondition_of(t))

    # 以空入口实例化一次：内部冲突（与入口无关）+ 片段体出口新增移动。
    binding = {p: (p,) for p in params}
    inst = instantiate(template, binding, id_prefix=f"__sum_{template.fragment}")
    out, raw_conflicts = run_invocation(
        inst, _State(), types, caller_loan_info={},
        site=(-1, -1))
    internal: List[InternalConflict] = []
    for c in raw_conflicts:
        label = inst.pos_label[(c.block, c.op)]
        internal.append(InternalConflict(c.kind, label, c.path))
    internal = _dedup_internal(internal)
    effect_sub = tuple(sorted(out.moved_subtree))
    effect_exact = tuple(sorted(out.moved_exact))

    # 存活表：对形参类型树每条路径分别注入一个入口移动，单趟解释后
    # 只统计带注入标记（origin_block=-100）的出口条目。
    survival_subtree: Dict[Path, Tuple[Tuple[str, Path], ...]] = {}
    survival_exact: Dict[Path, Tuple[Tuple[str, Path], ...]] = {}
    for q in all_type_paths(types):
        survival_subtree[q] = _survival(template, types, "sub", q)
        survival_exact[q] = _survival(template, types, "exact", q)

    return FragmentSummary(
        fragment=template.fragment,
        params=params,
        preconditions=tuple(preconditions),
        internal=tuple(internal),
        effect_subtree=effect_sub,
        effect_exact=effect_exact,
        survival_subtree=survival_subtree,
        survival_exact=survival_exact,
    )


def _precondition_of(t: TOp) -> Precondition:
    assert t.path is not None
    k = t.kind
    if k == "move":
        return Precondition(t.label, "move", t.path, True, "mut_any")
    if k == "borrow_mut":
        return Precondition(t.label, "borrow_mut", t.path, True, "mut_any")
    if k == "borrow_shared":
        return Precondition(t.label, "borrow_shared", t.path, True, "mut_only")
    if k == "read":
        return Precondition(t.label, "read", t.path, True, "mut_only")
    if k == "write":
        return Precondition(t.label, "write", t.path, not t.reinit,
                            "mut_any")
    return Precondition(t.label, "end_loan", t.path, False, None)


def _dedup_internal(internal: List[InternalConflict]) -> List[InternalConflict]:
    seen: set = set()
    out: List[InternalConflict] = []
    for ic in internal:
        k = (ic.kind, ic.label, ic.path)
        if k not in seen:
            seen.add(k)
            out.append(ic)
    return out


def _survival(template: Template, types: Dict[str, RecordNode],
              kind: str, q: Path) -> Tuple[Tuple[str, Path], ...]:
    """注入单个入口移动条目，统计其在出口（可能经 reinit 拆分）的形态。"""
    from .analyzer import Witness

    MARK_BLOCK = -100
    BODY_BLOCK = -200
    binding = {p: (p,) for p in template.param_tys}
    inst = instantiate(template, binding, id_prefix=f"__sur_{template.fragment}")
    loan_info = {rid: _LoanInfo(-1, -1, m, p)
                 for rid, (m, p, _, _k) in inst.fresh_loans.items()}
    n = len(inst.cfg.blocks)
    states: List[Optional[_State]] = [None] * n
    reachable = _reachable_from(inst.cfg.successors, inst.cfg.entry, n)

    def is_marked(w: Witness) -> bool:
        return w.origin_block == MARK_BLOCK

    for idx in range(n):
        if idx not in reachable:
            continue
        if idx == inst.cfg.entry:
            st = _State()
        else:
            st = _State()
            for p in range(idx):
                if idx in inst.cfg.successors[p] and states[p] is not None:
                    _join(st, _lift_frag(states[p], idx))  # type: ignore[arg-type]
        if idx == inst.cfg.entry:
            marker = Witness(kind="moved", origin_block=MARK_BLOCK,
                             origin_op=-1, block_chain=(_neg(idx),), path=q)
            if kind == "sub":
                st.moved_subtree[q] = marker
            else:
                st.moved_exact[q] = marker
        transfer = _Transfer(inst.cfg, types, loan_info, collect=False)
        for opi, op in enumerate(inst.cfg.blocks[idx].ops):
            if isinstance(op, Move):
                w = Witness(kind="moved", origin_block=BODY_BLOCK,
                            origin_op=opi, block_chain=(_neg(idx),), path=op.path)
                transfer._add_move(st, op.path, w)
            elif isinstance(op, Write) and op.reinit:
                transfer._restore(st, op.path)
        states[idx] = st

    out: Dict[Tuple[str, Path], None] = {}
    for idx in range(n):
        if not inst.cfg.successors[idx] and states[idx] is not None:
            st = states[idx]  # type: ignore[assignment]
            for m, w in st.moved_subtree.items():
                if is_marked(w):
                    out.setdefault(("sub", m))
            for m, w in st.moved_exact.items():
                if is_marked(w):
                    out.setdefault(("exact", m))
    return tuple(sorted(out))


def _fmt(path: Path) -> str:
    return ".".join(path)


def build_summaries(program: Program) -> Dict[str, FragmentSummary]:
    """构造程序中所有片段的摘要（形参命名空间），按片段名索引。"""
    templates = build_templates(program)
    return {name: summarize_template(tpl) for name, tpl in templates.items()}
