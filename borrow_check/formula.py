"""有限命题公式（v2 生命周期分析用）。

两层表示：

* :class:`Atom` / :class:`Not` / :class:`And` / :class:`Or` 构成的不可变
  表达式树（AST），来自 CFG 数据描述，用于校验、求值与展示；
* 给定全部原子的注册次序后，编译为 **真值表位向量**：k 个原子时共有
  ``2**k`` 个赋值，整数 ``table`` 的第 ``r`` 位就是公式在赋值 ``r`` 下的
  真值（``r`` 的第 ``i`` 位对应注册的第 ``i`` 个原子）。合取/析取/否定
  分别退化成整数按位 ``&``/``|``/``^``，可满足 = 非零。

本题限制 k <= 8，因此表最多 256 位，所有运算都是有界常数时间小整数运算，
不存在迭代发散；同一 CFG 的原子按名字排序注册，结果确定可复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Sequence, Tuple, Union

from .errors import CFGValidationError

MAX_CONDITIONS = 8
MAX_EXPR_NODES = 1000


@dataclass(frozen=True)
class Atom:
    name: str


@dataclass(frozen=True)
class Not:
    inner: "Expr"


@dataclass(frozen=True)
class And:
    parts: Tuple["Expr", ...]


@dataclass(frozen=True)
class Or:
    parts: Tuple["Expr", ...]


Expr = Union[Atom, Not, And, Or]


def atoms_of(expr: Expr) -> FrozenSet[str]:
    if isinstance(expr, Atom):
        return frozenset({expr.name})
    if isinstance(expr, Not):
        return atoms_of(expr.inner)
    return frozenset().union(*(atoms_of(p) for p in expr.parts))


def evaluate(expr: Expr, assignment: Dict[str, bool]) -> bool:
    """在给定原子真值赋值下求公式值（供具体重放使用）。"""
    if isinstance(expr, Atom):
        return bool(assignment[expr.name])
    if isinstance(expr, Not):
        return not evaluate(expr.inner, assignment)
    if isinstance(expr, And):
        return all(evaluate(p, assignment) for p in expr.parts)
    return any(evaluate(p, assignment) for p in expr.parts)


def expr_from_dict(data: Any, loc: str, _depth: List[int] | None = None) -> Expr:
    """严格解析纯数据公式描述。

    形式：原子为非空字符串；``{"not": e}``；``{"and": [e, ...]}``；
    ``{"or": [e, ...]}``（合取/析取至少 2 个支）。
    """
    counter = _depth if _depth is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_EXPR_NODES:
        raise CFGValidationError(
            f"公式节点数超过上限 {MAX_EXPR_NODES}", loc)
    if isinstance(data, str):
        if data == "":
            raise CFGValidationError("条件原子名必须是非空字符串", loc)
        return Atom(data)
    if not isinstance(data, dict):
        raise CFGValidationError(
            "公式必须是原子字符串或 {'not'|'and'|'or': ...} 对象", loc)
    keys = set(data)
    if len(keys) != 1:
        raise CFGValidationError(
            f"公式对象只能有一个连接词字段，得到 {sorted(keys)}", loc)
    (key,) = tuple(keys)
    if key == "not":
        return Not(expr_from_dict(data[key], f"{loc}.not", counter))
    if key in ("and", "or"):
        parts_data = data[key]
        if not isinstance(parts_data, list) or len(parts_data) < 2:
            raise CFGValidationError(
                f"{key!r} 必须是至少含 2 个支的列表", f"{loc}.{key}")
        parts = tuple(
            expr_from_dict(sub, f"{loc}.{key}[{i}]", counter)
            for i, sub in enumerate(parts_data))
        return And(parts) if key == "and" else Or(parts)
    raise CFGValidationError(
        f"未知连接词 {key!r}（允许 not/and/or）", f"{loc}.{key}")


def render_expr(expr: Expr) -> str:
    """确定性的人类可读形式。"""
    if isinstance(expr, Atom):
        return expr.name
    if isinstance(expr, Not):
        return f"!{render_expr(expr.inner)}"
    if isinstance(expr, And):
        return "(" + " & ".join(render_expr(p) for p in expr.parts) + ")"
    return "(" + " | ".join(render_expr(p) for p in expr.parts) + ")"


# --------------------------------------------------------------------------
# 编译到真值表
# --------------------------------------------------------------------------


class TruthTables:
    """固定原子注册次序下的真值表代数。

    :param names: 全部原子名，按字典序排序后注册；下标即位序。
    """

    def __init__(self, names: Sequence[str]) -> None:
        if len(names) > MAX_CONDITIONS:
            raise CFGValidationError(
                f"布尔条件数 {len(names)} 超过上限 {MAX_CONDITIONS}",
                "cfg.conditions")
        self.names: Tuple[str, ...] = tuple(names)
        self.index: Dict[str, int] = {name: i for i, name in enumerate(self.names)}
        self.k = len(self.names)
        self.rows = 1 << self.k
        self.full: int = (1 << self.rows) - 1
        self.false: int = 0
        self.true: int = self.full
        self._lit_cache: Dict[Tuple[int, bool], int] = {}

    def literal(self, name: str, polarity: bool = True) -> int:
        i = self.index[name]
        key = (i, polarity)
        cached = self._lit_cache.get(key)
        if cached is not None:
            return cached
        table = 0
        for row in range(self.rows):
            if bool((row >> i) & 1) == polarity:
                table |= 1 << row
        self._lit_cache[key] = table
        return table

    def compile(self, expr: Expr) -> int:
        if isinstance(expr, Atom):
            return self.literal(expr.name)
        if isinstance(expr, Not):
            return self.full ^ self.compile(expr.inner)
        if isinstance(expr, And):
            table = self.full
            for p in expr.parts:
                table &= self.compile(p)
            return table
        table = 0
        for p in expr.parts:
            table |= self.compile(p)
        return table

    def sat(self, table: int) -> bool:
        return table != 0

    def models(self, table: int) -> List[int]:
        return [r for r in range(self.rows) if (table >> r) & 1]

    def assignment_of(self, row: int) -> Dict[str, bool]:
        return {name: bool((row >> i) & 1)
                for i, name in enumerate(self.names)}

    def render_table(self, table: int) -> str:
        """渲染成析取范式（每个可满足赋值一支），供证据展示。"""
        models = self.models(table)
        if not models:
            return "false"
        if len(models) == self.rows:
            return "true"
        branches = []
        for row in models:
            assign = self.assignment_of(row)
            lit = " & ".join(
                (name if assign[name] else f"!{name}")
                for name in self.names)
            branches.append(f"({lit})" if self.k > 1 else (lit or "true"))
        return " | ".join(branches)
