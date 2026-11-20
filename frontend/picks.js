// 候選股票：picks.json 是用密碼加密的（PBKDF2-SHA256 → AES-GCM），在瀏覽器裡解開，密碼不會傳出去。
// 輸入總投入金額後，依規則從候選名單組出建議組合（系統規格書 STEP 07：建議部位與股數計算）。
"use strict";

const PW_KEY = "apex.viewpw.v1";
const BUDGET_KEY = "apex.budget.total.v1";
const $ = (id) => document.getElementById(id);
const pct = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const num = (x, d = 2) => (x === null || x === undefined ? "—" : Number(x).toLocaleString("zh-TW", { maximumFractionDigits: d }));
const money = (x) => Math.round(x).toLocaleString("zh-TW");
const sgn = (x) => `${x >= 0 ? "+" : ""}${money(x)}`;
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 私密模式 */ } },
  del: (k) => { try { localStorage.removeItem(k); } catch { /* 私密模式 */ } },
};

let BOX = null, DATA = null, RISK = null;

async function decrypt(box, password) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: b64(box.salt), iterations: box.iter },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(box.iv) }, key, b64(box.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}

// ── 費用 ────────────────────────────────────────────────────────────────
const fee = (value) => (value > 0 ? Math.max(1, Math.floor(value * 0.001425)) : 0);
// 持有到期後的損益：賣出金額 − 賣出手續費 − 證交稅 0.3% −（買進金額＋買進手續費）
function profit(shares, close, q) {
  const cost = shares * close, sell = cost * (1 + q);
  return sell - fee(sell) - Math.floor(sell * 0.003) - (cost + fee(cost));
}

// 目前持有的代號（從「我的持股」頁的交易紀錄粗算，只用來標示「已持有」）
function heldCodes() {
  let trades = [];
  try { trades = JSON.parse(localStorage.getItem("apex.trades.v1") || "[]"); } catch { /* 沒有資料 */ }
  const net = {};
  for (const t of trades) net[t.code] = (net[t.code] || 0) + (t.side === "buy" ? t.shares : -t.shares);
  return new Set(Object.entries(net).filter(([, s]) => s > 0).map(([c]) => c));
}

// ── 依金額組合 ──────────────────────────────────────────────────────────
// 規則（白話寫在頁面上）：
// 1. 金額越多選越多檔（2～8 檔），避免每檔金額太小。
// 2. 同一產業最多四成的檔數，避免全押同一個方向；走勢太像的不重複選。
// 3. 依波動反比分配：波動越大的股票分得越少（研究中可降低回撤）。
// 4. 小金額：同一個分數等級裡（網站上的預期報酬相同），優先選分到的錢能買 4 股以上的股票，
//    避免一檔只買得起 1～2 股、錢分得不平均。
// 5. 分到的金額買不起 1 股就跳過，改選下一名。
// 6. 買完整數股剩下的零頭，再逐股補給還沒買滿的股票（每檔最多超過分配 25%），讓錢盡量用完。
const TARGETS = [[20000, 2], [50000, 3], [100000, 4], [200000, 5], [400000, 6], [Infinity, 8]];
const MIN_SHARES = 4;
const OVERFILL = 1.25;

const MAX_CORR = 0.8; // 近 60 日走勢相關係數超過這個值，視為「太像」，不重複選
function corrOf(a, b) {
  const c = DATA.correlation;
  if (!c) return null;
  const i = c.codes.indexOf(a), j = c.codes.indexOf(b);
  return i < 0 || j < 0 ? null : c.matrix[i][j];
}

const buyCost = (shares, price) => shares * price + fee(shares * price);

function plan(cands, budget) {
  const n = TARGETS.find(([lim]) => budget < lim)[1];
  const indCap = Math.max(1, Math.floor(n * 0.4));
  const est = budget / n; // 平均每檔大約分到的錢
  const bucketOf = (r) => (r.h60 ? r.h60.bucket : "");
  const order = new Map();
  cands.forEach((r) => { if (!order.has(bucketOf(r))) order.set(bucketOf(r), order.size); });
  const fits = (r) => Math.floor(est / (r.close * 1.001425)) >= MIN_SHARES;
  // 等級不變；同等級內，價格適合這個金額的排前面，其餘照原本名次
  const sorted = cands.map((r, i) => ({ r, i }))
    .sort((a, b) => order.get(bucketOf(a.r)) - order.get(bucketOf(b.r)) || fits(b.r) - fits(a.r) || a.i - b.i)
    .map((x) => x.r);
  const skipped = new Map();
  let similar = new Map();
  for (let round = 0; round < 40; round++) {
    const chosen = [], perInd = {};
    similar = new Map();
    for (const r of sorted) {
      if (chosen.length >= n) break;
      if (skipped.has(r.code) || !r.h60 || !(r.close > 0)) continue;
      const ind = r.industry || "其他";
      if ((perInd[ind] || 0) >= indCap) continue;
      const twin = chosen.find((c) => (corrOf(c.code, r.code) ?? 0) > MAX_CORR);
      if (twin) { similar.set(r.code, `和 ${twin.code} ${twin.name} 走勢太像（相關係數 ${corrOf(twin.code, r.code).toFixed(2)}）`); continue; }
      perInd[ind] = (perInd[ind] || 0) + 1;
      chosen.push(r);
    }
    if (!chosen.length) return { n, indCap, rows: [], skipped, similar, cash: budget, priced: new Map() };
    const inv = chosen.map((r) => 1 / Math.max(r.vol_60 || 0.4, 0.1));
    const total = inv.reduce((a, b) => a + b, 0);
    const rows = chosen.map((r, i) => {
      const alloc = (budget * inv[i]) / total;
      return { r, alloc, shares: Math.floor(alloc / (r.close * 1.001425)) };
    });
    const bad = rows.filter((x) => x.shares < 1);
    if (bad.length) {
      for (const x of bad) skipped.set(x.r.code, `每股 ${num(x.r.close, 0)} 元，超過分到的約 ${money(x.alloc)} 元`);
      continue;
    }
    // 零頭補滿：每次給「離分配金額最遠」而且買得起的那檔多買 1 股
    let cash = budget - rows.reduce((s, x) => s + buyCost(x.shares, x.r.close), 0);
    for (let k = 0; k < 100000; k++) {
      const next = rows
        .filter((x) => buyCost(x.shares + 1, x.r.close) - buyCost(x.shares, x.r.close) <= cash && (x.shares + 1) * x.r.close <= x.alloc * OVERFILL)
        .sort((a, b) => (b.alloc - b.shares * b.r.close) - (a.alloc - a.shares * a.r.close))[0];
      if (!next) break;
      cash -= buyCost(next.shares + 1, next.r.close) - buyCost(next.shares, next.r.close);
      next.shares += 1;
    }
    // 因為價格不適合而改選同等級其他股票的（只列排在被選股票前面的）
    const priced = new Map();
    const picked = new Set(chosen.map((r) => r.code));
    for (const r of cands) {
      if (picked.has(r.code) || fits(r) || !r.h60) continue;
      const later = chosen.find((c) => bucketOf(c) === bucketOf(r) && cands.indexOf(c) > cands.indexOf(r));
      if (later) priced.set(r.code, `每股 ${num(r.close, 0)} 元，分到約 ${money(est)} 元只買得起 ${Math.floor(est / (r.close * 1.001425))} 股；同等級改選價格較適合的 ${later.code} ${later.name}`);
    }
    return { n, indCap, rows, skipped, similar, cash, priced };
  }
  return { n, indCap, rows: [], skipped, similar, cash: budget, priced: new Map() };
}

function renderPlan(budget) {
  const box = $("plan");
  box.innerHTML = "";
  $("rules-help").innerHTML = "";
  const held = heldCodes();
  $("exposure-box").hidden = !(RISK && RISK.bad_now && budget > 0);
  if (!(budget > 0)) {
    box.innerHTML = '<p class="subtle">輸入金額後，會幫你決定買哪幾檔、各買幾股。</p>';
    return [];
  }
  const p = plan(DATA.candidates, budget);
  $("rules-help").appendChild(UI.help("怎麼配的", [
    `這個金額選 ${p.n} 檔；同一產業最多 ${p.indCap} 檔。`,
    `走勢太像的（近 60 日相關係數超過 ${MAX_CORR}）不重複選。`,
    "波動越大的股票分得越少；分到的錢買不起 1 股就換下一名。",
    `金額小的時候，同一個分數等級裡（預期報酬相同）優先選能買 ${MIN_SHARES} 股以上的股票，錢分得比較平均。`,
    "買完整數股剩下的零頭，會再補買幾股，讓錢盡量用完。",
    "股數多少本身不影響賺多少：同樣投入 5,000 元、漲 5%，買 3 股或 100 股都是賺約 250 元。",
    ...[...p.priced].map(([c, why]) => `沒選 ${c}：${why}`),
    ...[...p.similar].map(([c, why]) => `沒選 ${c}：${why}`),
    ...[...p.skipped].map(([c, why]) => `略過 ${c}：${why}`),
  ]));
  if (!p.rows.length) {
    box.innerHTML = `<div class="banner warn">${UI.icon("alert")}<span>這個金額太少，買不起候選股票。可以提高金額。</span></div>`;
    return [];
  }
  let invested = 0, s25 = 0, s50 = 0, s75 = 0;
  const items = [];
  for (const x of p.rows) {
    const cost = x.shares * x.r.close + fee(x.shares * x.r.close);
    invested += cost;
    const h = x.r.h60;
    s25 += profit(x.shares, x.r.close, h.q25);
    s50 += profit(x.shares, x.r.close, h.q50);
    s75 += profit(x.shares, x.r.close, h.q75);
    items.push({ x, cost });
  }
  const lo = Math.min(s25, 0) * 1.15 - 1, hi = Math.max(s75, 0) * 1.15 + 1;
  let html = '<p class="subtle" style="margin:12px 0 0">3 個月後，中間的情況大約</p>' +
    `<div class="big-num ${cls(s50)}">${sgn(s50)} 元</div>` +
    `<div class="subtle">約 ${pct(s50 / invested)}，已扣買賣成本</div>` +
    UI.bar(lo, hi, { band: { from: s25, to: s75 }, marks: [{ v: 0, title: "不賺不賠" }, { v: s50, cls: "mid", title: "中間情況" }] }) +
    `<div class="bar-labels"><span>差的情況 ${sgn(s25)}</span><span>好的情況 ${sgn(s75)}</span></div>`;
  html += '<div class="row-list" style="margin-top:14px">';
  for (const { x, cost } of items) {
    html += `<div class="line-item"><span class="name">${UI.esc(x.r.code)} ${UI.esc(x.r.name)}${held.has(x.r.code) ? " " + UI.chip("已持有", "info") : ""}</span>` +
      `<span class="side"><b>${x.shares.toLocaleString("zh-TW")} 股</b> <span class="subtle">${money(cost)} 元</span></span>` +
      `<span class="minibar"><i style="width:${Math.round((cost / budget) * 100)}%"></i></span></div>`;
  }
  html += `<div class="line-item"><span class="name subtle">${UI.icon("wallet")} 留著的現金</span><span class="side"><b>${money(p.cash)} 元</b></span>` +
    `<span class="minibar"><i style="width:${Math.round((p.cash / budget) * 100)}%;background:var(--muted)"></i></span></div></div>`;
  box.innerHTML = html;
  return p.rows.map((x) => ({ r: x.r, shares: x.shares }));
}

// ── 個股卡片 ────────────────────────────────────────────────────────────
function card(r, shares) {
  const h = DATA.horizons[0], x = r[`h${h}`];
  const el = document.createElement("section");
  el.className = "card pick";
  el.innerHTML = `<div class="pick-top">${x ? UI.ring(x.up, 58, "上漲機率") : ""}` +
    `<div class="ttl"><b>${UI.esc(r.code)} ${UI.esc(r.name)}</b><span class="subtle">第 ${r.rank} 名${r.industry ? "｜" + UI.esc(r.industry) : ""}${x ? "｜3 個月後上漲機率" : ""}</span></div>` +
    `<div class="side" style="text-align:right"><b>${num(r.close)}</b><div class="subtle">收盤</div></div></div>`;
  if (!x) return el;
  const px = x.price;
  const lo = Math.min(px.q25, r.close) * 0.97, hi = Math.max(px.q75, r.close) * 1.03;
  el.insertAdjacentHTML("beforeend",
    UI.bar(lo, hi, { band: { from: px.q25, to: px.q75 }, marks: [{ v: px.q50, cls: "mid", title: "中間值" }, { v: r.close, dot: true, title: "現價" }] }) +
    `<div class="bar-labels"><span>${num(px.q25, 0)}</span><span>現價 <b>${num(r.close)}</b> → 中間 <b class="${cls(x.q50)}">${num(px.q50)}（${pct(x.q50)}）</b></span><span>${num(px.q75, 0)}</span></div>`);
  const chips = (r.checks || []).filter((c) => c.ok !== null).map((c) => UI.chip(c.name, c.ok ? "ok" : "", c.ok ? "check" : "x")).join("");
  const facts = [];
  if (shares) facts.push(`<span>${UI.icon("cart")}建議 <b>${shares.toLocaleString("zh-TW")} 股</b></span>`);
  facts.push(`<span>${UI.icon("wave")}波動 <b>${r.vol_60 ? Math.round(r.vol_60 * 100) + "%" : "—"}</b></span>`);
  if (x.beat != null) facts.push(`<span>${UI.icon("star")}贏大盤 <b>${Math.round(x.beat * 100)}%</b></span>`);
  el.insertAdjacentHTML("beforeend", `<div style="margin-top:10px">${chips}</div><div class="facts">${facts.join("")}</div>`);

  const rs = r.reasons || {};
  const more = [
    ...(rs.pros || []).map((t) => `加分：${t}`),
    ...(rs.cons || []).map((t) => `扣分：${t}`),
    ...(r.checks || []).map((c) => `${c.ok === true ? "✓" : c.ok === false ? "✗" : "—"} ${c.name}：${c.desc}`),
    ...[20, 120].filter((k) => r[`h${k}`]).map((k) => `${k} 個交易日後：上漲機率 ${Math.round(r[`h${k}`].up * 100)}%、中間值 ${pct(r[`h${k}`].q50)}`),
    `近半年最大跌幅 ${pct(r.mdd_120, 0)}；過去同等級平均（扣成本）${pct(x.net_mean)}，常被少數大漲拉高。`,
  ];
  if (shares) {
    more.push(`買 ${shares.toLocaleString("zh-TW")} 股：中間情況損益約 ${sgn(profit(shares, r.close, x.q50))} 元，` +
      `一半的情況在 ${sgn(profit(shares, r.close, x.q25))} ～ ${sgn(profit(shares, r.close, x.q75))} 元（已扣成本）。`);
  }
  el.appendChild(UI.help("為什麼選它", more));
  chartToggle(r.code, el);
  return el;
}

function renderTrack() {
  const t = DATA.track, box = $("track");
  box.innerHTML = "";
  if (!t) { box.textContent = "尚無紀錄。"; return; }
  const kind = { 正常: "good", 注意: "warn", 失準: "bad", 落後大盤: "bad" }[t.health.status] || "";
  const ic = { 正常: "check", 注意: "alert", 失準: "pause", 落後大盤: "pause" }[t.health.status] || "clock";
  box.innerHTML = `<div class="statusline">${UI.chip(t.health.status, `${kind} big`, ic)}</div><p class="subtle">${UI.esc(t.health.msg)}</p>`;
  if (t.matured) {
    const m = t.matured;
    box.insertAdjacentHTML("beforeend",
      `<div class="bar-labels"><span>實際上漲比例</span><b>${Math.round(m.hit * 100)}%</b></div>${UI.bar(0, 1, { fill: { v: m.hit }, marks: [{ v: m.expected_up, cls: "mid", title: "當時預期" }] })}` +
      `<div class="bar-labels"><span>紅線＝當時預期 ${Math.round(m.expected_up * 100)}%</span><span>平均 ${pct(m.mean)}｜0050 ${pct(m.bench)}</span></div>`);
  }
  const items = (t.recent || []).map((r) => `${r.date}：持有 ${r.days} 天${r.matured ? "（到期）" : ""}，前 ${t.top} 名平均 ${pct(r.mean)}、上漲 ${Math.round(r.hit * 100)}%；0050 ${pct(r.bench)}`);
  if (t.since) items.push(`自 ${t.since} 起每天記錄前 ${t.top || 10} 名；滿 ${t.horizon || 60} 個交易日才算到期。報酬不含交易成本。`);
  if (items.length) box.appendChild(UI.help("最近每天的推薦成績", items));
}

function render() {
  const d = DATA, h = d.horizons[0];
  renderTrack();
  const bad = d.track && ["失準", "落後大盤"].includes(d.track.health.status);
  $("alert").hidden = !bad;
  if (bad) $("alert").innerHTML = `${UI.icon("pause")}<span><b>策略失效警示：${UI.esc(d.track.health.status)}</b><br>${UI.esc(d.track.health.msg)}</span>`;
  $("meta").textContent = `資料日 ${d.data_date}｜模型訓練到 ${d.trained_until}`;
  const b = d.base[h];
  $("base").textContent = `對照：隨便挑一檔股票，過去 ${h} 個交易日後上漲機率 ${Math.round(b.up * 100)}%、中間值 ${pct(b.q50)}` +
    (b.beat != null ? `；比大盤好的機率 ${Math.round(b.beat * 100)}%。` : "。") + `歷史戰績：${d.calibration}`;
  const budget = Number($("budget").value) || 0;
  const picked = renderPlan(budget);
  const pickedCodes = new Set(picked.map((x) => x.r.code));
  const list = $("list");
  list.innerHTML = "";
  $("detail-title").hidden = !picked.length;
  for (const x of picked) list.appendChild(card(x.r, x.shares));
  const others = d.candidates.filter((r) => !pickedCodes.has(r.code));
  $("others-title").textContent = picked.length ? `其他候選（${others.length} 檔）` : `全部候選（${others.length} 檔）`;
  $("others-box").open = !picked.length;
  const ob = $("others");
  ob.innerHTML = "";
  for (const r of others) ob.appendChild(card(r, 0));
}

async function unlock(password, remember) {
  try {
    DATA = await decrypt(BOX, password);
  } catch {
    $("lock-msg").hidden = false;
    $("lock-msg").textContent = "密碼不對，請再試一次。";
    store.del(PW_KEY);
    return;
  }
  if (remember) store.set(PW_KEY, password);
  $("lock").hidden = true;
  $("content").hidden = false;
  render();
}

function init() {
  $("budget").value = store.get(BUDGET_KEY) || "";
  let timer = null;
  $("budget").addEventListener("input", () => {
    store.set(BUDGET_KEY, $("budget").value);
    clearTimeout(timer);
    timer = setTimeout(() => { if (DATA) render(); }, 250);
  });
  const cash = Number(store.get("apex.cash.v1") || 0);
  if (cash > 0) {
    $("use-cash").hidden = false;
    $("use-cash").textContent = `用「我的持股」的現金 ${money(cash)} 元`;
    $("use-cash").addEventListener("click", () => {
      $("budget").value = String(Math.floor(cash));
      store.set(BUDGET_KEY, $("budget").value);
      if (DATA) render();
    });
  }
  riskReady.then((r) => { RISK = r; if (DATA) render(); });
  $("unlock").addEventListener("submit", (e) => { e.preventDefault(); unlock($("password").value, $("remember").checked); });
  $("logout").addEventListener("click", () => { store.del(PW_KEY); location.reload(); });
  fetch(`picks.json?t=${Date.now()}`)
    .then((res) => { if (!res.ok) throw new Error(res.status); return res.json(); })
    .then((box) => {
      BOX = box;
      const saved = store.get(PW_KEY);
      if (saved) unlock(saved, true);
    })
    .catch(() => {
      $("lock-msg").hidden = false;
      $("lock-msg").textContent = "今天的候選股票還沒產生（或尚未設定密碼）。";
      $("unlock").querySelector("button").disabled = true;
    });
}
init();
