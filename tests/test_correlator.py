"""并列解计数口径回归测试。

修复背景：五项相似度权重与 gap_open/gap_extend 全为 0、参考孔与待校核孔
各只有一层时，“两层均匹配 / 两层均未匹配”两个归属方案曾被 H/V 间隙操作
顺序（先跳过参考层 vs 先跳过待校核层）重复计数为 3。修复后 solution_count
与 solutions_listed 统一按“匹配对集合 + 两侧未匹配层集合”的等价关系
去重：段内 DP 只走规范形路径（同一间隙段内参考侧间隙在待校核侧间隙之前），
多标志层分段组合按同一签名兜底去重；计数仍封顶 9999、最多列出前 100 条。

运行：python -m unittest discover -s tests -v
"""
import unittest

from app.correlator import correlate, prepare_layers
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


class LowOpenPenaltyMissingAssignmentTest(unittest.TestCase):
    """gap_open < gap_extend 时，归属按其可达最高 DP 分参与比较。

    触发例：五项权重全为 0、两孔各两层、gap_open=0、gap_extend=1。
    “全部未匹配”归属交错跳过两侧层段时每步都重开免费间隙（分值 0），
    而固定先跳完一侧再跳另一侧要付续间隙惩罚（-2）。旧的计分 DP 强制
    后者规范形，导致只返回 5 个归属、漏掉“全部未匹配”。
    """

    EXPECTED_SIX = {
        (((101, 201), (102, 202)), (), ()),
        (((101, 201),), (102,), (202,)),
        (((102, 202),), (101,), (201,)),
        (((101, 202),), (102,), (201,)),
        (((102, 201),), (101,), (202,)),
        ((), (101, 102), (201, 202)),       # 全部未匹配：曾被漏解
    }

    def test_2x2_open0_extend1_returns_six_assignments(self):
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        self.assertEqual(len(res["tie_solutions"]), 6)
        self.assertEqual(signatures(res), self.EXPECTED_SIX)
        # 全部未匹配只计、只列一次
        all_gap = [s for s in res["tie_solutions"]
                   if not s["pairs"]
                   and sorted(s["unmatched_ref"]) == [101, 102]
                   and sorted(s["unmatched_hole"]) == [201, 202]]
        self.assertEqual(len(all_gap), 1)
        # 六个归属最优分值同为 0
        self.assertEqual({s["score"] for s in res["tie_solutions"]}, {0.0})
        self.assertEqual(res["dp_score"], 0.0)
        # 正反向判定：两层各自的歧义区间均含 3 个结局（2 个候选 + 未匹配）
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 3)
            self.assertEqual(len(amb["partners"]), 2)
        # 主解仍为对角双匹配（匹配优先），未匹配列表为空
        self.assertEqual([(p["ref_layer_id"], p["hole_layer_id"])
                          for p in res["pairs"]], [(101, 201), (102, 202)])
        self.assertEqual(res["unmatched_ref"], [])
        self.assertEqual(res["unmatched_hole"], [])

    def test_open0_extend1_single_layer_still_two(self):
        """1x1、open=0/extend=1：匹配 0 分；未匹配开 1 个免费间隙也 0 分 -> 2。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), ()),
            ((), (101,), (201,)),
        })

    def test_3x2_open0_extend1_all_unmatched_optimal(self):
        """3 层对 2 层、open=0/extend=1：全部未匹配交错 4 游程分值 0。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        # 全部匹配不可能（3!=2 单调），但任一含 0 分相似度匹配的归属
        # 剩余层可交错开免费间隙，最优总分仍为 0
        self.assertEqual(res["dp_score"], 0.0)
        sig = signatures(res)
        self.assertIn(((), (101, 102, 103), (201, 202)), sig)
        # 计数与列出一致、无重复签名
        self.assertEqual(res["solution_count"], res["solutions_listed"])
        self.assertEqual(len(sig), res["solutions_listed"])

    def test_anchor_segments_open0_extend1(self):
        """锁定标志层分段：3 层对 3 层、中层锁定 -> 两段 1x1 -> 4 个归属。

        每个 1x1 段“匹配/未匹配”在 open=0/extend=1 下同为 0 分；锚点强制
        出现在每个方案中，全部未匹配（指两个非锚点层均未匹配）恰好一个。
        """
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 4)
        self.assertEqual(res["solutions_listed"], 4)
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202), (103, 203)), (), ()),
            (((101, 201), (102, 202)), (103,), (203,)),
            (((102, 202), (103, 203)), (101,), (201,)),
            (((102, 202),), (101, 103), (201, 203)),
        })
        for sol in res["tie_solutions"]:
            self.assertIn((102, 202), sol["pairs"])
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 2)

    def test_open_ge_extend_unchanged_behavior(self):
        """正惩罚下匹配严格占优；全零惩罚退化时 6 个归属并列。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        # open=2、extend=1：任一未匹配层都付正代价，仅全匹配（0 惩罚）最优
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(
            [(p["ref_layer_id"], p["hole_layer_id"]) for p in res["pairs"]],
            [(101, 201), (102, 202)])
        # open=extend=1：未匹配仍付正代价，仅全匹配最优
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        # 开/续惩罚全为 0：所有归属代价同为 0，6 个并列
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)


if __name__ == "__main__":
    unittest.main()
