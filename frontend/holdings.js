// 我的持股：交易紀錄只存在這台裝置的瀏覽器（localStorage），不會傳到任何伺服器。
// 計算方式與後端 apex portfolio 相同：還原約當股數、平均成本法、手續費與證交稅依券商規則。
"use strict";

const KEY = { trades: "apex.trades.v1", cash: "apex.cash.v1", snaps: "apex.snapshots.v1", discount: "apex.discount.v1" };
const COLUMNS = ["date", "code", "side", "shares", "price", "fee", "tax", "note"];
const DD_LEVELS = [[-0.30, "已超過可承受上限 −30%"], [-0.25, "已達回撤目標 −25%"], [-0.20, "接近回撤目標 −25%"], [-0.10, "回撤超過 −10%"]];
const MAX_WEIGHT = 0.30;

const $ = (id) => document.getElementById(id);
const load = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { alert("無法儲存（瀏覽器可能在私密模式）"); } };
const money = (x) => (x === null || Number.isNaN(x) ? "—" : Math.round(x).toLocaleString("zh-TW"));
const sgnMoney = (x) => (x > 0 ? "+" : "") + money(x);
const pct = (x, d = 1) => (x === null || Number.isNaN(x) || !Number.isFinite(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

let VIEW = null; // 候選股票加密檔裡的全部評分（需要密碼）
let RISK = null; // data.json 的出場規則與建議投入比例
let P = null; // prices.json：{date, stocks: {code: [name, sec_type, close, last_date?]}, events: {code: [[date, ratio]]}}

// ── 費用 ────────────────────────────────────────────────────────────────
function fee(value) {
  if (value <= 0) return 0;
  const discount = Number(load(KEY.discount, 1)) || 1;
  return Math.max(1, Math.floor(value * 0.001425 * discount));
}
function tax(value, code) {
  const t = P && P.stocks[code] ? P.stocks[code][1] : "stock";
  let rate = 0.001;
  if (t === "etf") rate = code.endsWith("B") ? 0 : 0.001;
  else if (t === "stock" || t === "dr" || t === "preferred") rate = 0.003;
  return Math.floor(value * rate);
}

// ── 計算 ────────────────────────────────────────────────────────────────
// 還原因子：交易日之後所有除權息、分割事件比例的乘積（最新一天 = 1）
function factor(code, date) {
  let f = 1;
  for (const [d, r] of (P.events[code] || [])) if (d > date) f *= r;
  return f;
}

function sortedTrades(trades) {
  // 同一天先買後賣（與後端相同）
  return trades.map((t, i) => ({ ...t, _i: i })).sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.side < b.side ? -1 : a.side > b.side ? 1 : a._i - b._i));
}

function compute(trades) {
  const units = {}, cost = {}, opened = {};
  let realized = 0;
  const errors = [];
  for (const t of sortedTrades(trades)) {
    const c = t.code;
    if (!P.stocks[c]) { errors.push(`找不到 ${c} 的行情（代號錯誤或已下市超過 60 天）`); continue; }
    const u = t.shares / factor(c, t.date);
    units[c] = units[c] || 0; cost[c] = cost[c] || 0;
    if (t.side === "buy") {
      if (units[c] <= 1e-9) opened[c] = t.date; // 這次持有從哪一天開始（全部賣掉後再買算重新開始）
      units[c] += u;
      cost[c] += t.shares * t.price + t.fee;
    } else {
      if (u > units[c] * (1 + 1e-6)) { errors.push(`${t.date} 賣出 ${c} ${t.shares} 股，超過當時持有的股數`); continue; }
      const received = t.shares * t.price - t.fee - t.tax;
      const frac = units[c] ? Math.min(1, u / units[c]) : 1;
      const removed = cost[c] * frac;
      realized += received - removed;
      cost[c] -= removed;
      units[c] = Math.max(0, units[c] - u);
    }
  }
  const holdings = [];
  for (const c of Object.keys(units)) {
    if (units[c] <= 1e-9) continue;
    const [name, type, close, lastDate] = P.stocks[c];
    holdings.push({ code: c, name, type, shares: units[c], close, value: units[c] * close, cost: cost[c], lastDate, opened: opened[c] });
  }
  const value = holdings.reduce((s, h) => s + h.value, 0);
  holdings.forEach((h) => { h.weight = value ? h.value / value : 0; });
  holdings.sort((a, b) => b.value - a.value);
  return { holdings, value, cost: holdings.reduce((s, h) => s + h.cost, 0), realized, errors, units };
}

// 回撤：每次有新的收盤日就記一筆快照，用上一筆快照的持股計算這段期間的報酬，串成指數
function updateSnapshots(r, cash) {
  let snaps = load(KEY.snaps, []);
  const last = snaps[snaps.length - 1];
  const close = Object.fromEntries(r.holdings.map((h) => [h.code, h.close]));
  const units = Object.fromEntries(r.holdings.map((h) => [h.code, h.shares]));
  if (!r.holdings.length && !cash) return snaps;
  if (!last) {
    snaps = [{ date: P.date, close, units, cash, index: 1 }];
  } else if (P.date > last.date) {
    let before = last.cash || 0, after = last.cash || 0;
    for (const [c, u] of Object.entries(last.units)) {
      if (!P.stocks[c] || !last.close[c]) continue;
      let g = 1; // 上次快照之後發生的事件
      for (const [d, ratio] of (P.events[c] || [])) if (d > last.date) g *= ratio;
      before += u * last.close[c];
      after += (u / g) * P.stocks[c][2];
    }
    const index = before > 0 ? last.index * (after / before) : last.index;
    snaps.push({ date: P.date, close, units, cash, index });
  } else {
    // 同一天：更新持股組成（新增交易後），指數不變
    snaps[snaps.length - 1] = { ...last, close, units, cash };
  }
  save(KEY.snaps, snaps);
  return snaps;
}

// ── 畫面 ────────────────────────────────────────────────────────────────
function td(row, text, className = "") {
  const c = document.createElement("td");
  c.textContent = text;
  if (className) c.className = className;
  row.appendChild(c);
  return c;
}

function render() {
  const trades = load(KEY.trades, []);
  const cash = Number(load(KEY.cash, 0)) || 0;
  const r = compute(trades);
  const snaps = updateSnapshots(r, cash);
  const total = r.value + cash;
  const unreal = r.value - r.cost;

  $("k-total").textContent = money(total);
  $("k-value").textContent = money(r.value);
  $("k-unreal").textContent = `${sgnMoney(unreal)}（${pct(r.cost ? unreal / r.cost : NaN)}）`;
  $("k-unreal").className = cls(unreal);
  $("k-real").textContent = sgnMoney(r.realized);
  $("k-real").className = cls(r.realized);

  let dd = 0, mdd = 0;
  if (snaps.length) {
    let peak = 0;
    for (const s of snaps) { peak = Math.max(peak, s.index); mdd = Math.min(mdd, s.index / peak - 1); }
    dd = snaps[snaps.length - 1].index / peak - 1;
  }
  $("k-dd").textContent = pct(dd);
  $("k-dd").className = cls(dd);
  // 回撤條：越往右跌越多，紅線是目標 −25%，最右邊是上限 −30%
  $("dd-bar").innerHTML = UI.bar(0, 0.3, { fill: { v: Math.min(-dd, 0.3), cls: "" }, marks: [{ v: 0.25, cls: "line", title: "目標 −25%" }] });
  $("dd-bar").querySelector(".fill").style.background = dd <= -0.2 ? "#d64545" : dd <= -0.1 ? "#e8a317" : "var(--s1)";
  $("dd-note").textContent = `紅線＝目標 −25%｜最右＝上限 −30%${snaps.length ? `｜最大 ${pct(mdd)}` : ""}`;

  const ratio = total ? r.value / total : 0;
  $("k-ratio").textContent = total ? `${Math.round(ratio * 100)}%` : "—";
  const cap = RISK ? RISK.exposure : 1;
  $("ratio-bar").innerHTML = UI.bar(0, 1, { fill: { v: ratio }, marks: cap < 1 ? [{ v: cap, cls: "line", title: "建議上限" }] : [] });
  $("ratio-note").textContent = cap < 1 ? `紅線＝市場不好時建議最多 ${Math.round(cap * 100)}%` : "市場狀態正常，沒有比例限制";

  const warns = r.errors.map((e) => ["alert", e]);
  for (const [level, msg] of DD_LEVELS) if (dd <= level) { warns.push(["alert", `帳戶${msg}：目前比最高點低 ${pct(dd)}`]); break; }
  for (const h of r.holdings) {
    if (h.type !== "etf" && h.weight > MAX_WEIGHT) warns.push(["alert", `${h.code} ${h.name} 占持股 ${Math.round(h.weight * 100)}%，單一個股超過 30%`]);
    if (h.lastDate) warns.push(["alert", `${h.code} ${h.name} 最後成交日 ${h.lastDate}，可能停止交易或下市`]);
  }
  if (RISK && RISK.bad_now && total > 0 && ratio > RISK.exposure + 0.05) {
    warns.push(["shield", `市場狀態不好，建議股票最多占 ${Math.round(RISK.exposure * 100)}%，目前 ${Math.round(ratio * 100)}%`]);
  }
  $("warnings").innerHTML = warns.map(([ic, w]) => `<div class="banner warn">${UI.icon(ic)}<span>${UI.esc(w)}</span></div>`).join("");

  renderCards(r.holdings);

  const tb = document.querySelector("#trades tbody");
  tb.innerHTML = "";
  sortedTrades(trades).reverse().forEach((t) => {
    const tr = document.createElement("tr");
    const name = P.stocks[t.code] ? P.stocks[t.code][0] : "";
    td(tr, t.date);
    td(tr, `${t.code} ${name}`);
    td(tr, t.side === "buy" ? "買" : "賣", t.side === "buy" ? "up" : "down");
    td(tr, t.shares.toLocaleString("zh-TW"));
    td(tr, String(t.price));
    td(tr, money(t.fee + t.tax));
    const del = document.createElement("button");
    del.textContent = "刪除";
    del.className = "small danger";
    del.onclick = () => {
      if (!confirm(`刪除 ${t.date} ${t.code} ${t.side === "buy" ? "買進" : "賣出"} ${t.shares} 股？`)) return;
      const list = load(KEY.trades, []);
      list.splice(t._i, 1);
      save(KEY.trades, list);
      render();
    };
    td(tr, "").appendChild(del);
    tb.appendChild(tr);
  });
}

// ── 每檔持股一張卡片：一個結論標籤＋出場線位置條＋重點數字，細節收起來 ─────────
// 停損價＝平均成本 ×（1 − 停損比例）；移動停損價＝這次買進後的最高收盤 ×（1 − 移動停損比例）。
// 價格都是「今天的還原價」：除權息後成本也跟著換算，不會因為除息誤觸停損。收盤跌破就在下一個交易日賣出。
const ADVICE_KIND = { 續抱: "info", 留意: "warn", 考慮減碼: "bad" };
let cardSeq = 0;
async function renderCards(holdings) {
  const seq = ++cardSeq;
  const box = $("cards");
  if (!holdings.length) {
    box.innerHTML = '<div class="card subtle">還沒有持股。在下方「新增交易」輸入買進紀錄。</div>';
    return;
  }
  const info = await Promise.all(holdings.map(async (h) => {
    const avg = h.cost / h.shares;
    let peak = Math.max(avg, h.close), days = null;
    try {
      const { dates, vals } = await loadHistory(h.code);
      const from = h.opened || dates[0];
      dates.forEach((d, i) => { if (d >= from && vals[i] !== null) peak = Math.max(peak, vals[i]); });
      days = dates.filter((d) => d > from).length;
    } catch { /* 沒有走勢資料：只用成本與收盤 */ }
    return { h, avg, peak, days };
  }));
  if (seq !== cardSeq) return; // 畫面已重新整理
  box.innerHTML = "";
  for (const { h, avg, peak, days } of info) box.appendChild(holdingCard(h, avg, peak, days, holdings));
}

function holdingCard(h, avg, peak, days, holdings) {
  const el = document.createElement("section");
  el.className = "card";
  const px = (x) => x.toLocaleString("zh-TW", { maximumFractionDigits: 2 });
  const gain = h.value - h.cost, ret = h.cost ? h.value / h.cost - 1 : NaN;
  const v = VIEW && VIEW.scores ? VIEW.scores[h.code] : null;
  const isEtf = h.type === "etf";

  // 結論：出場規則優先（該賣了／重新評估），其次是模型看法
  let badge = null, line = null;
  if (RISK && !isEtf) {
    const stopPx = avg * (1 - RISK.stop), trailPx = peak * (1 - RISK.trail);
    line = { stopPx, trailPx, at: Math.max(stopPx, trailPx), which: trailPx > stopPx ? "移動停損" : "停損" };
    if (h.close <= line.at) badge = [`該賣了（跌破${line.which}）`, "bad", "alert"];
    else if (days !== null && days >= RISK.review_days) badge = ["重新評估（滿 60 天）", "warn", "clock"];
    else if (h.close <= line.at * 1.05) badge = ["接近出場線", "warn", "alert"];
  }
  if (!badge && v) badge = [v.advice.level, ADVICE_KIND[v.advice.level] || "", v.advice.level === "續抱" ? "check" : "alert"];
  if (!badge) badge = isEtf ? ["長期持有", "info", "shield"] : ["正常", "good", "check"];

  let html = `<div class="hold-head"><b>${UI.esc(h.code)} ${UI.esc(h.name)}</b>${UI.chip(badge[0], badge[1], badge[2])}</div>`;
  if (line) {
    const lo = Math.min(line.at, h.close) * 0.95, hi = Math.max(peak, h.close) * 1.02;
    html += UI.bar(lo, hi, { band: { from: lo, to: line.at, cls: "danger" },
      marks: [{ v: line.at, cls: "line", title: "出場線" }, { v: peak, cls: "peak", title: "買進後最高" }, { v: h.close, dot: true, title: "現價" }] }) +
      `<div class="bar-labels"><span>出場線 <b>${px(line.at)}</b></span><span>現價 <b>${px(h.close)}</b></span><span>最高 ${px(peak)}</span></div>`;
  }
  const facts = [
    `<span>損益 <b class="${cls(gain)}">${sgnMoney(gain)}（${pct(ret)}）</b></span>`,
    `<span>市值 <b>${money(h.value)}</b>｜占 ${Math.round(h.weight * 100)}%</span>`,
  ];
  if (days !== null && !isEtf) facts.push(`<span>${UI.icon("clock")}持有 <b>${days}</b> / ${RISK ? RISK.review_days : 60} 天</span>`);
  if (v) facts.push(`<span>${UI.icon("star")}模型分數 <b>${Math.round(v.pct * 100)}</b></span>`);
  html += `<div class="facts">${facts.join("")}</div>`;
  el.innerHTML = html;

  const more = [`約當 ${h.shares.toLocaleString("zh-TW", { maximumFractionDigits: 0 })} 股，平均成本 ${px(avg)}（今天的還原價，含手續費）。`];
  if (line) more.push(`停損價 ${px(line.stopPx)}（成本 −${Math.round(RISK.stop * 100)}%）；移動停損價 ${px(line.trailPx)}（最高 ${px(peak)} −${Math.round(RISK.trail * 100)}%）。出場線取兩者較高的那個，收盤跌破就在下一個交易日賣出。`);
  if (isEtf) more.push("ETF 不套用個股停損，看上面的帳戶回撤就好；也不在模型評分範圍。");
  if (v) {
    more.push(`模型看法「${v.advice.level}」：${v.advice.why}`);
    more.push(`模型分數 ${Math.round(v.pct * 100)}（${v.bucket}），過去同等級 60 日後上漲機率 ${Math.round(v.up * 100)}%、中間值 ${pct(v.q50)}`);
    const rs = v.reasons || {};
    if (rs.pros && rs.pros.length) more.push(`加分：${rs.pros.join("；")}`);
    if (rs.cons && rs.cons.length) more.push(`扣分：${rs.cons.join("；")}`);
    if (v.advice.level !== "續抱") more.push(swapText(v, holdings));
  } else if (!isEtf && VIEW) {
    more.push("成交量太小或資料不足，不在模型評分範圍。");
  }
  el.appendChild(UI.help("看細節", more));
  chartToggle(h.code, el);
  return el;
}

// ── 模型看法（評分在候選股票的加密檔裡，用候選股票頁記住的密碼解開）──────────
const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
async function decrypt(box, password) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: b64(box.salt), iterations: box.iter },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(box.iv) }, key, b64(box.ct))));
}

async function loadViews() {
  let pw = null;
  try { pw = localStorage.getItem("apex.viewpw.v1"); } catch { /* 私密模式 */ }
  if (!pw) {
    $("view-note").innerHTML = '想看模型對持股的看法：先到 <a href="picks.html">候選股票</a> 頁輸入密碼並勾選「記住密碼」。';
    return;
  }
  try {
    const box = await (await fetch(`picks.json?t=${Date.now()}`)).json();
    VIEW = await decrypt(box, pw);
  } catch {
    $("view-note").textContent = "讀不到模型看法（今天的資料還沒產生，或密碼已變更）。";
    return;
  }
  $("view-note").textContent = "";
  render();
}

// 換股比較：和第一名（且還沒持有的）候選股比，預期中間值的差扣掉換股成本（約 0.8%）
function swapText(v, holdings) {
  const held = new Set(holdings.map((h) => h.code));
  const c = (VIEW.candidates || []).find((x) => !held.has(x.code) && x.h60);
  if (!c) return "沒有可比較的候選股。";
  const cost = VIEW.cost || 0.0078;
  const diff = c.h60.q50 - v.q50 - cost;
  return `換股比較：換成候選第一名 ${c.code} ${c.name}（中間值 ${pct(c.h60.q50)}，這檔 ${pct(v.q50)}），扣掉換股成本約 ${(cost * 100).toFixed(1)}% 後預期差 ${pct(diff)}。` +
    (diff > 0.01 ? "換股的預期比較好，但這只是歷史統計。" : diff > 0 ? "差距很小，換股不一定划算。" : "扣掉成本後換股不划算。");
}

function fillHelp() {
  const box = $("cards-help");
  box.innerHTML = "";
  const pc = (x) => `${Math.round(x * 100)}%`;
  box.appendChild(UI.help("標籤和出場線怎麼看", [
    "黑點是現價，紅線是出場線，灰線是這次買進後的最高價；黑點掉進左邊淡紅色區，就該賣了。",
    RISK ? `出場線＝停損價（平均成本 −${pc(RISK.stop)}）和移動停損價（買進後最高收盤 −${pc(RISK.trail)}）兩者較高的那個。漲上去之後，出場線會跟著往上移，幫你保住一部分獲利。` : "讀不到出場規則（data.json）。",
    RISK ? `持有滿 ${RISK.review_days} 個交易日（約 3 個月，模型預測的期間）會提醒你重新評估：到「候選股票」頁看它還在不在名單上。` : "",
    "沒有觸發出場規則時，標籤顯示模型看法：續抱（分數前半段且在季線上）、留意、考慮減碼（分數後 20% 且跌破季線）。",
    "所有價格都換算成今天的還原價（含除權息），和券商顯示的成本可能不同，但報酬一致。",
    "這些規則來自歷史回測，不保證未來有效；要不要賣由你決定。",
  ].filter(Boolean)));
}

// ── 表單 ────────────────────────────────────────────────────────────────
function resolveCode(input) {
  const v = input.trim();
  if (P.stocks[v]) return v;
  const head = v.split(/\s+/)[0];
  if (P.stocks[head]) return head;
  const hit = Object.entries(P.stocks).find(([, s]) => s[0] === v);
  return hit ? hit[0] : null;
}

function updateHint() {
  const code = resolveCode($("f-code").value);
  const shares = Number($("f-shares").value), price = Number($("f-price").value);
  if (!code || !shares || !price) { $("f-hint").textContent = code ? `${code} ${P.stocks[code][0]}，最新收盤 ${P.stocks[code][2]}` : ""; return; }
  const value = shares * price;
  const f = fee(value), t = $("f-side").value === "sell" ? tax(value, code) : 0;
  $("f-hint").textContent = `${code} ${P.stocks[code][0]}｜成交金額 ${money(value)}，自動計算手續費 ${f}、證交稅 ${t}`;
}

function addTrade(e) {
  e.preventDefault();
  const code = resolveCode($("f-code").value);
  if (!code) { alert("找不到這個代號或名稱"); return; }
  const shares = Number($("f-shares").value), price = Number($("f-price").value);
  const side = $("f-side").value, value = shares * price;
  const t = {
    date: $("f-date").value, code, side, shares, price,
    fee: $("f-fee").value === "" ? fee(value) : Number($("f-fee").value),
    tax: $("f-tax").value === "" ? (side === "sell" ? tax(value, code) : 0) : Number($("f-tax").value),
    note: $("f-note").value.trim(),
  };
  const list = load(KEY.trades, []);
  const test = compute([...list, t]);
  if (test.errors.length > compute(list).errors.length) { alert(test.errors[test.errors.length - 1]); return; }
  list.push(t);
  save(KEY.trades, list);
  for (const id of ["f-code", "f-shares", "f-price", "f-fee", "f-tax", "f-note"]) $(id).value = "";
  $("f-hint").textContent = "已新增";
  render();
}

// ── 匯出／匯入（與 personal/trades.csv 相同格式）──────────────────────────
function csvCell(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function exportCsv() {
  const rows = [COLUMNS.join(",")].concat(sortedTrades(load(KEY.trades, [])).map((t) => COLUMNS.map((c) => csvCell(t[c])).join(",")));
  const blob = new Blob(["﻿" + rows.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}
function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", q = false;
  text = text.replace(/^﻿/, "");
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') q = false; else cell += ch;
    } else if (ch === '"') q = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") { if (ch === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim()));
}
function importCsv(file) {
  const reader = new FileReader();
  reader.onload = () => {
    const rows = parseCsv(String(reader.result));
    const head = rows.shift().map((h) => h.trim().toLowerCase());
    const idx = Object.fromEntries(COLUMNS.map((c) => [c, head.indexOf(c)]));
    if (["date", "code", "side", "shares", "price"].some((c) => idx[c] < 0)) { alert("CSV 缺少必要欄位：date, code, side, shares, price"); return; }
    const list = [];
    for (const r of rows) {
      const get = (c) => (idx[c] >= 0 ? (r[idx[c]] || "").trim() : "");
      const t = { date: get("date"), code: get("code"), side: get("side").toLowerCase(), shares: Number(get("shares")),
        price: Number(get("price")), fee: Number(get("fee") || 0), tax: Number(get("tax") || 0), note: get("note") };
      if (!/^\d{4}-\d{2}-\d{2}$/.test(t.date) || !["buy", "sell"].includes(t.side) || !(t.shares > 0) || !(t.price > 0)) {
        alert(`這一行格式不對：${r.join(",")}`); return;
      }
      list.push(t);
    }
    if (!confirm(`匯入 ${list.length} 筆交易，會取代目前的 ${load(KEY.trades, []).length} 筆紀錄。確定嗎？`)) return;
    save(KEY.trades, list);
    render();
  };
  reader.readAsText(file, "utf-8");
}

// ── 啟動 ────────────────────────────────────────────────────────────────
function init() {
  $("price-date").textContent = `行情資料日 ${P.date}（每個交易日晚上更新）`;
  const now = new Date();
  $("f-date").value = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  $("cash").value = load(KEY.cash, "") || "";
  $("discount").value = load(KEY.discount, 1);
  const dl = $("codes");
  for (const [c, s] of Object.entries(P.stocks)) {
    const o = document.createElement("option");
    o.value = `${c} ${s[0]}`;
    dl.appendChild(o);
  }
  for (const id of ["f-code", "f-shares", "f-price", "f-side"]) $(id).addEventListener("input", updateHint);
  $("trade-form").addEventListener("submit", addTrade);
  $("cash").addEventListener("change", () => { save(KEY.cash, Math.max(0, Number($("cash").value) || 0)); render(); });
  $("discount").addEventListener("change", () => { save(KEY.discount, Math.min(1, Math.max(0.01, Number($("discount").value) || 1))); updateHint(); });
  $("export").addEventListener("click", exportCsv);
  $("import").addEventListener("change", (e) => { if (e.target.files[0]) importCsv(e.target.files[0]); e.target.value = ""; });
  $("reset-history").addEventListener("click", () => {
    if (confirm("重設回撤紀錄？會從今天重新開始累積。交易紀錄不受影響。")) { save(KEY.snaps, []); render(); }
  });
  $("clear").addEventListener("click", () => {
    if (!confirm("清除這個瀏覽器裡的全部交易、現金與回撤紀錄？無法復原，建議先匯出 CSV。")) return;
    for (const k of Object.values(KEY)) localStorage.removeItem(k);
    init2();
  });
  render();
}
function init2() { $("cash").value = ""; $("discount").value = 1; render(); }

fetch(`prices.json?t=${Date.now()}`)
  .then((res) => { if (!res.ok) throw new Error(res.status); return res.json(); })
  .then((data) => { P = data; init(); loadViews(); riskReady.then((r) => { RISK = r; fillHelp(); render(); }); })
  .catch(() => { $("price-date").textContent = "行情讀取失敗，請稍後再試。"; });
