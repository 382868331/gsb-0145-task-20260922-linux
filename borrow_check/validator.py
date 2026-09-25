"""CFG 语义校验。

在任何分析状态建立之前完成全部校验，因此 ``check`` 对非法输入只抛出
:class:`CFGValidationError`，不会产生部分结果或部分变更。

校验内容：块数 1..50；边下标合法且不重复；entry 合法；操作类型合法；
根变量恰好 declare 一次；declare 必须在 CFG 上**支配**每个使用点
（块内要求操作下标不早于声明，跨块要求声明块支配使用块；不可达块豁免）；
所有路径在记录树中存在；loan_id 全局唯一对应一个 borrow，
end_loan 必须引用已存在的 loan。
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .errors import CFGValidationError, checked_int
from .model import (
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    Call,
    Decl,
    EndLoan,
    Move,
    Operation,
    Path,
    Program,
    Read,
    Segment,
    Write,
)

MAX_BLOCKS = 50
MAX_OPS_PER_BLOCK = 10_000
MAX_TYPE_NODES = 100_000


def _check_path_shape(path: Path, loc: str) -> None:
    if not isinstance(path, tuple, ):
        raise CFGValidationError("内部路径必须是 tuple", loc)
    if len(path) == 0:
        raise CFGValidationError("路径至少包含根变量名", loc)
    for seg in path:
        if not isinstance(seg, str) or seg == "":
            raise CFGValidationError(f"路径段必须是非空字符串，得到 {seg!r}", loc)


def _check_type(ty: object, loc: str, _counter: List[int]) -> None:
    _counter[0] += 1
    if _counter[0] > MAX_TYPE_NODES:
        raise CFGValidationError(
            f"记录树节点数超过上限 {MAX_TYPE_NODES}", loc
        )
    if ty is None:
        return
    if not isinstance(ty, dict):
        raise CFGValidationError("记录类型必须是 dict 或 null", loc)
    for key, sub in ty.items():
        if not isinstance(key, str) or key == "":
            raise CFGValidationError("字段名必须是非空字符串", f"{loc}[{key!r}]")
        _check_type(sub, f"{loc}.{key}", _counter)


def validate_cfg(cfg: CFG, segments: Optional[Dict[str, Segment]] = None) -> None:
    """对 :class:`CFG` 做完整结构与语义校验，非法即抛 :class:`CFGValidationError`。

    :param segments: 命名片段表。主 CFG 只允许在提供片段表（即作为
        :class:`Program` 的一部分）时出现 ``call`` 操作；此时还会校验
        调用的片段存在、形参名集合精确匹配、实参路径合法。
        为 ``None``（直接 ``check(cfg)``/``cfg_from_dict``）时出现 ``call``
        即报错——片段调用必须经 :class:`Program` 提供片段定义。
    """
    if not isinstance(cfg, CFG):
        raise CFGValidationError("需要 borrow_check.CFG 实例（可用 cfg_from_dict 构造）", "cfg")
    blocks = cfg.blocks
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= MAX_BLOCKS):
        raise CFGValidationError(f"blocks 必须是 1..{MAX_BLOCKS} 个 Block 的列表", "cfg.blocks")

    declared: Dict[str, Decl] = {}
    decl_site: Dict[str, Tuple[int, int]] = {}
    loan_site: Dict[str, Operation] = {}
    op_path_users: List[Tuple[int, int, Operation, Path]] = []
    call_sites: List[Tuple[int, int, Call]] = []
    type_node_counter = [0]

    for bi, block in enumerate(blocks):
        bloc = f"blocks[{bi}]"
        if not isinstance(block, Block):
            raise CFGValidationError("块必须是 Block 实例", bloc)
        ops = block.ops
        if not isinstance(ops, list) or len(ops) > MAX_OPS_PER_BLOCK:
            raise CFGValidationError(
                f"ops 必须是列表且长度 <= {MAX_OPS_PER_BLOCK}", f"{bloc}.ops"
            )

        for oi, op in enumerate(ops):
            oloc = f"{bloc}.ops[{oi}]"
            if not isinstance(op, Operation):
                raise CFGValidationError(
                    f"非法操作对象 {type(op).__name__}，请使用 model 中的操作数据类", oloc
                )
            if isinstance(op, Decl):
                if not isinstance(op.var, str) or op.var == "":
                    raise CFGValidationError("declare.var 必须是非空字符串", f"{oloc}.var")
                _check_type(op.ty, f"{oloc}.ty", type_node_counter)
                if op.var in declared:
                    raise CFGValidationError(
                        f"根变量 {op.var!r} 被重复 declare（每个根变量只能声明一次）", oloc
                    )
                declared[op.var] = op
                decl_site[op.var] = (bi, oi)
            elif isinstance(op, (Move, Read)):
                _check_path_shape(op.path, f"{oloc}.path")
                op_path_users.append((bi, oi, op, op.path))
            elif isinstance(op, Write):
                _check_path_shape(op.path, f"{oloc}.path")
                if not isinstance(op.reinit, bool):
                    raise CFGValidationError("write.reinit 必须是 bool", f"{oloc}.reinit")
                op_path_users.append((bi, oi, op, op.path))
            elif isinstance(op, (BorrowShared, BorrowMut)):
                if not isinstance(op.loan_id, str) or op.loan_id == "":
                    raise CFGValidationError("loan_id 必须是非空字符串", f"{oloc}.loan_id")
                if op.loan_id in loan_site:
                    raise CFGValidationError(
                        f"loan_id {op.loan_id!r} 不唯一：每个 loan_id 只能有一个 borrow 点",
                        f"{oloc}.loan_id",
                    )
                loan_site[op.loan_id] = op
                _check_path_shape(op.path, f"{oloc}.path")
                op_path_users.append((bi, oi, op, op.path))
            elif isinstance(op, EndLoan):
                if not isinstance(op.loan_id, str) or op.loan_id == "":
                    raise CFGValidationError("loan_id 必须是非空字符串", f"{oloc}.loan_id")
            elif isinstance(op, Call):
                _validate_call_op(op, oloc, segments)
                # 实参路径与普通使用点一样做根变量/字段存在性与支配校验。
                for _, apath in op.args:
                    _check_path_shape(apath, f"{oloc}.args")
                    op_path_users.append((bi, oi, op, apath))
                call_sites.append((bi, oi, op))
            else:  # pragma: no cover - 防御性分支
                raise CFGValidationError(f"未知操作类型 {type(op).__name__}", oloc)

        block_path_ops = [(oi, op) for oi, op in enumerate(ops)
                         if not isinstance(op, Decl) and hasattr(op, "path")]
        for ui, uop in block_path_ops:
            op_path_users.append((bi, ui, uop, uop.path))

    # end_loan 引用的 loan 必须存在。
    for bi, block in enumerate(blocks):
        for oi, op in enumerate(block.ops):
            if isinstance(op, EndLoan) and op.loan_id not in loan_site:
                raise CFGValidationError(
                    f"end_loan 引用了不存在的 loan_id {op.loan_id!r}",
                    f"blocks[{bi}].ops[{oi}].loan_id",
                )

    # 路径根变量与字段存在性。
    for bi, oi, op, path in op_path_users:
        is_call = isinstance(op, Call)
        oloc = f"blocks[{bi}].ops[{oi}]{'.args' if is_call else '.path'}"
        root = path[0]
        if root not in declared:
            raise CFGValidationError(f"路径根变量 {root!r} 未声明", oloc)
        node = declared[root].ty
        for fi, field in enumerate(path[1:], start=1):
            if node is None:
                raise CFGValidationError(
                    f"路径 {_fmt_path(path)} 在第 {fi} 段越界：{path[fi - 1]!r} 是叶子",
                    oloc,
                )
            if field not in node:
                raise CFGValidationError(
                    f"记录类型中不存在字段 {field!r}（路径 {_fmt_path(path)}）", oloc
                )
            node = node[field]

    # 调用：形参路径替换为实参后，片段内每条路径都必须在实参记录树中存在。
    if segments is not None:
        for bi, oi, call in call_sites:
            _check_call_paths_exist(
                call, segments[call.segment], declared,
                f"blocks[{bi}].ops[{oi}]")

    # 边与入口。
    succs = cfg.successors
    if not isinstance(succs, list) or len(succs) != len(blocks):
        raise CFGValidationError("successors 必须是与 blocks 等长的列表", "cfg.successors")
    for bi, succ in enumerate(succs):
        sloc = f"successors[{bi}]"
        if not isinstance(succ, list):
            raise CFGValidationError("后继必须是列表", sloc)
        seen: Set[int] = set()
        for s in succ:
            checked_int("successor", s, min_value=0)
            if s >= len(blocks):
                raise CFGValidationError(f"后继下标 {s} 越界（共 {len(blocks)} 块）", sloc)
            if s in seen:
                raise CFGValidationError(f"后继 {s} 重复", sloc)
            seen.add(s)
    checked_int("entry", cfg.entry, min_value=0)
    if cfg.entry >= len(blocks):
        raise CFGValidationError(f"entry {cfg.entry} 越界", "cfg.entry")

    # 声明次序：declare 必须支配每个使用点（块级支配 + 块内下标次序）。
    # 不可达块不会被任何具体执行到达，豁免块级支配要求。
    reachable = _reachable_blocks(succs, cfg.entry)
    dominators = _dominators(succs, cfg.entry, reachable)
    for bi, oi, op, path in op_path_users:
        root = path[0]
        if root not in decl_site:
            continue  # “未声明”已在上面的路径检查中报错
        dblock, dop = decl_site[root]
        if bi not in reachable:
            continue
        if dblock not in dominators[bi]:
            suffix = ".args" if isinstance(op, Call) else ".path"
            raise CFGValidationError(
                f"根变量 {root!r} 的 declare(blocks[{dblock}].ops[{dop}]) "
                f"不支配使用点 blocks[{bi}].ops[{oi}]：存在不经过声明的控制路径",
                f"blocks[{bi}].ops[{oi}]{suffix}",
            )
        if dblock == bi and dop > oi:
            suffix = ".args" if isinstance(op, Call) else ".path"
            raise CFGValidationError(
                f"根变量 {root!r} 在本块 declare 之前被使用",
                f"blocks[{bi}].ops[{oi}]{suffix}",
            )


def _reachable_blocks(succs: List[List[int]], entry: int) -> Set[int]:
    seen: Set[int] = {entry}
    queue: deque[int] = deque([entry])
    while queue:
        b = queue.popleft()
        for s in succs[b]:
            if s not in seen:
                seen.add(s)
                queue.append(s)
    return seen


def _dominators(succs: List[List[int]], entry: int,
                reachable: Set[int]) -> List[Set[int]]:
    """经典支配集不动点：Dom(entry)={entry}；Dom(n)={n} ∪ ⋂Dom(p)。

    前驱只计可达块；不可达块的支配集为空集（校验时已豁免）。
    """
    n = len(succs)
    preds: Dict[int, List[int]] = {i: [] for i in range(n)}
    for b, ss in enumerate(succs):
        for s in ss:
            preds[s].append(b)
    dom: List[Set[int]] = [set() for _ in range(n)]
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for b in range(n):
            if b == entry or b not in reachable:
                continue
            incoming = [dom[p] for p in preds[b] if p in reachable and dom[p]]
            if not incoming:
                continue
            new = {b} | set.intersection(*incoming)
            if new != dom[b]:
                dom[b] = new
                changed = True
    return dom


def _fmt_path(path: Iterable[str]) -> str:
    return ".".join(path)


# --------------------------------------------------------------------------
# 操作片段（Segment）与 Program 校验
# --------------------------------------------------------------------------


def _validate_call_op(op: Call, oloc: str,
                      segments: Optional[Dict[str, Segment]]) -> None:
    """校验主 CFG 中的单个 call 操作（片段存在性 + 形参实参精确匹配）。"""
    if not isinstance(op.segment, str) or op.segment == "":
        raise CFGValidationError("call.segment 必须是非空字符串", f"{oloc}.segment")
    if segments is None:
        raise CFGValidationError(
            "call 操作只能出现在 Program 中：请用 program_from_dict 提供片段定义，"
            "不要直接对含 call 的 CFG 调 check",
            f"{oloc}.segment",
        )
    seg = segments.get(op.segment)
    if seg is None:
        raise CFGValidationError(
            f"调用了未定义的片段 {op.segment!r}", f"{oloc}.segment")
    if not isinstance(op.args, tuple):
        raise CFGValidationError("call.args 必须是元组", f"{oloc}.args")
    seen_params: Set[str] = set()
    for pname, apath in op.args:
        if not isinstance(pname, str) or pname == "":
            raise CFGValidationError("形参名必须是非空字符串", f"{oloc}.args")
        if pname not in seg.params:
            raise CFGValidationError(
                f"片段 {op.segment!r} 没有形参 {pname!r}"
                f"（声明的形参：{list(seg.params)}）",
                f"{oloc}.args",
            )
        if pname in seen_params:
            raise CFGValidationError(
                f"形参 {pname!r} 在同一次调用中被重复绑定", f"{oloc}.args")
        seen_params.add(pname)
        if not isinstance(apath, tuple) or not apath:
            raise CFGValidationError(
                "实参路径必须是非空字符串元组", f"{oloc}.args")
    missing = [p for p in seg.params if p not in seen_params]
    if missing:
        raise CFGValidationError(
            f"调用片段 {op.segment!r} 缺少形参绑定：{missing}", f"{oloc}.args")


def _check_call_paths_exist(call: Call, seg: Segment,
                            declared: Dict[str, Decl], oloc: str) -> None:
    """形参路径替换为实参后，片段内每条路径都必须在实参记录树中存在。

    片段本身无记录树（只做形参替换），字段存在性只能结合调用点实参校验。
    """
    subst = {pname: apath for pname, apath in call.args}

    def walk(formal: Path) -> Path:
        return (*subst[formal[0]], *formal[1:])

    for sb, block in enumerate(seg.blocks):
        for so, op in enumerate(block.ops):
            if not hasattr(op, "path"):
                continue
            actual = walk(op.path)
            root = actual[0]
            decl = declared.get(root)
            if decl is None:
                # 实参根未声明：已在主路径存在性校验中报错。
                continue
            node = decl.ty
            for fi, field in enumerate(actual[1:], start=1):
                if node is None or field not in node:
                    raise CFGValidationError(
                        f"片段 {call.segment!r} 内路径 "
                        f"{_fmt_path(op.path)} 替换为实参 {_fmt_path(actual)} 后，"
                        f"第 {fi} 段在实参记录类型中不存在（片段 blocks[{sb}].ops[{so}]）",
                        f"{oloc}.args",
                    )
                node = node[field]


def _is_acyclic(succs: List[List[int]]) -> bool:
    """Kahn 拓扑排序判定有向图是否无环。"""
    n = len(succs)
    indeg = [0] * n
    for ss in succs:
        for s in ss:
            indeg[s] += 1
    queue = deque(i for i in range(n) if indeg[i] == 0)
    seen = 0
    while queue:
        b = queue.popleft()
        seen += 1
        for s in succs[b]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    return seen == n


def validate_segment(name: str, seg: Segment) -> None:
    """校验单个片段：形参、指令白名单、形参根路径、局部 loan、无环且全可达。"""
    loc = f"segments.{name}"
    if not isinstance(seg, Segment):
        raise CFGValidationError("片段必须是 Segment 实例", loc)
    if not seg.params or any(not isinstance(p, str) or p == "" for p in seg.params):
        raise CFGValidationError("params 必须是非空字符串元组", f"{loc}.params")
    if len(set(seg.params)) != len(seg.params):
        raise CFGValidationError("片段形参名重复", f"{loc}.params")
    param_set = set(seg.params)

    blocks = seg.blocks
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= MAX_BLOCKS):
        raise CFGValidationError(
            f"片段块数必须在 1..{MAX_BLOCKS}", f"{loc}.blocks")
    local_loans: Set[str] = set()
    for bi, block in enumerate(blocks):
        bloc = f"{loc}.blocks[{bi}]"
        if not isinstance(block, Block):
            raise CFGValidationError("片段块必须是 Block 实例", bloc)
        if len(block.ops) > MAX_OPS_PER_BLOCK:
            raise CFGValidationError(
                f"单块操作数上限为 {MAX_OPS_PER_BLOCK}", f"{bloc}.ops")
        for oi, op in enumerate(block.ops):
            oloc = f"{bloc}.ops[{oi}]"
            # 片段只含“已有指令”，且不得 declare / 嵌套 call。
            if isinstance(op, Decl):
                raise CFGValidationError(
                    "片段内不允许 declare（片段没有记录树，路径只做形参替换）",
                    oloc)
            if isinstance(op, Call):
                raise CFGValidationError(
                    "片段内不允许嵌套 call（调用图保持无环、无递归）", oloc)
            if not isinstance(
                op, (Move, BorrowShared, BorrowMut, EndLoan, Read, Write)):
                raise CFGValidationError(
                    f"片段内非法操作 {type(op).__name__}", oloc)
            if hasattr(op, "path"):
                if not isinstance(op.path, tuple) or not op.path:
                    raise CFGValidationError("路径必须是非空字符串元组", f"{oloc}.path")
                if op.path[0] not in param_set:
                    raise CFGValidationError(
                        f"片段内路径 {_fmt_path(op.path)} 的根 {op.path[0]!r} "
                        f"不是形参（形参：{list(seg.params)}）",
                        f"{oloc}.path")
            if isinstance(op, Write) and not isinstance(op.reinit, bool):
                raise CFGValidationError(
                    "write.reinit 必须是 bool", f"{oloc}.reinit")
            if isinstance(op, (BorrowShared, BorrowMut)):
                if not op.loan_id:
                    raise CFGValidationError(
                        "loan_id 必须是非空字符串", f"{oloc}.loan_id")
                if op.loan_id in local_loans:
                    raise CFGValidationError(
                        f"片段内 loan_id {op.loan_id!r} 不唯一（局部 loan 在片段内"
                        "也只能有一个 borrow 点）",
                        f"{oloc}.loan_id")
                local_loans.add(op.loan_id)

    for bi, block in enumerate(blocks):
        for oi, op in enumerate(block.ops):
            if isinstance(op, EndLoan):
                if op.loan_id not in local_loans:
                    raise CFGValidationError(
                        f"片段内 end_loan 引用了未在片段中开启的 loan "
                        f"{op.loan_id!r}",
                        f"{loc}.blocks[{bi}].ops[{oi}].loan_id")

    succs = seg.successors
    if not isinstance(succs, list) or len(succs) != len(blocks):
        raise CFGValidationError(
            "successors 必须与 blocks 等长", f"{loc}.successors")
    checked_int("entry", seg.entry, min_value=0)
    if seg.entry >= len(blocks):
        raise CFGValidationError("片段 entry 越界", f"{loc}.entry")
    # 要求全部块从 entry 可达：小片段不承载死代码（摘要只解释可达部分）。
    reachable = _reachable_blocks(succs, seg.entry)
    if len(reachable) != len(blocks):
        dead = [i for i in range(len(blocks)) if i not in reachable]
        raise CFGValidationError(
            f"片段存在从 entry 不可达的块 {dead}（片段必须是连通的小 CFG）",
            f"{loc}.blocks")
    if not _is_acyclic(succs):
        raise CFGValidationError(
            "片段 CFG 必须无环（片段是无环小 CFG，摘要不做片段内不动点）",
            f"{loc}.successors")


def validate_program(program: Program) -> None:
    """校验 :class:`Program`：主 CFG（带片段表）+ 全部片段。"""
    if not isinstance(program, Program):
        raise CFGValidationError(
            "需要 borrow_check.Program 实例（可用 program_from_dict 构造）",
            "program")
    if not isinstance(program.segments, dict):
        raise CFGValidationError("segments 必须是 dict", "program.segments")
    for name, seg in program.segments.items():
        if not isinstance(name, str) or name == "":
            raise CFGValidationError("片段名必须是非空字符串", "program.segments")
        validate_segment(name, seg)
    # 主 CFG 语义校验（含 call 的片段存在性/形参匹配/实参路径合法性）。
    validate_cfg(program.cfg, program.segments)
