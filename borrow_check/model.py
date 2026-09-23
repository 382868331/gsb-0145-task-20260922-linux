"""公开数据结构：变量声明、记录树、操作、CFG。

路径语义
========
路径是字段名元组，例如 ``("a", "x")`` 表示变量 ``a`` 的字段 ``x``。
两个路径 **重叠** 当且仅当其中一个是另一个的前缀（包含相等）。
不同字段（如 ``a.x`` 与 ``a.y``）互不重叠，可以独立借用。

记录树
======
变量是“已给定的有限记录树”，用 :class:`RecordType` 描述：叶子为 ``None``，
内部节点为 ``{字段名: RecordType | None}`。程序只允许声明过的根变量，
访问的路径必须在树中存在。

操作
====
v1（默认，冻结语义）每个块含有序操作：``declare``、``move``、
``borrow_shared``、``borrow_mut``、``end_loan``、``read``、``write``。
每个 loan_id 静态唯一、作用于固定路径（在唯一的 borrow 处确定）。

v2（``"version": 2``）在其之上增加：

* ``declare`` 可带 ``init: false``（声明但未初始化）与 ``scope``；
* ``init``（初始化写）、``drop``（显式析构）、
  ``end_scope``（词法析构点：按声明逆序析构，只访问仍初始化的字段）；
* ``borrow_mut`` 可带 ``two_phase: true`` 进入**预约**，
  再由 ``activate`` 激活（两阶段可变借用）；
* CFG 顶层 ``conditions`` 声明布尔条件，后继边可带 ``when`` 命题公式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union

from .errors import CFGValidationError, checked_int

# 路径：根变量名 + 零或多个字段名。空元组在操作中表示“整个变量”（move x）。
Path = Tuple[str, ...]

# 记录树：内部节点 dict；叶子用 None。
RecordNode = Optional[Dict[str, "RecordNode"]]

DEFAULT_SCOPE = "0"


@dataclass(frozen=True)
class Decl:
    """declare：声明根变量及其记录类型。

    :param initialized: v2 下可为 ``False``：变量已声明但未初始化，
        需由 :class:`Init` 或重组写初始化。v1 恒为已初始化。
    :param scope: 词法作用域名；:class:`EndScope` 按作用域收集变量，
        析构顺序为该作用域内**声明顺序的逆序**。
    """

    var: str
    ty: Optional[Dict[str, RecordNode]] = None
    initialized: bool = True
    scope: str = DEFAULT_SCOPE


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
    """borrow_mut：在 path 上开启可变借用。

    :param two_phase: v2 两阶段借用：``True`` 时只进入**预约**
        （reservation）——预约期间允许对重叠路径的共享读取与共享借用，
        但写/移动/析构/其他可变借用仍冲突；:class:`Activate` 后按独占。
    """

    loan_id: str
    path: Path
    two_phase: bool = False


@dataclass(frozen=True)
class EndLoan:
    """end_loan：结束此前开启的借用（预约或已激活都由此撤销）。"""

    loan_id: str


@dataclass(frozen=True)
class Read:
    """read：读取路径。路径不得可能未初始化/已移动，且不得与活跃可变借用冲突。"""

    path: Path


@dataclass(frozen=True)
class Write:
    """write：写入路径。

    若 ``reinit`` 为真，表示对**恰好该路径**的重新赋值：恢复该路径
    本身（及其子树）的可用性（含未初始化与已移动）；否则普通写要求
    路径可用且不与任何活跃借用（共享/可变/预约）冲突。
    """

    path: Path
    reinit: bool = False


@dataclass(frozen=True)
class Init:
    """init（v2）：初始化写。使 path 子树进入已初始化状态。

    对已移动/未初始化的子树等价于重组；借用检查与普通写一致
    （不得与任何活跃借用/预约重叠）。
    """

    path: Path


@dataclass(frozen=True)
class Drop:
    """drop（v2）：显式析构一个 place（消费它）。

    与词法析构不同，显式 drop 要求 place 整体初始化且未被（部分）移动，
    否则报 ``use_of_uninitialized`` / ``use_of_moved``；析构性访问与
    任何活跃借用（共享/可变/预约）冲突。
    """

    path: Path


@dataclass(frozen=True)
class Activate:
    """activate（v2）：把两阶段可变借用由预约激活为独占。

    激活点按独占规则检查：重叠路径上不得再有其他活跃借用（含共享）
    或预约；激活后该 loan 按普通可变借用规则约束。
    """

    loan_id: str


@dataclass(frozen=True)
class EndScope:
    """end_scope（v2）：词法析构点。

    对该作用域内声明的根变量按**声明逆序**执行析构；变量内部字段按
    类型树字段声明的逆序递归析构；未初始化或已（部分）移动的字段跳过
    （“只能访问仍初始化的字段”），仍初始化字段上的析构访问与任何
    活跃借用/预约冲突。析构后这些变量在当前路径上视为已消费。
    """

    scope: str = DEFAULT_SCOPE


Operation = Union[
    Decl, Move, BorrowShared, BorrowMut, EndLoan, Read, Write,
    Init, Drop, Activate, EndScope,
]

_OP_KINDS = {
    "declare": Decl,
    "move": Move,
    "borrow_shared": BorrowShared,
    "borrow_mut": BorrowMut,
    "end_loan": EndLoan,
    "read": Read,
    "write": Write,
    "init": Init,
    "drop": Drop,
    "activate": Activate,
    "end_scope": EndScope,
}

# v2 才允许的操作。
_V2_ONLY_OPS = frozenset({"init", "drop", "activate", "end_scope"})


@dataclass
class Block:
    """基本块：有序操作列表。"""

    ops: List[Operation] = field(default_factory=list)


@dataclass
class CFG:
    """控制流图。

    :param blocks: 下标即块 id（v1 最多 50 块，v2 最多 80 块）。
    :param successors: ``blocks[i]`` 的后继块下标列表（CFG 边）。
    :param entry: 入口块下标。
    :param version: 接口版本 1 或 2。
    :param conditions: v2 布尔条件名（至多 8 个，顺序固定，
        第 j 个对应赋值位的第 j 位，决定证据输出的并列次序）。
    :param guards: v2 边守卫 :class:`Formula`，键为 ``(源块, 目标块)``；
        未登记的边视为恒真。
    """

    blocks: List[Block]
    successors: List[List[int]]
    entry: int = 0
    version: int = 1
    conditions: Tuple[str, ...] = ()
    guards: Dict[Tuple[int, int], "Formula"] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 命题公式（有限命题逻辑）：解析后的不可变对象。
# 条件数 <= 8，赋值空间 <= 256；分析器把公式编译成位掩码真值表
# （第 b 位置 1 当且仅当公式在整数编码 b 的赋值下为真）。
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Formula:
    """有限命题公式（``not`` / ``and`` / ``or`` / 条件名 / const）。"""

    tree: object

    @staticmethod
    def build(tree: object, loc: str = "formula") -> "Formula":
        """从纯数据构造并做形状校验（条件名存在性在 CFG 校验时做）。"""
        _validate_formula_shape(tree, loc, depth=0, nodes=[0])
        return Formula(tree=tree)

    def compile(self, condition_index: Dict[str, int]) -> int:
        """编译为赋值空间上的位掩码真值表。"""
        k = len(condition_index)
        size = 1 << k

        def ev(node: object) -> int:
            if isinstance(node, str):
                if node not in condition_index:
                    raise CFGValidationError(
                        f"公式引用了未声明的条件 {node!r}", "cfg.conditions")
                j = condition_index[node]
                mask = 0
                for b in range(size):
                    if (b >> j) & 1:
                        mask |= 1 << b
                return mask
            if isinstance(node, (list, tuple)):
                if not node:
                    raise CFGValidationError("公式节点不能为空", "formula")
                op = node[0]
                args = node[1:]
                if op == "const":
                    if len(args) != 1 or not isinstance(args[0], bool):
                        raise CFGValidationError(
                            "const 必须带一个布尔参数", "formula")
                    return (1 << size) - 1 if args[0] else 0
                if op == "not":
                    if len(args) != 1:
                        raise CFGValidationError("not 必须带一个参数", "formula")
                    return (~ev(args[0])) & ((1 << size) - 1)
                if op in ("and", "or"):
                    acc = (1 << size) - 1 if op == "and" else 0
                    for a in args:
                        m = ev(a)
                        acc = (acc & m) if op == "and" else (acc | m)
                    return acc
                raise CFGValidationError(
                    f"未知公式连接符 {op!r}（允许 not/and/or/const）", "formula")
            raise CFGValidationError(
                f"公式节点必须是条件名或列表，得到 {type(node).__name__}",
                "formula")

        return ev(self.tree)


FORMULA_MAX_NODES = 200
FORMULA_MAX_DEPTH = 32


def _validate_formula_shape(node: object, loc: str, *, depth: int,
                            nodes: List[int]) -> None:
    if depth > FORMULA_MAX_DEPTH:
        raise CFGValidationError(f"公式嵌套深度超过 {FORMULA_MAX_DEPTH}", loc)
    nodes[0] += 1
    if nodes[0] > FORMULA_MAX_NODES:
        raise CFGValidationError(f"公式节点数超过 {FORMULA_MAX_NODES}", loc)
    if isinstance(node, str):
        if node == "":
            raise CFGValidationError("条件名必须是非空字符串", loc)
        return
    if isinstance(node, (list, tuple)):
        if not node:
            raise CFGValidationError("公式节点不能为空", loc)
        op = node[0]
        if not isinstance(op, str):
            raise CFGValidationError("公式连接符必须是字符串", loc)
        if op == "const":
            if len(node) != 2 or not isinstance(node[1], bool):
                raise CFGValidationError("const 必须带一个布尔参数", loc)
            return
        if op == "not":
            if len(node) != 2:
                raise CFGValidationError("not 必须带一个参数", loc)
        elif op in ("and", "or"):
            if len(node) < 2:
                raise CFGValidationError(f"{op} 至少带一个参数", loc)
        else:
            raise CFGValidationError(
                f"未知公式连接符 {op!r}（允许 not/and/or/const）", loc)
        for i, sub in enumerate(node[1:], start=1):
            _validate_formula_shape(sub, f"{loc}[{i}]", depth=depth + 1,
                                    nodes=nodes)
        return
    raise CFGValidationError(
        f"公式节点必须是条件名字符串或列表，得到 {type(node).__name__}", loc)


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


def _require_keys(data: dict, allowed: FrozenSet[str], loc: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise CFGValidationError(
            f"未知字段 {sorted(unknown)}（允许的字段：{sorted(allowed)}）", loc
        )


def operation_from_dict(data: object, loc: str, *, version: int = 1) -> Operation:
    """把一个纯数据操作描述转换为 :data:`Operation`，同时做严格校验。"""
    if not isinstance(data, dict):
        raise CFGValidationError("操作必须是对象", loc)
    kind = data.get("op")
    if kind not in _OP_KINDS:
        raise CFGValidationError(
            f"op 必须是 {sorted(_OP_KINDS)} 之一，得到 {kind!r}", f"{loc}.op"
        )
    if version < 2 and kind in _V2_ONLY_OPS:
        raise CFGValidationError(
            f"{kind} 只在 version=2 中可用（当前 CFG 为 v1）", f"{loc}.op")
    try:
        if kind == "declare":
            allowed = {"op", "var", "ty"}
            if version >= 2:
                allowed |= {"init", "scope"}
            _require_keys(data, frozenset(allowed), loc)
            var = data.get("var")
            if not _is_ident(var):
                raise CFGValidationError("var 必须是非空字符串", f"{loc}.var")
            ty = _validate_type(data.get("ty"), f"{loc}.ty")
            if version >= 2:
                initialized = data.get("init", False)
                if not isinstance(initialized, bool):
                    raise CFGValidationError(
                        "init 必须是布尔值（v2 declare 默认未初始化）", f"{loc}.init")
                scope = data.get("scope", DEFAULT_SCOPE)
                if not _is_ident(scope):
                    raise CFGValidationError("scope 必须是非空字符串", f"{loc}.scope")
                return Decl(var=var, ty=ty, initialized=initialized, scope=scope)  # type: ignore[arg-type]
            return Decl(var=var, ty=ty)  # type: ignore[arg-type]
        if kind in ("move", "read"):
            _require_keys(data, frozenset({"op", "path"}), loc)
            path = _validate_path(data.get("path"), f"{loc}.path")
            cls = Move if kind == "move" else Read
            return cls(path=path)
        if kind in ("init", "drop"):
            _require_keys(data, frozenset({"op", "path"}), loc)
            path = _validate_path(data.get("path"), f"{loc}.path")
            cls = Init if kind == "init" else Drop
            return cls(path=path)
        if kind == "borrow_shared":
            _require_keys(data, frozenset({"op", "loan_id", "path"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            path = _validate_path(data.get("path"), f"{loc}.path")
            return BorrowShared(loan_id=loan_id, path=path)  # type: ignore[arg-type]
        if kind == "borrow_mut":
            allowed = {"op", "loan_id", "path"}
            if version >= 2:
                allowed.add("two_phase")
            _require_keys(data, frozenset(allowed), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            path = _validate_path(data.get("path"), f"{loc}.path")
            two_phase = False
            if version >= 2:
                two_phase = data.get("two_phase", False)
                if not isinstance(two_phase, bool):
                    raise CFGValidationError("two_phase 必须是布尔值", f"{loc}.two_phase")
            return BorrowMut(loan_id=loan_id, path=path, two_phase=two_phase)  # type: ignore[arg-type]
        if kind == "end_loan":
            _require_keys(data, frozenset({"op", "loan_id"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            return EndLoan(loan_id=loan_id)
        if kind == "activate":
            _require_keys(data, frozenset({"op", "loan_id"}), loc)
            loan_id = data.get("loan_id")
            if not _is_ident(loan_id):
                raise CFGValidationError("loan_id 必须是非空字符串", f"{loc}.loan_id")
            return Activate(loan_id=loan_id)
        if kind == "end_scope":
            _require_keys(data, frozenset({"op", "scope"}), loc)
            scope = data.get("scope", DEFAULT_SCOPE)
            if not _is_ident(scope):
                raise CFGValidationError("scope 必须是非空字符串", f"{loc}.scope")
            return EndScope(scope=scope)
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

    v1（无 ``version`` 字段或 ``"version": 1``）语义冻结：块数 1..50、
    七种旧操作、旧错误类型与确定性结果全部保留。``"version": 2``
    打开布尔条件、守卫边、初始化/析构与两阶段借用。
    """
    loc = "cfg"
    if not isinstance(data, dict):
        raise CFGValidationError("CFG 必须是对象", loc)
    version = data.get("version", 1)
    checked_int("version", version, min_value=1)
    if version > 2:
        raise CFGValidationError(f"不支持的版本 {version}（支持 1、2）", "cfg.version")
    allowed = {"blocks", "successors", "entry", "version"}
    if version >= 2:
        allowed.add("conditions")
    _require_keys(data, frozenset(allowed), loc)

    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise CFGValidationError("blocks 必须是列表", "cfg.blocks")
    if not raw_blocks:
        raise CFGValidationError("至少需要 1 个块", "cfg.blocks")
    max_blocks = 80 if version >= 2 else 50
    if len(raw_blocks) > max_blocks:
        raise CFGValidationError(
            f"块数上限：v1 为 50，v2 为 80；当前 version={version}，"
            f"得到 {len(raw_blocks)} 块", "cfg.blocks")

    blocks: List[Block] = []
    for bi, raw_block in enumerate(raw_blocks):
        bloc = f"blocks[{bi}]"
        if not isinstance(raw_block, dict) or not isinstance(raw_block.get("ops"), list):
            raise CFGValidationError("块必须是含 ops 列表的对象", f"{bloc}.ops")
        if len(raw_block["ops"]) > 10000:
            raise CFGValidationError("单块操作数上限为 10000", f"{bloc}.ops")
        ops = [
            operation_from_dict(raw_op, f"{bloc}.ops[{oi}]", version=version)
            for oi, raw_op in enumerate(raw_block["ops"])
        ]
        blocks.append(Block(ops=ops))

    # v2：布尔条件表。
    conditions: Tuple[str, ...] = ()
    if version >= 2:
        raw_conds = data.get("conditions", [])
        if not isinstance(raw_conds, list) or len(raw_conds) > 8:
            raise CFGValidationError(
                "conditions 必须是至多 8 个条件名的列表", "cfg.conditions")
        seen_cond: set[str] = set()
        for ci, c in enumerate(raw_conds):
            if not _is_ident(c):
                raise CFGValidationError(
                    f"条件名必须是非空字符串，得到 {c!r}",
                    f"cfg.conditions[{ci}]")
            if c in seen_cond:
                raise CFGValidationError(
                    f"条件名 {c!r} 重复", f"cfg.conditions[{ci}]")
            seen_cond.add(c)
        conditions = tuple(raw_conds)

    raw_succ = data.get("successors")
    if not isinstance(raw_succ, list) or len(raw_succ) != len(blocks):
        raise CFGValidationError(
            "successors 必须是与 blocks 等长的列表", "cfg.successors")
    successors: List[List[int]] = []
    guards: Dict[Tuple[int, int], Formula] = {}
    for bi, raw_list in enumerate(raw_succ):
        sloc = f"successors[{bi}]"
        if not isinstance(raw_list, list):
            raise CFGValidationError("每个后继列表必须是列表", sloc)
        succ: List[int] = []
        for si, raw_entry in enumerate(raw_list):
            eloc = f"{sloc}[{si}]"
            formula: Optional[Formula] = None
            if isinstance(raw_entry, dict):
                if version < 2:
                    raise CFGValidationError(
                        "对象形式后继 {\"block\", \"when\"} 仅在 version=2 可用", eloc)
                _require_keys(raw_entry, frozenset({"block", "when"}), eloc)
                s = raw_entry.get("block")
                checked_int("successor index", s, min_value=0)
                formula = Formula.build(raw_entry.get("when"), f"{eloc}.when")
            else:
                if isinstance(raw_entry, bool) or not isinstance(raw_entry, int):
                    checked_int("successor index", raw_entry, min_value=0)
                s = raw_entry
            s = int(s)
            if s >= len(blocks):
                raise CFGValidationError(f"后继块下标 {s} 越界", sloc)
            if s in succ:
                raise CFGValidationError("同一后继在列表中重复（请用 or 公式合并守卫）", sloc)
            succ.append(s)
            if formula is not None:
                guards[(bi, s)] = formula
        successors.append(succ)

    entry = data.get("entry", 0)
    checked_int("entry", entry, min_value=0)
    if entry >= len(blocks):
        raise CFGValidationError(f"入口块下标 {entry} 越界", "cfg.entry")
    cfg = CFG(blocks=blocks, successors=successors, entry=entry,
              version=version, conditions=conditions, guards=guards)
    # 构造即完成全部语义校验；失败时不返回任何对象（无部分结果）。
    from .validator import validate_cfg

    validate_cfg(cfg)
    return cfg
