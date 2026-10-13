"use strict";
/* Shared by the dashboard pages (index, security, dora). */
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, {...opts, headers: {"X-MRPilot": "1", "Content-Type": "application/json", ...(opts.headers || {})}});
  if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.data = j; throw e; }
  return j;
};
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };

function ago(ts) {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "baru saja";
  if (s < 3600) return Math.floor(s / 60) + " mnt lalu";
  if (s < 86400) return Math.floor(s / 3600) + " jam lalu";
  return Math.floor(s / 86400) + " hari lalu";
}
function dur(sec) {
  if (sec == null) return "–";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h >= 48) return Math.floor(h / 24) + " hari";
  if (h >= 1) return h + " j " + m + " m";
  return Math.max(1, m) + " mnt";
}
const clock = ts => new Date(ts * 1000).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const shortDate = d => new Date(d + "T00:00:00").toLocaleDateString("id-ID", { day: "numeric", month: "short" });
const fmt = n => n == null ? "–" : Number(n).toLocaleString("id-ID");
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

/* ================================================================ theme */
function initTheme(onChange) {
  const saved = store.get("mrp-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  const btn = $("#theme");
  if (!btn) return;
  btn.onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("mrp-theme", document.documentElement.dataset.theme);
    if (onChange) onChange();
  };
}

/* ================================================================ charts (SVG) */
function tooltip(host) {
  let t = host.querySelector(".tip");
  if (!t) { t = document.createElement("div"); t.className = "tip"; host.appendChild(t); }
  return {
    show(html, x, y) {
      t.innerHTML = html; t.style.display = "block";
      const w = t.offsetWidth, hw = host.clientWidth;
      t.style.left = Math.min(Math.max(0, x + 12), hw - w) + "px";
      t.style.top = Math.max(0, y - t.offsetHeight - 10) + "px";
    },
    hide() { t.style.display = "none"; }
  };
}
const niceMax = v => { if (v <= 4) return 4; const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p; return v; };

function lineChart(host, rows, series, opts = {}) {
  // null values leave a gap in the line (e.g. a week without deployments has no lead time)
  const W = host.clientWidth || 600, H = opts.height || 220, P = { l: 40, r: 64, t: 10, b: 26 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b, vf = opts.fmt || fmt;
  const vals = rows.flatMap(r => series.map(s => r[s.key])).filter(v => v != null);
  const max = niceMax(Math.max(1, ...vals));
  const x = i => P.l + (rows.length < 2 ? iw / 2 : i * iw / (rows.length - 1));
  const y = v => P.t + ih - v / max * ih;
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = max * k / 4, yy = y(v);
    g += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}" stroke="${k ? css("--grid") : css("--axis")}" />`;
    g += `<text x="${P.l - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="${css("--muted")}" style="font-variant-numeric:tabular-nums">${esc(vf(v))}</text>`;
  }
  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(iw / 64))));
  rows.forEach((r, i) => { if ((i % step === 0 && rows.length - 1 - i >= step * 0.6) || i === rows.length - 1) g += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle" font-size="11" fill="${css("--muted")}">${esc(shortDate(r.date))}</text>`; });
  series.forEach(s => {
    const col = css(s.color);
    let d = "", pen = false;
    rows.forEach((r, i) => { const v = r[s.key]; if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i)},${y(v)}`; pen = true; });
    g += `<path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    rows.forEach((r, i) => {  // isolated points (gap on both sides) would be invisible as a path
      const v = r[s.key], pv = i ? rows[i - 1][s.key] : null, nv = i < rows.length - 1 ? rows[i + 1][s.key] : null;
      if (v != null && pv == null && nv == null) g += `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${col}"/>`;
    });
    let li = rows.length - 1; while (li >= 0 && rows[li][s.key] == null) li--;
    if (li >= 0) {
      const lv = rows[li][s.key];
      g += `<circle cx="${x(li)}" cy="${y(lv)}" r="4" fill="${col}" stroke="${css("--surface")}" stroke-width="2"/>`;
      g += `<text x="${x(li) + 8}" y="${y(lv) + 4}" font-size="11" fill="${css("--ink-2")}">${esc(vf(lv))} ${esc(s.short || "")}</text>`;
    }
  });
  g += `<line class="xh" y1="${P.t}" y2="${P.t + ih}" stroke="${css("--line-2")}" visibility="hidden"/>`;
  const legend = series.length > 1 ? `<div class="legend">${series.map(s => `<span><i style="background:${css(s.color)}"></i>${esc(s.label)}</span>`).join("")}</div>` : "";
  host.innerHTML = legend + `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "")}">${g}<rect x="${P.l}" y="${P.t}" width="${iw}" height="${ih}" fill="transparent" class="hit"/></svg>` + tableTwin(rows, series, vf);
  const svg = host.querySelector("svg"), tip = tooltip(host), xh = svg.querySelector(".xh");
  svg.querySelector(".hit").addEventListener("mousemove", ev => {
    const rect = svg.getBoundingClientRect(), sx = (ev.clientX - rect.left) * W / rect.width;
    const i = Math.max(0, Math.min(rows.length - 1, Math.round((sx - P.l) / (iw / Math.max(1, rows.length - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    const top = Math.max(0, ...series.map(s => rows[i][s.key] ?? 0));
    tip.show(`<b>${esc(opts.dateLabel ? opts.dateLabel(rows[i].date) : shortDate(rows[i].date))}</b>` + series.map(s => `<div class="r"><i style="background:${css(s.color)}"></i>${esc(s.label)}: ${rows[i][s.key] == null ? "–" : esc(vf(rows[i][s.key]))}</div>`).join(""),
      x(i) * rect.width / W, y(top) * rect.height / H + 30);
  });
  svg.querySelector(".hit").addEventListener("mouseleave", () => { tip.hide(); xh.setAttribute("visibility", "hidden"); });
}

function stackedColumns(host, rows, series, opts = {}) {
  const W = host.clientWidth || 600, H = opts.height || 230, P = { l: 34, r: 8, t: 10, b: 26 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const totals = rows.map(r => series.reduce((a, s) => a + r[s.key], 0));
  const max = niceMax(Math.max(1, ...totals));
  const band = iw / rows.length, bw = Math.min(24, band * 0.7);
  const y = v => P.t + ih - v / max * ih;
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = max * k / 4, yy = y(v);
    g += `<line x1="${P.l}" x2="${W - P.r}" y1="${yy}" y2="${yy}" stroke="${k ? css("--grid") : css("--axis")}" />`;
    g += `<text x="${P.l - 8}" y="${yy + 4}" text-anchor="end" font-size="11" fill="${css("--muted")}" style="font-variant-numeric:tabular-nums">${fmt(v)}</text>`;
  }
  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(iw / 64))));
  rows.forEach((r, i) => {
    const cx = P.l + band * i + band / 2;
    let acc = 0;
    const visible = series.filter(s => r[s.key] > 0);
    visible.forEach((s, j) => {
      const h = r[s.key] / max * ih, top = y(acc + r[s.key]);
      const isTop = j === visible.length - 1;
      const hh = Math.max(0, h - (j > 0 ? 2 : 0));
      if (isTop && hh > 4) {
        const x0 = cx - bw / 2, rr = 4;
        g += `<path d="M${x0},${top + hh} V${top + rr} Q${x0},${top} ${x0 + rr},${top} H${x0 + bw - rr} Q${x0 + bw},${top} ${x0 + bw},${top + rr} V${top + hh} Z" fill="${css(s.color)}"/>`;
      } else {
        g += `<rect x="${cx - bw / 2}" y="${top}" width="${bw}" height="${hh}" fill="${css(s.color)}"/>`;
      }
      acc += r[s.key];
    });
    if ((i % step === 0 && rows.length - 1 - i >= step * 0.6) || i === rows.length - 1) g += `<text x="${cx}" y="${H - 6}" text-anchor="middle" font-size="11" fill="${css("--muted")}">${esc(shortDate(r.date))}</text>`;
    g += `<rect x="${P.l + band * i}" y="${P.t}" width="${band}" height="${ih}" fill="transparent" data-i="${i}" class="hit"/>`;
  });
  host.innerHTML = (series.length > 1 ? `<div class="legend">${series.map(s => `<span><i class="sq" style="background:${css(s.color)}"></i>${esc(s.label)}</span>`).join("")}</div>` : "") + `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "")}">${g}</svg>` + tableTwin(rows, series);
  const svg = host.querySelector("svg"), tip = tooltip(host);
  svg.querySelectorAll(".hit").forEach(h => {
    h.addEventListener("mouseenter", () => {
      const i = +h.dataset.i, rect = svg.getBoundingClientRect();
      tip.show(`<b>${esc(shortDate(rows[i].date))}</b>` + series.map(s => `<div class="r"><i style="background:${css(s.color)}"></i>${esc(s.label)}: ${fmt(rows[i][s.key])}</div>`).join("")
        + (series.length > 1 ? `<div class="r">Total: ${fmt(totals[i])}</div>` : ""), (P.l + band * i + band / 2) * rect.width / W, y(totals[i]) * rect.height / H + 30);
    });
    h.addEventListener("mouseleave", () => tip.hide());
  });
}

function hbars(host, items, opts = {}) {
  if (!items.length) { host.innerHTML = `<div class="empty">Belum ada data.</div>`; return; }
  const max = Math.max(...items.map(i => i.value));
  host.innerHTML = `<div style="display:grid;gap:10px">` + items.map(it => `
    <div title="${esc(it.title || it.label)}">
      <div style="display:flex;justify-content:space-between;gap:10px;font-size:13px">
        <span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(it.label)}</span>
        <span style="font-variant-numeric:tabular-nums;color:var(--ink-2)">${fmt(it.value)}${it.extra ? " · " + esc(it.extra) : ""}</span>
      </div>
      <div style="height:8px;border-radius:0 4px 4px 0;background:var(--s1);width:${Math.max(2, it.value / max * 100)}%;margin-top:4px"></div>
    </div>`).join("") + `</div>`;
  void opts;
}

function tableTwin(rows, series, vf = fmt) {
  return `<details class="tbl"><summary>Lihat sebagai tabel</summary><div class="tscroll"><table><thead><tr><th>Tanggal</th>${series.map(s => `<th class="num">${esc(s.label)}</th>`).join("")}</tr></thead><tbody>`
    + rows.map(r => `<tr><td>${esc(shortDate(r.date))}</td>${series.map(s => `<td class="num">${r[s.key] == null ? "–" : esc(vf(r[s.key]))}</td>`).join("")}</tr>`).join("") + `</tbody></table></div></details>`;
}
