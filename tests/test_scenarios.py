"""Required acceptance scenarios with hand-specified expectations."""

import unittest

from borrowcheck import (
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    ConflictCode,
    Declare,
    EndLoan,
    Move,
    Read,
    Write,
    check,
)

TYPES = {
    "a": {"x": None, "y": None},
    "b": {"left": {"v": None}, "right": {"v": None}},
}


def cfg(blocks, entry="b0", types=None):
    return CFG(blocks=blocks, entry=entry, types=types or TYPES)


def codes(result):
    return sorted(c.code for c in result.conflicts)


class DisjointFieldTests(unittest.TestCase):
    def test_sibling_fields_independent_mut_loans(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            BorrowMut("L1", ("a", "x")),
                            BorrowMut("L2", ("a", "y")),
                            EndLoan("L2"),
                            Read(("a", "y")),
                            Write(("a", "y")),
                            EndLoan("L1"),
                            Read(("a", "x")),
                            Read(("a", "y")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])

    def test_deep_disjoint_branches(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("b"),
                            BorrowMut("L", ("b", "left", "v")),
                            Read(("b", "right", "v")),
                            Write(("b", "right")),
                            EndLoan("L"),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertTrue(result.ok)

    def test_parent_child_loans_conflict(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            BorrowShared("S", ("a",)),
                            BorrowMut("M", ("a", "x")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.LOAN_CONFLICT])
        c = result.conflicts[0]
        self.assertEqual(c.loan_id, "S")
        self.assertEqual(c.path, ("a", "x"))

    def test_shared_overlap_with_write_conflicts_but_reads_ok(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            BorrowShared("S", ("a", "y")),
                            Read(("a", "y")),
                            Write(("a", "y")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.WRITE_WHILE_LOAN_ACTIVE])


class LoopLoanTests(unittest.TestCase):
    def test_unended_loan_in_loop_body(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (Declare("a"), BorrowMut("L1", ("a", "x"))),
                        ("b1",),
                    ),
                    Block("b1", (Read(("a", "x")),), ("b1",)),
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.READ_WHILE_MUT_LOAN])
        c = result.conflicts[0]
        self.assertEqual(c.location.block_id, "b1")
        # witness must trace back across the loop edge to the origin
        blocks_on_chain = {s.block_id for s in c.witness.steps}
        self.assertIn("b0", blocks_on_chain)
        self.assertIn("b1", blocks_on_chain)
        self.assertEqual(c.witness.steps[0].kind, "origin")

    def test_loan_ended_before_loop_is_clean(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            BorrowMut("L1", ("a", "x")),
                            EndLoan("L1"),
                        ),
                        ("b1",),
                    ),
                    Block("b1", (Read(("a", "x")),), ("b1",)),
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])

    def test_loan_active_on_one_loop_path_is_may_active(self):
        # b1 branches: one arm borrows and ends, other arm does nothing;
        # back-edge merge must keep the loan may-active only while it can
        # actually be live; here end_loan in the merge is legal only if the
        # loan is active on *some* path (it is, on first iteration) -> the
        # checker must not report end_inactive, but reads in the merge
        # after the end must be clean.
        result = check(
            cfg(
                [
                    Block("b0", (Declare("a"),), ("b1",)),
                    Block("b1", (), ("b2", "b3")),
                    Block(
                        "b2",
                        (BorrowMut("L1", ("a", "x")), EndLoan("L1")),
                        ("b3",),
                    ),
                    Block("b3", (Read(("a", "x")),), ("b1",)),
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])


class BranchMoveTests(unittest.TestCase):
    def test_move_in_one_branch_then_read_at_merge(self):
        result = check(
            cfg(
                [
                    Block("b0", (Declare("a"),), ("b1", "b2")),
                    Block("b1", (Move(("a", "x")),), ("b3",)),
                    Block("b2", (), ("b3",)),
                    Block("b3", (Read(("a", "x")),), ()),
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.USE_OF_MOVED])
        c = result.conflicts[0]
        self.assertEqual(c.location.block_id, "b3")
        self.assertEqual(c.against_path, ("a", "x"))
        # witness origin is the move in b1, chain crosses an edge to b3
        kinds = [s.kind for s in c.witness.steps]
        self.assertEqual(kinds[0], "origin")
        self.assertIn("edge", kinds)
        edge = next(s for s in c.witness.steps if s.kind == "edge")
        self.assertEqual((edge.block_id, edge.next_block), ("b1", "b3"))

    def test_move_both_branches_then_write_restores(self):
        result = check(
            cfg(
                [
                    Block("b0", (Declare("a"),), ("b1", "b2")),
                    Block("b1", (Move(("a", "x")),), ("b3",)),
                    Block("b2", (Move(("a", "x")),), ("b3",)),
                    Block("b3", (Write(("a", "x")), Read(("a", "x"))), ()),
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])

    def test_move_sibling_field_does_not_block_other_field(self):
        result = check(
            cfg(
                [
                    Block("b0", (Declare("a"),), ("b1", "b2")),
                    Block("b1", (Move(("a", "x")),), ("b3",)),
                    Block("b2", (), ("b3",)),
                    Block("b3", (Read(("a", "y")),), ()),
                ]
            )
        )
        self.assertTrue(result.ok)


class ParentReassemblyTests(unittest.TestCase):
    def test_move_parent_kills_children_then_reassign_parent(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            Move(("a",)),
                            Read(("a", "x")),
                            Write(("a",)),
                            Read(("a", "x")),
                            Read(("a", "y")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.USE_OF_MOVED])
        self.assertEqual(result.conflicts[0].location.instruction_index, 2)

    def test_field_write_under_moved_parent_is_rejected(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (Declare("a"), Move(("a",)), Write(("a", "x"))),
                        (),
                    )
                ]
            )
        )
        self.assertEqual(
            codes(result), [ConflictCode.WRITE_TO_FIELD_OF_MOVED_PARENT]
        )

    def test_partial_move_reassign_field_restores_only_field(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            Move(("a", "x")),
                            Write(("a", "x")),
                            Read(("a", "x")),
                            Read(("a", "y")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])

    def test_partial_move_of_descendant_invalidates_parent_use(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (Declare("a"), Move(("a", "x")), Move(("a",))),
                        (),
                    )
                ]
            )
        )
        # moving the whole parent while a child is partially moved is a use
        # through a path overlapped by the moved descendant
        self.assertEqual(codes(result), [ConflictCode.USE_OF_MOVED])
        self.assertEqual(result.conflicts[0].against_path, ("a", "x"))


class DeclarationMergeTests(unittest.TestCase):
    def test_declared_on_one_branch_only_is_may_undeclared_at_merge(self):
        result = check(
            cfg(
                [
                    Block("b0", (), ("b1", "b2")),
                    Block("b1", (Declare("a"),), ("b3",)),
                    Block("b2", (), ("b3",)),
                    Block("b3", (Read(("a", "x")),), ()),
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.USE_BEFORE_DECLARE])
        self.assertEqual(result.conflicts[0].witness.steps[0].kind, "entry")

    def test_declared_on_both_branches_is_fine(self):
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
        self.assertTrue(result.ok, [c.message for c in result.conflicts])


class MayMovedRecoveryTests(unittest.TestCase):
    def test_write_after_may_moved_merge_restores_field(self):
        result = check(
            cfg(
                [
                    Block("b0", (Declare("a"),), ("b1", "b2")),
                    Block("b1", (Move(("a", "x")),), ("b3",)),
                    Block("b2", (), ("b3",)),
                    Block("b3", (Write(("a", "x")), Read(("a", "x"))), ()),
                ]
            )
        )
        self.assertTrue(result.ok, [c.message for c in result.conflicts])

    def test_move_after_recovery_rebreaks_the_field(self):
        result = check(
            cfg(
                [
                    Block(
                        "b0",
                        (
                            Declare("a"),
                            Move(("a", "x")),
                            Write(("a", "x")),
                            Move(("a", "x")),
                            Read(("a", "x")),
                        ),
                        (),
                    )
                ]
            )
        )
        self.assertEqual(codes(result), [ConflictCode.USE_OF_MOVED])
        # witness origin is the *second* move (instruction 3), not the first
        self.assertEqual(
            result.conflicts[0].witness.steps[0].location.instruction_index, 3
        )


if __name__ == "__main__":
    unittest.main()
