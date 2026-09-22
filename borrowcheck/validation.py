"""Strict input validation.

Everything that can be wrong about the *shape* of a CFG description is
checked here, before any analysis runs, and reported as a
:class:`CFGValidationError` with a location trail such as::

    block 'b2' -> instruction 0 (borrow_mut) -> path[2]

The checker never guesses: unknown variables/fields/successors, duplicate
ids, path errors, bad numeric arguments and type mismatches are all fatal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import CFGValidationError
from .model import (
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    Declare,
    EndLoan,
    Instruction,
    Move,
    Read,
    Write,
)

_INSTRUCTION_TYPES = (
    Declare,
    Move,
    BorrowShared,
    BorrowMut,
    EndLoan,
    Read,
    Write,
)


@dataclass(frozen=True, slots=True)
class LoanRecord:
    kind: str  # "shared" | "mut"
    path: tuple[str, ...]
    block_id: str
    instruction_index: int


@dataclass(frozen=True, slots=True)
class ValidatedProgram:
    """Result of validation: everything the analysis needs, pre-checked."""

    cfg: CFG
    blocks: dict[str, Block]
    instructions: dict[str, tuple[Instruction, ...]]
    loans: dict[str, LoanRecord]
    types: dict[str, Any]
    entry: str


def _is_real_int(value: Any) -> bool:
    """``int`` accepted, ``bool`` rejected (bool is an int subclass)."""

    return isinstance(value, int) and not isinstance(value, bool)


def validate_nonneg_int(value: Any, what: str, loc: str) -> int:
    """Validate a non-negative integer argument; reject bool/NaN/Infinity.

    Floats are rejected even when finite and integral: the contract asks
    for integer parameters; requiring the exact type also makes the
    NaN/Infinity screen unambiguous.
    """

    if not _is_real_int(value):
        if isinstance(value, float):
            if math.isnan(value):
                raise CFGValidationError(f"{what} must not be NaN", loc, value)
            if math.isinf(value):
                raise CFGValidationError(
                    f"{what} must not be Infinity", loc, value
                )
        raise CFGValidationError(
            f"{what} must be an int (bool rejected), got {type(value).__name__}",
            loc,
            value,
        )
    if value < 0:
        raise CFGValidationError(f"{what} must be >= 0, got {value}", loc, value)
    return value


def validate_pos_int(value: Any, what: str, loc: str) -> int:
    n = validate_nonneg_int(value, what, loc)
    if n == 0:
        raise CFGValidationError(f"{what} must be > 0", loc, value)
    return n


def validate_identifier(value: Any, what: str, loc: str) -> str:
    if not isinstance(value, str):
        raise CFGValidationError(
            f"{what} must be a str, got {type(value).__name__}", loc, value
        )
    if value == "":
        raise CFGValidationError(f"{what} must be non-empty", loc, value)
    if any(ch.isspace() for ch in value):
        raise CFGValidationError(
            f"{what} must not contain whitespace, got {value!r}", loc, value
        )
    return value


def validate_path(path: Any, loc: str) -> tuple[str, ...]:
    if not isinstance(path, (tuple, list)):
        raise CFGValidationError(
            f"path must be a tuple/list of field names, got {type(path).__name__}",
            loc,
            path,
        )
    out: list[str] = []
    for i, component in enumerate(path):
        if not isinstance(component, str):
            raise CFGValidationError(
                f"path[{i}] must be a str field name, got "
                f"{type(component).__name__}",
                f"{loc} -> path[{i}]",
                component,
            )
        if component == "":
            raise CFGValidationError(
                "path components must be non-empty", f"{loc} -> path[{i}]", component
            )
        out.append(component)
    if not out:
        raise CFGValidationError(
            "path must name at least a root variable", loc, path
        )
    return tuple(out)


def validate_type_tree(
    node: Any, prefix: tuple[str, ...], loc: str, max_nodes: int
) -> int:
    """Validate one type subtree; return the number of tree nodes."""

    if node is None:
        return 1
    if not isinstance(node, dict):
        raise CFGValidationError(
            f"type of {'.'.join(prefix)!r} must be a dict of fields or None "
            f"(leaf), got {type(node).__name__}",
            loc,
            node,
        )
    count = 1
    seen: set[str] = set()
    for fname, fchild in node.items():
        if not isinstance(fname, str) or fname == "":
            raise CFGValidationError(
                "field names in type tree must be non-empty str", loc, fname
            )
        if fname in seen:
            raise CFGValidationError(
                f"duplicate field {fname!r} in type of {'.'.join(prefix)!r}", loc
            )
        seen.add(fname)
        count += validate_type_tree(fchild, prefix + (fname,), loc, max_nodes)
        if count > max_nodes:
            raise CFGValidationError(
                f"type tree exceeds the {max_nodes}-node limit", loc
            )
    return count


def instr_kind_name(instr: Any) -> str:
    if isinstance(instr, Instruction):
        return instr.kind
    return type(instr).__name__.lower()


def validate_cfg(
    cfg: CFG,
    max_blocks: int,
    max_instructions_per_block: int,
    max_path_depth: int,
    max_type_nodes: int,
) -> ValidatedProgram:
    """Validate the whole CFG and return a :class:`ValidatedProgram`."""

    if not isinstance(cfg, CFG):
        raise CFGValidationError(
            f"check() expects a CFG, got {type(cfg).__name__}", "cfg", cfg
        )

    # ---- types -----------------------------------------------------------
    if not isinstance(cfg.types, dict):
        raise CFGValidationError(
            f"types must be a dict mapping var -> type tree, got "
            f"{type(cfg.types).__name__}",
            "types",
            cfg.types,
        )
    type_roots: set[str] = set()
    total_type_nodes = 0
    for var, tree in cfg.types.items():
        validate_identifier(var, "type tree variable name", "types")
        if var in type_roots:
            raise CFGValidationError(
                f"duplicate type root {var!r}", "types"
            )
        total_type_nodes += validate_type_tree(
            tree, (var,), f"types -> {var!r}", max_type_nodes
        )
        if total_type_nodes > max_type_nodes:
            raise CFGValidationError(
                f"type trees exceed the {max_type_nodes}-node limit", "types"
            )
        type_roots.add(var)

    def path_error_location(path: tuple[str, ...], loc: str) -> str:
        """Locate the first path component that cannot exist in the tree."""

        node: Any = cfg.types.get(path[0])
        if node is None:
            return f"{loc} -> path[0]"
        depth = 1
        for component in path[1:]:
            if not isinstance(node, dict) or component not in node:
                return f"{loc} -> path[{depth}]"
            node = node[component]
            depth += 1
        return loc

    def path_exists(path: tuple[str, ...]) -> bool:
        node: Any = cfg.types.get(path[0])
        if node is None:
            return False
        for component in path[1:]:
            if not isinstance(node, dict) or component not in node:
                return False
            node = node[component]
        return True

    # ---- blocks ----------------------------------------------------------
    if not isinstance(cfg.blocks, (tuple, list)):
        raise CFGValidationError(
            f"blocks must be a tuple/list, got {type(cfg.blocks).__name__}",
            "blocks",
        )
    if len(cfg.blocks) == 0:
        raise CFGValidationError("blocks must contain at least one block", "blocks")
    if len(cfg.blocks) > max_blocks:
        raise CFGValidationError(
            f"too many blocks: {len(cfg.blocks)} > limit {max_blocks}",
            "blocks",
        )

    validate_identifier(cfg.entry, "entry", "entry")

    block_objs: dict[str, Block] = {}
    instruction_lists: dict[str, list[Instruction]] = {}
    for block in cfg.blocks:
        if not isinstance(block, Block):
            raise CFGValidationError(
                f"each block must be a Block, got {type(block).__name__}", "blocks"
            )
        validate_identifier(block.id, "block id", "blocks")
        if block.id in block_objs:
            raise CFGValidationError(
                f"duplicate block id {block.id!r}", f"block {block.id!r}"
            )
        if not isinstance(block.instructions, (tuple, list)):
            raise CFGValidationError(
                "instructions must be a tuple/list",
                f"block {block.id!r} -> instructions",
            )
        if len(block.instructions) > max_instructions_per_block:
            raise CFGValidationError(
                f"block has {len(block.instructions)} instructions, limit is "
                f"{max_instructions_per_block}",
                f"block {block.id!r} -> instructions",
            )
        if not isinstance(block.successors, (tuple, list)):
            raise CFGValidationError(
                "successors must be a tuple/list of block ids",
                f"block {block.id!r} -> successors",
            )
        block_objs[block.id] = block
        instruction_lists[block.id] = []

    if cfg.entry not in block_objs:
        raise CFGValidationError(
            f"entry {cfg.entry!r} is not among the blocks", "entry", cfg.entry
        )

    # successors
    for bid, block in block_objs.items():
        seen_succ: set[str] = set()
        for i, s in enumerate(block.successors):
            validate_identifier(s, "successor id", f"block {bid!r} -> successors[{i}]")
            if s not in block_objs:
                raise CFGValidationError(
                    f"successor {s!r} does not name a block",
                    f"block {bid!r} -> successors[{i}]",
                    s,
                )
            if s in seen_succ:
                raise CFGValidationError(
                    f"duplicate successor {s!r}",
                    f"block {bid!r} -> successors[{i}]",
                )
            seen_succ.add(s)
        if cfg.entry in block.successors:
            raise CFGValidationError(
                f"edge into the entry block {cfg.entry!r} is illegal: the entry "
                "must have an empty (program-start) state on every reaching path",
                f"block {bid!r} -> successors",
            )

    # ---- instructions ----------------------------------------------------
    seen_loan_ids: set[str] = set()
    loan_records: dict[str, LoanRecord] = {}

    for bid, block in block_objs.items():
        for index, instr in enumerate(block.instructions):
            loc = f"block {bid!r} -> instruction {index} ({instr_kind_name(instr)})"
            if not isinstance(instr, _INSTRUCTION_TYPES):
                raise CFGValidationError(
                    "instruction must be one of Declare, Move, BorrowShared, "
                    f"BorrowMut, EndLoan, Read, Write; got {type(instr).__name__}",
                    loc,
                    instr,
                )
            if isinstance(instr, Declare):
                validate_identifier(instr.var, "declare variable", loc)
                if instr.var not in type_roots:
                    raise CFGValidationError(
                        f"declare of {instr.var!r} with no type tree entry",
                        loc,
                        instr.var,
                    )
                # Re-declaring (e.g. one declare on each branch arm) is
                # allowed: declare is idempotent in the may analysis.
            elif isinstance(instr, (Move, Read, Write)):
                path = validate_path(instr.path, loc)
                if len(path) > max_path_depth:
                    raise CFGValidationError(
                        f"path depth {len(path)} exceeds limit {max_path_depth}",
                        loc,
                    )
                if not path_exists(path):
                    raise CFGValidationError(
                        f"path {'.'.join(path)!r} does not exist in the type tree",
                        path_error_location(path, loc),
                    )
            elif isinstance(instr, (BorrowShared, BorrowMut)):
                validate_identifier(instr.loan_id, "loan_id", loc)
                if instr.loan_id in seen_loan_ids:
                    raise CFGValidationError(
                        f"loan_id {instr.loan_id!r} is not statically unique", loc
                    )
                seen_loan_ids.add(instr.loan_id)
                path = validate_path(instr.path, loc)
                if len(path) > max_path_depth:
                    raise CFGValidationError(
                        f"path depth {len(path)} exceeds limit {max_path_depth}",
                        loc,
                    )
                if not path_exists(path):
                    raise CFGValidationError(
                        f"path {'.'.join(path)!r} does not exist in the type tree",
                        path_error_location(path, loc),
                    )
                loan_records[instr.loan_id] = LoanRecord(
                    kind="shared" if isinstance(instr, BorrowShared) else "mut",
                    path=path,
                    block_id=bid,
                    instruction_index=index,
                )
            else:  # EndLoan
                validate_identifier(instr.loan_id, "loan_id", loc)
            instruction_lists[bid].append(instr)

    for bid, block in block_objs.items():
        for index, instr in enumerate(block.instructions):
            if isinstance(instr, EndLoan) and instr.loan_id not in loan_records:
                raise CFGValidationError(
                    f"end_loan of unknown loan_id {instr.loan_id!r}",
                    f"block {bid!r} -> instruction {index} (end_loan)",
                )

    return ValidatedProgram(
        cfg=cfg,
        blocks=block_objs,
        instructions={bid: tuple(seq) for bid, seq in instruction_lists.items()},
        loans=loan_records,
        types=dict(cfg.types),
        entry=cfg.entry,
    )
