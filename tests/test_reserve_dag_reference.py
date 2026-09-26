"""含 reserve/activate 的小无环图枚举路径对照。

与 tests/test_dag_reference.py 同法：固定种子确定性生成小型前向边 DAG，
对照被测核心（反链 + must/may 不动点）与 tests/_reference.py 的独立参考
（位向量 + 全块序列逐路径解释，含预约状态机）。只比较冲突集合
(kind, block, op)。生成器保证覆盖：预约跨分支、重复激活、父/子字段重叠。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check
from tests._reference import _all_paths, reference_conflicts

FIELDS = ("x", "y")


def make_type(rng, depth):
    if depth == 0 or rng.random() < 0.3:
        return None
    return {f: make_type(rng, depth - 1) for f in
            rng.sample(FIELDS, rng.randint(1, len(FIELDS)))}


def generate_cfg(rng):
    n = rng.randint(1, 8)
    types = {f"v{k}": make_type(rng, 2) for k in range(rng.randint(1, 3))}
    legal_paths = []
    for root, ty in types.items():
        legal_paths.extend(_all_paths(ty, (root,)))

    blocks = [[{"op": "declare", "var": v, "ty": ty} for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]

    loan_pool = [f"L{k}" for k in range(6)]
    res_pool = [f"R{k}" for k in range(4)]
    used_loans, used_res = [], []

    def fresh(pool, used):
        for x in pool:
            if x not in used:
                used.append(x)
                return x
        return None

    for bi in range(n):
        for _ in range(rng.randint(0 if bi == 0 else 1, 4)):
            path = list(rng.choice(legal_paths))
            roll = rng.random()
            if roll < 0.14:
                blocks[bi].append({"op": "move", "path": path})
            elif roll < 0.26:
                lid = fresh(loan_pool, used_loans)
                if lid is not None:
                    blocks[bi].append(
                        {"op": "borrow_shared", "loan_id": lid, "path": path})
            elif roll < 0.38:
                lid = fresh(loan_pool, used_loans)
                if lid is not None:
                    blocks[bi].append(
                        {"op": "borrow_mut", "loan_id": lid, "path": path})
            elif roll < 0.54:
                rid = fresh(res_pool, used_res)
                if rid is not None:
                    blocks[bi].append(
                        {"op": "reserve", "reservation_id": rid, "path": path})
            elif roll < 0.68 and used_res:
                blocks[bi].append(
                    {"op": "activate", "reservation_id": rng.choice(used_res)})
            elif roll < 0.78 and (used_loans or used_res):
                blocks[bi].append({"op": "end_loan",
                                   "loan_id": rng.choice(used_loans + used_res)})
            elif roll < 0.9:
                blocks[bi].append({"op": "read", "path": path})
            else:
                blocks[bi].append({"op": "write", "path": path,
                                   "reinit": rng.random() < 0.5})

    successors = [[] for _ in range(n)]
    for j in range(1, n):
        edges = {j - 1}
        for i in range(j):
            if len(successors[i]) < 2 and rng.random() < 0.3:
                edges.add(i)
        for i in edges:
            successors[i].append(j)
    for lst in successors:
        lst.sort()

    return {
        "blocks": [{"ops": ops} for ops in blocks],
        "successors": successors,
        "entry": 0,
    }


def _op_kinds(data):
    return [op["op"] for b in data["blocks"] for op in b["ops"]]


class TestReserveDAGReferenceCrossCheck(unittest.TestCase):
    def test_matches_reference_on_many_dags(self):
        rng = random.Random(20260926)
        trials = 400
        mismatches = []
        seen = {"cross_branch": 0, "double_activate": 0, "nested_path": 0}
        for t in range(trials):
            data = generate_cfg(rng)
            # 覆盖性统计（不是期望值来源，只确认生成器触及关键形态）。
            kinds = _op_kinds(data)
            if "reserve" in kinds and "activate" in kinds:
                seen["cross_branch"] += 1
            if kinds.count("activate") >= 2:
                seen["double_activate"] += 1
            for b in data["blocks"]:
                for op in b["ops"]:
                    if op["op"] == "reserve" and len(op["path"]) >= 2:
                        seen["nested_path"] += 1
            cfg = cfg_from_dict(data)
            core = {(c.kind, c.block, c.op) for c in check(cfg).conflicts}
            ref = reference_conflicts(data)
            if core != ref:
                mismatches.append((t, core ^ ref, data))
        for key, count in seen.items():
            self.assertGreater(count, 0, f"生成器未覆盖 {key}")
        if mismatches:
            t, diff, data = mismatches[0]
            self.fail(
                f"第 {t} 个 DAG 与独立参考不一致，对称差={sorted(diff)}\nCFG={data}")


if __name__ == "__main__":
    unittest.main()

