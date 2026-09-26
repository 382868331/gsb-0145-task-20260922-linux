#!/usr/bin/env python3
"""预约式借用（reserve/activate）的离线演示：真实运行 borrow_check。

三部分（全部现场计算，无 sleep、无预录结果）：

1. 合法：先预约修改对象、再读取它的长度作为参数，跨分支汇合后激活；
2. 实际拒绝：激活前保留共享借用（activate_conflict），以及只在
   一条控制路径上预约（activate_not_live）；
3. 循环与预算：带循环的预约程序在默认预算内收敛；显式压缩预算
   演示 BudgetExhaustedError 与「证明无冲突」是两条分开的路径
   （含预约时迭代上界未被证明，见 README「已知限制」）。

运行：python demo_iteration.py（毫秒级，远小于 90 秒）。
"""

from __future__ import annotations

import time

from borrow_check import (
    BudgetExhaustedError,
    cfg_from_dict,
    check,
)

TY = {"data": None, "len": None}


def hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    started = time.perf_counter()

    # ------------------------------------------------------------------
    # 1) 合法：先准备修改对象（reserve），再读取它的长度作为参数，
    #    两条分支都保持预约存活，汇合后 activate 升级为独占。
    # ------------------------------------------------------------------
    hr("1) 合法：读取参数后激活（check 证明 clean）")
    ok = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "buf", "ty": TY},
                # 预约：准备稍后修改 buf.data，此刻仍是共享占用。
                {"op": "reserve", "reserve_id": "R1", "path": ["buf", "data"]},
                # 预约阶段允许共享读取：读取长度作为后续参数。
                {"op": "read", "path": ["buf", "len"]},
            ]},
            # 分支 A：再读一次被预约的路径本身，合法（共享占用）。
            {"ops": [{"op": "read", "path": ["buf", "data"]}]},
            # 分支 B：什么都不做。
            {"ops": []},
            # 汇合：两条路径都预约成功且未结束 -> 允许激活。
            {"ops": [
                {"op": "activate", "reserve_id": "R1"},
                {"op": "end_reserve", "reserve_id": "R1"},
                # 独占已释放，普通写恢复合法。
                {"op": "write", "path": ["buf", "data"]},
            ]},
        ],
        "successors": [[1, 2], [3], [3], []],
        "entry": 0,
    })
    result = check(ok)
    print(f"块数=4，不动点块处理次数={result.iterations}")
    print("结论：clean =", result.clean)
    assert result.clean, result.explain()

    # ------------------------------------------------------------------
    # 2a) 实际拒绝：激活前保留了共享借用，升级被拒绝。
    # ------------------------------------------------------------------
    hr("2a) 实际拒绝：激活前保留共享借用（activate_conflict）")
    kept = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "buf", "ty": TY},
                {"op": "reserve", "reserve_id": "R1", "path": ["buf", "data"]},
                {"op": "read", "path": ["buf", "len"]},
                # 共享借用与预约共存（合法），但一直没有 end_loan……
                {"op": "borrow_shared", "loan_id": "L1", "path": ["buf", "data"]},
                # ……激活要求独占，保留的共享借用导致拒绝。
                {"op": "activate", "reserve_id": "R1"},
            ]},
        ],
        "successors": [[]],
        "entry": 0,
    })
    rejected = check(kept)
    print("结论：clean =", rejected.clean)
    print(rejected.explain())
    assert not rejected.clean
    assert rejected.conflicts[0].kind == "activate_conflict"

    # ------------------------------------------------------------------
    # 2b) 实际拒绝：只在一条控制路径上预约，汇合点不能放行。
    # ------------------------------------------------------------------
    hr("2b) 实际拒绝：单路径预约（activate_not_live，must 语义）")
    one_sided = cfg_from_dict({
        "blocks": [
            {"ops": [{"op": "declare", "var": "buf", "ty": TY}]},
            # 只有分支 A 预约。
            {"ops": [
                {"op": "reserve", "reserve_id": "R1", "path": ["buf", "data"]},
            ]},
            # 分支 B 没有预约。
            {"ops": []},
            # 汇合：并非所有到达路径都预约成功 -> 拒绝，不能放行。
            {"ops": [{"op": "activate", "reserve_id": "R1"}]},
        ],
        "successors": [[1, 2], [3], [3], []],
        "entry": 0,
    })
    rejected2 = check(one_sided)
    print("结论：clean =", rejected2.clean)
    print(rejected2.explain())
    assert not rejected2.clean
    assert rejected2.conflicts[0].kind == "activate_not_live"

    # ------------------------------------------------------------------
    # 3) 循环与预算：预约跨越循环仍收敛；压缩预算演示「未证明」路径。
    # ------------------------------------------------------------------
    hr("3) 循环：默认预算内收敛；预算耗尽 != 证明无冲突")
    looping = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "buf", "ty": TY},
                {"op": "reserve", "reserve_id": "R1", "path": ["buf", "data"]},
            ]},
            # 自循环：预约存活期间反复共享读取，合法。
            {"ops": [
                {"op": "read", "path": ["buf", "len"]},
                {"op": "read", "path": ["buf", "data"]},
            ]},
            {"ops": [
                {"op": "activate", "reserve_id": "R1"},
                {"op": "end_reserve", "reserve_id": "R1"},
            ]},
        ],
        "successors": [[1], [1, 2], []],
        "entry": 0,
    })
    full = check(looping)
    print(f"完整预算：clean={full.clean}（证明无冲突），迭代 {full.iterations} 次")
    assert full.clean, full.explain()
    try:
        check(looping, max_iterations=1)
    except BudgetExhaustedError as exc:
        print(f"压缩预算后捕获 {type(exc).__name__}：{exc}")
    else:  # pragma: no cover
        raise AssertionError("预算必须被耗尽")
    print("说明：含预约时迭代次数上界未被证明（must 分量不在原证明内），")
    print("      预算耗尽只表示「未证明」，不表示有冲突或无冲突。")

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
