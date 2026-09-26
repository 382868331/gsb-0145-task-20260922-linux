"""独立参考语义（仅用于测试对照，**不得 import 被测核心**）。

与被测核心完全不同的实现路线：

1. 移动记录为（路径, 子树/精确）条目集合：``move(p)`` 记子树移动；
   ``reinit(p)`` 只恢复 ``p`` 子树，祖先移动拆分为「祖先精确移动 +
   沿途兄弟子树移动」。
2. 对**无环** CFG（DAG）枚举从入口到汇点的所有块序列，对每条序列做
   精确（逐路径）执行解释，再按 may 语义取并集。
3. 活跃借用沿块序列精确维护集合；预约（reserve/activate/end_reserve）
   的 activate/end_reserve 是 **must** 语义：只在所有经过该点的游走
   都满足条件时才生效（全有或全无），站点决策迭代到不动点。

被测核心用反链 + 单调不动点；本参考用有限位向量 + 全路径枚举。
两者在 DAG 上的冲突集合（kind, 块, 操作）必须一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

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

    # 1) 声明汇集：根变量 -> 记录树（移动/恢复判定要沿类型树展开兄弟字段）。
    types: Dict[str, Any] = {}
    for b in blocks:
        for op in b["ops"]:
            if op["op"] == "declare":
                types[op["var"]] = op.get("ty")

    loan_path: Dict[str, Tuple[str, ...]] = {}
    loan_mode: Dict[str, str] = {}
    reserve_path: Dict[str, Tuple[str, ...]] = {}
    for b in blocks:
        for op in b["ops"]:
            if op["op"] in ("borrow_shared", "borrow_mut"):
                loan_path[op["loan_id"]] = tuple(op["path"])
                loan_mode[op["loan_id"]] = (
                    "mut" if op["op"] == "borrow_mut" else "shared"
                )
            elif op["op"] == "reserve":
                reserve_path[op["reserve_id"]] = tuple(op["path"])

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

    # 3) 逐块序列精确解释。activate/end_reserve 的 must 决策
    #    （decisions: 站点 -> 是否允许）与状态互相依赖，迭代到不动点。
    def run(decisions: Dict[Tuple[int, int], bool]):
        conflicts: Set[ConflictKey] = set()
        # 每个 activate/end_reserve 站点在每条经过游走上观察到的状态。
        site_status: Dict[Tuple[int, int], Set[str]] = {}

        for seq in walks:
            # 移动记录：path -> "subtree"（压自身及子孙）/ "exact"（只压自身，
            # reinit 拆分祖先移动时产生）。逐条游走精确维护。
            moves: Dict[Tuple[str, ...], str] = {}
            active: Set[str] = set()
            # 预约状态：rid -> "reserved" / "activated"（单条游走内精确值）。
            resv: Dict[str, str] = {}

            def is_prefix(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
                return len(a) <= len(b) and b[: len(a)] == a

            def moved_overlap(p: Tuple[str, ...]) -> bool:
                # p 不可用：存在 p 的子树移动祖先（含相等），或 p 的精确移动。
                for m, kind in moves.items():
                    if kind == "subtree" and is_prefix(m, p):
                        return True
                    if kind == "exact" and m == p:
                        return True
                return False

            def do_move(p: Tuple[str, ...]) -> None:
                # 只标记 p 自身及其子孙（父路径移动 -> 子路径不可用，反向不成立）。
                for m, kind in moves.items():
                    if kind == "subtree" and is_prefix(m, p):
                        return  # 已有子树祖先移动，语义冗余
                for m in [m for m in moves
                          if len(m) > len(p) and is_prefix(p, m)]:
                    del moves[m]
                moves[p] = "subtree"

            def do_reinit(p: Tuple[str, ...]) -> None:
                # 只恢复 p 子树：落在 p 子树内的移动删除；p 的严格祖先
                # 子树移动被拆分——祖先本身记为精确移动，通往 p 沿途的
                # 兄弟字段各记为子树移动（中间路径节点恢复可用）。
                new: Dict[Tuple[str, ...], str] = {}
                for m, kind in moves.items():
                    if m == p or (len(m) > len(p) and is_prefix(p, m)):
                        continue  # p 子树恢复
                    if kind == "subtree" and len(m) < len(p) and is_prefix(m, p):
                        new[m] = "exact"  # 祖先对象本身仍可能已移动
                        for depth in range(len(m), len(p)):
                            prefix = p[:depth]
                            node = types[prefix[0]]
                            for seg in prefix[1:]:
                                node = node[seg]
                            if isinstance(node, dict):
                                nxt = p[depth]
                                for f in node:
                                    if f != nxt:
                                        new[prefix + (f,)] = "subtree"
                    else:
                        new.setdefault(m, kind)
                moves.clear()
                moves.update(new)

            def active_overlap(p: Tuple[str, ...], mode: str,
                               exclude: str = "") -> List[str]:
                # 占用 = 普通借用 + 预约（reserved 按共享、activated 按可变）。
                hit = []
                for lid in active:
                    if not _overlap(loan_path[lid], p):
                        continue
                    if mode == "shared" and loan_mode[lid] != "mut":
                        continue
                    hit.append(lid)
                for rid, st in resv.items():
                    if rid == exclude:
                        continue
                    if not _overlap(reserve_path[rid], p):
                        continue
                    if mode == "shared" and st != "activated":
                        continue
                    hit.append(rid)
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
                    elif kind_op == "reserve":
                        if moved_overlap(p):
                            conflicts.add(("use_of_moved", bi, oi))
                        if active_overlap(p, "shared"):
                            conflicts.add(("reserve_conflict", bi, oi))
                        resv[op["reserve_id"]] = "reserved"
                    elif kind_op == "activate":
                        rid = op["reserve_id"]
                        site_status.setdefault((bi, oi), set()).add(
                            resv.get(rid, "missing"))
                        if not decisions.get((bi, oi), False):
                            # must 不满足：全路径都不激活（全有或全无）。
                            conflicts.add(("activate_not_live", bi, oi))
                        else:
                            if active_overlap(reserve_path[rid], "mut_any_mode",
                                              exclude=rid):
                                conflicts.add(("activate_conflict", bi, oi))
                            resv[rid] = "activated"
                    elif kind_op == "end_reserve":
                        rid = op["reserve_id"]
                        site_status.setdefault((bi, oi), set()).add(
                            resv.get(rid, "missing"))
                        if not decisions.get((bi, oi), False):
                            conflicts.add(("end_not_live", bi, oi))
                        else:
                            resv.pop(rid, None)  # 迭代中间轮决策可能滞后
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

        return conflicts, site_status

    # 站点 must 决策的不动点：activate 要求所有经过游走都是 reserved；
    # end_reserve 要求所有经过游走都仍存活（reserved 或 activated）。
    decisions: Dict[Tuple[int, int], bool] = {}
    conflicts: Set[ConflictKey] = set()
    for _ in range(50):
        conflicts, site_status = run(decisions)
        new_decisions: Dict[Tuple[int, int], bool] = {}
        for (bi, oi), statuses in site_status.items():
            op = blocks[bi]["ops"][oi]
            if op["op"] == "activate":
                new_decisions[(bi, oi)] = statuses == {"reserved"}
            else:  # end_reserve
                new_decisions[(bi, oi)] = statuses <= {"reserved", "activated"}
        if new_decisions == decisions:
            break
        decisions = new_decisions
    else:  # pragma: no cover - 防御：DAG 上应在数轮内收敛
        raise AssertionError("预约 must 决策迭代未收敛")
    # 用最终决策再跑一次，拿到与稳定决策一致的冲突集合。
    conflicts, _ = run(decisions)

    return conflicts
