# borrow_check：字段敏感的静态借用检查模块

为离线编译器前端实现的可复用库：在一个最多 50 个基本块的 CFG 上做
**字段敏感（field-sensitive）的 may 借用检查**，返回带**前驱见证链**的
冲突列表。纯 Python 标准库实现，无任何第三方依赖、无网络、无 IO 副作用。

**本次新增：操作片段调用（fragment invocation）。** 调用方反复拼接同一段
借用操作时，可把这段已有指令抽成**无环小 CFG 片段**，只含显式路径形参与
局部 loan；调用图也无环（无递归）。片段先形成**带前置条件与状态变化的
摘要**，再在主 CFG 的不动点分析中**按实参路径替换**为具体无环 CFG 单趟
解释。不假设形参互不重叠：两个形参可绑到同一记录的不同字段，也可别名。

## 环境

* Python **3.14.7**（使用 3.14 标准语法；仅依赖标准库 `dataclasses`、
  `collections`、`typing` 等）。
* 安装 Python 后**无需安装任何其他依赖**。
* Linux 原生运行；核心、测试、demo 全部离线可重复。

## 准确命令

```bash
# 必要测试（70 个用例：含 400 个随机主 DAG、300 个随机片段程序的独立对照）
python -m unittest discover -s tests -v

# 固定输入演示：普通 clean/拒绝/异常边界 + 片段摘要与实参替换对照
python demo.py
```

demo 实测耗时约 2–3 毫秒（远小于 90 秒现场计算预算；不使用 sleep、
不使用预录结果，全部现场计算）。

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

### 片段调用（本次新增）

```python
from borrow_check import (
    Program, Fragment, Param, Invoke,
    program_from_dict, check_program, build_summaries,
)

program = program_from_dict({
  "cfg": {
    "blocks": [
      {"ops": [
        {"op": "declare", "var": "r", "ty": {"x": None, "y": None}},
        {"op": "invoke", "fragment": "Touch",
         "args": {"p": ["r", "x"], "q": ["r", "y"]}},   # 不同字段
      ]},
    ],
    "successors": [[]],
  },
  "fragments": [{
    "name": "Touch",
    "params": [{"name": "p", "ty": None}, {"name": "q", "ty": None}],
    "blocks": [{"ops": [
      {"op": "borrow_mut", "loan_id": "lp", "path": ["p"]},
      {"op": "borrow_mut", "loan_id": "lq", "path": ["q"]},
      {"op": "end_loan", "loan_id": "lp"},
      {"op": "end_loan", "loan_id": "lq"},
    ]}],
    "successors": [[]],
  }],
})
result = check_program(program)
summaries = build_summaries(program)   # 形参命名空间摘要
```

* **片段**：`Fragment(name, params, blocks, successors, entry)`。`params`
  是显式路径形参 `Param(name, ty)`（至少一个），形参在片段内作为路径根，
  其 `ty` 是形参记录形状（dict/None），片段内路径必须落在形状内。片段块
  内只有七种已有指令（**不含 declare**），loan_id **局部**于该片段。
* **无环**：片段 CFG 的边只准连向**更大下标**（结构性无环）；片段间调用
  图也必须无环（自调用/互相递归都拒绝），因此可自底向上完全内联，且不
  引入递归函数。
* **invoke**：`Invoke(fragment, args={形参名: 调用方实参路径})`，实参
  必须齐全且无多余；实参路径的实际记录形状必须**覆盖**形参形状。两个形参
  可绑到同一记录的不同字段（`r.x`、`r.y`），也可绑到互相重叠/别名的路径
  （`r` 与 `r.x`）——分析**不假设形参互不重叠**。
* **摘要**（`build_summaries` / `FragmentSummary`，在**形参命名空间**）：
  每个片段内操作点的**前置条件**（入口可能已移动 / 入口活跃借用重叠）、
  与入口无关的**内部冲突**、**出口状态变化**（片段体自身新增的子树/精确
  移动，以及入口移动条目穿过片段含分支并集与 reinit 拆分的存活表）。
* **接入主不动点**：每个 invoke 点把片段模板**按实参路径替换**成主命名
  空间下的具体无环 CFG，以调用点当前状态为入口**单趟**解释（may 合并），
  因此别名情形与“真正展开片段”逐项一致；主 CFG 的 worklist 不动点、
  见证、预算上界机制不变。片段内是无环单趟，不增加主格迭代层数。
* **效果归属**：**局部 loan 在片段退出时一律结束**，不逃逸到调用方；
  **移动与写回（reinit）效果返回调用方**。调用方持有的活跃借用按对称
  重叠继续约束片段内访问。
* **错误见证同时给两处位置**：`Conflict.block/op` 锚定主 CFG 的调用位置；
  `Witness.frag_chain` 给出片段内 label 链
  `(片段名, 片段块, 片段操作)`（嵌套调用向外增长），片段局部借用另在
  `Witness.frag_loan` 给出 `(label 链, 局部 loan_id, mode)`；`detail`
  文本也标注片段内冲突位置。
* **重复调用的身份隔离**：每次调用都为片段 loan 生成带调用点与单调调用
  序号的新鲜运行时 id，重复调用不复用上一次调用的 loan 身份。
* 普通 `check` 不接受 `invoke`（抛可定位错误）；含调用必须走
  `program_from_dict` + `check_program`。无片段程序上 `check_program`
  与 `check` 等价。

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
* **片段处理**：调用图无环，故自底向上把每个片段内联成形参命名空间的
  *模板*（前向 DAG，指令带片段 label 链，loan 用诞生 label+局部 id 标识）。
  主分析遇到 invoke 时把模板**按实参路径替换**为具体无环 CFG 单趟解释：
  这正是“真正展开片段”的数据流，故形参别名（含父子重叠）与展开逐项一致；
  片段内为无环单趟，不向主格引入新的不动点迭代层，主上界
  `E·(P+L) + n` 仍然成立。局部 loan 在出口删除，移动/写回效果回传调用方。

## 输入上限（接口边界）

* 主 CFG 块数：**1..50**；单块操作数：≤ 10 000；
* 片段块数：**1..50**；片段完全内联后的模板操作总数：≤ **100 000**
  （接口边界，防止嵌套内联指数膨胀；测试与 demo 的小程序远小于此）；
* 记录树节点总数：≤ 100 000；
* 路径段、变量名、loan_id、片段名、形参名：非空字符串；每个 loan_id
  在其作用域（主 CFG 或单个片段）内仅一个 borrow 点；
* 后继下标必须在范围内、不可重复；`entry` 必须合法；片段边只准连向
  更大下标（无环），片段调用图必须无环。

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
* `tests/test_fragments.py`：片段四场景（同字段双可变借用、父子路径别名、
  不同字段独立访问、某分支移动后返回）、局部 loan 退出结束、调用方状态
  触发片段内前置条件、嵌套片段 label 链、重复调用 loan 身份新鲜、出口
  状态与手工展开逐项比较，以及环/缺实参/形状不覆盖/declare 等非法输入。
* `tests/test_fragment_expansion.py`：固定种子（20260925）确定性生成
  300 个小型片段程序，用**独立的 dict 级内联器**（不 import 被测
  `fragments` 模块）把片段展开成普通前向 CFG，三路对照：独立参考
  位向量枚举、核心 `check` 跑展开 CFG、`check_program` 跑片段程序；
  按 `(kind, 具体路径)` 集合一致（位置在三种表示中不同，路径同源可比）。
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
* 片段摘要在**形参命名空间**给出前置条件与状态变化，但**权威冲突判定
  不读摘要的“形参互不相交”假设**——调用点一律按实参路径实例化后真正
  单趟执行，故同字段/父子别名与不同字段的区别完全由实参替换后的重叠
  决定，而不是在摘要里猜形参关系。摘要用于暴露片段契约（哪些点依赖入口
  状态、片段自身改了什么），实例化执行用于检查；两者共用同一套数据流
  原语（反链移动、对称借用重叠、reinit 拆分、may 并集）。
* 片段是**词法内联**而非通用函数：无返回值/参数化多态/跨过程类型推断，
  局部 loan 到片段出口即结束，不建模“借用逃出片段”。这与“不另搭一套
  通用编译器”的范围一致。

## 已知限制（本题数学/协议子集，不宣称行业标准兼容）

* 不是完整 Rust 借用检查器：无生命周期推断、无 NLL/place 上下文、
  无闭包/函数间分析、无解构模式、无投影外的别名模型；路径就是记录树
  上的字段投影，不模拟原始指针/索引/数组元素。
* 块是扁平操作序列，不建模表达式树；一次操作只作用于一个固定路径。
* `end_loan` 是词法操作，不推断隐式结束点（NLL）。
* may 分析会报告真实编译器可能接受的分支组合（例如互斥分支分别移动不同
  字段后按并集处理），这是题面指定并集语义的直接结果。
* 片段只支持题面子集：无环、无递归、形参仅为显式路径、局部 loan 不逃逸；
  无返回值借用、无高阶/闭包片段、无片段多态。

## 文件

```
borrow_check/errors.py     # 异常类型、NaN/Infinity/bool 数值守卫
borrow_check/model.py      # 数据结构（含 Invoke/Param/Fragment/Program）、
                           # dict->CFG/Program 构造与结构校验
borrow_check/validator.py  # 语义校验（支配集不动点、片段无环/形状覆盖）
borrow_check/analyzer.py   # 核心：反链+精确移动、worklist 不动点、见证、
                           # check / check_program
borrow_check/fragments.py  # 本次新增：片段模板内联、形参命名空间摘要、
                           # 按实参路径实例化与片段内冲突位置翻译
demo.py                    # 固定输入演示（含片段摘要/别名/展开对照）
tests/                     # 必要测试 + 独立参考实现 + 片段随机展开对照
```
