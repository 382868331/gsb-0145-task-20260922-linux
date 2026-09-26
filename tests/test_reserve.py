"""预约式借用（reserve/activate/end_reserve）的验收与异常路径测试。

场景对应题面：
* 先预约修改对象、再读取它的长度作为参数，最后激活 —— 合法；
* 激活前保留共享借用 —— 拒绝（activate_conflict）；
* 预约之后出现的冲突可变借用 —— 报 borrow_conflict；
* 控制流汇合后只在所有到达路径都预约成功且未结束时允许激活；
* 重复激活、结束后再操作 —— 只能作用于仍存活的那一次；
* 父字段与子字段重叠按现有路径规则判定；
* 带循环时沿用原预算，预算耗尽与「证明无冲突」严格分开。
"""

import unittest

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    cfg_from_dict,
    check,
)

TY = {"data": None, "len": None, "meta": {"tag": None}}


def single(ops, ty=TY):
    return cfg_from_dict({
        "blocks": [{"ops": [{"op": "declare", "var": "d", "ty": ty}, *ops]}],
        "successors": [[]],
    })


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


class TestReserveBasic(unittest.TestCase):
    def test_read_args_then_activate_is_legal(self):
        # 先预约修改对象，再读取它的长度作为参数，最后激活：全程干净。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "read", "path": ["d", "len"]},      # 预约阶段允许共享读取
            {"op": "read", "path": ["d", "data"]},     # 读取被预约路径本身也允许
            {"op": "activate", "reserve_id": "R1"},
            {"op": "end_reserve", "reserve_id": "R1"},
            {"op": "write", "path": ["d", "data"]},    # 结束后独占已释放
        ])
        result = check(cfg)
        self.assertTrue(result.clean, result.explain())

    def test_shared_borrow_kept_before_activate_rejected(self):
        # 激活前保留共享借用 -> 升级被拒绝。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_shared", "loan_id": "L1", "path": ["d", "data"]},
            {"op": "read", "path": ["d", "len"]},
            {"op": "activate", "reserve_id": "R1"},
        ])
        self.assertEqual(keys(check(cfg)), {("activate_conflict", 0, 4)})

    def test_shared_borrow_ended_before_activate_is_legal(self):
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_shared", "loan_id": "L1", "path": ["d", "data"]},
            {"op": "end_loan", "loan_id": "L1"},
            {"op": "activate", "reserve_id": "R1"},
            {"op": "end_reserve", "reserve_id": "R1"},
        ])
        self.assertTrue(check(cfg).clean)

    def test_mut_borrow_after_reserve_conflicts(self):
        # 预约之后出现的冲突可变借用仍须报出。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "data"]},
        ])
        self.assertEqual(keys(check(cfg)), {("borrow_conflict", 0, 2)})

    def test_reserve_after_mut_borrow_conflicts(self):
        cfg = single([
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "data"]},
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
        ])
        self.assertEqual(keys(check(cfg)), {("reserve_conflict", 0, 2)})

    def test_read_after_activate_conflicts(self):
        # 激活后即为独占：普通读取被拒绝。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "activate", "reserve_id": "R1"},
            {"op": "read", "path": ["d", "data"]},
        ])
        self.assertEqual(keys(check(cfg)), {("read_while_mut_borrowed", 0, 3)})

    def test_double_activate_rejected(self):
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "activate", "reserve_id": "R1"},
            {"op": "activate", "reserve_id": "R1"},  # 已激活，不再存活
        ])
        self.assertEqual(keys(check(cfg)), {("activate_not_live", 0, 3)})

    def test_end_then_operate_rejected(self):
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "end_reserve", "reserve_id": "R1"},
            {"op": "end_reserve", "reserve_id": "R1"},  # 已结束
            {"op": "activate", "reserve_id": "R1"},     # 已结束
        ])
        self.assertEqual(
            keys(check(cfg)),
            {("end_not_live", 0, 3), ("activate_not_live", 0, 4)})

    def test_activate_after_end_reserve_then_reinit(self):
        # 结束后重新预约同一 id 不可能（静态唯一），但结束->新预约另一 id 合法。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "end_reserve", "reserve_id": "R1"},
            {"op": "reserve", "reserve_id": "R2", "path": ["d", "data"]},
            {"op": "activate", "reserve_id": "R2"},
            {"op": "end_reserve", "reserve_id": "R2"},
        ])
        self.assertTrue(check(cfg).clean)


class TestReserveFieldOverlap(unittest.TestCase):
    def test_reserve_parent_mut_child_conflicts(self):
        # 预约父路径后，子路径的可变借用冲突（对称重叠）。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d"]},
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "data"]},
        ])
        self.assertEqual(keys(check(cfg)), {("borrow_conflict", 0, 2)})

    def test_reserve_child_mut_parent_conflicts(self):
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "meta", "tag"]},
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "meta"]},
        ])
        self.assertEqual(keys(check(cfg)), {("borrow_conflict", 0, 2)})

    def test_activate_with_parent_shared_borrow_rejected(self):
        # 预约子路径，但父路径上保留着共享借用：激活仍被拒绝。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_shared", "loan_id": "L1", "path": ["d"]},
            {"op": "activate", "reserve_id": "R1"},
        ])
        self.assertEqual(keys(check(cfg)), {("activate_conflict", 0, 3)})

    def test_disjoint_fields_do_not_conflict(self):
        # d.data 的预约与 d.len 的可变借用互不相交。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "len"]},
            {"op": "activate", "reserve_id": "R1"},
            {"op": "end_reserve", "reserve_id": "R1"},
            {"op": "end_loan", "loan_id": "L1"},
        ])
        # 激活时 L1 仍活跃但与预约路径不相交 -> 合法。
        self.assertTrue(check(cfg).clean)


class TestReserveControlFlow(unittest.TestCase):
    def _branch_cfg(self, then_ops, else_ops, join_ops):
        return cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "d", "ty": TY},
                    {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
                ]},
                {"ops": then_ops},
                {"ops": else_ops},
                {"ops": join_ops},
            ],
            "successors": [[1, 2], [3], [3], []],
            "entry": 0,
        })

    def test_activate_allowed_when_live_on_all_paths(self):
        # 两条分支都未结束预约 -> 汇合后允许激活。
        cfg = self._branch_cfg(
            [{"op": "read", "path": ["d", "len"]}],
            [{"op": "read", "path": ["d", "data"]}],
            [{"op": "activate", "reserve_id": "R1"},
             {"op": "end_reserve", "reserve_id": "R1"}],
        )
        self.assertTrue(check(cfg).clean)

    def test_activate_rejected_when_ended_on_one_path(self):
        # 一条分支结束了预约：不能因为另一条路可用就放行。
        cfg = self._branch_cfg(
            [{"op": "end_reserve", "reserve_id": "R1"}],
            [],
            [{"op": "activate", "reserve_id": "R1"}],
        )
        self.assertEqual(keys(check(cfg)), {("activate_not_live", 3, 0)})

    def test_end_rejected_when_ended_on_one_path(self):
        cfg = self._branch_cfg(
            [{"op": "end_reserve", "reserve_id": "R1"}],
            [],
            [{"op": "end_reserve", "reserve_id": "R1"}],
        )
        self.assertEqual(keys(check(cfg)), {("end_not_live", 3, 0)})

    def test_reserve_inside_single_branch_not_visible_at_join(self):
        # 预约只发生在一条分支里：汇合点 must 不成立。
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [{"op": "declare", "var": "d", "ty": TY}]},
                {"ops": [
                    {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
                ]},
                {"ops": []},
                {"ops": [{"op": "activate", "reserve_id": "R1"}]},
            ],
            "successors": [[1, 2], [3], [3], []],
            "entry": 0,
        })
        self.assertEqual(keys(check(cfg)), {("activate_not_live", 3, 0)})

    def test_mixed_end_on_paths_uses_alive_must(self):
        # 一条分支激活、一条分支保持预约：汇合后 end_reserve 合法
        # （两条路径上都仍存活），但再次 activate 不合法。
        cfg = self._branch_cfg(
            [{"op": "activate", "reserve_id": "R1"}],
            [],
            [{"op": "end_reserve", "reserve_id": "R1"}],
        )
        self.assertTrue(check(cfg).clean)


class TestReserveLoops(unittest.TestCase):
    def _loop_cfg(self):
        # 预约在循环外，循环内反复共享读取，出循环后激活并结束。
        return cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "d", "ty": TY},
                    {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
                ]},
                {"ops": [
                    {"op": "read", "path": ["d", "len"]},
                    {"op": "read", "path": ["d", "data"]},
                ]},
                {"ops": [
                    {"op": "activate", "reserve_id": "R1"},
                    {"op": "end_reserve", "reserve_id": "R1"},
                ]},
            ],
            "successors": [[1], [1, 2], []],
            "entry": 0,
        })

    def test_loop_with_live_reserve_converges_clean(self):
        result = check(self._loop_cfg())
        self.assertTrue(result.clean, result.explain())

    def test_loop_budget_exhausted_is_not_proof(self):
        # 沿用原预算接口：显式压缩预算 -> BudgetExhaustedError，
        # 与 clean=True 是两条严格分开的路径（含预约时上界未被证明，
        # 见 README「已知限制」）。
        with self.assertRaises(BudgetExhaustedError):
            check(self._loop_cfg(), max_iterations=1)


class TestReserveValidation(unittest.TestCase):
    def test_duplicate_reserve_id_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([
                {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
                {"op": "reserve", "reserve_id": "R1", "path": ["d", "len"]},
            ])

    def test_activate_unknown_id_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([{"op": "activate", "reserve_id": "nope"}])

    def test_end_reserve_unknown_id_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([{"op": "end_reserve", "reserve_id": "nope"}])

    def test_reserve_bad_path_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([{"op": "reserve", "reserve_id": "R1", "path": ["d", "nope"]}])

    def test_reserve_missing_field_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([{"op": "reserve", "path": ["d", "data"]}])

    def test_activate_empty_id_rejected(self):
        with self.assertRaises(CFGValidationError):
            single([{"op": "activate", "reserve_id": ""}])

    def test_reserve_witness_chain_rendered(self):
        # 冲突见证链覆盖预约来源（demo 的 explain 路径）。
        cfg = single([
            {"op": "reserve", "reserve_id": "R1", "path": ["d", "data"]},
            {"op": "borrow_mut", "loan_id": "L1", "path": ["d", "data"]},
        ])
        text = check(cfg).explain()
        self.assertIn("R1", text)
        self.assertIn("blocks[0]", text)


if __name__ == "__main__":
    unittest.main()
