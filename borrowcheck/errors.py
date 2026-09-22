"""Error hierarchy and locatable validation diagnostics.

Error semantics (fixed public contract):

* :class:`CFGValidationError` is raised *before* any analysis state is
  produced for every malformed input (unknown variable, unknown field,
  duplicate block/instruction/loan id, bad successor graph, wrong types,
  non-finite numbers, ``bool`` where ``int`` is required, ...).
  The error carries a :attr:`location` trail that pinpoints the offending
  object, e.g. ``"block 3 -> instruction 1 (borrow_mut) -> path[1]"``.
  Invalid input is never silently repaired.
* :class:`BudgetExhaustedError` is raised only when the analysis search
  budget is exhausted.  It is deliberately distinct from a result proving
  the CFG conflict-free: callers can tell "did not finish" from "proven
  clean".  ``check`` either returns a complete result or raises; it never
  leaves partially published state (CFG objects are not mutated).
"""

from __future__ import annotations

from typing import Any


class BorrowCheckError(Exception):
    """Base class for all errors raised by the borrow checker."""


class CFGValidationError(BorrowCheckError):
    """The CFG description violates the input contract.

    Attributes:
        message:  Human readable description.
        location: Location trail inside the input (``None`` for whole-CFG
            errors such as a missing entry block).
        value:    The offending value, when one exists (repr-safe only; it
            may be a user supplied object).
    """

    def __init__(
        self,
        message: str,
        location: str | None = None,
        value: Any = None,
    ) -> None:
        self.message = message
        self.location = location
        self.value = value
        prefix = f"invalid CFG [{location}]: " if location else "invalid CFG: "
        super().__init__(prefix + message)


class BudgetExceededError(BorrowCheckError):
    """The fixed analysis budget was exhausted before a fixed point.

    This means the checker *could not finish*, never that the program was
    proven correct.  With the documented limits (``MAX_BLOCKS`` etc.) the
    standard-limit budget always suffices; this error can only be reached
    via an explicit reduced ``budget`` argument to
    :func:`borrowcheck.checker.check`.
    """

    def __init__(self, message: str, *, iterations: int, budget: int) -> None:
        self.iterations = iterations
        self.budget = budget
        super().__init__(
            f"{message} (used {iterations} of {budget} work units); "
            "analysis incomplete — not a proof of absence of conflicts"
        )
