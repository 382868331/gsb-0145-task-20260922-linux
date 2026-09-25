# borrow_check：字段敏感的静态借用检查模块（含操作片段调用）

为离线编译器前端实现的可复用库：在一个最多 50 个基本块的 CFG 上做
**字段敏感（field-sensitive）的 may 借用检查**，返回带**前驱见证链**的
冲突列表；并支持把反复拼接的借用操作抽成**无环操作片段（Segment）**，
对片段先形成“前置条件 + 状态变化”的摘要，再按实参路径替换接入主 CFG 的
不动点分析。纯 Python 标准库实现，无任何第三方依赖、无网络、无 IO 副作用。

## 环境

* Python **3.14.7**（使用 3.14 标准语法；仅依赖标准库 `dataclasses`、
  `collections`、`typing` 等）。
* 安装 Python 后**无需安装任何其他依赖**。
* Linux 原生运行；核心、测试、demo 全部离线可重复。

## 准确命令

```bash
# 必要测试（82 个用例：含 400 个随机 DAG 与独立参考对照，
# 以及片段调用与“真正展开片段”的逐项对照）
python -m unittest discover -s tests -v

# 固定输入演示：clean 正常结果 + 实际触发的拒绝/异常边界 + 片段调用
python demo.py
```

demo 实测耗时约 5 毫秒（不使用 sleep、不使用预录结果，全部现场计算，
远小于 90 秒）。

## 公开接口

```python
from borrow_check import (
    Program, Segment, Call,         # 操作片段与调用
    program_from_dict, check_program, summarize_segment, build_summaries,
    SegmentSummary, Precondition, SegmentEffect,
    CFG, Block,                     # 图
    Decl, Move, BorrowShared, BorrowMut, EndLoan, Read, Write,  # 七种操作
    cfg_from_dict, validate_cfg, validate_program, check,
    CheckResult, Conflict, Witness,  # 结果
    CFGValidationError, BudgetExhaustedError,
)
```

不含片段调用时，原有接口完全不变：`cfg_from_dict` + `check(cfg)`。
片段调用必须经 `Program`（主 CFG + 命名片段表）提供片段定义；直接对含
`call` 的裸 CFG 调 `check` 会得到可定位的校验错误。

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
* 片段内冲突时，`Conflict`/`Witness` 额外带 `segment`、`seg_block`、
  `seg_op`、`formal_path`：`block/op` 是**调用指令**在主 CFG 的位置，
  片段内坐标与形参路径另由这些字段给出（出错见证同时给调用位置与片段内
  路径）；`path` 始终是实参替换后的调用方真实路径。
* `result.iterations` 为到达不动点前的块处理次数。
* `result.explain()` 渲染人类可读的冲突来源链（demo 使用）。

## 操作片段调用（本次新增）

### 概念

* **片段 `Segment`**：由**已有七种指令**（不含 `declare`、不含嵌套
  `call`）组成的**无环**小 CFG，只有**显式路径形参**（如 `p`、`q`）和
  **局部 loan**。片段块数受主 CFG 同样的 1..50 上限约束，且要求所有块从
  `entry` 可达、图无环。片段内路径必须以某个形参名为根，例如
  `("p", "x")`；片段**没有记录树**，字段存在性在每次调用按实参替换后校验。
* **调用图无环、无递归**：片段内禁止 `call`，调用图天然只有一层。
* **调用 `Call`**：`segment` 给出片段名，`args` 为
  `(形参名, 实参路径)` 元组，必须为片段的**全部**形参各绑定一次（不缺、
  不重、无多余）。两个形参可以指向**同一记录的不同字段**，也可以别名
  （相等或父子重叠）——分析**不默认形参互不重叠**。

```python
Program(cfg=CFG(...), segments={"swap_borrow": Segment(
    params=("p", "q"),
    blocks=[Block(ops=[BorrowMut("Lp", ("p",)), BorrowMut("Lq", ("q",)),
                       EndLoan("Lp"), EndLoan("Lq")])],
    successors=[[]])})
# 主 CFG 操作：Call("swap_borrow", (("p", ("r", "a")), ("q", ("r", "b"))))
```

dict 形式：顶层 `{"cfg": {...主CFG...}, "segments": {名字: 片段}}`，
用 `program_from_dict` 构造、`check_program` 检查；片段操作用字符串形参
名作路径根（如 `{"op": "borrow_mut", "loan_id": "Lp", "path": ["p"]}`）。

### 摘要：前置条件 + 状态变化

每个片段在分析前由 `summarize_segment` **一次性**形成
`SegmentSummary`：

* `topo` / `exit_blocks`：无环拓扑序与出口块；
* `preconditions`：每个片段操作对到达状态的要求（`not_moved`、
  `no_overlapping_loan`、`no_overlapping_mut_loan`），带片段内坐标与
  符号（形参）路径；
* `effects`：声明的写回状态变化——`move`（子树移动）与
  `write(reinit=True)`（只恢复该路径子树）；
* `local_loans` / `loans_scoped_out`：片段局部借用。

摘要用符号形参根描述路径，**不预设形参替换后是否重叠**。

### 接入主 CFG 不动点

主 CFG 的 worklist 不动点传输遇到 `call` 时，在**该调用点的当前状态**上：

1. 建形参→实参替换 `("p","x") → arg_p + ("x",)`；
2. 片段局部 loan 换发**本次调用的全新身份**（含调用序号），片段内
   `borrow/end_loan/read/write/move` 全部用替换后的真实路径，沿**拓扑序**
   逐块执行一次（无环 ⇒ 不求片段内不动点），汇合块做 may 并集；
3. 片段退出时**硬清除**本次调用的全部局部 loan——**局部借用在片段退出时
   结束**；移动与 reinit 写回效果**并入调用方状态**（留给调用方）；
4. 调用方在调用前开启、未结束的借用跨越调用仍活跃。

形参间的重叠（同字段、父子别名）在替换后的真实路径上判定，因此别名冲突
被自然发现，而不同字段独立访问通过。局部 loan 在调用块出口前被清除、不
进入块间状态，所以主不动点的步数上界 `E·(P+L) + n` 不因片段而增大
（L 只数主 CFG loan）。

### 出错见证

片段内冲突的 `Conflict.block/op` 是调用指令位置，`seg_block/seg_op` 是
片段内位置，`formal_path` 是片段内形参路径，`path` 是实参替换后的真实
路径；片段内诞生的 move/loan 见证同样带这些字段。`explain()` 同时打印
调用位置与片段内路径。

## 算法

* 每块入口/出口各维护一个抽象状态：
  * 可能已移动路径：**子树移动反链**（`move` 产生，压自身及子孙）+
    **精确移动集合**（只压自身，reinit 拆分祖先移动时产生）；
  * 可能仍活跃借用：`loan_id` 集合（may：分支并集，故包含“可能未结束”）。
* 分支合并 = 两者并集；worklist 单调迭代到**不动点**（循环求不动点），
  对任意含环 CFG 都终止，终止性不依赖递归深度或截断。
* 变更判定建立在状态的**语义外延**（被压制路径集合 ∪ 活跃 loan）上；
  可证明块处理次数上界为 `E·(P+L+1) + n`（E=边数，P=类型树路径数，
  L=主 CFG loan 数，n=块数；`+1` 计入每块出口外延的首次记录——回边会把
  已处理块重新入队）。`check` 对只读输入，失败不留部分变更。
* 冲突在不动点上做一次有序终扫描权威收集，故结果与 worklist 调度次序无关。
* 片段调用在每个调用点沿片段拓扑序执行**一次**（片段无环，无片段内不动点），
  是对“真正展开成主 CFG 块”的精确模拟；局部 loan 不出调用块，主不动点的
  格与步数上界不变。

## 输入上限（接口边界）

* 主 CFG 块数：**1..50**；单块操作数：≤ 10 000；
* 片段块数：**1..50**，必须无环且全部块从 `entry` 可达；
  形参数：≥1，形参名非空、互不重复；每次调用必须为全部形参各绑定一次；
* 记录树节点总数：≤ 100 000；
* 路径段、变量名、loan_id、片段名、形参名：非空字符串；每个 loan_id 仅一个
  borrow 点（片段局部 loan 在片段内唯一，调用时另换新身份）；
* 后继下标必须在范围内、不可重复；`entry` 必须合法。
* 片段内路径形参后缀（如 `p.x`）替换为实参后，必须在实参记录类型中存在，
  否则在该调用的 `...ops[i].args` 报可定位错误。

## 错误语义

* 非法输入抛 `CFGValidationError`，带可定位字符串 `location`
  （如 `blocks[2].ops[0].path`、片段 `segments.s.blocks[0].ops[1].path`、
  调用实参 `blocks[0].ops[1].args`），**不静默纠正**；`cfg_from_dict` /
  `program_from_dict` 构造时即完成全部结构与语义校验，失败时不返回对象。
* 片段自身的非法之处（有环、含 declare/嵌套 call、路径根不是形参、不可达
  块、局部 loan 重复或 end_loan 未开启等）定位到 `segments.<名>...`；
  调用与片段签名不符（片段未定义、缺/重/多余形参）或实参替换后越界，定位
  到主 CFG 的调用点。
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
* `tests/test_segments.py`：片段四个验收场景（**同字段双可变借用、父子路径
  别名、不同字段独立访问、某分支移动后返回**），外加局部 loan 退出即结束、
  重复调用不串用 loan 身份、调用方借用跨调用存活、reinit 写回、片段坐标
  见证，以及数据类原生 API。
* `tests/test_segment_inline.py`：固定种子（20260925 / 20260926）确定性生成
  含片段调用的小程序，对每个程序用 `tests/_inline.py` **真正展开**成普通
  CFG（克隆片段块、出口补局部 `end_loan`、多出口汇入同一续延块、loan 按
  调用重命名），再由原 `check` 检查；与 `check_program` 的冲突集合（含片段
  内坐标）逐项一致。分别覆盖主 CFG 为 DAG 与含回边/自环（片段无环、摘要
  接入主不动点）两种情形。`_inline.py` 是纯 dict 变换，**不 import 被测
  核心**，展开规模限定在 50 块接口上限内。
* `tests/test_segment_validation.py`：摘要内容（前置条件/写回效果/拓扑）
  与片段、调用的非法输入定位。

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
* 片段摘要只描述**本次新增**的复用能力，刻意不做成通用编译器：片段无环、
  无嵌套调用/递归、无声明、无返回值，只有显式路径形参与局部 loan。摘要
  给出稳定的前置条件/写回效果清单，权威结论以每次调用按实参替换后的
  实例化执行为准，因此**不假设形参互不重叠**——别名在真实路径上判定。
* 片段内冲突与写回沿主 CFG 状态在调用点就地计算，而不是预先把符号结果
  跨调用缓存：保证重复调用（尤其在不动点里同一块被多次重算）不会串用上一
  次调用的局部 loan 身份或分支状态。

## 已知限制（本题数学/协议子集，不宣称行业标准兼容）

* 不是完整 Rust 借用检查器：无生命周期推断、无 NLL/place 上下文、
  无闭包/函数间分析、无解构模式、无投影外的别名模型；路径就是记录树
  上的字段投影，不模拟原始指针/索引/数组元素。
* 块是扁平操作序列，不建模表达式树；一次操作只作用于一个固定路径。
* `end_loan` 是词法操作，不推断隐式结束点（NLL）。
* may 分析会报告真实编译器可能接受的分支组合（例如互斥分支分别移动不同
  字段后按并集处理），这是题面指定并集语义的直接结果。
* 片段是**结构化复用**而非通用函数：无递归、无嵌套调用、无返回值、无异常/
  发散路径建模；片段内多出口按 may 并集回到同一调用点。片段不携带记录树，
  形参只按字段投影替换，不做类型多态、索引或指针别名；形参是否别名完全由
  调用点实参路径决定。
* 片段局部 loan 的“退出即结束”是**词法作用域**约定（在每个无环出口硬
  清除），不推断 NLL 隐式结束点。

## 文件

```
borrow_check/errors.py     # 异常类型、NaN/Infinity/bool 数值守卫
borrow_check/model.py      # 数据结构（含 Segment/Program/Call）、dict 构造
borrow_check/validator.py  # 语义校验（含支配集不动点、片段/调用校验）
borrow_check/summaries.py  # 片段摘要：前置条件、写回效果、无环拓扑
borrow_check/analyzer.py   # 核心：反链+精确移动、worklist 不动点、调用实例化、见证
demo.py                    # 固定输入演示（含片段调用与真实展开逐项比较）
tests/                     # 必要测试 + 独立参考实现 + 片段真实展开对照
```
