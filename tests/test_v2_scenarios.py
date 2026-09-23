"""v2 手写验收场景：

* 局部变量初始化 / 未初始化使用 / init 恢复；
* 部分移动 + 逆声明序词法析构（移动后析构只访问仍初始化字段）；
* 布尔条件限定：互斥分支不产生伪冲突，无保护合流给确定冲突与具体赋值；
* 两阶段可变借用：预约期共享读、激活独占、预约与析构/回边联合分析；
* 预算耗尽 -> unknown（绝不与 clean 混淆）；
* 一个同时用到三组能力 + 条件参与循环不动点/激活的端到端用例。
"""

import unittest

from borrow_check import cfg_from_dict, check, CFGValidationError, Decl


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


def walk_is_executable(cfg, evidence):
    """核对证据游走：起点为入口、边存在且在认证赋值下守卫为真。"""
    index = {name: i for i, name in enumerate(cfg.conditions)}
    size = 1 << len(cfg.conditions)
    walk = evidence.walk
    assert walk[0] == cfg.entry
    for u, v in zip(walk, walk[1:]):
        assert v in cfg.successors[u], f"边 {u}->{v} 不存在"
        formula = cfg.guards.get((u, v))
        if formula is None:
            continue
        mask = formula.compile(index)
        assert (mask >> evidence.assignment_bits) & 1, (
            f"边 {u}->{v} 的守卫在认证赋值下为假")


class TestInitAndUninitialized(unittest.TestCase):
    def test_uninitialized_use_then_init(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": None, "y": None}, "init": False},
                {"op": "read", "path": ["a", "x"]},      # uninit
                {"op": "init", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},      # ok
                {"op": "read", "path": ["a", "y"]},      # 仍 uninit
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(r.status, "conflict")
        self.assertEqual(keys(r), {
            ("use_of_uninitialized", 0, 1),
            ("use_of_uninitialized", 0, 4),
        })

    def test_declare_default_v2_is_uninitialized(self):
        # v2 下 declare 不带 init 即默认未初始化（缺省选择固定）。
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None},
                {"op": "read", "path": ["a"]},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {("use_of_uninitialized", 0, 1)})

    def test_v1_declare_stays_initialized(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None},
                {"op": "read", "path": ["a"]},
            ]}], "successors": [[]],
        })
        self.assertTrue(check(cfg).clean)


class TestLexicalDrop(unittest.TestCase):
    def test_partial_move_then_drop_skips_moved_field(self):
        # 移动 a.x 后词法析构：只析构 a.y，跳过已移动的 a.x，无冲突。
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": None, "y": None}, "init": True},
                {"op": "move", "path": ["a", "x"]},
                {"op": "end_scope", "scope": "0"},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(r.status, "clean",
                         [(c.kind, c.detail) for c in r.conflicts])

    def test_drop_order_is_reverse_declaration(self):
        # 先声明 x 后声明 y；预约 x。逆序析构先 y（干净）后 x（冲突），
        # 冲突证据必须定位到先声明的 x。
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "x", "ty": None, "init": True},
                {"op": "declare", "var": "y", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["x"],
                 "two_phase": True},
                {"op": "end_scope", "scope": "0"},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {("drop_while_reserved", 0, 3)})
        c = r.conflicts[0]
        self.assertIn("x", c.detail)
        # 逐步事件里 y 的析构必须早于 x 的冲突事件。
        texts = [e.text for e in c.evidence.events]
        iy = next(i for i, t in enumerate(texts) if "变量 y" in t)
        ix = next(i for i, t in enumerate(texts) if "变量 x" in t)
        self.assertLess(iy, ix)

    def test_explicit_drop_requires_whole_place(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": None, "y": None}, "init": True},
                {"op": "move", "path": ["a", "x"]},
                {"op": "drop", "path": ["a"]},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {("use_of_moved", 0, 2)})


class TestConditionCorrelation(unittest.TestCase):
    def _diamond(self, merge_reads):
        return cfg_from_dict({
            "version": 2, "conditions": ["c"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}, "init": True}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": [{"op": "move", "path": ["a", "y"]}]},
                {"ops": merge_reads},
            ],
            "successors": [
                [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
                [3], [3], []],
        })

    def test_correlated_merge_is_clean(self):
        # 第二次分流与第一次用同一条件：各执行只读自己仍初始化的字段。
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}, "init": True}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": [{"op": "move", "path": ["a", "y"]}]},
                {"ops": [{"op": "read", "path": ["a", "y"]}]},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [
                [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
                [{"block": 3, "when": "c"}, {"block": 4, "when": ["not", "c"]}],
                [{"block": 3, "when": "c"}, {"block": 4, "when": ["not", "c"]}],
                [], []],
        })
        r = check(cfg)
        self.assertEqual(r.status, "clean",
                         [(c.kind, c.detail) for c in r.conflicts])

    def test_unguarded_merge_conflicts_with_concrete_inputs(self):
        cfg = self._diamond([
            {"op": "read", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "y"]},
        ])
        r = check(cfg)
        self.assertEqual(r.status, "conflict")
        self.assertEqual(keys(r), {
            ("use_of_moved", 3, 0), ("use_of_moved", 3, 1)})
        by_op = {c.op: c for c in r.conflicts}
        # 读 a.x 只在 c=1 的执行冲突；读 a.y 只在 c=0。
        self.assertEqual(by_op[0].evidence.assignment, (("c", True),))
        self.assertEqual(by_op[1].evidence.assignment, (("c", False),))
        for c in r.conflicts:
            walk_is_executable(cfg, c.evidence)

    def test_unsat_guard_edge_is_dead(self):
        # 恒假守卫后的移动永不可达；读必须干净。
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": None, "init": True}]},
                {"ops": [{"op": "move", "path": ["a"]}]},
                {"ops": [{"op": "read", "path": ["a"]}]},
            ],
            "successors": [
                [{"block": 1, "when": ["const", False]},
                 {"block": 2, "when": ["const", True]}],
                [2], []],
        })
        r = check(cfg)
        self.assertEqual(r.status, "clean",
                         [(c.kind, c.detail) for c in r.conflicts])


class TestTwoPhase(unittest.TestCase):
    def test_reservation_allows_shared_read_and_reborrow(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["v"],
                 "two_phase": True},
                {"op": "borrow_shared", "loan_id": "S", "path": ["v"]},
                {"op": "read", "path": ["v"]},
                {"op": "end_loan", "loan_id": "S"},
                {"op": "activate", "loan_id": "L"},
                {"op": "read", "path": ["v"]},       # 激活后独占：冲突
                {"op": "end_loan", "loan_id": "L"},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {("read_while_mut_borrowed", 0, 6)})

    def test_activate_requires_shared_loans_gone(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["v"],
                 "two_phase": True},
                {"op": "borrow_shared", "loan_id": "S", "path": ["v"]},
                {"op": "activate", "loan_id": "L"},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {("activate_conflict", 0, 3)})

    def test_reservation_blocks_write_move_second_mut(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["v"],
                 "two_phase": True},
                {"op": "write", "path": ["v"]},
                {"op": "borrow_mut", "loan_id": "M", "path": ["v"]},
            ]}], "successors": [[]],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {
            ("write_while_reserved", 0, 2),
            ("borrow_while_reserved", 0, 3),
        })

    def test_reservation_across_loop_back_edge(self):
        # 头块 H 先写 a.x（第一轮无预约，干净）；体块 B 预约 L；
        # 回边 B->H 后：第二轮 H 的写与预约冲突（write_while_reserved），
        # 且重入 B 时再预约同一 loan 与既有预约冲突（reservation_conflict）。
        # 两个冲突都必须靠回边不动点 + 条件 (c=1,d=1) 才能还原。
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c", "d"],
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a",
                     "ty": {"x": None}, "init": True},
                    {"op": "write", "path": ["a", "x"]},
                ]},
                {"ops": [
                    {"op": "borrow_mut", "loan_id": "L",
                     "path": ["a", "x"], "two_phase": True},
                    {"op": "read", "path": ["a", "x"]},
                ]},
                {"ops": []},
            ],
            # H->B 当 c；B->H 当 d（回边），B->exit 当 not d。
            "successors": [
                [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
                [{"block": 0, "when": "d"}, {"block": 2, "when": ["not", "d"]}],
                []],
        })
        r = check(cfg)
        self.assertEqual(keys(r), {
            ("write_while_reserved", 0, 1),
            ("reservation_conflict", 1, 0),
        })
        by = {(c.block, c.op): c for c in r.conflicts}
        wc = by[(0, 1)]
        self.assertEqual(wc.evidence.assignment, (("c", True), ("d", True)))
        self.assertEqual(wc.evidence.walk, (0, 1, 0))
        self.assertEqual(wc.evidence.back_edges, 1)
        walk_is_executable(cfg, wc.evidence)
        rc = by[(1, 0)]
        self.assertEqual(rc.evidence.back_edges, 1)
        self.assertEqual(rc.evidence.assignment, (("c", True), ("d", True)))
        walk_is_executable(cfg, rc.evidence)
        # 预约期共享读本身从不冲突。
        self.assertNotIn(("read_while_mut_borrowed", 1, 1), keys(r))


class TestUnknownBudget(unittest.TestCase):
    def test_deep_conflict_becomes_unknown_not_clean(self):
        # 同上回边场景；把回边预算压到 0：冲突需要 1 条回边才能重放，
        # 预算内无法证实 -> unknown，而不是 clean，也不是硬报 conflict。
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c", "d"],
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a",
                     "ty": {"x": None}, "init": True},
                    {"op": "write", "path": ["a", "x"]},
                ]},
                {"ops": [{"op": "borrow_mut", "loan_id": "L",
                          "path": ["a", "x"], "two_phase": True}]},
                {"ops": []},
            ],
            "successors": [
                [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
                [{"block": 0, "when": "d"}, {"block": 2, "when": ["not", "d"]}],
                []],
        })
        r = check(cfg, max_loop_unrolls=0)
        self.assertEqual(r.status, "unknown")
        self.assertEqual(r.conflicts, [])
        self.assertEqual({(u.kind, u.block, u.op) for u in r.unknown_reasons},
                         {("write_while_reserved", 0, 1),
                          ("reservation_conflict", 1, 0)})
        # 完备搜索精确判定：冲突为真但需要 >0 轮回边（4 轮预算下 0 轮不可达）。
        self.assertTrue(all(u.reason in
                            ("beyond_loop_budget", "budget_exhausted")
                            for u in r.unknown_reasons))
        self.assertTrue(all(u.reason == "beyond_loop_budget"
                            for u in r.unknown_reasons))
        self.assertTrue(all("至少需要" in u.detail for u in r.unknown_reasons))

    def test_deterministic_repeat(self):
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c"],
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
        r1 = check(cfg)
        r2 = check(cfg)
        self.assertEqual(
            [(c.kind, c.block, c.op, c.evidence.assignment_bits,
              c.evidence.walk) for c in r1.conflicts],
            [(c.kind, c.block, c.op, c.evidence.assignment_bits,
              c.evidence.walk) for c in r2.conflicts])

    def test_cert_state_budget_exhaustion_is_unknown(self):
        # 白盒：直链上停止点从不触发目标 kind；完备阶段的可达具体状态数
        # 超过 max_search_states 时必须返回 budget_exhausted（调用方据此
        # 判 unknown），而不是 refuted/clean。
        from borrow_check.replay import certify, _LoanMeta, all_paths_of
        cfg = cfg_from_dict({
            "version": 2, "conditions": [],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}, "init": True}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": [{"op": "read", "path": ["a", "y"]}]},
                {"ops": [{"op": "read", "path": ["a", "y"]}]},
            ],
            "successors": [[1], [2], [3], []],
        })
        types, loans, scope_vars = {}, {}, {}
        for bi, block in enumerate(cfg.blocks):
            for op in block.ops:
                if isinstance(op, Decl):
                    types[op.var] = op.ty
                    scope_vars.setdefault(op.scope, []).append((op.var, op.ty))
        tree_paths = all_paths_of(types)
        edges = frozenset((u, v) for u, vs in enumerate(cfg.successors)
                          for v in vs)
        # 停止点 blocks[3].ops[0] 的 read 永远不会 use_of_moved（a.y 活着）。
        out = certify(cfg, types, loans, scope_vars, tree_paths, edges, 0,
                      (3, 0), "use_of_moved",
                      max_loop_unrolls=4, max_search_states=2)
        self.assertIsNone(out.certified)
        self.assertEqual(out.reason, "budget_exhausted")
        # 给足预算后同样的候选被证伪（具体状态搜索完整）。
        full = certify(cfg, types, loans, scope_vars, tree_paths, edges, 0,
                       (3, 0), "use_of_moved",
                       max_loop_unrolls=4, max_search_states=1000)
        self.assertIsNone(full.certified)
        self.assertEqual(full.reason, "refuted")


class TestEndToEndCombined(unittest.TestCase):
    def test_all_three_capabilities_with_loop_fixpoint(self):
        """端到端：部分移动 + 逆序析构 + 条件相关 + 两阶段预约/激活，
        且路径条件参与循环不动点与激活判定。

        程序（条件 c=循环继续, d=异常退出）::

            H(0): declare a{x,y} 已初始化
            B(1): 预约 L on a.x；共享读 a.x（预约期允许）；
                  if d 异常退出到 D(3)，否则到 A(2)
            A(2): end_loan S（若有）；激活 L；激活后 move a.x 冲突；
                  end_loan L；回 H 当 c
            D(3): 部分移动 a.y 后 end_scope：x 仍被预约？——预约在 B 产生后
                  经 B->D 到达；析构只访问仍初始化字段：a.y 已移动被跳过，
                  a.x 仍初始化且与预约 L 重叠 -> drop_while_reserved。

        断言：
        * c=1 路径上循环第二轮激活后 move 报 move_while_borrowed（激活独占）；
        * d=1 异常路径上报 drop_while_reserved（部分移动 + 逆序析构 + 预约）；
        * 两个冲突各带可执行输入；其余读干净（无伪冲突）。
        """
        cfg = cfg_from_dict({
            "version": 2, "conditions": ["c", "d"],
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
        r = check(cfg)
        self.assertEqual(r.status, "conflict",
                         [(u.kind, u.reason) for u in r.unknown_reasons])
        found = {(c.kind, c.block, c.op): c for c in r.conflicts}
        # 异常路径：预约后退出，析构仍初始化的 a.x 与预约冲突；a.y 已移动被跳过。
        self.assertIn(("drop_while_reserved", 3, 1), found)
        dc = found[("drop_while_reserved", 3, 1)]
        self.assertEqual(dc.evidence.assignment,
                         (("c", False), ("d", True)))
        self.assertEqual(dc.evidence.walk, (0, 1, 3))
        walk_is_executable(cfg, dc.evidence)
        # 激活后移动与独占借用冲突（首次进入即可触发；回环后同样触发，
        # 由 test_reservation_across_loop_back_edge 单独核对回边不动点）。
        self.assertIn(("move_while_borrowed", 2, 1), found)
        mc = found[("move_while_borrowed", 2, 1)]
        walk_is_executable(cfg, mc.evidence)
        # B 块预约期的共享读在任何迭代都干净（无伪冲突）。
        self.assertNotIn(("read_while_mut_borrowed", 1, 1), found)
        # 逐步重放事件必须真实包含“预约”“逆序析构”“跳过”语义。
        texts = [e.text for e in dc.evidence.events]
        self.assertTrue(any("预约" in t for t in texts))
        self.assertTrue(any("a.y" in t and "跳过" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
