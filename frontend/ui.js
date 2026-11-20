// 三個頁面共用的圖示與小圖形（紅綠燈、圓環、區間條）。全部放在 UI 底下，避免和各頁的變數撞名。
"use strict";

const UI = (() => {
  const PATHS = {
    check: "M5 12l5 5L20 7",
    x: "M6 6l12 12M18 6L6 18",
    alert: "M12 3l10 18H2zM12 10v4M12 17.5v.5",
    up: "M3 17l6-6 4 4 8-8M14 7h7v7",
    down: "M3 7l6 6 4-4 8 8M14 17h7v-7",
    flat: "M3 12h18M15 6l6 6-6 6",
    wave: "M2 12c2-7 4-7 6 0s4 7 6 0 4-7 6 0",
    bars: "M5 20v-6M10 20V9M15 20v-9M20 20V5",
    shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
    cart: "M3 4h2l2.5 11h10.5l2-7H6.5M9 20h.01M17 20h.01",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v5l3 2",
    info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5M12 8v.5",
    pause: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM10 9v6M14 9v6",
    star: "M12 3l2.8 5.8 6.2.9-4.5 4.4 1.1 6.2L12 17.4l-5.6 2.9 1.1-6.2L3 9.7l6.2-.9z",
    wallet: "M3 7h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 7l13-4v4M16 13h2",
    db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    flask: "M9 3h6M10 3v6L4 19a1.5 1.5 0 0 0 1.3 2h13.4A1.5 1.5 0 0 0 20 19l-6-10V3",
    swap: "M4 8h14l-4-4M20 16H6l4 4",
  };

  function icon(name, cls = "") {
    return `<svg class="ico ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${PATHS[name] || ""}"/></svg>`;
  }

  // 紅綠燈：state = red / yellow / green / gray
  function light(state) {
    return `<div class="light" role="img" aria-label="${{ red: "紅燈", yellow: "黃燈", green: "綠燈" }[state] || "無資料"}">` +
      ["red", "yellow", "green"].map((c) => `<i class="${c}${c === state ? " on" : ""}"></i>`).join("") + "</div>";
  }

  // 圓環：p 介於 0～1，中間寫百分比
  function ring(p, size = 56, label = "") {
    const r = size / 2 - 5, c = 2 * Math.PI * r, v = Math.max(0, Math.min(1, p || 0));
    return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img" aria-label="${label}${Math.round(v * 100)}%">` +
      `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="ring-bg"/>` +
      `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="ring-fg" stroke-dasharray="${(c * v).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>` +
      `<text x="50%" y="50%" dy=".35em" text-anchor="middle">${Math.round(v * 100)}%</text></svg>`;
  }

  // 區間條：lo～hi 的範圍上畫色塊（band）與標記（marks: {v, cls, title}）
  function bar(lo, hi, { band = null, fill = null, marks = [] } = {}) {
    const span = hi - lo || 1;
    const at = (v) => `${Math.max(0, Math.min(100, ((v - lo) / span) * 100)).toFixed(1)}%`;
    let html = '<div class="bar">';
    if (fill !== null) html += `<div class="fill ${fill.cls || ""}" style="width:${at(fill.v)}"></div>`;
    if (band) html += `<div class="band ${band.cls || ""}" style="left:${at(band.from)};width:calc(${at(band.to)} - ${at(band.from)})"></div>`;
    for (const m of marks) html += `<div class="${m.dot ? "dot" : "mark"} ${m.cls || ""}" style="left:${at(m.v)}" title="${m.title || ""}"></div>`;
    return html + "</div>";
  }

  // 標籤：kind = ok / no / up / down / warn / info
  function chip(text, kind = "", ic = "") {
    return `<span class="chip ${kind}">${ic ? icon(ic) : ""}${text}</span>`;
  }

  // 收起來的說明（點「說明」才展開）
  function help(title, items) {
    const d = document.createElement("details");
    d.className = "help";
    const s = document.createElement("summary");
    s.innerHTML = `${icon("info")} ${title}`;
    d.appendChild(s);
    const ul = document.createElement("ul");
    for (const t of items) { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); }
    d.appendChild(ul);
    return d;
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  return { icon, light, ring, bar, chip, help, esc };
})();
