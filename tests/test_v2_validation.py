"""v2 输入边界与校验：版本选择、条件上限、守卫公式、两阶段/作用域规则。"""

import unittest

from borrow_check import (
    BudgetExhaustedError,
    CFG,
    Block,
    BorrowMut,
    Decl,
    cfg_from_dict,
    check,
)


def make(**overrides):
    data = {
        "version": 2,
        "conditions": [],
        "blocks": [
            {"ops": [{"op": "declare", "var": "a",
                      "ty": {"x": None}, "init": True}]},
            {"ops": []},
        ],
        "successors": [[1], []],
        "entry": 0,
    }
    data.update(overrides)
    return data


class TestVersioning(unittest.TestCase):
    def test_v1_default_is_frozen(self):
        # 不带 version 即 v1；v2 专属操作被拒绝。
        with self.assertRaises(Exception):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "init", "path": ["a"]},
                ]}], "successors": [[]]})

    def test_unknown_version_rejected(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(version=3))

    def test_v1_block_limit_50_v2_block_limit_80(self):
        d51 = {
            "blocks": [{"ops": []} for _ in range(51)],
            "successors": [[] for _ in range(51)],
        }
        with self.assertRaises(Exception):
            cfg_from_dict(d51)
        d81 = make(
            blocks=[{"ops": []} for _ in range(81)],
            successors=[[] for _ in range(81)])
        with self.assertRaises(Exception):
            cfg_from_dict(d81)
        # v2 下 60 块合法（结构即可；语义要求路径支配，故纯空块）。
        blocks60 = [{"ops": [{"op": "declare", "var": "a",
                              "ty": None, "init": True}]}]
        blocks60 += [{"ops": []} for _ in range(59)]
        succ60 = [[min(i + 1, 59)] for i in range(60)]
        succ60[59] = []
        d60 = make(blocks=blocks60, successors=succ60)
        cfg_from_dict(d60)


class TestConditions(unittest.TestCase):
    def test_too_many_conditions(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(conditions=[f"c{i}" for i in range(9)]))

    def test_duplicate_condition(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(conditions=["c", "c"]))

    def test_guard_references_unknown_condition(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(
                blocks=[{"ops": [{"op": "declare", "var": "a",
                                  "ty": None, "init": True}]},
                        {"ops": []}],
                successors=[[{"block": 1, "when": "z"}], []]))

    def test_bad_formula_shape(self):
        for bad in (["not"], ["and"], ["xor", "c0"], ["const", 1],
                    "guard needs string or list"):
            with self.assertRaises(Exception):
                cfg_from_dict(make(
                    conditions=["c0"],
                    blocks=[{"ops": [{"op": "declare", "var": "a",
                                      "ty": None, "init": True}]},
                            {"ops": []}],
                    successors=[[{"block": 1, "when": bad}], []]))

    def test_duplicate_successor_rejected_use_or(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(
                conditions=["c0"],
                blocks=[{"ops": [{"op": "declare", "var": "a",
                                  "ty": None, "init": True}]},
                        {"ops": []}, {"ops": []}],
                successors=[[{"block": 1, "when": "c0"},
                             {"block": 1, "when": ["not", "c0"]}],
                            [2], []]))


class TestTwoPhaseValidation(unittest.TestCase):
    def test_activate_requires_two_phase_loan(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(
                blocks=[{"ops": [
                    {"op": "declare", "var": "a", "ty": None, "init": True},
                    {"op": "borrow_mut", "loan_id": "L",
                     "path": ["a"], "two_phase": False},
                    {"op": "activate", "loan_id": "L"},
                ]}],
                successors=[[]]))

    def test_activate_unknown_loan(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(
                blocks=[{"ops": [
                    {"op": "declare", "var": "a", "ty": None, "init": True},
                    {"op": "activate", "loan_id": "ZZ"},
                ]}],
                successors=[[]]))

    def test_two_phase_rejected_in_v1(self):
        with self.assertRaises(Exception):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "a", "ty": None},
                    {"op": "borrow_mut", "loan_id": "L",
                     "path": ["a"], "two_phase": True},
                ]}], "successors": [[]]})

    def test_native_dataclass_v2_build(self):
        cfg = CFG(
            version=2, conditions=("c",),
            blocks=[Block(ops=[
                Decl("a", {"x": None}, initialized=False, scope="0"),
                BorrowMut("L", ("a", "x"), two_phase=True),
            ])],
            successors=[[]],
        )
        # 未初始化字段上预约：use_of_uninitialized 确定冲突。
        r = check(cfg)
        self.assertEqual(r.status, "conflict")
        self.assertEqual({(c.kind, c.block, c.op) for c in r.conflicts},
                         {("use_of_uninitialized", 0, 1)})


class TestScopeValidation(unittest.TestCase):
    def test_end_scope_unknown_scope(self):
        with self.assertRaises(Exception):
            cfg_from_dict(make(
                blocks=[{"ops": [
                    {"op": "declare", "var": "a", "ty": None, "init": True},
                    {"op": "end_scope", "scope": "nope"},
                ]}],
                successors=[[]]))

    def test_end_scope_must_be_dominated_by_declarations(self):
        # end_scope 在分支块，declare 在汇合块：不支配。
        with self.assertRaises(Exception):
            cfg_from_dict({
                "version": 2, "conditions": ["c"],
                "blocks": [
                    {"ops": []},
                    {"ops": [{"op": "end_scope", "scope": "0"}]},
                    {"ops": []},
                    {"ops": [{"op": "declare", "var": "a",
                              "ty": None, "init": True}]},
                ],
                "successors": [
                    [{"block": 1, "when": "c"}, {"block": 2, "when": ["not", "c"]}],
                    [3], [3], []],
            })


class TestV2Budget(unittest.TestCase):
    def test_max_iterations_budget_exhaustion_is_exception(self):
        cfg = cfg_from_dict(make(
            blocks=[{"ops": [
                {"op": "declare", "var": "a", "ty": None, "init": True},
                {"op": "borrow_mut", "loan_id": "L", "path": ["a"]},
            ]}, {"ops": [{"op": "end_loan", "loan_id": "L"}]}],
            successors=[[1], [0]]))
        with self.assertRaises(BudgetExhaustedError):
            check(cfg, max_iterations=1)

    def test_loop_budget_must_be_non_negative_int(self):
        cfg = cfg_from_dict(make())
        with self.assertRaises((TypeError, ValueError)):
            check(cfg, max_loop_unrolls=-1)
        with self.assertRaises(TypeError):
            check(cfg, max_loop_unrolls=True)


if __name__ == "__main__":
    unittest.main()
