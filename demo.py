#!/usr/bin/env python3
"""固定输入的离线演示：真实运行 borrow_check。

五部分（全部现场计算，无 sleep、无预录结果）：

1. 正常结果：互不相交字段的独立借用，经分支/循环后干净通过；
2. 实际拒绝：循环中未结束的借用，打印带前驱传播链的冲突来源；
3. 异常边界：可定位的非法输入错误，以及“预算耗尽 != 证明无冲突”；
4. 片段调用（本次新增）：片段摘要 + 不同字段独立访问 clean，
   同字段双可变借用 / 父子别名的拒绝（见证同时给调用位置与片段内路径）；
5. 真正展开对照：同一逻辑手工展开成普通 CFG，与片段版逐项比较。

运行：python demo.py（毫秒级，远小于 8 秒；片段演示 < 90 秒预算）。
"""

from __future__ import annotations

import time

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    build_summaries,
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
    # 4) 片段调用（本次新增）：先形成摘要，再按实参替换接入不动点。
    # ------------------------------------------------------------------
    hr("4) 片段调用：摘要 + 实参替换（不假设形参互不重叠）")
    # 片段 Touch(p, q)：各开一个可变借用，比较两个字段是否冲突。
    touch = {
        "name": "Touch",
        "params": [
            {"name": "p", "ty": None},
            {"name": "q", "ty": None},
        ],
        "blocks": [{"ops": [
            {"op": "borrow_mut", "loan_id": "lp", "path": ["p"]},
            {"op": "borrow_mut", "loan_id": "lq", "path": ["q"]},
            {"op": "end_loan", "loan_id": "lp"},
            {"op": "end_loan", "loan_id": "lq"},
        ]}],
        "successors": [[]],
    }

    # 4a) 先打印形参命名空间摘要（前置条件 + 状态变化）。
    summary_program = program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
            {"op": "invoke", "fragment": "Touch",
             "args": {"p": ["r", "x"], "q": ["r", "y"]}},
        ]}], "successors": [[]]},
        "fragments": [touch],
    })
    summary = build_summaries(summary_program)["Touch"]
    print(summary.describe())

    # 4b) 不同字段独立访问：p=r.x, q=r.y -> clean。
    disjoint = program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
            {"op": "invoke", "fragment": "Touch",
             "args": {"p": ["r", "x"], "q": ["r", "y"]}},
        ]}], "successors": [[]]},
        "fragments": [touch],
    })
    res_disjoint = check_program(disjoint)
    print(f"\n不同字段实参 (p=r.x, q=r.y)：clean = {res_disjoint.clean}")
    assert res_disjoint.clean, res_disjoint.explain()

    # 4c) 同字段双可变借用：p=q=r.x -> 片段内冲突。
    same = program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
            {"op": "invoke", "fragment": "Touch",
             "args": {"p": ["r", "x"], "q": ["r", "x"]}},
        ]}], "successors": [[]]},
        "fragments": [touch],
    })
    res_same = check_program(same)
    print(f"同字段实参 (p=q=r.x)：clean = {res_same.clean}")
    print(res_same.explain())
    assert not res_same.clean

    # 4d) 父子路径别名：p=r（父），q=r.x（子），片段借 p.x 与 q。
    alias = {
        "name": "Alias",
        "params": [
            {"name": "p", "ty": {"x": None}},
            {"name": "q", "ty": None},
        ],
        "blocks": [{"ops": [
            {"op": "borrow_mut", "loan_id": "lp", "path": ["p", "x"]},
            {"op": "borrow_mut", "loan_id": "lq", "path": ["q"]},
            {"op": "end_loan", "loan_id": "lp"},
            {"op": "end_loan", "loan_id": "lq"},
        ]}],
        "successors": [[]],
    }
    aliased = program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "r", "ty": {"x": None}},
            {"op": "invoke", "fragment": "Alias",
             "args": {"p": ["r"], "q": ["r", "x"]}},
        ]}], "successors": [[]]},
        "fragments": [alias],
    })
    res_alias = check_program(aliased)
    print(f"\n父子别名 (p=r, q=r.x)：clean = {res_alias.clean}（重叠冲突）")
    assert not res_alias.clean

    # ------------------------------------------------------------------
    # 5) 真正展开对照：同一段逻辑手工展开为普通 CFG，逐项比较。
    #    片段 MoveY(p)：某分支移动 p.y 后返回；调用后 r.y may 已移动。
    # ------------------------------------------------------------------
    hr("5) 真正展开对照：片段版 vs 手工展开普通 CFG")
    movey_frag = {
        "name": "MoveY",
        "params": [{"name": "p", "ty": {"x": None, "y": None}}],
        "blocks": [{"ops": [{"op": "move", "path": ["p", "y"]}]}],
        "successors": [[]],
    }
    fragment_program = {
        "cfg": {"blocks": [
            {"ops": [
                {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
                {"op": "invoke", "fragment": "MoveY", "args": {"p": ["r"]}},
            ]},
            {"ops": [{"op": "read", "path": ["r", "y"]}]},
        ], "successors": [[1], []]},
        "fragments": [movey_frag],
    }
    expanded_cfg = cfg_from_dict({
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["r", "y"]},  # 片段体按实参 p=r 替换
            ]},
            {"ops": [{"op": "read", "path": ["r", "y"]}]},
        ],
        "successors": [[1], []],
    })
    res_frag = check_program(program_from_dict(fragment_program))
    res_exp = check(expanded_cfg)
    frag_kp = sorted((c.kind, c.path) for c in res_frag.conflicts)
    exp_kp = sorted((c.kind, c.path) for c in res_exp.conflicts)
    print(f"片段版冲突 (kind,path) = {frag_kp}")
    print(f"展开版冲突 (kind,path) = {exp_kp}")
    assert frag_kp == exp_kp, "片段版与真正展开必须逐项一致"
    print("两者逐项一致：片段内移动 p.y 按实参替换为 r.y，移动效果返回调用方；")
    print("片段版见证锚定调用位置并附片段内路径，展开版锚定内联位置。")

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms（无 sleep、无预录结果）。")

    # ------------------------------------------------------------------
    # 6) 有界规模的现场计算演示（确定性生成，远小于接口上限与 90 秒预算）。
    #    规模说清：50 个主块（接口上限）、每块 1 个 invoke；3 层嵌套片段
    #    （每片段 2 个前向块、每块 2 条借用相关指令）；模板内联后约
    #    50 * 8 = 400 个模板块。不是上限压力测试（上限 100 000 模板操作），
    #    只是一个现场跑完含调用不动点的中等程序。
    # ------------------------------------------------------------------
    hr("6) 有界规模现场计算（确定性生成，非上限压测）")
    scale_started = time.perf_counter()
    leaf = {"x": None, "y": None}
    depth_frags = []
    prev_name = None
    for d in range(3):
        name = f"Lvl{d}"
        inner_call = ([{"op": "invoke", "fragment": prev_name,
                        "args": {"p": ["p"]}}] if prev_name else [])
        depth_frags.append({
            "name": name,
            "params": [{"name": "p", "ty": leaf}],
            "blocks": [
                {"ops": inner_call + [
                    {"op": "borrow_mut", "loan_id": f"m{d}", "path": ["p", "x"]},
                    {"op": "read", "path": ["p", "y"]},
                    {"op": "end_loan", "loan_id": f"m{d}"},
                ]},
                {"ops": [{"op": "read", "path": ["p", "y"]}]},
            ],
            "successors": [[1], []],
        })
        prev_name = name
    top_name = prev_name
    main_blocks = [
        {"ops": [{"op": "declare", "var": "r", "ty": leaf}]}
    ]
    for _ in range(49):
        main_blocks.append({"ops": [
            {"op": "invoke", "fragment": top_name, "args": {"p": ["r"]}},
        ]})
    # 链状主 CFG（0->1->...->49）。
    big = program_from_dict({
        "cfg": {"blocks": main_blocks,
                "successors": [[i + 1] for i in range(49)] + [[]]},
        "fragments": depth_frags,
    })
    big_res = check_program(big)
    scale_ms = (time.perf_counter() - scale_started) * 1000
    print(f"规模：50 主块 × 每块 1 个 invoke；3 层嵌套片段；clean="
          f"{big_res.clean}，不动点块处理 {big_res.iterations} 次")
    print(f"现场计算耗时 {scale_ms:.1f} ms（远小于 90 秒预算；非 100000 "
          f"模板操作上限压测）。")
    assert scale_ms < 90_000


if __name__ == "__main__":
    main()
