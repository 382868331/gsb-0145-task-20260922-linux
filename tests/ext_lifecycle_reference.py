"""v2 小规模有界穷举参考对照（独立解释器，不调用被测核心）。

固定种子确定性生成带条件、循环、两阶段借用、drop 的小 CFG
（<=7 块、<=2 个条件、记录树很小、4 轮循环展开），对照：

* 被测核心 ``check(cfg, lifecycle=True)`` 的确定冲突集合；
* ``tests/_reference_lifecycle`` 独立有界穷举解释器的
  ``{(kind, block, op): 可达条件赋值行}``。

两者冲突键集合必须一致；且核心给出的证据赋值行必须在参考的可达行集合内。
核心在该规模下不允许出现 unknown（预算外/不可辨识），否则视为分歧。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check
from tests._reference_lifecycle import reference_lifecycle_conflicts

FIELDS = ("x", "y")


def make_type(rng, depth):
    if depth == 0 or rng.random() < 0.3:
        return None
    return {f: make_type(rng, depth - 1) for f in
            rng.sample(FIELDS, rng.randint(1, len(FIELDS)))}


def all_paths(ty, prefix):
    out = [prefix]
    if isinstance(ty, dict):
        for f, sub in ty.items():
            out.extend(all_paths(sub, (*prefix, f)))
    return out


def make_formula(rng, atoms, depth=0):
    if not atoms or depth >= 2:
        return rng.choice(atoms) if atoms else None
    roll = rng.random()
    if roll < 0.5 or depth > 0:
        return rng.choice(atoms)
    if roll < 0.75:
        return {"not": make_formula(rng, atoms, depth + 1)}
    op = rng.choice(("and", "or"))
    return {op: [make_formula(rng, atoms, depth + 1)
                 for _ in range(2)]}


def generate_cfg(rng, n_max=7, k_max=2):
    n = rng.randint(2, n_max)
    k = rng.randint(0, k_max)
    atoms = [f"c{i}" for i in range(k)]
    types = {f"v{t}": make_type(rng, 1) for t in range(rng.randint(1, 2))}
    legal = []
    for root, ty in types.items():
        legal.extend(all_paths(ty, (root,)))

    blocks = [[{"op": "declare", "var": v, "ty": ty}
               for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]

    loan_ids = [f"L{i}" for i in range(7)]
    used = {}          # loan_id -> "shared"/"mut"/"reserve"
    activated = set()

    def fresh(kind):
        for lid in loan_ids:
            if lid not in used:
                used[lid] = kind
                return lid
        return None

    for bi in range(1, n):
        for _ in range(rng.randint(0, 3)):
            path = list(rng.choice(legal))
            roll = rng.random()
            if roll < 0.16:
                blocks[bi].append({"op": "move", "path": path})
            elif roll < 0.30:
                lid = fresh("shared")
                if lid:
                    blocks[bi].append(
                        {"op": "borrow_shared", "loan_id": lid, "path": path})
            elif roll < 0.44:
                lid = fresh("mut")
                if lid:
                    blocks[bi].append(
                        {"op": "borrow_mut", "loan_id": lid, "path": path})
            elif roll < 0.62:
                lid = fresh("reserve")
                if lid:
                    blocks[bi].append(
                        {"op": "borrow_reserve", "loan_id": lid, "path": path})
                    if rng.random() < 0.7:
                        blocks[bi].append(
                            {"op": "borrow_activate", "loan_id": lid})
                        activated.add(lid)
            elif roll < 0.72 and used:
                lid = rng.choice(list(used))
                if used[lid] == "reserve" and lid not in activated and rng.random() < 0.5:
                    blocks[bi].append(
                        {"op": "borrow_activate", "loan_id": lid})
                    activated.add(lid)
                else:
                    blocks[bi].append({"op": "end_loan", "loan_id": lid})
            elif roll < 0.88:
                blocks[bi].append({"op": "read", "path": path})
            else:
                blocks[bi].append(
                    {"op": "write", "path": path,
                     "reinit": rng.random() < 0.4})
        # 块尾偶尔 drop（声明块 0 之外的块都在声明作用域内）。
        if rng.random() < 0.35:
            blocks[bi].append({"op": "drop"})

    # 边：链保证可达；随机加前向/回边（每块 <=2 出边），允许成环。
    succs = [[] for _ in range(n)]
    for j in range(1, n):
        succs[j - 1].append(j)
    for bi in range(n):
        candidates = [s for s in range(n) if s != bi]
        rng.shuffle(candidates)
        for s in candidates[:rng.randint(0, 1)]:
            if s not in succs[bi]:
                succs[bi].append(s)
        if len(succs[bi]) > 2:
            succs[bi] = succs[bi][:2]
    # 边守卫。
    out_successors = []
    for bi, targets in enumerate(succs):
        edges = []
        for s in targets:
            if atoms and rng.random() < 0.5:
                edges.append({"block": s,
                              "when": make_formula(rng, atoms)})
            else:
                edges.append(s)
        out_successors.append(edges)

    return {
        "blocks": [{"ops": ops} for ops in blocks],
        "successors": out_successors,
        "entry": 0,
        "conditions": atoms,
    }


class TestLifecycleReferenceCrossCheck(unittest.TestCase):
    def test_matches_bounded_reference(self):
        rng = random.Random(20260924)
        trials = 500
        mismatches = []
        for t in range(trials):
            data = generate_cfg(rng)
            try:
                cfg = cfg_from_dict(data, max_blocks=64)
            except Exception:
                continue
            result = check(cfg, lifecycle=True)
            core = {(c.kind, c.block, c.op) for c in result.conflicts}
            ref = reference_lifecycle_conflicts(data, unroll=4)
            ref_keys = set(ref)
            if core != ref_keys or (result.status == "unknown" and not ref_keys
                                    and result.unknown_reasons):
                mismatches.append((t, core, ref_keys, data,
                                   result.unknown_reasons))
                if len(mismatches) >= 3:
                    break
            # 证据赋值行必须在参考可达行集合中。
            ev_by_key = {(e.kind, e.block, e.op): e for e in result.evidence}
            names = data["conditions"]
            for key, rows in ref.items():
                ev = ev_by_key.get(key)
                self.assertIsNotNone(ev, f"trial {t}: missing evidence {key}")
                row = sum((1 << i) for i, name in enumerate(names)
                          if ev.assignment[name])
                self.assertIn(row, rows,
                              f"trial {t}: evidence row {row} not in {rows}")
        if mismatches:
            t, core, ref_keys, data, reasons = mismatches[0]
            self.fail(
                f"第 {t} 个 CFG 与有界穷举参考不一致\n"
                f"core={sorted(core)}\nref={sorted(ref_keys)}\n"
                f"unknown={reasons}\nCFG={data}")

    def test_deterministic_seed_settles(self):
        rng = random.Random(20260924)
        data = generate_cfg(rng)
        cfg1 = cfg_from_dict(data, max_blocks=64)
        cfg2 = cfg_from_dict(data, max_blocks=64)
        r1 = check(cfg1, lifecycle=True)
        r2 = check(cfg2, lifecycle=True)
        self.assertEqual(
            [(c.kind, c.block, c.op) for c in r1.conflicts],
            [(c.kind, c.block, c.op) for c in r2.conflicts])


if __name__ == "__main__":
    unittest.main()
