"""操作片段调用（本次新增功能）的测试。

覆盖题面四个场景：

1. 同字段双可变借用；
2. 父子路径别名（两个形参绑定到互相重叠的实参）；
3. 不同字段独立访问；
4. 某分支移动后返回。

另含：

* 小程序“真正展开片段”与“摘要检查”逐项比较（状态与冲突）；
* 重复调用不复用上一次调用的 loan 身份；
* 局部 loan 在片段退出结束；移动/写回效果留给调用方；
* 嵌套片段、前置条件由调用方状态触发；
* 非法片段/调用（环、缺实参、形状不覆盖、declare 等）的可定位错误。
"""

import unittest

from borrow_check import (
    CFGValidationError,
    Fragment,
    Invoke,
    Param,
    Program,
    build_summaries,
    build_templates,
    check,
    check_program,
    instantiate,
    program_from_dict,
)
from borrow_check.fragments import run_invocation
from borrow_check.analyzer import _State


def keys(result):
    return {(c.kind, c.block, c.op) for c in result.conflicts}


# --------------------------------------------------------------------------
# 测试用固定片段
# --------------------------------------------------------------------------

# swap-like 片段：两个形参 p、q；分支0：双 mut 借 p.x / q.x（看别名），
# 分支1：读 p.x 后移动 q.x（看分支移动返回），分支2：访问 p.x 与 p.y。
DUAL_FRAG = {
    "name": "Dual",
    "params": [
        {"name": "p", "ty": {"x": None, "y": None}},
        {"name": "q", "ty": {"x": None, "y": None}},
    ],
    "blocks": [
        {"ops": []},
        # 分支 A：同字段双可变借用（都打在 p.x / q.x）。
        {"ops": [
            {"op": "borrow_mut", "loan_id": "lp", "path": ["p", "x"]},
            {"op": "borrow_mut", "loan_id": "lq", "path": ["q", "x"]},
            {"op": "end_loan", "loan_id": "lp"},
            {"op": "end_loan", "loan_id": "lq"},
        ]},
        # 分支 B：移动 q.x 后返回（移动效果留给调用方）。
        {"ops": [{"op": "move", "path": ["q", "x"]}]},
        # 分支 C：不同字段独立访问。
        {"ops": [
            {"op": "borrow_mut", "loan_id": "lx", "path": ["p", "x"]},
            {"op": "read", "path": ["p", "y"]},
            {"op": "end_loan", "loan_id": "lx"},
        ]},
    ],
    "successors": [[1, 2, 3], [], [], []],
}


def main_cfg(decl_ty, invoke_args):
    return {
        "cfg": {
            "blocks": [
                {"ops": [{"op": "declare", "var": "r", "ty": decl_ty},
                         {"op": "invoke", "fragment": "Dual",
                          "args": invoke_args}]},
                {"ops": [{"op": "read", "path": ["r", "x"]}]},
            ],
            "successors": [[1], []],
        },
        "fragments": [DUAL_FRAG],
    }


REC = {"x": None, "y": None}


class TestSameFieldDoubleMut(unittest.TestCase):
    def test_conflict_with_fragment_witness(self):
        # 两个形参都指向同一记录的同一字段（完全别名）。
        data = main_cfg(REC, {"p": ["r"], "q": ["r"]})
        prog = program_from_dict(data)
        res = check_program(prog)
        self.assertIn(("borrow_conflict", 0, 1), keys(res))
        c = next(c for c in res.conflicts if c.kind == "borrow_conflict")
        # 见证同时给调用位置与片段内路径：冲突点锚定 invoke (0,1)，
        # 先诞生的同字段借用见证的 frag_chain 指向片段内 (Dual,块1,op0)。
        self.assertEqual((c.block, c.op), (0, 1))
        ws = c.witnesses
        self.assertTrue(any(w.frag_chain and w.frag_chain[-1] == ("Dual", 1, 0)
                            for w in ws))
        # detail 文本中给出片段内冲突位置 (Dual,块1,op1)。
        self.assertIn("Dual[1.1]", c.detail)


class TestParentChildAlias(unittest.TestCase):
    def test_parent_child_overlap_conflicts(self):
        # p -> r（父），q -> r.x（子）：borrow_mut p.x 与 borrow_mut q.x 实际
        # 都作用在 r.x 上，冲突；且父形参借 p.x 与子形参 q（即 r.x）重叠。
        frag = {
            "name": "PC",
            "params": [
                {"name": "p", "ty": {"x": None}},
                {"name": "q", "ty": None},
            ],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p", "x"]},
                {"op": "borrow_mut", "loan_id": "b", "path": ["q"]},
                {"op": "end_loan", "loan_id": "a"},
                {"op": "end_loan", "loan_id": "b"},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": {"x": None}},
                {"op": "invoke", "fragment": "PC",
                 "args": {"p": ["r"], "q": ["r", "x"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        res = check_program(program_from_dict(data))
        self.assertIn(("borrow_conflict", 0, 1), keys(res))

    def test_disjoint_binding_of_same_fragment_is_clean(self):
        # 同一片段，形参绑定到互不相交的两个变量则干净。
        frag = {
            "name": "PC",
            "params": [
                {"name": "p", "ty": {"x": None}},
                {"name": "q", "ty": None},
            ],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p", "x"]},
                {"op": "borrow_mut", "loan_id": "b", "path": ["q"]},
                {"op": "end_loan", "loan_id": "a"},
                {"op": "end_loan", "loan_id": "b"},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": {"x": None}},
                {"op": "declare", "var": "s", "ty": None},
                {"op": "invoke", "fragment": "PC",
                 "args": {"p": ["r"], "q": ["s"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        self.assertTrue(check_program(program_from_dict(data)).clean)


class TestDistinctFields(unittest.TestCase):
    def test_different_fields_independent(self):
        # p -> r.x，q -> r.y：分支 A 两个 mut 借用落在不同字段，不冲突；
        # 分支 C 也只访问不相交字段。
        data = main_cfg(REC, {"p": ["r", "x"], "q": ["r", "y"]})
        # 但片段内路径是 p.x / q.x：实参 p=r.x 时 p.x=r.x.x 越界。
        # 故换一个形参为叶子形状的片段来表达“不同字段独立访问”。
        frag = {
            "name": "Ind",
            "params": [{"name": "p", "ty": None}, {"name": "q", "ty": None}],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p"]},
                {"op": "borrow_mut", "loan_id": "b", "path": ["q"]},
                {"op": "end_loan", "loan_id": "a"},
                {"op": "end_loan", "loan_id": "b"},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": REC},
                {"op": "invoke", "fragment": "Ind",
                 "args": {"p": ["r", "x"], "q": ["r", "y"]}},
                {"op": "read", "path": ["r", "x"]},
                {"op": "read", "path": ["r", "y"]},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        self.assertTrue(check_program(program_from_dict(data)).clean)


class TestBranchMoveThenReturn(unittest.TestCase):
    def test_move_effect_returns_to_caller(self):
        # 只走“移动 q.x”分支：调用后 r.x may 已移动。
        frag = {
            "name": "M",
            "params": [{"name": "q", "ty": {"x": None}}],
            "blocks": [{"ops": [{"op": "move", "path": ["q", "x"]}]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [
                {"ops": [
                    {"op": "declare", "var": "r", "ty": REC},
                    {"op": "invoke", "fragment": "M",
                     "args": {"q": ["r"]}},
                ]},
                {"ops": [{"op": "read", "path": ["r", "x"]}]},
                {"ops": [{"op": "read", "path": ["r", "y"]}]},
            ], "successors": [[1, 2], [], []]},
            "fragments": [frag],
        }
        res = check_program(program_from_dict(data))
        self.assertEqual(keys(res), {("use_of_moved", 1, 0)})
        c = res.conflicts[0]
        # 冲突点是调用之后的读取 (1,0)；移动见证诞生点锚定调用位置 (0,1)，
        # 并给出片段内路径 (M,0,0)。
        self.assertEqual((c.block, c.op), (1, 0))
        w = c.witnesses[0]
        self.assertEqual((w.origin_block, w.origin_op), (0, 1))
        self.assertEqual(w.frag_chain[-1], ("M", 0, 0))


# --------------------------------------------------------------------------
# 局部 loan 退出结束；前置条件由调用方状态触发
# --------------------------------------------------------------------------


class TestLocalLoansEndAtExit(unittest.TestCase):
    def test_local_loan_does_not_escape(self):
        frag = {
            "name": "B",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                # 借用后不 end：在片段内仍是活跃局部 loan，但退出即结束。
                {"op": "borrow_mut", "loan_id": "a", "path": ["p"]},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "B", "args": {"p": ["r"]}},
                {"op": "read", "path": ["r"]},  # 局部 loan 已结束：合法
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        self.assertTrue(check_program(program_from_dict(data)).clean)

    def test_caller_active_mut_loan_conflicts_inside(self):
        # 调用方持有 r 的活跃 mut 借用；片段内再借 r（经形参 p）必须冲突，
        # 且见证来自调用方（block_chain 指向调用块）。
        frag = {
            "name": "B",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p"]},
                {"op": "end_loan", "loan_id": "a"},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "borrow_mut", "loan_id": "OUT", "path": ["r"]},
                {"op": "invoke", "fragment": "B", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        res = check_program(program_from_dict(data))
        self.assertIn(("borrow_conflict", 0, 2), keys(res))
        c = next(c for c in res.conflicts if c.kind == "borrow_conflict")
        # 来自调用方的 loan 见证保留主 loan_id 与主块链。
        self.assertTrue(any(w.loan_id == "OUT" for w in c.witnesses))


# --------------------------------------------------------------------------
# 真正展开 vs 摘要/检查 逐项比较
# --------------------------------------------------------------------------


class TestExpansionEquivalence(unittest.TestCase):
    def _frag(self):
        # 含分支与局部 loan 的小片段。
        return {
            "name": "E",
            "params": [{"name": "p", "ty": {"x": None, "y": None}}],
            "blocks": [
                {"ops": [{"op": "borrow_mut", "loan_id": "m",
                          "path": ["p", "x"]}]},
                {"ops": [
                    {"op": "read", "path": ["p", "y"]},
                    {"op": "move", "path": ["p", "y"]},
                    {"op": "end_loan", "loan_id": "m"},
                ]},
                {"ops": [{"op": "write", "path": ["p", "y"], "reinit": True}]},
            ],
            "successors": [[1, 2], [], []],
        }

    def test_expanded_cfg_matches_summary_check(self):
        frag_dict = self._frag()
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": REC},
                {"op": "invoke", "fragment": "E", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [frag_dict],
        }
        prog = program_from_dict(data)

        # 1) 摘要（形参命名空间）。
        summaries = build_summaries(prog)
        summary = summaries["E"]
        # 摘要的前置条件覆盖三个分支块内每个读写借操作。
        op_names = [(pc.label[-1][1], pc.path) for pc in summary.preconditions]
        self.assertIn((0, ("p", "x")), op_names)
        self.assertIn((1, ("p", "y")), op_names)
        # 分支并集出口效果：p.y 可能被子树移动（块1），reinit 拆分后块2
        # 恢复 p.y；may 并集下 p.y 仍可能已移动。
        self.assertIn(("p", "y"), summary.effect_subtree)

        # 2) 真正展开：手工把片段内联成等价主 CFG（无 invoke）。
        from borrow_check import cfg_from_dict
        expanded = {
            "blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": REC},
                {"op": "borrow_mut", "loan_id": "m", "path": ["r", "x"]},
            ]}, {
                "ops": [
                    {"op": "read", "path": ["r", "y"]},
                    {"op": "move", "path": ["r", "y"]},
                    {"op": "end_loan", "loan_id": "m"},
                ],
            }, {"ops": [{"op": "write", "path": ["r", "y"], "reinit": True}]}],
            "successors": [[1, 2], [], []],
        }
        exp_res = check(cfg_from_dict(expanded))

        # 3) 片段调用版。
        inv_res = check_program(prog)

        # 4) 逐项比较冲突种类集合（调用版位置锚定 blocks[0].ops[1]，
        #    展开版位置在各内联块；按 kind 与具体路径比较）。
        exp_kind_path = {(c.kind, c.path) for c in exp_res.conflicts}
        inv_kind_path = {(c.kind, c.path) for c in inv_res.conflicts}
        self.assertEqual(exp_kind_path, inv_kind_path)

        # 5) 出口状态也一致：调用后 r.y may 已移动，r.x 未移动。
        from borrow_check.model import CFG as _CFG  # noqa
        tpl = build_templates(prog)["E"]
        inst = instantiate(tpl, {"p": ("r",)}, id_prefix="t")
        out, _ = run_invocation(inst, _State(),
                                {"r": REC}, {}, (0, 1))
        self.assertIn(("r", "y"), out.moved_subtree)
        self.assertNotIn(("r", "x"), out.moved_subtree)
        self.assertEqual(out.loans, {})  # 局部 loan 全部结束


class TestRepeatedCallsFreshIdentity(unittest.TestCase):
    def test_two_calls_do_not_share_loan_identity(self):
        frag = {
            "name": "R",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p"]},
                {"op": "end_loan", "loan_id": "a"},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "R", "args": {"p": ["r"]}},
                {"op": "invoke", "fragment": "R", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        prog = program_from_dict(data)
        tpl = build_templates(prog)["R"]
        i1 = instantiate(tpl, {"p": ("r",)}, id_prefix="call1")
        i2 = instantiate(tpl, {"p": ("r",)}, id_prefix="call2")
        ids1 = set(i1.fresh_loans)
        ids2 = set(i2.fresh_loans)
        self.assertTrue(ids1)
        self.assertTrue(ids2.isdisjoint(ids1))
        # 两次调用整体干净（loan 不串用、且都在片内结束）。
        self.assertTrue(check_program(prog).clean)

    def test_unended_loan_in_first_call_does_not_poison_second(self):
        # 片段不 end 局部 loan：退出时被强制结束，第二次调用不继承首次身份。
        frag = {
            "name": "R",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "a", "path": ["p"]},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "R", "args": {"p": ["r"]}},
                {"op": "invoke", "fragment": "R", "args": {"p": ["r"]}},
                {"op": "read", "path": ["r"]},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        self.assertTrue(check_program(program_from_dict(data)).clean)


# --------------------------------------------------------------------------
# 嵌套片段
# --------------------------------------------------------------------------


class TestNestedFragments(unittest.TestCase):
    def test_nested_call_label_chain(self):
        inner = {
            "name": "Inner",
            "params": [{"name": "z", "ty": None}],
            "blocks": [{"ops": [
                {"op": "borrow_mut", "loan_id": "i", "path": ["z"]},
                {"op": "borrow_mut", "loan_id": "j", "path": ["z"]},
                {"op": "end_loan", "loan_id": "i"},
                {"op": "end_loan", "loan_id": "j"},
            ]}],
            "successors": [[]],
        }
        outer = {
            "name": "Outer",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                {"op": "invoke", "fragment": "Inner",
                 "args": {"z": ["p"]}},
            ]}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "Outer",
                 "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [inner, outer],
        }
        res = check_program(program_from_dict(data))
        self.assertIn(("borrow_conflict", 0, 1), keys(res))
        c = res.conflicts[0]
        # label 链：调用位置 -> Outer 的 invoke -> Inner 内块操作。
        chains = [w.frag_chain for w in c.witnesses if w.frag_chain]
        self.assertTrue(any(chain[0][0] == "Outer" and chain[-1][0] == "Inner"
                            for chain in chains))


# --------------------------------------------------------------------------
# 非法输入
# --------------------------------------------------------------------------


class TestFragmentValidation(unittest.TestCase):
    def _base(self, frag, cfg_ops=None):
        return {
            "cfg": {"blocks": [{"ops": cfg_ops if cfg_ops is not None else [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "F", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }

    def test_fragment_cycle_rejected(self):
        frag = {
            "name": "F",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [
                {"op": "invoke", "fragment": "F", "args": {"p": ["p"]}},
            ]}],
            "successors": [[]],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(self._base(frag))

    def test_fragment_back_edge_rejected(self):
        frag = {
            "name": "F",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": []}, {"ops": []}],
            # 回边 1 -> 0 破坏无环。
            "successors": [[1], [0]],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(self._base(frag))

    def test_declare_inside_fragment_rejected(self):
        frag = {
            "name": "F",
            "params": [{"name": "p", "ty": None}],
            "blocks": [{"ops": [{"op": "declare", "var": "q", "ty": None}]}],
            "successors": [[]],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(self._base(frag))

    def test_missing_arg_rejected(self):
        frag = {
            "name": "F",
            "params": [{"name": "p", "ty": None}, {"name": "q", "ty": None}],
            "blocks": [{"ops": []}],
            "successors": [[]],
        }
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "F", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [frag],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(data)

    def test_shape_not_covered_rejected(self):
        # 形参要求字段 x，但实参 r 是叶子。
        frag = {
            "name": "F",
            "params": [{"name": "p", "ty": {"x": None}}],
            "blocks": [{"ops": [{"op": "read", "path": ["p", "x"]}]}],
            "successors": [[]],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(self._base(frag))

    def test_invoke_in_plain_cfg_rejected(self):
        from borrow_check import cfg_from_dict
        with self.assertRaises(CFGValidationError):
            cfg_from_dict({"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "F", "args": {"p": ["r"]}},
            ]}], "successors": [[]]})

    def test_unknown_fragment_rejected(self):
        data = {
            "cfg": {"blocks": [{"ops": [
                {"op": "declare", "var": "r", "ty": None},
                {"op": "invoke", "fragment": "Nope", "args": {"p": ["r"]}},
            ]}], "successors": [[]]},
            "fragments": [],
        }
        with self.assertRaises(CFGValidationError):
            program_from_dict(data)


if __name__ == "__main__":
    unittest.main()
