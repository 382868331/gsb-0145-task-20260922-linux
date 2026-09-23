"""字段敏感的静态借用检查可复用模块（离线编译器前端用）。

公开接口：

* 数据结构：:class:`CFG`、:class:`Block` 及操作
  (:class:`Decl` :class:`Move` :class:`BorrowShared` :class:`BorrowMut`
  :class:`EndLoan` :class:`Read` :class:`Write`；v2 另增
  :class:`Init` :class:`Drop` :class:`Activate` :class:`EndScope`)。
* 命题公式：:class:`Formula`。
* 构造与校验：:func:`cfg_from_dict`、:func:`validate_cfg`。
* 分析：:func:`check`。v1 CFG 返回 :class:`CheckResult`（冻结语义）；
  v2 CFG（``"version": 2``）返回 :class:`CheckResultV2`，其确定冲突带
  经独立重放认证的 :class:`Evidence`，无法在预算内判定的候选进入
  ``status="unknown"`` 而不是误判 clean。
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
    Drop,
    EndLoan,
    EndScope,
    Activate,
    Formula,
    Init,
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
    check as _check_v1,
)
from .analyzer_v2 import (
    CheckResultV2,
    ConflictV2,
    WitnessV2,
    UnknownReason,
    check_v2,
)
from .validator import MAX_BLOCKS, MAX_BLOCKS_V2, MAX_OPS_PER_BLOCK, validate_cfg


def check(cfg, *, max_iterations=None, max_loop_unrolls=4,
          max_cert_states=20_000):
    """对 CFG 运行字段敏感静态借用检查（按 ``cfg.version`` 选择引擎）。

    * v1（默认）：冻结的 may-不动点分析，返回 :class:`CheckResult`，
      旧调用、旧异常类型与确定性结果完全保留。
    * v2（``version=2``）：条件限定不动点 + 两阶段借用 + 初始化/析构，
      候选冲突经 :mod:`borrow_check.replay` 的独立具体解释器认证，
      返回 :class:`CheckResultV2`。``max_loop_unrolls``（默认 4）与
      ``max_cert_states``（默认 20000）是认证搜索预算；预算内无法
      证实或排除的候选使 ``status`` 为 ``"unknown"``，绝不与
      ``"clean"`` 混淆。

    :raises CFGValidationError: 非法输入（携带 location）。
    :raises BudgetExhaustedError: 不动点搜索预算耗尽。
    """
    version = getattr(cfg, "version", 1)
    if version >= 2:
        return check_v2(
            cfg,
            max_iterations=max_iterations,
            max_loop_unrolls=max_loop_unrolls,
            max_cert_states=max_cert_states,
        )
    return _check_v1(cfg, max_iterations=max_iterations)


__all__ = [
    # model
    "Block",
    "BorrowMut",
    "BorrowShared",
    "CFG",
    "Decl",
    "Drop",
    "EndLoan",
    "EndScope",
    "Activate",
    "Formula",
    "Init",
    "Move",
    "Operation",
    "Path",
    "Read",
    "Write",
    "cfg_from_dict",
    "operation_from_dict",
    # validation
    "MAX_BLOCKS",
    "MAX_BLOCKS_V2",
    "MAX_OPS_PER_BLOCK",
    "validate_cfg",
    # analysis
    "CheckResult",
    "Conflict",
    "Witness",
    "all_type_paths",
    "check",
    # v2 analysis
    "CheckResultV2",
    "ConflictV2",
    "WitnessV2",
    "UnknownReason",
    "check_v2",
    # errors
    "BorrowCheckError",
    "BudgetExhaustedError",
    "CFGValidationError",
    "checked_int",
    "finite_number",
]
