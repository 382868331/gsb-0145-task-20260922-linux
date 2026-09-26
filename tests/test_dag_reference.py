"""小无环图（DAG）枚举路径对照。

用仓库内固定种子的确定性生成器构造大量小型前向边 CFG（边只连向更大下标，
结构性保证无环），对照两个独立实现：

* 被测核心：``borrow_check.check``（反链 + worklist 不动点）；
* ``tests._reference.reference_conflicts``（类型树全路径位向量 +
  DAG 全块序列枚举解释，不 import 被测核心）。

只比较冲突集合 ``(kind, block, op)``。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check
from tests._reference import _all_paths, reference_conflicts

FIELDS = ("x", "y")


def make_type(rng, depth):
    if depth == 0 or rng.random() < 0.35:
        return None
    return {f: make_type(rng, depth - 1) for f in
            rng.sample(FIELDS, rng.randint(1, len(FIELDS)))}


def generate_cfg(rng):
    n = rng.randint(1, 8)
    # 块0 声明全部变量 -> 声明支配所有使用点。
    types = {f"v{k}": make_type(rng, 2) for k in range(rng.randint(1, 3))}
    legal_paths = []
    for root, ty in types.items():
        legal_paths.extend(_all_paths(ty, (root,)))

    # 块0 声明全部变量 -> 声明支配所有使用点；其余块先置空。
    blocks = [[{"op": "declare", "var": v, "ty": ty} for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]

    loan_pool = [f"L{k}" for k in range(8)]
    used_loans = []

    def fresh_loan():
        for lid in loan_pool:
            if lid not in used_loans:
                used_loans.append(lid)
                return lid
        return None

    reserve_pool = [f"R{k}" for k in range(6)]
    used_reserves = []

    def fresh_reserve():
        for rid in reserve_pool:
            if rid not in used_reserves:
                used_reserves.append(rid)
                return rid
        return None

    for bi in range(n):
        for _ in range(rng.randint(0 if bi == 0 else 1, 4)):
            path = list(rng.choice(legal_paths))
            roll = rng.random()
            if roll < 0.15:
                blocks[bi].append({"op": "move", "path": path})
            elif roll < 0.3:
                lid = fresh_loan()
                if lid is not None:
                    blocks[bi].append(
                        {"op": "borrow_shared", "loan_id": lid, "path": path})
            elif roll < 0.45:
                lid = fresh_loan()
                if lid is not None:
                    blocks[bi].append(
                        {"op": "borrow_mut", "loan_id": lid, "path": path})
            elif roll < 0.55 and used_loans:
                blocks[bi].append(
                    {"op": "end_loan", "loan_id": rng.choice(used_loans)})
            elif roll < 0.65:
                rid = fresh_reserve()
                if rid is not None:
                    blocks[bi].append(
                        {"op": "reserve", "reserve_id": rid, "path": path})
            elif roll < 0.75 and used_reserves:
                blocks[bi].append(
                    {"op": "activate", "reserve_id": rng.choice(used_reserves)})
            elif roll < 0.82 and used_reserves:
                blocks[bi].append(
                    {"op": "end_reserve", "reserve_id": rng.choice(used_reserves)})
            elif roll < 0.92:
                blocks[bi].append({"op": "read", "path": path})
            else:
                blocks[bi].append({"op": "write", "path": path,
                                   "reinit": rng.random() < 0.5})

    # 前向边构成 DAG；(j-1)->j 链保证所有块可达。
    successors = [[] for _ in range(n)]
    for j in range(1, n):
        edges = {j - 1}
        earlier = list(range(j))
        for i in earlier:
            if len(successors[i]) < 2 and rng.random() < 0.3:
                edges.add(i)  # 收集候选（按源点加入）
        for i in edges:
            successors[i].append(j)
    for lst in successors:
        lst.sort()

    return {
        "blocks": [{"ops": ops} for ops in blocks],
        "successors": successors,
        "entry": 0,
    }


class TestDAGReferenceCrossCheck(unittest.TestCase):
    def test_matches_reference_on_many_dags(self):
        rng = random.Random(20260922)
        trials = 400
        mismatches = []
        for t in range(trials):
            data = generate_cfg(rng)
            cfg = cfg_from_dict(data)
            core = {(c.kind, c.block, c.op) for c in check(cfg).conflicts}
            ref = reference_conflicts(data)
            if core != ref:
                mismatches.append((t, core ^ ref, data))
        if mismatches:
            t, diff, data = mismatches[0]
            self.fail(
                f"第 {t} 个 DAG 与独立参考不一致，对称差={sorted(diff)}\nCFG={data}")

    def test_walk_count_is_small(self):
        # 防御：确认该规模下全路径枚举是可承受的（不做长压力测试）。
        rng = random.Random(42)
        data = generate_cfg(rng)
        n = len(data["blocks"])
        self.assertLessEqual(n, 8)


if __name__ == "__main__":
    unittest.main()
