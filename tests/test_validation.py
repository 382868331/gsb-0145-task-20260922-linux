"""Validation, error semantics, budgets and witness integrity tests."""

import math
import unittest

from borrowcheck import (
    Block,
    BorrowMut,
    BorrowShared,
    BudgetExceededError,
    CFG,
    CFGValidationError,
    ConflictCode,
    Declare,
    EndLoan,
    Location,
    Move,
    Read,
    Write,
    check,
)
from borrowcheck.validation import validate_nonneg_int

TYPES = {"a": {"x": None, "y": None}}


def cfg(blocks, entry="b0", types=None):
    return CFG(blocks=blocks, entry=entry, types=types or TYPES)


MINIMAL = (Block("b0", (Declare("a"),), ()),)


class ValidationTests(unittest.TestCase):
    def _expect(self, ctor, fragment=None):
        with self.assertRaises(CFGValidationError) as cm:
            check(ctor())
        if fragment is not None:
            self.assertIn(fragment, str(cm.exception))
        return cm.exception

    def test_not_a_cfg(self):
        err = self._expect(lambda: 42)
        self.assertIsNotNone(err.location)

    def test_wrong_block_container_type(self):
        bad = CFG(blocks=Block("b0", (), ()), entry="b0", types={})
        self._expect(lambda: bad)

    def test_empty_blocks(self):
        self._expect(
            lambda: CFG(blocks=(), entry="b0", types={}), "at least one"
        )

    def test_too_many_blocks(self):
        blocks = [Block("b0", (), ("b1",))]
        blocks += [Block(f"b{i}", (), ()) for i in range(1, 51)]
        # 51 blocks total
        self.assertEqual(len(blocks), 51)
        err = self._expect(
            lambda: CFG(blocks=blocks, entry="b0", types=TYPES), "limit"
        )
        self.assertIn("50", str(err))

    def test_duplicate_block_id(self):
        self._expect(
            lambda: cfg(
                [Block("b0", (), ("b1",)), Block("b0", (), ())]
            ),
            "duplicate block",
        )

    def test_missing_entry(self):
        self._expect(
            lambda: cfg([Block("b0", (), ())], entry="zz"),
            "is not among the blocks",
        )

    def test_dangling_successor(self):
        self._expect(
            lambda: cfg([Block("b0", (), ("zz",))]),
            "does not name a block",
        )

    def test_duplicate_successor(self):
        self._expect(
            lambda: cfg(
                [
                    Block("b0", (), ("b1", "b1")),
                    Block("b1", (), ()),
                ]
            ),
            "duplicate successor",
        )

    def test_edge_into_entry_rejected(self):
        err = self._expect(
            lambda: cfg(
                [
                    Block("b0", (), ("b1",)),
                    Block("b1", (), ("b0",)),
                ]
            ),
            "entry block",
        )
        self.assertEqual(err.location, "block 'b1' -> successors")
        self.assertIn("b0", err.message)

    def test_unknown_field_in_path(self):
        err = self._expect(
            lambda: cfg([Block("b0", (Declare("a"), Read(("a", "zz"))), ())]),
            "does not exist in the type tree",
        )
        self.assertIn("path[1]", err.location)

    def test_empty_path(self):
        self._expect(
            lambda: cfg([Block("b0", (Declare("a"), Read(())), ())]),
            "at least a root",
        )

    def test_path_component_not_str(self):
        err = self._expect(
            lambda: cfg(
                [Block("b0", (Declare("a"), Read(("a", 7))), ())]
            ),
            "must be a str",
        )
        self.assertIn("path[1]", err.location)

    def test_unknown_type_root_on_declare(self):
        self._expect(
            lambda: CFG(
                blocks=[Block("b0", (Declare("zz"),), ())],
                entry="b0",
                types={},
            ),
            "no type tree entry",
        )

    def test_redeclare_on_separate_branches_is_allowed(self):
        # declare is idempotent; one declare per branch arm must validate.
        result = check(
            cfg(
                [
                    Block("b0", (), ("b1", "b2")),
                    Block("b1", (Declare("a"),), ("b3",)),
                    Block("b2", (Declare("a"),), ("b3",)),
                    Block("b3", (Read(("a", "x")),), ()),
                ]
            )
        )
        self.assertTrue(result.ok)

    def test_duplicate_loan_id(self):
        err = self._expect(
            lambda: cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            BorrowMut("L1", ("a", "x")),
                            BorrowShared("L1", ("a", "y")),
                        ),
                        (),
                    )
                ]
            ),
            "statically unique",
        )
        self.assertIn("instruction 2", err.location)

    def test_end_unknown_loan(self):
        err = self._expect(
            lambda: cfg(
                [Block("b0", (Declare("a"), EndLoan("nope")), ())]
            ),
            "unknown loan_id",
        )
        self.assertIn("end_loan", err.location)

    def test_bad_instruction_object(self):
        err = self._expect(
            lambda: cfg([Block("b0", ("read a",), ())]),
            "must be one of",
        )
        self.assertIn("instruction 0", err.location)

    def test_bad_type_tree_node(self):
        self._expect(
            lambda: CFG(
                blocks=[Block("b0", (), ())],
                entry="b0",
                types={"a": 7},
            ),
            "dict of fields or None",
        )

    def test_numeric_arguments_reject_nan_infinity_bool(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(CFGValidationError):
                validate_nonneg_int(bad, "budget", "check")
        with self.assertRaises(CFGValidationError) as cm:
            validate_nonneg_int(True, "budget", "check")
        self.assertIn("bool rejected", str(cm.exception))
        for bad in (1.5, "3", None):
            with self.assertRaises(CFGValidationError):
                validate_nonneg_int(bad, "budget", "check")
        self.assertEqual(validate_nonneg_int(0, "budget", "check"), 0)
        self.assertEqual(validate_nonneg_int(5, "budget", "check"), 5)
        with self.assertRaises(CFGValidationError):
            validate_nonneg_int(-1, "budget", "check")

    def test_check_budget_rejects_bool_and_float(self):
        good = cfg([Block("b0", (Declare("a"),), ())])
        for bad in (True, 1.0, math.nan, math.inf, 0, -2):
            with self.assertRaises(CFGValidationError):
                check(good, budget=bad)

    def test_location_trail_string(self):
        err = self._expect(
            lambda: cfg(
                [Block("b0", (Declare("a"), BorrowMut("", ("a", "x"))), ())]
            ),
            "non-empty",
        )
        self.assertIn("block 'b0'", err.location)
        self.assertIn("borrow_mut", err.location)


class BudgetSemanticsTests(unittest.TestCase):
    def test_tiny_budget_is_distinct_from_proof(self):
        # A loop header reachable from the entry (no edge into entry).
        program = cfg(
            [
                Block("b0", (Declare("a"),), ("b1",)),
                Block("b1", (), ("b1",)),
            ]
        )
        with self.assertRaises(BudgetExceededError) as cm:
            check(program, budget=1)
        self.assertGreaterEqual(cm.exception.iterations, 1)
        self.assertEqual(cm.exception.budget, 1)
        self.assertIn("incomplete", str(cm.exception))

    def test_default_budget_always_converges(self):
        program = cfg(
            [
                Block("b0", (Declare("a"), BorrowMut("L", ("a", "x"))), ("b1",)),
                Block("b1", (Read(("a", "x")),), ("b2",)),
                Block("b2", (), ("b1", "b3")),
                Block("b3", (EndLoan("L"), Read(("a", "x"))), ()),
            ]
        )
        result = check(program)
        self.assertIsInstance(result.iterations, int)
        self.assertGreater(result.iterations, 0)


class WitnessIntegrityTests(unittest.TestCase):
    def test_witness_chain_is_contiguous_and_grounded(self):
        program = cfg(
            [
                Block("b0", (Declare("a"),), ("b1", "b2")),
                Block("b1", (Move(("a", "x")),), ("b3",)),
                Block("b2", (), ("b3",)),
                Block("b3", (Read(("a", "x")),), ("b4",)),
                Block("b4", (), ()),
            ]
        )
        result = check(program)
        conflict = next(
            c
            for c in result.conflicts
            if c.code == ConflictCode.USE_OF_MOVED
        )
        steps = conflict.witness.steps
        self.assertEqual(steps[0].kind, "origin")
        self.assertIsInstance(steps[0].location, Location)
        # origin instruction is really a Move of the against path
        origin_block = next(
            b for b in program.blocks if b.id == steps[0].location.block_id
        )
        origin_instr = origin_block.instructions[steps[0].location.instruction_index]
        self.assertIsInstance(origin_instr, Move)
        self.assertEqual(origin_instr.path, conflict.against_path)

        # edge steps must name real successor edges in order
        edge_steps = [s for s in steps if s.kind == "edge"]
        self.assertTrue(edge_steps)
        succ_map = {b.id: set(b.successors) for b in program.blocks}
        for step in edge_steps:
            self.assertIn(step.next_block, succ_map[step.block_id])
        # the chain ends in the block containing the conflict
        self.assertEqual(
            steps[-1].block_id, conflict.location.block_id
        )

    def test_loan_witness_names_real_borrow(self):
        program = cfg(
            [
                Block(
                    "b0",
                    (
                        Declare("a"),
                        BorrowShared("S", ("a", "x")),
                        BorrowMut("M", ("a", "x")),
                    ),
                    (),
                )
            ]
        )
        conflict = result_conflict(check(program), ConflictCode.LOAN_CONFLICT)
        first = conflict.witness.steps[0]
        self.assertEqual(first.location, Location("b0", 1))

    def test_end_inactive_witness_is_grounded_at_entry(self):
        program = cfg(
            [
                Block("b0", (Declare("a"), EndLoan("L1")), ("b1",)),
                Block(
                    "b1",
                    (BorrowMut("L1", ("a", "x")), EndLoan("L1")),
                    (),
                ),
            ]
        )
        conflict = result_conflict(check(program), ConflictCode.END_INACTIVE_LOAN)
        self.assertEqual(conflict.witness.steps[0].kind, "entry")

    def test_no_traversal_depth_cutoff_long_back_edge_chain(self):
        # 50 blocks: long straight run feeding a loop; witness must still
        # reach the concrete loan origin regardless of chain length.
        types = {"a": {"x": None}}
        blocks = [
            Block("b0", (Declare("a"), BorrowMut("L", ("a", "x"))), ("b1",))
        ]
        for i in range(1, 48):
            blocks.append(Block(f"b{i}", (), (f"b{i+1}",)))
        blocks.append(Block("b48", (Read(("a", "x")),), ("b49",)))
        blocks.append(Block("b49", (Read(("a", "x")),), ("b48",)))
        result = check(CFG(blocks=blocks, entry="b0", types=types))
        loan_conflicts = [
            c
            for c in result.conflicts
            if c.code == ConflictCode.READ_WHILE_MUT_LOAN
        ]
        self.assertTrue(loan_conflicts)
        tail = next(c for c in loan_conflicts if c.location.block_id == "b48")
        self.assertEqual(tail.witness.steps[0].location.block_id, "b0")
        edge_blocks = [
            s.block_id for s in tail.witness.steps if s.kind == "edge"
        ]
        # no truncation: chain must cross all intermediate blocks
        self.assertIn("b47", edge_blocks)


class ResultImmutabilityTests(unittest.TestCase):
    def test_cfg_input_not_mutated(self):
        instrs = (Declare("a"), Move(("a", "x")), Read(("a", "y")))
        block = Block("b0", instrs, ())
        program = cfg([block])
        check(program)
        self.assertIs(block.instructions, instrs)
        self.assertEqual(block.successors, ())
        self.assertIs(program.blocks[0], block)

    def test_conflict_result_fields(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (Declare("a"), BorrowMut("L", ("a", "x")), Read(("a", "x"))),
                        (),
                    )
                ]
            )
        )
        self.assertFalse(result.ok)
        c = result.conflicts[0]
        with self.assertRaises(Exception):
            c.location = Location("z", 0)  # type: ignore[misc]
        self.assertIn("b0", result.facts)
        self.assertIn("moved", dir(result.facts["b0"].entry))


def result_conflict(result, code):
    return next(c for c in result.conflicts if c.code == code)


if __name__ == "__main__":
    unittest.main()
