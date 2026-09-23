"""8 条件完整穷举对照、证据可执行性校验与主规模非退化用例。"""

import time
import random
import unittest

from borrow_check import cfg_from_dict, check, MAX_BLOCKS_LIFECYCLE
from borrow_check.formula import (
    evaluate as eval_expr, expr_from_dict)
from tests._reference_lifecycle import reference_lifecycle_conflicts
from tests.ext_lifecycle_reference import generate_cfg


def evidence_executable(cfg_dict, ev):
    """独立复核证据：块游走中每条边守卫在证据赋值下为真。"""
    walk = ev.walk
    succs = cfg_dict["successors"]
    for a, b in zip(walk, walk[1:]):
        guard = None
        for e in succs[a]:
            target = e if isinstance(e, int) else e["block"]
            if target == b:
                guard = None if isinstance(e, int) else e["when"]
                break
        if guard is not None and not eval_expr(
                expr_from_dict(guard, f"edge {a}->{b}"), ev.assignment):
            return False
    return ev.reproduced


class TestEightConditionExhaustive(unittest.TestCase):
    def test_full_256_assignment_crosscheck(self):
        rng = random.Random(2026092408)
        trials = 12
        for t in range(trials):
            data = generate_cfg(rng, n_max=5, k_max=8)
            cfg = cfg_from_dict(data, max_blocks=64)
            result = check(cfg, lifecycle=True)
            ref = reference_lifecycle_conflicts(data, unroll=4)
            core = {(c.kind, c.block, c.op) for c in result.conflicts}
            self.assertEqual(core, set(ref),
                             f"trial {t}: diff={core ^ set(ref)}\n{data}")
            names = data["conditions"]
            for ev in result.evidence:
                row = sum((1 << i) for i, name in enumerate(names)
                          if ev.assignment[name])
                self.assertIn(row, ref[(ev.kind, ev.block, ev.op)])
                self.assertTrue(evidence_executable(data, ev))

    def test_explicit_eight_condition_program(self):
        conds = ["p", "q", "r", "s", "t", "u", "v", "w"]
        move_when = {"and": ["p", "q", "r", "s",
                             {"not": "t"}, {"not": "u"},
                             {"not": "v"}, {"not": "w"}]}
        data = {
            "conditions": conds,
            "blocks": [
                {"ops": [{"op": "declare", "var": "a", "ty": {"x": None}}]},
                {"ops": [{"op": "move", "path": ["a", "x"]}]},
                {"ops": []},
                {"ops": [{"op": "read", "path": ["a", "x"]}]},
            ],
            "successors": [
                [{"block": 1, "when": move_when},
                 {"block": 2, "when": {"not": move_when}}],
                [3], [3], []],
        }
        r = check(cfg_from_dict(data, max_blocks=64), lifecycle=True)
        self.assertEqual({(c.kind, c.block, c.op) for c in r.conflicts},
                         {("use_of_moved", 3, 0)})
        ev = r.evidence[0]
        self.assertEqual(ev.assignment,
                         {"p": True, "q": True, "r": True, "s": True,
                          "t": False, "u": False, "v": False, "w": False})
        self.assertEqual(ev.walk, (0, 1, 3))
        self.assertEqual(set(reference_lifecycle_conflicts(data)),
                         {("use_of_moved", 3, 0)})


class TestEvidenceExecutable(unittest.TestCase):
    def test_every_evidence_is_independently_executable(self):
        rng = random.Random(99)
        for _ in range(25):
            data = generate_cfg(rng, n_max=6, k_max=2)
            cfg = cfg_from_dict(data, max_blocks=64)
            r = check(cfg, lifecycle=True)
            for ev in r.evidence:
                self.assertTrue(evidence_executable(data, ev))
                self.assertEqual((ev.steps[-1].block, ev.steps[-1].op),
                                 (ev.block, ev.op))


def build_scale_program():
    """64 块、每变量 16 字段的非退化程序（全部 64 块可达）。

    * 块0：声明 v、w（各 16 个叶子字段）；
    * 块1..27：线性链，奇数块两阶段借用（reserve/read/activate/end 闭合），
      偶数块部分移动 + reinit 恢复；
    * 块28 按 c0 菱形分叉 29/30（两分支操作不相交字段），块31 汇合；
    * 块31..60：线性链继续两阶段借用；
    * 块61：循环体（c1 回边，每轮借用闭合），!c1 到块62；
    * 块62 读取；块63 词法析构（逆声明顺序，无借用，干净）。
    """
    fields = {f"f{i:02d}": None for i in range(16)}
    n = 64
    blocks: list = [
        [{"op": "declare", "var": "v", "ty": dict(fields)},
         {"op": "declare", "var": "w", "ty": dict(fields)}]]
    blocks += [[] for _ in range(n - 1)]
    succs: list = [[] for _ in range(n)]

    for i in range(1, 28):
        succs[i - 1].append(i)
        f1 = f"f{i % 16:02d}"
        f2 = f"f{(i + 1) % 16:02d}"
        lid = f"R{i:02d}"
        if i % 2 == 1:
            blocks[i] = [
                {"op": "borrow_reserve", "loan_id": lid,
                 "path": ["v", f1]},
                {"op": "read", "path": ["v", f1]},
                {"op": "borrow_activate", "loan_id": lid},
                {"op": "end_loan", "loan_id": lid},
            ]
        else:
            blocks[i] = [
                {"op": "move", "path": ["v", f1]},
                {"op": "read", "path": ["v", f2]},
                {"op": "write", "path": ["v", f1], "reinit": True},
            ]

    succs[27] = [28]
    succs[28] = [{"block": 29, "when": "c0"},
                 {"block": 30, "when": {"not": "c0"}}]
    blocks[29] = [
        {"op": "borrow_reserve", "loan_id": "RT", "path": ["v", "f00"]},
        {"op": "read", "path": ["v", "f00"]},
        {"op": "borrow_activate", "loan_id": "RT"},
        {"op": "end_loan", "loan_id": "RT"},
    ]
    blocks[30] = [
        {"op": "move", "path": ["v", "f15"]},
        {"op": "write", "path": ["v", "f15"], "reinit": True},
    ]
    succs[29] = [31]
    succs[30] = [31]

    for i in range(32, 61):
        succs[i - 1].append(i)
        f1 = f"f{i % 16:02d}"
        lid = f"R{i:02d}"
        blocks[i] = [
            {"op": "borrow_reserve", "loan_id": lid, "path": ["v", f1]},
            {"op": "borrow_activate", "loan_id": lid},
            {"op": "end_loan", "loan_id": lid},
        ]
    # 块31 自身的操作（菱形汇合后的第一个两阶段借用块）。
    blocks[31] = [
        {"op": "borrow_reserve", "loan_id": "R31", "path": ["v", "f15"]},
        {"op": "borrow_activate", "loan_id": "R31"},
        {"op": "end_loan", "loan_id": "R31"},
    ]

    succs[60] = [61]
    blocks[61] = [
        {"op": "borrow_reserve", "loan_id": "RL", "path": ["v", "f00"]},
        {"op": "read", "path": ["v", "f00"]},
        {"op": "borrow_activate", "loan_id": "RL"},
        {"op": "end_loan", "loan_id": "RL"},
    ]
    succs[61] = [{"block": 61, "when": "c1"},
                 {"block": 62, "when": {"not": "c1"}}]
    blocks[62] = [{"op": "read", "path": ["v", "f00"]}]
    succs[62] = [63]
    blocks[63] = [{"op": "drop"}]

    return {
        "blocks": [{"ops": ops} for ops in blocks],
        "successors": succs,
        "entry": 0,
        "conditions": ["c0", "c1"],
    }


class TestScale(unittest.TestCase):
    def test_sixty_four_blocks_sixteen_fields(self):
        data = build_scale_program()
        self.assertEqual(len(data["blocks"]), 64)
        cfg = cfg_from_dict(data, max_blocks=MAX_BLOCKS_LIFECYCLE)
        start = time.perf_counter()
        r = check(cfg, lifecycle=True)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 30.0, f"规模用例耗时 {elapsed:.1f}s")
        self.assertEqual(r.status, "ok", r.explain_evidence())
        self.assertGreater(r.iterations, 64)

    def test_scale_determinism(self):
        data = build_scale_program()
        r1 = check(cfg_from_dict(data, max_blocks=64), lifecycle=True)
        r2 = check(cfg_from_dict(data, max_blocks=64), lifecycle=True)
        self.assertEqual(r1.status, r2.status)
        self.assertEqual(
            [(c.kind, c.block, c.op) for c in r1.conflicts],
            [(c.kind, c.block, c.op) for c in r2.conflicts])


if __name__ == "__main__":
    unittest.main()
