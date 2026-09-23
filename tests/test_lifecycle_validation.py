"""v2 生命周期模式的输入校验、边界与兼容行为。"""

import unittest

from borrow_check import (
    CFG,
    Block,
    BorrowActivate,
    BorrowReserve,
    CFGValidationError,
    Decl,
    cfg_from_dict,
    check,
)


def loc_of(exc):
    return exc.location


class TestLifecycleValidation(unittest.TestCase):
    def test_activate_unknown_loan(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [
                    {"ops": [{"op": "declare", "var": "a", "ty": None}]},
                    {"ops": [{"op": "borrow_activate", "loan_id": "X"}]},
                ],
                "successors": [[1], []],
            }, max_blocks=64)
        self.assertIn("未出生", str(cm.exception))

    def test_activate_one_shot_loan_rejected(self):
        # 一阶段 borrow_mut 出生即活跃，不能对其 borrow_activate。
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "borrow_mut", "loan_id": "M", "path": ["a"]},
                    {"op": "borrow_activate", "loan_id": "M"},
                ]}],
                "successors": [[]],
            }, max_blocks=64)
        self.assertIn("不是两阶段预约", str(cm.exception))

    def test_duplicate_activate_rejected(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "borrow_reserve", "loan_id": "R", "path": ["a"]},
                    {"op": "borrow_activate", "loan_id": "R"},
                    {"op": "borrow_activate", "loan_id": "R"},
                ]}],
                "successors": [[]],
            }, max_blocks=64)
        self.assertIn("重复", str(cm.exception))

    def test_too_many_conditions(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None}]}],
                "successors": [[]],
                "conditions": [f"c{i}" for i in range(9)],
            }, max_blocks=64)
        self.assertEqual(loc_of(cm.exception), "cfg.conditions")

    def test_duplicate_condition_atom(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None}]}],
                "successors": [[]],
                "conditions": ["p", "p"],
            }, max_blocks=64)

    def test_guard_unknown_atom(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [
                    {"ops": [{"op": "declare", "var": "a", "ty": None}]},
                    {"ops": []},
                ],
                "successors": [[{"block": 1, "when": "z"}], []],
                "conditions": ["p"],
            }, max_blocks=64)
        self.assertIn("未在 conditions", str(cm.exception))

    def test_guard_malformed(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({
                "blocks": [
                    {"ops": [{"op": "declare", "var": "a", "ty": None}]},
                    {"ops": []},
                ],
                "successors": [[{"block": 1, "when": {"xor": ["p"]}}], []],
                "conditions": ["p"],
            }, max_blocks=64)

    def test_v1_max_blocks_still_50(self):
        data = {
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None}]}
                for _ in range(51)],
            "successors": [[] for _ in range(51)],
        }
        with self.assertRaises(CFGValidationError):
            cfg_from_dict(data)  # 默认 v1 上限 50

    def test_native_two_phase_dataclass_api(self):
        cfg = CFG(
            blocks=[Block(ops=[
                Decl("a", {"x": None}),
                BorrowReserve("R", ("a", "x")),
                BorrowActivate("R"),
            ])],
            successors=[[]],
        )
        r = check(cfg, lifecycle=True)
        self.assertEqual(r.status, "ok")

    def test_v1_rejects_v2_ops_and_guards(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None},
                {"op": "borrow_reserve", "loan_id": "R",
                 "path": ["a"]}]},
                {"ops": []}],
            "successors": [[1], []],
        }, max_blocks=64)
        with self.assertRaises(CFGValidationError):
            check(cfg)  # v1 默认引擎拒绝 v2 操作，不静默忽略
        cfg2 = cfg_from_dict({
            "conditions": ["p"],
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": None}]},
                {"ops": []}],
            "successors": [[{"block": 1, "when": "p"}], []],
        }, max_blocks=64)
        with self.assertRaises(CFGValidationError):
            check(cfg2)

    def test_v1_result_has_default_status_fields(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "a", "ty": None}]}],
            "successors": [[]],
        })
        r = check(cfg)
        self.assertTrue(r.clean)
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.lifecycle)
        self.assertEqual(r.unknown_reasons, ())


if __name__ == "__main__":
    unittest.main()
