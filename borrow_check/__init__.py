"""字段敏感的静态借用检查可复用模块（离线编译器前端用）。

公开接口：

* 数据结构：:class:`CFG`、:class:`Block` 及操作
  (:class:`Decl` :class:`Move` :class:`BorrowShared` :class:`BorrowMut`
  :class:`BorrowReserve` :class:`BorrowActivate` :class:`EndLoan`
  :class:`Read` :class:`Write` :class:`Drop`)。
* 构造与校验：:func:`cfg_from_dict`、:func:`validate_cfg`。
* 分析：:func:`check`（``lifecycle=False`` 为 v1 may 分析；
  ``lifecycle=True`` 为 v2 生命周期分析），返回 :class:`CheckResult`
  （含 :class:`Conflict`、前驱见证 :class:`Witness` 与可执行证据
  :class:`ExecutableEvidence`）。
* 错误：:class:`CFGValidationError`、:class:`BudgetExhaustedError`。

只用 Python 3.14 标准库。
"""

from .errors import (
    BorrowCheckError,
    BudgetExhaustedError,
    CFGValidationError,
    checked_int,
    finite_number,
)
from .model import (
    Block,
    BorrowActivate,
    BorrowMut,
    BorrowReserve,
    BorrowShared,
    CFG,
    Decl,
    Drop,
    EndLoan,
    Move,
    Operation,
    Path,
    Read,
    Write,
    cfg_from_dict,
    operation_from_dict,
)
from .analyzer import (
    CheckResult,
    Conflict,
    Witness,
    all_type_paths,
    check,
)
from .lifecycle import (
    ExecutableEvidence,
    ReplayStep,
    check_lifecycle,
)
from .formula import (
    MAX_CONDITIONS,
    And,
    Atom,
    Expr,
    Not,
    Or,
    TruthTables,
    evaluate as evaluate_formula,
    expr_from_dict,
    render_expr,
)
from .validator import (
    MAX_BLOCKS,
    MAX_BLOCKS_LIFECYCLE,
    MAX_OPS_PER_BLOCK,
    validate_cfg,
)

__all__ = [
    # model
    "Block",
    "BorrowActivate",
    "BorrowMut",
    "BorrowReserve",
    "BorrowShared",
    "CFG",
    "Decl",
    "Drop",
    "EndLoan",
    "Move",
    "Operation",
    "Path",
    "Read",
    "Write",
    "cfg_from_dict",
    "operation_from_dict",
    # formula
    "MAX_CONDITIONS",
    "And",
    "Atom",
    "Expr",
    "Not",
    "Or",
    "TruthTables",
    "evaluate_formula",
    "expr_from_dict",
    "render_expr",
    # validation
    "MAX_BLOCKS",
    "MAX_BLOCKS_LIFECYCLE",
    "MAX_OPS_PER_BLOCK",
    "validate_cfg",
    # analysis
    "CheckResult",
    "Conflict",
    "Witness",
    "ExecutableEvidence",
    "ReplayStep",
    "all_type_paths",
    "check",
    "check_lifecycle",
    # errors
    "BorrowCheckError",
    "BudgetExhaustedError",
    "CFGValidationError",
    "checked_int",
    "finite_number",
]
