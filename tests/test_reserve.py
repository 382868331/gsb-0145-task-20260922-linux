"""reserve/activate 预约-激活借用的验收场景与异常路径。"""

import unittest

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    cfg_from_dict,
    check,
)


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


def single_block(ops, ty=None):
    return {
        "blocks": [{"ops": [{"op": "declare", "var": "a",
                             "ty": ty if ty is not None else {"x": None, "y": None}}]
                   + ops}],
        "successors": [[]],
        "entry": 0,
    }


class TestReserveActivateBasics(unittest.TestCase):
    def test_read_arg_then_activate_is_clean(self):
        # 目标场景：先预约修改对象，再读取它的字段作为参数，最后激活。
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "read", "path": ["a", "x"]},   # 预约阶段允许共享读取
            {"op": "read", "path": ["a", "y"]},
            {"op": "activate", "reservation_id": "R"},
            {"op": "end_loan", "loan_id": "R"},
            {"op": "write", "path": ["a", "x"]},
        ]))
        result = check(cfg)
        self.assertTrue(result.clean, result.explain())

    def test_shared_borrow_held_across_activate_rejected(self):
        # 与上面唯一的区别：激活前开了共享借用且未结束 -> 拒绝。
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "borrow_shared", "loan_id": "S", "path": ["a", "x"]},
            {"op": "activate", "reservation_id": "R"},
        ]))
        result = check(cfg)
        self.assertEqual(keys(result), {("activate_conflict", 0, 3)})

    def test_shared_borrow_ended_before_activate_is_clean(self):
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "borrow_shared", "loan_id": "S", "path": ["a", "x"]},
            {"op": "end_loan", "loan_id": "S"},
            {"op": "activate", "reservation_id": "R"},
        ]))
        self.assertTrue(check(cfg).clean)

    def test_mut_borrow_after_reserve_conflicts(self):
        # 预约之后出现的冲突可变借用仍须报出。
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "borrow_mut", "loan_id": "M", "path": ["a", "x"]},
        ]))
        self.assertEqual(keys(check(cfg)), {("borrow_conflict", 0, 2)})

    def test_reserve_while_mut_loan_active_conflicts(self):
        cfg = cfg_from_dict(single_block([
            {"op": "borrow_mut", "loan_id": "M", "path": ["a", "x"]},
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
        ]))
        self.assertEqual(keys(check(cfg)), {("reserve_conflict", 0, 2)})

    def test_activated_reservation_blocks_read(self):
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "activate", "reservation_id": "R"},
            {"op": "read", "path": ["a", "x"]},   # 已激活 = 独占，读冲突
            {"op": "read", "path": ["a", "y"]},   # 不相交字段仍可读
        ]))
        self.assertEqual(keys(check(cfg)), {("read_while_mut_borrowed", 0, 3)})

    def test_parent_child_field_overlap(self):
        # 父路径预约与子路径可变借用重叠；子路径预约与父路径读取（激活后）。
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a",
                 "ty": {"x": {"z": None}, "y": None}},
                {"op": "reserve", "reservation_id": "R1", "path": ["a"]},
                {"op": "borrow_mut", "loan_id": "M", "path": ["a", "x", "z"]},
                {"op": "end_loan", "loan_id": "M"},
                {"op": "end_loan", "loan_id": "R1"},
                {"op": "reserve", "reservation_id": "R2", "path": ["a", "x"]},
                {"op": "activate", "reservation_id": "R2"},
                {"op": "read", "path": ["a", "x"]},
                {"op": "read", "path": ["a", "y"]},
            ]}],
            "successors": [[]],
        })
        self.assertEqual(
            keys(check(cfg)),
            {("borrow_conflict", 0, 2), ("read_while_mut_borrowed", 0, 7)})


class TestReserveControlFlow(unittest.TestCase):
    branch_cfg = {
        "blocks": [
            {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
            {"ops": []},  # 分支 A
            {"ops": []},  # 分支 B
            {"ops": []},  # 汇合
        ],
        "successors": [[1, 2], [3], [3], []],
        "entry": 0,
    }

    def make(self, pre=(), branch_a=(), branch_b=(), post=()):
        data = {"blocks": [{"ops": list(b["ops"])} for b in self.branch_cfg["blocks"]],
                "successors": self.branch_cfg["successors"], "entry": 0}
        data["blocks"][0]["ops"] += list(pre)
        data["blocks"][1]["ops"] += list(branch_a)
        data["blocks"][2]["ops"] += list(branch_b)
        data["blocks"][3]["ops"] += list(post)
        return cfg_from_dict(data)

    def test_reserve_before_branch_activate_at_merge_clean(self):
        cfg = self.make(
            pre=[{"op": "reserve", "reservation_id": "R", "path": ["a", "x"]}],
            post=[{"op": "activate", "reservation_id": "R"}])
        self.assertTrue(check(cfg).clean)

    def test_reserve_in_one_branch_activate_at_merge_rejected(self):
        # 只在分支 A 预约：汇合后 B 路径上未预约，不能放行。
        cfg = self.make(
            branch_a=[{"op": "reserve", "reservation_id": "R", "path": ["a", "x"]}],
            post=[{"op": "activate", "reservation_id": "R"}])
        self.assertEqual(keys(check(cfg)), {("reservation_not_live", 3, 0)})

    def test_end_in_one_branch_activate_at_merge_rejected(self):
        cfg = self.make(
            pre=[{"op": "reserve", "reservation_id": "R", "path": ["a", "x"]}],
            branch_a=[{"op": "end_loan", "loan_id": "R"}],
            post=[{"op": "activate", "reservation_id": "R"}])
        self.assertEqual(keys(check(cfg)), {("reservation_not_live", 3, 0)})

    def test_double_activate_rejected(self):
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "activate", "reservation_id": "R"},
            {"op": "activate", "reservation_id": "R"},
        ]))
        self.assertEqual(keys(check(cfg)), {("reservation_not_live", 0, 3)})

    def test_activate_after_end_rejected(self):
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "end_loan", "loan_id": "R"},
            {"op": "activate", "reservation_id": "R"},
        ]))
        self.assertEqual(keys(check(cfg)), {("reservation_not_live", 0, 3)})

    def test_double_end_rejected(self):
        cfg = cfg_from_dict(single_block([
            {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
            {"op": "end_loan", "loan_id": "R"},
            {"op": "end_loan", "loan_id": "R"},
        ]))
        self.assertEqual(keys(check(cfg)), {("reservation_not_live", 0, 3)})

    def test_loop_second_activation_rejected_and_budget_separate(self):
        # 循环：第一轮激活合法，回边后第二轮预约已激活 -> 报冲突；
        # 压缩预算 -> BudgetExhaustedError，与 clean 结论严格分开。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
                ]},
                {"ops": [
                    {"op": "activate", "reservation_id": "R"},
                ]},
            ],
            "successors": [[1], [1]],
            "entry": 0,
        })
        result = check(cfg)
        self.assertEqual(keys(result), {("reservation_not_live", 1, 0)})
        with self.assertRaises(BudgetExhaustedError):
            check(cfg, max_iterations=1)


class TestReserveValidation(unittest.TestCase):
    def test_activate_unknown_reservation(self):
        with self.assertRaises(CFGValidationError) as ctx:
            cfg_from_dict(single_block([
                {"op": "activate", "reservation_id": "R"}]))
        self.assertIn("reservation_id", ctx.exception.location)

    def test_duplicate_reservation_id(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(single_block([
                {"op": "reserve", "reservation_id": "R", "path": ["a", "x"]},
                {"op": "reserve", "reservation_id": "R", "path": ["a", "y"]},
            ]))

    def test_reservation_id_collides_with_loan_id(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(single_block([
                {"op": "borrow_shared", "loan_id": "R", "path": ["a", "x"]},
                {"op": "reserve", "reservation_id": "R", "path": ["a", "y"]},
            ]))

    def test_end_loan_unknown_id(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(single_block([
                {"op": "end_loan", "loan_id": "R"}]))

    def test_reserve_path_must_exist(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(single_block([
                {"op": "reserve", "reservation_id": "R", "path": ["a", "nope"]}]))

    def test_reserve_rejects_unknown_keys(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(single_block([
                {"op": "reserve", "reservation_id": "R", "path": ["a", "x"],
                 "loan_id": "x"}]))


if __name__ == "__main__":
    unittest.main()
