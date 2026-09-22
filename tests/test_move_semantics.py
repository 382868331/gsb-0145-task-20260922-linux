"""方向性移动语义、reinit 拆分的边界与跨分支并集。"""

import unittest

from borrow_check import cfg_from_dict, check


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


class TestDirectionalMove(unittest.TestCase):
    def test_move_child_does_not_disable_parent_or_sibling(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": None, "y": {"z": None}}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "read", "path": ["a"]},        # 父路径仍可用
                {"op": "read", "path": ["a", "y"]},    # 兄弟字段可用
                {"op": "read", "path": ["a", "y", "z"]},
                {"op": "read", "path": ["a", "x"]},    # 被移动字段不可用
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(check(cfg)), {("use_of_moved", 0, 5)})

    def test_move_parent_disables_descendants_at_all_depths(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"u": {"w": None}}, "y": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x", "u", "w"]},
                {"op": "read", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(check(cfg)), {("use_of_moved", 0, 2)})

    def test_move_child_then_borrow_child_and_parent(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a", "x"]},
                {"op": "borrow_shared", "loan_id": "P", "path": ["a"]},
                {"op": "read", "path": ["a", "y"]},
                {"op": "borrow_mut", "loan_id": "Q", "path": ["a", "x"]},
            ]}],
            "successors": [[]],
        })
        # 借父路径合法（父未移动）；借已移动子路径报 use_of_moved，
        # 同时 mut a.x 与仍活跃的 shared a 对称重叠，构成借用冲突。
        self.assertEqual(keys(check(cfg)),
                         {("use_of_moved", 0, 4),
                          ("borrow_conflict", 0, 4)})


class TestReinitSplit(unittest.TestCase):
    def test_reinit_sole_field_keeps_root_moved(self):
        # a 只有字段 x：reinit a.x 恢复 x 子树，但根对象本身仍可能已移动。
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a", "x"], "reinit": True},
                {"op": "read", "path": ["a", "x"]},
                {"op": "read", "path": ["a"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(check(cfg)), {("use_of_moved", 0, 4)})

    def test_reinit_root_restores_whole_subtree(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"u": None}, "y": None}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a"], "reinit": True},
                {"op": "read", "path": ["a", "x", "u"]},
                {"op": "read", "path": ["a", "y"]},
                {"op": "read", "path": ["a"]},
            ]}],
            "successors": [[]],
        })
        self.assertTrue(check(cfg).clean)

    def test_branch_reinit_union_both_fields_may_move(self):
        # 两分支分别重组 a.x / a.y；合并后两个字段都可能已移动。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                    {"op": "move", "path": ["a"]},
                ]},
                {"ops": [{"op": "write", "path": ["a", "x"], "reinit": True}]},
                {"ops": [{"op": "write", "path": ["a", "y"], "reinit": True}]},
                {"ops": [
                    {"op": "read", "path": ["a", "x"]},
                    {"op": "read", "path": ["a", "y"]},
                    {"op": "read", "path": ["a"]},
                ]},
            ],
            "successors": [[1, 2], [3], [3], []],
        })
        # 分支1后 a.x 可用 a.y 移动；分支2反之；may 并集 -> 三者都可能移动。
        self.assertEqual(keys(check(cfg)),
                         {("use_of_moved", 3, 0),
                          ("use_of_moved", 3, 1),
                          ("use_of_moved", 3, 2)})

    def test_reinit_then_move_again_then_read(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a", "x"], "reinit": True},
                {"op": "move", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(check(cfg)),
                         {("use_of_moved", 0, 4), ("use_of_moved", 0, 5)})

    def test_reinit_carries_ancestor_move_witness(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "move", "path": ["a"]},
                {"op": "write", "path": ["a", "x"], "reinit": True},
                {"op": "read", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        result = check(cfg)
        conflict = result.conflicts[0]
        # 兄弟字段不可用的见证必须追溯到最初的 move a（诞生点 ops[1]）。
        self.assertEqual(conflict.witnesses[0].origin_op, 1)
        self.assertEqual(conflict.witnesses[0].path, ("a",))


class TestReadWriteWithLoans(unittest.TestCase):
    def test_read_other_field_while_mut_borrow_is_fine(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
                {"op": "borrow_mut", "loan_id": "M", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "y"]},
                {"op": "write", "path": ["a", "y"]},
                {"op": "end_loan", "loan_id": "M"},
            ]}],
            "successors": [[]],
        })
        self.assertTrue(check(cfg).clean)

    def test_shared_loan_blocks_write_only(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": {"x": None}},
                {"op": "borrow_shared", "loan_id": "S", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "x"]},
                {"op": "write", "path": ["a", "x"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(keys(check(cfg)), {("write_while_borrowed", 0, 3)})


if __name__ == "__main__":
    unittest.main()
