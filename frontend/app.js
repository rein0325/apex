// 讀取後端每日產生的 data.json，畫出市場狀態頁。只有公開的市場資訊，沒有持股或個股清單。
"use strict";

// 台股慣例：紅漲綠跌
const REGIME_COLOR = { 多頭: "#d64545", 空頭: "#2e9e5b", 盤整: "#8a8f98", 高波動: "#e08a1e", 資料不足: "#8a8f98" };

const $ = (id) => document.getElementById(id);
const pct = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

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

// 一句話結論：紅燈風險高、黃燈小心、綠燈趨勢向上。只描述大盤，不建議買賣或減碼（docs/研究報告/出場規則研究.md）
function verdict(d) {
  const bad = d.risk ? d.risk.bad_now : d.regime === "空頭";
  if (d.regime === "資料不足") return { light: "gray", head: "資料不足", text: "指標還算不出來，先不判斷。" };
  if (bad) {
    return { light: "red", head: "風險高", text: `大盤${d.regime}，而且在長期均線之下，大跌的機會比較高。` };
  }
  if (d.regime === "多頭") return { light: "green", head: "趨勢向上", text: "大盤在長期均線之上，而且均線往上走。" };
  if (d.regime === "高波動") return { light: "yellow", head: "小心", text: "大盤還在長期均線之上，但最近漲跌很劇烈。" };
  return { light: "yellow", head: "小心", text: "趨勢不明確，沒有明顯往上或往下。" };
}

function tiles(d) {
  const up = d.ma_gap > 0 && d.ma_slope > 0, down = d.ma_gap < 0 && d.ma_slope < 0;
  const trend = up ? ["up", "c-red", "向上"] : down ? ["down", "c-green", "向下"]
    : d.ma_gap < 0 ? ["down", "c-green", "轉弱"] : d.ma_gap > 0 ? ["up", "c-red", "轉強"] : ["flat", "c-gray", "盤整"];
  const v = d.vol_pct;
  const vol = v > 0.8 ? ["c-yellow", "很大"] : v > 0.5 ? ["c-gray", "偏大"] : ["c-gray", "平穩"];
  const b = d.breadth;
  const br = b >= 0.5 ? "c-red" : b < 0.3 ? "c-green" : "c-gray";
  const tile = (ic, c, label, value, small) =>
    `<div class="tile"><span class="${c}">${UI.icon(ic)}</span>${label}<b>${value}</b>${small}</div>`;
  $("tiles").innerHTML =
    tile(trend[0], trend[1], "長期趨勢", trend[2], `距均線 ${pct(d.ma_gap, 0)}`) +
    tile("wave", vol[0], "漲跌幅度", vol[1], v == null ? "" : `比過去 3 年 ${Math.round(v * 100)}% 的時間大`) +
    tile("bars", br, "轉強的股票", b == null ? "—" : `${Math.round(b * 100)}%`, "股價在季線之上");
}

function returns(d) {
  const keys = ["近1月", "近3月", "今年", "距一年高點"];
  let html = "<span></span>" + keys.map((k) => `<span class="h">${k === "距一年高點" ? "距高點" : k}</span>`).join("");
  for (const [name, v] of Object.entries(d.returns)) {
    const label = { 加權報酬指數: "大盤", "櫃買股票（自算）": "櫃買" }[name] || name;
    html += `<span>${UI.esc(label)}</span>` + keys.map((k) => `<span class="v ${cls(v[k])}">${pct(v[k], 0)}</span>`).join("");
  }
  $("returns").innerHTML = html;
}

function industries(list) {
  const rows = list.length > 10 ? [...list.slice(0, 5), null, ...list.slice(-5)] : list;
  const max = Math.max(...list.map((r) => Math.abs(r.r20 || 0)), 0.01);
  $("industries").innerHTML = rows.map((r) => {
    if (!r) return '<div class="subtle" style="text-align:center">⋯</div>';
    const w = (Math.abs(r.r20 || 0) / max) * 50;
    const pos = r.r20 >= 0 ? `left:50%;width:${w}%;background:var(--up)` : `left:${50 - w}%;width:${w}%;background:var(--down)`;
    return `<div class="ind-row"><span class="n">${UI.esc(r.name)}</span>` +
      `<span class="track"><i class="axis"></i><i style="${pos}"></i></span><span class="v ${cls(r.r20)}">${pct(r.r20, 0)}</span></div>`;
  }).join("");
  $("ind-help").appendChild(UI.help("怎麼算的", [
    "同產業上市櫃股票依成交金額加權的近 1 個月漲跌幅，上面 5 個最強、下面 5 個最弱。",
    "只列 5 檔以上的產業。紅色是漲、綠色是跌。",
  ]));
}

function status(list) {
  const bad = list.filter((s) => !s.ok);
  $("status-line").innerHTML = bad.length
    ? `<span class="c-yellow">${UI.icon("alert")}</span>有 ${bad.length} 項資料還沒更新：${bad.map((s) => UI.esc(s.name)).join("、")}`
    : `<span class="c-green">${UI.icon("check")}</span>資料都已更新（${list.length} 項）`;
  $("status-list").innerHTML = list.map((s) =>
    `<div class="line-item"><span>${s.ok ? UI.icon("check", "c-green") : UI.icon("alert", "c-yellow")} ${UI.esc(s.name)}</span>` +
    `<span class="side subtle">${UI.esc(s.date || "—")}${s.note ? `（${UI.esc(s.note)}）` : ""}</span></div>`).join("");
}

function render(d) {
  $("meta").textContent = `資料日 ${d.data_date}｜更新 ${d.generated.replace("T", " ")}`;
  if (d.stale) {
    $("stale").hidden = false;
    $("stale").innerHTML = `${UI.icon("clock")}<span>資料不是最新：最近應有資料的交易日是 ${d.expected_date}，目前只到 ${d.data_date}。交易所可能尚未公布，稍後會再更新。</span>`;
  }
  const v = verdict(d);
  $("light").innerHTML = UI.light(v.light);
  $("headline").textContent = v.head;
  $("headline").className = `headline c-${v.light}`;
  $("regime-text").textContent = `${v.text}（${d.regime}，自 ${d.regime_since} 起）`;
  tiles(d);
  if (d.otc) {
    const c = { 多頭: "c-red", 空頭: "c-green", 高波動: "c-yellow" }[d.otc.regime] || "c-gray";
    $("otc").hidden = false;
    $("otc").innerHTML = `<span class="${c}">●</span> 櫃買市場：${UI.esc(d.otc.regime)}，距長期均線 ${pct(d.otc.ma_gap, 0)}`;
  }
  $("regime-help").appendChild(UI.help("燈號怎麼判斷", [
    "紅燈「風險高」：空頭，或漲跌劇烈而且大盤在 200 日均線之下。",
    "黃燈「小心」：趨勢不明確，或漲跌劇烈但還在均線之上。",
    "綠燈「趨勢向上」：大盤在 200 日均線（約一年的平均價格）之上，而且均線往上走。",
    "漲跌幅度：近 20 日的波動，和過去 3 年相比排在哪裡。轉強的股票：股價在 60 日均線（季線）之上的比例。",
    "燈號只描述大盤，不是買賣訊號。回測發現紅燈時減碼能少賠、但也會少賺，要不要減碼由你決定。",
  ]));

  if (d.history) chart(d.history);
  returns(d);
  if (d.industries && d.industries.length) industries(d.industries);
  if (d.status) status(d.status);

  const since = d.paper.map((p) => p.since).filter(Boolean).sort()[0];
  $("paper-since").textContent = since ? `自 ${since} 起` : "尚未開始";
  $("paper").innerHTML = d.paper.map((p) =>
    `<div class="line-item"><span class="name">${UI.esc(p.name)}</span>` +
    `<span class="side">${p.periods ? `<b class="${cls(p.cum)}">${pct(p.cum)}</b> <span class="subtle">0050 ${pct(p.bench_cum)}</span>` : '<span class="subtle">尚未開始</span>'}</span></div>`).join("");
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
