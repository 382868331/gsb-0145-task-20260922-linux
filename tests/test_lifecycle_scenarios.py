"""v2 生命周期分析的验收场景（手写期望值）。

覆盖：移动后析构、互斥别名/互斥分支不产生伪冲突、预约期共享读取、
预约后异常退出（drop）、两阶段借用与循环回边、部分移动 + reinit 拆分，
以及一个同时用到三组新能力的端到端例子。每个确定冲突都检查可执行证据：
条件赋值、块游走、逐步重放、现场复现标记。
"""

import unittest

from borrow_check import cfg_from_dict, check


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


def lc(data, **kw):
    return check(cfg_from_dict(data, max_blocks=64), lifecycle=True, **kw)


class TestTwoPhaseBorrow(unittest.TestCase):
    def test_reserve_allows_shared_read(self):
        r = lc({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},
                {"op": "borrow_shared", "loan_id": "S", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},
                {"op": "end_loan", "loan_id": "S"},
                {"op": "borrow_activate", "loan_id": "R"},
                {"op": "end_loan", "loan_id": "R"},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(r.status, "ok", r.explain_evidence())

    def test_reserve_blocks_write_and_move(self):
        r = lc({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                {"op": "write", "path": ["a", "x"]},
                {"op": "move", "path": ["a", "x"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(r),
                         {("write_while_borrowed", 0, 2),
                          ("move_while_borrowed", 0, 3)})

    def test_read_after_activation_conflicts(self):
        r = lc({
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                    {"op": "borrow_activate", "loan_id": "R"},
                    {"op": "read", "path": ["a", "x"]},
                ]},
            ],
            "successors": [[1], []],
        })
        self.assertEqual(keys(r), {("read_while_mut_borrowed", 1, 2)})
        ev = r.evidence[0]
        self.assertTrue(ev.reproduced)
        self.assertEqual(ev.walk, (0, 1))
        self.assertEqual(ev.origin, (1, 0))

    def test_two_reserves_coexist_activation_is_exclusive(self):
        r = lc({
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [
                    {"op": "borrow_reserve", "loan_id": "R1", "path": ["a", "x"]},
                    {"op": "borrow_reserve", "loan_id": "R2", "path": ["a", "x"]},
                    {"op": "borrow_activate", "loan_id": "R1"},
                ]},
            ],
            "successors": [[1], []],
        })
        # 预约之间不冲突；激活 R1 时 R2 仍预约 -> 独占冲突。
        self.assertEqual(keys(r), {("borrow_conflict", 1, 2)})


class TestConditionsAndBranches(unittest.TestCase):
    def test_mutex_branches_no_phantom_on_disjoint_fields(self):
        # p 分支移动 a.x；!p 分支移动 a.y；汇合后分别读两个字段：
        # 每条读只在一个分支下冲突 -> 都是确定冲突（各有可执行输入），
        # 而"同一次执行两个字段都被移动"的伪冲突不存在。
        r = lc({
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
                [{"block": 1, "when": "p"}, {"block": 2, "when": {"not": "p"}}],
                [3], [3], []],
        })
        self.assertEqual(keys(r),
                         {("use_of_moved", 3, 0),
                          ("use_of_moved", 3, 1)})
        by_op = {c.op: c for c in r.conflicts}
        # 读 a.x 的可执行输入必须是 p=1（走移动分支）；读 a.y 必须 p=0。
        ev30 = next(e for e in r.evidence if e.op == 0)
        ev31 = next(e for e in r.evidence if e.op == 1)
        self.assertEqual(ev30.assignment, {"p": True})
        self.assertEqual(ev31.assignment, {"p": False})
        self.assertIn(1, ev30.walk)
        self.assertIn(2, ev31.walk)
        for ev in (ev30, ev31):
            self.assertTrue(ev.reproduced)

    def test_mutex_aliases_both_ended_is_clean(self):
        # 两个互斥分支各自借同一字段并结束；汇合读取不得报伪冲突。
        r = lc({
            "conditions": ["p"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [
                    {"op": "borrow_mut", "loan_id": "L1", "path": ["a", "x"]},
                    {"op": "end_loan", "loan_id": "L1"}]},
                {"ops": [
                    {"op": "borrow_mut", "loan_id": "L2", "path": ["a", "x"]},
                    {"op": "end_loan", "loan_id": "L2"}]},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [
                [{"block": 1, "when": "p"}, {"block": 2, "when": {"not": "p"}}],
                [3], [3], []],
        })
        self.assertEqual(r.status, "ok", r.explain_evidence())

    def test_compound_guards_share_condition(self):
        # 守卫 (p & q) 与 !(p & q) 互斥；p&q 分支移动，汇合读 -> 确定。
        r = lc({
            "conditions": ["p", "q"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": []},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [
                [{"block": 1, "when": {"and": ["p", "q"]}},
                 {"block": 2, "when": {"not": {"and": ["p", "q"]}}}],
                [3], [3], []],
        })
        self.assertEqual(keys(r), {("use_of_moved", 3, 0)})
        ev = r.evidence[0]
        self.assertEqual(ev.assignment, {"p": True, "q": True})


class TestDrop(unittest.TestCase):
    def test_partial_move_then_drop_clean(self):
        # a.x 已部分移动；词法析构只访问仍初始化的 a.y，不报错。
        r = lc({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "drop"},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(r.status, "ok")

    def test_move_then_drop_borrowed_sibling_conflicts(self):
        # a.x 被移走；a.y 仍被可变借用：析构 a.y 时冲突，a.x 跳过。
        r = lc({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "borrow_mut", "loan_id": "M", "path": ["a", "y"]},
                {"op": "drop"},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(r), {("drop_while_borrowed", 0, 3)})
        self.assertEqual(r.evidence[0].origin, (0, 2))

    def test_drop_reverse_declaration_order(self):
        # 声明顺序 a 再 b；drop 先析构 b 再 a。b 的借用冲突先报。
        r = lc({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None},
                {"op": "declare", "var": "b", "ty": None},
                {"op": "borrow_mut", "loan_id": "LB", "path": ["b"]},
                {"op": "borrow_mut", "loan_id": "LA", "path": ["a"]},
                {"op": "drop"},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(r), {("drop_while_borrowed", 0, 4)})
        # 同一 drop 点给一条冲突，证据的诞生点取最早的活跃借用即可，
        # 但两个 loan 都必须出现在见证中（逆序析构两个变量都撞上）。
        loans = {w.loan_id for w in r.conflicts[0].witnesses}
        self.assertEqual(loans, {"LA", "LB"})

    def test_reserve_then_abnormal_exit_drop(self):
        # 异常退出边（!ok）直接到 drop 块：预约未激活也要在析构时冲突。
        r = lc({
            "conditions": ["ok"],
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
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
        self.assertEqual(keys(r), {("drop_while_borrowed", 2, 0)})
        ev = r.evidence[0]
        self.assertEqual(ev.assignment, {"ok": False})
        self.assertEqual(ev.walk, (0, 2))
        self.assertTrue(ev.reproduced)


class TestLoopWithTwoPhase(unittest.TestCase):
    def test_closed_reserve_activate_end_loop_is_clean(self):
        r = lc({
            "conditions": ["c"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}}]},
                {"ops": [
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
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
        self.assertEqual(r.status, "ok")

    def test_activated_not_ended_conflicts_on_back_edge(self):
        # 激活后不结束就回边：第二轮 reserve 撞第一轮遗留 mut；
        # 第二轮 activate 也撞上一轮 mut。路径条件 c 参与不动点。
        r = lc({
            "conditions": ["c"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                    {"op": "borrow_activate", "loan_id": "R"},
                ]},
                {"ops": []},
            ],
            "successors": [[1],
                           [{"block": 1, "when": "c"},
                            {"block": 2, "when": {"not": "c"}}],
                           []],
        })
        self.assertEqual(keys(r),
                         {("borrow_conflict", 1, 0),
                          ("borrow_conflict", 1, 1)})
        ev = next(e for e in r.evidence if e.op == 0)
        self.assertEqual(ev.assignment, {"c": True})
        # 证据游走必须经过回边（块1 出现两次）。
        self.assertEqual(ev.walk, (0, 1, 1))
        self.assertTrue(all(e.reproduced for e in r.evidence))


class TestTripleComboEndToEnd(unittest.TestCase):
    def test_partial_move_drop_reserve_conditions_together(self):
        # 同时用到：部分移动+析构 / 命题分支 / 两阶段预约。
        # 块0：declare a{x,y}，预约 R on a.x；
        # p=1 正常路径：读 a.x（预约允许），部分移动 a.y，激活 R（独占），
        #   结束后 drop（a.y 已移走，只析构 a.x，无借用）-> 干净；
        # p=0 异常路径：直接 drop，a.x 仍初始化且 R 在预约期 -> 确定冲突。
        r = lc({
            "conditions": ["p"],
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a",
                     "ty": {"x": None, "y": None}},
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                ]},
                {"ops": [
                    {"op": "read", "path": ["a", "x"]},
                    {"op": "move", "path": ["a", "y"]},
                    {"op": "borrow_activate", "loan_id": "R"},
                    {"op": "end_loan", "loan_id": "R"},
                    {"op": "drop"},
                ]},
                {"ops": [{"op": "drop"}]},
            ],
            "successors": [
                [{"block": 1, "when": "p"}, {"block": 2, "when": {"not": "p"}}],
                [], []],
        })
        self.assertEqual(keys(r), {("drop_while_borrowed", 2, 0)})
        ev = r.evidence[0]
        self.assertEqual(ev.assignment, {"p": False})
        # 逐步重放：declare -> reserve -> 异常分支 drop，现场复现。
        ops_seen = [(s.block, s.op_name) for s in ev.steps]
        self.assertEqual(ops_seen,
                         [(0, "declare"), (0, "borrow_reserve"),
                          (2, "drop")])
        self.assertTrue(ev.reproduced)


class TestUnknownAndDeterminism(unittest.TestCase):
    def test_tight_search_budget_reports_unknown_not_clean(self):
        # 压缩证据搜索预算：存在抽象冲突但预算内找不到可执行游走时，
        # 必须如实 unknown，而不是当作干净通过。
        data = {
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a", "x"]},
                    {"op": "borrow_activate", "loan_id": "R"},
                ]},
                {"ops": []},
            ],
            "conditions": ["c"],
            "successors": [[1],
                           [{"block": 1, "when": "c"},
                            {"block": 2, "when": {"not": "c"}}],
                           []],
        }
        r = lc(data, search_budget=1)
        self.assertFalse(r.clean)
        self.assertIn("unverified_conflict", r.unknown_reasons[0])
        # 默认预算下同一输入给确定答案。
        r2 = lc(data)
        self.assertEqual(r2.status, "conflict")

    def test_deterministic_repeated_results(self):
        data = {
            "conditions": ["p", "q"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": []},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [
                [{"block": 1, "when": {"and": ["p", "q"]}},
                 {"block": 2, "when": {"not": {"and": ["p", "q"]}}}],
                [3], [3], []],
        }
        r1 = lc(data)
        r2 = lc(data)
        self.assertEqual([(c.kind, c.block, c.op) for c in r1.conflicts],
                         [(c.kind, c.block, c.op) for c in r2.conflicts])
        self.assertEqual(r1.evidence[0].assignment, r2.evidence[0].assignment)
        self.assertEqual(r1.evidence[0].walk, r2.evidence[0].walk)


if __name__ == "__main__":
    unittest.main()
