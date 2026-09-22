"""字段敏感的静态借用检查可复用模块（离线编译器前端用）。

公开接口：

* 数据结构：:class:`CFG`、:class:`Block` 及七种操作
  (:class:`Decl` :class:`Move` :class:`BorrowShared` :class:`BorrowMut`
  :class:`EndLoan` :class:`Read` :class:`Write`)。
* 构造与校验：:func:`cfg_from_dict`、:func:`validate_cfg`。
* 分析：:func:`check`，返回 :class:`CheckResult`（含 :class:`Conflict`
  与前驱见证 :class:`Witness`）。
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
    BorrowMut,
    BorrowShared,
    CFG,
    Decl,
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
from .validator import MAX_BLOCKS, MAX_OPS_PER_BLOCK, validate_cfg

__all__ = [
    # model
    "Block",
    "BorrowMut",
    "BorrowShared",
    "CFG",
    "Decl",
    "EndLoan",
    "Move",
    "Operation",
    "Path",
    "Read",
    "Write",
    "cfg_from_dict",
    "operation_from_dict",
    # validation
    "MAX_BLOCKS",
    "MAX_OPS_PER_BLOCK",
    "validate_cfg",
    # analysis
    "CheckResult",
    "Conflict",
    "Witness",
    "all_type_paths",
    "check",
    # errors
    "BorrowCheckError",
    "BudgetExhaustedError",
    "CFGValidationError",
    "checked_int",
    "finite_number",
]
