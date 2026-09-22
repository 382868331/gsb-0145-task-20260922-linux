"""独立参考语义（仅用于测试对照，**不得 import 被测核心**）。

与被测核心完全不同的实现路线：

1. 枚举记录类型树上的**全部有限路径**，may-moved 用位向量表示：
   ``move(p)`` 把所有与 ``p`` 重叠的路径置位；``reinit(p)`` 只清除
   ``p`` 子树；祖先移动对兄弟字段的压制在位向量中自然成立。
2. 对**无环** CFG（DAG）枚举从入口到汇点的所有块序列，对每条序列做
   精确（逐路径）执行解释，再按 may 语义取并集。
3. 活跃借用沿块序列精确维护集合。

被测核心用反链 + 单调不动点；本参考用有限位向量 + 全路径枚举。
两者在 DAG 上的冲突集合（kind, 块, 操作）必须一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple

# 与 borrow_check 的冲突 kind 字符串保持一致（语义规则各自独立编码）。
ConflictKey = Tuple[str, int, int]


def _all_paths(ty: Any, prefix: Tuple[str, ...]) -> List[Tuple[str, ...]]:
    out = [prefix]
    if isinstance(ty, dict):
        for f, sub in ty.items():
            out.extend(_all_paths(sub, (*prefix, f)))
    return out


def _overlap(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    return a[: len(b)] == b if len(a) >= len(b) else b[: len(a)] == a


def reference_conflicts(cfg_dict: Dict[str, Any]) -> Set[ConflictKey]:
    """返回 DAG 形式 CFG（纯数据）上参考语义认定的冲突集合。"""
    blocks = cfg_dict["blocks"]
    succs = cfg_dict["successors"]
    entry = cfg_dict.get("entry", 0)
    n = len(blocks)

    # 1) 类型树全路径 -> 位下标
    types: Dict[str, Any] = {}
    for b in blocks:
        for op in b["ops"]:
            if op["op"] == "declare":
                types[op["var"]] = op.get("ty")
    paths: List[Tuple[str, ...]] = []
    for root, ty in types.items():
        paths.extend(_all_paths(ty, (root,)))
    index = {p: i for i, p in enumerate(paths)}

    loan_path: Dict[str, Tuple[str, ...]] = {}
    loan_mode: Dict[str, str] = {}
    for b in blocks:
        for op in b["ops"]:
            if op["op"] in ("borrow_shared", "borrow_mut"):
                loan_path[op["loan_id"]] = tuple(op["path"])
                loan_mode[op["loan_id"]] = (
                    "mut" if op["op"] == "borrow_mut" else "shared"
                )

    # 2) 枚举 DAG 上全部入口->汇点块序列（调用方保证 n 很小）。
    sinks = [i for i in range(n) if not succs[i]]
    walks: List[Tuple[int, ...]] = []

    def enumerate_walks(start: int) -> None:
        # DAG 上简单 DFS；边只连向更大下标的生成器保证无环。
        stack: List[Tuple[int, Tuple[int, ...]]] = [(start, (start,))]
        while stack:
            node, seq = stack.pop()
            if not succs[node]:
                walks.append(seq)
                continue
            for s in succs[node]:
                stack.append((s, (*seq, s)))

    enumerate_walks(entry)
    if not walks and sinks:
        # entry 无后继时 seq=(entry,) 已被加入；此处仅防御。
        walks = [(entry,)]

    conflicts: Set[ConflictKey] = set()

    # 3) 逐块序列精确解释。
    for seq in walks:
        moved = [False] * len(paths)
        active: Set[str] = set()

        def moved_overlap(p: Tuple[str, ...]) -> bool:
            # 位向量存的是外延（每条不可用路径各占一位）：do_move 已把
            # 被移动路径的全部子孙置位，故直接查 p 自己那一位即可。
            return moved[index[p]]

        def do_move(p: Tuple[str, ...]) -> None:
            # 只标记 p 自身及其子孙（父路径移动 -> 子路径不可用，反向不成立）。
            for i, q in enumerate(paths):
                if len(q) >= len(p) and q[: len(p)] == p:
                    moved[i] = True

        def do_reinit(p: Tuple[str, ...]) -> None:
            for i, q in enumerate(paths):
                if q == p or (len(q) > len(p) and q[: len(p)] == p):
                    moved[i] = False

        def active_overlap(p: Tuple[str, ...], mode: str) -> List[str]:
            hit = []
            for lid in active:
                if not _overlap(loan_path[lid], p):
                    continue
                if mode == "shared" and loan_mode[lid] != "mut":
                    continue
                hit.append(lid)
            return hit

        for bi in seq:
            for oi, op in enumerate(blocks[bi]["ops"]):
                kind_op = op["op"]
                if kind_op == "declare":
                    continue
                p = tuple(op["path"]) if "path" in op else None
                if kind_op == "move":
                    if moved_overlap(p):
                        conflicts.add(("use_of_moved", bi, oi))
                    if active_overlap(p, "mut_any_mode"):
                        conflicts.add(("move_while_borrowed", bi, oi))
                    do_move(p)
                elif kind_op == "borrow_shared":
                    if moved_overlap(p):
                        conflicts.add(("use_of_moved", bi, oi))
                    if active_overlap(p, "shared"):
                        conflicts.add(("borrow_conflict", bi, oi))
                    active.add(op["loan_id"])
                elif kind_op == "borrow_mut":
                    if moved_overlap(p):
                        conflicts.add(("use_of_moved", bi, oi))
                    if active_overlap(p, "mut_any_mode"):
                        conflicts.add(("borrow_conflict", bi, oi))
                    active.add(op["loan_id"])
                elif kind_op == "end_loan":
                    active.discard(op["loan_id"])
                elif kind_op == "read":
                    if moved_overlap(p):
                        conflicts.add(("use_of_moved", bi, oi))
                    if active_overlap(p, "shared"):
                        conflicts.add(("read_while_mut_borrowed", bi, oi))
                elif kind_op == "write":
                    if active_overlap(p, "mut_any_mode"):
                        conflicts.add(("write_while_borrowed", bi, oi))
                    if op.get("reinit", False):
                        do_reinit(p)
                    elif moved_overlap(p):
                        conflicts.add(("use_of_moved", bi, oi))

    return conflicts
