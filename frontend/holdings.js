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
  const units = {}, cost = {};
  let realized = 0;
  const errors = [];
  for (const t of sortedTrades(trades)) {
    const c = t.code;
    if (!P.stocks[c]) { errors.push(`找不到 ${c} 的行情（代號錯誤或已下市超過 60 天）`); continue; }
    const u = t.shares / factor(c, t.date);
    units[c] = units[c] || 0; cost[c] = cost[c] || 0;
    if (t.side === "buy") {
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
    holdings.push({ code: c, name, type, shares: units[c], close, value: units[c] * close, cost: cost[c], lastDate });
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
  $("k-ratio").textContent = total ? `${Math.round((r.value / total) * 100)}%` : "—";

  let dd = 0, mdd = 0;
  if (snaps.length) {
    let peak = 0;
    for (const s of snaps) { peak = Math.max(peak, s.index); mdd = Math.min(mdd, s.index / peak - 1); }
    dd = snaps[snaps.length - 1].index / peak - 1;
    $("k-dd").textContent = pct(dd);
    $("k-dd").className = cls(dd);
    $("k-mdd").textContent = `最大 ${pct(mdd)}｜自 ${snaps[0].date}`;
  }

  const warns = [...r.errors];
  for (const [level, msg] of DD_LEVELS) if (dd <= level) { warns.push(`帳戶${msg}：目前比最高點低 ${pct(dd)}`); break; }
  for (const h of r.holdings) {
    if (h.type !== "etf" && h.weight > MAX_WEIGHT) warns.push(`${h.code} ${h.name} 占持股 ${Math.round(h.weight * 100)}%，單一個股超過 30%`);
    if (h.lastDate) warns.push(`${h.code} ${h.name} 最後成交日 ${h.lastDate}，可能停止交易或下市`);
  }
  $("warnings").innerHTML = "";
  for (const w of warns) {
    const p = document.createElement("p");
    p.className = "warn";
    p.textContent = `! ${w}`;
    $("warnings").appendChild(p);
  }

  const hb = document.querySelector("#holdings tbody");
  hb.innerHTML = "";
  for (const h of r.holdings) {
    const tr = document.createElement("tr");
    td(tr, `${h.code} ${h.name}`);
    td(tr, h.shares.toLocaleString("zh-TW", { maximumFractionDigits: 0 }));
    td(tr, h.close.toLocaleString("zh-TW"));
    td(tr, money(h.value));
    td(tr, sgnMoney(h.value - h.cost), cls(h.value - h.cost));
    td(tr, pct(h.cost ? h.value / h.cost - 1 : NaN), cls(h.value - h.cost));
    td(tr, `${Math.round(h.weight * 100)}%`);
    hb.appendChild(tr);
  }
  if (!r.holdings.length) {
    const tr = document.createElement("tr");
    td(tr, "尚無持股，請在下方新增交易").colSpan = 7;
    hb.appendChild(tr);
  }

  renderViews(r.holdings);

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
    $("view-note").innerHTML = '需要密碼：先到 <a href="picks.html">候選股票</a> 頁輸入密碼並勾選「記住密碼」，這裡就會顯示。';
    return;
  }
  try {
    const box = await (await fetch(`picks.json?t=${Date.now()}`)).json();
    const d = await decrypt(box, pw);
    VIEW = d;
  } catch {
    $("view-note").textContent = "讀不到模型看法（今天的資料還沒產生，或密碼已變更）。";
    return;
  }
  render();
}

const ADVICE_CLASS = { 續抱: "keep", 留意: "watch", 考慮減碼: "reduce" };
function renderViews(holdings) {
  if (!VIEW) return;
  const box = $("views");
  box.innerHTML = "";
  $("view-note").textContent = `資料日 ${VIEW.data_date}｜模型分數是全市場可投資股票中的百分位（100% 最好）。`;
  if (!holdings.length) { box.innerHTML = '<p class="note">還沒有持股。</p>'; return; }
  for (const h of holdings) {
    const v = VIEW.scores[h.code];
    const el = document.createElement("div");
    el.className = "view";
    const head = document.createElement("p");
    const badge = document.createElement("span");
    badge.className = `advice ${v ? ADVICE_CLASS[v.advice.level] : "none"}`;
    badge.textContent = v ? v.advice.level : "不評分";
    head.appendChild(badge);
    head.append(` ${h.code} ${h.name}`);
    el.appendChild(head);
    const p = document.createElement("p");
    p.className = "note";
    if (!v) {
      p.textContent = h.type === "etf" ? "ETF 不在模型評分範圍，請參考市場狀態頁。" : "成交量太小或資料不足，不在模型評分範圍。";
      el.appendChild(p);
    } else {
      p.textContent = `${v.advice.why}｜模型分數 ${Math.round(v.pct * 100)}%（${v.bucket}），過去同等級 60 日後上漲機率 ${Math.round(v.up * 100)}%、中間值 ${v.q50 > 0 ? "+" : ""}${(v.q50 * 100).toFixed(1)}%`;
      el.appendChild(p);
      const rs = v.reasons || {};
      const ok = (v.checks || []).filter((c) => c.ok === true).map((c) => c.name);
      const ul = document.createElement("ul");
      ul.className = "note";
      if (rs.pros && rs.pros.length) { const li = document.createElement("li"); li.textContent = `加分：${rs.pros.join("；")}`; ul.appendChild(li); }
      if (rs.cons && rs.cons.length) { const li = document.createElement("li"); li.textContent = `扣分：${rs.cons.join("；")}`; ul.appendChild(li); }
      const li = document.createElement("li");
      li.textContent = `符合的量化條件：${ok.length ? ok.join("、") : "無"}`;
      ul.appendChild(li);
      el.appendChild(ul);
    }
    box.appendChild(el);
  }
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
  .then((data) => { P = data; init(); loadViews(); })
  .catch(() => { $("price-date").textContent = "行情讀取失敗，請稍後再試。"; });
