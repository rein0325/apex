// 個股近一年走勢（還原收盤價）：讀 h/代號.json，畫成簡單的折線圖。公開資料，不含個人資訊。
"use strict";

let __dates = null;
const __hist = {};

// 回傳 {dates, vals}（還原收盤價，以最新一天為基準）；沒有資料時丟出錯誤
async function loadHistory(code) {
  if (!__dates) __dates = await (await fetch("h/_dates.json")).json();
  if (!__hist[code]) {
    __hist[code] = fetch(`h/${code}.json`).then((res) => {
      if (!res.ok) throw new Error(res.status);
      return res.json();
    });
  }
  try {
    return { dates: __dates, vals: await __hist[code] };
  } catch (e) {
    delete __hist[code];
    throw e;
  }
}

async function stockChart(code, el) {
  el.textContent = "讀取中…";
  try {
    const { dates, vals } = await loadHistory(code);
    const W = 420, H = 150, L = 40, R = 8, T = 8, B = 20;
    const pts = vals.map((v, i) => [i, v]).filter(([, v]) => v !== null);
    if (pts.length < 2) { el.textContent = "沒有足夠的走勢資料。"; return; }
    const lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1])), span = hi - lo || 1;
    const n = vals.length;
    const x = (i) => L + ((W - L - R) * i) / Math.max(n - 1, 1);
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / span);
    const first = pts[0][1], last = pts[pts.length - 1][1];
    const color = last >= first ? "var(--up)" : "var(--down)";
    const fmt = (v) => (v >= 100 ? Math.round(v).toLocaleString("zh-TW") : v.toFixed(2));
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${code} 近一年走勢">` +
      [lo, (lo + hi) / 2, hi].map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>` +
        `<text x="${L - 4}" y="${y(v) + 4}" class="tick" text-anchor="end">${fmt(v)}</text>`).join("") +
      `<polyline points="${pts.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}" style="stroke:${color}"/>` +
      `<text x="${L}" y="${H - 4}" class="tick">${dates[0]}</text>` +
      `<text x="${W - R}" y="${H - 4}" class="tick" text-anchor="end">${dates[n - 1]}</text></svg>` +
      `<p class="note">近一年 ${((last / first - 1) * 100).toFixed(0)}%（還原股價，含股利）</p>`;
  } catch {
    el.textContent = "沒有這檔股票的走勢資料。";
  }
}

// 在 parent 裡加一個「看近一年走勢」按鈕，點了才載入
function chartToggle(code, parent) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "secondary small";
  btn.textContent = "看近一年走勢";
  const box = document.createElement("div");
  btn.onclick = () => {
    if (box.dataset.loaded) { box.hidden = !box.hidden; return; }
    box.dataset.loaded = "1";
    stockChart(code, box);
  };
  parent.appendChild(btn);
  parent.appendChild(box);
}

// 出場規則與建議投入比例：data.json 的 risk（後端 apex/risk.py，數字由回測研究決定）。沒有時回傳 null。
const riskReady = fetch(`data.json?t=${Date.now()}`)
  .then((res) => (res.ok ? res.json() : null))
  .then((d) => (d && d.risk ? { ...d.risk, regime: d.regime } : null))
  .catch(() => null);
