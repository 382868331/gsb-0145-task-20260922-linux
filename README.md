# borrow_check：字段敏感的静态借用检查模块

为离线编译器前端实现的可复用库：在一个最多 50 个基本块的 CFG 上做
**字段敏感（field-sensitive）的 may 借用检查**，返回带**前驱见证链**的
冲突列表。纯 Python 标准库实现，无任何第三方依赖、无网络、无 IO 副作用。

## 环境

* Python **3.14.7**（使用 3.14 标准语法；仅依赖标准库 `dataclasses`、
  `collections`、`typing` 等）。
* 安装 Python 后**无需安装任何其他依赖**。
* Linux 原生运行；核心、测试、demo 全部离线可重复。

## 准确命令

```bash
# 必要测试（51 个用例，含 400 个随机 DAG 与独立参考的对照）
python -m unittest discover -s tests -v

# 固定输入演示：一个 clean 的正常结果 + 实际触发的拒绝与异常边界
python demo.py
```

demo 实测耗时约 1 毫秒（不使用 sleep、不使用预录结果，全部现场计算）。

## 公开接口

```python
from borrow_check import (
    CFG, Block,                       # 图
    Decl, Move, BorrowShared, BorrowMut, EndLoan, Read, Write,  # 七种操作
    cfg_from_dict, validate_cfg, check,
    CheckResult, Conflict, Witness,  # 结果
    CFGValidationError, BudgetExhaustedError,
)
```

### 路径与记录树

* 路径是字段名元组，如 `("user", "profile", "name")`；dict 输入中写作
  `["user", "profile", "name"]`。空路径非法（至少含根变量名）。
* 变量是**已给定的有限记录树**：`declare(var, ty)`，其中 `ty` 为
  `{字段名: 子类型}` 的 dict，叶子为 `None`。所有访问路径必须在树中存在。
* **路径重叠**：一方为另一方前缀（含相等）。`a.x` 与 `a.y` 不相交；
  `a` 与 `a.x`、`a.x` 与 `a.x.z` 重叠。

### 操作语义（每块内有序执行）

| 操作 | 含义与检查 |
|---|---|
| `declare(var, ty)` | 声明根变量一次；必须在 CFG 上**支配**每个使用点 |
| `move(path)` | 移动路径；若该路径可能已移动或与活跃借用重叠则冲突 |
| `borrow_shared(loan_id, path)` | 开共享借用；与路径重叠的活跃 **mut** 借用冲突，多个 shared 可共存 |
| `borrow_mut(loan_id, path)` | 开可变借用；与路径重叠的**任何**活跃借用冲突 |
| `end_loan(loan_id)` | 结束借用；loan_id 必须存在 |
| `read(path)` | 路径不得可能已移动；不得有重叠的活跃可变借用（共享借用允许读） |
| `write(path, reinit=False)` | 普通写不得与任何活跃借用重叠、路径不得可能已移动；`reinit=True` 为字段重组写 |

约定：

* **每个 `loan_id` 静态唯一**，对应唯一 borrow 点，其模式（shared/mut）
  与固定路径在该点确定。
* **移动父路径使所有子路径不可用**（方向性：祖先 → 子孙）。移动子路径
  **不**使父路径或兄弟字段不可用——不同字段可独立移动/借用。借用冲突
  使用的是**对称重叠**（祖先与子孙任一方借了都冲突）。两套关系刻意区分。
* **重新赋值字段只恢复该字段**：`write(path, reinit=True)` 恢复 `path`
  子树的可用性；若此前移动的是祖先路径，则祖先对象本身仍可能已移动
  （记为“精确移动”，只压自身），通往 `path` 沿途的兄弟字段仍不可用。
  普通 `write`（`reinit=False`）不恢复任何东西。

### check

```python
result = check(cfg, *, max_iterations=None) -> CheckResult
```

* `result.clean: bool` —— 无冲突（“证明无冲突”路径）。
* `result.conflicts: list[Conflict]`，按 `(块, 操作)` 有序；每个
  `Conflict` 含 `kind`、`block`、`op`、`op_name`、`path`、`detail`
  和 `witnesses: tuple[Witness, ...]`。
* `Witness` 是前驱链：诞生点 `(origin_block, origin_op)`（move 或 borrow
  操作）、作用路径/loan_id/模式、以及从诞生块到冲突块的 `block_chain`。
  见证链经过环时规范化为简单路径（may 分析下删环仍为合法见证），
  **不靠遍历深度截断**。
* 冲突种类：`use_of_moved`、`move_while_borrowed`、`borrow_conflict`、
  `read_while_mut_borrowed`、`write_while_borrowed`。
* `result.iterations` 为到达不动点前的块处理次数。
* `result.explain()` 渲染人类可读的冲突来源链（demo 使用）。

## 算法

* 每块入口/出口各维护一个抽象状态：
  * 可能已移动路径：**子树移动反链**（`move` 产生，压自身及子孙）+
    **精确移动集合**（只压自身，reinit 拆分祖先移动时产生）；
  * 可能仍活跃借用：`loan_id` 集合（may：分支并集，故包含“可能未结束”）。
* 分支合并 = 两者并集；worklist 单调迭代到**不动点**（循环求不动点），
  对任意含环 CFG 都终止，终止性不依赖递归深度或截断。
* 变更判定建立在状态的**语义外延**（被压制路径集合 ∪ 活跃 loan）上；
  可证明块处理次数上界为 `E·(P+L) + n`（E=边数，P=类型树路径数，
  L=loan 数，n=块数）。`check` 对只读输入，失败不留部分变更。
* 冲突在不动点上做一次有序终扫描权威收集，故结果与 worklist 调度次序无关。

## 输入上限（接口边界）

* 块数：**1..50**；单块操作数：≤ 10 000；
* 记录树节点总数：≤ 100 000；
* 路径段、变量名、loan_id：非空字符串；每个 loan_id 仅一个 borrow 点；
* 后继下标必须在范围内、不可重复；`entry` 必须合法。

## 错误语义

* 非法输入抛 `CFGValidationError`，带可定位字符串 `location`
  （如 `blocks[2].ops[0].path`），**不静默纠正**；`cfg_from_dict`
  构造时即完成全部结构与语义校验，失败时不返回对象。
* 整数参数（块下标、`entry`、`max_iterations` 等）拒绝 `bool`
  （`True` 不会被当作 1）、拒绝非整数与越界值。
* 辅助 `finite_number(name, value)` 拒绝 `bool`、`NaN`、`±Infinity` 与非数值；
  `checked_int(name, value, min_value=...)` 拒绝 `bool`/浮点（含 NaN/Infinity）。
* **预算耗尽 ≠ 证明无解/无冲突**：默认预算是上述可证明上界，合法输入
  不会耗尽；显式传入更小的 `max_iterations` 时抛 `BudgetExhaustedError`，
  与 `clean=True` 是两条严格分开的路径。

### 浮点与误差口径

本模块的数据模型与分析**不包含任何浮点计算**（路径、集合、迭代次数全部
为离散整数/字符串），因此不存在数值误差或容差问题；任何实数入口都通过
`finite_number` 拒绝 NaN/Infinity。测试中出现的随机数只用固定种子的
标准库 `random.Random` 做**结构性**枚举选择，不参与被比较的期望值。

## 测试如何得到期望值（独立性）

* `tests/test_scenarios.py`：四个验收场景（互不相交字段、循环中未结束
  借用、单分支移动后读取、父对象重组）与方向移动/reinit 边界，期望值按
  题面契约手写。
* `tests/test_dag_reference.py`：固定种子（20260922）确定性生成 400 个
  小型前向边 CFG（结构性无环），对照 `tests/_reference.py` 的**独立参考
  实现**：类型树全路径位向量 + DAG 全块序列枚举解释。该参考**不 import
  被测核心**，与核心的反链不动点是两条独立实现路线；两者的冲突集合
  `(kind, block, op)` 必须逐例一致。
* `tests/test_validation.py`：非法输入定位、数值守卫、预算/无冲突分离。

## 设计取舍

* may 语义（可能已移动 / 可能仍活跃取并集）：宁可误报也要保证健全，
  与题面“合并取并集”一致；不做 must 分析或路径条件精化。
* `declare` 要求在 CFG 上支配使用点（经典支配集不动点判定），避免在
  “声明可能尚未执行”的状态上分析；不可达块豁免。
* 字段重组用“精确移动 + 兄弟子树移动”拆分祖先移动，严格实现
  “重新赋值字段只恢复该字段”，包括“唯一字段被重组后根对象本身仍可能
  已移动”的边界。
* 冲突检测是健全性报告：即使操作本身已构成冲突，其声明的状态效果仍按
  语义应用（如 reinit 仍恢复字段），使后续冲突能被完整枚举，不做首错即停。

## 已知限制（本题数学/协议子集，不宣称行业标准兼容）

* 不是完整 Rust 借用检查器：无生命周期推断、无 NLL/place 上下文、
  无闭包/函数间分析、无解构模式、无投影外的别名模型；路径就是记录树
  上的字段投影，不模拟原始指针/索引/数组元素。
* 块是扁平操作序列，不建模表达式树；一次操作只作用于一个固定路径。
* `end_loan` 是词法操作，不推断隐式结束点（NLL）。
* may 分析会报告真实编译器可能接受的分支组合（例如互斥分支分别移动不同
  字段后按并集处理），这是题面指定并集语义的直接结果。

## 文件

```
borrow_check/errors.py     # 异常类型、NaN/Infinity/bool 数值守卫
borrow_check/model.py      # 数据结构、dict->CFG 构造与结构校验
borrow_check/validator.py  # 语义校验（含支配集不动点）
borrow_check/analyzer.py   # 核心：反链+精确移动、worklist 不动点、见证
demo.py                    # 固定输入演示
tests/                     # 必要测试 + 独立参考实现
```
