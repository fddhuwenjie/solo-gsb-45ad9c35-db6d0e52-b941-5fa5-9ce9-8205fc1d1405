"""岩性 / 粒径 / 含水状态字典与属性相似度。"""
import math

# 可选岩性及其所属岩性族
LITHOLOGIES = {
    "黏土": "细粒碎屑", "粉质黏土": "细粒碎屑", "粉土": "细粒碎屑",
    "细砂": "砂", "中砂": "砂", "粗砂": "砂",
    "砾石": "砾", "卵石": "砾",
    "泥岩": "泥质岩", "页岩": "泥质岩",
    "粉砂岩": "碎屑岩", "砂岩": "碎屑岩", "砾岩": "碎屑岩",
    "石灰岩": "碳酸盐岩", "白云岩": "碳酸盐岩",
    "煤层": "可燃有机岩",
    "花岗岩": "岩浆岩", "玄武岩": "岩浆岩",
    "片麻岩": "变质岩", "大理岩": "变质岩",
    "未知": "未知",
}

# 岩性族之间的亲缘关系（同族 0.5，近缘族 0.3，其余 0）
FAMILY_AFFINITY = {
    frozenset(("细粒碎屑", "砂")): 0.3,
    frozenset(("砂", "砾")): 0.3,
    frozenset(("细粒碎屑", "泥质岩")): 0.3,
    frozenset(("砂", "碎屑岩")): 0.3,
    frozenset(("泥质岩", "碎屑岩")): 0.3,
    frozenset(("碳酸盐岩", "碎屑岩")): 0.2,
    frozenset(("岩浆岩", "变质岩")): 0.3,
}


def lithology_similarity(a: str, b: str) -> float:
    if a == b and a != "未知":
        return 1.0
    if a == "未知" or b == "未知":
        return 0.4
    fa, fb = LITHOLOGIES.get(a, "未知"), LITHOLOGIES.get(b, "未知")
    if fa == fb:
        return 0.5
    return FAMILY_AFFINITY.get(frozenset((fa, fb)), 0.0)


GRAINS = ["未知", "泥", "细粒", "中粒", "粗粒", "巨粒"]
_GRAIN_IDX = {g: i for i, g in enumerate(GRAINS)}


def grain_similarity(a: str, b: str) -> float:
    if a not in _GRAIN_IDX or b not in _GRAIN_IDX:
        return 0.4
    if a == "未知" or b == "未知":
        return 0.4
    return max(0.0, 1.0 - abs(_GRAIN_IDX[a] - _GRAIN_IDX[b]) / (len(GRAINS) - 1))


WATERS = ["未知", "干燥", "稍湿", "湿润", "饱和", "承压"]
_WATER_IDX = {w: i for i, w in enumerate(WATERS)}


def water_similarity(a: str, b: str) -> float:
    if a not in _WATER_IDX or b not in _WATER_IDX:
        return 0.4
    if a == "未知" or b == "未知":
        return 0.4
    # “干燥”与“承压”是截然不同的含水状态，跨 4 档相似度为 0
    return max(0.0, 1.0 - abs(_WATER_IDX[a] - _WATER_IDX[b]) / 4.0)


def elev_similarity(delta: float, scale: float) -> float:
    return math.exp(-abs(delta) / max(scale, 1e-9))


LITHOLOGY_COLORS = {
    "细粒碎屑": "#c9a36f", "砂": "#e8d28a", "砾": "#b8a890",
    "泥质岩": "#8f7a5e", "碎屑岩": "#d9b36c", "碳酸盐岩": "#8fc9c4",
    "可燃有机岩": "#3b3b3b", "岩浆岩": "#c97f72", "变质岩": "#a48fbf",
    "未知": "#e3e3e3",
}


def lithology_color(name: str) -> str:
    return LITHOLOGY_COLORS.get(LITHOLOGIES.get(name, "未知"), "#e3e3e3")
