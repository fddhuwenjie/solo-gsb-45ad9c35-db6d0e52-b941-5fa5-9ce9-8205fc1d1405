/* 钻孔地层关联校核台 —— 前端逻辑（原生 JS） */
"use strict";

const state = {
  config: null,
  boreholes: [],
  markers: [],
  settings: null,
  result: null,
  ui: { referenceId: null, locked: new Set(), editingHoleId: null, editingMarkerId: null },
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败 (${resp.status})`);
  return data;
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2600);
}

// ---------------------------------------------------------------------------
// 加载
// ---------------------------------------------------------------------------

async function loadState() {
  state.config = await api("GET", "/api/config");
  const s = await api("GET", "/api/state");
  state.boreholes = s.boreholes;
  state.markers = s.markers;
  state.settings = s.settings;
  state.result = s.result;
  if (s.settings.last_run) {
    state.ui.referenceId = s.settings.last_run.reference_id;
    state.ui.locked = new Set(s.settings.last_run.marker_ids);
  } else if (state.boreholes.length) {
    state.ui.referenceId = state.boreholes[0].id;
  }
  renderAll();
}

// ---------------------------------------------------------------------------
// 渲染：左栏钻孔
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderHoleList() {
  const wrap = $("#holeList");
  wrap.innerHTML = "";
  state.boreholes.forEach((h) => {
    const isRef = h.id === state.ui.referenceId;
    const card = document.createElement("div");
    card.className = "hole-card" + (isRef ? " ref" : "");
    const depthMax = h.layers.length ? Math.max(...h.layers.map((l) => l.bottom)) : 0;
    const rows = h.layers.map((l) => {
      const color = lithColor(l.lithology);
      const mks = markerLabelsFor(h.id, l.id);
      return `<div class="row">
        <span><i class="swatch" style="background:${color}"></i>${l.seq}. ${esc(l.lithology)}
          <small>${esc(l.grain)} · ${esc(l.water)} · 置信${l.confidence}</small></span>
        <span><small>${l.top}–${l.bottom}m</small>${mks}</span>
      </div>`;
    }).join("");
    card.innerHTML = `
      <div class="hole-card-head">
        <span class="nm">${esc(h.name)}</span>
        ${isRef ? '<span class="ref-tag">参考孔</span>' : ""}
        <span class="meta">口${h.collar_elev}m · ${h.layers.length}层 · 深${depthMax}m</span>
        <button class="small set-ref" ${isRef ? "disabled" : ""}>设为参考</button>
      </div>
      <div class="hole-mini">
        ${rows || '<div class="row"><small>（暂无层段）</small></div>'}
        <span class="edit-link">✎ 编辑孔信息与层段</span>
      </div>`;
    card.querySelector(".set-ref").onclick = (e) => {
      e.stopPropagation();
      state.ui.referenceId = h.id;
      renderAll();
    };
    card.querySelector(".edit-link").onclick = () => openHoleModal(h.id);
    card.querySelector(".hole-card-head").onclick = () => openHoleModal(h.id);
    wrap.appendChild(card);
  });
}

function lithColor(name) {
  const fam = state.config.lithology_families[name] || "未知";
  const map = {
    "细粒碎屑": "#c9a36f", "砂": "#e8d28a", "砾": "#b8a890",
    "泥质岩": "#8f7a5e", "碎屑岩": "#d9b36c", "碳酸盐岩": "#8fc9c4",
    "可燃有机岩": "#3b3b3b", "岩浆岩": "#c97f72", "变质岩": "#a48fbf",
    "未知": "#e3e3e3",
  };
  return map[fam] || "#e3e3e3";
}

function markerLabelsFor(holeId, layerId) {
  const labels = state.markers
    .filter((m) => String(m.links[holeId]) === String(layerId))
    .map((m) => m.label);
  return labels.length
    ? labels.map((l) => `<span class="tag" style="background:#fbe7d3;color:#a35a14">⚑${esc(l)}</span>`).join("")
    : "";
}

// ---------------------------------------------------------------------------
// 渲染：右栏参考孔 / 标志层 / 权重 / 结果
// ---------------------------------------------------------------------------

function renderControls() {
  const sel = $("#refSelect");
  sel.innerHTML = state.boreholes
    .map((h) => `<option value="${h.id}" ${h.id === state.ui.referenceId ? "selected" : ""}>${esc(h.name)}</option>`)
    .join("");
  sel.onchange = () => { state.ui.referenceId = Number(sel.value); renderAll(); };

  const mk = $("#markerLock");
  if (!state.markers.length) {
    mk.innerHTML = '<p class="hint">尚未定义标志层。标志层用于强制对齐，关联路径禁止跨越。</p>';
  } else {
    mk.innerHTML = state.markers.map((m) => `
      <label class="mk-row">
        <input type="checkbox" data-mid="${m.id}" ${state.ui.locked.has(m.id) ? "checked" : ""}>
        <span><span class="lb">⚑ ${esc(m.label)}</span><br><span class="ds">${esc(m.description)}</span></span>
        <span class="edit-mk linkbtn">编辑</span>
      </label>`).join("");
    mk.querySelectorAll("input").forEach((cb) => {
      cb.onchange = () => {
        const id = Number(cb.dataset.mid);
        cb.checked ? state.ui.locked.add(id) : state.ui.locked.delete(id);
      };
    });
    mk.querySelectorAll(".edit-mk").forEach((b, i) => {
      b.onclick = () => openMarkerModal(state.markers[i].id);
    });
  }

  const w = state.settings.weights;
  const defs = [
    ["lithology", "岩性"], ["grain", "粒径"], ["water", "含水状态"],
    ["elev", "高程接近度"], ["confidence", "置信度"],
  ];
  $("#weightSliders").innerHTML = defs.map(([k, label]) => `
    <div class="slider-row">
      <div class="sl-head"><span>${label}权重</span><span id="wv_${k}">${w[k]}</span></div>
      <input type="range" min="0" max="1" step="0.05" value="${w[k]}" data-w="${k}">
    </div>`).join("");
  $$("#weightSliders input").forEach((r) => {
    r.oninput = () => { $("#wv_" + r.dataset.w).textContent = Number(r.value).toFixed(2); };
  });
  $("#inpElevScale").value = state.settings.elev_scale;
  $("#inpGapOpen").value = state.settings.gap_open;
  $("#inpGapExtend").value = state.settings.gap_extend;
}

function renderResults() {
  const r = state.result;
  const sum = $("#resultSummary");
  const holes = $("#resultHoles");
  if (!r) {
    sum.innerHTML = '<p class="hint">尚未计算。</p>';
    holes.innerHTML = "";
    return;
  }
  const totalAmb = r.holes.reduce((a, h) => a + h.ambiguities.length, 0);
  const totalGaps = r.holes.reduce((a, h) => a + h.gap_count, 0);
  const totalSols = r.holes.reduce((a, h) => a + (h.solution_count > 1 ? 1 : 0), 0);
  sum.innerHTML = `
    <div><span class="score-big">${r.average_score}<small> /100 平均总分</small></span></div>
    <div class="kvgrid">
      <span>参考孔</span><b>${esc(r.reference.hole_name)}</b>
      <span>锁定标志层</span><b>${r.locked_markers.map((m) => "⚑" + esc(m.label)).join(" ") || "无"}</b>
      <span>间隙总数</span><b>${totalGaps}</b>
      <span>歧义层位</span><b>${totalAmb} 处${totalSols ? `（${totalSols} 孔存在并列最优）` : ""}</b>
    </div>
    ${r.warnings.length ? r.warnings.map((w) => `<div class="tag warn">⚠ ${esc(w)}</div>`).join("") : ""}
    <small>${esc(r.generated_at)}</small>`;

  holes.innerHTML = r.holes.map((h, hi) => {
    const ambTags = h.ambiguities.map((a) =>
      `<span class="tag amb">参考层${a.ref_seq}→{${[
        ...a.partners.map((p) => p.seq),
        ...(a.can_be_unmatched ? ["未匹配"] : []),
      ].join("/")}} · 跨${a.elev_span}m</span>`).join("");
    const unref = h.unmatched_ref.map((u) => `<span class="tag unm">参考${u.seq} ${esc(u.lithology)}</span>`).join("");
    const unhole = h.unmatched_hole.map((u) => `<span class="tag unm">本孔${u.seq} ${esc(u.lithology)}</span>`).join("");
    const tie = h.solution_count > 1
      ? `<div class="line"><span class="linkbtn" data-ties="${hi}">查看 ${h.solution_count >= 9999 ? "≥9999" : h.solution_count} 条并列最优解（列出前 ${h.solutions_listed} 条）</span></div>`
      : '<div class="line"><small>✔ 唯一最优解</small></div>';
    return `<div class="result-hole">
      <div class="result-hole-head">${esc(h.hole_name)}<span class="sc">${h.total_score} 分</span></div>
      <div class="result-hole-body">
        <div class="line">匹配 ${h.matched_count} 层 · 间隙 ${h.gap_count} 处
          ${h.anchors.map((a) => `<span class="tag" style="background:#fbe7d3;color:#a35a14">⚑锚 参考${a.ref_seq}↔${a.hole_seq} (${a.score.toFixed(2)})</span>`).join("")}</div>
        ${ambTags ? `<div class="line">${ambTags}</div>` : ""}
        ${unref ? `<div class="line">未匹配参考层：${unref}</div>` : ""}
        ${unhole ? `<div class="line">未匹配本孔层：${unhole}</div>` : ""}
        ${tie}
      </div>
    </div>`;
  }).join("");
  holes.querySelectorAll("[data-ties]").forEach((b) => {
    b.onclick = () => openTiesModal(r.holes[Number(b.dataset.ties)]);
  });
}

// ---------------------------------------------------------------------------
// SVG 剖面
// ---------------------------------------------------------------------------

const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function drawSection() {
  const svg = $("#sectionSvg");
  const ph = $("#sectionPlaceholder");
  svg.innerHTML = "";
  const r = state.result;
  if (!r) { ph.style.display = "flex"; svg.style.display = "none"; renderLegend(); return; }
  ph.style.display = "none";
  svg.style.display = "block";

  // 参与剖面的孔（参考 + 结果孔），按 x 排序
  const cols = [
    { id: r.reference.hole_id, name: r.reference.hole_name, x: r.reference.x,
      elev: r.reference.collar_elev, layers: r.reference.layers_geo, isRef: true },
    ...r.holes.map((h) => ({
      id: h.hole_id, name: h.hole_name, x: h.x, elev: h.collar_elev,
      layers: h.layers_geo, isRef: false, result: h,
    })),
  ].sort((a, b) => a.x - b.x);

  const allLayers = cols.flatMap((c) => c.layers);
  if (!allLayers.length) return;
  let zMax = Math.max(...cols.map((c) => c.elev), ...allLayers.map((l) => l.ztop));
  let zMin = Math.min(...allLayers.map((l) => l.zbottom));
  zMax = Math.ceil(zMax + 2);
  zMin = Math.floor(zMin - 2);

  const ML = 64, MR = 40, MT = 46, MB = 24, COLW = 64;
  const zScale = 11;                       // 像素 / 米
  const gap = 150;                          // 相邻孔最小间距
  const xRange = Math.max(...cols.map((c) => c.x)) - Math.min(...cols.map((c) => c.x));
  const innerW = Math.max(xRange * 7, (cols.length - 1) * gap);
  const W = ML + innerW + MR;
  const H = MT + (zMax - zMin) * zScale + MB;
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const yOf = (z) => MT + (zMax - z) * zScale;
  const xMin = Math.min(...cols.map((c) => c.x));
  const span = Math.max(...cols.map((c) => c.x)) - xMin;
  const usableW = Math.max(innerW - COLW, 1);
  const cxOf = {};
  cols.forEach((c, i) => {
    cxOf[c.id] = span > 0
      ? ML + COLW / 2 + ((c.x - xMin) / span) * usableW
      : ML + innerW / 2 + (i - (cols.length - 1) / 2) * gap;
  });

  // 高程网格
  const gridStep = (zMax - zMin) > 60 ? 10 : 5;
  for (let z = Math.ceil(zMin / gridStep) * gridStep; z <= zMax; z += gridStep) {
    const y = yOf(z);
    el("line", { x1: ML - 8, x2: W - MR + 14, y1: y, y2: y, class: "grid-line" }, svg);
    const t = el("text", { x: ML - 12, y: y + 3.5, "text-anchor": "end", class: "elev-label" }, svg);
    t.textContent = z;
  }

  const showAmb = $("#chkAmb").checked;

  // 歧义带（在连线之前绘制）
  if (showAmb) {
    r.holes.forEach((h) => {
      const x2 = cxOf[h.hole_id];
      const x1 = cxOf[r.reference.hole_id];
      h.ambiguities.forEach((a) => {
        const yT = yOf(a.elev_max), yB = yOf(a.elev_min);
        el("rect", { x: Math.min(x1, x2) - COLW / 2, y: yT,
          width: Math.abs(x2 - x1) + COLW, height: Math.max(yB - yT, 6),
          class: "amb-band" }, svg);
        el("line", { x1: x1, y1: yT, x2: x2, y2: yT, class: "amb-band-edge" }, svg);
        el("line", { x1: x1, y1: yB, x2: x2, y2: yB, class: "amb-band-edge" }, svg);
      });
    });
  }

  // 孔柱
  cols.forEach((c) => {
    const cx = cxOf[c.id];
    const yCollar = yOf(c.elev);
    const yBottom = yOf(Math.min(...c.layers.map((l) => l.zbottom)));
    el("rect", { x: cx - COLW / 2, y: yCollar, width: COLW,
      height: yBottom - yCollar, class: "bore-column" }, svg);
    // 孔口
    el("line", { x1: cx - COLW / 2 - 6, x2: cx + COLW / 2 + 6,
      y1: yCollar, y2: yCollar, class: "collar-line" }, svg);
    const nameT = el("text", { x: cx, y: yCollar - 16, class: "collar-label" }, svg);
    nameT.textContent = c.name + (c.isRef ? "（参考）" : "");
    const elT = el("text", { x: cx, y: yCollar - 4, "text-anchor": "middle",
      class: "elev-label" }, svg);
    elT.textContent = "口" + c.elev + "m";

    c.layers.forEach((l) => {
      const yT = yOf(l.ztop), yB = yOf(l.zbottom);
      const rect = el("rect", {
        x: cx - COLW / 2, y: yT, width: COLW, height: Math.max(yB - yT, 1),
        fill: lithColor(l.lithology), class: "layer-block",
      }, svg);
      const tip = el("title", {}, rect);
      tip.textContent = `${c.name} 第${l.seq}层 ${l.lithology}` +
        `\n粒径:${l.grain} 含水:${l.water} 置信:${l.confidence}` +
        `\n高程:${l.zbottom}~${l.ztop} m`;
      if (yB - yT > 13) {
        const t = el("text", { x: cx, y: (yT + yB) / 2 + 3,
          "text-anchor": "middle", class: "layer-label" }, svg);
        t.textContent = l.seq;
      }
    });
  });

  // 未匹配标记
  r.holes.forEach((h) => {
    const cx = cxOf[h.hole_id];
    h.unmatched_hole.forEach((u) => { drawCross(svg, cx, yOf(u.zmid)); });
  });
  const rcx = cxOf[r.reference.hole_id];
  new Set(r.holes.flatMap((h) => h.unmatched_ref.map((u) => u.layer_id))).forEach((lid) => {
    const l = r.reference.layers_geo.find((x) => x.id === lid);
    if (l) drawCross(svg, rcx, yOf(l.zmid));
  });

  // 歧义层位集合，用于线型判定
  const ambMap = {};   // holeId -> refLayerId -> ambiguity
  r.holes.forEach((h) => h.ambiguities.forEach((a) => {
    (ambMap[h.hole_id] ||= {})[a.ref_layer_id] = a;
  }));
  const layerPos = {};
  cols.forEach((c) => c.layers.forEach((l) => { layerPos[l.id] = { cx: cxOf[c.id], l }; }));

  // 候选替代连线（歧义，浅色虚线）
  if (showAmb) {
    r.holes.forEach((h) => {
      h.ambiguities.forEach((a) => {
        const p1 = layerPos[a.ref_layer_id];
        a.partners.forEach((p) => {
          const chosen = h.pairs.find((pr) => pr.ref_layer_id === a.ref_layer_id
            && pr.hole_layer_id === p.layer_id);
          if (chosen) return;
          const p2 = layerPos[p.layer_id];
          if (!p1 || !p2) return;
          el("line", { x1: p1.cx, y1: yOf(a.ref_zmid), x2: p2.cx, y2: yOf(p.zmid),
            class: "corr-line ambiguous", opacity: 0.4 }, svg);
        });
      });
    });
  }

  // 主匹配连线
  const showScores = $("#chkScores").checked;
  r.holes.forEach((h) => {
    const ambH = ambMap[h.hole_id] || {};
    h.pairs.forEach((p) => {
      const p1 = layerPos[p.ref_layer_id], p2 = layerPos[p.hole_layer_id];
      if (!p1 || !p2) return;
      const ambiguous = !!ambH[p.ref_layer_id];
      el("line", {
        x1: p1.cx, y1: yOf(p.ref_zm), x2: p2.cx, y2: yOf(p.hole_zm),
        class: "corr-line" + (p.marker ? " anchor" : ambiguous ? " ambiguous" : ""),
      }, svg);
      if (showScores && Math.abs(p2.cx - p1.cx) > 90) {
        const t = el("text", {
          x: (p1.cx + p2.cx) / 2, y: (yOf(p.ref_zm) + yOf(p.hole_zm)) / 2 - 2,
          "text-anchor": "middle", class: "corr-label",
        }, svg);
        t.textContent = p.score.toFixed(2);
      }
    });
  });

  renderLegend();
}

function drawCross(svg, cx, cy) {
  const d = 5;
  el("line", { x1: cx - d, y1: cy - d, x2: cx + d, y2: cy + d, class: "unmatched-mark" }, svg);
  el("line", { x1: cx - d, y1: cy + d, x2: cx + d, y2: cy - d, class: "unmatched-mark" }, svg);
}

function renderLegend() {
  $("#legend").innerHTML = `
    <span class="item"><i class="ln" style="border-color:var(--primary)"></i>匹配连线（数字为相似度）</span>
    <span class="item"><i class="ln" style="border-color:var(--accent)"></i>锁定标志层强制对齐</span>
    <span class="item"><i class="ln" style="border-color:var(--amb);border-top-style:dashed"></i>歧义候选 / 歧义带</span>
    <span class="item"><svg width="12" height="12"><line x1="1" y1="1" x2="11" y2="11" class="unmatched-mark"/><line x1="1" y1="11" x2="11" y2="1" class="unmatched-mark"/></svg>未匹配层（间隙）</span>
    <span class="item">柱内数字为层序号；悬停查看属性</span>`;
}

// ---------------------------------------------------------------------------
// 钻孔弹窗
// ---------------------------------------------------------------------------

function openHoleModal(id) {
  state.ui.editingHoleId = id ?? null;
  const h = id ? state.boreholes.find((x) => x.id === id) : null;
  $("#holeModalTitle").textContent = h ? `编辑钻孔 ${h.name}` : "新建钻孔";
  $("#fHoleName").value = h?.name || "";
  $("#fHoleX").value = h?.x ?? (state.boreholes.length
    ? Math.max(...state.boreholes.map((b) => b.x)) + 60 : 0);
  $("#fHoleElev").value = h?.collar_elev ?? "";
  $("#fHoleNote").value = h?.note || "";
  $("#btnDeleteHole").hidden = !h;

  const layers = h ? h.layers.map((l) => ({ ...l })) : [];
  if (!layers.length) layers.push(blankLayer());
  renderLayerRows(layers);
  $("#holeModal").hidden = false;
}

function blankLayer() {
  return { top: 0, bottom: 0, lithology: "未知", grain: "未知",
    water: "未知", confidence: 1.0 };
}

function renderLayerRows(layers) {
  const tb = $("#layerTbody");
  tb.innerHTML = "";
  layers.forEach((l, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input class="num" type="number" step="0.1" data-f="top" value="${l.top}"></td>
      <td><input class="num" type="number" step="0.1" data-f="bottom" value="${l.bottom}"></td>
      <td><select data-f="lithology">${state.config.lithologies
        .map((v) => `<option ${v === l.lithology ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></td>
      <td><select data-f="grain">${state.config.grains
        .map((v) => `<option ${v === l.grain ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></td>
      <td><select data-f="water">${state.config.waters
        .map((v) => `<option ${v === l.water ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></td>
      <td><input class="conf" type="number" min="0" max="1" step="0.05" data-f="confidence" value="${l.confidence}"></td>
      <td><button class="rm" title="删除">✕</button></td>`;
    tr.querySelectorAll("[data-f]").forEach((inp) => {
      inp.onchange = () => {
        const f = inp.dataset.f;
        layers[i][f] = inp.tagName === "SELECT" ? inp.value : Number(inp.value);
      };
    });
    tr.querySelector(".rm").onclick = () => {
      layers.splice(i, 1);
      renderLayerRows(layers);
    };
    tr._layers = layers;
    tb.appendChild(tr);
  });
  $("#btnAddLayer").onclick = () => { layers.push(blankLayer()); renderLayerRows(layers); };
  $("#layerTbody").dataset.layers = "";
  $("#layerTbody")._get = () => layers;
}

function collectLayersFromModal() {
  return $("#layerTbody")._get().map((l) => ({
    top: Number(l.top), bottom: Number(l.bottom),
    lithology: l.lithology, grain: l.grain, water: l.water,
    confidence: Number(l.confidence),
  }));
}

$("#btnSaveHole").onclick = async () => {
  const body = {
    name: $("#fHoleName").value.trim(),
    x: Number($("#fHoleX").value),
    collar_elev: Number($("#fHoleElev").value),
    note: $("#fHoleNote").value.trim(),
    layers: collectLayersFromModal(),
  };
  try {
    const id = state.ui.editingHoleId;
    if (id) await api("PUT", `/api/boreholes/${id}`, body);
    else {
      const created = await api("POST", "/api/boreholes", body);
      if (!state.ui.referenceId) state.ui.referenceId = created.id;
    }
    $("#holeModal").hidden = true;
    await refreshAfterEdit("钻孔已保存");
  } catch (e) { toast(e.message, "err"); }
};

$("#btnDeleteHole").onclick = async () => {
  const id = state.ui.editingHoleId;
  if (!id) return;
  if (!confirm("确认删除该钻孔及其全部层段？相关标志层链接也会移除。")) return;
  await api("DELETE", `/api/boreholes/${id}`);
  $("#holeModal").hidden = true;
  if (state.ui.referenceId === id) state.ui.referenceId = null;
  await refreshAfterEdit("钻孔已删除");
};

// ---------------------------------------------------------------------------
// 标志层弹窗
// ---------------------------------------------------------------------------

function openMarkerModal(id) {
  state.ui.editingMarkerId = id ?? null;
  const m = id ? state.markers.find((x) => x.id === id) : null;
  $("#markerModalTitle").textContent = m ? `编辑标志层 ${m.label}` : "新建标志层";
  $("#fMarkerLabel").value = m?.label || "";
  $("#fMarkerDesc").value = m?.description || "";
  $("#btnDeleteMarker").hidden = !m;

  $("#markerLinkRows").innerHTML = state.boreholes.map((h) => {
    const cur = m?.links[h.id];
    const opts = ['<option value="">— 不指定 —</option>'].concat(
      h.layers.map((l) =>
        `<option value="${l.id}" ${String(cur) === String(l.id) ? "selected" : ""}>`
        + `第${l.seq}层 ${esc(l.lithology)}（埋深 ${l.top}–${l.bottom}m）</option>`)
    ).join("");
    return `<div class="marker-link-row"><span class="hn">${esc(h.name)}</span>
      <select data-hole="${h.id}">${opts}</select></div>`;
  }).join("");
  $("#markerModal").hidden = false;
}

$("#btnSaveMarker").onclick = async () => {
  const links = {};
  $$("#markerLinkRows select").forEach((s) => {
    if (s.value) links[s.dataset.hole] = Number(s.value);
  });
  const body = {
    label: $("#fMarkerLabel").value.trim(),
    description: $("#fMarkerDesc").value.trim(),
    links,
  };
  try {
    const id = state.ui.editingMarkerId;
    if (id) await api("PUT", `/api/markers/${id}`, body);
    else await api("POST", "/api/markers", body);
    $("#markerModal").hidden = true;
    await refreshAfterEdit("标志层已保存");
  } catch (e) { toast(e.message, "err"); }
};

$("#btnDeleteMarker").onclick = async () => {
  const id = state.ui.editingMarkerId;
  if (!id) return;
  if (!confirm("确认删除该标志层？")) return;
  await api("DELETE", `/api/markers/${id}`);
  state.ui.locked.delete(id);
  $("#markerModal").hidden = true;
  await refreshAfterEdit("标志层已删除");
};

// ---------------------------------------------------------------------------
// 并列解弹窗
// ---------------------------------------------------------------------------

function openTiesModal(h) {
  const refName = state.result.reference.hole_name;
  $("#tiesBody").innerHTML =
    `<p class="hint">${esc(h.hole_name)} 相对参考孔 ${esc(refName)} 存在 ${h.solution_count >= 9999 ? "≥9999" : h.solution_count} 条总分相同的最优关联方案（DP 间隙惩罚相同），以下列出前 ${h.solutions_listed} 条。橙色为锁定标志层。</p>`
    + h.tie_solutions.map((s, i) => {
      const refLayer = (id) => state.result.reference.layers_geo.find((l) => l.id === id);
      const holeLayer = (id) => h.layers_geo.find((l) => l.id === id);
      const pairHtml = s.pairs.map(([rid, hid]) => {
        const a = refLayer(rid), b = holeLayer(hid);
        const isAnchor = h.pairs.find((p) => p.marker && p.ref_layer_id === rid && p.hole_layer_id === hid);
        return `<span class="tie-pill ${isAnchor ? "anchor" : ""}">${a?.seq}→${b?.seq}</span>`;
      }).join("");
      return `<div class="tie-item">
        <div class="th"><span>方案 ${i + 1}</span><span>${s.score} 分 · 间隙 ${s.gap_count}</span></div>
        <div class="pairs">${pairHtml}</div>
        ${s.unmatched_ref.length ? `<div class="un">未匹配参考层：${s.unmatched_ref.map((id) => refLayer(id)?.seq).join(", ")}</div>` : ""}
        ${s.unmatched_hole.length ? `<div class="un">未匹配本孔层：${s.unmatched_hole.map((id) => holeLayer(id)?.seq).join(", ")}</div>` : ""}
      </div>`;
    }).join("");
  $("#tiesModal").hidden = false;
}

// ---------------------------------------------------------------------------
// 动作
// ---------------------------------------------------------------------------

async function recalc() {
  if (!state.ui.referenceId) { toast("请先选择参考孔", "err"); return; }
  try {
    state.result = await api("POST", "/api/correlate", {
      reference_id: state.ui.referenceId,
      marker_ids: Array.from(state.ui.locked),
    });
    renderResults();
    drawSection();
    const n = state.result.holes.length;
    toast(`关联完成：${state.result.average_score} 分（${n} 孔）`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function refreshAfterEdit(msg) {
  const s = await api("GET", "/api/state");
  state.boreholes = s.boreholes;
  state.markers = s.markers;
  state.settings = s.settings;
  state.result = s.result;
  renderAll();
  if (msg) toast(msg, "ok");
}

$("#btnRecalc").onclick = recalc;

$("#btnSaveWeights").onclick = async () => {
  const weights = {};
  $$("#weightSliders input").forEach((r) => { weights[r.dataset.w] = Number(r.value); });
  try {
    state.settings = await api("PUT", "/api/settings", {
      weights,
      elev_scale: Number($("#inpElevScale").value),
      gap_open: Number($("#inpGapOpen").value),
      gap_extend: Number($("#inpGapExtend").value),
    });
    await recalc();
  } catch (e) { toast(e.message, "err"); }
};

$("#btnDefaultWeights").onclick = async () => {
  const d = state.config.defaults;
  state.settings = await api("PUT", "/api/settings", d);
  renderControls();
  if (state.result) await recalc();
  toast("已恢复默认参数", "ok");
};

$("#btnAddHole").onclick = () => openHoleModal(null);
$("#btnAddMarker").onclick = () => openMarkerModal(null);

$("#btnReset").onclick = async () => {
  if (!confirm("将清空当前全部钻孔、层段、标志层并恢复内置演示数据，确定继续？")) return;
  await api("POST", "/api/demo/reset");
  await loadState();
  toast("已恢复演示数据", "ok");
};

$("#chkAmb").onchange = drawSection;
$("#chkScores").onchange = drawSection;

$$("[data-close]").forEach((b) => {
  b.onclick = () => { $("#" + b.dataset.close).hidden = true; };
});
$$(".modal-mask").forEach((m) => {
  m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$(".modal-mask").forEach((m) => (m.hidden = true));
});

function renderAll() {
  renderHoleList();
  renderControls();
  renderResults();
  drawSection();
}

loadState().catch((e) => toast("初始化失败：" + e.message, "err"));
