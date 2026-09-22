#!/usr/bin/env python3
"""固定输入的离线演示：真实运行 borrow_check。

三部分（全部现场计算，无 sleep、无预录结果）：

1. 正常结果：互不相交字段的独立借用，经分支/循环后干净通过；
2. 实际拒绝：循环中未结束的借用，打印带前驱传播链的冲突来源；
3. 异常边界：可定位的非法输入错误，以及“预算耗尽 != 证明无冲突”。

运行：python demo.py（约毫秒级，远小于 8 秒）。
"""

from __future__ import annotations

import time

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    cfg_from_dict,
    check,
)


def hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    started = time.perf_counter()

    # ------------------------------------------------------------------
    # 1) 正常结果：不同字段独立借用（含分支 + 循环）。
    #    记录树 user = { profile: {name, email}, token }
    # ------------------------------------------------------------------
    hr("1) 正常结果：互不相交字段独立借用（check 证明 clean）")
    good = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "user", "ty": {
                    "profile": {"name": None, "email": None},
                    "token": None,
                }},
            ]},
            # 分支 A：可变借用 profile.name，用完即结束。
            {"ops": [
                {"op": "borrow_mut", "loan_id": "L_name",
                 "path": ["user", "profile", "name"]},
                {"op": "end_loan", "loan_id": "L_name"},
            ]},
            # 分支 B：与 A 互不相交，可变借用 token 后立即结束。
            {"ops": [
                {"op": "borrow_mut", "loan_id": "L_tok", "path": ["user", "token"]},
                {"op": "end_loan", "loan_id": "L_tok"},
            ]},
            # B 后进入自循环块：无活跃借用，反复读写 token 与 name（不动点干净）。
            {"ops": [
                {"op": "write", "path": ["user", "token"]},
                {"op": "read", "path": ["user", "profile", "name"]},
            ]},
            # 汇合块：读取与前序字段互不相交的 email，合法。
            {"ops": [
                {"op": "read", "path": ["user", "profile", "email"]},
            ]},
        ],
        # 边：0 -> 1/2（两分支），1 -> 4（A 汇合），2 -> 3（B 进自循环），
        # 3 -> 3/4（循环若干次后汇合），4 -> []。
        "successors": [[1, 2], [4], [3], [3, 4], []],
        "entry": 0,
    })
    result = check(good)
    print(f"块数=5，不动点块处理次数={result.iterations}")
    print("结论：clean =", result.clean)
    assert result.clean, result.explain()

    # ------------------------------------------------------------------
    # 2) 实际拒绝：借用在循环中从未结束，回边后与读取冲突。
    #    演示“冲突来源链”：诞生点 + 穿过回边的传播链。
    # ------------------------------------------------------------------
    hr("2) 实际拒绝：循环中未结束的借用（静态拒绝，附前驱见证链）")
    # 块1 内“先读 a.sum、再可变借用 a.sum”，且块1 自循环、从不 end_loan。
    # 首次进入块1 时读取合法；但经回边再次进入时，上一轮的借用可能仍活跃
    # —— 该冲突只有 may 不动点（并集 + 迭代）才能发现，单次直线遍历会漏报。
    bad = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "acc", "ty": {"sum": None, "cache": None}},
            ]},
            {"ops": [
                {"op": "read", "path": ["acc", "sum"]},
                {"op": "borrow_mut", "loan_id": "L_sum", "path": ["acc", "sum"]},
                {"op": "read", "path": ["acc", "cache"]},  # 不相交字段：始终无冲突
            ]},
        ],
        "successors": [[1], [1]],  # 块1 自循环；L_sum 从未 end_loan
        "entry": 0,
    })
    rejected = check(bad)
    print("结论：clean =", rejected.clean)
    print(f"冲突数 = {len(rejected.conflicts)}，来源链如下：\n")
    print(rejected.explain())
    assert not rejected.clean
    w = rejected.conflicts[0].witnesses[0]
    assert w.loan_id == "L_sum" and w.origin_block == 1 and w.origin_op == 1
    # 借用点(ops[1])在读取点(ops[0])之后，同块顺序下不可能直接触发，
    # 因此该见证必然经由 1->1 回边成立。
    assert w.origin_block == rejected.conflicts[0].block and w.origin_op > 0
    print("\n说明：借用诞生于 blocks[1].ops[1]，晚于同块读取 ops[0]；")
    print("      它只能沿 blocks[1] -> blocks[1] 回边传播，在第二轮进入时触发。")

    # ------------------------------------------------------------------
    # 3) 异常边界。
    # ------------------------------------------------------------------
    hr("3a) 异常边界：非法输入给出可定位错误（不静默纠正）")
    invalid = {
        "blocks": [{"ops": [
            {"op": "declare", "var": "a", "ty": {"x": None}},
            {"op": "read", "path": ["a", "y"]},  # 记录树中没有字段 y
        ]}],
        "successors": [[]],
    }
    try:
        check(cfg_from_dict(invalid))
    except CFGValidationError as exc:
        print(f"捕获 {type(exc).__name__}：{exc}")
        print(f"定位 location = {exc.location!r}")
    else:  # pragma: no cover
        raise AssertionError("非法输入必须被拒绝")

    hr("3b) 异常边界：搜索预算耗尽 与 证明无冲突 严格分开")
    looping = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "v", "ty": None},
                {"op": "borrow_mut", "loan_id": "L", "path": ["v"]},
            ]},
            {"ops": [{"op": "end_loan", "loan_id": "L"}]},
        ],
        "successors": [[1], [0]],
    })
    full = check(looping)
    print(f"完整预算：clean={full.clean}（这是“证明无冲突”路径），"
          f"迭代 {full.iterations} 次")
    try:
        check(looping, max_iterations=1)  # 故意只给 1 次块处理预算
    except BudgetExhaustedError as exc:
        print(f"压缩预算后捕获 {type(exc).__name__}：{exc}")
    else:  # pragma: no cover
        raise AssertionError("预算必须被耗尽")

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
