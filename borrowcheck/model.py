"""Public immutable data model for the borrow checker.

A program is a control-flow graph (:class:`CFG`) of at most
:data:`borrowcheck.checker.MAX_BLOCKS` :class:`Block`\\ s.  Each block holds
an ordered list of instructions, one of seven kinds:

``declare``        :class:`Declare`          introduce a root variable
``move``           :class:`Move`             move a path out
``borrow_shared``  :class:`BorrowShared`     start a shared loan
``borrow_mut``     :class:`BorrowMut`        start a mutable loan
``end_loan``       :class:`EndLoan`          finish a loan by id
``read``           :class:`Read`             read a path
``write``          :class:`Write`            assign a path (restore it)

Paths are tuples of field names rooted at a declared variable, e.g.
``("order", "customer", "name")``.  Two paths *overlap* when one is a
prefix of the other (equal paths included).

Variables carry a finite record *type tree* supplied with the CFG:
a nested ``dict`` of field name to subtree; leaves are ``None`` (or an
empty mapping).  Only paths that exist in this tree are legal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


# ---------------------------------------------------------------------------
# Instruction model
# ---------------------------------------------------------------------------


class Instruction:
    """Abstract base class; subclasses carry a stable ``kind`` string."""

    kind: str = ""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Declare(Instruction):
    """Declare root variable ``var`` (must exist in the type tree)."""

    var: str
    kind: str = field(default="declare", init=False)


@dataclass(frozen=True, slots=True)
class Move(Instruction):
    """Move ``path`` out.  The path and all of its subtree become unusable."""

    path: tuple[str, ...]
    kind: str = field(default="move", init=False)


@dataclass(frozen=True, slots=True)
class BorrowShared(Instruction):
    """Start a shared loan with statically unique ``loan_id`` on ``path``."""

    loan_id: str
    path: tuple[str, ...]
    kind: str = field(default="borrow_shared", init=False)


@dataclass(frozen=True, slots=True)
class BorrowMut(Instruction):
    """Start a mutable loan with statically unique ``loan_id`` on ``path``."""

    loan_id: str
    path: tuple[str, ...]
    kind: str = field(default="borrow_mut", init=False)


@dataclass(frozen=True, slots=True)
class EndLoan(Instruction):
    """End the loan with ``loan_id`` (must refer to an existing borrow)."""

    loan_id: str
    kind: str = field(default="end_loan", init=False)


@dataclass(frozen=True, slots=True)
class Read(Instruction):
    """Read ``path``."""

    path: tuple[str, ...]
    kind: str = field(default="read", init=False)


@dataclass(frozen=True, slots=True)
class Write(Instruction):
    """Assign ``path``.

    Assigning a field restores exactly that field and its subtree; it does
    not restore sibling fields.
    """

    path: tuple[str, ...]
    kind: str = field(default="write", init=False)


# ---------------------------------------------------------------------------
# CFG
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    """A basic block: ``id``, instructions, and explicit successor ids.

    Successor order is insignificant for the analysis; every successor
    edge is a possible control-flow edge (branch merge = union).
    """

    id: str
    instructions: tuple[Instruction, ...]
    successors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CFG:
    """Control-flow graph plus the finite record type tree.

    ``types`` maps each root variable name to a nested mapping of field
    names to subtrees; a leaf is ``None`` or an empty mapping.  Example::

        CFG(
            entry="b0",
            blocks=[Block("b0", (), ("b1",))],
            types={"a": {"x": None, "y": {"z": None}}},
        )
    """

    blocks: tuple[Block, ...]
    entry: str
    types: Mapping[str, object]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class LoanKind(str, Enum):
    SHARED = "shared"
    MUT = "mut"


class ConflictCode(str, Enum):
    """Why a conflict was reported.  Stable string values form the API."""

    USE_OF_MOVED = "use_of_moved"
    """Read/move/borrow through a path that may have been moved out."""

    USE_BEFORE_DECLARE = "use_before_declare"
    """A path is used on a control-flow path on which its root was not
    definitely declared yet."""

    READ_WHILE_MUT_LOAN = "read_while_mut_loan"
    """Read through a path overlapped by an active mutable loan."""

    WRITE_WHILE_LOAN_ACTIVE = "write_while_loan_active"
    """Write through a path overlapped by any active loan."""

    MOVE_WHILE_LOAN_ACTIVE = "move_while_loan_active"
    """Move through a path overlapped by any active loan."""

    LOAN_CONFLICT = "loan_conflict"
    """New loan is incompatible with an already active overlapping loan
    (shared/mut or mut/anything)."""

    WRITE_TO_FIELD_OF_MOVED_PARENT = "write_to_field_of_moved_parent"
    """Assigning ``a.b`` while ancestor ``a`` itself is moved out
    (re-assemble the ancestor first)."""

    END_INACTIVE_LOAN = "end_inactive_loan"
    """``end_loan`` names a loan that is not active at that point."""


@dataclass(frozen=True, slots=True)
class Location:
    """A place in the CFG: a block id and a 0-based instruction index."""

    block_id: str
    instruction_index: int

    def __str__(self) -> str:
        return f"block {self.block_id!r}, instruction {self.instruction_index}"


@dataclass(frozen=True, slots=True)
class WitnessStep:
    """One step of a predecessor-witness chain.

    Kinds:

    * ``"origin"``   : the instruction at ``location`` creates the fact
      (a move or the borrow of a loan).
    * ``"flow"``     : the fact survives inside ``block_id`` up to
      ``location`` (instruction boundary).
    * ``"edge"``     : control-flow edge ``block_id`` -> ``next_block``.
    * ``"entry"``    : the chain reaches the CFG entry with no declaration
      (used by ``use_before_declare`` witnesses).
    """

    kind: str
    block_id: str
    next_block: str | None = None
    location: Location | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Witness:
    """Predecessor chain proving how a fact reaches a conflict site.

    ``steps`` run from the fact's origin to (but not including) the
    conflict location.  The chain is found structurally over the final
    fixed-point state; its length is bounded by the CFG size and never by
    a recursion-depth cutoff.
    """

    steps: tuple[WitnessStep, ...]

    def render(self) -> str:
        lines: list[str] = []
        for step in self.steps:
            if step.kind == "origin":
                lines.append(f"  origin: {step.detail} @ {step.location}")
            elif step.kind == "flow":
                lines.append(f"  flow:   {step.detail} through block {step.block_id!r}")
            elif step.kind == "edge":
                lines.append(
                    f"  edge:   block {step.block_id!r} -> {step.next_block!r}"
                )
            elif step.kind == "entry":
                lines.append(
                    f"  entry:  {step.detail}"
                    if step.detail
                    else "  entry:  fact does not hold at program entry"
                )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Conflict:
    """A single borrow-checking violation with a predecessor witness."""

    code: ConflictCode
    location: Location
    instruction_kind: str
    path: tuple[str, ...] | None
    message: str
    witness: Witness
    loan_id: str | None = None
    loan_kind: LoanKind | None = None
    loan_path: tuple[str, ...] | None = None
    against_path: tuple[str, ...] | None = None
    """The other path involved in a moved-path conflict (the moved root
    of the overlap); ``None`` for loan/declaration conflicts."""


@dataclass(frozen=True, slots=True)
class FactState:
    """May-analysis facts at a block boundary.

    ``undeclared`` holds root variables that are *possibly not declared*
    on some reaching path (program entry starts with every type root
    undeclared; ``declare`` removes the root; merges unite).  A use is an
    error exactly when its root is in this set -- the may counterpart of
    "must be declared on every reaching path".
    """

    moved: frozenset[tuple[str, ...]]
    active_loans: frozenset[str]
    undeclared: frozenset[str]


@dataclass(frozen=True, slots=True)
class BlockFacts:
    entry: FactState
    exit_: FactState


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of :func:`borrowcheck.checker.check`.

    ``ok`` is ``True`` exactly when ``conflicts`` is empty.  ``facts``
    exposes the converged boundary states (useful for tooling).  A
    conflict-free result constitutes the proof; an exhausted budget is
    raised as :class:`borrowcheck.BudgetExceededError` instead.
    """

    conflicts: tuple[Conflict, ...]
    facts: Mapping[str, BlockFacts]
    iterations: int

    @property
    def ok(self) -> bool:
        return not self.conflicts
