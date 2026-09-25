"""字段敏感的静态借用检查可复用模块（离线编译器前端用）。

公开接口：

* 数据结构：:class:`CFG`、:class:`Block` 及七种操作
  (:class:`Decl` :class:`Move` :class:`BorrowShared` :class:`BorrowMut`
  :class:`EndLoan` :class:`Read` :class:`Write`)。
* 片段调用（本次新增）：:class:`Invoke`、:class:`Param`、:class:`Fragment`、
  :class:`Program`、:func:`program_from_dict`、:func:`fragment_from_dict`、
  :func:`check_program`，以及摘要 :class:`FragmentSummary` 与
  :func:`build_summaries`。
* 构造与校验：:func:`cfg_from_dict`、:func:`validate_cfg`、
  :func:`validate_program`。
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
    Fragment,
    Invoke,
    Move,
    Operation,
    Param,
    Path,
    Program,
    Read,
    Write,
    cfg_from_dict,
    fragment_from_dict,
    operation_from_dict,
    program_from_dict,
)
from .analyzer import (
    CheckResult,
    Conflict,
    Witness,
    all_type_paths,
    check,
    check_program,
)
from .fragments import (
    FragmentSummary,
    Precondition,
    InternalConflict,
    Template,
    TOp,
    build_templates,
    build_summaries,
    summarize_template,
    instantiate,
)
from .validator import (
    MAX_BLOCKS,
    MAX_OPS_PER_BLOCK,
    validate_cfg,
    validate_program,
)

__all__ = [
    # model
    "Block",
    "BorrowMut",
    "BorrowShared",
    "CFG",
    "Decl",
    "EndLoan",
    "Fragment",
    "Invoke",
    "Move",
    "Operation",
    "Param",
    "Path",
    "Program",
    "Read",
    "Write",
    "cfg_from_dict",
    "fragment_from_dict",
    "program_from_dict",
    "operation_from_dict",
    # validation
    "MAX_BLOCKS",
    "MAX_OPS_PER_BLOCK",
    "validate_cfg",
    "validate_program",
    # analysis
    "CheckResult",
    "Conflict",
    "Witness",
    "all_type_paths",
    "check",
    "check_program",
    # fragments
    "FragmentSummary",
    "Precondition",
    "InternalConflict",
    "Template",
    "TOp",
    "build_templates",
    "build_summaries",
    "summarize_template",
    "instantiate",
    # errors
    "BorrowCheckError",
    "BudgetExhaustedError",
    "CFGValidationError",
    "checked_int",
    "finite_number",
]
