"""非法输入的可定位错误、数值参数拒绝、预算/无冲突分离。"""

import math
import unittest

from borrow_check import (
    BudgetExhaustedError,
    CFGValidationError,
    Block,
    CFG,
    Decl,
    Move,
    Read,
    cfg_from_dict,
    check,
    checked_int,
    finite_number,
)


def loc_of(exc):
    return exc.location


class TestInputValidation(unittest.TestCase):
    def test_not_dict(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict([])
        self.assertEqual(loc_of(cm.exception), "cfg")

    def test_unknown_top_level_field(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({"blocks": [], "successors": [], "bogus": 1})
        self.assertIn("未知字段", str(cm.exception))

    def test_block_count_bounds(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({"blocks": [], "successors": []})
        with self.assertRaises(CFGValidationError) as cm:
            data = {"blocks": [{"ops": []} for _ in range(51)],
                    "successors": [[] for _ in range(51)]}
            cfg_from_dict(data)
        self.assertEqual(loc_of(cm.exception), "cfg.blocks")

    def test_successor_out_of_range_and_bool(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({"blocks": [{"ops": []}, {"ops": []}],
                           "successors": [[5], []]})
        # bool 不得被当作 0/1 下标。
        with self.assertRaises(TypeError):
            cfg_from_dict({"blocks": [{"ops": []}], "successors": [[True]]})

    def test_successor_duplicate(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": []}, {"ops": []}],
                "successors": [[1, 1], []],
            })
        self.assertIn("重复", str(cm.exception))

    def test_bad_op_kind(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [{"op": "explode"}]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].op")

    def test_bad_path(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [{"op": "read", "path": []}]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].path")

    def test_path_segment_not_string(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [{"op": "read", "path": ["a", 3]}]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].path")

    def test_undeclared_root(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [{"op": "read", "path": ["a"]}]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].path")

    def test_missing_field(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "read", "path": ["a", "y"]},
                ]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[1].path")

    def test_path_into_leaf(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "read", "path": ["a", "x"]},
                ]}],
                "successors": [[]],
            })

    def test_duplicate_declare(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "declare", "var": "a", "ty": None},
                ]}],
                "successors": [[]],
            })
        self.assertIn("重复 declare", str(cm.exception))

    def test_duplicate_loan_id(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "borrow_mut", "loan_id": "L", "path": ["a"]},
                    {"op": "borrow_mut", "loan_id": "L", "path": ["a"]},
                ]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[2].loan_id")

    def test_end_unknown_loan(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [{"op": "end_loan", "loan_id": "X"}]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].loan_id")

    def test_use_before_declare_same_block(self):
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "read", "path": ["a"]},
                    {"op": "declare", "var": "a", "ty": None},
                ]}],
                "successors": [[]],
            })
        self.assertEqual(loc_of(cm.exception), "blocks[0].ops[0].path")

    def test_declare_must_dominate_use(self):
        # 菱形：declare 在汇合块，不支配分支块中的 move。
        with self.assertRaises(CFGValidationError) as cm:
            cfg_from_dict({
                "blocks": [
                    {"ops": []},
                    {"ops": [{"op": "move", "path": ["a"]}]},
                    {"ops": []},
                    {"ops": [{"op": "declare", "var": "a", "ty": None}]},
                ],
                "successors": [[1, 2], [3], [3], []],
            })
        self.assertIn("不支配", str(cm.exception))

    def test_reinit_must_be_bool(self):
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "write", "path": ["a"], "reinit": 1},
                ]}],
                "successors": [[]],
            })

    def test_native_cfg_type_checked(self):
        with self.assertRaises(CFGValidationError):
            check("not a cfg")


class TestNumericGuards(unittest.TestCase):
    def test_int_rejects_bool(self):
        with self.assertRaises(TypeError):
            checked_int("x", True)
        self.assertEqual(checked_int("x", 3), 3)

    def test_int_rejects_float_nan_inf(self):
        for bad in (math.nan, math.inf, -math.inf, 1.5):
            with self.assertRaises(TypeError):
                checked_int("x", bad)

    def test_int_min_value(self):
        with self.assertRaises(ValueError):
            checked_int("x", -1, min_value=0)

    def test_finite_number_rejects_bad(self):
        with self.assertRaises(ValueError):
            finite_number("x", math.nan)
        with self.assertRaises(ValueError):
            finite_number("x", math.inf)
        with self.assertRaises(TypeError):
            finite_number("x", True)
        self.assertEqual(finite_number("x", 2), 2.0)


class TestBudgetVsClean(unittest.TestCase):
    def _loop_cfg(self):
        return cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "a", "ty": {"x": None}},
                    {"op": "borrow_mut", "loan_id": "L", "path": ["a", "x"]},
                ]},
                {"ops": [{"op": "end_loan", "loan_id": "L"}]},
            ],
            "successors": [[1], [0]],
        })

    def test_clean_proof_is_not_budget(self):
        result = check(self._loop_cfg())
        self.assertTrue(result.clean)
        self.assertGreater(result.iterations, 1)

    def test_budget_exhaustion_is_distinct_path(self):
        with self.assertRaises(BudgetExhaustedError):
            check(self._loop_cfg(), max_iterations=1)

    def test_budget_must_be_positive_int(self):
        with self.assertRaises(TypeError):
            check(self._loop_cfg(), max_iterations=True)
        with self.assertRaises(ValueError):
            check(self._loop_cfg(), max_iterations=0)


if __name__ == "__main__":
    unittest.main()
