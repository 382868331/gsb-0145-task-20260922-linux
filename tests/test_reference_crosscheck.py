"""Cross-check the core against the independent reference oracle.

The expected conflict sets come from ``tests.reference`` (explicit-state
reachability and, for DAGs, full path enumeration), which never imports
the core under test.  Programs are generated deterministically by a
seeded generator; no external corpus is used.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from borrowcheck import (  # noqa: E402
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    Declare,
    EndLoan,
    Move,
    Read,
    Write,
    check,
)
from reference import enumerated_path_conflicts, explicit_state_conflicts  # noqa: E402

# A small fixed record tree: paths are drawn from what exists here, so
# generated programs always validate (the validation layer itself is
# fuzzed separately).
TREE = {
    "a": {"x": None, "y": {"z": None, "w": None}},
    "b": {"left": {"v": None}, "right": {"v": None}},
}
PATHS = [
    ("a",),
    ("a", "x"),
    ("a", "y"),
    ("a", "y", "z"),
    ("a", "y", "w"),
    ("b",),
    ("b", "left"),
    ("b", "left", "v"),
    ("b", "right"),
    ("b", "right", "v"),
]
ROOTS = ["a", "b"]

KINDS = (
    "declare",
    "move",
    "shared",
    "mut",
    "end",
    "read",
    "write",
)


def generate_program(rng, n_blocks, allow_cycles):
    """Build a valid random CFG with forward (and maybe back) edges."""
    ids = [f"b{i}" for i in range(n_blocks)]
    blocks = []
    pending_loans: dict[str, str] = {}  # loan_id -> "shared"|"mut"
    loan_counter = 0
    declared = {"a": False, "b": False}

    for i, bid in enumerate(ids):
        instrs = []
        n_instr = rng.randrange(0, 6)
        for _ in range(n_instr):
            kind = rng.choice(KINDS)
            # ensure declarations exist before roots are used on *some*
            # path most of the time, but not always (undeclared cases are
            # wanted too)
            if not any(declared.values()) and kind != "declare":
                root = rng.choice(ROOTS)
                instrs.append(Declare(root))
                declared[root] = True
            if kind == "declare":
                choices = [r for r in ROOTS if not declared[r]] or ROOTS
                root = rng.choice(choices)
                if not declared[root]:
                    instrs.append(Declare(root))
                    declared[root] = True
            elif kind in ("move", "read", "write"):
                cls = {"move": Move, "read": Read, "write": Write}[kind]
                instrs.append(cls(rng.choice(PATHS)))
            elif kind in ("shared", "mut"):
                loan_counter += 1
                lid = f"L{loan_counter}"
                cls = BorrowShared if kind == "shared" else BorrowMut
                instrs.append(cls(lid, rng.choice(PATHS)))
                pending_loans[lid] = kind
            elif kind == "end":
                if pending_loans and rng.random() < 0.8:
                    lid = rng.choice(list(pending_loans))
                    instrs.append(EndLoan(lid))
                    pending_loans.pop(lid, None)
                # otherwise skip: never invent unknown loan ids
        succ = []
        if i + 1 < n_blocks and rng.random() < 0.8:
            succ.append(ids[i + 1])
        # branch to a later block
        if i + 2 < n_blocks and rng.random() < 0.4:
            succ.append(ids[rng.randrange(i + 2, n_blocks)])
        if allow_cycles and i > 1 and rng.random() < 0.3:
            # back edge, never into entry b0
            succ.append(ids[rng.randrange(1, i)])
        if not succ and i + 1 == n_blocks:
            succ = []
        blocks.append(Block(bid, tuple(instrs), tuple(succ)))

    # guarantee at least one declaration somewhere so programs are
    # interesting rather than uniformly undeclared
    first = blocks[0]
    if not any(ins.kind == "declare" for ins in first.instructions):
        blocks[0] = Block(
            first.id, (Declare("a"),) + first.instructions, first.successors
        )
    return CFG(blocks=blocks, entry=ids[0], types=TREE)


def core_signatures(result):
    sigs = set()
    for c in result.conflicts:
        sigs.add(
            (
                c.location.block_id,
                c.location.instruction_index,
                c.code.value,
                c.path,
                c.loan_id,
                c.against_path,
            )
        )
    return sigs


class ReferenceCrossCheckTests(unittest.TestCase):
    def test_dag_agrees_with_both_oracles_many_seeds(self):
        for seed in range(120):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                n = rng.randrange(1, 9)
                program = generate_program(rng, n, allow_cycles=False)
                result = check(program)
                expected_enum = enumerated_path_conflicts(program)
                expected_explicit = explicit_state_conflicts(program)
                got = core_signatures(result)
                self.assertEqual(
                    got,
                    expected_enum,
                    msg=f"seed {seed}: core != path-enumeration oracle",
                )
                self.assertEqual(
                    got,
                    expected_explicit,
                    msg=f"seed {seed}: core != explicit-state oracle",
                )

    def test_cyclic_graphs_agree_with_explicit_state_oracle(self):
        for seed in range(120, 220):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                n = rng.randrange(2, 9)
                program = generate_program(rng, n, allow_cycles=True)
                result = check(program)
                expected = explicit_state_conflicts(program)
                got = core_signatures(result)
                self.assertEqual(
                    got,
                    expected,
                    msg=f"seed {seed}: core != explicit-state oracle on CFG "
                    "with cycles",
                )

    def test_hand_built_diamond_path_count(self):
        # Tiny DAG whose path count is easy to reason about:
        # b0 -> (b1,b2) -> b3 -> b4 ; 2 root-to-merge paths.
        program = CFG(
            blocks=[
                Block("b0", (Declare("a"),), ("b1", "b2")),
                Block("b1", (Move(("a", "x")),), ("b3",)),
                Block("b2", (BorrowMut("M1", ("a", "y")),), ("b3",)),
                Block(
                    "b3",
                    (Read(("a", "x")), Read(("a", "y"))),
                    ("b4",),
                ),
                Block("b4", (EndLoan("M1"), Read(("a", "y"))), ()),
            ],
            entry="b0",
            types=TREE,
        )
        result = check(program)
        expected = enumerated_path_conflicts(program)
        self.assertEqual(core_signatures(result), expected)
        # Hand expectation: read a.x may hit the moved branch; read a.y
        # may hit the mut-loan branch; after end_loan in b4 (loan active on
        # the b2 path) a.y is clean.
        codes = sorted((c.location.block_id, c.code.value) for c in result.conflicts)
        self.assertIn(("b3", "use_of_moved"), codes)
        self.assertIn(("b3", "read_while_mut_loan"), codes)
        self.assertNotIn(("b4", "read_while_mut_loan"), codes)


if __name__ == "__main__":
    unittest.main()
