"""操作片段摘要：前置条件、状态变化与无环执行计划。

片段（:class:`borrow_check.Segment`）是由已有七种指令组成的**无环**小
CFG，只有显式路径形参与局部 loan。本模块在分析主 CFG 之前，对每个片段
**一次性**形成一份摘要 :class:`SegmentSummary`：

* ``topo``：Kahn 拓扑序（片段无环，摘要不做片段内不动点）；
* ``preconditions``：每个片段操作对“到达该操作的状态”的要求（按拓扑块序
  排列）。要求是按操作种类给出的标签（如 ``read`` 要求路径不“可能已移动”
  且无重叠的活跃可变借用）；
* ``effects``：片段声明的状态变化动作（``move`` 的子树移动、``write`` 的
  ``reinit`` 写回）。移动与 reinit 写回在调用后**留给调用方状态**；
* ``local_loans`` / ``loans_scoped_out``：片段局部借用，在片段退出时一律
  结束（作用域硬清除，不泄漏给调用方）。

形参不假定互不重叠
==================

摘要本身用**符号形参根**描述路径（如 ``("p", "x")``），它不预设两个形参
替换后是否重叠。真正的判定发生在每次调用时：调用执行器按实参把符号路径
替换成调用方 CFG 中的真实路径，再在真实状态上跑同一份块传输——因此两个
形参指向同一字段、或父子别名时，冲突在真实路径上被自然发现。摘要的动作/
要求清单是稳定事实，权威结论以替换后的实例化执行为准。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .errors import CFGValidationError
from .model import (
    BorrowMut,
    BorrowShared,
    EndLoan,
    Move,
    Path,
    Read,
    Segment,
    Write,
)


@dataclass(frozen=True)
class Precondition:
    """片段内一个操作对到达状态的前置要求。

    :param seg_block / seg_op: 片段内坐标（块下标、操作下标）。
    :param op_name: 操作名（``move``/``borrow_mut``/...）。
    :param symbolic_path: 片段内符号路径（end_loan 为 ``None``）。
    :param requirement: 要求标签元组，取值：

        * ``"not_moved"``：路径不得“可能已移动”；
        * ``"no_overlapping_loan"``：不得有任何重叠的活跃借用；
        * ``"no_overlapping_mut_loan"``：不得有重叠的活跃**可变**借用
          （共享借用允许共存）。
    """

    seg_block: int
    seg_op: int
    op_name: str
    symbolic_path: Path
    requirement: Tuple[str, ...]


@dataclass(frozen=True)
class SegmentEffect:
    """片段声明的一个写回状态变化动作。

    :param kind: ``"move"``（子树移动，压制自身及子孙）或
        ``"reinit"``（只恢复该路径子树）。
    :param symbolic_path: 片段内符号路径。
    """

    kind: str
    seg_block: int
    seg_op: int
    symbolic_path: Path


@dataclass(frozen=True)
class SegmentSummary:
    """片段的一次性摘要（前置条件 + 状态变化 + 无环执行计划）。"""

    name: str
    params: Tuple[str, ...]
    topo: Tuple[int, ...]
    exit_blocks: Tuple[int, ...]
    local_loans: Tuple[str, ...]
    preconditions: Tuple[Precondition, ...]
    effects: Tuple[SegmentEffect, ...]
    loans_scoped_out: Tuple[str, ...]


# 各操作对到达状态的前置要求标签（与 analyzer 的冲突种类一一对应）。
_REQUIREMENTS = {
    "move": ("not_moved", "no_overlapping_loan"),
    "borrow_shared": ("not_moved", "no_overlapping_mut_loan"),
    "borrow_mut": ("not_moved", "no_overlapping_loan"),
    "read": ("not_moved", "no_overlapping_mut_loan"),
    # reinit 写无论是否移动都执行恢复；普通写额外要求路径不“可能已移动”。
    "write": ("no_overlapping_loan", "not_moved"),
    "write_reinit": ("no_overlapping_loan",),
}


def _topo_order(succs: List[List[int]]) -> Tuple[int, ...]:
    n = len(succs)
    indeg = [0] * n
    for ss in succs:
        for s in ss:
            indeg[s] += 1
    # 按下标顺序出队，保证摘要与执行顺序确定。
    queue: deque[int] = deque(i for i in range(n) if indeg[i] == 0)
    order: List[int] = []
    while queue:
        b = queue.popleft()
        order.append(b)
        for s in sorted(succs[b]):
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(order) != n:
        # validator 已保证片段无环；这里仅作防御。
        raise CFGValidationError("片段 CFG 存在环，无法形成无环摘要", "segment")
    return tuple(order)


def summarize_segment(name: str, seg: Segment) -> SegmentSummary:
    """对片段形成一次性摘要（片段假定已经过 ``validate_segment``）。"""
    topo = _topo_order(seg.successors)
    exit_blocks = tuple(b for b in topo if not seg.successors[b])

    preconditions: List[Precondition] = []
    effects: List[SegmentEffect] = []
    local_loans: List[str] = []

    for bi in topo:
        for oi, op in enumerate(seg.blocks[bi].ops):
            if isinstance(op, Move):
                preconditions.append(Precondition(
                    bi, oi, "move", op.path, _REQUIREMENTS["move"]))
                effects.append(SegmentEffect("move", bi, oi, op.path))
            elif isinstance(op, BorrowShared):
                preconditions.append(Precondition(
                    bi, oi, "borrow_shared", op.path,
                    _REQUIREMENTS["borrow_shared"]))
                local_loans.append(op.loan_id)
            elif isinstance(op, BorrowMut):
                preconditions.append(Precondition(
                    bi, oi, "borrow_mut", op.path,
                    _REQUIREMENTS["borrow_mut"]))
                local_loans.append(op.loan_id)
            elif isinstance(op, Read):
                preconditions.append(Precondition(
                    bi, oi, "read", op.path, _REQUIREMENTS["read"]))
            elif isinstance(op, Write):
                key = "write_reinit" if op.reinit else "write"
                preconditions.append(Precondition(
                    bi, oi, "write", op.path, _REQUIREMENTS[key]))
                if op.reinit:
                    effects.append(SegmentEffect("reinit", bi, oi, op.path))
            elif isinstance(op, EndLoan):
                continue  # 结束局部 loan：无路径前置要求，也不产生写回效果。
            else:  # pragma: no cover - validator 已拒绝 Decl/Call
                raise CFGValidationError(
                    f"片段内非法操作 {type(op).__name__}",
                    f"segments.{name}.blocks[{bi}].ops[{oi}]")

    return SegmentSummary(
        name=name,
        params=tuple(seg.params),
        topo=topo,
        exit_blocks=exit_blocks,
        local_loans=tuple(local_loans),
        preconditions=tuple(preconditions),
        effects=tuple(effects),
        loans_scoped_out=tuple(local_loans),
    )


def build_summaries(segments: Dict[str, Segment]) -> Dict[str, SegmentSummary]:
    """对命名片段表逐个形成摘要。"""
    return {name: summarize_segment(name, seg) for name, seg in segments.items()}
