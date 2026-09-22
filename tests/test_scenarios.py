"""验收场景：互不相交字段、循环中未结束借用、单分支移动后读取、父对象重组。"""

import unittest

from borrow_check import (
    BudgetExhaustedError,
    CFG,
    Block,
    BorrowMut,
    BorrowShared,
    Decl,
    EndLoan,
    Move,
    Read,
    Write,
    cfg_from_dict,
    check,
)


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


class TestDisjointFields(unittest.TestCase):
    def test_distinct_fields_borrow_independently(self):
        # a.x 与 a.y 可同时可变借用；借用结束后读写均合法。
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "borrow_mut", "loan_id": "Lx", "path": ["a", "x"]},
                {"op": "borrow_mut", "loan_id": "Ly", "path": ["a", "y"]},
                {"op": "end_loan", "loan_id": "Lx"},
                {"op": "end_loan", "loan_id": "Ly"},
                {"op": "read", "path": ["a", "x"]},
                {"op": "write", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertTrue(result.clean, result.explain())

    def test_nested_paths_overlap(self):
        # a.x 与 a.x.z 重叠；a 与任意子路径重叠。
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"z": None}, "y": None}},
                {"op": "borrow_mut", "loan_id": "L1", "path": ["a", "x"]},
                {"op": "borrow_mut", "loan_id": "L2", "path": ["a", "x", "z"]},
                {"op": "borrow_shared", "loan_id": "L3", "path": ["a", "y"]},
                {"op": "borrow_shared", "loan_id": "L4", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("borrow_conflict", 0, 2)})

    def test_shared_then_mut_conflicts_but_shared_shared_ok(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None},
                {"op": "borrow_shared", "loan_id": "S1", "path": ["v"]},
                {"op": "borrow_shared", "loan_id": "S2", "path": ["v"]},
                {"op": "read", "path": ["v"]},
                {"op": "borrow_mut", "loan_id": "M1", "path": ["v"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("borrow_conflict", 0, 4)})

    def test_ended_loan_does_not_conflict(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": {"x": None}},
                {"op": "borrow_mut", "loan_id": "L1", "path": ["v", "x"]},
                {"op": "end_loan", "loan_id": "L1"},
                {"op": "borrow_mut", "loan_id": "L2", "path": ["v", "x"]},
                {"op": "end_loan", "loan_id": "L2"},
                {"op": "write", "path": ["v", "x"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertTrue(result.clean, result.explain())


class TestLoopUnendedLoan(unittest.TestCase):
    def test_loan_born_before_loop_active_at_loop_back_edge(self):
        # 块0 借用后进入自循环块1；不动点必须发现 L 在回边后仍可能活跃。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "borrow_mut", "loan_id": "L1", "path": ["a", "x"]},
                ]},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [[1], [1]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("read_while_mut_borrowed", 1, 0)})
        witness = result.conflicts[0].witnesses[0]
        self.assertEqual(witness.loan_id, "L1")
        # 见证链必须穿过回边：诞生块 0 -> 块1。
        self.assertEqual(witness.block_chain[0], 0)
        self.assertEqual(witness.block_chain[-1], 1)

    def test_loan_ended_inside_loop_is_clean(self):
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "borrow_mut", "loan_id": "L1", "path": ["a", "x"]},
                ]},
                {"ops": [{"op": "end_loan", "loan_id": "L1"}]},
            ],
            "successors": [[1], [0]],
        })
        result = check(cfg)
        self.assertTrue(result.clean, result.explain())

    def test_move_inside_loop_persists_via_may_union(self):
        # 块1 移动 a.x；回边后块1 入口 a.x 已可能移动：
        # read 与 move 自身都报 use_of_moved（may 语义）。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None, "y": None}}]},
                {"ops": [
                    {"op": "read", "path": ["a", "x"]},
                    {"op": "move", "path": ["a", "x"]},
                ]},
            ],
            "successors": [[1], [1]],
        })
        result = check(cfg)
        self.assertEqual(keys(result),
                         {("use_of_moved", 1, 0), ("use_of_moved", 1, 1)})
        witness = next(c for c in result.conflicts if c.op == 0).witnesses[0]
        self.assertEqual(witness.origin_block, 1)
        self.assertEqual(witness.path, ("a", "x"))


class TestBranchMoveThenRead(unittest.TestCase):
    def _cfg(self):
        return cfg_from_dict({
            "blocks": [
                {"ops": [{"op": "declare", "var": "a",
                          "ty": {"x": None, "y": None}}]},
                {"ops": [{"op": "move", "path": ["a"]}]},
                {"ops": []},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [[1, 2], [3], [3], []],
        })

    def test_may_union_flags_read_after_merge(self):
        result = check(self._cfg())
        self.assertEqual(keys(result), {("use_of_moved", 3, 0)})
        witness = result.conflicts[0].witnesses[0]
        self.assertEqual(witness.origin_block, 1)
        self.assertEqual(witness.path, ("a",))
        self.assertEqual(witness.block_chain, (1, 3))

    def test_witness_distinguishes_taken_branch(self):
        # 不经过移动分支的同形 CFG 必须干净。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [[1], []],
        })
        self.assertTrue(check(cfg).clean)


class TestParentReconstruction(unittest.TestCase):
    def test_reinit_restores_only_named_field(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a", "x"], "reinit": True},
                {"op": "read", "path": ["a", "x"]},   # 恢复：合法
                {"op": "read", "path": ["a", "y"]},   # 仍可能已移动
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("use_of_moved", 0, 4)})

    def test_reinit_nested_field_splits_ancestor_move(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"u": None, "v": None}, "y": {"z": None}}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a", "x"], "reinit": True},
                {"op": "read", "path": ["a", "x", "u"]},  # 恢复子树内
                {"op": "read", "path": ["a", "x", "v"]},  # 恢复子树内
                {"op": "read", "path": ["a", "y", "z"]},  # 兄弟仍不可用
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("use_of_moved", 0, 5)})

    def test_move_parent_kills_all_children(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"u": None}, "y": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x", "u"]},
                {"op": "read", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("use_of_moved", 0, 2)})

    def test_ordinary_write_does_not_restore(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "write", "path": ["a", "x"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("use_of_moved", 0, 2)})


class TestNativeDataclassAPI(unittest.TestCase):
    def test_build_cfg_with_dataclasses(self):
        cfg = CFG(
            blocks=[Block(ops=[
                Decl("p", {"q": None}),
                BorrowShared("S", ("p", "q")),
                Read(("p", "q")),
                EndLoan("S"),
            ])],
            successors=[[]],
        )
        self.assertTrue(check(cfg).clean)


if __name__ == "__main__":
    unittest.main()
