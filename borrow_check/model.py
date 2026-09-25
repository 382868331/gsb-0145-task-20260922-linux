"""公开数据结构：变量声明、记录树、操作、CFG、可调用片段（fragment）。

路径语义
========
路径是字段名元组，例如 ``("a", "x")`` 表示变量 ``a`` 的字段 ``x``。
两个路径 **重叠** 当且仅当其中一个是另一个的前缀（包含相等）。
不同字段（如 ``a.x`` 与 ``a.y``）互不重叠，可以独立借用。

记录树
======
变量是“已给定的有限记录树”，用 :class:`RecordType` 描述：叶子为 ``None``，
内部节点为 ``{字段名: RecordType | None}``。程序只允许声明过的根变量，
访问的路径必须在树中存在。

操作
====
每个块含一个有序操作列表，操作类型为：``declare``、``move``、
``borrow_shared``、``borrow_mut``、``end_loan``、``read``、``write``，
以及片段调用 ``invoke``。每个 loan_id 静态唯一、作用于固定路径（在唯一的
borrow 处确定）；片段内 loan_id 是**局部**于该片段的名字空间。

片段
====
:class:`Fragment` 是由已有指令组成的**无环**小 CFG，只含显式路径形参
（:class:`Param`，各带形参记录形状）与局部 loan，不含 ``declare``。
片段之间的调用图也必须无环（不支持递归）。``invoke`` 处按实参路径把形参
替换到主命名空间；两个形参可以指向同一记录的不同字段，也可以完全别名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from .errors import CFGValidationError, checked_int

# 路径：根变量名 + 零或多个字段名。空元组在操作中表示“整个变量”（move x）。
Path = Tuple[str, ...]

# 记录树：内部节点 dict；叶子用 None。
RecordNode = Optional[Dict[str, "RecordNode"]]


@dataclass(frozen=True)
class Decl:
    """declare：声明根变量及其记录类型（变量进入可用状态）。"""

    var: str
    ty: Optional[Dict[str, RecordNode]] = None


@dataclass(frozen=True)
class Move:
    """move：按位移动路径（空 path 表示整变量）。"""

    path: Path


@dataclass(frozen=True)
class BorrowShared:
    """borrow_shared：在 path 上开启共享借用。"""

    loan_id: str
    path: Path


@dataclass(frozen=True)
class BorrowMut:
    """borrow_mut：在 path 上开启可变借用。"""

    loan_id: str
    path: Path


@dataclass(frozen=True)
class EndLoan:
    """end_loan：结束此前开启的借用。"""

    loan_id: str


@dataclass(frozen=True)
class Read:
    """read：读取路径。路径不得可能已移动，且不得与活跃可变借用冲突。"""

    path: Path


@dataclass(frozen=True)
class Write:
    """write：写入路径。

    若 ``reinit`` 为真，表示对**恰好该路径**的重新赋值：只恢复该路径
    本身的可用性（父级移动仍压制其余子路径）。否则普通写要求路径可用
    且不与任何活跃借用（共享或可变）冲突。
    """

    path: Path
    reinit: bool = False


@dataclass(frozen=True)
class Invoke:
    """invoke：操作片段调用（本次新增）。

    :param fragment: 被调用片段名（必须在 :class:`Program` 的片段表中）。
    :param args: 形参名 -> 调用方实参路径（主命名空间中的具体路径）。
        每个形参必须恰好给一次实参；两个形参可绑定到同一记录的不同
        字段（如 ``a.x``、``a.y``），也可绑定到相互重叠（别名）的路径。
    """

    fragment: str
    args: Dict[str, Path] = field(default_factory=dict)


Operation = Union[Decl, Move, BorrowShared, BorrowMut, EndLoan, Read, Write, Invoke]

_OP_KINDS = {
    "declare": Decl,
    "move": Move,
    "borrow_shared": BorrowShared,
    "borrow_mut": BorrowMut,
    "end_loan": EndLoan,
    "read": Read,
    "write": Write,
    "invoke": Invoke,
}


@dataclass(frozen=True)
class Param:
    """片段形参：显式路径形参。

    :param name: 形参名（非空字符串），在片段内作为路径根使用，
        例如形参 ``p`` 的字段写作 ``("p", "x")``。
    :param ty: 形参记录形状（与 declare 的 ty 同构：dict 内部节点 / None 叶子）。
        片段内对 ``p.<...>`` 的访问必须落在该形状内；调用点实参路径所在
        实际类型必须至少包含该形状。
    """

    name: str
    ty: Optional[Dict[str, RecordNode]] = None


@dataclass
class Fragment:
    """操作片段：由已有指令组成的**无环**小 CFG。

    :param name: 片段名（非空字符串，在 :class:`Program` 内唯一）。
    :param params: 显式形参列表（形参名唯一）。
    :param blocks: 片段基本块（下标即块 id，最多 50）。块内指令只允许引用
        形参路径根与局部 loan_id；**不含** ``declare``。
    :param successors: 片段 CFG 边，结构性无环（只准连向更大下标，且不可自环）。
    :param entry: 片段入口块下标。
    """

    name: str
    params: List[Param]
    blocks: List[Block]
    successors: List[List[int]]
    entry: int = 0


@dataclass
class Program:
    """主 CFG + 片段表。

    :param cfg: 主控制流图（其块内可出现 :class:`Invoke`）。
    :param fragments: 片段名 -> :class:`Fragment`。片段调用图必须无环。
    """

    cfg: CFG
    fragments: Dict[str, Fragment] = field(default_factory=dict)


@dataclass
class Block:
    """基本块：有序操作列表。"""

    ops: List[Operation] = field(default_factory=list)


@dataclass
class CFG:
    """控制流图。

    :param blocks: 下标即块 id，最多 50 块。
    :param successors: ``blocks[i]`` 的后继块下标列表（CFG 边）。
    :param entry: 入口块下标。
    """

    blocks: List[Block]
    successors: List[List[int]]
    entry: int = 0


# --------------------------------------------------------------------------
# 从纯数据（JSON 风格 dict/list）构造，便于离线确定性生成与导入。
# --------------------------------------------------------------------------

def _is_ident(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _validate_path(value: object, loc: str) -> Path:
    if not isinstance(value, (list, tuple)):
        raise CFGValidationError("路径必须是字符串列表（如 ['a', 'x']）", loc)
    path: Path = tuple(value)  # type: ignore[arg-type]
    if not path:
        raise CFGValidationError("路径至少要包含根变量名", loc)
    for i, seg in enumerate(path):
        if not _is_ident(seg):
            raise CFGValidationError(f"路径第 {i} 段必须是非空字符串，得到 {seg!r}", loc)
    return path


def _validate_type(value: object, loc: str) -> Optional[Dict[str, RecordNode]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CFGValidationError("记录类型必须是 dict（字段名 -> 子类型）或 null 叶子", loc)
    result: Dict[str, RecordNode] = {}
    for key, sub in value.items():
        if not _is_ident(key):
            raise CFGValidationError("字段名必须是非空字符串", f"{loc}[{key!r}]")
        result[key] = _validate_type(sub, f"{loc}.{key}")
    return result


def _require_keys(data: dict, allowed: frozenset, loc: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise CFGValidationError(
            f"未知字段 {sorted(unknown)}（允许的字段：{sorted(allowed)}）", loc
        )


def operation_from_dict(data: object, loc: str) -> Operation:
    """把一个纯数据操作描述转换为 :data:`Operation`，同时做严格校验。"""
    if not isinstance(data, dict):
        raise CFGValidationError("操作必须是对象", loc)
    kind = data.get("op")
    if kind not in _OP_KINDS:
        raise CFGValidationError(
            f"op 必须是 {sorted(_OP_KINDS)} 之一，得到 {kind!r}", f"{loc}.op"
        )
    try:
        if kind == "declare":
            _require_keys(data, frozenset({"op", "var", "ty"}), loc)
            var = data.get("var")
            if not _is_ident(var):
                raise CFGValidationError("var 必须是非空字符串", f"{loc}.var")
            ty = _validate_type(data.get("ty"), f"{loc}.ty")
            return Decl(var=var, ty=ty)  # type: ignore[arg-type]
        if kind in ("move", "read"):
            _require_keys(data, frozenset({"op", "path"}), loc)
            path = _validate_path(data.get("path"), f"{loc}.path")
            cls = Move if kind == "move" else Read
            return cls(path=path)
        if kind in ("borrow_shared", "borrow_mut"):
            _require_keys(data, frozenset({"op", "loan_id", "path"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            path = _validate_path(data.get("path"), f"{loc}.path")
            cls = BorrowShared if kind == "borrow_shared" else BorrowMut
            return cls(loan_id=loan_id, path=path)  # type: ignore[arg-type]
        if kind == "end_loan":
            _require_keys(data, frozenset({"op", "loan_id"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            return EndLoan(loan_id=loan_id)
        if kind == "invoke":
            return _invoke_from_dict(data, loc)
        # write
        _require_keys(data, frozenset({"op", "path", "reinit"}), loc)
        path = _validate_path(data.get("path"), f"{loc}.path")
        reinit = data.get("reinit", False)
        if not isinstance(reinit, bool):
            raise CFGValidationError("reinit 必须是布尔值", f"{loc}.reinit")
        return Write(path=path, reinit=reinit)
    except CFGValidationError:
        raise
    except TypeError as exc:  # tuple() 等构造异常包装为可定位错误
        raise CFGValidationError(str(exc), loc) from exc


def _invoke_from_dict(data: dict, loc: str) -> Invoke:
    _require_keys(data, frozenset({"op", "fragment", "args"}), loc)
    fragment = data.get("fragment")
    if not _is_ident(fragment):
        raise CFGValidationError("fragment 必须是非空字符串", f"{loc}.fragment")
    raw_args = data.get("args")
    if not isinstance(raw_args, dict) or not raw_args:
        raise CFGValidationError(
            "args 必须是非空对象（形参名 -> 实参路径）", f"{loc}.args")
    args: Dict[str, Path] = {}
    for pname, ppath in raw_args.items():
        if not _is_ident(pname):
            raise CFGValidationError(
                "形参名必须是非空字符串", f"{loc}.args[{pname!r}]")
        args[pname] = _validate_path(ppath, f"{loc}.args[{pname!r}]")
    return Invoke(fragment=fragment, args=args)  # type: ignore[arg-type]


def _cfg_structural_from_dict(data: object, loc: str = "cfg") -> CFG:
    """只做形状构造（块/边/下标/操作字面量），不跑语义校验。

    语义校验（声明支配、路径越界、invoke 规则等）由 :func:`validate_cfg`
    或 :func:`validate_program` 在分析前统一完成。
    """
    if not isinstance(data, dict):
        raise CFGValidationError("CFG 必须是对象", loc)
    _require_keys(data, frozenset({"blocks", "successors", "entry"}), loc)
    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise CFGValidationError("blocks 必须是列表", f"{loc}.blocks")
    if not raw_blocks:
        raise CFGValidationError("至少需要 1 个块", f"{loc}.blocks")
    if len(raw_blocks) > 50:
        raise CFGValidationError("块数上限为 50", f"{loc}.blocks")

    blocks: List[Block] = []
    for bi, raw_block in enumerate(raw_blocks):
        bloc = f"blocks[{bi}]" if loc == "cfg" else f"{loc}.blocks[{bi}]"
        if not isinstance(raw_block, dict) or not isinstance(raw_block.get("ops"), list):
            raise CFGValidationError("块必须是含 ops 列表的对象", f"{bloc}.ops")
        if len(raw_block["ops"]) > 10000:
            raise CFGValidationError("单块操作数上限为 10000", f"{bloc}.ops")
        ops = [
            operation_from_dict(raw_op, f"{bloc}.ops[{oi}]")
            for oi, raw_op in enumerate(raw_block["ops"])
        ]
        blocks.append(Block(ops=ops))

    raw_succ = data.get("successors")
    if not isinstance(raw_succ, list) or len(raw_succ) != len(blocks):
        raise CFGValidationError(
            "successors 必须是与 blocks 等长的列表", f"{loc}.successors"
        )
    successors: List[List[int]] = []
    for bi, raw_list in enumerate(raw_succ):
        sloc = f"successors[{bi}]" if loc == "cfg" else f"{loc}.successors[{bi}]"
        if not isinstance(raw_list, list):
            raise CFGValidationError("每个后继列表必须是列表", sloc)
        succ: List[int] = []
        for s in raw_list:
            # 拒绝 bool（True == 1 的坑）与越界下标。
            checked_int("successor index", s, min_value=0)
            if s >= len(blocks):
                raise CFGValidationError(f"后继块下标 {s} 越界", sloc)
            succ.append(s)
        if len(set(succ)) != len(succ):
            raise CFGValidationError("同一后继在列表中重复", sloc)
        successors.append(succ)

    entry = data.get("entry", 0)
    checked_int("entry", entry, min_value=0)
    if entry >= len(blocks):
        raise CFGValidationError(f"入口块下标 {entry} 越界", f"{loc}.entry")
    return CFG(blocks=blocks, successors=successors, entry=entry)


def cfg_from_dict(data: object) -> CFG:
    """从纯数据构造并完整校验 :class:`CFG`（不含 invoke 的普通 CFG）。

    结构校验（块数、边、下标）与 :func:`validate_cfg` 的语义校验分离，
    构造函数只负责形状；语义（重复声明、路径越界、loan 规则）在分析前
    由 :func:`validate_cfg` 一次性完成。含片段调用的程序请用
    :func:`program_from_dict`。
    """
    cfg = _cfg_structural_from_dict(data, "cfg")
    # 构造即完成全部语义校验；失败时不返回任何对象（无部分结果）。
    from .validator import validate_cfg

    validate_cfg(cfg)
    return cfg


def fragment_from_dict(data: object, loc: str = "fragment") -> Fragment:
    """从纯数据构造片段（只做形状与字面量校验；片段语义在 validate_program 完成）。"""
    if not isinstance(data, dict):
        raise CFGValidationError("片段必须是对象", loc)
    _require_keys(
        data, frozenset({"name", "params", "blocks", "successors", "entry"}), loc)

    name = data.get("name")
    if not _is_ident(name):
        raise CFGValidationError("片段 name 必须是非空字符串", f"{loc}.name")

    raw_params = data.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        raise CFGValidationError(
            "params 必须是非空列表（片段至少一个显式路径形参）", f"{loc}.params")
    params: List[Param] = []
    seen_params: set[str] = set()
    for pi, raw_p in enumerate(raw_params):
        ploc = f"{loc}.params[{pi}]"
        if not isinstance(raw_p, dict):
            raise CFGValidationError("形参必须是对象", ploc)
        _require_keys(raw_p, frozenset({"name", "ty"}), ploc)
        pname = raw_p.get("name")
        if not _is_ident(pname):
            raise CFGValidationError("形参 name 必须是非空字符串", f"{ploc}.name")
        if pname in seen_params:
            raise CFGValidationError(f"形参 {pname!r} 重复", f"{ploc}.name")
        seen_params.add(pname)
        ty = _validate_type(raw_p.get("ty"), f"{ploc}.ty")
        params.append(Param(name=pname, ty=ty))

    cfg = _cfg_structural_from_dict(
        {"blocks": data["blocks"], "successors": data["successors"],
         "entry": data.get("entry", 0)},
        loc=loc,
    )
    return Fragment(name=name, params=params, blocks=cfg.blocks,
                    successors=cfg.successors, entry=cfg.entry)


def program_from_dict(data: object) -> Program:
    """从纯数据构造并完整校验 :class:`Program`（主 CFG + 命名片段）。

    格式::

        {
          "cfg": { ... 主 CFG（块内可含 invoke）... },
          "fragments": [ { "name": ..., "params": [...], "blocks": ...,
                           "successors": ..., "entry": 0 }, ... ]
        }
    """
    loc = "program"
    if not isinstance(data, dict):
        raise CFGValidationError("Program 必须是对象", loc)
    _require_keys(data, frozenset({"cfg", "fragments"}), loc)

    cfg = _cfg_structural_from_dict(data["cfg"], loc="cfg")

    raw_frags = data.get("fragments")
    if not isinstance(raw_frags, list):
        raise CFGValidationError("fragments 必须是列表（可为空）", f"{loc}.fragments")
    fragments: Dict[str, Fragment] = {}
    for fi, raw_f in enumerate(raw_frags):
        frag = fragment_from_dict(raw_f, loc=f"fragments[{fi}]")
        if frag.name in fragments:
            raise CFGValidationError(
                f"片段 {frag.name!r} 重名", f"fragments[{fi}].name")
        fragments[frag.name] = frag

    program = Program(cfg=cfg, fragments=fragments)
    from .validator import validate_program

    validate_program(program)
    return program
