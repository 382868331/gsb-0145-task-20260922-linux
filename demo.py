#!/usr/bin/env python3
"""固定输入的离线演示：真实运行 borrow_check。

v1 部分（lifecycle=False，默认）：
1. 正常结果：互不相交字段的独立借用，经分支/循环后干净通过；
2. 实际拒绝：循环中未结束的借用，打印带前驱传播链的冲突来源；
3. 异常边界：可定位的非法输入错误，以及“预算耗尽 != 证明无冲突”。

v2 部分（lifecycle=True，显式选项）：
4. 新增正常结果：两阶段可变借用（预约期共享读）+ 循环闭合干净；
5. 棘手边界：同一条件的互斥分支不产生伪冲突，确定冲突给出可执行输入
   （具体条件赋值 + 块游走）并逐步重放；
6. 实际拒绝：预约后异常退出分支触发词法析构冲突，附可执行证据；
7. 如实 unknown：压缩证据搜索预算后，抽象候选无法在预算内复现，
   返回 unknown 而不是通过。

运行：python demo.py（毫秒级，远小于 90 秒；无 sleep、无预录结果）。
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

    # ------------------------------------------------------------------
    # v2 生命周期分析（lifecycle=True）
    # ------------------------------------------------------------------
    hr("4) v2 正常结果：两阶段借用（预约期共享读）+ 循环闭合干净")
    lc_good = cfg_from_dict({
        "conditions": ["c"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None, "y": None}}]},
            {"ops": [
                {"op": "borrow_reserve", "loan_id": "R",
                 "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},   # 预约期允许共享读
                {"op": "read", "path": ["a", "y"]},
                {"op": "borrow_activate", "loan_id": "R"},
                {"op": "end_loan", "loan_id": "R"},
            ]},
            {"ops": []},
        ],
        "successors": [[1],
                       [{"block": 1, "when": "c"},
                        {"block": 2, "when": {"not": "c"}}],
                       []],
    })
    lr = check(lc_good, lifecycle=True)
    print(f"status={lr.status}（不动点块处理 {lr.iterations} 次）")
    assert lr.status == "ok", lr.explain_evidence()

    hr("5) v2 棘手边界：互斥分支不产生伪冲突；确定冲突给可执行输入")
    # p 分支移动 a.x；!p 分支移动 a.y；汇合后分别读。两条读各只在一个
    # 条件赋值下冲突，必须分别给出可执行输入（不是并集伪冲突）。
    lc_branch = cfg_from_dict({
        "conditions": ["p"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None, "y": None}}]},
            {"ops": [{"op": "move", "path": ["a", "x"]}]},
            {"ops": [{"op": "move", "path": ["a", "y"]}]},
            {"ops": [
                {"op": "read", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "y"]},
            ]},
        ],
        "successors": [
            [{"block": 1, "when": "p"},
             {"block": 2, "when": {"not": "p"}}],
            [3], [3], []],
    })
    br = check(lc_branch, lifecycle=True)
    print(f"status={br.status}，确定冲突 {len(br.conflicts)} 条；"
          f"unknown 原因 {len(br.unknown_reasons)} 条")
    for ev in br.evidence:
        assign = ", ".join(f"{k}={int(v)}"
                           for k, v in sorted(ev.assignment.items()))
        walk = " -> ".join(f"blocks[{b}]" for b in ev.walk)
        print(f"  {ev.kind} @ blocks[{ev.block}].ops[{ev.op}]："
              f"可执行输入 {{{assign}}}；游走 {walk}；"
              f"现场复现={ev.reproduced}")
    assert {(c.kind, c.block, c.op) for c in br.conflicts} == {
        ("use_of_moved", 3, 0), ("use_of_moved", 3, 1)}
    assert {e.assignment["p"] for e in br.evidence} == {True, False}

    hr("6) v2 实际拒绝：预约后异常退出，词法析构撞上预约借用")
    # 块0 declare + reserve a.x；ok 路径激活并结束后 drop（干净）；
    # !ok 异常退出直接 drop：a.x 仍初始化且预约未激活 -> 确定冲突。
    lc_drop = cfg_from_dict({
        "conditions": ["ok"],
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "borrow_reserve", "loan_id": "R",
                 "path": ["a", "x"]},
            ]},
            {"ops": [
                {"op": "borrow_activate", "loan_id": "R"},
                {"op": "end_loan", "loan_id": "R"},
                {"op": "drop"},
            ]},
            {"ops": [{"op": "drop"}]},
        ],
        "successors": [
            [{"block": 1, "when": "ok"},
             {"block": 2, "when": {"not": "ok"}}],
            [], []],
    })
    dr = check(lc_drop, lifecycle=True)
    print(f"status={dr.status}")
    print(dr.explain_evidence())
    assert dr.status == "conflict"
    assert dr.evidence[0].assignment == {"ok": False}
    assert dr.evidence[0].walk == (0, 2)

    hr("7) v2 如实 unknown：证据搜索预算耗尽不等于通过")
    # 与 6 同一形状的循环程序：把搜索预算压到极小，候选无法在预算内复现
    # 时必须返回 unknown，而不是 clean 通行证。
    lc_loop = cfg_from_dict({
        "conditions": ["c"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
            {"ops": [
                {"op": "borrow_reserve", "loan_id": "R",
                 "path": ["a", "x"]},
                {"op": "borrow_activate", "loan_id": "R"},
            ]},
            {"ops": []},
        ],
        "successors": [[1],
                       [{"block": 1, "when": "c"},
                        {"block": 2, "when": {"not": "c"}}],
                       []],
    })
    tight = check(lc_loop, lifecycle=True, search_budget=1)
    full = check(lc_loop, lifecycle=True)
    print(f"压缩预算：status={tight.status} clean={tight.clean}；"
          f"原因：{tight.unknown_reasons[0][:80]}...")
    print(f"完整预算：status={full.status}，确定冲突 "
          f"{len(full.conflicts)} 条（同一输入给确定答案）")
    assert tight.status == "unknown" and not tight.clean
    assert full.status == "conflict"

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
