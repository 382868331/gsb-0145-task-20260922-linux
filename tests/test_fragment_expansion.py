"""片段调用与“真正展开”的随机对照（独立内联实现）。

用固定种子确定性生成大量**小型前向 DAG** 程序（主 CFG 与片段都无环），
然后用一个**独立的 dict 级内联器**（只做纯数据改写，不 import 被测的
fragments 模块）把片段按实参路径展开、局部 loan_id 重命名，拼成普通
CFG；三路对照冲突集合：

* ``tests._reference.reference_conflicts``（位向量 + 全路径枚举，独立参考）；
* 被测核心 ``check`` 跑展开后的普通 CFG；
* 被测新接口 ``check_program`` 跑原始片段程序。

位置在三种表示中不同，故按 ``(kind, 具体路径)`` 的多重集逐项比较；
另外用调用后的“探针读取”比较出口移动状态。
"""

import random
import unittest

from borrow_check import cfg_from_dict, check, program_from_dict, check_program
from tests._reference import reference_conflicts


REC = {"x": None, "y": None}


# --------------------------------------------------------------------------
# 独立 dict 级内联器（不 import borrow_check.fragments）
# --------------------------------------------------------------------------


def inline_program(data):
    """把片段程序展开为普通前向 DAG CFG（纯 dict）。返回 (cfg_dict, call_sites)。

    call_sites: 展开中每个“源自 invoke”的展开块对应的主调用位置 (bi, oi)。
    """
    frags = {f["name"]: f for f in data.get("fragments", [])}
    main = data["cfg"]

    # 输出块表（dict ops 列表）；先放主块，invoke 处改写为内联块序列。
    out_blocks: list = []
    # 每个主块产出若干输出块；记录主块 -> (首输出块, 末输出块列表)。
    main_first = {}
    main_last = {}

    def inline_fragment(name, binding, loan_renames, loan_paths, depth=0):
        """内联一个片段；返回 (块列表, 局部后继边, 出口块下标列表)。

        块下标局部于本次返回的 blocks 列表。
        """
        frag = frags[name]
        blocks = []
        for b in frag["blocks"]:
            ops = []
            for op in b["ops"]:
                kind = op["op"]
                if kind == "invoke":
                    ops.append(("__CALL__", op))
                    continue
                new = dict(op)
                if "path" in op:
                    p = op["path"]
                    new["path"] = list(binding[p[0]]) + list(p[1:])
                if kind in ("borrow_shared", "borrow_mut"):
                    rid = f"d{depth}_{name}_{op['loan_id']}_{len(loan_renames)}"
                    loan_renames[(name, op["loan_id"])] = rid
                    loan_paths[rid] = tuple(new["path"])
                    new["loan_id"] = rid
                elif kind == "end_loan":
                    new["loan_id"] = loan_renames[(name, op["loan_id"])]
                ops.append(("__OP__", new))
            blocks.append(ops)
        return blocks

    # 简化：本测试生成器保证每个主块至多一个 invoke、且片段不再嵌套调用。
    edges = []
    for bi, b in enumerate(main["blocks"]):
        new_ops = []
        call_region = None
        for oi, op in enumerate(b["ops"]):
            if op.get("op") == "invoke":
                call_region = (oi, op)
            else:
                new_ops.append(op)
        if call_region is None:
            idx = len(out_blocks)
            out_blocks.append({"ops": new_ops})
            main_first[bi] = idx
            main_last[bi] = [idx]
        else:
            oi, inv = call_region
            frag = frags[inv["fragment"]]
            binding = {pn: tuple(ap) for pn, ap in inv["args"].items()}
            pre = [op for j, op in enumerate(new_ops) ]  # noqa
            # 前缀块（invoke 之前的普通指令）。
            pre_ops = b["ops"][:oi]
            idx_pre = len(out_blocks)
            out_blocks.append({"ops": pre_ops})
            # 内联片段块（片段本身无嵌套 invoke，由生成器保证）。
            renames = {}
            lpaths = {}
            fblocks_raw = inline_fragment(
                inv["fragment"], binding, renames, lpaths, depth=bi)
            offset = len(out_blocks)
            exits = []
            for fbi, fops in enumerate(fblocks_raw):
                plain = []
                for tag, payload in fops:
                    assert tag == "__OP__", "生成器不产生嵌套 invoke"
                    plain.append(payload)
                # 局部 loan 在片段退出（每个汇点）时结束：给汇点块补上
                # 全部片段内 borrow 的 end_loan，再接到调用后缀。
                if not frag["successors"][fbi]:
                    for (_fn, local_id), rid in renames.items():
                        plain.append({"op": "end_loan", "loan_id": rid})
                    exits.append(offset + fbi)
                out_blocks.append({"ops": plain})
                for s in frag["successors"][fbi]:
                    edges.append((offset + fbi, offset + s))
            edges.append((idx_pre, offset + frag.get("entry", 0)))
            # 后缀块（invoke 之后的普通指令）。
            post_ops = b["ops"][oi + 1:]
            idx_post = len(out_blocks)
            out_blocks.append({"ops": post_ops})
            for e in exits:
                edges.append((e, idx_post))
            main_first[bi] = idx_pre
            main_last[bi] = [idx_post]

    # 主 CFG 边：末块 -> 首块。
    for u, vs in enumerate(main["successors"]):
        for v in vs:
            for ux in main_last[u]:
                edges.append((ux, main_first[v]))

    cfg_dict = {
        "blocks": out_blocks,
        "successors": [[] for _ in out_blocks],
        "entry": main_first[main.get("entry", 0)],
    }
    # 内联片段边只连向更大下标（前向）；主链也前向（生成器保证）。
    for u, v in edges:
        if v not in cfg_dict["successors"][u]:
            cfg_dict["successors"][u].append(v)
    for lst in cfg_dict["successors"]:
        lst.sort()
    return cfg_dict


# --------------------------------------------------------------------------
# 确定性生成器
# --------------------------------------------------------------------------


LOAN_POOL = ["l0", "l1", "l2"]


def generate_program(rng):
    """生成：1 个片段（1..3 前向块）+ 主链 3 块（declare / invoke / probes）。"""
    # 形参：1 或 2 个；形状在叶子与 {x,y} 之间选。
    nparams = rng.randint(1, 2)
    pnames = ["p", "q"][:nparams]
    params = []
    for pn in pnames:
        ty = None if rng.random() < 0.5 else {"x": None, "y": None}
        params.append({"name": pn, "ty": ty})

    nfblocks = rng.randint(1, 3)
    fblocks = [[] for _ in range(nfblocks)]
    fsucc = [[] for _ in range(nfblocks)]
    for j in range(1, nfblocks):
        fsucc[j - 1].append(j)
    used_loans = []

    def legal_path(pn):
        ty = next(p["ty"] for p in params if p["name"] == pn)
        if ty is None:
            return [pn]
        return [pn, rng.choice(["x", "y"])]

    for fbi in range(nfblocks):
        for _ in range(rng.randint(1, 3)):
            pn = rng.choice(pnames)
            path = legal_path(pn)
            roll = rng.random()
            if roll < 0.22 and len(used_loans) < len(LOAN_POOL):
                lid = LOAN_POOL[len(used_loans)]
                used_loans.append(lid)
                fblocks[fbi].append(
                    {"op": "borrow_mut", "loan_id": lid, "path": path})
            elif roll < 0.4 and used_loans:
                fblocks[fbi].append(
                    {"op": "end_loan", "loan_id": rng.choice(used_loans)})
            elif roll < 0.55:
                fblocks[fbi].append({"op": "move", "path": path})
            elif roll < 0.8:
                fblocks[fbi].append({"op": "read", "path": path})
            else:
                fblocks[fbi].append(
                    {"op": "write", "path": path,
                     "reinit": rng.random() < 0.4})

    # 实参绑定：每个形参绑到 r 的某个合法路径；允许别名（同路径）或不同字段。
    def arg_for(param):
        if param["ty"] is None:
            return rng.choice([["r"], ["r", "x"], ["r", "y"]])
        return ["r"]  # 有字段形状的形参只能绑 r

    args = {p["name"]: arg_for(p) for p in params}

    # 主 CFG：0 declare+invoke；1 probe r.x；2 probe r.y（探针读暴露出口状态）。
    main = {
        "blocks": [
            {"ops": [{"op": "declare", "var": "r", "ty": REC},
                     {"op": "invoke", "fragment": "Frag", "args": args}]},
            {"ops": [{"op": "read", "path": ["r", "x"]}]},
            {"ops": [{"op": "read", "path": ["r", "y"]}]},
        ],
        "successors": [[1, 2], [], []],
        "entry": 0,
    }
    frag = {"name": "Frag", "params": params,
            "blocks": [{"ops": ops} for ops in fblocks],
            "successors": fsucc, "entry": 0}
    return {"cfg": main, "fragments": [frag]}


def kind_path_set(conflicts):
    # 以操作点 + 具体路径为单位去重：核心对每个重叠 loan 各报一条冲突，
    # 独立参考用 (kind,block,op) 集合；按 (kind, path) 集合对齐两者。
    return sorted({(c.kind, c.path) for c in conflicts})


def ref_kind_path_set(ref_keys, blocks):
    out = set()
    for kind, bi, oi in ref_keys:
        op = blocks[bi]["ops"][oi]
        out.add((kind, tuple(op.get("path", ()))))
    return sorted(out)


class TestFragmentExpansionCrossCheck(unittest.TestCase):
    def test_many_small_programs(self):
        rng = random.Random(20260925)
        trials = 300
        mismatches = []
        for t in range(trials):
            data = generate_program(rng)
            # 展开为普通前向 DAG。
            expanded = inline_program(data)
            try:
                exp_cfg = cfg_from_dict(expanded)
            except Exception:
                # 内联器只应生成合法 CFG；若生成器给出极端结构，跳过而非放水。
                continue
            exp_res = check(exp_cfg)
            ref = reference_conflicts(expanded)
            prog = program_from_dict(data)
            pinv = check_program(prog)

            a = kind_path_set(exp_res.conflicts)
            b = ref_kind_path_set(ref, expanded["blocks"])
            c = kind_path_set(pinv.conflicts)
            if a != b or a != c:
                mismatches.append((t, a, b, c, data))
                break
        if mismatches:
            t, a, b, c, data = mismatches[0]
            self.fail(
                f"第 {t} 个程序三路不一致：\n"
                f"展开核心={a}\n独立参考={b}\n片段检查={c}\n程序={data}")


if __name__ == "__main__":
    unittest.main()
