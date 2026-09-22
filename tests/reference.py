"""Independent reference oracle for the tests.

This module is deliberately written from the task specification alone and
does **not** import or call the core implementation under test
(``borrowcheck.checker``).  Expected values therefore come from an
independent algorithm, never from the code being tested.

Two oracles are provided:

* :func:`explicit_state_conflicts` -- explicit-state forward reachability.
  The state at every intra-block boundary is the *set* of concrete may
  states reachable along control-flow paths (states are kept apart rather
  than unioned into one may-state per block).  Loops are handled by
  iterating to a fixed point over this finite set-of-states domain.

* :func:`enumerated_path_conflicts` -- for small acyclic CFGs, enumerate
  every root-to-site path explicitly (block ids strictly increase, so the
  enumeration is finite) and evaluate the spec on each path.

Both return conflict *signatures* -- plain tuples -- that the tests
compare against the core's output without either side depending on the
other's implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

# Instruction tags mirror the public dataclasses structurally; the oracle
# reads ``kind`` and fields directly, so it works on the same frozen value
# objects without importing the checker logic.


def _is_prefix(prefix, path):
    return path[: len(prefix)] == prefix


def _overlaps(a, b):
    return _is_prefix(a, b) or _is_prefix(b, a)


@dataclass(frozen=True)
class RState:
    moved: frozenset
    active: frozenset
    declared: frozenset


_BOTTOM = RState(frozenset(), frozenset(), frozenset())


def ref_transfer(state: RState, instr) -> RState:
    """Spec transfer, written independently for the oracle."""

    kind = instr.kind
    moved = set(state.moved)
    active = set(state.active)
    declared = set(state.declared)

    if kind == "declare":
        declared.add(instr.var)
    elif kind == "move":
        moved.add(instr.path)
    elif kind in ("borrow_shared", "borrow_mut"):
        active.add(instr.loan_id)
    elif kind == "end_loan":
        active.discard(instr.loan_id)
    elif kind == "read":
        pass
    elif kind == "write":
        p = instr.path
        ancestor_moved = any(q != p and _is_prefix(q, p) for q in moved)
        if not ancestor_moved:
            moved = {q for q in moved if not _is_prefix(p, q)}
    return RState(frozenset(moved), frozenset(active), frozenset(declared))


def _instruction_conflicts(state: RState, instr, loan_table):
    """Conflict signatures produced by ``instr`` in one concrete state.

    A signature is
    ``(code, path, loan_id, loan_kind, loan_path, against_path)``.
    ``end_inactive_loan`` and ``use_before_declare`` are handled by the
    caller because they are "holds on *no* / *all* reaching paths" rules
    rather than existential may-rules.
    """

    out = []
    if instr.kind in ("declare", "end_loan"):
        return out

    p = instr.path

    # may-moved overlaps
    moved_hits = sorted(q for q in state.moved if _overlaps(q, p))
    if instr.kind == "write":
        moved_hits = [q for q in moved_hits if q != p and _is_prefix(q, p)]
        code = "write_to_field_of_moved_parent"
    else:
        code = "use_of_moved"
    for q in moved_hits:
        out.append((code, p, None, None, None, q))

    active_hits = sorted(
        lid for lid in state.active if _overlaps(loan_table[lid][1], p)
    )

    if instr.kind == "read":
        for lid in active_hits:
            lkind, lpath = loan_table[lid]
            if lkind == "mut":
                out.append(("read_while_mut_loan", p, lid, "mut", lpath, None))
    elif instr.kind == "write":
        for lid in active_hits:
            lkind, lpath = loan_table[lid]
            out.append(("write_while_loan_active", p, lid, lkind, lpath, None))
    elif instr.kind == "move":
        for lid in active_hits:
            lkind, lpath = loan_table[lid]
            out.append(("move_while_loan_active", p, lid, lkind, lpath, None))
    else:  # borrow_shared / borrow_mut
        new_mut = instr.kind == "borrow_mut"
        for lid in active_hits:
            lkind, lpath = loan_table[lid]
            if not new_mut and lkind == "shared":
                continue
            out.append(("loan_conflict", p, lid, lkind, lpath, None))

    return out


def _boundary_state_sets(cfg):
    """May-reachability as sets of concrete states per boundary.

    Returns ``states_at`` mapping ``(block_id, k)`` to a set of
    :class:`RState`, plus ``succ`` and the ordered blocks.
    """

    blocks = {b.id: b for b in cfg.blocks}
    order = [b.id for b in cfg.blocks]
    instrs = {b.id: tuple(b.instructions) for b in cfg.blocks}
    succ = {b.id: tuple(b.successors) for b in cfg.blocks}

    boundaries = []
    for bid in order:
        for k in range(len(instrs[bid]) + 1):
            boundaries.append((bid, k))

    states_at = {node: set() for node in boundaries}
    states_at[(cfg.entry, 0)].add(_BOTTOM)

    # Iterate over the full boundary system to a fixed point.  The domain
    # is finite (facts are subsets of program vocabulary), so this always
    # terminates; no depth cutoff.
    changed = True
    while changed:
        changed = False
        for bid in order:
            seq = instrs[bid]
            for k, instr in enumerate(seq):
                node = (bid, k)
                nxt = (bid, k + 1)
                before = len(states_at[nxt])
                for st in states_at[node]:
                    states_at[nxt].add(ref_transfer(st, instr))
                if len(states_at[nxt]) != before:
                    changed = True
            exit_node = (bid, len(seq))
            for s in succ[bid]:
                target = (s, 0)
                before = len(states_at[target])
                states_at[target].update(states_at[exit_node])
                if len(states_at[target]) != before:
                    changed = True
    return states_at, instrs, succ, order


def explicit_state_conflicts(cfg):
    """Signature set ``{(block_id, index, code, path, lid, against)}``."""

    states_at, instrs, _succ, order = _boundary_state_sets(cfg)
    loan_table = {
        instr.loan_id: (
            "mut" if instr.kind == "borrow_mut" else "shared",
            instr.path,
        )
        for bid in order
        for instr in instrs[bid]
        if instr.kind in ("borrow_shared", "borrow_mut")
    }

    sigs: set[tuple] = set()
    for bid in order:
        seq = instrs[bid]
        for k, instr in enumerate(seq):
            states = states_at[(bid, k)]
            if not states:
                continue  # unreachable site

            if instr.kind == "end_loan":
                # Legal only if the loan is active on *some* reaching path.
                if all(instr.loan_id not in st.active for st in states):
                    sigs.add(
                        (bid, k, "end_inactive_loan", None, instr.loan_id, None)
                    )
                continue

            if instr.kind == "declare":
                continue

            p = instr.path
            if any(p[0] not in st.declared for st in states):
                sigs.add((bid, k, "use_before_declare", p, None, None))

            for st in states:
                for code, cp, lid, _lk, _lp, against in _instruction_conflicts(
                    st, instr, loan_table
                ):
                    sigs.add((bid, k, code, cp, lid, against))
    return sigs


def enumerated_path_conflicts(cfg):
    """Path-enumeration oracle; valid only for acyclic block graphs.

    Every block-index boundary walk starts at ``(entry, 0)``; because
    successor block ids are not required to be ordered here, acyclicity is
    guaranteed by the caller (the test only passes DAGs it built).  All
    reaching concrete states are gathered by DFS over block instances.
    """

    blocks = {b.id: b for b in cfg.blocks}
    instrs = {b.id: tuple(b.instructions) for b in cfg.blocks}
    succ = {b.id: tuple(b.successors) for b in cfg.blocks}

    # states reaching each boundary (unioned collection across paths;
    # identical states collapse)
    reached: dict[tuple, list[RState]] = {}

    def walk(bid, k, state, on_stack):
        node = (bid, k)
        states = reached.setdefault(node, [])
        if any(s == state for s in states):
            return
        states.append(state)
        seq = instrs[bid]
        if k < len(seq):
            walk(bid, k + 1, ref_transfer(state, seq[k]), on_stack)
        else:
            for s in succ[bid]:
                if s in on_stack:
                    raise ValueError("enumerated_path_conflicts requires a DAG")
                walk(s, 0, state, on_stack | {s})

    walk(cfg.entry, 0, _BOTTOM, {cfg.entry})

    loan_table = {
        instr.loan_id: (
            "mut" if instr.kind == "borrow_mut" else "shared",
            instr.path,
        )
        for b in cfg.blocks
        for instr in b.instructions
        if instr.kind in ("borrow_shared", "borrow_mut")
    }

    order = [b.id for b in cfg.blocks]
    sigs: set[tuple] = set()
    for bid in order:
        for k, instr in enumerate(instrs[bid]):
            states = reached.get((bid, k), [])
            if not states:
                continue
            if instr.kind == "end_loan":
                if all(instr.loan_id not in st.active for st in states):
                    sigs.add(
                        (bid, k, "end_inactive_loan", None, instr.loan_id, None)
                    )
                continue
            if instr.kind == "declare":
                continue
            p = instr.path
            if any(p[0] not in st.declared for st in states):
                sigs.add((bid, k, "use_before_declare", p, None, None))
            for st in states:
                for code, cp, lid, _lk, _lp, against in _instruction_conflicts(
                    st, instr, loan_table
                ):
                    sigs.add((bid, k, code, cp, lid, against))
    return sigs
