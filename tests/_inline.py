"""测试辅助：把含片段调用的 Program **真正展开**成一个普通主 CFG。

这是与被测核心完全独立的一条路线：纯 dict 变换，不 import 被测核心，
用于和 ``check_program`` 的摘要式调用检查逐项对照。

展开语义与摘要约定严格一致：

* 片段是无环小 CFG：按块克隆、按拓扑边相连；每个片段出口块（补局部
  ``end_loan`` 后）都连到调用点之后的同一个续延块，may 并集由主 CFG 的
  普通分支合并自然得到；
* 片段局部 loan 在**每个片段出口块**末尾补一条 ``end_loan``（作用域硬
  清除），保证不泄漏到调用方——与“局部借用在片段退出时结束”一致；
* 每次调用的 loan 重命名为 ``c{块}_{操作}__{片段内loan}``，不同调用点
  身份互不相同（重复调用不串用 loan）；
* 形参路径按实参字面替换。

返回 ``origins``：每个展开块的每条操作来源标签，供把普通 CFG 上的冲突
位置映射回规范坐标 ``(主块, 主操作, 片段块|None, 片段操作|None)``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ("main", b, oi)            主 CFG 操作
# ("seg", cb, co, sb, so)    片段内操作
# ("end", cb, co, loan)      出口补的 end_loan（永不产生冲突）
Tag = Tuple[Any, ...]
# 规范冲突坐标：(主块, 主操作, 片段块|None, 片段操作|None)
NormKey = Tuple[str, int, int, Optional[int], Optional[int]]


class _Builder:
    def __init__(self) -> None:
        self.blocks: List[Dict[str, Any]] = []
        self.origins: List[List[Tag]] = []
        self.edges: List[Tuple[int, int]] = []

    def block(self) -> int:
        idx = len(self.blocks)
        self.blocks.append({"ops": []})
        self.origins.append([])
        return idx

    def op(self, b: int, op: Dict[str, Any], tag: Tag) -> None:
        self.blocks[b]["ops"].append(op)
        self.origins[b].append(tag)

    def edge(self, src: int, dst: int) -> None:
        self.edges.append((src, dst))


def _instantiate_seg_op(sop: Dict[str, Any], subst: Dict[str, Tuple[str, ...]],
                        rename: Any) -> Dict[str, Any]:
    kind = sop["op"]

    def sub(path: List[str]) -> List[str]:
        return [*subst[path[0]], *path[1:]]

    if kind in ("move", "read"):
        return {"op": kind, "path": sub(sop["path"])}
    if kind == "write":
        return {"op": "write", "path": sub(sop["path"]),
                "reinit": sop.get("reinit", False)}
    if kind in ("borrow_shared", "borrow_mut"):
        return {"op": kind, "loan_id": rename(sop["loan_id"]),
                "path": sub(sop["path"])}
    if kind == "end_loan":
        return {"op": "end_loan", "loan_id": rename(sop["loan_id"])}
    raise AssertionError(f"片段内非法操作 {kind!r}")  # pragma: no cover


def inline_program(program: Dict[str, Any]
                   ) -> Tuple[Dict[str, Any], List[List[Tag]]]:
    """把 Program 展开为普通 CFG（纯数据），返回 (cfg_dict, origins)。"""
    cfg = program["cfg"]
    segments = program.get("segments", {})
    main_blocks = cfg["blocks"]
    main_succ = cfg["successors"]
    entry = cfg.get("entry", 0)

    bld = _Builder()
    starts: Dict[int, int] = {}
    ends: Dict[int, int] = {}

    for b, mblock in enumerate(main_blocks):
        start = bld.block()
        starts[b] = start
        cur = start
        for oi, mop in enumerate(mblock["ops"]):
            if mop.get("op") != "call":
                bld.op(cur, mop, ("main", b, oi))
                continue

            seg_name = mop["segment"]
            seg = segments[seg_name]
            subst = {p: tuple(a) for p, a in mop["args"]}

            def rename(lid: str, _b=b, _oi=oi) -> str:
                return f"c{_b}_{_oi}__{lid}"

            # 克隆片段块。
            smap: Dict[int, int] = {}
            local_loans: List[str] = []
            for sb, sblock in enumerate(seg["blocks"]):
                nb = bld.block()
                smap[sb] = nb
                for so, sop in enumerate(sblock["ops"]):
                    if sop["op"] in ("borrow_shared", "borrow_mut"):
                        local_loans.append(sop["loan_id"])
                    bld.op(nb, _instantiate_seg_op(sop, subst, rename),
                           ("seg", b, oi, sb, so))

            # 调用前块 -> 片段入口。
            call_entry = smap[seg["entry"]]
            bld.edge(cur, call_entry)
            # 片段内部边（无环）。
            for sx, dsts in enumerate(seg["successors"]):
                for sy in dsts:
                    bld.edge(smap[sx], smap[sy])
            # 出口块补局部 end_loan，再连到新续延块。
            cont = bld.block()
            for sb, dsts in enumerate(seg["successors"]):
                if dsts:
                    continue
                xb = smap[sb]
                for lid in local_loans:
                    bld.op(xb, {"op": "end_loan", "loan_id": rename(lid)},
                           ("end", b, oi, lid))
                bld.edge(xb, cont)
            cur = cont
        ends[b] = cur

    # 主 CFG 边：末端展开块 -> 后继主块的起始展开块。
    for b, dsts in enumerate(main_succ):
        for s in dsts:
            bld.edge(ends[b], starts[s])

    n = len(bld.blocks)
    successors: List[List[int]] = [[] for _ in range(n)]
    for src, dst in bld.edges:
        successors[src].append(dst)
    for lst in successors:
        # 去重保序（同一对边不应重复）。
        seen: set = set()
        deduped = []
        for x in lst:
            if x not in seen:
                seen.add(x)
                deduped.append(x)
        lst[:] = deduped

    inlined = {
        "blocks": bld.blocks,
        "successors": successors,
        "entry": starts[entry],
    }
    return inlined, bld.origins


def normalize_conflict_key(tag_block: int, tag_op: int,
                           origins: List[List[Tag]]) -> NormKey:
    """把展开 CFG 上的 (块, 操作) 映射为规范冲突坐标。

    返回 ``(kind 占位由调用方填, 主块, 主操作, 片段块, 片段操作)``；
    主 CFG 操作的片段坐标为 ``None``。出口补的 end_loan 不会产生冲突。
    """
    tag = origins[tag_block][tag_op]
    if tag[0] == "main":
        return ("", tag[1], tag[2], None, None)
    if tag[0] == "seg":
        return ("", tag[1], tag[2], tag[3], tag[4])
    raise AssertionError(f"出口补的 end_loan 不应产生冲突：{tag}")  # pragma: no cover
