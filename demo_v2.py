#!/usr/bin/env python3
"""v2 固定输入离线演示：真实运行 borrow_check 的条件感知借用检查。

五部分（全部现场计算，无 sleep、无预录结果）：

1. 正常结果（条件相关性）：互斥分支分别移动不同字段，第二次按同一条件
   分流读取 -> clean（may 并集下会出现的伪冲突被路径条件消除）；
2. 正常结果（两阶段借用）：预约期间共享读/共享重借用合法，共享借用
   结束后激活，再正常使用；
3. 实际拒绝：预约后异常退出 + 部分移动 + 逆声明序词法析构，打印
   经独立具体解释器逐步重放认证的冲突证据（可执行输入 + 块游走）；
4. 棘手边界：同一程序在不同认证预算下 conflict vs unknown（4 轮回边
   预算），并展示抽象伪候选被完备具体状态搜索证伪（不计冲突、不 unknown）；
5. 三态分离：不动点预算耗尽抛 BudgetExhaustedError，与 clean/unknown
   严格区分。

运行：python demo_v2.py（毫秒级，远小于 90 秒）。
"""

from __future__ import annotations

import time

from borrow_check import BudgetExhaustedError, cfg_from_dict, check


def render_conflict(c) -> str:
    e = c.evidence
    lines = [f"冲突[{c.kind}] @ blocks[{c.block}].ops[{c.op}] ({c.op_name})："
             f"{c.detail}"]
    lines.append("    认证输入: " +
                 ", ".join(f"{n}={int(v)}" for n, v in e.assignment))
    lines.append("    可执行游走: " + " -> ".join(f"blocks[{b}]" for b in e.walk)
                 + f"（回边 {e.back_edges} 条）")
    lines.append("    逐步重放:")
    for ev in e.events:
        lines.append(f"      · blocks[{ev.block}].ops[{ev.op}] {ev.text}")
    return "\n".join(lines)


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def correlated_clean():
    """互斥别名：同一条件的两次分流使读取与移动在每条执行上互不重叠。"""
    return cfg_from_dict({
        "version": 2,
        "conditions": ["c"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None, "y": None}, "init": True}]},
            {"ops": [{"op": "move", "path": ["a", "x"]}]},          # c=1
            {"ops": [{"op": "move", "path": ["a", "y"]}]},          # c=0
            {"ops": [{"op": "read", "path": ["a", "y"]}]},          # c=1 读 y
            {"ops": [{"op": "read", "path": ["a", "x"]}]},          # c=0 读 x
        ],
        "successors": [
            [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
            [{"block": 3, "when": "c"}, {"block": 4, "when": ["not", "c"]}],
            [{"block": 3, "when": "c"}, {"block": 4, "when": ["not", "c"]}],
            [], []],
    })


def unguarded_bad():
    """合流后无保护地读两个字段：两条确定冲突，各带相反的具体输入。"""
    return cfg_from_dict({
        "version": 2,
        "conditions": ["c"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None, "y": None}, "init": True}]},
            {"ops": [{"op": "move", "path": ["a", "x"]}]},
            {"ops": [{"op": "move", "path": ["a", "y"]}]},
            {"ops": [{"op": "read", "path": ["a", "x"]},
                     {"op": "read", "path": ["a", "y"]}]},
        ],
        "successors": [
            [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
            [3], [3], []],
    })


def two_phase_clean():
    return cfg_from_dict({
        "version": 2,
        "conditions": [],
        "blocks": [{"ops": [
            {"op": "declare", "var": "v", "ty": None, "init": True},
            {"op": "borrow_mut", "loan_id": "L", "path": ["v"],
             "two_phase": True},
            {"op": "borrow_shared", "loan_id": "S", "path": ["v"]},
            {"op": "read", "path": ["v"]},
            {"op": "end_loan", "loan_id": "S"},
            {"op": "activate", "loan_id": "L"},
            {"op": "end_loan", "loan_id": "L"},
            {"op": "read", "path": ["v"]},
        ]}],
        "successors": [[]],
    })


def end_to_end():
    """三组能力组合：部分移动 + 逆序析构 + 条件 + 两阶段预约/激活。

    异常路径（d=1）：B 预约 a.x 后直接到 D；D 先部分移动 a.y，再
    end_scope——a.y 已移动被跳过，仍初始化的 a.x 与预约 L 冲突。
    """
    return cfg_from_dict({
        "version": 2,
        "conditions": ["c", "d"],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None, "y": None}, "init": True}]},
            {"ops": [
                {"op": "borrow_mut", "loan_id": "L",
                 "path": ["a", "x"], "two_phase": True},
                {"op": "read", "path": ["a", "x"]},
            ]},
            {"ops": [
                {"op": "activate", "loan_id": "L"},
                {"op": "move", "path": ["a", "x"]},
                {"op": "end_loan", "loan_id": "L"},
            ]},
            {"ops": [
                {"op": "move", "path": ["a", "y"]},
                {"op": "end_scope", "scope": "0"},
            ]},
        ],
        "successors": [
            [1],
            [{"block": 3, "when": "d"}, {"block": 2, "when": ["not", "d"]}],
            [{"block": 1, "when": "c"}, {"block": 3, "when": ["not", "c"]}],
            []],
    })


def loop_reservation():
    """回边让预约传播到下一轮的写：冲突需要 1 条回边才能重放。"""
    return cfg_from_dict({
        "version": 2,
        "conditions": ["c", "d"],
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None},
                 "init": True},
                {"op": "write", "path": ["a", "x"]},
            ]},
            {"ops": [
                {"op": "borrow_mut", "loan_id": "L",
                 "path": ["a", "x"], "two_phase": True},
                {"op": "read", "path": ["a", "x"]},
            ]},
            {"ops": []},
        ],
        "successors": [
            [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
            [{"block": 0, "when": "d"}, {"block": 2, "when": ["not", "d"]}],
            []],
    })


def fixpoint_budget_cfg():
    return cfg_from_dict({
        "version": 2,
        "conditions": [],
        "blocks": [
            {"ops": [
                {"op": "declare", "var": "v", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["v"]},
            ]},
            {"ops": [{"op": "end_loan", "loan_id": "L"}]},
        ],
        "successors": [[1], [0]],
    })


def main() -> None:
    started = time.perf_counter()

    # 1) 正常结果：条件相关性消除互斥分支的伪冲突。
    hr("1) 正常结果：互斥别名 + 条件相关（status=clean）")
    r = check(correlated_clean())
    print(f"结论 status={r.status!r}，确定冲突 {len(r.conflicts)} 个，"
          f"被完备具体搜索证伪的抽象候选 {r.refuted_candidates} 个")
    assert r.status == "clean"

    # 对照：合流后无保护读取 -> 两个确定冲突，输入恰好相反。
    rb = check(unguarded_bad())
    print("对照（无保护合流）：")
    for c in rb.conflicts:
        a = ", ".join(f"{n}={int(v)}" for n, v in c.evidence.assignment)
        print(f"  {c.kind} @ blocks[{c.block}].ops[{c.op}]，认证输入 ({a})，"
              f"游走 {'->'.join(map(str, c.evidence.walk))}")
    assert rb.status == "conflict" and len(rb.conflicts) == 2

    # 2) 正常结果：两阶段预约期共享读。
    hr("2) 正常结果：两阶段可变借用（预约期共享读，激活后独占）")
    r2 = check(two_phase_clean())
    print(f"结论 status={r2.status!r}，确定冲突 {len(r2.conflicts)} 个")
    assert r2.status == "clean"

    # 3) 实际拒绝：三组能力组合 + 逐步重放证据。
    hr("3) 实际拒绝：预约后异常退出 + 部分移动 + 逆序词法析构")
    r3 = check(end_to_end())
    print(f"结论 status={r3.status!r}，确定冲突 {len(r3.conflicts)} 个：")
    for c in sorted(r3.conflicts, key=lambda x: (x.block, x.op, x.kind)):
        print(f"  - {c.kind} @ blocks[{c.block}].ops[{c.op}]")
    print("\n其中异常退出路径（d=1）的析构冲突，逐步重放证据：\n")
    target = next(c for c in r3.conflicts
                  if (c.kind, c.block) == ("drop_while_reserved", 3))
    print(render_conflict(target))
    assert target.evidence.assignment == (("c", False), ("d", True))
    assert target.evidence.walk == (0, 1, 3)
    assert any(c.kind == "move_while_borrowed" and c.block == 2
               for c in r3.conflicts)

    # 4) 棘手边界：回边预算 conflict vs unknown；伪候选被证伪。
    hr("4) 棘手边界：认证预算（4 轮回边）与 unknown，伪候选必须被证伪")
    loop_cfg = loop_reservation()
    full = check(loop_cfg)
    print(f"默认预算（4 轮）：status={full.status!r}，"
          f"确定冲突 {len(full.conflicts)} 个，unknown {len(full.unknown_reasons)} 个")
    wc = next(c for c in full.conflicts
              if (c.block, c.op) == (0, 1))
    print(f"  回边冲突 {wc.kind}：认证输入 c=1,d=1，"
          f"游走 {wc.evidence.walk}，回边 {wc.evidence.back_edges} 条")
    tight = check(loop_cfg, max_loop_unrolls=0)
    print(f"压缩到 0 轮回边：status={tight.status!r} "
          f"（{len(tight.unknown_reasons)} 个候选无法在 0 轮内证实——"
          f"不报 clean，也不硬报 conflict）")
    for u in tight.unknown_reasons:
        print(f"  - {u.kind} @ blocks[{u.block}].ops[{u.op}]：{u.detail}")
    assert full.status == "conflict" and tight.status == "unknown"

    # 伪候选证伪：may 不动点可能过粗，但完备具体状态搜索会把伪候选证伪
    # （既不报冲突也不 unknown）。用固定种子的测试生成器实时找一例。
    import random
    from tests.test_v2_exhaustive import generate_cfg
    rng = random.Random(20260924)
    example = None
    for _ in range(200):
        data = generate_cfg(rng)
        cand = check(cfg_from_dict(data))
        if cand.refuted_candidates:
            example = cand
            break
    assert example is not None
    print(f"  另：固定种子生成器的首个含伪候选 CFG 上，{example.refuted_candidates} "
          f"个抽象候选被完备具体搜索证伪，status={example.status!r}，"
          f"确定冲突仍报 {len(example.conflicts)} 个（证伪不影响真冲突）")

    # 5) 三态分离：不动点预算耗尽是异常，不是 clean/unknown。
    hr("5) 异常边界：不动点预算耗尽（BudgetExhaustedError）")
    try:
        check(fixpoint_budget_cfg(), max_iterations=1)
    except BudgetExhaustedError as exc:
        print(f"捕获 {type(exc).__name__}：{exc}")
    else:  # pragma: no cover
        raise AssertionError("预算必须被耗尽")
    full5 = check(fixpoint_budget_cfg())
    print(f"完整预算：status={full5.status!r}（与预算耗尽严格分开）")

    elapsed = time.perf_counter() - started
    print(f"\n演示完成，实际耗时 {elapsed * 1000:.1f} ms"
          f"（无 sleep、无预录结果，全部现场计算与重放）。")


if __name__ == "__main__":
    main()
