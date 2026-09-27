"""infer_loan_ends=True：按使用位置推断借用结束点的测试。

期望值按题面契约手写：借用在其区域最后一次被 read 之后结束；
end_loan 显式结束保留；分支上不再使用的路径允许原对象恢复操作；
循环回边使用收敛后的 liveness 不动点。
"""

from __future__ import annotations

import unittest

from borrow_check import cfg_from_dict, check

DECL = {"op": "declare", "var": "a", "ty": {"x": None, "y": None}}


def mk(blocks, successors):
    return cfg_from_dict({"blocks": blocks, "successors": successors, "entry": 0})


def kinds(result):
    return [(c.kind, c.block, c.op) for c in result.conflicts]


class TestInferBasic(unittest.TestCase):
    def test_unused_mut_loan_allows_write(self):
        # borrow_mut(a.x) 后 a.x 再未被读取：推断模式下写合法；显式模式冲突。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "write", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(kinds(check(cfg)),
                         [("write_while_borrowed", 0, 2)])
        self.assertTrue(check(cfg, infer_loan_ends=True).clean)

    def test_unused_mut_loan_allows_move(self):
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "move", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(kinds(check(cfg)),
                         [("move_while_borrowed", 0, 2)])
        self.assertTrue(check(cfg, infer_loan_ends=True).clean)

    def test_read_is_a_use_and_keeps_loan_live(self):
        # 借用区域之后还会被读取：读取点借用仍活跃，冲突照常报告。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(kinds(check(cfg, infer_loan_ends=True)),
                         [("read_while_mut_borrowed", 0, 2)])

    def test_write_before_later_read_still_conflicts(self):
        # 最后一次使用（read）之前的写仍与活跃借用冲突。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "write", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(
            kinds(check(cfg, infer_loan_ends=True)),
            [("write_while_borrowed", 0, 2), ("read_while_mut_borrowed", 0, 3)])

    def test_shared_loan_ends_after_last_read(self):
        # shared 借用：最后一次读取之后写合法。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_shared", "loan_id": "L", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "x"]},
            {"op": "write", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(kinds(check(cfg)),
                         [("write_while_borrowed", 0, 3)])
        self.assertTrue(check(cfg, infer_loan_ends=True).clean)

    def test_explicit_end_loan_still_honored(self):
        # 显式 end_loan 之后的读取不算借用的使用：两种模式都干净。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "end_loan", "loan_id": "L"},
            {"op": "read", "path": ["a", "x"]},
            {"op": "write", "path": ["a", "x"]},
        ]}], [[]])
        self.assertTrue(check(cfg).clean)
        self.assertTrue(check(cfg, infer_loan_ends=True).clean)

    def test_overlapping_region_read_keeps_ancestor_loan_live(self):
        # 读取子路径 a.x 也算使用了作用于祖先 a 的借用。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a"]},
            {"op": "write", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(
            kinds(check(cfg, infer_loan_ends=True)),
            [("write_while_borrowed", 0, 2), ("read_while_mut_borrowed", 0, 3)])


class TestInferBranches(unittest.TestCase):
    def test_loan_unused_on_one_branch_ends_early_there(self):
        # 块0 借用；块1 读取 a.x（该路径上仍活跃）；块2 写 a.x 且之后不再读：
        # 推断模式下块2 的写合法，块1 的读取冲突保留。
        cfg = mk([
            {"ops": [DECL,
                     {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]}]},
            {"ops": [{"op": "read", "path": ["a", "x"]}]},
            {"ops": [{"op": "write", "path": ["a", "x"]}]},
        ], [[1, 2], [], []])
        self.assertEqual(kinds(check(cfg, infer_loan_ends=True)),
                         [("read_while_mut_borrowed", 1, 0)])
        # 显式模式：块2 的写也冲突。
        self.assertEqual(
            kinds(check(cfg)),
            [("read_while_mut_borrowed", 1, 0), ("write_while_borrowed", 2, 0)])

    def test_use_after_merge_keeps_loan_live_on_all_paths(self):
        # 汇合块读取 a.x：任意前驱路径上的写都不能提前结束借用。
        cfg = mk([
            {"ops": [DECL,
                     {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]}]},
            {"ops": [{"op": "read", "path": ["a", "y"]}]},
            {"ops": [{"op": "write", "path": ["a", "x"]}]},
            {"ops": [{"op": "read", "path": ["a", "x"]}]},
        ], [[1, 2], [3], [3], []])
        self.assertEqual(
            kinds(check(cfg, infer_loan_ends=True)),
            [("write_while_borrowed", 2, 0), ("read_while_mut_borrowed", 3, 0)])


class TestInferLoops(unittest.TestCase):
    def test_loop_write_conflicts_when_read_after_loop(self):
        # 循环内写 a.x，循环外读 a.x：借用穿过循环保持活跃，写冲突。
        cfg = mk([
            {"ops": [DECL,
                     {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]}]},
            {"ops": [{"op": "write", "path": ["a", "x"]}]},
            {"ops": [{"op": "read", "path": ["a", "x"]}]},
        ], [[1], [1, 2], []])
        self.assertEqual(
            kinds(check(cfg, infer_loan_ends=True)),
            [("write_while_borrowed", 1, 0), ("read_while_mut_borrowed", 2, 0)])

    def test_loop_write_clean_when_never_read(self):
        # 循环内写 a.x，之后只读不相交字段 a.y：借用提前结束，干净。
        cfg = mk([
            {"ops": [DECL,
                     {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]}]},
            {"ops": [{"op": "write", "path": ["a", "x"]}]},
            {"ops": [{"op": "read", "path": ["a", "y"]}]},
        ], [[1], [1, 2], []])
        self.assertTrue(check(cfg, infer_loan_ends=True).clean)
        # 显式模式：借用从未结束，循环内写冲突。
        self.assertEqual(kinds(check(cfg)),
                         [("write_while_borrowed", 1, 0)])

    def test_unended_loan_in_loop_still_detected(self):
        # 与 demo 场景 2 相同：循环内借用且之后每轮都读取，
        # 推断模式同样必须经回边发现冲突（liveness 不动点）。
        cfg = mk([
            {"ops": [DECL]},
            {"ops": [
                {"op": "read", "path": ["a", "x"]},
                {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            ]},
        ], [[1], [1]])
        result = check(cfg, infer_loan_ends=True)
        self.assertEqual(kinds(result), [("read_while_mut_borrowed", 1, 0)])
        w = result.conflicts[0].witnesses[0]
        self.assertEqual((w.loan_id, w.origin_block, w.origin_op), ("L", 1, 1))


class TestInferParamValidation(unittest.TestCase):
    def test_non_bool_rejected(self):
        cfg = mk([{"ops": [DECL]}], [[]])
        for bad in (1, 0, "yes", None, 1.0):
            with self.assertRaises(TypeError, msg=repr(bad)):
                check(cfg, infer_loan_ends=bad)

    def test_default_is_explicit_semantics(self):
        # 不传参 == 显式模式（与 infer_loan_ends=False 一致）。
        cfg = mk([{"ops": [
            DECL,
            {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
            {"op": "write", "path": ["a", "x"]},
        ]}], [[]])
        self.assertEqual(kinds(check(cfg)), kinds(check(cfg, infer_loan_ends=False)))


if __name__ == "__main__":
    unittest.main()

