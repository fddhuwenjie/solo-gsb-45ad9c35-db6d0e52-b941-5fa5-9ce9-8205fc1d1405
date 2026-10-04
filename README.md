# 钻孔地层关联校核台

基于 **Python Flask + SQLite + 原生 HTML/CSS/JavaScript** 的单容器 Web 工具，
用于多口钻孔岩性层段的井间自动关联与人工校核。

## 一条命令启动

```bash
docker compose up -d --build
```

浏览器访问 <http://localhost:8000> 即可（内置 4 口演示钻孔 ZK01–ZK04 与 2 个标志层 M1/L1；
ZK04 在 M1–L1 之间含一对高程对称的分叉砂岩层，默认参数下即为“并列最优 + 歧义区间”的天然示例）。

停止：`docker compose down`；数据持久化在宿主机 `./data/correlation.db`。

### 不用 Docker（本地开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python wsgi.py          # http://localhost:8000
```

### 运行测试

核心算法（`app/correlator.py`）不依赖 Flask，可直接运行回归测试
（含“全零参数下单层对单层只计 2 个并列解”的反例与多标志层分段组合用例）：

```bash
python3 -m unittest discover -s tests -v
```

## 功能

- **数据录入**：多口钻孔的孔口高程、水平位置（剖面坐标），以及按深度排列的岩性层段
  （岩性、粒径、含水状态、置信度 0–1），深度重叠/非法值在服务端校验。
- **深度→高程统一**：层段坐标换算为 `孔口高程 − 埋深`，全部对比与剖面绘制均在高程域进行。
- **参考孔与标志层锁定**：任选一口孔作参考；标志层按孔指定对应层，锁定后成为强制对齐锚点，
  DP 匹配路径**禁止跨越锁定标志层**（锚点交叉顺序冲突会直接报错）。
- **带间隙惩罚的动态规划**：
  - 层间相似度 = 岩性 / 粒径 / 含水状态 / 高程接近度（指数衰减）/ 置信度五项加权；
  - 三态 DP（匹配态、参考孔间隙态、待校核孔间隙态）实现**仿射间隙代价**（开间隙 `gap_open`、续间隙 `gap_extend`），
    保持层序单调、允许尖灭/缺失/新增层；
  - 总分按 `匹配相似度之和 / max(两孔层数) × 100` 归一化。
- **输出**：相关层带（匹配对及五项相似度明细）、未匹配层（参考孔侧/待校核孔侧分列）、
  锚点对、总分与多孔平均分。
- **并列解与歧义区间**：正/反 DP 表联合判定每条边是否出现在某条最优路径上，
  - 列出存在并列最优的层位及其全部候选（含“可未匹配”结局）；
  - 给出候选的**高程歧义区间**（最低–最高高程、跨度），剖面图以紫色虚线候选连线和歧义带显示；
  - 并列解按“**匹配对集合 + 两侧未匹配层集合**”的等价关系计数：同一归属方案不会因
    间隙操作先后顺序（先跳过参考层还是先跳过待校核层）被重复计数——段内 DP 只走规范形
    路径（同一间隙段内参考侧间隙在待校核侧间隙之前），多标志层分段组合按同一签名去重，
    `solution_count` 与 `solutions_listed` 口径一致；
  - 并列解计数封顶 9999，并列方案枚举最多列出前 100 条，可在结果区弹窗逐方案查看。
- **剖面图**：SVG 绘制统一高程的钻孔柱状，连线颜色区分普通匹配 / 标志层锚点 / 歧义候选，
  未匹配层标红叉，悬停层块查看属性，可开关歧义带与相似度标注。
- **重算与导出**：随时调整五项权重、高程特征尺度和间隙惩罚后重算；
  「导出 JSON」下载包含钻孔、层段、标志层、当前参数和最近一次完整计算结果的归档文件。

## 岩性/属性相似度规则（可在 `app/domain.py` 调整）

| 属性 | 规则 |
|------|------|
| 岩性 | 完全相同 1.0；同岩性族 0.5；近缘族（如砂↔砾、泥岩↔页岩）0.2–0.3；未知 0.4 |
| 粒径 | 泥/细粒/中粒/粗粒/巨粒按档极差线性折减 |
| 含水状态 | 干燥→承压共 5 档，按档极差线性折减 |
| 高程接近度 | `exp(−|层中高程差| / elev_scale)`，默认特征尺度 5 m |
| 置信度 | 两孔该层置信度均值 |

默认权重：岩性 0.45、粒径 0.15、含水 0.10、高程 0.20、置信度 0.10（自动归一）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/state` | 钻孔/层段/标志层/参数/最近结果 |
| GET | `/api/config` | 岩性、粒径、含水状态字典与默认参数 |
| POST/PUT/DELETE | `/api/boreholes[/:id]` | 钻孔与层段 CRUD（层段随孔整体保存） |
| POST/PUT/DELETE | `/api/markers[/:id]` | 标志层及各孔层链接 CRUD |
| PUT | `/api/settings` | 权重、高程尺度、间隙惩罚 |
| POST | `/api/correlate` | `{reference_id, marker_ids}` 触发关联计算 |
| GET | `/api/export` | 下载 JSON 归档 |
| POST | `/api/demo/reset` | 恢复内置演示数据 |

## 目录结构

```
app/
  db.py           SQLite 建表 / 默认参数
  domain.py       岩性族字典、属性相似度
  correlator.py   三态 DP、标志层分段、正反表歧义判定、并列解枚举
  seed.py         内置演示数据
  routes.py       Flask 路由
  static/         index.html / style.css / app.js（原生前端，SVG 剖面）
tests/            并列解计数口径回归测试（unittest，无需 Flask）
wsgi.py           开发入口 & gunicorn 入口
Dockerfile / docker-compose.yml
```

## 计算结果 JSON 摘要

`POST /api/correlate` 返回结构（单孔部分）：

```json
{
  "hole_name": "ZK02",
  "total_score": 82.04,
  "pairs": [{ "ref_seq": 4, "hole_seq": 5, "score": 0.83,
              "detail": {"lithology": 1.0, "grain": 0.75, "water": 0.5,
                         "elev": 0.9, "confidence": 0.7},
              "marker": false }],
  "anchors":  [{ "ref_seq": 6, "hole_seq": 7, "score": 0.97 }],
  "unmatched_ref": [],
  "unmatched_hole": [{ "seq": 4, "lithology": "粗砂", "zmid": 105.75 }],
  "ambiguities": [{ "ref_seq": 10, "outcome_count": 2,
                    "partners": [{"seq": 10}, {"seq": 11}],
                    "can_be_unmatched": true,
                    "elev_min": 61.2, "elev_max": 64.8, "elev_span": 3.6 }],
  "solution_count": 1,
  "solutions_listed": 1,
  "tie_solutions": [ /* 并列方案：匹配对 id、未匹配层 id、各自总分 */ ]
}
```
