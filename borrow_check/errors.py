"""错误类型与基础数值校验。

错误语义约定（见 README）：

* ``CFGValidationError``：CFG 结构或内容非法。错误总是携带 ``location``
  （可定位到字段/块/操作下标），校验在任何分析状态产生之前完成，
  因此失败不会留下部分变更（``check`` 对输入只读）。
* ``BudgetExhaustedError``：不动点迭代的**可证明步数上界**被超过。
  本分析在有限格上单调迭代，给定上限时该错误不可能发生；它与
  “分析完成且无冲突”（``CheckResult.clean is True``）是严格分开的两条路径。
"""

from __future__ import annotations

import math
from typing import Optional


class BorrowCheckError(Exception):
    """本库所有异常的公共基类。"""


class CFGValidationError(BorrowCheckError):
    """非法 CFG 输入。

    :param message: 人类可读的错误说明。
    :param location: 可定位字符串，例如 ``blocks[2].ops[0].path``；
        无法精确定位时为 ``None``。
    """

    def __init__(self, message: str, location: Optional[str] = None) -> None:
        self.location = location
        if location:
            super().__init__(f"[{location}] {message}")
        else:
            super().__init__(message)


class BudgetExhaustedError(BorrowCheckError):
    """不动点搜索预算耗尽（区别于“证明无冲突”）。"""


def checked_int(name: str, value: object, *, min_value: Optional[int] = None) -> int:
    """整数参数校验：拒绝 bool、非整数、NaN/Infinity。

    :raises TypeError: ``value`` 不是 ``int``（``bool`` 虽是 ``int`` 子类也拒绝），
        或为 ``float``（包含 NaN/Infinity，浮点不属于本接口的整数参数）。
    :raises ValueError: 整数存在但越界。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            if math.isnan(value):
                raise TypeError(f"{name!r} 必须是整数，不能是 NaN")
            if math.isinf(value):
                raise TypeError(f"{name!r} 必须是整数，不能是 Infinity")
            raise TypeError(f"{name!r} 必须是整数，不能是浮点数 {value!r}")
        raise TypeError(f"{name!r} 必须是整数，得到 {type(value).__name__}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name!r} 必须 >= {min_value}，得到 {value}")
    return value


def finite_number(name: str, value: object) -> float:
    """实数参数校验：拒绝 bool、NaN、±Infinity 与非数值。

    本库的 CFG 数据模型不接受任何浮点数；该辅助函数供需要真实数值参数的
    扩展点使用，并保证 NaN/Infinity 永远不会被静默接受。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name!r} 必须是实数，得到 {type(value).__name__}")
    fvalue = float(value)
    if math.isnan(fvalue):
        raise ValueError(f"{name!r} 不能是 NaN")
    if math.isinf(fvalue):
        raise ValueError(f"{name!r} 不能是 Infinity")
    return fvalue
