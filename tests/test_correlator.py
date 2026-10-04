"""并列解计数口径回归测试。

修复背景 1：五项相似度权重与 gap_open/gap_extend 全为 0、参考孔与待校核孔
各只有一层时，“两层均匹配 / 两层均未匹配”两个归属方案曾被 H/V 间隙操作
顺序（先跳过参考层 vs 先跳过待校核层）重复计数为 3。修复后 solution_count
与 solutions_listed 统一按“匹配对集合 + 两侧未匹配层集合”的等价关系
去重：DP 直接在匹配节点（归属等价类）上进行，多标志层分段组合按同一签名
兜底去重；计数仍封顶 9999、最多列出前 100 条。

修复背景 2（低开间隙惩罚漏解）：PUT /api/settings 允许 gap_open=0、
gap_extend=1（任意非负组合，接口不要求 open >= extend）。五项相似度权重
全 0、两孔各两层时，“全部未匹配”归属可由 GA/GB 两侧交错跳过达到 DP
分值 0（每个间隙只付开间隙代价 0），而旧规范形 DP（强制先跳一侧再跳另
一侧、禁止 V->H）必须承担 2 次续间隙惩罚（-2），导致只返回 5 个归属、
漏掉“全部未匹配”。修复后同一匹配对集合（连同两侧未匹配层集合）即为同
一方案，以该归属**可达到的最高 DP 分值**参与最优比较；正反向判定、主
解、歧义区间、solution_count、solutions_listed 与 tie_solutions 口径一
致，该例得到 6 个不同归属，“全部未匹配”只计、只列一次。

运行：python -m unittest discover -s tests -v
"""
import unittest

from app.correlator import correlate, prepare_layers, _gap_cost
from app.db import DEFAULT_SETTINGS
from app.seed import BOREHOLES, LAYERS, MARKERS

ZERO_WEIGHTS = {"lithology": 0.0, "grain": 0.0, "water": 0.0,
                "elev": 0.0, "confidence": 0.0}


def make_layers(specs, collar=100.0):
    """specs: [(layer_id, lithology), ...]，深度按 2m 规则递增。"""
    raw = []
    for seq, (lid, lith) in enumerate(specs, 1):
        raw.append({
            "id": lid, "seq": seq,
            "top": (seq - 1) * 2.0, "bottom": seq * 2.0,
            "lithology": lith, "grain": "中粒", "water": "稍湿",
            "confidence": 0.9,
        })
    return prepare_layers(raw, collar)


def signatures(res):
    """每个列出方案的等价签名：匹配对集合 + 两侧未匹配层集合。"""
    return {
        (tuple(sorted(s["pairs"])),
         tuple(sorted(s["unmatched_ref"])),
         tuple(sorted(s["unmatched_hole"])))
        for s in res["tie_solutions"]
    }


class TieCountEquivalenceTest(unittest.TestCase):
    """solution_count 与 solutions_listed 使用同一等价关系。"""

    def test_single_layer_all_zero_counts_two_solutions(self):
        """任务反例：各一层、全零参数 -> 2 个方案而非 3。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), ()),
            ((), (101,), (201,)),
        })

    def test_optimal_subgraph_order_dedup(self):
        """2x2 全零：单调匹配集合共 6 个（1 空 + 4 单对 + 1 双对）。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        self.assertEqual(len(signatures(res)), 6)

    def test_multi_segment_combination_dedup(self):
        """多标志层分段组合：3 层对 3 层、中间层锁定 -> 两段 1x1 -> 4 个方案。"""
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 4)
        self.assertEqual(res["solutions_listed"], 4)
        # 锁定锚点强制出现在每个方案中
        for sol in res["tie_solutions"]:
            self.assertIn((102, 202), sol["pairs"])
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202), (103, 203)), (), ()),
            (((101, 201), (102, 202)), (103,), (203,)),
            (((102, 202), (103, 203)), (101,), (201,)),
            (((102, 202),), (101, 103), (201, 203)),
        })
        # 两个非锚点参考层各自给出“可未匹配”的歧义区间
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 2)

    def test_count_cap_and_listing_limit(self):
        """10x10 全零：C(20,10)=184756 个匹配集合 -> 计数封顶 9999、列出 100。"""
        ref = make_layers([(100 + i, "砂岩") for i in range(1, 11)])
        hole = make_layers([(200 + i, "砂岩") for i in range(1, 11)])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 9999)
        self.assertEqual(res["solutions_listed"], 100)
        self.assertEqual(len(res["tie_solutions"]), 100)
        self.assertEqual(len(signatures(res)), 100)


class LowOpenPenaltyTest(unittest.TestCase):
    """gap_open < gap_extend（含 open=0、extend=1）下的最优归属不漏解。"""

    def test_gap_cost_block_formula(self):
        """间隙块最低仿射代价：单侧连续、双侧按 open/extend 大小取交错段数。"""
        # 空块
        self.assertEqual(_gap_cost(0, 0, 0.0, 1.0), 0.0)
        # 单侧：open + (层数-1)*extend，与 open/extend 相对大小无关
        self.assertEqual(_gap_cost(3, 0, 0.0, 1.0), 2.0)
        self.assertEqual(_gap_cost(0, 3, 2.0, 1.0), 4.0)
        # 双侧 open>=extend：各归一段（2 段）
        #   go=2,ge=1,a=b=2 -> 4 层续间隙 + 2 次(2-1) = 6
        self.assertEqual(_gap_cost(2, 2, 2.0, 1.0), 6.0)
        # 双侧 open<extend：尽量交错（a=b=2 时 4 段全为开间隙）= 0
        self.assertEqual(_gap_cost(2, 2, 0.0, 1.0), 0.0)
        # a=b=2, go=1, ge=0：归组 2 段与交错等价 = 2 次 open = 2
        self.assertEqual(_gap_cost(2, 2, 1.0, 0.0), 2.0)
        # a=3,b=1 时最多 3 段：3 层 extend - 2 次(extend-open) = 1
        self.assertEqual(_gap_cost(3, 1, 0.0, 1.0), 1.0)
        self.assertEqual(_gap_cost(1, 3, 0.0, 1.0), 1.0)
        # a=3,b=2 最多 5 段：5*extend - 5*(extend-open) = 0
        self.assertEqual(_gap_cost(3, 2, 0.0, 1.0), 0.0)
        # 全零退化
        self.assertEqual(_gap_cost(4, 4, 0.0, 0.0), 0.0)

    def test_trigger_two_by_two_open0_extend1(self):
        """触发例：五项权重 0、各两层、open=0/extend=1 -> 6 个归属。

        “全部未匹配”经两侧交错跳过可达 DP 分 0，与 1 个双对 + 4 个单对
        方案并列，只计、只列一次；旧规范形 DP 曾漏它只得 5 个。
        """
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["dp_score"], 0.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        sigs = signatures(res)
        expected = {
            (((101, 201), (102, 202)), (), ()),
            (((101, 201),), (102,), (202,)),
            (((102, 202),), (101,), (201,)),
            (((101, 202),), (102,), (201,)),
            (((102, 201),), (101,), (202,)),
            ((), (101, 102), (201, 202)),          # 全部未匹配
        }
        self.assertEqual(sigs, expected)
        # 全部未匹配恰好出现一次
        all_unmatched = [s for s in res["tie_solutions"]
                         if not s["pairs"]]
        self.assertEqual(len(all_unmatched), 1)
        self.assertEqual(all_unmatched[0]["gap_count"], 4)
        # 每个归属的公开总分（匹配相似度和 / max(层数)）都为 0
        self.assertTrue(all(abs(s["score"]) < 1e-9
                            for s in res["tie_solutions"]))
        # 主解必须是 6 个并列最优之一，且全部字段自洽
        primary = tuple(sorted((p["ref_layer_id"], p["hole_layer_id"])
                               for p in res["pairs"]))
        self.assertIn(primary, {sig[0] for sig in sigs})
        # 两个参考层都有完整歧义：2 个匹配候选 + 可未匹配 = 3 种结局
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 3)
            self.assertEqual(len(amb["partners"]), 2)

    def test_all_unmatched_interleave_open0_extend2(self):
        """open=0、extend=2 时交错解仍为 0，全部未匹配同样不漏。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩"), (203, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 2.0)
        self.assertEqual(res["dp_score"], 0.0)
        sigs = signatures(res)
        # 全部未匹配：块(3,3) 最多 6 段全为开间隙 = 0
        self.assertIn(((), (101, 102, 103), (201, 202, 203)), sigs)
        # 3x3 共 C(6,3)=20 个单调匹配集合；其中单对 (1,3) 与 (3,1) 的
        # 头/尾均为单侧两块（0+2 与 2+0），代价 4 非最优，其余 18 个并列 0 分
        self.assertEqual(res["solution_count"], 18)
        self.assertEqual(res["solutions_listed"], 18)
        self.assertNotIn((((101, 203),), (102, 103), (201, 202)), sigs)
        self.assertNotIn((((103, 201),), (101, 102), (202, 203)), sigs)

    def test_anchor_segment_low_open_extend(self):
        """锚点回归（归组/交错块代价在分段下仍正确）：3 层对 3 层、锁定
        中间煤层，open=0/extend=1。

        上下两段均为 1x1：匹配(0 代价) 与 两侧均未匹配（块(1,1) 交错成
        两段，只付开间隙 0）并列，单跳一侧要付 1 次续间隙不并列 -> 每段
        2 个归属，段间乘积 = 4；锚点强制出现，主解偏好连续匹配（全匹配）。
        """
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 4)
        self.assertEqual(res["solutions_listed"], 4)
        self.assertEqual(res["dp_score"], 0.0)
        for sol in res["tie_solutions"]:
            self.assertIn((102, 202), sol["pairs"])
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202), (103, 203)), (), ()),
            (((101, 201), (102, 202)), (103,), (203,)),
            (((102, 202), (103, 203)), (101,), (201,)),
            (((102, 202),), (101, 103), (201, 203)),
        })
        # 主解是无间隙的全匹配（主解偏好连续匹配）
        self.assertEqual([(p["ref_seq"], p["hole_seq"]) for p in res["pairs"]],
                         [(1, 1), (2, 2), (3, 3)])
        # 两个非锚点参考层各有“匹配 + 可未匹配”歧义；锚点层不报
        ref_amb = {a["ref_seq"] for a in res["ambiguities"]}
        self.assertEqual(ref_amb, {1, 3})
        self.assertTrue(all(a["can_be_unmatched"] for a in res["ambiguities"]))

    def test_anchor_segment_interleave_open0_extend1(self):
        """锚点 + 低开惩罚：段内 2x1 经交错让全部未匹配等方案参与比较。"""
        ref = make_layers([(101, "砂岩"), (102, "煤层"),
                           (103, "砂岩"), (104, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        # 锚点 (102,202)：上段 1x1（2 归属）；下段 A 两层(103,104)、B 一层(203)
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        # 下段：匹配 103（尾块单侧(1,0)=0）或匹配 104（头块单侧(1,0)=0）
        # 或全未匹配（块(2,1) 最多 3 段，代价 3-3=0）= 3 归属；上段 2 -> 6
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        sigs = signatures(res)
        # 下段全未匹配 × 上段未匹配/匹配 两种
        self.assertIn((((102, 202),), (101, 103, 104), (201, 203)), sigs)
        self.assertIn((((101, 201), (102, 202)), (103, 104), (203,)), sigs)

    def test_open_one_extend_zero_matches_class_semantics(self):
        """open=1、extend=0：续间隙免费，归组与交错等价，计数仍按归属去重。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1.0, 0.0)
        # 6 个归属，全部未匹配的归组代价 = 2 次 open = 2，其余见各块；
        # 只有全匹配（0 间隙，0 代价）唯一最优
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["pairs"][0]["ref_seq"], 1)
        self.assertEqual(res["dp_score"], 0.0)


class DefaultParamsRegressionTest(unittest.TestCase):
    """默认参数下的既有行为（锚点、歧义区间、总分）保持不变。"""

    def _seed_holes(self):
        holes = {}
        for name, _x, elev, _note in BOREHOLES:
            raw = [{"id": f"{name}-{i}", "seq": i, "top": t, "bottom": b,
                    "lithology": li, "grain": g, "water": w, "confidence": c}
                   for i, (t, b, li, g, w, c) in enumerate(LAYERS[name], 1)]
            holes[name] = prepare_layers(raw, elev)
        return holes

    def test_seed_natural_tie_preserved(self):
        """演示数据 ZK04 分叉砂岩层的天然并列：2 个方案、1 处歧义区间。"""
        holes = self._seed_holes()
        s = DEFAULT_SETTINGS
        anchors = sorted((mp["ZK01"] - 1, mp["ZK04"] - 1)
                         for _lbl, _desc, mp in MARKERS)
        res = correlate(holes["ZK01"], holes["ZK04"], anchors,
                        s["weights"], s["elev_scale"],
                        s["gap_open"], s["gap_extend"])
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(len(res["ambiguities"]), 1)
        # 两个锁定锚点强制匹配（M1: 6<->6，L1: 8<->9）
        anchor_pairs = {(a["ref_seq"], a["hole_seq"]) for a in res["anchors"]}
        self.assertEqual(anchor_pairs, {(6, 6), (8, 9)})
        matched = {(p["ref_seq"], p["hole_seq"]) for p in res["pairs"]}
        self.assertTrue(anchor_pairs <= matched)
        self.assertGreater(res["total_score"], 0)

    def test_unique_solution_when_signal_clear(self):
        """强信号下只有 1 个最优方案，计数与列出一致。"""
        ref = make_layers([(101, "砂岩"), (102, "泥岩")])
        hole = make_layers([(201, "砂岩"), (202, "泥岩")])
        res = correlate(ref, hole, [], DEFAULT_SETTINGS["weights"],
                        5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["solutions_listed"], 1)
        self.assertEqual([(p["ref_layer_id"], p["hole_layer_id"])
                          for p in res["pairs"]], [(101, 201), (102, 202)])
        # 单项相似度：岩性/粒径/含水/高程 = 1.0，置信度 = 0.9 -> 0.99
        self.assertAlmostEqual(res["total_score"], 99.0)


class ApiTriggerTest(unittest.TestCase):
    """经真实 HTTP 接口复现：PUT /api/settings(open=0,extend=1) +
    POST /api/correlate 返回 6 个归属，导出归档口径一致。

    容器（docker compose）内安装了 Flask 时执行；纯算法本地环境自动跳过。
    """

    def setUp(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("未安装 Flask，端到端接口用例在容器内运行")
        import pathlib
        import tempfile
        from app import db as dbmod
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_path = dbmod.DB_PATH
        # 必须在 routes 导入/建库前指向临时库
        dbmod.DB_PATH = pathlib.Path(self._tmp.name)
        from app import routes
        routes.dbmod.init_db()
        conn = routes.dbmod.get_db()
        try:
            routes.seed_if_empty(conn)
        finally:
            conn.close()
        self.client = routes.app.test_client()

    def tearDown(self):
        if not hasattr(self, "client"):
            return
        import os
        from app import db as dbmod
        dbmod.DB_PATH = self._old_path
        os.unlink(self._tmp.name)

    def _two_hole_two_layer(self):
        st = self.client.get("/api/state").get_json()
        for b in st["boreholes"]:
            self.client.delete(f"/api/boreholes/{b['id']}")
        layers = [{"seq": 1, "top": 0.0, "bottom": 2.0, "lithology": "砂岩"},
                  {"seq": 2, "top": 2.0, "bottom": 4.0, "lithology": "砂岩"}]
        rid = self.client.post("/api/boreholes", json={
            "name": "R1", "x": 0, "collar_elev": 100.0, "layers": layers,
        }).get_json()["id"]
        hid = self.client.post("/api/boreholes", json={
            "name": "H1", "x": 10, "collar_elev": 100.0,
            "layers": [dict(l) for l in layers],
        }).get_json()["id"]
        return rid, hid

    def test_settings_accepts_open0_extend1_and_correlate_six(self):
        rid, hid = self._two_hole_two_layer()
        r = self.client.put("/api/settings", json={
            "weights": {"lithology": 0, "grain": 0, "water": 0,
                        "elev": 0, "confidence": 0},
            "gap_open": 0, "gap_extend": 1,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["gap_open"], 0.0)
        self.assertEqual(r.get_json()["gap_extend"], 1.0)

        r = self.client.post("/api/correlate",
                             json={"reference_id": rid, "marker_ids": []})
        self.assertEqual(r.status_code, 200)
        h = r.get_json()["holes"][0]
        self.assertEqual(h["solution_count"], 6)
        self.assertEqual(h["solutions_listed"], 6)
        pairs_sets = {tuple(sorted(s["pairs"])) for s in h["tie_solutions"]}
        # 全部未匹配恰出现一次
        self.assertEqual(
            sum(1 for s in h["tie_solutions"] if not s["pairs"]), 1)
        self.assertIn((), pairs_sets)
        self.assertEqual(h["dp_score"], 0.0)
        # 两个参考层都有 3 结局歧义（2 候选 + 可未匹配）
        self.assertEqual(len(h["ambiguities"]), 2)
        for amb in h["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 3)

        # 重算结果已持久化，导出归档携带同样的 6 个归属
        export = self.client.get("/api/export").get_json()
        last = export["last_result"]["holes"][0]
        self.assertEqual(last["solution_count"], 6)
        self.assertEqual(last["solutions_listed"], 6)
        self.assertEqual(export["settings"]["gap_open"], 0.0)
        self.assertEqual(export["settings"]["gap_extend"], 1.0)


if __name__ == "__main__":
    unittest.main()
