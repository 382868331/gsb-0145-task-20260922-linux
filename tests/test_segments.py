"""操作片段调用：四个验收场景 + 局部 loan 作用域 + 写回/别名/重复调用。

冲突规范坐标为 ``(kind, 主块, 主操作, 片段块|None, 片段操作|None)``：
片段内冲突的主坐标是**调用指令**位置，片段内坐标另由 seg_* 字段给出。
"""

import unittest

from borrow_check import (
    CFG,
    Block,
    BorrowMut,
    Call,
    Decl,
    Move,
    Program,
    Read,
    Segment,
    Write,
    check,
    check_program,
    program_from_dict,
)

TY = {"x": None, "y": None}

# 片段 S(p, q)：各自可变借用 p、q，再各自结束。
SEG_DOUBLE = {
    "params": ["p", "q"],
    "blocks": [{"ops": [
        {"op": "borrow_mut", "loan_id": "Lp", "path": ["p"]},
        {"op": "borrow_mut", "loan_id": "Lq", "path": ["q"]},
        {"op": "end_loan", "loan_id": "Lp"},
        {"op": "end_loan", "loan_id": "Lq"},
    ]}],
    "successors": [[]],
    "entry": 0,
}

# 片段 M(p)：菱形无环；一个分支 move p，另一分支空，汇合后返回。
SEG_BRANCH_MOVE = {
    "params": ["p"],
    "blocks": [
        {"ops": []},
        {"ops": [{"op": "move", "path": ["p"]}]},
        {"ops": []},
    ],
    "successors": [[1, 2], [2], []],
    "entry": 0,
}


def keys5(result):
    return {(c.kind, c.block, c.op, c.seg_block, c.seg_op)
            for c in result.conflicts}


def call_program(segments, main_ops):
    return {
        "cfg": {"blocks": [{"ops": main_ops}], "successors": [[]], "entry": 0},
        "segments": segments,
    }


class TestSameFieldDoubleMut(unittest.TestCase):
    def _prog(self, arg_p, arg_q):
        return program_from_dict(call_program(
            {"S": SEG_DOUBLE},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "S",
              "args": [["p", arg_p], ["q", arg_q]]}]))

    def test_same_field_two_mut_borrows_conflict(self):
        # 两个形参指向同一条路径 a.x：不默认形参互不重叠。
        result = check_program(self._prog(["a", "x"], ["a", "x"]))
        self.assertEqual(
            keys5(result),
            {("borrow_conflict", 0, 1, 0, 1)})
        c = result.conflicts[0]
        # 出错见证同时给调用位置与片段内路径。
        self.assertEqual((c.block, c.op), (0, 1))       # 调用指令位置
        self.assertEqual((c.seg_block, c.seg_op), (0, 1))  # 片段内第二个 borrow
        self.assertEqual(c.formal_path, ("q",))
        self.assertEqual(c.path, ("a", "x"))             # 实参替换后真实路径
        w = c.witnesses[0]
        self.assertEqual(w.segment, "S")
        self.assertEqual(w.formal_path, ("p",))         # 第一个 borrow 形参路径
        self.assertEqual(w.path, ("a", "x"))
        self.assertEqual(w.loan_id, "Lp")

    def test_parent_child_alias_conflicts(self):
        # 形参 p=a、q=a.x：父子路径别名，对称重叠 -> 冲突。
        result = check_program(self._prog(["a"], ["a", "x"]))
        self.assertEqual(
            keys5(result),
            {("borrow_conflict", 0, 1, 0, 1)})

    def test_distinct_fields_independent(self):
        # 形参指向同一记录的不同字段 a.x / a.y：互不相交，干净。
        result = check_program(self._prog(["a", "x"], ["a", "y"]))
        self.assertTrue(result.clean, result.explain())


class TestBranchMoveThenReturn(unittest.TestCase):
    def test_move_in_one_branch_write_back_to_caller(self):
        prog = program_from_dict(call_program(
            {"M": SEG_BRANCH_MOVE},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "M", "args": [["p", ["a", "x"]]]},
             {"op": "read", "path": ["a", "x"]},   # 可能已被分支移动
             {"op": "read", "path": ["a", "y"]}]))  # 不同字段不受影响
        result = check_program(prog)
        self.assertEqual(
            keys5(result),
            {("use_of_moved", 0, 2, None, None)})
        c = result.conflicts[0]
        w = c.witnesses[0]
        # 移动发生在片段内，见证要追到片段 blocks[1].ops[0] 的形参路径 p。
        self.assertEqual(w.segment, "M")
        self.assertEqual((w.seg_block, w.seg_op), (1, 0))
        self.assertEqual(w.formal_path, ("p",))
        self.assertEqual(w.path, ("a", "x"))
        self.assertEqual((w.origin_block, w.origin_op), (0, 1))  # 调用点

    def test_move_nested_field_via_param_suffix(self):
        # 片段内访问形参的字段：move p.x（p=a 即 move a.x），只压 x 子树，
        # 兄弟字段 a.y 仍可用——验证形参后缀替换与方向性移动。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [{"op": "move", "path": ["p", "x"]}]}],
            "successors": [[]], "entry": 0,
        }
        prog = program_from_dict(call_program(
            {"P": seg},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "P", "args": [["p", ["a"]]]},
             {"op": "read", "path": ["a", "x"]},
             {"op": "read", "path": ["a", "y"]}]))
        result = check_program(prog)
        self.assertEqual(keys5(result), {("use_of_moved", 0, 2, None, None)})


class TestLocalLoansScopedOut(unittest.TestCase):
    def test_unended_local_loan_does_not_leak(self):
        # 片段内可变借用 p 后**不 end**；退出片段即结束，调用方随后写 p 合法。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "L", "path": ["p"]}]}],
            "successors": [[]], "entry": 0,
        }
        prog = program_from_dict(call_program(
            {"K": seg},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "K", "args": [["p", ["a", "x"]]]},
             {"op": "write", "path": ["a", "x"]}]))
        self.assertTrue(check_program(prog).clean)

    def test_unended_local_loan_still_conflicts_inside_segment(self):
        # 但片段内部该借用与随后的读取仍冲突（片段内坐标）。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "L", "path": ["p"]},
                {"op": "read", "path": ["p"]}]}],
            "successors": [[]], "entry": 0,
        }
        prog = program_from_dict(call_program(
            {"K": seg},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "K", "args": [["p", ["a", "x"]]]}]))
        result = check_program(prog)
        self.assertEqual(
            keys5(result),
            {("read_while_mut_borrowed", 0, 1, 0, 1)})

    def test_repeated_calls_do_not_reuse_loan_identity(self):
        # 片段 [borrow_mut p; read p] 连续调用两次：每次恰好一个片段内
        # read_while_mut；若串用上一次调用的 loan，第二次的 borrow 还会
        # 额外报 borrow_conflict。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "L", "path": ["p"]},
                {"op": "read", "path": ["p"]}]}],
            "successors": [[]], "entry": 0,
        }
        main = [{"op": "declare", "var": "a", "ty": TY},
                {"op": "call", "segment": "K", "args": [["p", ["a", "x"]]]},
                {"op": "call", "segment": "K", "args": [["p", ["a", "x"]]]}]
        result = check_program(program_from_dict(call_program({"K": seg}, main)))
        self.assertEqual(keys5(result),
                         {("read_while_mut_borrowed", 0, 1, 0, 1),
                          ("read_while_mut_borrowed", 0, 2, 0, 1)})

    def test_caller_loan_remains_active_across_call(self):
        # 调用方在调用前开启、未结束的借用跨越调用仍活跃；片段借同路径
        # 必须与它冲突（局部清除只清片段自己的 loan）。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "L", "path": ["p"]}]}],
            "successors": [[]], "entry": 0,
        }
        main = [{"op": "declare", "var": "a", "ty": TY},
                {"op": "borrow_mut", "loan_id": "OUT", "path": ["a", "x"]},
                {"op": "call", "segment": "K", "args": [["p", ["a", "x"]]]},
                {"op": "read", "path": ["a", "x"]}]
        result = check_program(program_from_dict(call_program({"K": seg}, main)))
        # 片段 borrow 与调用方 OUT 冲突；调用后 OUT 仍活跃，read 也冲突。
        self.assertEqual(keys5(result),
                         {("borrow_conflict", 0, 2, 0, 0),
                          ("read_while_mut_borrowed", 0, 3, None, None)})


class TestWriteBackEffects(unittest.TestCase):
    def test_reinit_inside_segment_restores_for_caller(self):
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [
                {"op": "move", "path": ["p"]},
                {"op": "write", "path": ["p"], "reinit": True}]}],
            "successors": [[]], "entry": 0,
        }
        prog = program_from_dict(call_program(
            {"R": seg},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "call", "segment": "R", "args": [["p", ["a", "x"]]]},
             {"op": "read", "path": ["a", "x"]}]))
        self.assertTrue(check_program(prog).clean)

    def test_move_inside_segment_conflicts_with_caller_state(self):
        # 调用方先 move a.x；片段内再 move p(=a.x)：片段操作也要按调用方
        # 进入状态检查，报片段内 use_of_moved。
        seg = {
            "params": ["p"],
            "blocks": [{"ops": [{"op": "move", "path": ["p"]}]}],
            "successors": [[]], "entry": 0,
        }
        prog = program_from_dict(call_program(
            {"P": seg},
            [{"op": "declare", "var": "a", "ty": TY},
             {"op": "move", "path": ["a", "x"]},
             {"op": "call", "segment": "P", "args": [["p", ["a", "x"]]]}]))
        result = check_program(prog)
        self.assertEqual(keys5(result), {("use_of_moved", 0, 2, 0, 0)})


class TestNativeDataclassSegmentAPI(unittest.TestCase):
    def test_build_program_with_dataclasses(self):
        seg = Segment(
            params=("p",),
            blocks=[Block(ops=[BorrowMut("L", ("p",)), Read(("p",))])],
            successors=[[]],
        )
        cfg = CFG(blocks=[Block(ops=[
            Decl("a", TY),
            Call("S", (("p", ("a", "x")),)),
        ])], successors=[[]])
        result = check_program(Program(cfg=cfg, segments={"S": seg}))
        self.assertEqual(keys5(result),
                         {("read_while_mut_borrowed", 0, 1, 0, 1)})

    def test_plain_check_rejects_call_without_program(self):
        # 直接对含 call 的 CFG 调 check（无片段定义）必须报可定位错误，
        # 已有非调用指令行为不变。
        cfg = CFG(blocks=[Block(ops=[
            Decl("a", TY),
            Call("S", (("p", ("a", "x")),)),
        ])], successors=[[]])
        with self.assertRaises(Exception):
            check(cfg)


if __name__ == "__main__":
    unittest.main()
