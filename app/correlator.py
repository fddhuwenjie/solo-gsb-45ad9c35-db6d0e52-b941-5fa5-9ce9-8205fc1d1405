"""地层关联核心算法。

流程：
1. 深度统一为高程（孔口高程 - 深度）。
2. 以锁定标志层为“强制匹配锚点”，把参考孔与待校核孔的层序切成若干段；
   匹配路径禁止跨越锚点，从而保证标志层不被穿越。
3. 每段用带仿射间隙惩罚（open/extend）的动态规划做层序对齐，
   相似度由岩性、粒径、含水状态、高程差、置信度加权得到。
4. 正向表 + 反向表联合判定：哪些匹配边/间隙块出现在任意一条最优路径上，
   由此给出并列解的歧义区间；并枚举最优归属（上限 100 条）。
并列解的等价口径：一个“方案”（归属）由匹配对集合唯一确定（两侧未匹配层
集合随之确定）。同一集合可对应多条逐边 DP 路径——间隙区内“跳过参考层
（GA）”与“跳过待校核层（GB）”可以交错执行，交错顺序不影响归属。

DP 直接在“匹配节点”（等价类）上进行：F[i][j] 表示以匹配 (i,j) 收尾的
最优归属分值，两次相邻匹配（或起点/终点）之间是一个**间隙块**，块内有
a 个未匹配参考层、b 个未匹配待校核层。块的最低仿射代价 _gap_cost(a,b)
对任意允许的非负 (gap_open, gap_extend) 组合都取该归属能达到的最高分：

- 仅一侧有未匹配层：连续一段，代价 open + (层数-1)*extend；
- 两侧都有：块由若干交替的 GA 段 / GB 段组成（两侧段数差不超过 1），
  open >= extend 时段数越少越好（各归为一段，共 2 段，等价于旧规范形
  “GA 全在 GB 前”）；open < extend 时段数越多越好（两侧逐层交错，
  各层只付开间隙代价），例如 a=b=2、open=0、extend=1 时交错方案代价 0，
  而归组要付 2 次续间隙——旧规范形 DP 曾因此漏掉“全部未匹配”最优解。

因此每个归属恰对应一条匹配节点路径，计数 DP、路径枚举、歧义判定与方案
列表使用同一等价关系，同一归属只计一次、只列一次；对非负惩罚的任意组合
（含 gap_open=0、gap_extend=1 与全零退化情形）均不漏最优。
"""

from .domain import (
    lithology_similarity, grain_similarity, water_similarity, elev_similarity,
)

EPS = 1e-7
MAX_SOLUTIONS = 100
COUNT_CAP = 9999          # 并列解计数封顶，避免大整数


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

def _runs_max(a, b):
    """a 个 GA 与 b 个 GB 两侧交错时最多能切成多少段（同段只含同侧间隙）。"""
    if a <= 0 or b <= 0:
        return 1
    return min(a, b + 1) + min(b, a + 1)


def _gap_cost(a, b, gap_open, gap_extend):
    """相邻两次匹配（或起点/终点）之间的间隙块的最低仿射代价（非负）。

    块内 a 个参考层、b 个待校核层未匹配。单侧为连续一段；两侧交错成
    R 段（GA 段数与 GB 段数差不超过 1），代价
    (a+b)*extend + R*(open-extend)，按 open 与 extend 的大小取 R 的
    最小（=2）或最大值。
    """
    if a == 0 and b == 0:
        return 0.0
    if a == 0 or b == 0:
        return gap_open + (a + b - 1) * gap_extend
    if gap_open >= gap_extend:
        runs = 2
    else:
        runs = _runs_max(a, b)
    return (a + b) * gap_extend + runs * (gap_open - gap_extend)


def _block_key(a, b):
    """间隙块的主解偏好序（值小优先），沿用逐边 DP 的“匹配 > A间隙 >
    B间隙”回溯偏好：连续匹配 > 纯 A 间隙 > 纯 B 间隙 > 两侧交错。
    同类块跨度大者优先（对应旧 H/V 回溯时续间隙尽量延伸），交错块先
    尽量多排 B 侧（旧回溯先走 V）、再尽量多排 A 侧。"""
    if a == 0 and b == 0:
        return (0, 0, 0)
    if b == 0:
        return (1, -a, 0)
    if a == 0:
        return (2, -b, 0)
    return (3, -b, -a)


def _align_segment(A, B, sim, gap_open, gap_extend):
    """返回段对齐结果（在“匹配节点”等价类上做 DP）。

    F[i][j]（i,j>=1）：以匹配 (i,j) 收尾的归属最高分值；F[0][0]=0 为
    虚拟起点。两条相邻匹配节点 (i0,j0)->(i,j) 之间是一个间隙块，跳过
    i-i0-1 个 A 层、j-j0-1 个 B 层，代价为 _gap_cost。终点 (n,m) 之后
    同样允许一个尾部间隙块（不匹配终点）。

    每个“匹配对集合”恰对应一条匹配节点路径，因此 cF 的并列数即为按
    等价类去重后的方案数，不再依赖逐边间隙操作的先后顺序。
    """
    n, m = len(A), len(B)
    F = [[0.0] * (m + 1) for _ in range(n + 1)]
    cF = [[0] * (m + 1) for _ in range(n + 1)]
    # 每个匹配节点/终点的最优前驱：[(i0, j0, a, b), ...]，按主解偏好排序
    preds = [[[] for _ in range(m + 1)] for _ in range(n + 1)]
    cF[0][0] = 1
    # 间隙块代价表（每段只算一次）：g[a][b] = _gap_cost(a, b, ...)
    g = [[_gap_cost(a, b, gap_open, gap_extend) for b in range(m + 1)]
         for a in range(n + 1)]

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            # 收集所有前驱（首个匹配的前驱是虚拟起点 (0,0)）
            cand = [(0, 0, i - 1, j - 1,
                     F[0][0] + sim[(i, j)] - g[i - 1][j - 1])]
            for i0 in range(1, i):
                ga = g[i - i0 - 1]
                fi = F[i0]
                for j0 in range(1, j):
                    if cF[i0][j0] == 0:
                        continue
                    b = j - j0 - 1
                    cand.append((i0, j0, i - i0 - 1, b,
                                 fi[j0] + sim[(i, j)] - ga[b]))
            best = max(c[4] for c in cand)
            chosen = [(c[0], c[1], c[2], c[3]) for c in cand
                      if abs(c[4] - best) <= EPS]
            cnt = 0
            for i0, j0, _, _ in chosen:
                cnt = min(COUNT_CAP, cnt + cF[i0][j0])
            chosen.sort(key=lambda t: _block_key(t[2], t[3]))
            F[i][j], cF[i][j], preds[i][j] = best, cnt, chosen

    # 虚拟终点：尾部间隙块（最后一个匹配 (i0,j0) -> (n,m)），或整段全未匹配
    end_cand = []
    for i0 in range(0, n + 1):
        gn = g[n - i0]
        fi = F[i0]
        for j0 in range(0, m + 1):
            if cF[i0][j0] == 0:
                continue
            end_cand.append((i0, j0, n - i0, m - j0,
                             fi[j0] - gn[m - j0]))
    total = max(c[4] for c in end_cand)
    end_preds = [(c[0], c[1], c[2], c[3]) for c in end_cand
                 if abs(c[4] - total) <= EPS]
    total_count = 0
    for i0, j0, _, _ in end_preds:
        total_count = min(COUNT_CAP, total_count + cF[i0][j0])
    end_preds.sort(key=lambda t: _block_key(t[2], t[3]))

    # 反向表：B[i][j] = 以匹配 (i,j) 收尾后走到终点的最优 suffix
    # （不含 (i,j) 自身的相似度）；B[0][0] = total
    B = [[0.0] * (m + 1) for _ in range(n + 1)]
    B[0][0] = total
    for i in range(n, 0, -1):
        for j in range(m, 0, -1):
            best = -g[n - i][m - j]
            for i2 in range(i + 1, n + 1):
                ga = g[i2 - i - 1]
                bi = B[i2]
                for j2 in range(j + 1, m + 1):
                    val = -ga[j2 - j - 1] + sim[(i2, j2)] + bi[j2]
                    if val > best:
                        best = val
            B[i][j] = best

    # ---- 判定每条匹配边 (i,j) 是否在某条最优归属上 ----
    match_partners = {i: set() for i in range(1, n + 1)}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if abs(F[i][j] + B[i][j] - total) <= 1e-5:
                match_partners[i].add(j)

    # ---- 哪些参考层能在某条最优归属中处于未匹配间隙块 ----
    skip_a = {i: False for i in range(1, n + 1)}

    # 相邻匹配节点之间的块：块在最优归属上当且仅当
    # F[i0][j0] - cost + sim[(i,j)] + B[i][j] == total
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sbij = sim[(i, j)] + B[i][j]
            for i0, j0, a, b in preds[i][j]:
                if abs(F[i0][j0] - g[a][b] + sbij - total) <= 1e-5:
                    for x in range(i0 + 1, i):
                        skip_a[x] = True
    # 尾部块：最后一个匹配 (i0,j0) -> 虚拟终点
    for i0, j0, a, b in end_preds:
        if abs(F[i0][j0] - g[a][b] - total) <= 1e-5:
            for x in range(i0 + 1, n + 1):
                skip_a[x] = True

    # 主解不在这里单独回溯：枚举时 end_preds/preds 已按 _block_key 偏好
    # 排序，DFS 产出的第一条链即主解（correlate 取去重后首条）。

    return {
        "total": total,
        "count": total_count,
        "match_partners": match_partners,
        "skip_a": skip_a,
        "preds": preds,
        "end_preds": end_preds,
    }


def _expand_chain_steps(chain, n, m):
    """把匹配节点链展开为逐边步骤（M/GA/GB，1 基局部坐标）。

    间隙块内按规范顺序先列 GA 再列 GB——仅用于输出确定性，不参与计分；
    归属等价类与块内顺序无关。
    """
    steps = []
    prev_i, prev_j = 0, 0
    for i, j in chain:
        if (i, j) == (0, 0):
            continue
        for x in range(prev_i + 1, i):
            steps.append(("GA", x, 0))
        for y in range(prev_j + 1, j):
            steps.append(("GB", 0, y))
        steps.append(("M", i, j))
        prev_i, prev_j = i, j
    for x in range(prev_i + 1, n + 1):
        steps.append(("GA", x, 0))
    for y in range(prev_j + 1, m + 1):
        steps.append(("GB", 0, y))
    return steps


def _enum_segment_paths(seg, limit=MAX_SOLUTIONS):
    """在最优前驱图上 DFS 枚举段内并列最优归属，返回匹配节点链列表
    （段内局部坐标，1 基，按匹配先后排序）。每个归属恰一条链，天然去重。"""
    n, m = seg["n"], seg["m"]
    results = []

    def dfs(i0, j0, chain):
        if len(results) >= limit:
            return
        if (i0, j0) == (0, 0):
            results.append(list(reversed(chain)))
            return
        for pi, pj, _, _ in seg["preds"][i0][j0]:
            chain.append((pi, pj))
            dfs(pi, pj, chain)
            chain.pop()
            if len(results) >= limit:
                return

    # 终点前驱已按主解偏好排序，第一条枚举到的即主解
    for i0, j0, _, _ in seg["end_preds"]:
        dfs(i0, j0, [(i0, j0)] if (i0, j0) != (0, 0) else [])
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

    # 各段枚举归属链（封顶 100 条），展开为步骤后做笛卡尔积
    seg_paths = [[_expand_chain_steps(chain, r["n"], r["m"]) for chain in
                  _enum_segment_paths(r)] for r in segments]
    full_solutions = []

    def combine(seg_k, acc):
        if len(full_solutions) >= MAX_SOLUTIONS:
            return
        if seg_k == len(seg_paths):
            full_solutions.append(list(acc))
            return
        ia, jb = bounds[seg_k + 1]
        seg = segments[seg_k]
        for path in seg_paths[seg_k]:
            for kind, pi, pj in path:
                gi = seg["a_idx"][pi-1] if kind in ("M", "GA") else None
                gj = seg["b_idx"][pj-1] if kind in ("M", "GB") else None
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
        for pi in range(1, r["n"] + 1):
            if r["skip_a"][pi]:
                gap_pos[r["a_idx"][pi-1]].add(1)   # 占位：该参考层可未匹配
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
    # 按“匹配对集合 + 未匹配层集合”去重：段内 DP/枚举已在等价类上进行
    # （每个归属恰一条匹配节点链），这里再按同一等价关系跨段兜底去重，
    # 保证 solutions_listed 与 solution_count 口径一致。
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
