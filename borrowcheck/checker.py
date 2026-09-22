"""Core field-sensitive borrow checking.

Algorithm
=========

The analysis is a *may* data-flow analysis over block boundaries:

* ``moved``        -- paths that may have been moved out and not yet
  reassigned on some path reaching the point;
* ``active_loans`` -- loan ids that may still be live on some reaching
  path;
* ``undeclared``   -- root variables that may be *undeclared* on some
  reaching path (the may-dual of "declared on every reaching path").

At a branch merge facts are united ("may have been moved / may still be
borrowed / may be undeclared on *some* incoming edge").  Inside a block
instructions apply in order; block summaries are recomputed by a chaotic
worklist algorithm until the fixed point is reached (this is what handles
loops).  All three lattices are finite subsets of the paths, loan ids and
variables that occur in the program, so iteration grows facts
monotonically and always terminates; no recursion-depth cutoff is
involved.

Per-instruction semantics:

================  ======================================================
``declare v``     remove ``v`` from undeclared
``move p``        use p; afterwards p (and hence its subtree) is moved
``read p``        use p; conflicts with overlapping moved paths / mut loans
``write p``       assignment: restores p and its subtree only; writing a
                  field of a still-moved ancestor is an error
``borrow_* p``    use p; checked against active overlapping loans, then
                  the loan becomes active
``end_loan L``    L must be active; it is removed
================  ======================================================

Two paths overlap when one is a prefix of the other, so sibling fields
can be borrowed independently while a parent/child pair cannot.

Conflicts carry a *predecessor witness*.  After convergence every block
is replayed over its final entry facts to collect conflicts; for each
offending fact a witness chain is extracted by an explicit, finite
forward BFS over boundary states ``(block id, boundary index)`` from a
concrete origin of the fact (the move/borrow instruction, an ``end_loan``
that kills a loan, or the CFG entry for a missing declaration) to the
conflict site.  The search graph has one node per intra-block boundary
plus CFG edges, so witness length is bounded by program size and never by
a traversal-depth limit.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Optional

from .errors import BudgetExceededError
from .model import (
    BlockFacts,
    BorrowMut,
    BorrowShared,
    CheckResult,
    Conflict,
    ConflictCode,
    Declare,
    EndLoan,
    FactState,
    LoanKind,
    Location,
    Move,
    Read,
    Witness,
    WitnessStep,
    Write,
)
from .validation import ValidatedProgram, validate_cfg, validate_pos_int

#: Hard interface limits.
MAX_BLOCKS = 50
MAX_INSTRUCTIONS_PER_BLOCK = 1000
MAX_PATH_DEPTH = 32
MAX_TYPE_NODES = 10_000

def _bottom(all_roots: tuple[str, ...]) -> FactState:
    # At program entry every root variable may be undeclared; the may set
    # shrinks when declare executes on a path.
    return FactState(
        moved=frozenset(),
        active_loans=frozenset(),
        undeclared=frozenset(all_roots),
    )
Boundary = tuple[str, int]


# ---------------------------------------------------------------------------
# Path relations
# ---------------------------------------------------------------------------


def _is_prefix(prefix: tuple[str, ...], path: tuple[str, ...]) -> bool:
    return path[: len(prefix)] == prefix


def _overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


def _join(path: tuple[str, ...]) -> str:
    return ".".join(path)


# ---------------------------------------------------------------------------
# Transfer function
# ---------------------------------------------------------------------------


def _transfer(state: FactState, instr: Any) -> FactState:
    """Apply one instruction.  Monotone in ``state``; kills are explicit."""

    moved = set(state.moved)
    active = set(state.active_loans)
    undeclared = set(state.undeclared)

    if isinstance(instr, Declare):
        undeclared.discard(instr.var)
    elif isinstance(instr, Move):
        moved.add(instr.path)
    elif isinstance(instr, (BorrowShared, BorrowMut)):
        active.add(instr.loan_id)
    elif isinstance(instr, EndLoan):
        active.discard(instr.loan_id)
    elif isinstance(instr, Read):
        pass
    elif isinstance(instr, Write):
        path = instr.path
        ancestor_moved = any(q != path and _is_prefix(q, path) for q in moved)
        if not ancestor_moved:
            # Restore exactly this field and its subtree; sibling fields
            # keep their own moved status.  Assigning a field while the
            # ancestor itself is moved is illegal and changes nothing.
            moved = {q for q in moved if not _is_prefix(path, q)}
    return FactState(
        moved=frozenset(moved),
        active_loans=frozenset(active),
        undeclared=frozenset(undeclared),
    )


def _merge(states: list[FactState]) -> FactState:
    """Branch merge = may-union.  Empty predecessor list = bottom."""

    if not states:
        return _bottom(())
    moved: set[tuple[str, ...]] = set()
    active: set[str] = set()
    undeclared: set[str] = set()
    for state in states:
        moved |= state.moved
        active |= state.active_loans
        undeclared |= state.undeclared
    return FactState(
        moved=frozenset(moved),
        active_loans=frozenset(active),
        undeclared=frozenset(undeclared),
    )


# ---------------------------------------------------------------------------
# Fixed point
# ---------------------------------------------------------------------------


def _forward_closure(entry: str, succ: dict[str, tuple[str, ...]]) -> set[str]:
    seen = {entry}
    queue = deque([entry])
    while queue:
        b = queue.popleft()
        for s in succ[b]:
            if s not in seen:
                seen.add(s)
                queue.append(s)
    return seen


def _fixed_point(
    program: ValidatedProgram, budget: int
) -> tuple[dict[str, FactState], dict[str, FactState], int]:
    """May-analysis fixed point via a chaotic worklist.

    The entry boundary starts with every type root possibly undeclared
    (program start); edges into the entry block are rejected during
    validation.  A block is (re)processed whenever a reachable
    predecessor's exit state grows, until no exit state changes anymore.
    """

    blocks = program.blocks
    instrs = program.instructions
    roots = tuple(program.types)
    succ: dict[str, tuple[str, ...]] = {
        bid: block.successors for bid, block in blocks.items()
    }
    reachable = _forward_closure(program.entry, succ)
    preds: dict[str, list[str]] = {bid: [] for bid in reachable}
    for p in reachable:
        for s in succ[p]:
            preds[s].append(p)

    entry_facts: dict[str, FactState] = {}
    exit_facts: dict[str, FactState] = {}

    worklist: deque[str] = deque([program.entry])
    queued = {program.entry}
    used = 0
    while worklist:
        bid = worklist.popleft()
        queued.discard(bid)
        used += 1
        if used > budget:
            raise BudgetExceededError(
                "fixed-point worklist exhausted the iteration budget",
                iterations=used,
                budget=budget,
            )

        if bid == program.entry:
            incoming = _bottom(roots)
        else:
            incoming = _merge(
                [exit_facts[p] for p in preds[bid] if p in exit_facts]
            )

        state = incoming
        for instr in instrs[bid]:
            state = _transfer(state, instr)

        # Entry facts may grow even when the exit fact does not change
        # (a newly arriving may-fact can be killed inside the block), and
        # intra-block conflicts depend on that entry state -- so both are
        # tracked independently.
        entry_changed = entry_facts.get(bid) != incoming
        exit_changed = exit_facts.get(bid) != state
        if entry_changed:
            entry_facts[bid] = incoming
        if exit_changed:
            exit_facts[bid] = state
            for s in succ[bid]:
                if s in reachable and s not in queued:
                    worklist.append(s)
                    queued.add(s)

    return entry_facts, exit_facts, used


# ---------------------------------------------------------------------------
# Boundary states and witness reconstruction
# ---------------------------------------------------------------------------


class BoundaryMap:
    """Fact state at every intra-block boundary of the final solution.

    Boundary ``(bid, k)`` is the state *before* instruction ``k`` of block
    ``bid``; ``k == len(instructions)`` is the block exit.
    """

    def __init__(self, program: ValidatedProgram, entries: dict[str, FactState]):
        self.states: dict[str, list[FactState]] = {}
        for bid in entries:
            seq = [entries[bid]]
            for instr in program.instructions[bid]:
                seq.append(_transfer(seq[-1], instr))
            self.states[bid] = seq

    def fact(self, node: Boundary) -> FactState:
        return self.states[node[0]][node[1]]

    def __contains__(self, node: Boundary) -> bool:
        bid, k = node
        seq = self.states.get(bid)
        return seq is not None and 0 <= k < len(seq)


def _forward_witness(
    program: ValidatedProgram,
    boundaries: BoundaryMap,
    goal: Boundary,
    *,
    holds: Callable[[FactState], bool],
    seeds: list[tuple[Boundary, WitnessStep]],
    flow_detail: str,
) -> Witness:
    """Forward BFS from fact origins to the conflict boundary.

    Every traversed boundary must satisfy ``holds``; instruction effects
    that kill the fact are therefore natural barriers, and the found path
    is a concrete control-flow path on which the fact survives all the way
    to the conflict.
    """

    instrs = program.instructions
    succ = {bid: block.successors for bid, block in program.blocks.items()}
    reachable = set(boundaries.states)

    parent: dict[Boundary, Optional[Boundary]] = {}
    seed_of: dict[Boundary, WitnessStep] = {}
    queue: deque[Boundary] = deque()
    for node, step in seeds:
        if node not in boundaries or node in parent:
            continue
        if not holds(boundaries.fact(node)):
            continue
        parent[node] = None
        seed_of[node] = step
        queue.append(node)

    found: Optional[Boundary] = None
    while queue:
        node = queue.popleft()
        if node == goal:
            found = node
            break
        bid, k = node
        candidates: list[Boundary] = []
        if k < len(instrs[bid]):
            candidates.append((bid, k + 1))
        else:
            for s in succ[bid]:
                if s in reachable:
                    candidates.append((s, 0))
        for cand in candidates:
            if cand in parent or cand not in boundaries:
                continue
            if not holds(boundaries.fact(cand)):
                continue
            parent[cand] = node
            queue.append(cand)

    if found is None:
        # Unreachable for facts produced by the transfer function; a
        # failure here is a checker bug rather than a user input error.
        raise RuntimeError(
            "internal error: could not reconstruct a predecessor witness"
        )

    chain: list[Boundary] = []
    cur: Optional[Boundary] = found
    while cur is not None:
        chain.append(cur)
        cur = parent[cur]
    chain.reverse()

    # Group the boundary walk into contiguous per-block runs, then emit
    # one origin/entry step, one "flow" step per run that advances, and one
    # "edge" step per crossing between runs.
    runs: list[tuple[str, int, int]] = []  # (block, first boundary, last)
    for bid, k in chain:
        if runs and runs[-1][0] == bid:
            pb, pk, _ = runs[-1]
            runs[-1] = (pb, pk, k)
        else:
            runs.append((bid, k, k))

    steps: list[WitnessStep] = [seed_of[chain[0]]]
    for i, (bid, k0, k1) in enumerate(runs):
        if i > 0 or k1 > k0:
            detail = flow_detail
            if k1 > k0:
                detail = f"{flow_detail} (boundaries {k0}..{k1})"
            steps.append(WitnessStep(kind="flow", block_id=bid, detail=detail))
        if i + 1 < len(runs):
            steps.append(
                WitnessStep(
                    kind="edge", block_id=bid, next_block=runs[i + 1][0]
                )
            )
    return Witness(steps=tuple(steps))


# ---------------------------------------------------------------------------
# Witness builders per fact kind
# ---------------------------------------------------------------------------


def _moved_seeds(
    program: ValidatedProgram, moved_path: tuple[str, ...]
) -> list[tuple[Boundary, WitnessStep]]:
    seeds: list[tuple[Boundary, WitnessStep]] = []
    for bid, seq in program.instructions.items():
        for index, instr in enumerate(seq):
            if isinstance(instr, Move) and instr.path == moved_path:
                seeds.append(
                    (
                        (bid, index + 1),
                        WitnessStep(
                            kind="origin",
                            block_id=bid,
                            location=Location(bid, index),
                            detail=f"path {_join(moved_path)!r} is moved out here",
                        ),
                    )
                )
    return seeds


def _moved_witness(
    program: ValidatedProgram,
    boundaries: BoundaryMap,
    start: Boundary,
    moved_path: tuple[str, ...],
) -> Witness:
    return _forward_witness(
        program,
        boundaries,
        start,
        holds=lambda f: moved_path in f.moved,
        seeds=_moved_seeds(program, moved_path),
        flow_detail=f"moved path {_join(moved_path)!r} stays unusable",
    )


def _loan_witness(
    program: ValidatedProgram,
    boundaries: BoundaryMap,
    start: Boundary,
    loan_id: str,
) -> Witness:
    rec = program.loans[loan_id]
    seed = (
        (rec.block_id, rec.instruction_index + 1),
        WitnessStep(
            kind="origin",
            block_id=rec.block_id,
            location=Location(rec.block_id, rec.instruction_index),
            detail=f"{rec.kind} loan {loan_id!r} of {_join(rec.path)!r} starts here",
        ),
    )
    return _forward_witness(
        program,
        boundaries,
        start,
        holds=lambda f: loan_id in f.active_loans,
        seeds=[seed],
        flow_detail=f"loan {loan_id!r} stays active (not ended)",
    )


def _undeclared_witness(
    program: ValidatedProgram,
    boundaries: BoundaryMap,
    start: Boundary,
    var: str,
) -> Witness:
    entry = program.entry
    return _forward_witness(
        program,
        boundaries,
        start,
        holds=lambda f: var in f.undeclared,
        seeds=[
            (
                (entry, 0),
                WitnessStep(
                    kind="entry",
                    block_id=entry,
                    detail=f"variable {var!r} is not declared at program entry",
                ),
            )
        ],
        flow_detail=f"variable {var!r} stays undeclared along this branch",
    )


def _inactive_loan_witness(
    program: ValidatedProgram,
    boundaries: BoundaryMap,
    start: Boundary,
    loan_id: str,
) -> Witness:
    rec = program.loans[loan_id]
    entry = program.entry
    seeds: list[tuple[Boundary, WitnessStep]] = [
        (
            (entry, 0),
            WitnessStep(
                kind="entry",
                block_id=entry,
                detail=(
                    f"loan {loan_id!r} ({rec.kind}) is not started at program "
                    f"entry; it is first started at block {rec.block_id!r}, "
                    f"instruction {rec.instruction_index}"
                ),
            ),
        )
    ]
    # A loan may also be inactive *because* it was already ended: every
    # end_loan boundary is an origin of the "inactive" fact.
    for bid, seq in program.instructions.items():
        for index, instr in enumerate(seq):
            if isinstance(instr, EndLoan) and instr.loan_id == loan_id:
                seeds.append(
                    (
                        (bid, index + 1),
                        WitnessStep(
                            kind="origin",
                            block_id=bid,
                            location=Location(bid, index),
                            detail=f"loan {loan_id!r} is ended here",
                        ),
                    )
                )
    return _forward_witness(
        program,
        boundaries,
        start,
        holds=lambda f: loan_id not in f.active_loans,
        seeds=seeds,
        flow_detail=f"loan {loan_id!r} is not active on this path",
    )


# ---------------------------------------------------------------------------
# Conflict detection (replay over the converged states)
# ---------------------------------------------------------------------------


def _loan_enum(value: str) -> LoanKind:
    return LoanKind.MUT if value == "mut" else LoanKind.SHARED


def _make_conflict(
    code: ConflictCode,
    bid: str,
    index: int,
    instr: Any,
    path: Optional[tuple[str, ...]],
    message: str,
    witness: Witness,
    *,
    loan_id: Optional[str] = None,
    loan_kind: Optional[LoanKind] = None,
    loan_path: Optional[tuple[str, ...]] = None,
    against_path: Optional[tuple[str, ...]] = None,
) -> Conflict:
    return Conflict(
        code=code,
        location=Location(bid, index),
        instruction_kind=instr.kind,
        path=path,
        message=message,
        witness=witness,
        loan_id=loan_id,
        loan_kind=loan_kind,
        loan_path=loan_path,
        against_path=against_path,
    )


def _detect_conflicts(
    program: ValidatedProgram, boundaries: BoundaryMap
) -> list[Conflict]:
    conflicts: list[Conflict] = []
    loans = program.loans

    for bid in program.blocks:
        instr_seq = program.instructions[bid]
        if bid not in boundaries.states:
            continue  # unreachable blocks are not analyzed
        for index, instr in enumerate(instr_seq):
            start: Boundary = (bid, index)
            pre = boundaries.fact(start)

            if isinstance(instr, Declare):
                continue

            if isinstance(instr, EndLoan):
                if instr.loan_id not in pre.active_loans:
                    conflicts.append(
                        _make_conflict(
                            ConflictCode.END_INACTIVE_LOAN,
                            bid,
                            index,
                            instr,
                            None,
                            f"end_loan of {instr.loan_id!r} but the loan is "
                            "not active on any reaching path",
                            _inactive_loan_witness(
                                program, boundaries, start, instr.loan_id
                            ),
                            loan_id=instr.loan_id,
                        )
                    )
                continue

            path: tuple[str, ...] = instr.path
            root = path[0]

            # (1) possibly undeclared root on some reaching path
            if root in pre.undeclared:
                conflicts.append(
                    _make_conflict(
                        ConflictCode.USE_BEFORE_DECLARE,
                        bid,
                        index,
                        instr,
                        path,
                        f"{instr.kind} of {_join(path)!r} but root variable "
                        f"{root!r} is not declared on some reaching path",
                        _undeclared_witness(program, boundaries, start, root),
                    )
                )

            # (2) moved paths (prefix overlap in either direction)
            overlapping_moved = sorted(q for q in pre.moved if _overlaps(q, path))
            if isinstance(instr, Write):
                # Assignment is the recovery operation: writing the path
                # itself restores it, and writing an ancestor restores the
                # subtree.  Only a still-moved strict ancestor makes the
                # write illegal (re-assemble the ancestor object first).
                overlapping_moved = [
                    q
                    for q in overlapping_moved
                    if q != path and _is_prefix(q, path)
                ]
                moved_code = ConflictCode.WRITE_TO_FIELD_OF_MOVED_PARENT
            else:
                moved_code = ConflictCode.USE_OF_MOVED
            for q in overlapping_moved:
                if q == path:
                    relation = "moved path"
                elif _is_prefix(q, path):
                    relation = "moved ancestor path"
                else:
                    relation = "descendant invalidated by a partial move"
                conflicts.append(
                    _make_conflict(
                        moved_code,
                        bid,
                        index,
                        instr,
                        path,
                        f"{instr.kind} of {_join(path)!r} conflicts with "
                        f"{relation} {_join(q)!r}",
                        _moved_witness(program, boundaries, start, q),
                        against_path=q,
                    )
                )

            # (3) active loans
            overlapping_loans = sorted(
                lid for lid in pre.active_loans if _overlaps(loans[lid].path, path)
            )

            if isinstance(instr, Read):
                for lid in overlapping_loans:
                    rec = loans[lid]
                    if rec.kind != "mut":
                        continue
                    conflicts.append(
                        _make_conflict(
                            ConflictCode.READ_WHILE_MUT_LOAN,
                            bid,
                            index,
                            instr,
                            path,
                            f"read of {_join(path)!r} while mutable loan "
                            f"{lid!r} of {_join(rec.path)!r} may still be active",
                            _loan_witness(program, boundaries, start, lid),
                            loan_id=lid,
                            loan_kind=LoanKind.MUT,
                            loan_path=rec.path,
                        )
                    )
            elif isinstance(instr, Write):
                for lid in overlapping_loans:
                    rec = loans[lid]
                    conflicts.append(
                        _make_conflict(
                            ConflictCode.WRITE_WHILE_LOAN_ACTIVE,
                            bid,
                            index,
                            instr,
                            path,
                            f"write of {_join(path)!r} while {rec.kind} loan "
                            f"{lid!r} of {_join(rec.path)!r} may still be active",
                            _loan_witness(program, boundaries, start, lid),
                            loan_id=lid,
                            loan_kind=_loan_enum(rec.kind),
                            loan_path=rec.path,
                        )
                    )
            elif isinstance(instr, Move):
                for lid in overlapping_loans:
                    rec = loans[lid]
                    conflicts.append(
                        _make_conflict(
                            ConflictCode.MOVE_WHILE_LOAN_ACTIVE,
                            bid,
                            index,
                            instr,
                            path,
                            f"move of {_join(path)!r} while {rec.kind} loan "
                            f"{lid!r} of {_join(rec.path)!r} may still be active",
                            _loan_witness(program, boundaries, start, lid),
                            loan_id=lid,
                            loan_kind=_loan_enum(rec.kind),
                            loan_path=rec.path,
                        )
                    )
            else:  # borrow_shared / borrow_mut
                new_is_mut = isinstance(instr, BorrowMut)
                for lid in overlapping_loans:
                    rec = loans[lid]
                    if not new_is_mut and rec.kind == "shared":
                        continue  # shared/shared on overlapping paths is OK
                    conflicts.append(
                        _make_conflict(
                            ConflictCode.LOAN_CONFLICT,
                            bid,
                            index,
                            instr,
                            path,
                            f"{'mutable' if new_is_mut else 'shared'} loan "
                            f"{instr.loan_id!r} of {_join(path)!r} overlaps "
                            f"active {rec.kind} loan {lid!r} of "
                            f"{_join(rec.path)!r}",
                            _loan_witness(program, boundaries, start, lid),
                            loan_id=lid,
                            loan_kind=_loan_enum(rec.kind),
                            loan_path=rec.path,
                        )
                    )

    return conflicts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check(cfg: Any, *, budget: Optional[int] = None) -> CheckResult:
    """Check ``cfg`` and return a :class:`CheckResult`.

    Args:
        cfg:    :class:`borrowcheck.CFG` satisfying the documented limits.
        budget: Optional positive ``int`` cap on fixed-point work units.
            ``None`` (default) uses a budget derived from the program size
            that is provably sufficient for the finite lattices.  Passing
            a smaller budget may raise :class:`BudgetExceededError`, which
            means *incomplete analysis* -- never that the CFG was proven
            conflict free.  ``bool`` is rejected as an integer.

    Raises:
        CFGValidationError: malformed input; the error locates the bad
            object and no result object is produced (input CFGs are never
            mutated).
        BudgetExceededError: the explicit budget was exhausted before the
            fixed point was reached.
    """

    program = validate_cfg(
        cfg,
        MAX_BLOCKS,
        MAX_INSTRUCTIONS_PER_BLOCK,
        MAX_PATH_DEPTH,
        MAX_TYPE_NODES,
    )

    total_instr = sum(len(seq) for seq in program.instructions.values())
    n_blocks = max(1, len(program.blocks))
    fact_dimensions = (
        max(1, total_instr)
        + max(1, len(program.loans))
        + max(1, len(program.types))
    )
    # Each reprocessing adds at least one may-fact somewhere; a block can
    # be processed at most fact_dimensions+1 times before global stasis.
    auto_budget = n_blocks * (fact_dimensions + 1) + n_blocks + 16
    if budget is None:
        effective_budget = auto_budget
    else:
        effective_budget = validate_pos_int(budget, "budget", "check(budget=...)")

    entries, exits, iterations = _fixed_point(program, effective_budget)
    boundaries = BoundaryMap(program, entries)
    conflicts = _detect_conflicts(program, boundaries)

    facts = {
        bid: BlockFacts(entry=entries[bid], exit_=exits[bid]) for bid in entries
    }
    return CheckResult(
        conflicts=tuple(conflicts),
        facts=facts,
        iterations=iterations,
    )
