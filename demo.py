#!/usr/bin/env python3
"""固定输入的离线演示：真实运行 borrow_check。

四部分（全部现场计算，无 sleep、无预录结果）：

1. 正常结果：互不相交字段的独立借用，经分支/循环后干净通过；
2. 实际拒绝：循环中未结束的借用，打印带前驱传播链的冲突来源；
3. 异常边界：可定位的非法输入错误，以及“预算耗尽 != 证明无冲突”；
4. 操作片段调用：同字段双可变借用拒绝（调用位置 + 片段内路径）、不同字段
   独立通过、某分支移动后的写回、局部 loan 退出即结束/重复调用身份独立，
   并把一个含环主 CFG **真正展开**成普通 CFG，与摘要检查逐项比较冲突。

运行：python demo.py（约毫秒级，远小于 90 秒）。
"""

from __future__ import annotations

import time

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    cfg_from_dict,
    check,
    check_program,
    program_from_dict,
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
    # 4) 操作片段调用（本次新增）。
    #    片段 swap_borrow(p, q)：对 p、q 各开一个可变借用，用完各自结束。
    #    片段 maybe_move(p)：无环菱形，一个分支 move p 后返回。
    # ------------------------------------------------------------------
    hr("4) 操作片段调用：摘要前置条件 + 实参替换（别名/独立/写回）")
    seg_swap = {
        "params": ["p", "q"],
        "blocks": [{"ops": [
            {"op": "borrow_mut", "loan_id": "Lp", "path": ["p"]},
            {"op": "borrow_mut", "loan_id": "Lq", "path": ["q"]},
            {"op": "end_loan", "loan_id": "Lp"},
            {"op": "end_loan", "loan_id": "Lq"},
        ]}],
        "successors": [[]], "entry": 0,
    }
    seg_move = {
        "params": ["p"],
        "blocks": [
            {"ops": []},
            {"ops": [{"op": "move", "path": ["p"]}]},
            {"ops": []},
        ],
        "successors": [[1, 2], [2], []], "entry": 0,
    }
    segments = {"swap_borrow": seg_swap, "maybe_move": seg_move}

    def prog_with_call(call_ops):
        return {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r",
                 "ty": {"a": None, "b": None}},
                *call_ops,
            ]}], "successors": [[]], "entry": 0},
            "segments": segments,
        }

    # 4a) 同字段双可变借用：p=q=r.a（不默认形参互不重叠）。
    same = program_from_dict(prog_with_call([
        {"op": "call", "segment": "swap_borrow",
         "args": [["p", ["r", "a"]], ["q", ["r", "a"]]]}]))
    r_same = check_program(same)
    print(f"4a 同字段 p=q=r.a：clean={r_same.clean}，冲突 {len(r_same.conflicts)} 个")
    print(r_same.explain())
    assert not r_same.clean
    c0 = r_same.conflicts[0]
    assert (c0.block, c0.op, c0.seg_block, c0.seg_op) == (0, 1, 0, 1)

    # 4b) 父子别名 p=r、q=r.a：对称重叠 -> 冲突。
    parent_child = program_from_dict(prog_with_call([
        {"op": "call", "segment": "swap_borrow",
         "args": [["p", ["r"]], ["q", ["r", "a"]]]}]))
    r_pc = check_program(parent_child)
    print(f"\n4b 父子别名 p=r,q=r.a：clean={r_pc.clean}（冲突 {len(r_pc.conflicts)} 个）")
    assert not r_pc.clean

    # 4c) 不同字段 p=r.a、q=r.b：互不相交 -> 干净。
    distinct = program_from_dict(prog_with_call([
        {"op": "call", "segment": "swap_borrow",
         "args": [["p", ["r", "a"]], ["q", ["r", "b"]]]}]))
    r_dist = check_program(distinct)
    print(f"4c 不同字段 p=r.a,q=r.b：clean={r_dist.clean}")
    assert r_dist.clean

    # 4d) 某分支移动后返回：调用后读 r.a may-moved，兄弟 r.b 可用。
    moved = program_from_dict(prog_with_call([
        {"op": "call", "segment": "maybe_move", "args": [["p", ["r", "a"]]]},
        {"op": "read", "path": ["r", "a"]},
        {"op": "read", "path": ["r", "b"]},
    ]))
    r_mv = check_program(moved)
    print(f"4d 分支移动后返回：clean={r_mv.clean}，冲突 {len(r_mv.conflicts)} 个")
    print(r_mv.conflicts[0].detail, "| 见证片段坐标:",
          (r_mv.conflicts[0].witnesses[0].seg_block,
           r_mv.conflicts[0].witnesses[0].seg_op,
           r_mv.conflicts[0].witnesses[0].formal_path))
    assert {(c.kind, c.block, c.op) for c in r_mv.conflicts} == \
        {("use_of_moved", 0, 2)}

    # 4e) 局部 loan 退出即结束 + 重复调用身份独立。
    seg_hold = {
        "params": ["p"],
        "blocks": [{"ops": [
            {"op": "borrow_mut", "loan_id": "H", "path": ["p"]},
            {"op": "read", "path": ["p"]}]}],
        "successors": [[]], "entry": 0,
    }
    rep = program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "r", "ty": {"a": None}},
            {"op": "call", "segment": "hold", "args": [["p", ["r", "a"]]]},
            {"op": "call", "segment": "hold", "args": [["p", ["r", "a"]]]},
            {"op": "write", "path": ["r", "a"]},  # 局部 loan 已退出：合法
        ]}], "successors": [[]], "entry": 0},
        "segments": {"hold": seg_hold}})
    r_rep = check_program(rep)
    # 每次调用恰好 1 个片段内 read_while_mut；无跨调用 borrow_conflict。
    assert {(c.kind, c.block, c.op) for c in r_rep.conflicts} == \
        {("read_while_mut_borrowed", 0, 1), ("read_while_mut_borrowed", 0, 2)}
    print("4e 两次调用各 1 个片段内冲突，调用后写合法：局部 loan 不串用、不泄漏")

    # 4f) 真正展开一个含环主 CFG，与摘要检查逐项比较。
    from tests._inline import inline_program, normalize_conflict_key
    cyclic = {
        "cfg": {
            "blocks": [
                {"ops": [{"op": "declare", "var": "r",
                          "ty": {"a": None, "b": None}}]},
                {"ops": [
                    {"op": "call", "segment": "hold",
                     "args": [["p", ["r", "a"]]]},
                    {"op": "read", "path": ["r", "b"]},
                ]},
            ],
            # 块0 -> 块1，块1 自环：摘要调用接入主 CFG 不动点。
            "successors": [[1], [1]], "entry": 0,
        },
        "segments": {"hold": seg_hold},
    }
    inlined, origins = inline_program(cyclic)
    r_inline = check(cfg_from_dict(inlined))
    r_summary = check_program(program_from_dict(cyclic))
    inline_keys = set()
    for c in r_inline.conflicts:
        _, mb, mo, sb, so = normalize_conflict_key(c.block, c.op, origins)
        inline_keys.add((c.kind, mb, mo, sb, so))
    summary_keys = {(c.kind, c.block, c.op, c.seg_block, c.seg_op)
                    for c in r_summary.conflicts}
    print(f"4f 含环主 CFG：展开为 {len(inlined['blocks'])} 个普通块，"
          f"展开冲突 {len(inline_keys)} 个，摘要冲突 {len(summary_keys)} 个，"
          f"逐项一致={inline_keys == summary_keys}")
    assert inline_keys == summary_keys

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
