"""按引用实际使用点推断借用结束（可选 NLL 风格模式）。"""

import unittest

from borrow_check import cfg_from_dict


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


class TestInferredLoanEnd(unittest.TestCase):
    def test_last_use_allows_owner_write_in_same_block(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None},
                {"op": "borrow_mut", "loan_id": "r", "path": ["v"]},
                {"op": "use_loan", "loan_id": "r"},
                {"op": "write", "path": ["v"]},
            ]}],
            "successors": [[]],
        })

        lexical = check_default(cfg)
        inferred = check_inferred(cfg)

        self.assertEqual(keys(lexical), {("write_while_borrowed", 0, 3)})
        self.assertTrue(inferred.clean, inferred.explain())

    def test_branch_without_later_use_ends_loan_on_that_edge(self):
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "v", "ty": None},
                    {"op": "borrow_mut", "loan_id": "r", "path": ["v"]},
                ]},
                {"ops": [{"op": "use_loan", "loan_id": "r"}]},
                {"ops": [{"op": "write", "path": ["v"]}]},
                {"ops": []},
            ],
            "successors": [[1, 2], [3], [3], []],
        })

        inferred = check_inferred(cfg)

        self.assertTrue(inferred.clean, inferred.explain())

    def test_branch_merging_before_later_use_keeps_loan_active(self):
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "v", "ty": None},
                    {"op": "borrow_mut", "loan_id": "r", "path": ["v"]},
                ]},
                {"ops": []},
                {"ops": [{"op": "write", "path": ["v"]}]},
                {"ops": [{"op": "use_loan", "loan_id": "r"}]},
            ],
            "successors": [[1, 2], [3], [3], []],
        })

        inferred = check_inferred(cfg)

        self.assertEqual(keys(inferred), {("write_while_borrowed", 2, 0)})

    def test_loop_future_use_keeps_loan_active_after_current_use(self):
        cfg = cfg_from_dict({
            "blocks": [
                {"ops": [
                    {"op": "declare", "var": "v", "ty": None},
                    {"op": "borrow_mut", "loan_id": "r", "path": ["v"]},
                ]},
                {"ops": [
                    {"op": "use_loan", "loan_id": "r"},
                    {"op": "write", "path": ["v"]},
                ]},
            ],
            "successors": [[1], [1]],
        })

        inferred = check_inferred(cfg)

        self.assertEqual(keys(inferred), {("write_while_borrowed", 1, 1)})
        witness = inferred.conflicts[0].witnesses[0]
        self.assertEqual(witness.loan_id, "r")
        self.assertEqual(witness.block_chain, (0, 1))

    def test_explicit_end_still_works_when_inference_is_enabled(self):
        cfg = cfg_from_dict({
            "blocks": [{"ops": [
                {"op": "declare", "var": "v", "ty": None},
                {"op": "borrow_mut", "loan_id": "r", "path": ["v"]},
                {"op": "end_loan", "loan_id": "r"},
                {"op": "write", "path": ["v"]},
            ]}],
            "successors": [[]],
        })

        self.assertTrue(check_inferred(cfg).clean)

    def test_unknown_use_loan_is_rejected(self):
        from borrow_check import CFGValidationError

        with self.assertRaises(CFGValidationError):
            cfg_from_dict({
                "blocks": [{"ops": [
                    {"op": "declare", "var": "v", "ty": None},
                    {"op": "use_loan", "loan_id": "missing"},
                ]}],
                "successors": [[]],
            })


def check_default(cfg):
    from borrow_check import check
    return check(cfg)


def check_inferred(cfg):
    from borrow_check import check
    return check(cfg, infer_loan_end=True)


if __name__ == "__main__":
    unittest.main()
