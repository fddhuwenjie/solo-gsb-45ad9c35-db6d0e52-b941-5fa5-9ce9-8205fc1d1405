"""地层关联核心算法。

流程：
1. 深度统一为高程（孔口高程 - 深度）。
2. 以锁定标志层为“强制匹配锚点”，把参考孔与待校核孔的层序切成若干段；
   匹配路径禁止跨越锚点，从而保证标志层不被穿越。
3. 每段用带仿射间隙惩罚（open/extend）的动态规划做层序对齐，
   相似度由岩性、粒径、含水状态、高程差、置信度加权得到。
4. 正向表 + 反向表联合判定：哪些匹配/间隙边出现在任意一条最优路径上，
   由此给出并列解的歧义区间；并枚举最优路径（上限 100 条）。

并列解的等价口径：一个“方案”由匹配对集合唯一确定（两侧未匹配层集合随
之确定）。同一集合可对应多条 DP 路径（间隙区内“跳过参考层”与“跳过待
校核层”的先后顺序不同），为避免顺序重复计数，DP 只走规范形路径——同
一间隙段内参考侧间隙（GA）必须排在待校核侧间隙（GB）之前，即禁止 V 态
后紧跟 H 态。每个匹配对集合恰对应一条规范形路径，因此计数 DP 的并列解
数、路径枚举与最终方案列表使用同一等价关系。在 gap_open >= gap_extend
（含全零退化情形）下，任一路径重排为规范形不会变劣，最优总分与最优匹配
集合均不受影响。
"""
import math

from .domain import (
    lithology_similarity, grain_similarity, water_similarity, elev_similarity,
)

NEG = -1e18
EPS = 1e-7
MAX_SOLUTIONS = 100
COUNT_CAP = 9999          # 并列解计数封顶，避免大整数
ENUM_COUNT_LIMIT = 5000  # 解数超过该值时只给前 100 条，不遍历全图


def elevation_layer(layer, collar_elev):
    top = layer["top"]
    bottom = layer["bottom"]
    ztop = collar_elev - top
    zbottom = collar_elev - bottom
    layer = dict(layer)
    layer.update(ztop=ztop, zbottom=zbottom, zmid=(ztop + zbottom) / 2.0)
    return layer


def prepare_layers(raw_layers, collar_elev):
    """按深度排序并转换为高程层序（浅层在前 = 高程大的在前）。"""
    layers = sorted(raw_layers, key=lambda x: (x["top"], x["seq"]))
    return [elevation_layer(ly, collar_elev) for ly in layers]


def pair_similarity(a, b, weights, elev_scale):
    sl = lithology_similarity(a["lithology"], b["lithology"])
    sg = grain_similarity(a["grain"], b["grain"])
    sw = water_similarity(a["water"], b["water"])
    se = elev_similarity(a["zmid"] - b["zmid"], elev_scale)
    sc = (float(a["confidence"]) + float(b["confidence"])) / 2.0
    wsum = sum(weights[k] for k in
               ("lithology", "grain", "water", "elev", "confidence"))
    if wsum <= 0:
        total = 0.0
    else:
        total = (
            weights["lithology"] * sl
            + weights["grain"] * sg
            + weights["water"] * sw
            + weights["elev"] * se
            + weights["confidence"] * sc
        ) / wsum
    return total, {
        "lithology": sl, "grain": sg, "water": sw,
        "elev": se, "confidence": sc,
    }


# ---------------------------------------------------------------------------
# 单段序列对齐（局部坐标：A = 参考孔层，B = 待校核孔层）
# ---------------------------------------------------------------------------

def _align_segment(A, B, sim, gap_open, gap_extend):
    """返回段对齐结果。

    状态 D：上一步为匹配；H：上一步为 A 出列（B 方向间隙，参考层未匹配）；
    V：上一步为 B 出列（A 方向间隙，待校核层未匹配）。

    规范形约束：H 态不允许从 V 态转移（禁止 GB 后紧跟 GA），使同一间隙段内
    GA 一律排在 GB 之前；每个“匹配对集合”因此只对应一条 DP 路径，计数结果
    即为按等价类去重后的并列解数。
    """
    n, m = len(A), len(B)
    D = [[NEG] * (m + 1) for _ in range(n + 1)]
    H = [[NEG] * (m + 1) for _ in range(n + 1)]
    V = [[NEG] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0.0
    # 计数 DP（并列解个数）
    cD = [[0] * (m + 1) for _ in range(n + 1)]
    cH = [[0] * (m + 1) for _ in range(n + 1)]
    cV = [[0] * (m + 1) for _ in range(n + 1)]
    cD[0][0] = 1

    def add_count(cnt, best, val, c):
        if val > best + EPS:
            return val, c
        if abs(val - best) <= EPS:
            return best, min(COUNT_CAP, cnt + c)
        return best, cnt

    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            if i >= 1 and j >= 1:
                s = sim[(i, j)]
                best, cnt = NEG, 0
                for pv, pc in ((D[i-1][j-1], cD[i-1][j-1]),
                               (H[i-1][j-1], cH[i-1][j-1]),
                               (V[i-1][j-1], cV[i-1][j-1])):
                    best, cnt = add_count(cnt, best, pv + s, pc)
                D[i][j], cD[i][j] = best, cnt
            if i >= 1:
                best, cnt = NEG, 0
                best, cnt = add_count(cnt, best, H[i-1][j] - gap_extend, cH[i-1][j])
                best, cnt = add_count(cnt, best, D[i-1][j] - gap_open, cD[i-1][j])
                # 规范形：不允许 V -> H（GB 后不得紧跟 GA）
                H[i][j], cH[i][j] = best, cnt
            if j >= 1:
                best, cnt = NEG, 0
                best, cnt = add_count(cnt, best, V[i][j-1] - gap_extend, cV[i][j-1])
                best, cnt = add_count(cnt, best, D[i][j-1] - gap_open, cD[i][j-1])
                best, cnt = add_count(cnt, best, H[i][j-1] - gap_open, cH[i][j-1])
                V[i][j], cV[i][j] = best, cnt

    total = max(D[n][m], H[n][m], V[n][m])
    total_count = 0
    for tv, tc in ((D[n][m], cD[n][m]), (H[n][m], cH[n][m]), (V[n][m], cV[n][m])):
        if abs(tv - total) <= EPS:
            total_count = min(COUNT_CAP, total_count + tc)

    # 反向表：RD[i][j] = 以“匹配态”到达节点 (i,j) 后走到终点的最优 suffix
    RD = [[NEG] * (m + 1) for _ in range(n + 1)]
    RH = [[NEG] * (m + 1) for _ in range(n + 1)]
    RV = [[NEG] * (m + 1) for _ in range(n + 1)]
    RD[n][m] = RH[n][m] = RV[n][m] = 0.0
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                continue
            vals_d, vals_h, vals_v = [], [], []
            if i < n and j < m:
                suffix = sim[(i+1, j+1)] + RD[i+1][j+1]
                vals_d.append(suffix)
                vals_h.append(suffix)
                vals_v.append(suffix)
            if i < n:
                vals_d.append(-gap_open + RH[i+1][j])
                vals_h.append(-gap_extend + RH[i+1][j])
                # 规范形：V 态之后不得进入 H 态，vals_v 不加 H 方向
            if j < m:
                vals_d.append(-gap_open + RV[i][j+1])
                vals_h.append(-gap_open + RV[i][j+1])
                vals_v.append(-gap_extend + RV[i][j+1])
            RD[i][j] = max(vals_d)
            RH[i][j] = max(vals_h)
            # i<n 且 j==m 时 V 态无任何合法后继（H 被禁），视为不可达
            RV[i][j] = max(vals_v) if vals_v else NEG

    # ---- 判定每条匹配边 (i,j) 是否在某条最优路径上 ----
    match_edges = {}                 # (i,j) -> bool
    match_partners = {i: set() for i in range(1, n + 1)}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pre = max(D[i-1][j-1], H[i-1][j-1], V[i-1][j-1])
            on_path = abs(pre + sim[(i, j)] + RD[i][j] - total) <= 1e-5
            if on_path:
                match_edges[(i, j)] = True
                match_partners[i].add(j)

    # ---- 参考层 i 的“被跳过”位置（间隙边）----
    gap_positions = {i: set() for i in range(1, n + 1)}
    for i in range(1, n + 1):
        for j in range(0, m + 1):
            # 边 (i-1,j) -> (i,j)，A[i] 出列（规范形：前驱不含 V 态）
            best = max(
                H[i-1][j] - gap_extend,
                D[i-1][j] - gap_open,
            )
            if best <= NEG / 2:
                continue
            if abs(best + RH[i][j] - total) <= 1e-5:
                gap_positions[i].add(j)

    # ---- 主解回溯（偏好：匹配 > A间隙 > B间隙）----
    primary = []
    i, j, state = n, m, ("D" if abs(D[n][m] - total) <= EPS
                         else "H" if abs(H[n][m] - total) <= EPS else "V")
    while i > 0 or j > 0:
        if state == "D":
            s = sim[(i, j)]
            pre = max(D[i-1][j-1], H[i-1][j-1], V[i-1][j-1])
            primary.append(("M", i, j))
            state = "D" if abs(D[i-1][j-1] - pre) <= EPS else \
                    "H" if abs(H[i-1][j-1] - pre) <= EPS else "V"
            i, j = i - 1, j - 1
        elif state == "H":
            cands = (
                ("H", H[i-1][j] - gap_extend, H[i-1][j]),
                ("D", D[i-1][j] - gap_open, D[i-1][j]),
            )
            best = max(c[1] for c in cands)
            nxt = next(c[0] for c in cands if abs(c[1] - best) <= EPS)
            primary.append(("GA", i, 0))   # A[i] 未匹配
            i, state = i - 1, nxt
        else:
            cands = (
                ("V", V[i][j-1] - gap_extend, V[i][j-1]),
                ("D", D[i][j-1] - gap_open, D[i][j-1]),
                ("H", H[i][j-1] - gap_open, H[i][j-1]),
            )
            best = max(c[1] for c in cands)
            nxt = next(c[0] for c in cands if abs(c[1] - best) <= EPS)
            primary.append(("GB", 0, j))   # B[j] 未匹配
            j, state = j - 1, nxt
    primary.reverse()

    return {
        "total": total,
        "count": total_count,
        "steps": primary,
        "match_edges": match_edges,
        "match_partners": match_partners,
        "gap_positions": gap_positions,
        "D": D, "H": H, "V": V,
        "RD": RD, "RH": RH, "RV": RV,
    }


def _enum_segment_paths(seg, limit=MAX_SOLUTIONS):
    """在最优子图上 DFS 枚举段内并列最优路径（步骤为段内局部坐标，1 基）。"""
    A_len, B_len = seg["n"], seg["m"]
    total = seg["total"]
    sim = seg["sim"]
    D, H, V = seg["D"], seg["H"], seg["V"]
    RD, RH, RV = seg["RD"], seg["RH"], seg["RV"]
    go, ge = seg["gap_open"], seg["gap_extend"]
    results = []

    def dfs(i, j, state, path):
        if len(results) >= limit:
            return
        if i == 0 and j == 0:
            results.append(list(reversed(path)))
            return
        if state == "D":
            if i > 0 and j > 0:
                pre = max(D[i-1][j-1], H[i-1][j-1], V[i-1][j-1])
                if abs(pre + sim[(i, j)] + RD[i][j] - total) <= 1e-5:
                    for pst, pv in (("D", D[i-1][j-1]),
                                    ("V", V[i-1][j-1]),
                                    ("H", H[i-1][j-1])):
                        if abs(pv - pre) <= EPS:
                            path.append(("M", i, j))
                            dfs(i-1, j-1, pst, path)
                            path.pop()
                            if len(results) >= limit:
                                return
        elif state == "H":
            cands = (
                ("H", H[i-1][j] - ge, H[i-1][j]),
                ("D", D[i-1][j] - go, D[i-1][j]),
            ) if i > 0 else ()
            best = max((c[1] for c in cands), default=NEG)
            if best > NEG / 2 and abs(best + RH[i][j] - total) <= 1e-5:
                for pst, val, _ in cands:
                    if abs(val - best) <= EPS:
                        path.append(("GA", i, 0))
                        dfs(i-1, j, pst, path)
                        path.pop()
                        if len(results) >= limit:
                            return
        else:
            cands = (
                ("V", V[i][j-1] - ge, V[i][j-1]),
                ("D", D[i][j-1] - go, D[i][j-1]),
                ("H", H[i][j-1] - go, H[i][j-1]),
            ) if j > 0 else ()
            best = max((c[1] for c in cands), default=NEG)
            if best > NEG / 2 and abs(best + RV[i][j] - total) <= 1e-5:
                for pst, val, _ in cands:
                    if abs(val - best) <= EPS:
                        path.append(("GB", 0, j))
                        dfs(i, j-1, pst, path)
                        path.pop()
                        if len(results) >= limit:
                            return

    n, m = A_len, B_len
    # D/H/V 三个终止态可能彼此并列，逐一作为起点枚举
    for end_state in ("D", "H", "V"):
        if abs({"D": D[n][m], "H": H[n][m], "V": V[n][m]}[end_state] - total) <= EPS:
            dfs(n, m, end_state, [])
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# 带标志层锚点的整孔对齐
# ---------------------------------------------------------------------------

def correlate(ref_layers, hole_layers, anchors, weights, elev_scale,
              gap_open, gap_extend):
    """对单口待校核孔做关联。

    anchors: [(ref 0 基下标, hole 0 基下标), ...] 已校验为共同标志层、顺序一致。
    返回匹配对、未匹配层、歧义区间、并列解与得分。
    """
    A, B = ref_layers, hole_layers
    n, m = len(A), len(B)

    sim = {}
    sim_detail = {}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s, det = pair_similarity(A[i-1], B[j-1], weights, elev_scale)
            sim[(i, j)] = s
            sim_detail[(i, j)] = det

    # 切段：锚点本身不进段
    bounds = [(-1, -1)] + [(ia, jb) for ia, jb in anchors] + [(n, m)]
    segments = []
    for k in range(len(bounds) - 1):
        i0, j0 = bounds[k]
        i1, j1 = bounds[k+1]
        seg_A = list(range(i0 + 1, i1))    # 段内 A 的全局 0 基下标
        seg_B = list(range(j0 + 1, j1))
        seg_sim = {(pi + 1, pj + 1): sim[(gi + 1, gj + 1)]
                   for pi, gi in enumerate(seg_A)
                   for pj, gj in enumerate(seg_B)}
        res = _align_segment(seg_A, seg_B, seg_sim, gap_open, gap_extend)
        res.update({
            "n": len(seg_A), "m": len(seg_B), "sim": seg_sim,
            "gap_open": gap_open, "gap_extend": gap_extend,
            "a_idx": seg_A, "b_idx": seg_B,
        })
        segments.append(res)

    anchor_sim = {}
    for ia, jb in anchors:
        anchor_sim[(ia, jb)] = sim[(ia + 1, jb + 1)]

    dp_total = sum(r["total"] for r in segments) + sum(anchor_sim.values())
    solution_count = 1
    for r in segments:
        solution_count = min(COUNT_CAP, solution_count * r["count"])

    # 各段枚举（封顶 100 条），后做笛卡尔积
    seg_paths = [_enum_segment_paths(r) for r in segments]
    full_solutions = []

    def combine(seg_k, acc):
        if len(full_solutions) >= MAX_SOLUTIONS:
            return
        if seg_k == len(seg_paths):
            full_solutions.append(list(acc))
            return
        ia, jb = bounds[seg_k + 1]
        for path in seg_paths[seg_k]:
            for kind, pi, pj in path:
                gi = segments[seg_k]["a_idx"][pi-1] if kind in ("M", "GA") else None
                gj = segments[seg_k]["b_idx"][pj-1] if kind in ("M", "GB") else None
                acc.append((kind, gi, gj))
            if (ia, jb) != (n, m):
                acc.append(("X", ia, jb))   # 强制锚点
            combine(seg_k + 1, acc)
            for _ in range(len(path) + (1 if (ia, jb) != (n, m) else 0)):
                acc.pop()
            if len(full_solutions) >= MAX_SOLUTIONS:
                return

    combine(0, [])

    # ---- 汇总最优边（换算为全局下标）----
    partners = {i: set() for i in range(n)}
    gap_pos = {i: set() for i in range(n)}
    for k, r in enumerate(segments):
        for pi, pjs in r["match_partners"].items():
            gi = r["a_idx"][pi-1]
            for pj in pjs:
                partners[gi].add(r["b_idx"][pj-1])
        for pi, pjs in r["gap_positions"].items():
            gi = r["a_idx"][pi-1]
            gap_pos[gi].update(pjs)   # pj 是段内节点位置
    anchor_set_i = {ia for ia, _ in anchors}

    def build_solution(steps):
        pairs, unref, unhole = [], [], []
        gap_count = 0
        for kind, gi, gj in steps:
            if kind in ("M", "X"):
                key = (gi + 1, gj + 1)
                pairs.append({
                    "ref_layer_id": A[gi]["id"],
                    "hole_layer_id": B[gj]["id"],
                    "ref_seq": A[gi]["seq"],
                    "hole_seq": B[gj]["seq"],
                    "ref_lithology": A[gi]["lithology"],
                    "hole_lithology": B[gj]["lithology"],
                    "ref_zm": A[gi]["zmid"],
                    "hole_zm": B[gj]["zmid"],
                    "elev_delta": A[gi]["zmid"] - B[gj]["zmid"],
                    "score": round(sim[key], 4),
                    "detail": {k: round(v, 4) for k, v in
                               sim_detail[key].items()},
                    "marker": kind == "X",
                })
            elif kind == "GA":
                unref.append(_layer_brief(A[gi]))
                gap_count += 1
            elif kind == "GB":
                unhole.append(_layer_brief(B[gj]))
                gap_count += 1
        return pairs, unref, unhole, gap_count

    raw_solutions = [build_solution(steps) for steps in full_solutions]
    # 按“匹配对集合 + 未匹配层集合”去重：段内枚举已只产生规范形路径，
    # 这里再按同一等价关系兜底去重，保证 solutions_listed 与 solution_count
    # 口径一致（间隙插入顺序不同但归属相同的方案只算、只列一次）
    seen, solutions_out = set(), []
    for sol in raw_solutions:
        sig = (
            tuple(sorted((p["ref_layer_id"], p["hole_layer_id"]) for p in sol[0])),
            tuple(sorted(u["layer_id"] for u in sol[1])),
            tuple(sorted(u["layer_id"] for u in sol[2])),
        )
        if sig not in seen:
            seen.add(sig)
            solutions_out.append(sol)
    primary_pairs, primary_unref, primary_unhole, primary_gaps = (
        solutions_out[0] if solutions_out else ([], [], [], 0)
    )

    sum_sim = sum(p["score"] for p in primary_pairs)
    denom = max(n, m, 1)
    total_score = round(100.0 * sum_sim / denom, 2)

    # ---- 歧义区间（仅针对参考孔非锚点层）----
    ambiguities = []
    for gi in range(n):
        if gi in anchor_set_i:
            continue
        pset = partners[gi]
        gset = gap_pos[gi]
        if len(pset) + (1 if gset else 0) <= 1:
            continue
        partner_layers = [
            {
                "layer_id": B[gj]["id"],
                "seq": B[gj]["seq"],
                "lithology": B[gj]["lithology"],
                "zmid": round(B[gj]["zmid"], 3),
            }
            for gj in sorted(pset)
        ]
        zs = [B[gj]["zmid"] for gj in pset]
        if gset:
            zs.append(A[gi]["zmid"])
        ambiguities.append({
            "ref_layer_id": A[gi]["id"],
            "ref_seq": A[gi]["seq"],
            "ref_lithology": A[gi]["lithology"],
            "ref_zmid": round(A[gi]["zmid"], 3),
            "partners": partner_layers,
            "can_be_unmatched": bool(gset),
            "outcome_count": len(pset) + (1 if gset else 0),
            "elev_min": round(min(zs), 3),
            "elev_max": round(max(zs), 3),
            "elev_span": round(max(zs) - min(zs), 3),
        })

    anchor_pairs = [
        {
            "ref_layer_id": A[ia]["id"],
            "hole_layer_id": B[jb]["id"],
            "ref_seq": A[ia]["seq"],
            "hole_seq": B[jb]["seq"],
            "score": round(anchor_sim[(ia, jb)], 4),
        }
        for ia, jb in anchors
    ]

    return {
        "pairs": primary_pairs,
        "unmatched_ref": primary_unref,
        "unmatched_hole": primary_unhole,
        "anchors": anchor_pairs,
        "ambiguities": ambiguities,
        "total_score": total_score,
        "sum_similarity": round(sum_sim, 4),
        "dp_score": round(dp_total, 4),
        "gap_count": primary_gaps,
        "matched_count": len(primary_pairs),
        "solution_count": solution_count,
        "solutions_listed": len(solutions_out),
        "tie_solutions": [
            {
                "pairs": [(p["ref_layer_id"], p["hole_layer_id"]) for p in sol[0]],
                "unmatched_ref": [u["layer_id"] for u in sol[1]],
                "unmatched_hole": [u["layer_id"] for u in sol[2]],
                "score": round(sum(p["score"] for p in sol[0]) / denom * 100, 2),
                "gap_count": sol[3],
            }
            for sol in solutions_out
        ],
    }


def _layer_brief(ly):
    return {
        "layer_id": ly["id"],
        "seq": ly["seq"],
        "lithology": ly["lithology"],
        "grain": ly["grain"],
        "water": ly["water"],
        "confidence": ly["confidence"],
        "top": ly["top"],
        "bottom": ly["bottom"],
        "zmid": round(ly["zmid"], 3),
    }
