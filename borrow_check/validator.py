"""CFG 语义校验。

在任何分析状态建立之前完成全部校验，因此 ``check`` 对非法输入只抛出
:class:`CFGValidationError`，不会产生部分结果或部分变更。

校验内容：块数 1..max_blocks；边下标合法且不重复；entry 合法；操作类型
合法；根变量恰好 declare 一次；declare 必须在 CFG 上**支配**每个使用点
（块内要求操作下标不早于声明，跨块要求声明块支配使用块；不可达块豁免）；
所有路径在记录树中存在；loan_id 全局唯一对应一个出生点（v1：
borrow_shared/borrow_mut；v2：还可以是 borrow_reserve），end_loan 与
borrow_activate 必须引用已出生的 loan，且每个预约 loan 至多被激活一次；
边守卫公式引用的原子必须出自 cfg.conditions。
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Set, Tuple

from .errors import CFGValidationError, checked_int
from .formula import MAX_CONDITIONS, atoms_of
from .model import (
    Block,
    BorrowActivate,
    BorrowMut,
    BorrowReserve,
    BorrowShared,
    CFG,
    Decl,
    EndLoan,
    Move,
    Operation,
    Path,
    Read,
    Write,
)

MAX_BLOCKS = 50
MAX_BLOCKS_LIFECYCLE = 64
MAX_OPS_PER_BLOCK = 10_000
MAX_TYPE_NODES = 100_000


def _check_path_shape(path: Path, loc: str) -> None:
    if not isinstance(path, tuple):
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


def validate_cfg(cfg: CFG, *, max_blocks: int = MAX_BLOCKS) -> None:
    """对 :class:`CFG` 做完整结构与语义校验，非法即抛 :class:`CFGValidationError`。"""
    if not isinstance(cfg, CFG):
        raise CFGValidationError("需要 borrow_check.CFG 实例（可用 cfg_from_dict 构造）", "cfg")
    checked_int("max_blocks", max_blocks, min_value=1)
    blocks = cfg.blocks
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= max_blocks):
        raise CFGValidationError(
            f"blocks 必须是 1..{max_blocks} 个 Block 的列表", "cfg.blocks")

    conditions = tuple(cfg.conditions) if cfg.conditions else ()
    if not all(isinstance(c, str) and c for c in conditions):
        raise CFGValidationError(
            "conditions 必须是非空字符串元组", "cfg.conditions")
    if len(set(conditions)) != len(conditions):
        raise CFGValidationError("conditions 含重复原子", "cfg.conditions")
    if len(conditions) > MAX_CONDITIONS:
        raise CFGValidationError(
            f"布尔条件数超过上限 {MAX_CONDITIONS}", "cfg.conditions")
    condition_set: Set[str] = set(conditions)

    declared: Dict[str, Decl] = {}
    decl_site: Dict[str, Tuple[int, int]] = {}
    # loan 出生点：shared/mut/reserve 三类。
    loan_birth: Dict[str, Operation] = {}
    activated: Set[str] = set()
    op_path_users: List[Tuple[int, int, Operation, Path]] = []
    type_node_counter = [0]

    path_ops = (Move, Read, Write, BorrowShared, BorrowMut, BorrowReserve)

    for bi, block in enumerate(blocks):
        bloc = f"blocks[{bi}]"
        if not isinstance(block, Block):
            raise CFGValidationError("块必须是 Block 实例", bloc)
        ops = block.ops
        if not isinstance(ops, list) or len(ops) > MAX_OPS_PER_BLOCK:
            raise CFGValidationError(
                f"ops 必须是列表且长度 <= {MAX_OPS_PER_BLOCK}", f"{bloc}.ops")

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
            elif isinstance(op, path_ops):
                _check_path_shape(op.path, f"{oloc}.path")
                if isinstance(op, Write) and not isinstance(op.reinit, bool):
                    raise CFGValidationError(
                        "write.reinit 必须是 bool", f"{oloc}.reinit")
                if isinstance(op, (BorrowShared, BorrowMut, BorrowReserve)):
                    if not isinstance(op.loan_id, str) or op.loan_id == "":
                        raise CFGValidationError(
                            "loan_id 必须是非空字符串", f"{oloc}.loan_id")
                    if op.loan_id in loan_birth:
                        raise CFGValidationError(
                            f"loan_id {op.loan_id!r} 不唯一：每个 loan_id 只能有一个出生点",
                            f"{oloc}.loan_id",
                        )
                    loan_birth[op.loan_id] = op
                op_path_users.append((bi, oi, op, op.path))
            elif isinstance(op, (EndLoan, BorrowActivate)):
                if not isinstance(op.loan_id, str) or op.loan_id == "":
                    raise CFGValidationError(
                        "loan_id 必须是非空字符串", f"{oloc}.loan_id")
                if op.loan_id not in loan_birth:
                    expected = ("borrow_reserve" if isinstance(op, BorrowActivate)
                                else "borrow_shared/borrow_mut/borrow_reserve")
                    raise CFGValidationError(
                        f"{type(op).__name__} 引用了未出生的 loan_id "
                        f"{op.loan_id!r}（应先有 {expected}）",
                        f"{oloc}.loan_id",
                    )
                if isinstance(op, BorrowActivate):
                    birth = loan_birth[op.loan_id]
                    if not isinstance(birth, BorrowReserve):
                        raise CFGValidationError(
                            f"loan_id {op.loan_id!r} 不是两阶段预约 loan，"
                            "不能 borrow_activate（一阶段 borrow_shared/borrow_mut "
                            "出生即活跃，用 end_loan 结束）",
                            f"{oloc}.loan_id",
                        )
                    if op.loan_id in activated:
                        raise CFGValidationError(
                            f"预约 loan_id {op.loan_id!r} 被重复 activate"
                            "（每个预约至多激活一次）",
                            f"{oloc}.loan_id",
                        )
                    activated.add(op.loan_id)
            else:  # pragma: no cover - 防御性分支（Drop 无字段，无需校验）
                from .model import Drop
                if not isinstance(op, Drop):
                    raise CFGValidationError(
                        f"未知操作类型 {type(op).__name__}", oloc)

    # 边与入口。
    succs = cfg.successors
    if not isinstance(succs, list) or len(succs) != len(blocks):
        raise CFGValidationError("successors 必须是与 blocks 等长的列表", "cfg.successors")
    guards = cfg.guards
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
        if guards is not None:
            glist = guards[bi]
            if not isinstance(glist, list) or len(glist) != len(succ):
                raise CFGValidationError(
                    "guards 必须与 successors 对齐", f"cfg.guards[{bi}]")
            for gi, gexpr in enumerate(glist):
                if gexpr is None:
                    continue
                unknown = atoms_of(gexpr) - condition_set
                if unknown:
                    raise CFGValidationError(
                        f"边守卫引用了未在 conditions 中声明的原子 "
                        f"{sorted(unknown)}", f"cfg.guards[{bi}][{gi}]")
    checked_int("entry", cfg.entry, min_value=0)
    if cfg.entry >= len(blocks):
        raise CFGValidationError(f"entry {cfg.entry} 越界", "cfg.entry")

    # 路径根变量与字段存在性。
    for bi, oi, op, path in op_path_users:
        oloc = f"blocks[{bi}].ops[{oi}].path"
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
            raise CFGValidationError(
                f"根变量 {root!r} 的 declare(blocks[{dblock}].ops[{dop}]) "
                f"不支配使用点 blocks[{bi}].ops[{oi}]：存在不经过声明的控制路径",
                f"blocks[{bi}].ops[{oi}].path",
            )
        if dblock == bi and dop > oi:
            raise CFGValidationError(
                f"根变量 {root!r} 在本块 declare 之前被使用",
                f"blocks[{bi}].ops[{oi}].path",
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
