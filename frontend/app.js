// 讀取後端每日產生的 data.json，畫出市場狀態頁。只有公開的市場資訊，沒有持股或個股清單。
"use strict";

// 台股慣例：紅漲綠跌
const REGIME_COLOR = { 多頭: "#d64545", 空頭: "#2e9e5b", 盤整: "#8a8f98", 高波動: "#e08a1e", 資料不足: "#8a8f98" };
const REGIME_TEXT = {
  多頭: "指數在長期均線之上且均線上揚。",
  空頭: "指數在長期均線之下且均線下彎，留意持股回撤。",
  盤整: "趨勢不明確。",
  高波動: "近期波動處於歷史高檔，價格可能大幅震盪。",
  資料不足: "資料不足以判斷。",
};

const $ = (id) => document.getElementById(id);
const pct = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

function setValue(id, x, digits = 1, signed = true) {
  const el = $(id);
  el.textContent = signed ? pct(x, digits) : `${Math.round(x * 100)}%`;
  if (signed) el.className = cls(x);
}

function cell(row, text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  row.appendChild(td);
}

function chart(h) {
  const W = 420, H = 200, L = 44, R = 10, T = 10, B = 22;
  const w = W - L - R, ht = H - T - B, n = h.dates.length;
  const series = [h.tr, h.e50].filter((s) => s && s.some((v) => v !== null));
  const vals = series.flat().filter((v) => v !== null);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = (i) => L + (w * i) / Math.max(n - 1, 1);
  const y = (v) => T + ht * (1 - (v - lo) / span);
  const p = [`<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="近一年走勢">`];
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || h.regime[i] !== h.regime[start]) {
      const c = REGIME_COLOR[h.regime[start]] || "#8a8f98";
      p.push(`<rect x="${x(start)}" y="${T}" width="${Math.max(x(i - 1) - x(start), 1)}" height="${ht}" fill="${c}" opacity="0.10"/>`);
      start = i;
    }
  }
  for (let k = 0; k <= 4; k++) {
    const v = lo + (span * k) / 4;
    p.push(`<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`);
    p.push(`<text x="${L - 6}" y="${y(v) + 4}" class="tick" text-anchor="end">${pct(v - 1, 0)}</text>`);
  }
  p.push(`<text x="${x(0)}" y="${H - 4}" class="tick">${h.dates[0]}</text>`);
  p.push(`<text x="${x(n - 1)}" y="${H - 4}" class="tick" text-anchor="end">${h.dates[n - 1]}</text>`);
  [h.tr, h.e50].forEach((s, k) => {
    if (!s) return;
    const pts = s.map((v, i) => (v === null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");
    p.push(`<polyline points="${pts}" class="s${k + 1}"/>`);
  });
  p.push("</svg>");
  $("chart").innerHTML = p.join("");
}

function render(d) {
  $("meta").textContent = `資料日 ${d.data_date}｜更新時間 ${d.generated.replace("T", " ")}（台灣時間）`;
  if (d.stale) {
    $("stale").hidden = false;
    $("stale").textContent = `資料不是最新：最近應有資料的交易日是 ${d.expected_date}，目前只到 ${d.data_date}。交易所可能尚未公布，稍後會再更新。`;
  }
  const r = $("regime");
  r.textContent = d.regime;
  r.style.background = REGIME_COLOR[d.regime] || "#8a8f98";
  $("regime-text").textContent = `${REGIME_TEXT[d.regime] || ""}自 ${d.regime_since} 起。`;
  setValue("ma-gap", d.ma_gap);
  setValue("ma-slope", d.ma_slope, 2);
  setValue("vol-pct", d.vol_pct, 0, false);
  setValue("breadth", d.breadth, 0, false);
  if (d.history) chart(d.history);

  const rb = document.querySelector("#returns tbody");
  for (const [name, v] of Object.entries(d.returns)) {
    const tr = document.createElement("tr");
    cell(tr, name);
    for (const k of ["近1月", "近3月", "今年", "距一年高點"]) cell(tr, pct(v[k]), cls(v[k]));
    rb.appendChild(tr);
  }

  const pb = document.querySelector("#paper tbody");
  const since = d.paper.map((p) => p.since).filter(Boolean).sort()[0];
  $("paper-since").textContent = since ? `自 ${since} 起` : "尚未開始";
  for (const p of d.paper) {
    const tr = document.createElement("tr");
    cell(tr, p.name);
    cell(tr, String(p.periods));
    cell(tr, p.periods ? pct(p.cum) : "—", p.periods ? cls(p.cum) : "");
    cell(tr, p.periods ? pct(p.bench_cum) : "—", p.periods ? cls(p.bench_cum) : "");
    pb.appendChild(tr);
  }
}

fetch(`data.json?t=${Date.now()}`)
  .then((res) => {
    if (!res.ok) throw new Error(res.status);
    return res.json();
  })
  .then(render)
  .catch(() => {
    $("meta").textContent = "資料讀取失敗，請稍後再試。";
  });
