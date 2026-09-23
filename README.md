# borrow_check：字段敏感的静态借用检查模块（v1 may + v2 生命周期）

为离线编译器前端实现的可复用库。包含**两个并列引擎**，共用同一套数据
模型、错误类型与公开入口 `check`：

* **v1（默认，`lifecycle=False`）**：在最多 50 个基本块的 CFG 上做字段
  敏感的 **may 借用检查**（可能已移动 / 可能仍活跃取并集，循环求不动点），
  返回带**前驱见证链**的冲突列表。行为与第一版完全一致。
* **v2（显式选项，`lifecycle=True`）**：真实的分支资源生命周期分析，
  块数上限 64：局部变量初始化/部分移动/**词法析构**（逆声明顺序、只访问
  仍初始化字段）、布尔**条件命题**（互斥分支不产生伪冲突）、**两阶段
  可变借用**（预约期允许共享读取，激活后独占），结论分
  `ok / conflict / unknown`，每条确定冲突附**可执行输入与逐步重放**。

纯 Python 3.14.7 标准库实现，无第三方依赖、无网络、无 IO 副作用。

## 环境与准确命令

* Python **3.14.7**；无需安装任何依赖；Linux 原生离线可重复。

```bash
# 快速回归（77 个用例，约 0.2 秒）：v1 全部旧测试 + v2 场景与输入校验
python -m unittest discover -s tests -v

# 扩展验证（约 4.5 秒）：v2 有界穷举独立参考对照（300 随机 + 12 个
# 8 条件 256 赋值穷举）、证据可执行性独立复核、64 块/16 字段主规模用例
python -m unittest tests.ext_lifecycle_reference tests.ext_lifecycle_scale -v

# 固定输入演示（实测约 10 毫秒）：v1 与 v2 的正常结果、棘手边界、
# 实际拒绝与一次如实 unknown；无 sleep、无预录结果
python demo.py
```

## 公开接口

```python
from borrow_check import (
    CFG, Block,
    Decl, Move, Read, Write,
    BorrowShared, BorrowMut, EndLoan,          # v1 借用
    BorrowReserve, BorrowActivate, Drop,       # v2 新增操作
    cfg_from_dict, validate_cfg, check,
    CheckResult, Conflict, Witness,
    ExecutableEvidence, ReplayStep,            # v2 可执行证据
    CFGValidationError, BudgetExhaustedError,
)

result = check(cfg)                      # v1（默认；上限 50 块）
result = check(cfg, lifecycle=True)      # v2（上限 64 块）
cfg = cfg_from_dict(data, max_blocks=64) # 构造时显式选择块数边界
```

### 路径与记录树

* 路径是字段名元组，如 `("user", "profile", "name")`；dict 输入写作
  `["user", "profile", "name"]`。空路径非法（至少含根变量名）。
* 变量是有限记录树：`declare(var, ty)`，`ty` 为
  `{字段名: 子类型}` 的 dict，叶子为 `None`。所有访问路径必须在树中存在。
* **路径重叠**：一方为另一方前缀（含相等）。

## v2 生命周期语义（新增能力）

### 1. 初始化、部分移动与词法析构

* `declare(var, ty)` 是变量的（重新）初始化点：到达该点的执行上整个
  子树恢复初始化。循环回边再次经过 declare 视为全新局部变量。
* `move(path)` 压子树（祖先移动压子孙；移动子路径不使父路径不可用，
  字段独立）。`write(path, reinit=True)` 只恢复该子树并按 v1 规则拆分
  祖先移动（祖先对象本身仍可能已移动，兄弟子树仍不可用）。
* `drop`（无参数词法析构点）：对该点词法作用域内（声明点支配当前块）
  的局部变量，**按声明点逆序**析构；**只访问仍初始化的路径**——已部分
  移动的字段及其子树跳过。析构仍初始化字段时若与活跃/预约借用重叠，
  报 `drop_while_borrowed`；析构后变量在出边状态中不可用，其借用死亡。
  循环重入同一 drop 点时，已析构变量的析构是空操作（分析内用对用户
  不可见的“析构相位位”区分首次访问与重入）。

### 2. 布尔条件与命题守卫

* CFG 顶层 `conditions: [原子名, ...]`（至多 **8** 个非空、不重复）。
* 后继可以是整数（无条件边）或 `{"block": j, "when": 公式}`。公式：
  原子字符串、`{"not": e}`、`{"and": [e, e, ...]}`、`{"or": [...]}`
  （合取/析取至少 2 支）。守卫引用的原子必须出自 `conditions`。
* 抽象状态的每个事实都挂**真值表位向量**（8 条件时 256 个赋值）：边
  传播与守卫合取，分支合流按赋值行析取。因此同一条件的互斥分支在汇合
  点不共享事实行——**伪冲突在不动点内部消除，而不是报错后再筛**。
  路径条件直接参与循环不动点与两阶段激活。

### 3. 两阶段可变借用

| 操作 | 语义 |
|---|---|
| `borrow_reserve(loan_id, path)` | 预约（借用出生点）。预约期允许对重叠路径的共享读取与共享借用；阻止写/move；与活跃 shared/mut 冲突；预约之间可共存 |
| `borrow_activate(loan_id)` | 把该预约激活为独占可变借用：此刻才与任何重叠借用（含其他预约、以及循环回边带来的上一轮遗留激活）按独占规则冲突 |
| `end_loan(loan_id)` | 结束（预约态或激活态都清除） |

每个 loan_id 静态唯一，恰好一个出生点；`borrow_activate` 引用的必须是
预约 loan，且每个预约至多激活一次（构造期校验）。预约与词法析构、
重新赋值及循环回边在同一不动点内一起分析。

### 结论状态（CheckResult.status）

| status | clean | 含义 |
|---|---|---|
| `"ok"` | True | 证明无冲突 |
| `"conflict"` | False | 至少一条**确定冲突**；每条附可执行证据 |
| `"unknown"` | False | 无法在预算内给出确定结论（见下），**不是通过** |

v1 模式只产生 `ok`/`conflict`（`status="unknown"` 不出现）。

**确定冲突的判据**：抽象引擎提出候选（坏条件赋值表非空）后，还必须由
**独立的有界具体解释器**（不调用抽象引擎）在某条件赋值下搜索到真正
走到冲突点并**现场复现**该冲突的块游走。证据 `ExecutableEvidence`
包含：具体条件赋值（可执行输入）、逐边守卫均为真的块游走、逐操作
`ReplayStep` 重放、诞生点与 `reproduced` 标记。

**unknown 的来源（如实返回，绝不当通用实现）**：

* 证据搜索状态预算耗尽（`search_budget`，默认 20000）；
* 每事实键不同诞生点超过 64 条触发的捕获合并（`fact_budget`）。

有界穷举确认不可复现的抽象候选（may 分析在“仍初始化”这类否定条件上
的固有过近似）会被静默证伪丢弃，不报确定冲突。小规模、独立参考覆盖的
用例必须给确定答案——扩展测试对此逐例核对。

### 预算与边界（固定缺省，同输入同结果）

| 边界 | v1 | v2 |
|---|---|---|
| 基本块数 | 1..**50** | 1..**64**（至少覆盖 60） |
| 布尔条件 | — | 0..**8** |
| 单变量字段数 | 类型树 ≤100000 节点（每变量支持 **16+** 字段） | 同 |
| 单块操作数 | ≤10000 | 同 |
| 不动点 | 可证上界 `E·(P+L)+n` | 按事实键×真值表行×每键 64 条计的单调上界 |
| 循环穷举参考 | — | **4 轮**展开（`MAX_LOOP_UNROLL`） |
| 证据搜索 | — | 4 轮展开、20000 个 (块,具体状态)（`search_budget`） |

`max_iterations`（两引擎通用）：显式传更小正数时抛
`BudgetExhaustedError`，与 clean/ok 严格分开。v2 的
`search_budget` 耗尽则给 `status="unknown"`（不是异常，也不是通过）。
所有排序（条件原子、见证、冲突、证据赋值行）固定，同输入重复运行结果
逐字节一致。

### 新增冲突种类与证据

v2 在 v1 的 `use_of_moved / move_while_borrowed / borrow_conflict /
read_while_mut_borrowed / write_while_borrowed` 之外增加
`drop_while_borrowed`。`declare` 撞上遗留借用记为
`write_while_borrowed`。

```python
result.explain()            # v1/v2 冲突的前驱见证链
result.explain_evidence()   # v2 可执行输入 + 逐步重放 + unknown 原因
for ev in result.evidence:
    ev.assignment          # {'p': True, ...}：布尔原子的具体真值
    ev.walk                # (0, 2)：块游走
    ev.steps               # 逐操作重放，最后一步现场复现
    ev.reproduced          # 独立具体解释器的现场复现标记
```

检查器不信任求解器自报的“认证/成功”标志：确定冲突的可执行性由独立
具体解释器搜索、再由测试用独立的边守卫求值器二次复核。

## 算法

* **v1**：每块入口/出口维护子树移动反链 + 精确移动集合 + 活跃 loan
  集合；合并取并集；worklist 单调不动点；终扫描有序收集冲突。
* **v2**：状态按根变量分分量，每分量的事实表用 `2R` 行（`R=2**k`）
  编码，额外一位是对用户不可见的析构相位（区分首次 drop 与循环重入）；
  边传播合取守卫、合流按行析取；传输关于状态单调（kill 的行只依赖当前
  路径条件），故必达不动点且有显式整数上界。其后独立的有界具体解释器
  （借用 token 多重集 + (块,状态) BFS，携带可行赋值表）负责把候选
  证实为确定冲突或在预算内置为 unknown。
* **独立测试参考**（不 import 被测核心）：
  `tests/_reference.py`（v1：类型树位向量 + DAG 全块序列枚举）；
  `tests/_reference_lifecycle.py`（v2：枚举全部 `2**k` 条件赋值 ×
  有界具体解释器 (块,状态) BFS，4 轮展开）。期望值均独立编码，往返一致
  只作补充。

## v1 → v2 迁移与兼容（逐条）

v2 通过显式选项接入，**默认行为、合法调用、异常类型与确定性结果全部
保留**；旧 51 个测试一个未删、断言未放松。

| 变更点 | 兼容处理 |
|---|---|
| `check` 增加 `lifecycle`/`search_budget` 关键字参数 | 均有关键字缺省（`False`/`None`），旧位置/关键字调用不变 |
| 块数上限 50 → v2 64 | `cfg_from_dict`/`validate_cfg` 增加 `max_blocks`，默认仍 **50**；v2 显式传 64 |
| CFG 增加 `conditions`/边守卫 | 均可选，缺省无条件、边为整数；旧 dict 与 dataclass 直接可用 |
| 新增操作 `borrow_reserve/borrow_activate/drop` | v1 输入不含即不受影响；这些操作只在 v2 分析中产生生命周期语义 |
| `CheckResult` 增加字段 | `status` 缺省 `"ok"`、`unknown_reasons=()`、`lifecycle=False`、`evidence=()`；`clean/conflicts/iterations/explain()` 语义不变 |
| 新增异常情形 | 预约/激活规则违例（激活未出生 loan、激活一阶段 loan、重复激活）、条件超 8、守卫原子未声明：构造期抛带 location 的 `CFGValidationError`（旧输入不触发） |
| 旧 v1 may 语义（合流无条件取并集） | 与 v2 的命题精化是两个引擎，不互相改写；需要“互斥分支不产生伪冲突”时显式用 v2 |

## 错误语义与数值口径

* 非法输入抛 `CFGValidationError`，带可定位 `location`
  （如 `blocks[2].ops[0].path`、`cfg.conditions`、`successors[1][0].when`），
  构造时完成全部结构与语义校验，失败不返回对象、不留部分变更。
* 整数参数拒绝 `bool`/浮点/越界；`finite_number` 拒绝 NaN/±Infinity。
* 数据模型与分析**无浮点计算**（路径、集合、真值表、迭代次数全为离散
  整数/字符串），无数值误差或容差问题。
* 预算耗尽（异常）与 unknown（状态）都不等于“证明无冲突”。

## 设计取舍与已知限制

* v1 是无条件 may 分析，按题面取并集会报告真实编译器可能接受的分支
  组合；v2 用命题路径条件消除同一条件互斥分支的伪冲突，但 may 分析对
  “仍初始化”等否定条件仍有固有过近似——这类候选必须通过独立具体
  解释器证实，否则不报（预算内未穷尽则 unknown）。
* 循环的语义边界是 4 轮展开（证据/穷举口径）；抽象不动点本身对任意识
  环都终止，不依赖深度截断，但只对 4 轮内可复现的冲突给确定证据。
* 不是完整 Rust 借用检查器：无生命周期推断/NLL、无函数间/闭包分析、
  无别名投影外模型；`end_loan`/`drop` 为词法点，不推断隐式结束。
* 块是扁平操作序列，一次操作一个固定路径；守卫只依赖声明的 8 个布尔
  原子，不建模算术/指针/数组下标。

## 文件

```
borrow_check/errors.py     # 异常类型、数值守卫
borrow_check/formula.py    # 命题公式 AST 与真值表代数（≤8 原子）
borrow_check/model.py      # 数据结构（含 v2 操作/条件/守卫）与构造校验
borrow_check/validator.py  # 语义校验（支配集、loan 与预约规则）
borrow_check/analyzer.py   # v1 核心；check 入口分发
borrow_check/lifecycle.py  # v2 核心：受护事实表不动点 + 具体解释器证据
demo.py                    # v1+v2 固定输入演示
tests/test_*.py            # 快速回归（77 个）
tests/ext_*.py             # 扩展验证（穷举参考对照、8 条件、主规模）
tests/_reference.py        # v1 独立参考（DAG 枚举）
tests/_reference_lifecycle.py  # v2 独立参考（有界穷举具体解释器）
```
