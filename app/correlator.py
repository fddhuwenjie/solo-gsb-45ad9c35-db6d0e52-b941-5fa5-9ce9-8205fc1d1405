"""地层关联核心算法。

流程：
1. 深度统一为高程（孔口高程 - 深度）。
2. 以锁定标志层为“强制匹配锚点”，把参考孔与待校核孔的层序切成若干段；
   匹配路径禁止跨越锚点，从而保证标志层不被穿越。
3. 每段用带仿射间隙惩罚（open/extend）的动态规划做层序对齐，
   相似度由岩性、粒径、含水状态、高程差、置信度加权得到。
4. 正向表 + 反向表联合判定：哪些匹配/间隙边出现在任意一条最优路径上，
   由此给出并列解的歧义区间；并枚举最优归属（上限 100 条）。

并列解的等价口径：一个“归属方案”由匹配对集合与两侧未匹配层集合唯一确定。
同一归属可对应多条 DP 路径——相邻两次匹配（或段首/段尾）之间的“间隙区”
内，跳过参考层（GA）与跳过待校核层（GB）的先后顺序不影响归属，只影响
仿射间隙代价。例如 open=0、extend=1 且两侧各两层时，“全部未匹配”归属
交错跳两侧（GA1,GB1,GA2,GB2，每步都重开免费间隙）分值为 0，而固定先
跳完一侧再跳另一侧要付续间隙惩罚（-2）。

计分因此分两层，均不受限（允许两侧间隙游程自由交错）：

* 正/反 Gotoh 三态表 D/H/V 给出整段最优分值与“某条边是否在某条最优
  路径上”的歧义判定；
* 间隙区代价由独立的两态小 DP（_gap_cost_table：GH/GV 只含 GA/GB 边、
  前后衔接匹配态）精确求出；匹配节点表 FD 以前驱匹配节点 + 间隙区
  (a,b) 递推，CD 按归属计数（一个前驱 + 一个 (a,b) 唯一确定一个归属，
  与区内跳步顺序无关），故既不会按路径顺序重复计数，也不会因强制某种
  排列多付 extend 而漏掉本可最优的归属。枚举时每个归属用 _canonical_region
  在小 DP 上回溯出唯一一条达到最优代价的规范步骤（并列时优先同侧连跳）。

支持接口允许的全部非负 gap_open/gap_extend 组合（含 open<extend）。
"""
from .domain import (
    lithology_similarity, grain_similarity, water_similarity, elev_similarity,
)

NEG = -1e18
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


def _gap_cost_table(gap_open, gap_extend, n, m):
    """预计算全部间隙区的最优（最省）代价与规范回溯表。

    间隙区只含 A 出列（GA，行 +1）与 B 出列（GB，列 +1）两种边，前后均
    衔接匹配态 D。GA 用表 GH、GB 用表 GV：
      GH[x][y]：走到 (x,y) 且最后一步为 GA 的最高分值
      GV[x][y]：走到 (x,y) 且最后一步为 GB 的最高分值
    从匹配态开间隙付 open、同侧续跳付 extend，两侧游程可自由交错。
    返回 (cost, GH, GV)，cost[(a,b)] = max(GH[a][b], GV[a][b])。
    """
    GH = [[NEG] * (m + 1) for _ in range(n + 1)]
    GV = [[NEG] * (m + 1) for _ in range(n + 1)]
    for x in range(n + 1):
        for y in range(m + 1):
            if x == 0 and y == 0:
                continue
            cands_h, cands_v = [], []
            if x >= 1:
                # 末步 GA 到 (x,y)：同侧续跳 GH、异侧切游程 GV、从匹配态开间隙
                cands_h.append(GH[x-1][y] - gap_extend)
                cands_h.append(GV[x-1][y] - gap_open)
                if x == 1 and y == 0:
                    cands_h.append(-gap_open)
                GH[x][y] = max(cands_h)
            if y >= 1:
                cands_v.append(GV[x][y-1] - gap_extend)
                cands_v.append(GH[x][y-1] - gap_open)
                if x == 0 and y == 1:
                    cands_v.append(-gap_open)
                GV[x][y] = max(cands_v)
    cost = {}
    for a in range(n + 1):
        for b in range(m + 1):
            cost[(a, b)] = 0.0 if a == 0 and b == 0 else max(GH[a][b], GV[a][b])
    return cost, GH, GV


def _canonical_region(a, b, p, q, gap_open, gap_extend):
    """间隙区（从匹配节点 (p,q) 起跳 a 个 A、b 个 B）的规范步骤（局部 1 基）。

    在区域局部两态小 DP（尺寸 (a,b)，自匹配态 (0,0) 出发）上自 (a,b) 向
    (0,0) 回溯，给出达到最优代价的一条确定排列：并列时优先延续同侧
    （游程数最少、最易读），同侧不可行再切换另一侧，首个游程从匹配态
    直接开出；最终反转。仅用于主解与方案枚举的展示，不参与计分。步骤
    编号换算为段内绝对坐标（GA: p+1..p+a、GB: q+1..q+b）。
    """
    if a == 0 and b == 0:
        return []
    _c, lH, lV = _gap_cost_table(gap_open, gap_extend, a, b)
    steps = []
    x, y = a, b
    state = "H" if lH[x][y] >= lV[x][y] - EPS else "V"
    while x > 0 or y > 0:
        if state == "H":
            cont = lH[x-1][y] - gap_extend
            switch = lV[x-1][y] - gap_open
            start = -gap_open if (x == 1 and y == 0) else NEG
            best = max(cont, switch, start)
            steps.append(("GA", p + x, 0))
            if abs(cont - best) <= EPS:
                state = "H"
            elif abs(switch - best) <= EPS:
                state = "V"
            else:
                state = "D"
            x -= 1
        else:
            cont = lV[x][y-1] - gap_extend
            switch = lH[x][y-1] - gap_open
            start = -gap_open if (x == 0 and y == 1) else NEG
            best = max(cont, switch, start)
            steps.append(("GB", 0, q + y))
            if abs(cont - best) <= EPS:
                state = "V"
            elif abs(switch - best) <= EPS:
                state = "H"
            else:
                state = "D"
            y -= 1
        if state == "D":
            # 首步已接回区域起点匹配态，必已到 (0,0)
            break
    steps.reverse()
    return steps

# ---------------------------------------------------------------------------
# 单段序列对齐（局部坐标：A = 参考孔层，B = 待校核孔层）
# ---------------------------------------------------------------------------

def _align_segment(A, B, sim, gap_open, gap_extend):
    """返回段对齐结果。

    两张表：
    * 不受限的 Gotoh 三态表 D/H/V（允许 H->V 与 V->H，两侧游程可交错），
      正反向各一张，用于最优分值与“某条边是否在某条最优路径上”的判定；
    * 匹配节点表 FD/CD：FD[i][j] 为“恰含匹配对 (i,j)”前缀的最优分值，
      CD[i][j] 为达到该分值的不同归属数（前驱匹配对 + 间隙区 (a,b) 唯一
      确定一个归属，与区内跳步顺序无关，故无重复计数）。

    状态 D：上一步为匹配；H：上一步为 A 出列（B 方向间隙，参考层未匹配）；
    V：上一步为 B 出列（A 方向间隙，待校核层未匹配）。
    """
    n, m = len(A), len(B)
    D = [[NEG] * (m + 1) for _ in range(n + 1)]
    H = [[NEG] * (m + 1) for _ in range(n + 1)]
    V = [[NEG] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0.0

    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            if i >= 1 and j >= 1:
                D[i][j] = sim[(i, j)] + max(
                    D[i-1][j-1], H[i-1][j-1], V[i-1][j-1])
            if i >= 1:
                H[i][j] = max(
                    H[i-1][j] - gap_extend,
                    D[i-1][j] - gap_open,
                    V[i-1][j] - gap_open,   # 不受限：V -> H 允许交错
                )
            if j >= 1:
                V[i][j] = max(
                    V[i][j-1] - gap_extend,
                    D[i][j-1] - gap_open,
                    H[i][j-1] - gap_open,
                )

    total = max(D[n][m], H[n][m], V[n][m])

    # 反向 Gotoh 表：RX[i][j] = 以状态 X 到达节点 (i,j) 后走到终点的最优 suffix
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
                vals_v.append(-gap_open + RH[i+1][j])   # 不受限：V -> H
            if j < m:
                vals_d.append(-gap_open + RV[i][j+1])
                vals_h.append(-gap_open + RV[i][j+1])
                vals_v.append(-gap_extend + RV[i][j+1])
            RD[i][j] = max(vals_d) if vals_d else NEG
            RH[i][j] = max(vals_h) if vals_h else NEG
            RV[i][j] = max(vals_v) if vals_v else NEG

    # ---- 匹配节点表：按归属计数 ----
    # FD[i][j] = sim(i,j) + max over 前驱匹配节点 (p,q)
    #            of FD[p][q] + gcost(i-p-1, j-q-1)
    # FD[0][0] = 0 为虚拟段首节点；CD 为取得 FD 的不同归属数（封顶 9999）。
    # 注意匹配 (i,j) 与前驱 (p,q) 之间跳过的是 i-p-1 / j-q-1 层（不含两端）。
    FD = [[NEG] * (m + 1) for _ in range(n + 1)]
    CD = [[0] * (m + 1) for _ in range(n + 1)]
    FD[0][0] = 0.0
    CD[0][0] = 1
    gcost, GH, GV = _gap_cost_table(gap_open, gap_extend, n, m)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, cnt = NEG, 0
            s = sim[(i, j)]
            for p in range(i):
                for q in range(j):
                    pv = FD[p][q]
                    if pv <= NEG / 2:
                        continue
                    # (p,q) 匹配后跳过 A 的 p+1..i-1、B 的 q+1..j-1
                    val = pv + gcost[(i - p - 1, j - q - 1)] + s
                    if val > best + EPS:
                        best, cnt = val, CD[p][q]
                    elif abs(val - best) <= EPS:
                        cnt = min(COUNT_CAP, cnt + CD[p][q])
            FD[i][j], CD[i][j] = best, cnt

    # 段尾虚节点 (n,m) 的纯间隙收尾：最后匹配 (p,q) 后跳过 p+1..n、q+1..m
    total_count = 0
    for p in range(n + 1):
        for q in range(m + 1):
            if FD[p][q] <= NEG / 2:
                continue
            val = FD[p][q] + gcost[(n - p, m - q)]
            if abs(val - total) <= 1e-5:
                total_count = min(COUNT_CAP, total_count + CD[p][q])

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

    # ---- 参考层 i 能否在某条最优路径上“被跳过” ----
    # 边 (i-1,j) -> (i,j)（A[i] 出列），前驱允许 D/H/V 三种状态（不受限）。
    # 只需“能/否”结论，沿用 {0} 作哨兵。
    gap_positions = {i: set() for i in range(1, n + 1)}
    for i in range(1, n + 1):
        for j in range(0, m + 1):
            best = max(
                H[i-1][j] - gap_extend,
                D[i-1][j] - gap_open,
                V[i-1][j] - gap_open,
            )
            if best <= NEG / 2:
                continue
            if abs(best + RH[i][j] - total) <= 1e-5:
                gap_positions[i].add(0)

    # ---- 主解回溯（偏好：间隙区尽量小 = 尽量多匹配）----
    primary = _build_primary_steps(
        n, m, sim, FD, gcost, total, gap_open, gap_extend)

    return {
        "total": total,
        "count": total_count,
        "steps": primary,
        "match_edges": match_edges,
        "match_partners": match_partners,
        "gap_positions": gap_positions,
        "D": D, "H": H, "V": V,
        "RD": RD, "RH": RH, "RV": RV,
        "FD": FD, "CD": CD, "gcost": gcost, "GH": GH, "GV": GV,
    }


def _best_region_predecessor(i, j, FD, gcost, target, real_match,
                             sim_score=0.0):
    """节点 (i,j) 经最优间隙区后的最佳前驱匹配节点 (p,q)。

    target 为该节点要求达到的分值。real_match=True 表示 (i,j) 是真实
    匹配节点，前驱必须严格落在左上（p<i、q<j），区间跳过 i-p-1/j-q-1
    层，且进入 (i,j) 还要取得该对相似度 sim_score；False 表示段尾虚
    节点 (n,m)，允许沿边界纯间隙收尾（p/q 可与 i/j 相等），区间跳过
    i-p/j-q 层。并列时偏好间隙区最小（a+b 小 = 尽量多匹配），其次
    p、q 较大，保证主解确定、与“匹配优先”一致。
    """
    cands = []
    for p in range(i + 1):
        for q in range(j + 1):
            if FD[p][q] <= NEG / 2:
                continue
            if real_match and (p >= i or q >= j):
                continue
            a = i - p - 1 if real_match else i - p
            b = j - q - 1 if real_match else j - q
            val = FD[p][q] + gcost[(a, b)] + (sim_score if real_match else 0.0)
            if abs(val - target) <= EPS:
                cands.append((p, q, a + b))
    cands.sort(key=lambda t: (t[2], -t[0], -t[1]))
    return cands[0][0], cands[0][1]


def _build_primary_steps(n, m, sim, FD, gcost, total,
                         gap_open, gap_extend):
    """从段尾反向贪心选出主解，再展开为正向规范步骤。

    每个 block 记 (前驱(p,q), 当前(ci,cj), 当前是否真实匹配节点)。
    段尾 (n,m) 若能以“匹配到该点”取得 total，优先匹配收尾，否则以纯
    间隙收尾（block 不产生 M）。
    """
    blocks = []
    i, j = n, m
    # 空段（相邻锚点间两侧均无层）：无任何步骤，恰有一个空归属
    if n == 0 and m == 0:
        return []
    # 段尾：优先“匹配到 (n,m)”收尾（真实匹配节点，real_match=True），
    # 否则以纯间隙收尾（虚节点，real_match=False）
    match_end = FD[i][j] > NEG / 2 and abs(FD[i][j] - total) <= 1e-5
    if match_end:
        p, q = _best_region_predecessor(
            i, j, FD, gcost, FD[i][j], True, sim[(i, j)])
    else:
        p, q = _best_region_predecessor(
            i, j, FD, gcost, total, False)
    blocks.append((p, q, i, j, match_end))
    i, j = p, q
    # 其余节点必为真实匹配节点
    while i > 0 or j > 0:
        p, q = _best_region_predecessor(
            i, j, FD, gcost, FD[i][j], True, sim[(i, j)])
        blocks.append((p, q, i, j, True))
        i, j = p, q
    steps = []
    for (p, q, ci, cj, is_match) in reversed(blocks):
        if is_match:
            # 匹配节点 (ci,cj) 与前驱 (p,q) 之间跳过 ci-p-1 / cj-q-1 层
            steps.extend(_canonical_region(
                ci - p - 1, cj - q - 1, p, q, gap_open, gap_extend))
            steps.append(("M", ci, cj))
        else:
            # 纯间隙收尾：跳过 ci-p / cj-q 层
            steps.extend(_canonical_region(
                ci - p, cj - q, p, q, gap_open, gap_extend))
    return steps


def _enum_segment_paths(seg, limit=MAX_SOLUTIONS):
    """在最优归属上 DFS 枚举段内方案（步骤为段内局部坐标，1 基）。

    节点 (i,j) 表示“最近一个匹配节点”（(0,0) 为段首）。从节点出发经一个
    间隙区：跳过若干层后到真实匹配节点 (ni,nj)（附 M 边），或在段尾
    (n,m) 以纯间隙收尾。只有使整段分值等于最优 total 的后继才展开；每个
    后继选择唯一确定一个归属，规范步骤由 _canonical_region 给出，故结果
    天然无重复，无需按归属签名再去重。
    """
    n, m = seg["n"], seg["m"]
    total = seg["total"]
    sim = seg["sim"]
    FD = seg["FD"]
    gcost = seg["gcost"]
    go, ge = seg["gap_open"], seg["gap_extend"]
    results = []

    def successors(i, j):
        """最优后继：(ni, nj, region_steps, is_match)。

        两类后继：a,b>=1 的真实匹配节点（附 M 边），与段尾 (n,m) 的纯
        间隙收尾（a,b>=0）。节点在最优完整路径上，当且仅当“到该点的
        分值 + 该点最优 suffix == total”；对真实匹配节点还要求按最优
        前缀到达（FD[ni][nj]），与 suffix 条件联合即充分。
        """
        out = []
        prefix = FD[i][j]
        # 真实匹配后继 (ni,nj)：跳过 A 的 i+1..ni-1（a=ni-i-1 个）与
        # B 的 j+1..nj-1（b=nj-j-1 个），再匹配 (ni,nj)。
        for ni in range(i + 1, n + 1):
            for nj in range(j + 1, m + 1):
                a, b = ni - i - 1, nj - j - 1
                base = prefix + gcost[(a, b)] + sim[(ni, nj)]
                if abs(base - FD[ni][nj]) <= EPS and \
                        abs(FD[ni][nj] + seg["RD"][ni][nj] - total) <= 1e-5:
                    region = _canonical_region(a, b, i, j, go, ge)
                    out.append((ni, nj, region + [("M", ni, nj)], True))
        # 段尾 (n,m) 纯间隙收尾：跳过 n-i / m-j 层，不产生匹配
        a, b = n - i, m - j
        if a > 0 or b > 0:
            if abs(prefix + gcost[(a, b)] - total) <= 1e-5:
                region = _canonical_region(a, b, i, j, go, ge)
                out.append((n, m, region, False))
        # 主解优先：匹配收尾在前；间隙区小（匹配多）在前；顺序确定
        out.sort(key=lambda t: (not t[3], len(t[2]), -(t[0] + t[1]), -t[0]))
        return out

    def dfs(i, j, acc):
        if len(results) >= limit:
            return
        if i == n and j == m:
            results.append(list(acc))
            return
        for ni, nj, region, _is_match in successors(i, j):
            acc.extend(region)
            dfs(ni, nj, acc)
            del acc[-len(region):]
            if len(results) >= limit:
                return

    dfs(0, 0, [])
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
        seg = segments[seg_k]
        for path in seg_paths[seg_k]:
            # 段内步骤为局部 1 基坐标：M 用 (pi,pj)；GA 仅 pi 有效；
            # GB 仅 pj 有效。换算为段内 0 基下标后再映射到全局。
            for kind, pi, pj in path:
                gi = seg["a_idx"][pi - 1] if kind in ("M", "GA") else None
                gj = seg["b_idx"][pj - 1] if kind in ("M", "GB") else None
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
    # 段内枚举对每个归属恰产生一条规范步骤、段间为锚点强制笛卡尔积，因此
    # 一般已无重复；这里再按“匹配对集合 + 两侧未匹配层集合”签名兜底去重，
    # 保证 solutions_listed 与 solution_count 同口径（同归属只算、只列一次）。
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
