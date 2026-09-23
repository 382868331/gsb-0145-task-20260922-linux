# borrow_check：字段敏感的静态借用检查模块（v1 + v2）

为离线编译器前端实现的可复用库。**v1**（默认，冻结）在最多 50 个基本块的
CFG 上做字段敏感（field-sensitive）的 may 借用检查；**v2**（显式
`"version": 2`）扩展到真实的分支资源生命周期：局部变量初始化、部分移动、
逆声明序词法析构、布尔条件限定的路径条件，以及两阶段可变借用
（预约/激活）。纯 Python 3.14.7 标准库实现，无第三方依赖、无网络、
无 IO 副作用。

## 环境

* Python **3.14.7**（仅依赖标准库 `dataclasses`、`collections`、`typing` 等）。
* 安装 Python 后**无需安装任何其他依赖**。Linux 原生运行；核心、测试、
  demo 全部离线可重复。

## 准确命令

```bash
# 快速回归（87 个用例：v1 的 51 个 + v2 的 36 个；含独立穷举解释器对照）
python -m unittest discover -s tests -v

# 扩展验证（固定种子 20260924，400 个 ≤10 块/≤8 条件/≤16 字段 CFG，
# 与独立穷举参考逐条对照；实测约 12 秒，不属于快速回归）
python tests/extended_verification.py

# v1 固定输入演示（旧入口，毫秒级）
python demo.py

# v2 固定输入演示：新增正常结果 + 棘手边界（预算 conflict/unknown）
# + 一次实际触发的拒绝（带逐步重放证据），实测 < 0.1 秒
python demo_v2.py
```

## 版本与兼容性（重要）

* **v1 是冻结的超集起点**：不传 `version`（或传 1）的所有旧调用、旧异常
  类型、旧冲突种类、旧确定性结果与 50 块上限**完全保留**（旧的 51 个测试
  一字未改全部通过）。v1 引擎代码仍在 `borrow_check/analyzer.py`。
* **v2 必须显式打开**：dict 输入传 `"version": 2`，或直接构造
  `CFG(version=2, conditions=..., guards=...)`。`check()` 按 `cfg.version`
  选择引擎；v2 返回 `CheckResultV2`（三态 `status`），v1 返回
  `CheckResult`（`clean: bool`）。
* v2 块上限 **80**（题面要求的 60 基本块之上留余量）；v1 仍为 50。
* 与旧行为真正不同的一点：v1 对**不可达块**也以空入口状态分析并可能报错；
  v2 只分析从入口可达的块——因为 v2 规定每条确定冲突必须给出**可执行
  输入**，不可达块没有可执行游走。结构合法性（路径存在、loan 唯一、
  支配等）对不可达块仍然校验。这是唯一有意的语义变更，旧 v1 调用不受影响。

## 公开接口

```python
from borrow_check import (
    CFG, Block, Formula,           # 图与命题公式
    Decl, Move, BorrowShared, BorrowMut, EndLoan, Read, Write,  # v1 七种
    Init, Drop, Activate, EndScope,                              # v2 四种
    cfg_from_dict, validate_cfg, check,
    CheckResult, Conflict, Witness,       # v1 结果
    CheckResultV2, ConflictV2, WitnessV2, UnknownReason, Evidence,  # v2 结果
    CFGValidationError, BudgetExhaustedError,
)
```

### 路径与记录树（两版本相同）

* 路径是字段名元组，dict 输入写作字符串列表；至少含根变量名。
* 变量是有限记录树：`declare(var, ty)`，叶子 `None`。
* **路径重叠**：一方为另一方前缀（含相等）。
* **移动方向性**：移动父路径压制全部子孙；移动子路径**不**使父路径或
  兄弟字段不可用。借用冲突用对称重叠。两套关系刻意区分（v1 契约，v2 沿用）。

## v2 输入格式

### 布尔条件与边守卫

```jsonc
{
  "version": 2,
  "conditions": ["c", "d"],          // 至多 8 个，顺序固定（决定赋值位序）
  "blocks": [ ... ],
  "successors": [
    [1],
    [ {"block": 2, "when": "d"}, {"block": 3, "when": ["not", "d"]} ],
    ...
  ],
  "entry": 0
}
```

* 后继可以是裸整数（守卫恒真）或 `{"block": <int>, "when": <公式>}`。
  同一目标后继不可重复（互斥条件请用 `or` 合并成一条边）。
* 公式：条件名字符串，或 `["const", true|false]`、`["not", f]`、
  `["and", f, ...]`、`["or", f, ...]`；节点 ≤ 200、深度 ≤ 32。
* 8 条件 → 256 个布尔赋值；公式在构造后编译成 256 位真值表掩码。
  **路径条件直接进入不动点格**：边传播把每个事实的条件掩码与守卫合取，
  块合流析取。恒假守卫后的代码不可达，不产生冲突。

### 新增/扩展操作

| 操作 | 语义 |
|---|---|
| `declare`（v2） | 新增 `init`（缺省 **false**：声明但未初始化）与 `scope`（缺省 `"0"`）。`init: true` 时整块初始化为可用 |
| `init path` | 初始化/重组写：path 子树恢复为已初始化；仍按写规则检查活跃借用与预约 |
| `drop path` | 显式析构：要求**整个 place** 可用（子树内任一节点未初始化/已移动/已析构都报 `use_of_*`），与任何活跃借用/预约冲突，随后消费 |
| `end_scope`（可带 `scope`，缺省 `"0"`） | 词法析构点：作用域内变量按**声明逆序**、字段按类型树声明逆序递归析构；只访问**仍初始化**区域，移动/未初始化区域跳过；与借用/预约冲突后整变量视为已消费 |
| `borrow_mut`（v2） | 新增 `two_phase: true`：开一个两阶段借用的**预约** |
| `activate loan_id` | 把预约激活为独占可变借用；只能引用一个 two_phase 的 borrow_mut |

### 两阶段借用规则

* **预约期间**：允许与预约路径重叠的共享读（`read`）和共享重借用
  （`borrow_shared`）；写、普通写、init、move、显式 drop、词法析构、
  普通（立即）可变借用、其他预约都冲突。
* **激活点**：重叠路径上不得再有其他活跃借用（含共享）或预约；激活后
  该 loan 按普通可变借用规则（共享读也冲突）。
* 预约与激活都是普通事实，参与**同一个**掩码不动点：预约沿回边传播、
  在合流处保留可满足条件，与析构/重新赋值/循环回边一起分析。
* 循环中重入同一 borrow/activate 点的 may 语义与 v1 一致：loan 静态
  唯一，may 分析下“上一轮的借用可能仍在”会与重入点冲突（具体重放决定
  它是否真是确定冲突）。

## v2 结果三态（并列规则）

`check(cfg)` 返回 `CheckResultV2`：

* `status == "clean"`：从入口出发的所有可执行路径（256 个赋值 × 有界
  循环展开）都无冲突。
* `status == "conflict"`：存在一个或多个**确定冲突** `ConflictV2`，
  且没有预算缺口。每个确定冲突都带：
  * `evidence.assignment`：一个具体布尔赋值（**可执行输入**，按
    `conditions` 顺序）；
  * `evidence.walk`：从入口出发的块下标序列（允许重复块）；
  * `evidence.back_edges`：该游走经过的回边（目标支配源的边）条数；
  * `evidence.events`：独立具体解释器的**逐步重放**记录。
  同一点多类/多触发条件冲突并列；输出顺序按 `(块, 操作, kind)` 确定。
* `status == "unknown"`：存在候选无法在预算内**证实或排除**
  （`unknown_reasons` 给出 `beyond_loop_budget` / `budget_exhausted`
  与细节）。**unknown 与 clean 严格互斥**——预算耗尽绝不是“无冲突”。

冲突 kind（v2）：v1 的五种（`use_of_moved`、`move_while_borrowed`、
`borrow_conflict`、`read_while_mut_borrowed`、`write_while_borrowed`）
加上 `use_of_uninitialized`、`use_of_dropped`、`write_while_reserved`、
`move_while_reserved`、`reservation_conflict`、`borrow_while_reserved`、
`activate_conflict`、`drop_while_borrowed`、`drop_while_reserved`。

### 认证而非信任标志

掩码不动点是 may 的、只产**候选**。每个候选都交给
`borrow_check/replay.py` 的**独立具体解释器**（与不动点是两套代码）：

1. 有界阶段：在候选条件掩码的每个赋值下做 DFS，回边计数 ≤
   `max_loop_unrolls`（默认 **4**，题面参考穷举限制），命中即逐步重放出
   可执行证据；
2. 完备阶段：有界未命中时，在有限具体状态空间上按 `(块, 具体状态)`
   记忆化、不设回边上界再搜（具体状态有限，必然终止）：
   * 任何深度都不触发 → 抽象过粗的**伪候选**，`reason="refuted"`，
     直接丢弃（计入 `refuted_candidates`，不冲突也不 unknown）；
   * 只在 > 4 轮触发 → 真冲突但超预算，`beyond_loop_budget` → unknown；
   * 状态展开超 `max_cert_states`（默认 20000）→ `budget_exhausted`
     → unknown。

因此检查器从不把分析器自己的布尔标志当作“已证明”；小规模参考域上被
穷举覆盖的例子必须给确定答案。

## check 参数与预算

```python
result = check(cfg, *, max_iterations=None,   # 不动点块处理预算
               max_loop_unrolls=4,            # 认证回边展开轮数
               max_cert_states=20_000)        # 完备认证具体状态预算
```

* `max_iterations`：v1/v2 都有。默认取可证明上界，合法输入不会耗尽；
  显式传更小的正数会在未收敛时抛 `BudgetExhaustedError`。
* v2 不动点上界（位宽随条件数指数）：
  `边数 × 2^k × (6·类型树路径数 + 2·loan 数) + 块数`。
* 整数参数拒绝 `bool`/非整数/越界（`checked_int`）。

## 输入上限（接口边界）

* v1 块数 1..**50**；v2 块数 1..**80**；单块操作 ≤ 10000；
* 记录树节点 ≤ 100000；v2 布尔条件 ≤ 8（256 赋值），公式节点 ≤ 200、
  嵌套深度 ≤ 32；
* 每变量字段数本身无独立上限，只受记录树节点总数约束（验收覆盖 16 字段）；
* 路径段/变量名/loan_id/条件名/作用域名为非空字符串；loan_id 全局唯一；
  后继下标合法不重复；`entry` 合法。

## 错误语义

* 非法输入抛 `CFGValidationError`（带可定位 `location`），构造时即完成
  结构 + 语义校验，失败不返回对象、不留部分变更；`check` 对输入只读。
* v2 追加校验：条件唯一且 ≤ 8、公式只引用已声明条件且形状合法、
  `activate` 必须引用 two_phase 的 borrow_mut、`end_scope` 的作用域必须
  存在且其全部 declare 支配该析构点、v2 专属操作不得出现在 v1 CFG 中。
* 整数参数拒绝 `bool`（`True` 不当 1）；`finite_number` 拒绝
  NaN/±Infinity（本模块无浮点计算，数据全是离散整数/字符串）。

## 算法（v2）

* 块边界状态 = 可达条件掩码 + 四个标签（`uninit/moved/dropped/__ok__`）
  各自的“子树事实反链 + 精确事实”掩码格 + 活跃借用掩码 + 预约掩码。
* 传输：`init`/`reinit` 恢复子树为 may-OK 并把严格祖先按路径上每个前缀
  拆成精确事实（v1 reinit 拆分规则的四标签掩码版）；`move`/`drop`/
  `declare`/`end_scope` 覆盖子树标签但保留严格祖先；读/借用的可用性仍按
  方向性（祖先压制子孙，子孙不压父）；显式 drop 按整个子树检查。
* worklist 在 `(可达掩码, 全部标签事实掩码, 活跃, 预约)` 的有穷格上单调
  迭代到不动点；变更判定用完整签名（含正向 OK 标签），与调度次序无关。
* 候选在不动点上有序终扫描收集，再走上述两阶段独立认证。

## 测试如何得到期望值（独立性）

* `tests/test_scenarios.py` 等 v1 四个文件（51 用例）：旧契约手写期望 +
  400 个随机 DAG 与旧独立参考 `tests/_reference.py`（不 import 被测）
  对照，**原样保留**。
* `tests/test_v2_scenarios.py`（17 用例）：初始化/析构、互斥别名、两阶段
  预约-激活、回边不动点、预算 unknown（含认证状态预算白盒）、确定性重复、
  三组能力组合端到端。
* `tests/test_v2_exhaustive.py`（3 用例）：
  * 固定种子 20260924 生成 200 个小 CFG（≤8 块、≤3 条件、含循环），
    对照 **`tests/_reference_v2.py`**：独立的第三套实现——自己解析、
    逐赋值（2^k）逐游走做具体解释，`(块, 具体状态)` 记忆化剪枝，
    并记录每个冲突的最小回边距离；**不 import 被测、不调用被测算期望**。
    小参考域上参考必须完整（不截断），且所有冲突最小距离 ≤ 4，被测的
    三态与冲突集合逐例一致；每条证据的游走/守卫/赋值/回边距离被独立核对。
  * 一个 **62 块 / 8 条件 / 32 字段节点**的手工主规模程序，断言 6 个
    确定冲突（回边预约冲突、预约后异常退出析构、激活后移动、两个相反
    输入的互斥别名冲突）各自的具体输入与游走，且可重复。
* `tests/test_v2_validation.py`（16 用例）：版本边界、条件/公式校验、
  两阶段与作用域规则、预算参数。
* `tests/extended_verification.py`：400 个 ≤10 块 / ≤8 条件 / ≤16 字段
  CFG 的扩展对照（约 12 秒），与快速回归分开。

## 已知限制（本题数学/协议子集，不宣称行业标准兼容）

* 不是完整 Rust 借用检查器：无生命周期推断、无 NLL place 上下文、
  无闭包/函数间分析、无解构模式；路径只是记录树字段投影。
* 块是扁平操作序列；一次操作只作用于一个固定路径；`end_loan`/
  `activate` 是词法点，不推断隐式位置。
* 守卫公式是有限命题公式（无量词、无理论、无算术）；条件是自由布尔
  变量，分析不对条件间的逻辑相关做跨公式化简（真值表已精确处理单个
  公式的可满足性）。
* 认证是有界的：>4 轮回边才出现的真实冲突给 `unknown`（带原因），
  不冒充 clean，也不无证据硬报 conflict。
* v2 的 `end_scope` 以作用域为单位，作用域嵌套/提前 return 的隐式析构
  需要由前端显式放置 `end_scope`。

## 文件

```
borrow_check/errors.py       # 异常类型、数值守卫（两版本共用）
borrow_check/model.py        # 数据结构（11 种操作）、Formula、dict->CFG
borrow_check/validator.py    # v1/v2 结构与语义校验（支配不动点）
borrow_check/analyzer.py     # v1 冻结核心：反链 may 不动点
borrow_check/analyzer_v2.py  # v2 核心：条件限定掩码不动点 + 候选
borrow_check/replay.py       # v2 独立具体解释器 + 两阶段认证/证伪
demo.py / demo_v2.py         # v1 / v2 固定输入演示
tests/_reference.py          # v1 独立参考（位向量 + DAG 枚举）
tests/_reference_v2.py       # v2 独立穷举具体解释器（不 import 被测）
tests/test_*.py              # 快速回归（87 用例）
tests/extended_verification.py  # 扩展验证（约 12 秒）
```
