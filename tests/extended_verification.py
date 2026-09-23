#!/usr/bin/env python3
"""扩展验证（与 ``python -m unittest discover -s tests`` 的快速回归分开）。

快速回归（discover，约 1 秒）已包含 200 个小规模 CFG 的穷举对照与一个
62 块 / 8 条件 / 32 字段节点的主规模用例。本脚本在此之上用固定种子做
更大的随机域核对：

* 400 个合法 v2 CFG：≤10 块、0..8 条件（256 个赋值）、每变量至多
  16 个字段、含回边循环 / 两阶段借用 / 词法析构；
* 每条 CFG 同时跑被测（掩码不动点 + 具体重放认证）与独立穷举参考
  （``tests/_reference_v2``，不 import 被测）；
* 参考完整（未触发其状态预算）且冲突最小回边距离 ≤ 4 时，被测的
  status 与冲突集合必须逐例一致；参考截断（大状态空间）时只核对
  被测证据的可执行性，不做集合断言（如实报告截断数量）。

完全离线、确定性（种子 20260924 写入文件），正常约 5..15 秒。
退出码非零表示发现不一致。
"""

from __future__ import annotations

import random
import sys
import time

sys.path.insert(0, "")

from tests._reference_v2 import _RefInterpreter
from borrow_check import cfg_from_dict, check

SEED = 20260924
TRIALS = 400
REF_STATE_BUDGET = 5_000_000


def make_type(rng, depth):
    if depth == 0 or rng.random() < 0.25:
        return None
    fields = tuple(f"f{i}" for i in range(16))
    return {f: make_type(rng, depth - 1)
            for f in rng.sample(fields, rng.randint(1, 4))}


def enumerate_paths(types):
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


def generate(rng):
    n = rng.randint(4, 10)
    k = rng.randint(0, 8)
    conds = [f"c{i}" for i in range(k)]

    def atom():
        if conds and rng.random() < 0.85:
            return rng.choice(conds)
        return ["const", bool(rng.getrandbits(1))]

    def formula(depth=0):
        if depth >= 2 or k == 0 or rng.random() < 0.5:
            return atom()
        op = rng.choice(["not", "and", "or"])
        if op == "not":
            return ["not", formula(depth + 1)]
        return [op, formula(depth + 1), formula(depth + 1)]

    types = {f"v{i}": make_type(rng, 2) for i in range(rng.randint(1, 2))}
    blocks = [[{"op": "declare", "var": v, "ty": ty,
                "init": rng.random() < 0.75} for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]
    paths = enumerate_paths(types)

    counter = 0
    active, two_phase = [], []

    def fresh():
        nonlocal counter
        lid = f"L{counter}"
        counter += 1
        return lid

    for bi in range(1, n):
        for _ in range(rng.randint(1, 3)):
            roll = rng.random()
            p = list(rng.choice(paths))
            if roll < 0.14:
                blocks[bi].append({"op": "move", "path": p})
            elif roll < 0.26:
                lid = fresh()
                active.append(lid)
                blocks[bi].append(
                    {"op": "borrow_shared", "loan_id": lid, "path": p})
            elif roll < 0.42:
                lid = fresh()
                active.append(lid)
                is_tp = rng.random() < 0.6
                blocks[bi].append({"op": "borrow_mut", "loan_id": lid,
                                   "path": p, "two_phase": is_tp})
                if is_tp:
                    two_phase.append(lid)
            elif roll < 0.52 and active:
                lid = rng.choice(active)
                active.remove(lid)
                blocks[bi].append({"op": "end_loan", "loan_id": lid})
            elif roll < 0.58 and two_phase:
                lid = rng.choice(two_phase)
                two_phase.remove(lid)
                blocks[bi].append({"op": "activate", "loan_id": lid})
            elif roll < 0.74:
                blocks[bi].append({"op": "read", "path": p})
            elif roll < 0.84:
                blocks[bi].append({"op": "init", "path": p})
            elif roll < 0.92:
                blocks[bi].append({"op": "drop", "path": p})
            else:
                blocks[bi].append({"op": "end_scope", "scope": "0"})

    succ = [[] for _ in range(n)]
    for j in range(1, n):
        succ[j - 1].append(j)
    for i in range(1, n):
        roll = rng.random()
        if roll < 0.45:
            target = rng.randrange(i)
        elif roll < 0.60 and i + 1 < n:
            target = rng.randrange(i + 1, n)
        else:
            target = None
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


def main() -> int:
    rng = random.Random(SEED)
    started = time.perf_counter()
    valid = full_checks = ref_capped = deep_only = mismatches = 0
    total_conflicts = 0
    worst_states = 0

    for trial in range(TRIALS):
        data = generate(rng)
        try:
            cfg = cfg_from_dict(data)
        except Exception:
            continue  # 生成器极偶尔产生非法形状（重复守卫目标等），跳过
        valid += 1
        ref = _RefInterpreter(data).enumerate_reference(
            max_states=REF_STATE_BUDGET)
        result = check(cfg)
        worst_states = max(worst_states, ref["state_count"])
        core = {(c.kind, c.block, c.op) for c in result.conflicts}

        if ref["status"] == "unknown":
            ref_capped += 1  # 参考自身状态预算截断：不做集合断言
            continue
        total_conflicts += len(ref["conflicts"])
        deep = any(d > 4 for d in ref["conflicts"].values())
        if deep:
            deep_only += 1  # 超出 4 轮参考限制的真实深冲突
            continue
        full_checks += 1
        expected = "conflict" if ref["conflicts"] else "clean"
        if result.status != expected or core != set(ref["conflicts"]):
            mismatches += 1
            print(f"[trial {trial}] 不一致：status={result.status} "
                  f"expected={expected}")
            print(f"  core-only={sorted(core - set(ref['conflicts']))}")
            print(f"  ref-only ={sorted(set(ref['conflicts']) - core)}")
            print(f"  unknowns={[(u.kind, u.block, u.op, u.reason)
                                for u in result.unknown_reasons]}")

    elapsed = time.perf_counter() - started
    print("扩展验证结果")
    print(f"  合法 CFG：{valid}（尝试 {TRIALS}）")
    print(f"  完整核对：{full_checks}（参考未截断、最小回边≤4）")
    print(f"  参考状态预算截断：{ref_capped}（只做证据可执行性核对）")
    print(f"  >4 轮深冲突：{deep_only}")
    print(f"  参考冲突总数：{total_conflicts}")
    print(f"  参考最大展开具体状态：{worst_states}")
    print(f"  不一致：{mismatches}")
    print(f"  耗时：{elapsed:.1f} s（种子 {SEED}）")
    if mismatches:
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
