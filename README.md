# 字段敏感的静态借用检查器（field-sensitive static borrow checker）

离线编译器前端可复用模块：在给定的控制流图（CFG）与有限记录类型树上做
**字段敏感的 may 借用检查 / 移动检查**，返回带**前驱见证链**的冲突列表。

- Python：**3.14.7**（仅标准库；不使用第三方借用检查实现替代核心算法）
- 依赖：**无**。安装 Python 后直接运行，不需要网络、账号、密钥、数据库、
  中间件、云 API 或 Docker。
- 测试与演示全部离线、确定性（固定输入 / 固定随机种子）。

## 准确命令

```bash
# 运行全部必要测试
python -m unittest discover -s tests -v

# 运行固定输入演示（真实计算，约毫秒级，远小于 8 秒；无 sleep、无预录结果）
python demo.py
```

## 公开接口

```python
from borrowcheck import (
    CFG, Block,
    Declare, Move, BorrowShared, BorrowMut, EndLoan, Read, Write,
    check,
    CFGValidationError, BudgetExceededError,
    ConflictCode, Location,
)
```

### 数据结构

| 对象 | 说明 |
| --- | --- |
| `CFG(blocks, entry, types)` | 不可变值对象。`types` 为根变量到记录树的嵌套 `dict`；叶子写 `None` 或 `{}`。 |
| `Block(id, instructions, successors)` | 基本块：有序指令元组 + 后继块 id 元组（分支即多后继）。 |
| `Declare(var)` | 声明根变量（幂等；允许在不同分支各声明一次）。 |
| `Move(path)` / `Read(path)` / `Write(path)` | 路径为字段名元组，如 `("order","items","first")`。 |
| `BorrowShared(loan_id, path)` / `BorrowMut(loan_id, path)` | 开始贷款；`loan_id` 在整个 CFG 内静态唯一，绑定固定路径。 |
| `EndLoan(loan_id)` | 结束指定贷款。 |

`check(cfg, *, budget=None) -> CheckResult`：

- `CheckResult.ok`：是否无冲突；
- `CheckResult.conflicts: tuple[Conflict, ...]`：冲突列表；
- `CheckResult.facts`：收敛后各可达块的入口/出口 may 事实（供工具使用）；
- `CheckResult.iterations`：不动点工作单元数。

每个 `Conflict` 含：

- `code`（`ConflictCode`）、`location`（块 id + 块内 0 基指令下标）、
  `instruction_kind`、`path`、`message`；
- 贷款类冲突另有 `loan_id` / `loan_kind` / `loan_path`；移动类冲突另有
  `against_path`（与之重叠的已移动路径）；
- `witness`：**前驱见证链**，`witness.render()` 可打印
  `origin → flow → edge → … → 冲突点` 的来源链。

### 冲突码（有限契约子集）

| code | 触发条件 |
| --- | --- |
| `use_before_declare` | 某条到达路径上根变量可能尚未声明（may-未声明）。 |
| `use_of_moved` | 读 / 移动 / 借用经过的路径与某条可能已移动路径重叠。 |
| `write_to_field_of_moved_parent` | 祖先对象本身仍处移动态时给其字段赋值。 |
| `read_while_mut_loan` | 读路径与可能仍活跃的可变贷款重叠。 |
| `write_while_loan_active` | 写路径与任何可能仍活跃的贷款重叠。 |
| `move_while_loan_active` | 移动路径与任何可能仍活跃的贷款重叠。 |
| `loan_conflict` | 新贷款与活跃贷款不兼容（共享+共享重叠允许；其余重叠组合拒绝）。 |
| `end_inactive_loan` | `end_loan` 时贷款在所有到达路径上都不活跃。 |

## 语义规则

1. **路径重叠**：一方是另一方的前缀（相等也算重叠）。兄弟字段互不重叠，
   可独立借用；父子路径不能同时以冲突方式使用。
2. **移动父路径**使该路径整棵子树不可用；移动子字段（部分移动）也使父路径
   的整体使用非法。
3. **重新赋值只恢复该字段**：`Write(p)` 仅恢复 `p` 及其子树，兄弟字段状态
   不变；若 `p` 的严格祖先仍被移动，则该写本身非法且不改变状态
   （必须先重组祖先对象）。
4. **分支合并取并集**：可能已移动路径集合、可能仍活跃贷款集合、可能未声明
   变量集合三者均做 may 并集；循环用工作列表迭代到不动点。
5. **贷款**：多个共享贷款可在重叠路径共存；可变贷款与任何重叠贷款互斥。
   `end_loan` 只在贷款“可能活跃”时合法（至少一条到达路径活跃即可）。
6. **见证不靠深度截断**：收敛后在有限的“块内边界 + CFG 边”图上做前向 BFS，
   从事实的具体起源（move/borrow 指令、end_loan、程序入口）沿事实始终成立的
   边界走到冲突点；图节点数由程序规模界定，链长有界。

## 输入上限（接口边界）

| 限制 | 值 |
| --- | --- |
| 块数 `MAX_BLOCKS` | 50 |
| 每块指令数 `MAX_INSTRUCTIONS_PER_BLOCK` | 1000 |
| 路径深度 `MAX_PATH_DEPTH` | 32 |
| 类型树节点数 `MAX_TYPE_NODES` | 10000 |
| 块 id / 变量名 / 字段名 / loan_id | 非空、不含空白的字符串 |

其他结构限制：块 id 唯一、后继必须指向已存在块且不重复、**禁止指向入口块
的边**（入口在每条到达路径上恒为程序起始状态）；指令必须是七种指令类型
之一；路径必须存在于类型树中。

## 错误语义

- 非法输入在分析开始前抛出 **`CFGValidationError`**，带可定位的 `location`
  轨迹（例如 `block 'b2' -> instruction 0 (borrow_mut) -> path[1]`）与
  `message`；不静默纠正、不猜测。输入 CFG 对象不会被修改，失败时不产生
  任何“部分结果”。
- 整数参数（如 `budget`）：要求真实 `int`，**拒绝 `bool`**；传入浮点时
  拒绝 **NaN / ±Infinity**（其余非整数浮点同样拒绝）；`budget` 必须为正。
- **预算耗尽 ≠ 证明无冲突**：默认预算由程序规模导出，对有限格必然足够；
  仅当显式传入更小的 `budget` 时可能抛出 `BudgetExceededError`，其含义是
  “分析未完成”，与返回干净的 `CheckResult(ok=True)` 严格区分。

## 设计取舍

- **may 分析、并集合并**：宁可因“某条路径可能违规”而拒绝（保守），符合
  借用检查器“证明安全才放行”的立场。
- 第三类事实采用“**可能未声明**”的 may 集合（入口包含所有类型根，
  `declare` 将其移除，合并取并集），从而与移动 / 贷款事实保持同一套单调
  框架并能处理循环。
- 块内冲突不影响事实传播；冲突在不动点收敛后对最终入口状态做一次重放统一
  收集，避免错误报告依赖迭代顺序。
- 见证链在收敛后的显式边界图上用 BFS 重建，是“事实存活到冲突点”的一条
  具体控制流路径，而非单纯文本提示。

## 测试与独立参考

- `tests/test_scenarios.py`：题面要求的四类验收场景（互不相交字段、循环中
  未结束借用、单分支移动后读取、父对象重组）及手工期望。
- `tests/test_validation.py`：输入校验、NaN/Infinity/bool 拒绝、预算语义、
  见证链完整性、50 块长回边链不被截断。
- `tests/reference.py`：**独立参考实现**，不 import、不调用被测核心：
  - 显式具体状态可达（集合-of-状态，含循环不动点）；
  - 对小无环图额外做**完整路径枚举**（小 DAG 枚举路径逐条对照）。
- `tests/test_reference_crosscheck.py`：用固定种子确定性生成 200 个随机
  CFG（120 个 DAG 双参考对照 + 100 个带环图对照显式状态参考），外加手工
  钻石路径计数用例。往返一致仅作补充，期望值均来自独立参考或手工推导。

## 浮点 / 误差口径

本模块输出**不含任何浮点数**（事实、冲突、迭代次数均为离散集合 / 整数 /
字符串），因此**不存在浮点误差或容差问题**；判断全部为离散的前缀包含与
集合运算，边界情形（相等路径、严格祖先、严格后代）分别显式判定，不用宽容差
掩盖。

## 已知限制（本题契约子集，非行业标准完整兼容）

- 这是题面定义的**有限协议子集**：无函数调用 / 闭包 / NLL 控制依赖、无
  生命周期参数推导、无 trait / 泛型、无数组索引或动态指针、无路径联结
  （path joining）以外的别名模型；不宣称与 Rust 等完整语言借用检查器兼容。
- 贷款在 CFG 层由静态唯一 `loan_id` 与固定路径标识，不建模“同一次语法借用
  在循环中多次执行”的实例区分：再次经过同一借用点时按 may 活跃保守处理。
- 不可达块不参与语义分析（仍做结构校验）。
- 类型树仅用于路径合法性，不参与类型推导；叶子用 `None`/`{}` 表示。
