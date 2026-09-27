"""公开数据结构：变量声明、记录树、操作、CFG。

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
``borrow_shared``、``borrow_mut``、``end_loan``、``read``、``write``。
每个 loan_id 静态唯一、作用于固定路径（在唯一的 borrow 处确定）。
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
class UseLoan:
    """use_loan：引用 loan_id 的一次实际使用点。

    该操作只服务于可选的非词法借用结束推断；默认词法模式下无需使用。
    """

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


Operation = Union[Decl, Move, BorrowShared, BorrowMut, EndLoan, UseLoan, Read, Write]

_OP_KINDS = {
    "declare": Decl,
    "move": Move,
    "borrow_shared": BorrowShared,
    "borrow_mut": BorrowMut,
    "end_loan": EndLoan,
    "use_loan": UseLoan,
    "read": Read,
    "write": Write,
}


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
        if kind == "use_loan":
            _require_keys(data, frozenset({"op", "loan_id"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            return UseLoan(loan_id=loan_id)
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


def cfg_from_dict(data: object) -> CFG:
    """从纯数据构造并完整校验 :class:`CFG`。

    结构校验（块数、边、下标）与 :func:`validate_cfg` 的语义校验分离，
    构造函数只负责形状；语义（重复声明、路径越界、loan 规则）在分析前
    由 :func:`validate_cfg` 一次性完成。
    """
    loc = "cfg"
    if not isinstance(data, dict):
        raise CFGValidationError("CFG 必须是对象", loc)
    _require_keys(data, frozenset({"blocks", "successors", "entry"}), loc)
    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise CFGValidationError("blocks 必须是列表", "cfg.blocks")
    if not raw_blocks:
        raise CFGValidationError("至少需要 1 个块", "cfg.blocks")
    if len(raw_blocks) > 50:
        raise CFGValidationError("块数上限为 50", "cfg.blocks")

    blocks: List[Block] = []
    for bi, raw_block in enumerate(raw_blocks):
        bloc = f"blocks[{bi}]"
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
            "successors 必须是与 blocks 等长的列表", "cfg.successors"
        )
    successors: List[List[int]] = []
    for bi, raw_list in enumerate(raw_succ):
        sloc = f"successors[{bi}]"
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
        raise CFGValidationError(f"入口块下标 {entry} 越界", "cfg.entry")
    cfg = CFG(blocks=blocks, successors=successors, entry=entry)
    # 构造即完成全部语义校验；失败时不返回任何对象（无部分结果）。
    from .validator import validate_cfg

    validate_cfg(cfg)
    return cfg
