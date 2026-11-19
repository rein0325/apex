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

let BOX = null, DATA = null;

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
// 2. 同一產業最多四成的檔數，避免全押同一個方向。
// 3. 依波動反比分配：波動越大的股票分得越少（研究中可降低回撤）。
// 4. 分到的金額買不起 1 股就跳過，改選下一名。
const TARGETS = [[20000, 2], [50000, 3], [100000, 4], [200000, 5], [400000, 6], [Infinity, 8]];

const MAX_CORR = 0.8; // 近 60 日走勢相關係數超過這個值，視為「太像」，不重複選
function corrOf(a, b) {
  const c = DATA.correlation;
  if (!c) return null;
  const i = c.codes.indexOf(a), j = c.codes.indexOf(b);
  return i < 0 || j < 0 ? null : c.matrix[i][j];
}

function plan(cands, budget) {
  const n = TARGETS.find(([lim]) => budget < lim)[1];
  const indCap = Math.max(1, Math.floor(n * 0.4));
  const skipped = new Map();
  let similar = new Map();
  for (let round = 0; round < 40; round++) {
    const chosen = [], perInd = {};
    similar = new Map();
    for (const r of cands) {
      if (chosen.length >= n) break;
      if (skipped.has(r.code) || !r.h60 || !(r.close > 0)) continue;
      const ind = r.industry || "其他";
      if ((perInd[ind] || 0) >= indCap) continue;
      const twin = chosen.find((c) => (corrOf(c.code, r.code) ?? 0) > MAX_CORR);
      if (twin) { similar.set(r.code, `和 ${twin.code} ${twin.name} 走勢太像（相關係數 ${corrOf(twin.code, r.code).toFixed(2)}）`); continue; }
      perInd[ind] = (perInd[ind] || 0) + 1;
      chosen.push(r);
    }
    if (!chosen.length) return { n, indCap, rows: [], skipped, similar, cash: budget };
    const inv = chosen.map((r) => 1 / Math.max(r.vol_60 || 0.4, 0.1));
    const total = inv.reduce((a, b) => a + b, 0);
    const rows = chosen.map((r, i) => {
      const alloc = (budget * inv[i]) / total;
      const shares = Math.floor(alloc / (r.close * 1.001425));
      return { r, alloc, shares };
    });
    const bad = rows.filter((x) => x.shares < 1);
    if (!bad.length) {
      const used = rows.reduce((s, x) => s + x.shares * x.r.close + fee(x.shares * x.r.close), 0);
      return { n, indCap, rows, skipped, similar, cash: budget - used };
    }
    for (const x of bad) skipped.set(x.r.code, `每股 ${num(x.r.close, 0)} 元，超過分到的約 ${money(x.alloc)} 元`);
  }
  return { n, indCap, rows: [], skipped, similar, cash: budget };
}

function renderPlan(budget) {
  const box = $("plan");
  box.innerHTML = "";
  const held = heldCodes();
  if (!(budget > 0)) {
    box.innerHTML = '<p class="note">輸入金額後，會依下方規則組出建議股數。</p>';
    return [];
  }
  const p = plan(DATA.candidates, budget);
  $("rules").textContent = `這個金額的規則：選 ${p.n} 檔；同一產業最多 ${p.indCap} 檔；走勢太像的（近 60 日相關係數超過 ${MAX_CORR}）不重複選；波動越大的股票分得越少；分到的錢買不起 1 股就換下一名。`;
  if (!p.rows.length) {
    box.innerHTML = '<p class="warn">這個金額太少，買不起候選股票。可以提高金額，或考慮零股價格較低的股票。</p>';
    return [];
  }
  const t = document.createElement("table");
  t.innerHTML = "<thead><tr><th>股票</th><th>股數</th><th>金額</th><th>比重</th><th>中間值價位</th></tr></thead>";
  const tb = document.createElement("tbody");
  let invested = 0, s25 = 0, s50 = 0, s75 = 0, avg = 0;
  for (const x of p.rows) {
    const cost = x.shares * x.r.close + fee(x.shares * x.r.close);
    invested += cost;
    const h = x.r.h60;
    s25 += profit(x.shares, x.r.close, h.q25);
    s50 += profit(x.shares, x.r.close, h.q50);
    s75 += profit(x.shares, x.r.close, h.q75);
    avg += x.shares * x.r.close * h.net_mean;
    const tr = document.createElement("tr");
    const cells = [`${x.r.code} ${x.r.name}${held.has(x.r.code) ? "（已持有）" : ""}`, x.shares.toLocaleString("zh-TW"),
      money(cost), `${Math.round((cost / budget) * 100)}%`, num(h.price.q50)];
    for (const c of cells) { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  const wrap = document.createElement("div");
  wrap.className = "scroll";
  wrap.appendChild(t);
  box.appendChild(wrap);

  const sum = document.createElement("div");
  sum.className = "kpis";
  const kpi = (label, value, sub, c = "") => {
    const d = document.createElement("div");
    d.className = "kpi";
    d.innerHTML = "<span></span><b></b><small></small>";
    d.querySelector("span").textContent = label;
    d.querySelector("b").textContent = value;
    if (c) d.querySelector("b").className = c;
    d.querySelector("small").textContent = sub;
    sum.appendChild(d);
  };
  kpi("實際投入（含手續費）", money(invested), `剩下現金 ${money(p.cash)} 元`);
  kpi("每檔都落在中間值", `${sgn(s50)} 元`, `約 ${pct(s50 / invested)}，60 個交易日後`, cls(s50));
  kpi("一半範圍", `${sgn(s25)} ～ ${sgn(s75)} 元`, "每檔都在下緣／上緣的極端假設，實際多半介於中間");
  kpi("平均（扣成本）", `${sgn(avg)} 元`, "被少數大漲拉高，參考用", cls(avg));
  box.appendChild(sum);

  if (p.similar && p.similar.size) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "沒選（走勢太像）：" + [...p.similar].map(([c, why]) => {
      const r = DATA.candidates.find((x) => x.code === c);
      return `${c} ${r ? r.name : ""}（${why}）`;
    }).join("；");
    box.appendChild(note);
  }
  if (p.skipped.size) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "略過：" + [...p.skipped].map(([c, why]) => {
      const r = DATA.candidates.find((x) => x.code === c);
      return `${c} ${r ? r.name : ""}（${why}）`;
    }).join("；");
    box.appendChild(note);
  }
  return p.rows.map((x) => ({ r: x.r, shares: x.shares }));
}

// ── 推薦理由 ────────────────────────────────────────────────────────────
// 模型理由：LightGBM 各特徵對分數的貢獻（往上推最多／往下拉最多的幾項）；量化條件：Phase 2 的規則是否成立。
function explain(r) {
  const box = document.createElement("div");
  box.className = "why";
  const list = (title, items, className) => {
    if (!items || !items.length) return;
    const h = document.createElement("p");
    h.className = `why-title ${className}`;
    h.textContent = title;
    box.appendChild(h);
    const ul = document.createElement("ul");
    for (const t of items) { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); }
    box.appendChild(ul);
  };
  const rs = r.reasons || {};
  list("為什麼選它（模型最看重的）", rs.pros, "up");
  list("模型扣分的地方", rs.cons, "down");
  if (r.checks && r.checks.length) {
    const ok = r.checks.filter((c) => c.ok === true).length;
    const known = r.checks.filter((c) => c.ok !== null).length;
    const p = document.createElement("p");
    p.className = "why-title";
    p.textContent = `量化條件：符合 ${ok} / ${known} 項${ok === 0 ? "（和模型意見不同，要特別小心）" : ok >= 3 ? "（和模型意見一致）" : ""}`;
    box.appendChild(p);
    const ul = document.createElement("ul");
    ul.className = "checks";
    for (const c of r.checks) {
      const li = document.createElement("li");
      li.textContent = `${c.ok === true ? "✓" : c.ok === false ? "✗" : "—"} ${c.name}：${c.desc}`;
      if (c.ok === true) li.className = "ok";
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }
  return box;
}

// ── 個股卡片 ────────────────────────────────────────────────────────────
function card(r, shares) {
  const h = DATA.horizons[0], x = r[`h${h}`], s = r.h20;
  const el = document.createElement("section");
  el.className = "card pick";
  const head = document.createElement("div");
  head.className = "pick-head";
  head.innerHTML = '<div><b class="pick-name"></b><span class="note pick-ind"></span></div><div class="pick-price"></div>';
  head.querySelector(".pick-name").textContent = `${r.code} ${r.name}`;
  head.querySelector(".pick-ind").textContent = `｜第 ${r.rank} 名${r.industry ? "｜" + r.industry : ""}`;
  head.querySelector(".pick-price").textContent = `收盤 ${num(r.close)}`;
  el.appendChild(head);
  if (!x) return el;
  const grid = document.createElement("div");
  grid.className = "kpis";
  const kpi = (label, value, sub, c = "") => {
    const d = document.createElement("div");
    d.className = "kpi";
    d.innerHTML = "<span></span><b></b><small></small>";
    d.querySelector("span").textContent = label;
    d.querySelector("b").textContent = value;
    if (c) d.querySelector("b").className = c;
    d.querySelector("small").textContent = sub;
    grid.appendChild(d);
  };
  kpi(`${h} 個交易日後上漲機率`, `${Math.round(x.up * 100)}%`, `分數等級：${x.bucket}`);
  kpi("預期中間值", pct(x.q50), `約 ${num(x.price.q50)} 元`, cls(x.q50));
  kpi("一半落在", `${pct(x.q25, 0)} ～ ${pct(x.q75, 0)}`, `約 ${num(x.price.q25, 0)} ～ ${num(x.price.q75, 0)} 元`);
  kpi("平均（扣成本）", pct(x.net_mean), "被少數大漲拉高，參考用", cls(x.net_mean));
  el.appendChild(grid);
  const risk = document.createElement("p");
  risk.className = "note";
  const sub = s ? `｜20 日：上漲機率 ${Math.round(s.up * 100)}%、中間值 ${pct(s.q50)}` : "";
  risk.textContent = `波動 ${r.vol_60 ? Math.round(r.vol_60 * 100) + "%" : "—"}｜近半年最大跌幅 ${pct(r.mdd_120, 0)}${sub}`;
  el.appendChild(risk);
  el.appendChild(explain(r));
  if (shares) {
    const p = document.createElement("p");
    p.className = "calc";
    p.textContent = `建議 ${shares.toLocaleString("zh-TW")} 股：落在中間值損益約 ${sgn(profit(shares, r.close, x.q50))} 元；` +
      `一半的情況在 ${sgn(profit(shares, r.close, x.q25))} ～ ${sgn(profit(shares, r.close, x.q75))} 元之間（已扣買賣成本）。`;
    el.appendChild(p);
  }
  return el;
}

function renderTrack() {
  const t = DATA.track, box = $("track");
  box.innerHTML = "";
  if (!t) { box.textContent = "尚無紀錄。"; return; }
  const status = document.createElement("p");
  const badge = document.createElement("span");
  badge.className = `advice ${{ 正常: "keep", 注意: "watch", 失準: "reduce" }[t.health.status] || "none"}`;
  badge.textContent = t.health.status;
  status.appendChild(badge);
  status.append(` ${t.health.msg}`);
  box.appendChild(status);
  if (t.matured) {
    const m = t.matured, p = document.createElement("p");
    p.textContent = `已到期 ${m.n} 筆（${m.dates} 天的推薦）：實際上漲 ${Math.round(m.hit * 100)}%（當時預期 ${Math.round(m.expected_up * 100)}%），平均 ${pct(m.mean)}、中間值 ${pct(m.median)}；同期 0050 平均 ${pct(m.bench)}。`;
    box.appendChild(p);
  }
  if (t.recent && t.recent.length) {
    const tbl = document.createElement("table");
    tbl.innerHTML = `<thead><tr><th>推薦日</th><th>持有天數</th><th>前 ${t.top} 名平均</th><th>上漲比例</th><th>0050 同期</th></tr></thead>`;
    const tb = document.createElement("tbody");
    for (const r of t.recent) {
      const tr = document.createElement("tr");
      for (const [v, c] of [[r.date, ""], [`${r.days}${r.matured ? "（到期）" : ""}`, ""], [pct(r.mean), cls(r.mean)],
        [`${Math.round(r.hit * 100)}%`, ""], [pct(r.bench), cls(r.bench)]]) {
        const td = document.createElement("td"); td.textContent = v; if (c) td.className = c; tr.appendChild(td);
      }
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    const wrap = document.createElement("div"); wrap.className = "scroll"; wrap.appendChild(tbl);
    box.appendChild(wrap);
  }
  if (t.since) {
    const n = document.createElement("p");
    n.className = "note";
    n.textContent = `自 ${t.since} 起每天記錄前 ${t.top || 10} 名。持有天數是交易日；滿 ${t.horizon || 60} 個交易日才算到期。報酬不含交易成本。`;
    box.appendChild(n);
  }
}

function render() {
  const d = DATA, h = d.horizons[0];
  renderTrack();
  $("meta").textContent = `資料日 ${d.data_date}｜模型每天用最新已知結果重新訓練（訓練資料到 ${d.trained_until}）｜歷史戰績：${d.calibration}`;
  const b = d.base[h];
  $("base").textContent = `對照：隨便挑一檔股票，過去 ${h} 個交易日後上漲機率 ${Math.round(b.up * 100)}%、中間值 ${pct(b.q50)}。`;
  const budget = Number($("budget").value) || 0;
  const picked = renderPlan(budget);
  const pickedCodes = new Set(picked.map((x) => x.r.code));
  const list = $("list");
  list.innerHTML = "";
  if (picked.length) {
    $("detail-title").hidden = false;
    for (const x of picked) list.appendChild(card(x.r, x.shares));
  } else {
    $("detail-title").hidden = true;
  }
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
