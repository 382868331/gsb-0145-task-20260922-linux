"""Fixed-input demonstration of the field-sensitive borrow checker.

Run with::

    python demo.py

It performs real analysis (no sleep, no pre-recorded output) on two
small, hand-built programs and prints:

1. a *normal* result: an order-shipping function whose two branches
   borrow disjoint fields and re-assemble a moved object -- accepted;
2. a *rejection* boundary: a loop in which a mutable loan is started on
   one branch only, never ended, and the merged read conflicts with it --
   rejected with a full predecessor-witness chain;
3. an *exception* boundary: malformed input (non-existent field) raises a
   locatable ``CFGValidationError``.
"""

from __future__ import annotations

import time

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
    Move,
    Read,
    Write,
    check,
)

# A small fixed record tree used by both demo programs.
ORDER_TYPES = {
    "order": {
        "customer": {"name": None, "addr": None},
        "items": {"first": {"sku": None}, "second": {"sku": None}},
        "status": None,
    }
}


def heading(title: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n{title}\n{bar}")


def print_conflicts(result) -> None:
    if result.ok:
        print("  result: ACCEPTED (no conflicts)")
        return
    print(f"  result: REJECTED ({len(result.conflicts)} conflict(s))")
    for n, conflict in enumerate(result.conflicts, 1):
        print(f"\n  [{n}] {conflict.code.value}")
        print(f"      at      : {conflict.location} ({conflict.instruction_kind})")
        if conflict.path is not None:
            print(f"      path    : {'.'.join(conflict.path)}")
        if conflict.loan_id is not None:
            extra = (
                f" ({conflict.loan_kind.value})" if conflict.loan_kind else ""
            )
            print(f"      loan    : {conflict.loan_id}{extra}")
        print(f"      message : {conflict.message}")
        print("      witness : predecessor chain explaining the fact")
        for line in conflict.witness.render().splitlines():
            print(f"      {line}")


def build_accepted_program() -> CFG:
    """Disjoint-field borrows across branches + parent reassembly.

    * One branch mutably borrows ``order.items.first`` while reading the
      disjoint sibling ``order.items.second`` -- disjoint fields, so both
      are fine.  The other branch takes a *shared* loan of
      ``order.customer.name``; reads are allowed under a shared loan.
    * ``order.status`` is moved out and later the whole status slot is
      reassigned (parent reassembly), restoring it for the final read.
    """

    return CFG(
        blocks=[
            Block(
                "entry",
                (
                    Declare("order"),
                    Move(("order", "status")),
                ),
                ("pack_items", "notify"),
            ),
            Block(
                "pack_items",
                (
                    BorrowMut("loan_first_sku", ("order", "items", "first")),
                    # disjoint sibling field stays fully usable
                    Read(("order", "items", "second", "sku")),
                    EndLoan("loan_first_sku"),
                    # after ending the loan, its own field is readable again
                    Read(("order", "items", "first", "sku")),
                ),
                ("join",),
            ),
            Block(
                "notify",
                (
                    BorrowShared("loan_name", ("order", "customer", "name")),
                    Read(("order", "customer", "name")),
                    EndLoan("loan_name"),
                ),
                ("join",),
            ),
            Block(
                "join",
                (
                    # status was moved; reassigning that field restores it
                    Write(("order", "status")),
                    Read(("order", "status")),
                    # sibling item field untouched by either branch's move
                    Read(("order", "items", "second", "sku")),
                ),
                (),
            ),
        ],
        entry="entry",
        types=ORDER_TYPES,
    )


def build_rejected_program() -> CFG:
    """A loop that may revisit a read while a mutable loan is still live.

    On the first pass ``retry`` creates a mutable loan on
    ``order.customer.addr`` and never ends it; the loop back-edge means
    the read at the loop header is reachable with the loan active, and a
    move inside the loop additionally races the same loan.
    """

    return CFG(
        blocks=[
            Block(
                "start",
                (Declare("order"),),
                ("header",),
            ),
            Block(
                "header",
                (Read(("order", "customer", "addr")),),
                ("retry", "done"),
            ),
            Block(
                "retry",
                (
                    BorrowMut("loan_addr", ("order", "customer", "addr")),
                    Move(("order", "customer", "addr")),
                ),
                ("header",),
            ),
            Block("done", (), ()),
        ],
        entry="start",
        types=ORDER_TYPES,
    )


def main() -> int:
    start = time.perf_counter()

    heading("1) NORMAL RESULT -- disjoint fields, branch merge, reassembly")
    accepted = build_accepted_program()
    result = check(accepted)
    print_conflicts(result)
    print(f"  fixed-point iterations: {result.iterations}")
    assert result.ok, "demo program 1 is expected to be accepted"

    heading("2) REJECTION BOUNDARY -- un-ended mutable loan surviving a loop")
    rejected = build_rejected_program()
    result = check(rejected)
    print_conflicts(result)
    print(f"  fixed-point iterations: {result.iterations}")
    assert not result.ok, "demo program 2 is expected to be rejected"
    codes = {c.code for c in result.conflicts}
    assert ConflictCode.READ_WHILE_MUT_LOAN in codes
    assert ConflictCode.MOVE_WHILE_LOAN_ACTIVE in codes
    # every witness must be grounded at a concrete origin instruction
    for conflict in result.conflicts:
        assert conflict.witness.steps[0].kind in ("origin", "entry")

    heading("3) EXCEPTION BOUNDARY -- malformed input is located, not guessed")
    bad = CFG(
        blocks=[
            Block(
                "b0",
                (
                    Declare("order"),
                    Read(("order", "customer", "postcode")),  # no such field
                ),
                (),
            )
        ],
        entry="b0",
        types=ORDER_TYPES,
    )
    try:
        check(bad)
    except CFGValidationError as err:
        print(f"  raised   : {type(err).__name__}")
        print(f"  location : {err.location}")
        print(f"  message  : {err.message}")
    else:  # pragma: no cover - demo guard
        raise AssertionError("malformed CFG must raise CFGValidationError")

    # Demonstrate the budget-exhaustion vs. proven-clean distinction on a
    # deliberately tiny budget (this is the only "incomplete" outcome and
    # it is distinct from a clean result).
    heading("4) BUDGET BOUNDARY -- exhaustion is distinct from a proof")
    looping = CFG(
        blocks=[
            Block("b0", (Declare("order"),), ("b1",)),
            Block("b1", (), ("b1",)),
        ],
        entry="b0",
        types=ORDER_TYPES,
    )
    try:
        check(looping, budget=1)
    except BudgetExceededError as err:
        print(f"  raised   : {type(err).__name__}")
        print(f"  detail   : used {err.iterations} of {err.budget} work units")
        print("  meaning  : analysis incomplete; NOT a proof of no conflicts")

    elapsed = time.perf_counter() - start
    print(f"\nDemo finished in {elapsed:.3f}s (real computation, no sleeps).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
