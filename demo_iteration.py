#!/usr/bin/env python3
"""reserve/activate 预约-激活借用的固定输入演示（全部现场计算）。

1. 合法：先预约修改对象，读取它的字段作为参数，再激活为独占借用；
2. 拒绝：同样的调用顺序，但激活前保留了一个共享借用 -> 激活被拒；
3. 拒绝：预约只出现在一个分支，汇合后激活 -> 不满足"所有路径都存活"；
4. 循环中的预约：回边后重复激活被拒；压缩预算演示
   "预算耗尽 != 证明无冲突"。

运行：python demo_iteration.py（毫秒级，远小于 90 秒；无 sleep、无预录结果）。
"""

from __future__ import annotations

import time

from borrow_check import (
    BudgetExhaustedError,
    cfg_from_dict,
    check,
)


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    started = time.perf_counter()

    # ------------------------------------------------------------------
    # 1) 合法：reserve -> read（共享读取参数）-> activate -> end -> write。
    # ------------------------------------------------------------------
    hr("1) 合法：先预约修改对象，读取其长度作为参数，再激活（clean）")
    good = cfg_from_dict({
        "blocks": [{"ops": [
            {"op": "declare", "var": "buf",
             "ty": {"len": None, "data": None}},
            {"op": "reserve", "reservation_id": "R", "path": ["buf", "data"]},
            {"op": "read", "path": ["buf", "len"]},   # 预约阶段允许共享读取
            {"op": "read", "path": ["buf", "data"]},  # 被预约路径本身也可读
            {"op": "activate", "reservation_id": "R"},  # 升级为独占借用
            {"op": "end_loan", "loan_id": "R"},
            {"op": "write", "path": ["buf", "data"]},
        ]}],
        "successors": [[]],
        "entry": 0,
    })
    result = check(good)
    print("结论：clean =", result.clean,
          f"（不动点块处理 {result.iterations} 次）")
    assert result.clean, result.explain()

    # ------------------------------------------------------------------
    # 2) 拒绝：激活前保留了共享借用，激活升级为独占时被拒。
    # ------------------------------------------------------------------
    hr("2) 拒绝：激活前保留共享借用（activate_conflict，附前驱见证链）")
    held = cfg_from_dict({
        "blocks": [{"ops": [
            {"op": "declare", "var": "buf",
             "ty": {"len": None, "data": None}},
            {"op": "reserve", "reservation_id": "R", "path": ["buf", "data"]},
            {"op": "borrow_shared", "loan_id": "S", "path": ["buf", "data"]},
            {"op": "read", "path": ["buf", "len"]},
            {"op": "activate", "reservation_id": "R"},  # S 未结束 -> 拒绝
        ]}],
        "successors": [[]],
        "entry": 0,
    })
    rejected = check(held)
    print("结论：clean =", rejected.clean)
    print(rejected.explain())
    assert [c.kind for c in rejected.conflicts] == ["activate_conflict"]

    # ------------------------------------------------------------------
    # 3) 拒绝：预约只在一个分支发生，汇合后激活不满足"所有路径都存活"。
    # ------------------------------------------------------------------
    hr("3) 拒绝：预约跨分支——只在分支 A 预约，汇合后激活（reservation_not_live）")
    branched = cfg_from_dict({
        "blocks": [
            {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
            {"ops": [{"op": "reserve", "reservation_id": "R",
                      "path": ["a", "x"]}]},
            {"ops": []},
            {"ops": [{"op": "activate", "reservation_id": "R"}]},
        ],
        "successors": [[1, 2], [3], [3], []],
        "entry": 0,
    })
    r3 = check(branched)
    print("结论：clean =", r3.clean)
    print(r3.explain())
    assert [c.kind for c in r3.conflicts] == ["reservation_not_live"]
    reserved_before_fork = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            ]},
            {"ops": []},
            {"ops": []},
            {"ops": [{"op": "activate", "reservation_id": "R"}]},
        ],
        "successors": [[1, 2], [3], [3], []],
        "entry": 0,
    })
    print("对照：同样的图但 reserve 在分叉之前，clean =",
          check(reserved_before_fork).clean)

    # ------------------------------------------------------------------
    # 4) 循环中的预约：第二轮激活被拒；预算耗尽与"证明无冲突"分离。
    # ------------------------------------------------------------------
    hr("4) 循环：回边后重复激活被拒；压缩预算 -> BudgetExhaustedError")
    looping = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            ]},
            {"ops": [{"op": "activate", "reservation_id": "R"}]},
        ],
        "successors": [[1], [1]],
        "entry": 0,
    })
    r4 = check(looping)
    print("结论：clean =", r4.clean, "（回边后预约已激活，第二轮 activate 被拒）")
    print(r4.explain())
    try:
        check(looping, max_iterations=1)
    except BudgetExhaustedError as exc:
        print()
        print(f"压缩预算后捕获 {type(exc).__name__}：{exc}")
    else:  # pragma: no cover
        raise AssertionError("预算必须被耗尽")

    elapsed = time.perf_counter() - started
    print()
    print(f"演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
