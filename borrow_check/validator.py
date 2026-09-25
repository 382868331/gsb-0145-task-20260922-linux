"""CFG 与 Program（片段调用）语义校验。

在任何分析状态建立之前完成全部校验，因此 ``check``/``check_program`` 对
非法输入只抛出 :class:`CFGValidationError`，不会产生部分结果或部分变更。

普通 CFG 校验（:func:`validate_cfg`）：块数 1..50；边下标合法且不重复；
entry 合法；操作类型合法；根变量恰好 declare 一次；declare 必须在 CFG 上
**支配**每个使用点（块内要求操作下标不早于声明，跨块要求声明块支配使用块；
不可达块豁免）；所有路径在记录树中存在；loan_id 全局唯一对应一个 borrow，
end_loan 必须引用已存在的 loan；普通 CFG 不允许 ``invoke``。

Program 校验（:func:`validate_program`）：主 CFG 规则同上（允许 invoke），
另校验片段形参/局部 loan/无环结构、调用图无环、实参齐全且实参路径的实际
类型覆盖形参形状。
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
    Decl,
    EndLoan,
    Fragment,
    Invoke,
    Move,
    Operation,
    Param,
    Path,
    Program,
    Read,
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


def validate_cfg(cfg: CFG) -> None:
    """对 :class:`CFG` 做完整结构与语义校验，非法即抛 :class:`CFGValidationError`。

    普通 CFG 的块内不允许出现 :class:`Invoke`；含片段调用的程序用
    :func:`program_from_dict` + :func:`check_program`。
    """
    _validate_main_cfg(cfg, fragments=None)


def validate_program(program: Program) -> None:
    """校验主 CFG（允许 invoke）与全部片段、调用关系。"""
    if not isinstance(program, Program):
        raise CFGValidationError(
            "需要 borrow_check.Program 实例（可用 program_from_dict 构造）",
            "program")
    fragments = program.fragments
    if not isinstance(fragments, dict):
        raise CFGValidationError("fragments 必须是 dict", "program.fragments")
    for name, frag in fragments.items():
        if not isinstance(frag, Fragment) or frag.name != name:
            raise CFGValidationError(
                f"片段表项 {name!r} 非法", f"program.fragments[{name!r}]")
    _validate_fragments(fragments)
    _validate_main_cfg(program.cfg, fragments=fragments)


def _validate_main_cfg(cfg: CFG,
                       fragments: Optional[Dict[str, Fragment]]) -> None:
    """主 CFG 校验。``fragments`` 非 None 时允许并校验 invoke。"""
    if not isinstance(cfg, CFG):
        raise CFGValidationError("需要 borrow_check.CFG 实例（可用 cfg_from_dict 构造）", "cfg")
    blocks = cfg.blocks
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= MAX_BLOCKS):
        raise CFGValidationError(f"blocks 必须是 1..{MAX_BLOCKS} 个 Block 的列表", "cfg.blocks")

    declared: Dict[str, Decl] = {}
    decl_site: Dict[str, Tuple[int, int]] = {}
    loan_site: Dict[str, Operation] = {}
    op_path_users: List[Tuple[int, int, Operation, Path]] = []
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
            elif isinstance(op, Invoke):
                if fragments is None:
                    raise CFGValidationError(
                        "普通 CFG 不允许 invoke；含片段调用请用 program_from_dict"
                        " + check_program", oloc)
                _check_invoke_shape(op, fragments, oloc)
                # 实参路径作为“使用点”参与存在性与声明支配校验。
                for pname, apath in op.args.items():
                    _check_path_shape(apath, f"{oloc}.args[{pname!r}]")
                    op_path_users.append((bi, oi, op, apath))
            else:  # pragma: no cover - 防御性分支
                raise CFGValidationError(f"未知操作类型 {type(op).__name__}", oloc)

        block_path_ops = [(oi, uop) for oi, uop in enumerate(ops)
                          if not isinstance(uop, (Decl, Invoke))
                          and hasattr(uop, "path")]
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

    # invoke 实参形状覆盖：实参路径所在实际类型必须包含形参声明的字段形状。
    if fragments is not None:
        for bi, block in enumerate(blocks):
            for oi, op in enumerate(block.ops):
                if not isinstance(op, Invoke):
                    continue
                frag = fragments[op.fragment]
                for p in frag.params:
                    apath = op.args[p.name]
                    actual = _type_at_path(declared, apath)
                    if not _shape_covers(actual, p.ty):
                        raise CFGValidationError(
                            f"实参 {_fmt_path(apath)} 的实际记录形状不覆盖形参 "
                            f"{p.name!r} 声明的字段形状（片段 {frag.name!r}）",
                            f"blocks[{bi}].ops[{oi}].args[{p.name!r}]")

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


# --------------------------------------------------------------------------
# 片段（fragment）与 invoke 校验
# --------------------------------------------------------------------------


def _check_invoke_shape(op: Invoke, fragments: Dict[str, Fragment],
                        oloc: str) -> None:
    """invoke 的纯结构校验（实参齐全/无多余；实参形状覆盖在 _validate_fragments
    收集完类型后另行校验）。"""
    if not isinstance(op.fragment, str) or op.fragment == "":
        raise CFGValidationError("invoke.fragment 必须是非空字符串",
                                 f"{oloc}.fragment")
    if not isinstance(op.args, dict) or not op.args:
        raise CFGValidationError(
            "invoke.args 必须是非空对象（形参名 -> 实参路径）", f"{oloc}.args")
    if op.fragment not in fragments:
        raise CFGValidationError(
            f"invoke 引用了未定义片段 {op.fragment!r}", f"{oloc}.fragment")
    frag = fragments[op.fragment]
    pnames = [p.name for p in frag.params]
    given = set(op.args)
    expected = set(pnames)
    missing = expected - given
    extra = given - expected
    if missing:
        raise CFGValidationError(
            f"invoke 缺少形参实参 {sorted(missing)}", f"{oloc}.args")
    if extra:
        raise CFGValidationError(
            f"invoke 给出了多余实参 {sorted(extra)}（形参为 {pnames}）",
            f"{oloc}.args")
    for pname, apath in op.args.items():
        if not isinstance(apath, tuple) or not apath:
            raise CFGValidationError(
                f"形参 {pname!r} 的实参必须是非空路径 tuple",
                f"{oloc}.args[{pname!r}]")
        for seg in apath:
            if not isinstance(seg, str) or seg == "":
                raise CFGValidationError(
                    f"形参 {pname!r} 实参路径段必须是非空字符串，得到 {seg!r}",
                    f"{oloc}.args[{pname!r}]")


def _type_at_path(declared: Dict[str, Decl], path: Path):
    """返回主类型树中 path 指向节点的子类型（dict 或 None）。"""
    node = declared[path[0]].ty
    for seg in path[1:]:
        node = node[seg]
    return node


def _node_in_param_types(param_tys: Dict[str, object], path: Path):
    """返回片段形参形状中 path 指向节点的子类型。"""
    node = param_tys[path[0]]
    for seg in path[1:]:
        node = node[seg]
    return node


def _shape_covers(actual, param_ty) -> bool:
    """实参节点的实际记录形状是否覆盖形参形状（形参要求的字段实参都要有）。"""
    if param_ty is None:
        return True  # 形参是叶子：任何节点都可当叶子用（只用路径本身）。
    if not isinstance(actual, dict):
        return False  # 形参要求有字段，实参却是叶子。
    for f, sub in param_ty.items():
        if f not in actual:
            return False
        if not _shape_covers(actual[f], sub):
            return False
    return True


def _validate_fragments(fragments: Dict[str, Fragment]) -> None:
    for name, frag in fragments.items():
        loc = f"fragment {name!r}"
        _validate_one_fragment(frag, fragments, loc)
    # 调用图无环（含间接）：拓扑/Kahn 判定。
    _validate_call_dag(fragments)


def _validate_one_fragment(frag: Fragment, fragments: Dict[str, Fragment],
                           loc: str) -> None:
    if not isinstance(frag.name, str) or frag.name == "":
        raise CFGValidationError("片段名必须是非空字符串", f"{loc}.name")
    if not isinstance(frag.params, list) or not frag.params:
        raise CFGValidationError(
            "片段至少需要一个显式路径形参", f"{loc}.params")
    param_tys: Dict[str, object] = {}
    for pi, p in enumerate(frag.params):
        if not isinstance(p, Param) or not isinstance(p.name, str) or p.name == "":
            raise CFGValidationError("形参必须是含非空 name 的 Param",
                                     f"{loc}.params[{pi}]")
        if p.name in param_tys:
            raise CFGValidationError(f"形参 {p.name!r} 重复",
                                     f"{loc}.params[{pi}].name")
        _check_type(p.ty, f"{loc}.params[{pi}].ty", [0])
        param_tys[p.name] = p.ty

    blocks = frag.blocks
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= MAX_BLOCKS):
        raise CFGValidationError(
            f"片段块数必须是 1..{MAX_BLOCKS}", f"{loc}.blocks")
    succs = frag.successors
    if not isinstance(succs, list) or len(succs) != len(blocks):
        raise CFGValidationError("successors 必须与 blocks 等长",
                                 f"{loc}.successors")
    checked_int("fragment entry", frag.entry, min_value=0)
    if frag.entry >= len(blocks):
        raise CFGValidationError(f"entry {frag.entry} 越界", f"{loc}.entry")

    # 片段 CFG 结构性无环：边只连向更大下标、不可自环/回边、不重复。
    for bi, slist in enumerate(succs):
        seen: Set[int] = set()
        if not isinstance(slist, list):
            raise CFGValidationError("后继必须是列表",
                                     f"{loc}.successors[{bi}]")
        for s in slist:
            checked_int("fragment successor", s, min_value=0)
            if s >= len(blocks):
                raise CFGValidationError(f"后继 {s} 越界",
                                         f"{loc}.successors[{bi}]")
            if s <= bi:
                raise CFGValidationError(
                    f"片段必须无环：边 {bi} -> {s} 不是前向边（只允许连向更大下标）",
                    f"{loc}.successors[{bi}]")
            if s in seen:
                raise CFGValidationError(f"后继 {s} 重复",
                                         f"{loc}.successors[{bi}]")
            seen.add(s)

    # 指令校验：无 declare；路径根必须是形参且落在形参形状内；
    # loan_id 局部唯一；end_loan 引用局部 borrow；invoke 引用存在的片段。
    loan_site: Dict[str, Tuple[int, int]] = {}
    for bi, block in enumerate(blocks):
        bloc = f"{loc}.blocks[{bi}]"
        if not isinstance(block, Block):
            raise CFGValidationError("片段块必须是 Block", bloc)
        ops = block.ops
        if len(ops) > MAX_OPS_PER_BLOCK:
            raise CFGValidationError(
                f"单块操作数上限为 {MAX_OPS_PER_BLOCK}", f"{bloc}.ops")
        for oi, op in enumerate(ops):
            oloc = f"{bloc}.ops[{oi}]"
            if isinstance(op, Decl):
                raise CFGValidationError(
                    "片段内不允许 declare（片段只有形参，无全局变量）", oloc)
            if isinstance(op, Invoke):
                if op.fragment not in fragments:
                    raise CFGValidationError(
                        f"invoke 引用了未定义片段 {op.fragment!r}",
                        f"{oloc}.fragment")
                _check_invoke_shape(op, fragments, oloc)
                # 被调用片段的形参名必须在外层片段的形参集合内（路径替换
                # 后仍落在外层形参形状里——具体形状由模板实例化时覆盖判定，
                # 这里校验实参路径根是外层形参）。
                callee = fragments[op.fragment]
                for p in callee.params:
                    apath = op.args[p.name]
                    _check_path_shape(apath, f"{oloc}.args[{p.name!r}]")
                    if apath[0] not in param_tys:
                        raise CFGValidationError(
                            f"嵌套调用实参 {_fmt_path(apath)} 的根不是片段形参",
                            f"{oloc}.args[{p.name!r}]")
                    # 实参路径在外层形参形状中的节点类型必须覆盖被调用形参形状。
                    actual = _node_in_param_types(param_tys, apath)
                    if not _shape_covers(actual, p.ty):
                        raise CFGValidationError(
                            f"实参 {_fmt_path(apath)} 的形参形状不覆盖被调用片段 "
                            f"形参 {p.name!r} 声明的字段形状",
                            f"{oloc}.args[{p.name!r}]")
                continue
            if not isinstance(op, Operation):
                raise CFGValidationError(
                    f"非法操作对象 {type(op).__name__}", oloc)
            if hasattr(op, "path"):
                _check_frag_path(op.path, param_tys, oloc)
            if isinstance(op, (BorrowShared, BorrowMut)):
                if not op.loan_id:
                    raise CFGValidationError("loan_id 必须非空", f"{oloc}.loan_id")
                if op.loan_id in loan_site:
                    raise CFGValidationError(
                        f"片段内局部 loan_id {op.loan_id!r} 重复（每个 borrow 一个）",
                        f"{oloc}.loan_id")
                loan_site[op.loan_id] = (bi, oi)
            elif isinstance(op, EndLoan):
                if op.loan_id not in loan_site:
                    raise CFGValidationError(
                        f"end_loan 引用了片段内不存在的局部 loan {op.loan_id!r}",
                        f"{oloc}.loan_id")
            elif isinstance(op, Write):
                if not isinstance(op.reinit, bool):
                    raise CFGValidationError("reinit 必须是 bool", f"{oloc}.reinit")


def _check_frag_path(path: Path, param_tys: Dict[str, object], oloc: str) -> None:
    _check_path_shape(path, f"{oloc}.path")
    root = path[0]
    if root not in param_tys:
        raise CFGValidationError(
            f"路径根 {root!r} 不是片段形参（片段内只能访问形参）",
            f"{oloc}.path")
    node = param_tys[root]
    for fi, seg in enumerate(path[1:], start=1):
        if node is None:
            raise CFGValidationError(
                f"路径在第 {fi} 段越界：{path[fi - 1]!r} 是叶子", f"{oloc}.path")
        if seg not in node:
            raise CFGValidationError(
                f"形参形状中不存在字段 {seg!r}", f"{oloc}.path")
        node = node[seg]


def _validate_call_dag(fragments: Dict[str, Fragment]) -> None:
    """片段调用图必须无环（不支持递归）；自调用也算环。"""
    indeg: Dict[str, int] = {n: 0 for n in fragments}
    edges: Dict[str, Set[str]] = {n: set() for n in fragments}
    for name, frag in fragments.items():
        for block in frag.blocks:
            for op in block.ops:
                if isinstance(op, Invoke):
                    if op.fragment == name:
                        raise CFGValidationError(
                            f"片段 {name!r} 自调用（不支持递归）",
                            f"fragment {name!r}")
                    if op.fragment not in edges[name]:
                        edges[name].add(op.fragment)
                        indeg[op.fragment] += 1
    queue = deque(n for n, d in indeg.items() if d == 0)
    seen = 0
    while queue:
        n = queue.popleft()
        seen += 1
        for m in edges[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(fragments):
        cyc = [n for n, d in indeg.items() if d > 0]
        raise CFGValidationError(
            f"片段调用图存在环（不支持递归），涉及 {sorted(cyc)}", "fragment")


def _fmt_path(path: Iterable[str]) -> str:
    return ".".join(path)
