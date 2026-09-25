"""片段摘要内容与片段/调用的非法输入可定位错误。"""

import unittest

from borrow_check import (
    CFGValidationError,
    Block,
    BorrowMut,
    Call,
    CFG,
    Decl,
    EndLoan,
    Move,
    Program,
    Segment,
    check_program,
    program_from_dict,
    summarize_segment,
)


def seg_from(data):
    return program_from_dict({
        "cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
            {"op": "call", "segment": "S",
             "args": [["p", ["a", "x"]] for p in data["params"]]}]}],
                "successors": [[]], "entry": 0},
        "segments": {"S": data},
    })


class TestSummaryContent(unittest.TestCase):
    def test_preconditions_and_effects_and_topology(self):
        seg = Segment(
            params=("p",),
            blocks=[
                Block(ops=[]),
                Block(ops=[Move(("p",))]),
                Block(ops=[]),
            ],
            successors=[[1, 2], [2], []],
        )
        s = summarize_segment("S", seg)
        # 拓扑序：entry 先于其后继；出口块集合正确。
        self.assertEqual(s.topo[0], 0)
        self.assertEqual(set(s.exit_blocks), {2})
        # 前置条件：move p 需要 not_moved + no_overlapping_loan。
        self.assertEqual(len(s.preconditions), 1)
        pre = s.preconditions[0]
        self.assertEqual((pre.seg_block, pre.seg_op, pre.symbolic_path),
                         (1, 0, ("p",)))
        self.assertEqual(set(pre.requirement),
                         {"not_moved", "no_overlapping_loan"})
        # 状态变化：move 是写回效果。
        self.assertEqual(len(s.effects), 1)
        self.assertEqual((s.effects[0].kind, s.effects[0].symbolic_path),
                         ("move", ("p",)))
        self.assertEqual(s.local_loans, ())
        self.assertEqual(s.loans_scoped_out, ())

    def test_loan_is_precondition_and_scoped_out_not_effect(self):
        from borrow_check import BorrowMut, EndLoan
        seg = Segment(
            params=("p",),
            blocks=[Block(ops=[BorrowMut("L", ("p",)), EndLoan("L")])],
            successors=[[]],
        )
        s = summarize_segment("S", seg)
        # borrow_mut 的前置条件；loan 记为局部 loan/退出作用域，但不是写回效果。
        self.assertEqual(s.local_loans, ("L",))
        self.assertEqual(s.loans_scoped_out, ("L",))
        self.assertEqual(s.effects, ())
        kinds = {(p.seg_op, p.requirement) for p in s.preconditions}
        self.assertIn((0, ("not_moved", "no_overlapping_loan")), kinds)


class TestSegmentValidation(unittest.TestCase):
    def _expect(self, seg_data, fragment):
        with self.assertRaises(CFGValidationError) as cm:
            seg_from(seg_data)
        self.assertIn(fragment, str(cm.exception))
        return cm.exception

    def test_acyclic_required(self):
        data = {"params": ["p"],
                "blocks": [{"ops": []}, {"ops": []}],
                "successors": [[1], [0]], "entry": 0}
        self._expect(data, "无环")

    def test_no_declare_inside(self):
        data = {"params": ["p"],
                "blocks": [{"ops": [{"op": "declare", "var": "z", "ty": None}]}],
                "successors": [[]], "entry": 0}
        self._expect(data, "declare")

    def test_no_nested_call_inside(self):
        data = {"params": ["p"],
                "blocks": [{"ops": [
                    {"op": "call", "segment": "OTHER",
                     "args": [["p", ["p"]]]}]}],
                "successors": [[]], "entry": 0}
        self._expect(data, "嵌套 call")

    def test_path_root_must_be_param(self):
        data = {"params": ["p"],
                "blocks": [{"ops": [{"op": "read", "path": ["z"]}]}],
                "successors": [[]], "entry": 0}
        self._expect(data, "不是形参")

    def test_duplicate_param(self):
        data = {"params": ["p", "p"],
                "blocks": [{"ops": []}], "successors": [[]], "entry": 0}
        self._expect(data, "重复")

    def test_unreachable_block_rejected(self):
        data = {"params": ["p"],
                "blocks": [{"ops": []}, {"ops": []}],
                "successors": [[], []], "entry": 0}
        self._expect(data, "不可达")

    def test_unended_local_loan_reference(self):
        data = {"params": ["p"],
                "blocks": [{"ops": [{"op": "end_loan", "loan_id": "X"}]}],
                "successors": [[]], "entry": 0}
        self._expect(data, "未在片段中开启")

    def test_duplicate_local_loan(self):
        data = {"params": ["p"],
                "blocks": [{"ops": [
                    {"op": "borrow_mut", "loan_id": "L", "path": ["p"]},
                    {"op": "borrow_mut", "loan_id": "L", "path": ["p"]}]}],
                "successors": [[]], "entry": 0}
        self._expect(data, "不唯一")


class TestCallValidation(unittest.TestCase):
    def _prog(self, call_op):
        return {"cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
            call_op]}], "successors": [[]], "entry": 0},
            "segments": {"S": {"params": ["p", "q"],
                               "blocks": [{"ops": []}],
                               "successors": [[]], "entry": 0}}}

    def test_unknown_segment(self):
        with self.assertRaises(CFGValidationError) as cm:
            program_from_dict(self._prog(
                {"op": "call", "segment": "NOPE",
                 "args": [["p", ["a", "x"]], ["q", ["a", "y"]]]}))
        self.assertIn("未定义", str(cm.exception))

    def test_missing_param_binding(self):
        with self.assertRaises(CFGValidationError) as cm:
            program_from_dict(self._prog(
                {"op": "call", "segment": "S",
                 "args": [["p", ["a", "x"]]]}))
        self.assertIn("缺少形参", str(cm.exception))

    def test_unknown_param(self):
        with self.assertRaises(CFGValidationError) as cm:
            program_from_dict(self._prog(
                {"op": "call", "segment": "S",
                 "args": [["p", ["a", "x"]], ["zzz", ["a", "y"]]]}))
        self.assertIn("没有形参", str(cm.exception))

    def test_duplicate_param_binding(self):
        with self.assertRaises(CFGValidationError) as cm:
            program_from_dict(self._prog(
                {"op": "call", "segment": "S",
                 "args": [["p", ["a", "x"]], ["p", ["a", "y"]]]}))
        self.assertIn("重复绑定", str(cm.exception))

    def test_substituted_path_missing_field(self):
        # 片段访问 p.x；实参 a.y 是叶子（无 x 字段）：替换后越界，定位到 args。
        prog = {"cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "a", "ty": {"x": None, "y": None}},
            {"op": "call", "segment": "S",
             "args": [["p", ["a", "y"]]]}]}], "successors": [[]], "entry": 0},
            "segments": {"S": {"params": ["p"],
                               "blocks": [{"ops": [
                                   {"op": "read", "path": ["p", "x"]}]}],
                               "successors": [[]], "entry": 0}}}
        with self.assertRaises(CFGValidationError) as cm:
            program_from_dict(prog)
        self.assertEqual(cm.exception.location, "blocks[0].ops[1].args")

    def test_call_arg_must_be_declared_and_dominated(self):
        # 实参根变量未声明。
        prog = {"cfg": {"blocks": [{"ops": [
            {"op": "declare", "var": "a", "ty": None},
            {"op": "call", "segment": "S",
             "args": [["p", ["b"]]]}]}], "successors": [[]], "entry": 0},
            "segments": {"S": {"params": ["p"],
                               "blocks": [{"ops": []}],
                               "successors": [[]], "entry": 0}}}
        with self.assertRaises(CFGValidationError):
            program_from_dict(prog)


if __name__ == "__main__":
    unittest.main()
