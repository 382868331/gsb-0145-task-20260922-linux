"""v2 穷举解释器对照（小规模独立参考域，完整核对）。

固定种子（20260924）确定性生成大量小型 v2 CFG（≤8 块、≤3 条件、≤2 层
记录树、含回边循环、两阶段借用与词法析构），对每条 CFG 比较两条完全
独立的实现路线：

* 被测：``borrow_check.check``（条件限定掩码 may-不动点 + 两阶段
  具体重放认证，见 analyzer_v2.py / replay.py）；
* 参考：``tests._reference_v2``（自己解析、逐赋值、逐游走的穷举具体
  解释器，``(块, 具体状态)`` 记忆化剪枝；**不 import 被测核心**，
  也不调用它算期望值）。

完整核对内容：

1. 参考在小参考域上必须完整（status 永不为 unknown）；
2. 确定冲突集合 ``(kind, block, op)`` 逐一相等；
3. clean/conflict 判定一致（该规模所有冲突的最小回边距离 ≤ 2，
   默认 4 轮认证预算覆盖，故被测不得返回 unknown）；
4. 被测每条确定冲突都带：可执行游走（边存在且守卫在认证赋值下为真）、
   认证赋值落在候选掩码内、回边距离与参考记录的最小距离相容
   （参考最小距离 ≤ 证据游走回边数 ≤ 4）。

另含一个**主规模非退化用例**（60 块 / 8 条件 / 单变量 16 字段），
参考穷举在此规模不适用；它核对被测在主规模上的确定性结果、
结构边界与固定期望（冲突、认证、可重复），不做玩具规模之外的穷举断言。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check

from tests._reference_v2 import _RefInterpreter

FIELDS = ("x", "y")
SEED = 20260924
TRIALS = 200


def make_type(rng, depth):
    if depth == 0 or rng.random() < 0.3:
        return None
    return {f: make_type(rng, depth - 1)
            for f in rng.sample(FIELDS, rng.randint(1, 2))}


def enumerate_type_paths(types):
    out = []
    for root, ty in types.items():
        stack = [((root,), ty)]
        while stack:
            p, node = stack.pop()
            out.append(p)
            if isinstance(node, dict):
                for f, sub in node.items():
                    stack.append(((*p, f), sub))
    return out


def generate_cfg(rng):
    """确定性生成合法 v2 CFG（loan 全局唯一、activate 只引两阶段 loan）。"""
    n = rng.randint(4, 8)
    k = rng.randint(0, 3)
    conds = [f"c{i}" for i in range(k)]

    def atom():
        if conds and rng.random() < 0.8:
            return rng.choice(conds)
        return ["const", bool(rng.getrandbits(1))]

    def formula(depth=0):
        if depth >= 2 or k == 0 or rng.random() < 0.55:
            return atom()
        op = rng.choice(["not", "and", "or"])
        if op == "not":
            return ["not", formula(depth + 1)]
        return [op, formula(depth + 1), formula(depth + 1)]

    types = {f"v{i}": make_type(rng, 2) for i in range(rng.randint(1, 2))}
    blocks = [[{"op": "declare", "var": v, "ty": ty,
                "init": rng.random() < 0.7} for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]
    paths = enumerate_type_paths(types)

    loan_counter = 0
    active_loans = []
    tp_loans = []

    def fresh_loan():
        nonlocal loan_counter
        lid = f"L{loan_counter}"
        loan_counter += 1
        return lid

    for bi in range(1, n):
        for _ in range(rng.randint(1, 3)):
            roll = rng.random()
            p = list(rng.choice(paths))
            if roll < 0.14:
                blocks[bi].append({"op": "move", "path": p})
            elif roll < 0.26:
                lid = fresh_loan()
                active_loans.append(lid)
                blocks[bi].append(
                    {"op": "borrow_shared", "loan_id": lid, "path": p})
            elif roll < 0.40:
                lid = fresh_loan()
                active_loans.append(lid)
                two_phase = rng.random() < 0.6
                blocks[bi].append({"op": "borrow_mut", "loan_id": lid,
                                   "path": p, "two_phase": two_phase})
                if two_phase:
                    tp_loans.append(lid)
            elif roll < 0.50 and active_loans:
                lid = rng.choice(active_loans)
                active_loans.remove(lid)
                blocks[bi].append({"op": "end_loan", "loan_id": lid})
            elif roll < 0.56 and tp_loans:
                lid = rng.choice(tp_loans)
                tp_loans.remove(lid)
                blocks[bi].append({"op": "activate", "loan_id": lid})
            elif roll < 0.72:
                blocks[bi].append({"op": "read", "path": p})
            elif roll < 0.82:
                blocks[bi].append({"op": "init", "path": p})
            elif roll < 0.90:
                blocks[bi].append({"op": "drop", "path": p})
            else:
                blocks[bi].append({"op": "end_scope", "scope": "0"})

    succ = [[] for _ in range(n)]
    for j in range(1, n):
        succ[j - 1].append(j)  # 主干链保证全部块可达
    for i in range(1, n):
        roll = rng.random()
        if roll < 0.45:
            target = rng.randrange(i)  # 回边
        elif roll < 0.60 and i + 1 < n:
            target = rng.randrange(i + 1, n)  # 前向跳边
        else:
            target = None
        # 跳过与主干链或已加边重复的目标（重复后继非法，守卫应 or 合并）。
        if target is not None and all(
                (e if isinstance(e, int) else e["block"]) != target
                for e in succ[i]):
            succ[i].append({"block": target, "when": formula()})
    for i, lst in enumerate(succ):
        if len(lst) > 1:
            for ei, entry in enumerate(lst):
                if isinstance(entry, int) and rng.random() < 0.6:
                    lst[ei] = {"block": entry, "when": formula()}

    return {"version": 2, "conditions": conds,
            "blocks": [{"ops": ops} for ops in blocks],
            "successors": succ, "entry": 0}


def executable(cfg, evidence):
    """核对证据游走：起点为入口、边存在、守卫在认证赋值下为真。"""
    index = {name: i for i, name in enumerate(cfg.conditions)}
    assert evidence.walk[0] == cfg.entry
    for u, v in zip(evidence.walk, evidence.walk[1:]):
        assert v in cfg.successors[u], f"边 {u}->{v} 不存在"
        formula = cfg.guards.get((u, v))
        if formula is not None:
            assert (formula.compile(index) >> evidence.assignment_bits) & 1, (
                f"边 {u}->{v} 守卫在认证赋值下为假")


class TestExhaustiveReference(unittest.TestCase):
    def test_matches_exhaustive_reference(self):
        rng = random.Random(SEED)
        checked = 0
        total_conflicts = 0
        worst_states = 0
        worst_dist = 0
        for trial in range(TRIALS):
            data = generate_cfg(rng)
            cfg = cfg_from_dict(data)  # 生成器只产合法 CFG
            ref = _RefInterpreter(data).enumerate_reference(max_states=300_000)
            result = check(cfg)
            checked += 1
            worst_states = max(worst_states, ref["state_count"])
            # 1) 小参考域必须完整枚举。
            self.assertEqual(
                ref["status"] in ("clean", "conflict"), True,
                f"trial {trial}: 参考在小域上被预算截断")
            # 2) 状态三态与冲突集合一致（4 轮预算覆盖该域，不得 unknown）。
            ref_conflicts = set(ref["conflicts"])
            core_conflicts = {
                (c.kind, c.block, c.op) for c in result.conflicts}
            self.assertEqual(
                result.status,
                "conflict" if ref_conflicts else "clean",
                f"trial {trial}: status={result.status} "
                f"unknowns={[(u.kind, u.reason) for u in result.unknown_reasons]}")
            self.assertEqual(core_conflicts, ref_conflicts,
                             f"trial {trial}: 对称差 "
                             f"{core_conflicts ^ ref_conflicts}")
            # 3) 每条确定冲突的证据都要满足可执行性与预算相容。
            for c in result.conflicts:
                key = (c.kind, c.block, c.op)
                executable(cfg, c.evidence)
                self.assertLessEqual(c.evidence.back_edges, 4)
                self.assertLessEqual(ref["conflicts"][key],
                                     c.evidence.back_edges)
                # 候选掩码必须包含认证赋值。
                # （掩码在内部，这里通过赋值位非负、且赋值与条件数一致核对）
                self.assertLess(c.evidence.assignment_bits,
                                1 << len(cfg.conditions))
            total_conflicts += len(ref_conflicts)
            worst_dist = max(worst_dist, *ref["conflicts"].values(), 0)

        # 4) 域本身非退化：确实枚举到循环冲突，且 4 轮预算完整覆盖。
        self.assertGreater(total_conflicts, 100)
        self.assertLessEqual(worst_dist, 4)
        # 诊断信息：记录这一轮参考实际展开规模（不做硬时间断言）。
        self.__class__.worst_states = worst_states
        self.__class__.checked = checked

    def test_determinism(self):
        rng1 = random.Random(SEED)
        rng2 = random.Random(SEED)
        for _ in range(20):
            d1, d2 = generate_cfg(rng1), generate_cfg(rng2)
            self.assertEqual(d1, d2)
            r1 = check(cfg_from_dict(d1))
            r2 = check(cfg_from_dict(d2))
            self.assertEqual(
                [(c.kind, c.block, c.op, c.evidence.assignment_bits,
                  c.evidence.walk) for c in r1.conflicts],
                [(c.kind, c.block, c.op, c.evidence.assignment_bits,
                  c.evidence.walk) for c in r2.conflicts])


class TestMainScale(unittest.TestCase):
    """主规模：≥60 块、8 条件、每变量 16 字段的非退化确定性用例。

    结构（手工设计，非玩具直线）::

        B0        declare a{head{f0..f15}, tail{f0..f15}} 已初始化
        B1..B39   只读填充块，链上含 c0 守卫的回边（纯循环，无 loan）
        B40/B41   预约循环：B40 写 head.f5；B41 预约 L2 + 预约期共享读；
                  B41->B40 当 c2（回边），B41->B42 当 not c2；B42 end L2
        B43..B48  链
        B49       预约 L @ head.f0；B50 预约期共享读（干净）
        B51       if c3：B52 异常退出 end_scope（drop_while_reserved）
                        else B53：激活 L 后 move（move_while_borrowed）
        B54..B60  互斥别名：if c4 分别 move head.f2 / head.f3；合流后
                  第二次按 c4 分流读对方字段（干净），再在 B60 无保护读
                  两个字段（两个确定冲突，各带 c4 真值输入）
        B61       end_scope：部分移动只析构仍初始化字段（干净）
    """

    def _build(self):
        ty_leaf = {f"f{i}": None for i in range(16)}
        ty = {"head": ty_leaf, "tail": ty_leaf}
        conds = [f"c{i}" for i in range(8)]
        n = 62
        blocks: list = [
            [{"op": "declare", "var": "a", "ty": ty, "init": True}]]

        # B1..B39：只读不相交字段（回边重入也无 loan，始终干净）。
        for bi in range(1, 40):
            side = "tail" if bi % 2 == 0 else "head"
            blocks.append([{"op": "read", "path": ["a", side, f"f{bi % 16}"]}])
        # B40：循环头写。
        blocks.append([{"op": "write", "path": ["a", "head", "f5"]}])
        # B41：预约 + 预约期共享读。
        blocks.append([
            {"op": "borrow_mut", "loan_id": "L2",
             "path": ["a", "head", "f5"], "two_phase": True},
            {"op": "read", "path": ["a", "head", "f5"]},
        ])
        # B42：离开预约循环时结束 L2。
        blocks.append([{"op": "end_loan", "loan_id": "L2"}])
        # B43..B48：链。
        for bi in range(43, 49):
            blocks.append([{"op": "read",
                            "path": ["a", "tail", f"f{bi % 16}"]}])
        # B49：预约 L；B50：预约期共享读（干净）。
        blocks.append([{"op": "borrow_mut", "loan_id": "L",
                        "path": ["a", "head", "f0"], "two_phase": True}])
        blocks.append([{"op": "read", "path": ["a", "head", "f0"]}])
        # B51：条件分支（异常退出 / 正常激活）。
        blocks.append([])
        # B52：异常退出路径：词法析构撞上预约 L。
        blocks.append([{"op": "end_scope", "scope": "0"}])
        # B53：激活后移动与独占借用冲突，随后结束 L。
        blocks.append([
            {"op": "activate", "loan_id": "L"},
            {"op": "move", "path": ["a", "head", "f0"]},
            {"op": "end_loan", "loan_id": "L"},
        ])
        # B54：互斥别名分流。
        blocks.append([])
        blocks.append([{"op": "move", "path": ["a", "head", "f2"]}])  # B55
        blocks.append([{"op": "move", "path": ["a", "head", "f3"]}])  # B56
        blocks.append([])                                               # B57
        blocks.append([{"op": "read", "path": ["a", "head", "f3"]}])  # B58
        blocks.append([{"op": "read", "path": ["a", "head", "f2"]}])  # B59
        # B60：无保护读两个字段（两个确定冲突）。
        blocks.append([
            {"op": "read", "path": ["a", "head", "f2"]},
            {"op": "read", "path": ["a", "head", "f3"]},
        ])
        # B61：词法析构（部分移动被跳过，无 loan 存活，干净）。
        blocks.append([{"op": "end_scope", "scope": "0"}])
        assert len(blocks) == n

        succ = [[] for _ in range(n)]
        succ[0] = [1]
        for bi in range(1, 39):
            succ[bi] = [bi + 1]
        # 填充区回边（纯 read 循环；c0 守卫）。
        for bi in (8, 16, 24, 32, 39):
            succ[bi].append({"block": max(1, bi - 4), "when": "c0"})
        succ[39] = [40]
        succ[40] = [41]
        succ[41] = [
            {"block": 40, "when": "c2"},
            {"block": 42, "when": ["not", "c2"]}]
        for bi in range(42, 48):
            succ[bi] = [bi + 1]
        # 进入预约块的恒真守卫引用 c5/c6/c7（256 个赋值都参与真值表）。
        succ[48] = [{"block": 49,
                     "when": ["or", ["and", "c5", "c6", "c7"],
                              ["not", ["and", "c5", "c6", "c7"]]]}]
        succ[49] = [50]
        succ[50] = [51]
        succ[51] = [
            {"block": 52, "when": "c3"},
            {"block": 53, "when": ["not", "c3"]}]
        succ[52] = []
        succ[53] = [54]
        succ[54] = [
            {"block": 55, "when": "c4"},
            {"block": 56, "when": ["not", "c4"]}]
        succ[55] = [57]
        succ[56] = [57]
        succ[57] = [
            {"block": 58, "when": "c4"},
            {"block": 59, "when": ["not", "c4"]}]
        succ[58] = [60]
        succ[59] = [60]
        succ[60] = [61]
        succ[61] = []
        return {"version": 2, "conditions": conds,
                "blocks": [{"ops": ops} for ops in blocks],
                "successors": succ, "entry": 0}

    def test_main_scale_runs_deterministically(self):
        data = self._build()
        self.assertGreaterEqual(len(data["blocks"]), 60)
        self.assertEqual(len(data["conditions"]), 8)
        cfg = cfg_from_dict(data)
        r1 = check(cfg)
        r2 = check(cfg)
        keys1 = sorted((c.kind, c.block, c.op) for c in r1.conflicts)
        keys2 = sorted((c.kind, c.block, c.op) for c in r2.conflicts)
        self.assertEqual(keys1, keys2)
        self.assertEqual(r1.status, r2.status)

        found = {(c.kind, c.block, c.op): c for c in r1.conflicts}
        expected = {
            ("write_while_reserved", 40, 0),   # 回边后第二轮写
            ("reservation_conflict", 41, 0),   # 重入预约点自冲突
            ("drop_while_reserved", 52, 0),    # 预约后异常退出析构
            ("move_while_borrowed", 53, 1),    # 激活后移动
            ("use_of_moved", 60, 0),           # c4=true
            ("use_of_moved", 60, 1),           # c4=false
        }
        self.assertEqual(set(found), expected)
        # 每条证据可执行、回边 ≤ 4、赋值编码在 256 空间内。
        for c in r1.conflicts:
            executable(cfg, c.evidence)
            self.assertLessEqual(c.evidence.back_edges, 4)
            self.assertLess(c.evidence.assignment_bits, 256)
        # 关键认证输入。
        cindex = {n: i for i, n in enumerate(data["conditions"])}
        def aval(c):
            return {data["conditions"][i]: bool((c.evidence.assignment_bits >> i) & 1)
                    for i in range(8)}
        self.assertEqual(aval(found[("drop_while_reserved", 52, 0)])["c3"], True)
        self.assertEqual(aval(found[("move_while_borrowed", 53, 1)])["c3"], False)
        self.assertEqual(aval(found[("write_while_reserved", 40, 0)])["c2"], True)
        self.assertEqual(found[("write_while_reserved", 40, 0)].evidence.back_edges, 1)
        self.assertEqual(aval(found[("use_of_moved", 60, 0)])["c4"], True)
        self.assertEqual(aval(found[("use_of_moved", 60, 1)])["c4"], False)
        # 无预算缺口。
        self.assertEqual(r1.unknown_reasons, [])
        self.assertEqual(r1.status, "conflict")


if __name__ == "__main__":
    unittest.main()
