"""对小程序真正展开片段，与摘要检查逐项对照。

两条相互独立的实现路线：

* 被测核心：``check_program`` —— 片段摘要（前置条件 + 状态变化）+ 按实参
  路径替换接入主 CFG 不动点；
* 展开对照：``tests._inline.inline_program``（纯 dict 变换，不 import 被测
  核心）把每次调用克隆成真实 CFG 块、局部 loan 在出口补 ``end_loan``、
  多个出口汇入同一续延块，再交给原有 ``check`` 在展开后的普通 CFG 上跑。

两者的冲突集合（含片段内坐标）必须逐例一致。分别覆盖：

* 主 CFG 为前向边 DAG（结构无环）；
* 主 CFG 含回边/自环（片段仍无环；摘要接入主不动点）。

片段无环、调用图无环由校验强制；本测试只构造合法程序。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check, check_program, program_from_dict
from tests._inline import inline_program, normalize_conflict_key

FIELDS = ("x", "y")


def make_tree(rng, depth):
    if depth == 0 or rng.random() < 0.4:
        return None
    return {f: make_tree(rng, depth - 1)
            for f in rng.sample(FIELDS, rng.randint(1, len(FIELDS)))}


def all_paths(ty, prefix):
    out = [prefix]
    if isinstance(ty, dict):
        for f, sub in ty.items():
            out.extend(all_paths(sub, prefix + [f]))
    return out


def make_segment(rng, name, max_blocks):
    nparams = rng.randint(1, 2)
    params = [f"p{i}" for i in range(nparams)]
    sn = rng.randint(1, max_blocks)
    blocks = []
    pool = [f"g{name}_{k}" for k in range(3)]
    used = []

    def fresh():
        for lid in pool:
            if lid not in used:
                used.append(lid)
                return lid
        return None

    for _ in range(sn):
        ops = []
        for _ in range(rng.randint(0, 3)):
            par = rng.choice(params)
            formal = [par] + (["x"] if rng.random() < 0.4 else [])
            roll = rng.random()
            if roll < 0.2:
                ops.append({"op": "move", "path": formal})
            elif roll < 0.4:
                lid = fresh()
                if lid is not None:
                    ops.append({"op": "borrow_mut", "loan_id": lid,
                                "path": formal})
            elif roll < 0.52:
                lid = fresh()
                if lid is not None:
                    ops.append({"op": "borrow_shared", "loan_id": lid,
                                "path": formal})
            elif roll < 0.66 and used:
                ops.append({"op": "end_loan", "loan_id": rng.choice(used)})
            elif roll < 0.84:
                ops.append({"op": "read", "path": formal})
            else:
                ops.append({"op": "write", "path": formal,
                            "reinit": rng.random() < 0.4})
        blocks.append({"ops": ops})

    succs = [[] for _ in range(sn)]
    for j in range(1, sn):  # 前向边 -> 片段无环
        succs[j - 1].append(j)
        if rng.random() < 0.3 and j - 2 >= 0:
            succs[j - 2].append(j)
    return {"params": params, "blocks": blocks,
            "successors": succs, "entry": 0}


def generate_program(rng, *, cyclic, max_main_blocks):
    n = rng.randint(1, max_main_blocks)
    types = {f"v{k}": make_tree(rng, 2) for k in range(rng.randint(1, 2))}
    legal = []
    for var, ty in types.items():
        legal.extend(all_paths(ty, [var]))

    segments = {}
    names = []
    for si in range(rng.randint(1, 2)):
        nm = f"F{si}"
        names.append(nm)
        segments[nm] = make_segment(rng, nm, 3)

    blocks = [[{"op": "declare", "var": v, "ty": ty}
               for v, ty in types.items()]]
    blocks += [[] for _ in range(n - 1)]
    main_loan_seq = 0
    for bi in range(n):
        for _ in range(rng.randint(0 if bi == 0 else 1, 4)):
            apath = list(rng.choice(legal))
            roll = rng.random()
            if roll < 0.4:
                nm = rng.choice(names)
                seg = segments[nm]
                args = [[p, list(rng.choice(legal))] for p in seg["params"]]
                blocks[bi].append({"op": "call", "segment": nm, "args": args})
            elif roll < 0.58:
                blocks[bi].append({"op": "move", "path": apath})
            elif roll < 0.76:
                blocks[bi].append({"op": "read", "path": apath})
            elif roll < 0.9:
                blocks[bi].append({"op": "write", "path": apath,
                                   "reinit": rng.random() < 0.4})
            else:
                blocks[bi].append(
                    {"op": "borrow_mut", "loan_id": f"M{main_loan_seq}",
                     "path": apath})
                main_loan_seq += 1

    succs = [[] for _ in range(n)]
    for j in range(1, n):  # 前向链保证全部块可达
        succs[j - 1].append(j)
    if cyclic:
        for i in range(n):  # 回边/自环制造环
            if len(succs[i]) >= 2:
                continue
            if rng.random() < 0.4:
                j = rng.randint(0, i)
                if j not in succs[i]:
                    succs[i].append(j)
    else:
        for j in range(2, n):  # 额外前向边仍是 DAG
            if rng.random() < 0.3:
                i = rng.randint(0, j - 1)
                if len(succs[i]) < 2 and j not in succs[i]:
                    succs[i].append(j)
    return {"cfg": {"blocks": [{"ops": o} for o in blocks],
                    "successors": succs, "entry": 0},
            "segments": segments}


def normalized_inline_conflicts(result, origins):
    out = set()
    for c in result.conflicts:
        _, mb, mo, sb, so = normalize_conflict_key(c.block, c.op, origins)
        out.add((c.kind, mb, mo, sb, so))
    return out


def core_conflicts(result):
    return {(c.kind, c.block, c.op, c.seg_block, c.seg_op)
            for c in result.conflicts}


class TestInlineExpansionCrossCheck(unittest.TestCase):
    def _run(self, seed, trials, cyclic, max_main_blocks):
        rng = random.Random(seed)
        checked = 0
        for t in range(trials):
            prog = generate_program(
                rng, cyclic=cyclic, max_main_blocks=max_main_blocks)
            try:
                program_from_dict(prog)
            except Exception:
                continue  # 形参后缀替换后字段越界等非法程序：校验会拒绝
            # 展开规模必须落在主 CFG 的 50 块接口上限内。
            inlined, origins = inline_program(prog)
            if len(inlined["blocks"]) > 50:
                continue
            inline_result = check(cfg_from_dict(inlined))
            core_result = check_program(program_from_dict(prog))
            ki = normalized_inline_conflicts(inline_result, origins)
            kc = core_conflicts(core_result)
            self.assertEqual(
                ki, kc,
                msg=f"seed={seed} trial={t} cyclic={cyclic}\n"
                    f"inline-only={sorted(ki - kc)}\n"
                    f"core-only={sorted(kc - ki)}\nprog={prog}")
            checked += 1
        self.assertGreater(checked, 0)

    def test_dag_main_cfg(self):
        # 主 CFG 前向无环：300 个候选，固定种子。
        self._run(20260925, 300, cyclic=False, max_main_blocks=6)

    def test_cyclic_main_cfg(self):
        # 主 CFG 含回边/自环：片段仍无环，摘要接入主不动点。
        self._run(20260926, 300, cyclic=True, max_main_blocks=3)


if __name__ == "__main__":
    unittest.main()
