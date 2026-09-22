"""Field-sensitive static borrow checker (offline compiler frontend module).

Public surface:

* Data model: :class:`CFG`, :class:`Block`, the seven instruction types,
  :class:`Conflict`, :class:`Witness`, :class:`CheckResult`.
* Entry point: :func:`borrowcheck.checker.check`.
* Errors: :class:`CFGValidationError` (malformed input, locatable) and
  :class:`BudgetExceededError` (search budget exhausted, distinct from a
  proven result).
"""

from .model import (
    Block,
    BorrowMut,
    BorrowShared,
    CFG,
    CheckResult,
    Conflict,
    ConflictCode,
    Declare,
    EndLoan,
    LoanKind,
    Location,
    Move,
    Read,
    Write,
)
from .errors import BorrowCheckError, BudgetExceededError, CFGValidationError
from .checker import (
    MAX_BLOCKS,
    MAX_INSTRUCTIONS_PER_BLOCK,
    MAX_PATH_DEPTH,
    MAX_TYPE_NODES,
    check,
)

__version__ = "1.0.0"

__all__ = [
    "Block",
    "BorrowMut",
    "BorrowShared",
    "CFG",
    "CheckResult",
    "Conflict",
    "ConflictCode",
    "Declare",
    "EndLoan",
    "LoanKind",
    "Location",
    "Move",
    "Read",
    "Write",
    "BorrowCheckError",
    "BudgetExceededError",
    "CFGValidationError",
    "MAX_BLOCKS",
    "MAX_INSTRUCTIONS_PER_BLOCK",
    "MAX_PATH_DEPTH",
    "MAX_TYPE_NODES",
    "check",
]
