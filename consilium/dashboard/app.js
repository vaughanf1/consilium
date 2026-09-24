/* Consilium dashboard — vanilla JS, hand-drawn SVG. No build step, no CDN. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    return r.json();
  };
  const fmt = {
    pct: (v, d = 1) => v == null ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`,
    pct0: (v, d = 1) => v == null ? "–" : `${(v * 100).toFixed(d)}%`,
    money: (v) => v == null ? "–" : (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${Math.round(v).toLocaleString()}`),
    num: (v, d = 2) => v == null ? "–" : Number(v).toFixed(d),
    signed: (v, d = 2) => v == null ? "–" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}`,
    date: (s) => s,
  };
  const cls = (v) => v > 0 ? "gain" : v < 0 ? "loss" : "";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---------- theme ----------
  const root = document.documentElement;
  try { const t = localStorage.getItem("consilium-theme"); if (t) root.dataset.theme = t; } catch {}
  $("#theme-toggle").onclick = () => {
    const dark = root.dataset.theme !== "light";   // dark is the default; light is an explicit choice
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("consilium-theme", root.dataset.theme); } catch {}
    render();  // charts read CSS vars via classes, but table/legend text may need refresh
  };

  // ---------- nav ----------
  $$(".nav button").forEach((b) => b.onclick = () => showView(b.dataset.view));
  function showView(name) {
    $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    try { localStorage.setItem("consilium-view", name); } catch {}
    if (name === "paper") loadPaper();
    if (name === "mandates") loadMandates();
  }

  // ---------- state ----------
  const state = { meta: null, funds: [], result: null, records: [], cycleIdx: 0, source: "", paper: null, mandate: null };

  // =====================================================================
  // Charts
  // =====================================================================
  function makeSvg(w, h) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.height = `${h}px`;
    return svg;
  }
  const el = (tag, attrs = {}, text) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };
  function niceTicks(lo, hi, n = 4) {
    if (!(hi > lo)) { hi = lo + 1; }
    const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n + 1) || mag * 10;
    const ticks = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(v);
    return ticks;
  }

  /** Generic time-series chart: lines, optional areas/bands, crosshair tooltip, table twin. */
  function lineChart(container, opts) {
    const { dates, series, height = 260, format = fmt.money, baseline = null, bands = null, regime = null, actionsEl = null, legendEl = null, pad = { l: 58, r: 46, t: 12, b: 26 } } = opts;
    container.innerHTML = "";
    if (!dates || !dates.length) { container.innerHTML = `<div class="empty">no data yet</div>`; return; }
    const w = Math.max(320, container.clientWidth || 640), h = height;
    const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
    const all = [];
    series.forEach((s) => s.values.forEach((v) => v != null && all.push(v)));
    if (bands) Object.values(bands).forEach((arr) => arr.forEach((v) => all.push(v)));
    if (baseline != null) all.push(baseline);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi === lo) { hi += 1; lo -= 1; }
    const padY = (hi - lo) * 0.06; lo -= padY; hi += padY;
    const x = (i) => pad.l + (dates.length === 1 ? plotW / 2 : (i / (dates.length - 1)) * plotW);
    const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * plotH;
    const svg = makeSvg(w, h);

    // regime strip behind everything
    if (regime) {
      let i = 0;
      while (i < regime.length) {
        let j = i; while (j + 1 < regime.length && regime[j + 1] === regime[i]) j++;
        const x0 = i === 0 ? pad.l : (x(i - 1) + x(i)) / 2, x1 = j === regime.length - 1 ? pad.l + plotW : (x(j) + x(j + 1)) / 2;
        svg.appendChild(el("rect", { x: x0, y: pad.t, width: Math.max(0, x1 - x0), height: plotH, class: `regime ${regime[i]}` }));
        i = j + 1;
      }
    }
    // grid + y ticks
    niceTicks(lo, hi).forEach((t) => {
      svg.appendChild(el("line", { x1: pad.l, x2: pad.l + plotW, y1: y(t), y2: y(t), class: "grid-line" }));
      svg.appendChild(el("text", { x: pad.l - 8, y: y(t) + 4, "text-anchor": "end", class: "tick" }, format(t)));
    });
    // x ticks
    const nx = Math.max(2, Math.min(6, Math.floor(plotW / 95), dates.length));
    for (let k = 0; k < nx; k++) {
      const i = Math.round((k / Math.max(1, nx - 1)) * (dates.length - 1));
      svg.appendChild(el("text", { x: x(i), y: h - 6, "text-anchor": k === 0 ? "start" : k === nx - 1 ? "end" : "middle", class: "tick" }, dates[i].slice(0, 7)));
    }
    svg.appendChild(el("line", { x1: pad.l, x2: pad.l + plotW, y1: pad.t + plotH, y2: pad.t + plotH, class: "axis" }));
    if (baseline != null) svg.appendChild(el("line", { x1: pad.l, x2: pad.l + plotW, y1: y(baseline), y2: y(baseline), class: "axis" }));

    const path = (vals) => vals.map((v, i) => v == null ? null : `${i === 0 || vals[i - 1] == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).filter(Boolean).join(" ");
    // bands (monte carlo)
    if (bands) {
      const area = (upper, lower) => `${path(upper)} ${lower.map((v, i) => `L${x(lower.length - 1 - i).toFixed(1)},${y(lower[lower.length - 1 - i]).toFixed(1)}`).join(" ")} Z`;
      svg.appendChild(el("path", { d: area(bands.p95, bands.p5), class: "band" }));
      svg.appendChild(el("path", { d: area(bands.p75, bands.p25), class: "band inner" }));
    }
    // areas
    series.forEach((s) => {
      if (!s.area) return;
      const base = baseline != null ? baseline : lo;
      const d = `${path(s.values)} L${x(s.values.length - 1).toFixed(1)},${y(base).toFixed(1)} L${x(0).toFixed(1)},${y(base).toFixed(1)} Z`;
      svg.appendChild(el("path", { d, class: `area ${s.area}` }));
    });
    // lines
    series.forEach((s) => svg.appendChild(el("path", { d: path(s.values), class: `series ${s.cls}` })));
    // direct end labels (selective: last value only)
    series.forEach((s, k) => {
      const last = [...s.values].reverse().find((v) => v != null);
      if (last == null) return;
      const yy = y(last) + 4 + (k === 1 && series[0] && Math.abs(y(series[0].values.at(-1)) - y(last)) < 12 ? 12 : 0);
      svg.appendChild(el("text", { x: pad.l + plotW + 6, y: yy, class: "end-label" }, s.short || s.name));
    });

    // crosshair + tooltip
    const cross = el("line", { x1: 0, x2: 0, y1: pad.t, y2: pad.t + plotH, class: "crosshair", visibility: "hidden" });
    svg.appendChild(cross);
    const markers = series.map((s) => { const m = el("circle", { r: 4, class: "marker", visibility: "hidden" }); m.style.stroke = `var(--${s.cls === "fund" ? "fund" : s.cls === "bench" ? "bench" : s.cls})`; svg.appendChild(m); return m; });
    const tip = document.createElement("div"); tip.className = "tooltip";
    container.appendChild(svg); container.appendChild(tip);
    const onMove = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX ?? (ev.touches && ev.touches[0].clientX)) - rect.left) * (w / rect.width);
      const i = Math.max(0, Math.min(dates.length - 1, Math.round(((px - pad.l) / plotW) * (dates.length - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      let html = `<b>${dates[i]}</b>`;
      series.forEach((s, k) => {
        const v = s.values[i];
        if (v == null) { markers[k].setAttribute("visibility", "hidden"); return; }
        markers[k].setAttribute("cx", x(i)); markers[k].setAttribute("cy", y(v)); markers[k].setAttribute("visibility", "visible");
        html += `<br><span class="sw" style="background:var(--${s.cls})"></span>${s.name} ${format(v)}`;
      });
      if (regime) html += `<br><span class="muted">regime: ${regime[i]}</span>`;
      tip.innerHTML = html;
      tip.style.left = `${(x(i) / w) * rect.width}px`; tip.style.top = `${(pad.t / h) * rect.height}px`; tip.style.opacity = 1;
    };
    const onLeave = () => { cross.setAttribute("visibility", "hidden"); markers.forEach((m) => m.setAttribute("visibility", "hidden")); tip.style.opacity = 0; };
    svg.addEventListener("mousemove", onMove); svg.addEventListener("touchmove", onMove, { passive: true });
    svg.addEventListener("mouseleave", onLeave); svg.addEventListener("touchend", onLeave);

    if (legendEl) legendEl.innerHTML = series.map((s) => `<span><span class="sw" style="background:var(--${s.cls})"></span>${esc(s.name)}</span>`).join("") + (regime ? `<span><span class="sw" style="background:var(--gain);opacity:.35"></span>risk-on</span><span><span class="sw" style="background:var(--loss);opacity:.35"></span>risk-off</span>` : "");
    if (actionsEl) tableToggle(actionsEl, container, () => {
      const head = `<tr><th>date</th>${series.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}${regime ? "<th>regime</th>" : ""}</tr>`;
      const rows = dates.map((d, i) => `<tr><td class="mono">${d}</td>${series.map((s) => `<td class="num">${s.values[i] == null ? "–" : format(s.values[i])}</td>`).join("")}${regime ? `<td>${regime[i]}</td>` : ""}</tr>`).join("");
      return `<div class="table-view"><table>${head}${rows}</table></div>`;
    }, () => lineChart(container, opts));
  }

  function tableToggle(actionsEl, container, tableHtml, redraw) {
    actionsEl.innerHTML = `<button class="active" data-m="chart">chart</button><button data-m="table">table</button>`;
    $$("button", actionsEl).forEach((b) => b.onclick = () => {
      $$("button", actionsEl).forEach((x) => x.classList.toggle("active", x === b));
      if (b.dataset.m === "table") container.innerHTML = tableHtml(); else redraw();
    });
  }

  /** Horizontal bar chart for sector exposure with a cap line. */
  function barChart(container, items, cap) {
    container.innerHTML = "";
    if (!items.length) { container.innerHTML = `<div class="empty">flat</div>`; return; }
    const w = Math.max(320, container.clientWidth || 500), rowH = 26, pad = { l: 110, r: 50, t: 18, b: 22 };
    const h = pad.t + pad.b + rowH * items.length;
    const max = Math.max(cap || 0, ...items.map((i) => i.value)) * 1.1 || 1;
    const x = (v) => pad.l + (v / max) * (w - pad.l - pad.r);
    const svg = makeSvg(w, h);
    niceTicks(0, max, 4).forEach((t) => { svg.appendChild(el("line", { x1: x(t), x2: x(t), y1: pad.t, y2: h - pad.b, class: "grid-line" })); svg.appendChild(el("text", { x: x(t), y: h - 6, "text-anchor": "middle", class: "tick" }, fmt.pct0(t, 0))); });
    items.forEach((it, i) => {
      const yy = pad.t + i * rowH;
      svg.appendChild(el("text", { x: pad.l - 8, y: yy + rowH / 2 + 4, "text-anchor": "end", class: "tick" }, it.label));
      const r = el("rect", { x: pad.l, y: yy + 5, width: Math.max(0, x(it.value) - pad.l), height: rowH - 10, rx: 3 });
      r.style.fill = it.value > (cap || 1) + 1e-9 ? "var(--loss)" : "var(--fund)";
      svg.appendChild(r);
      svg.appendChild(el("text", { x: x(it.value) + 6, y: yy + rowH / 2 + 4, class: "tick" }, fmt.pct0(it.value, 0)));
    });
    if (cap) { const c = el("line", { x1: x(cap), x2: x(cap), y1: pad.t, y2: h - pad.b, class: "axis" }); c.style.stroke = "var(--accent)"; svg.appendChild(c); svg.appendChild(el("text", { x: x(cap), y: 11, "text-anchor": "middle", class: "tick" }, "cap")); }
    svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
    container.appendChild(svg);
  }

  // =====================================================================
  // Council — the live view: stages narrate, votes land one at a time
  // =====================================================================
  const STAGES = [["data", "Market data"], ["analysts", "Analysts"], ["red_team", "Red Team"],
                  ["cio", "CIO"], ["board", "The Council"]];

  function paintBudget(b) {
    const pill = $("#budget-pill");
    if (!b || !b.enabled || !b.measured) { pill.classList.add("hidden"); return; }
    pill.classList.remove("hidden");
    const out = !b.allow_llm;
    pill.innerHTML = `<span class="dot"></span>demo budget ${out ? "spent" : `$${b.spent_usd.toFixed(2)} / $${b.cap_usd.toFixed(2)}`}`;
    pill.style.borderColor = out ? "var(--warn)" : "";
    pill.style.color = out ? "var(--warn)" : "";
    return out;
  }

  function budgetNotice(b) {
    if (!b || b.allow_llm !== false) return "";
    return `<div class="callout warn" style="margin-bottom:12px">Today's demo budget is spent, so the council is sitting on its
      <b>rule-based fallback</b> instead of paid models — every seat still votes, and the numbers are still real.
      It resets at midnight UTC. Run it locally with your own key for the full multi-model council.</div>`;
  }

  function paintStages(state) {
    $("#stagebar").innerHTML = STAGES.map(([k, label]) =>
      `<div class="st ${state[k] || ""}" data-st="${k}"><span class="dot"></span>${label}</div>`).join("");
  }
  paintStages({});

  function seatCard(seat, vote) {
    if (!vote) {
      return `<div class="seat waiting" data-seat="${esc(seat.name)}">
        <div class="name">${esc(seat.display)}</div><span class="vendor">${esc(seat.model || "rules")}</span>
        <div class="thinking">considering the book…</div></div>`;
    }
    const s = vote.stance === "cut risk" ? "bearish" : vote.stance === "add risk" ? "bullish" : "neutral";
    return `<div class="seat landed ${vote.dissent ? "dissent" : ""}" data-seat="${esc(vote.member)}">
      <div class="name">${esc(vote.display)}</div>
      <span class="vendor">${esc(vote.fallback_reason ? `${vote.model || "model"} unreachable · rules` : (vote.model || "rules"))}</span>
      <div class="pct">${fmt.pct0(vote.exposure_vote, 0)}</div>
      <div class="gauge"><i style="width:${Math.round(vote.exposure_vote * 100)}%"></i></div>
      <span class="stance ${s}">${esc(vote.stance)}</span>
      <div class="says" style="margin-top:8px">${esc(vote.concern)}</div>
      ${vote.trim && vote.trim.length ? `<div style="margin-top:7px">${vote.trim.map((t) => `<span class="tag">trim ${esc(t)}</span>`).join("")}</div>` : ""}
    </div>`;
  }

  $("#btn-convene").onclick = async () => {
    const body = { fund: $("#c-fund").value, tickers: $("#c-tickers").value, date: $("#c-date").value || null,
                   provider: $("#c-provider").value, fresh: true };
    const btn = $("#btn-convene");
    btn.disabled = true; btn.textContent = "In session…";
    $("#provider-pill").textContent = `data: ${body.provider}`;
    const state = {}; paintStages(state);
    $("#council-resolution").innerHTML = ""; $("#seats").innerHTML = "";
    $("#brief-body").innerHTML = `<div class="empty">Pricing the universe…</div>`;
    $("#brief-pill").textContent = "in session";
    $("#council-book").innerHTML = `<div class="empty">The book appears once the council has voted.</div>`;
    let seats = [], votes = {};

    const handle = (m) => {
      if (m.type === "convened") {
        paintBudget(m.budget);
        const n = budgetNotice(m.budget);
        if (n) $("#brief-body").innerHTML = n;
      } else if (m.type === "stage") {
        state[m.name] = m.state; paintStages(state);
        const d = m.detail || {};
        if (m.name === "analysts" && m.state === "done") {
          $("#brief-body").innerHTML = `<div class="kv"><dt>views formed</dt><dd>${d.views}${d.abstained ? ` <span class="muted">(${d.abstained} abstained)</span>` : ""}</dd></div>`;
        }
        if (m.name === "red_team" && m.state === "done" && (d.notes || []).length) {
          $("#brief-body").innerHTML += `<div class="redstrip"><b>Red Team</b>${d.notes.map((n) =>
            `<div class="rt" title="${esc(n.strongest_objection)}"><span class="mono">${esc(n.ticker)}</span>
              <span class="${n.haircut >= .4 ? "loss" : n.haircut >= .15 ? "warn" : "gain"}">−${fmt.pct0(n.haircut, 0)}</span>
              <span class="why">${esc(n.strongest_objection)}</span></div>`).join("")}</div>`;
        }
        if (m.name === "cio" && m.state === "done") {
          $("#brief-body").innerHTML += `<div class="memo" style="margin-top:10px">${esc(d.memo)}<div class="meta">regime <b>${esc(d.regime)}</b> · proposes deploying ${fmt.pct0(d.exposure, 0)} · ${esc(d.source)} chair</div></div>`;
        }
        if (m.name === "board" && m.state === "running") {
          seats = d.seats || []; $("#seats").innerHTML = seats.map((x) => seatCard(x, null)).join("");
        }
        if (m.name === "board" && m.state === "done") {
          const r = d.resolution; if (!r) return;
          const counts = {}; r.votes.forEach((v) => counts[v.stance] = (counts[v.stance] || 0) + 1);
          const dir = r.stance === "cut risk" ? "loss" : r.stance === "add risk" ? "gain" : "";
          $("#council-resolution").innerHTML = `<div class="verdictbar">
            <div><div class="cap">COUNCIL RESOLUTION</div><div class="headline ${dir}">${fmt.pct0(r.exposure, 0)} deployed</div>
              <div class="cap">CIO proposed ${fmt.pct0(r.cio_proposal, 0)}</div></div>
            <div class="tally">${Object.entries(counts).map(([k, n]) =>
              `<span class="${k === "cut risk" ? "loss" : k === "add risk" ? "gain" : "muted"}">${n} × ${esc(k)}</span>`).join("")}</div>
            <div class="text">${esc(r.summary)}</div></div>`;
          // re-render with dissent flags now that the median is known
          $("#seats").innerHTML = seats.map((x) => seatCard(x, r.votes.find((v) => v.member === x.name) || votes[x.name])).join("");
        }
      } else if (m.type === "vote") {
        votes[m.vote.member] = m.vote;
        const card = $(`.seat[data-seat="${m.vote.member}"]`);
        if (card) card.outerHTML = seatCard({ name: m.vote.member, display: m.vote.display, model: m.vote.model }, m.vote);
      } else if (m.type === "record") {
        const rec = m.record;
        state.board = "done"; paintStages(state);
        $("#brief-pill").textContent = `NAV ${fmt.money(rec.nav)}`;
        const rows = Object.keys(rec.final_weights).filter((t) => Math.abs(rec.final_weights[t]) > 1e-6).sort((a, b) => Math.abs(rec.final_weights[b]) - Math.abs(rec.final_weights[a]));
        $("#council-book").innerHTML = rows.length ? `<table><tr><th>ticker</th><th>sector</th><th class="num">conviction</th><th class="num">weight</th><th class="num">shares</th></tr>` +
          rows.map((t) => `<tr><td class="mono">${esc(t)}</td><td class="muted">${esc(rec.sectors[t] || "")}</td><td class="num ${cls(rec.verdict.convictions[t])}">${fmt.signed(rec.verdict.convictions[t])}</td><td class="num">${fmt.pct(rec.final_weights[t])}</td><td class="num">${rec.positions[t] || 0}</td></tr>`).join("") + `</table>` : `<div class="empty">The council deployed nothing this cycle.</div>`;
        // the Committee / Risk tabs can read this meeting straight away
        state.records = [rec]; state.cycleIdx = 0; state.source = "council";
        $("#committee-empty").classList.add("hidden"); $("#committee-body").classList.remove("hidden");
        $("#risk-empty").classList.add("hidden"); $("#risk-body").classList.remove("hidden");
        renderCommittee(); renderRisk();
      } else if (m.type === "error") { throw new Error(m.error); }
    };

    try {
      const resp = await fetch("/api/council/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!resp.ok) { let msg = resp.statusText; try { msg = (await resp.json()).detail || msg; } catch {} throw new Error(msg); }
      const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl; while ((nl = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (line) handle(JSON.parse(line)); }
      }
      if (buf.trim()) handle(JSON.parse(buf));
    } catch (e) {
      $("#brief-body").innerHTML = `<div class="callout err">${esc(e.message)}</div>`;
      $("#brief-pill").textContent = "failed";
    }
    btn.disabled = false; btn.textContent = "Convene the Council";
  };

  // =====================================================================
  // Research
  // =====================================================================
  let btJob = null;
  $("#btn-backtest").onclick = async () => {
    const body = { fund: $("#f-fund").value, tickers: $("#f-tickers").value, start: $("#f-start").value || null, end: $("#f-end").value || null,
                   provider: $("#f-provider").value, model: $("#f-model").value || null };
    $("#btn-backtest").disabled = true; $("#bt-progress").classList.remove("hidden"); $("#bt-warning").classList.add("hidden");
    $("#bt-status").textContent = "convening…";
    $("#provider-pill").textContent = `data: ${body.provider}`;
    try { await streamBacktest(body); }
    catch (e) { $("#bt-status").innerHTML = `<span class="loss">${esc(e.message)}</span>`; $("#bt-progress").classList.add("hidden"); }
    $("#btn-backtest").disabled = false;
  };
  /** One request; NDJSON progress lines draw the curve as each meeting concludes; the full result arrives last. */
  async function streamBacktest(body) {
    const resp = await fetch("/api/backtest/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!resp.ok) { let m = resp.statusText; try { m = (await resp.json()).detail || m; } catch {} throw new Error(m); }
    const live = { dates: [], nav: [], benchmark_nav: [], regime: [] };
    let capital = null, lastDraw = 0;
    const handle = (msg) => {
      if (msg.type === "progress") {
        live.dates.push(msg.date); live.nav.push(msg.nav); live.benchmark_nav.push(msg.benchmark_nav); live.regime.push(msg.regime);
        if (capital == null) capital = msg.benchmark_nav;
        $("#bt-progress > i").style.width = `${Math.round((msg.i / msg.n) * 100)}%`;
        $("#bt-status").textContent = `${msg.date} · meeting ${msg.i}/${msg.n} · NAV $${Math.round(msg.nav).toLocaleString()} · ${msg.regime}`;
        const now = performance.now();
        if (now - lastDraw > 120 || msg.i === msg.n) { lastDraw = now; $("#research-empty").classList.add("hidden"); $("#research-body").classList.remove("hidden"); drawEquity(live.dates, live.nav, live.benchmark_nav, live.regime, capital); }
      } else if (msg.type === "result") { $("#bt-progress").classList.add("hidden"); setResult(msg.result, "backtest"); }
      else if (msg.type === "error") { throw new Error(msg.error); }
    };
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl; while ((nl = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (line) handle(JSON.parse(line)); }
    }
    if (buf.trim()) handle(JSON.parse(buf));
  }
  async function pollBacktest() {
    if (!btJob) return;
    let j;
    try { j = await api(`/api/jobs/${btJob.id}?result=${btJob.status === "done" ? "true" : "false"}`); } catch (e) { $("#bt-status").textContent = e.message; return; }
    $("#bt-progress > i").style.width = `${Math.round(j.progress * 100)}%`;
    $("#bt-status").textContent = j.message || j.status;
    if (j.status === "running" && j.live && j.live.dates && j.live.dates.length) {
      $("#research-empty").classList.add("hidden"); $("#research-body").classList.remove("hidden");
      drawEquity(j.live.dates, j.live.nav, j.live.benchmark_nav, j.live.regime, state.result ? state.result.capital : j.live.nav[0]);
    }
    if (j.status === "done") {
      const full = j.result ? j : await api(`/api/jobs/${btJob.id}?result=true`);
      $("#btn-backtest").disabled = false; $("#bt-progress").classList.add("hidden");
      setResult(full.result, "backtest");
      return;
    }
    if (j.status === "failed") {
      $("#btn-backtest").disabled = false; $("#bt-progress").classList.add("hidden");
      $("#bt-status").innerHTML = `<span class="loss">${esc(j.error)}</span>`;
      $("#bt-warning").classList.remove("hidden"); $("#bt-warning").classList.add("err"); $("#bt-warning").textContent = j.message;
      return;
    }
    btJob.status = j.status;
    setTimeout(pollBacktest, 500);
  }

  function setResult(res, source) {
    state.result = res; state.records = res.records; state.source = source; state.cycleIdx = res.records.length - 1;
    $("#research-empty").classList.add("hidden"); $("#research-body").classList.remove("hidden");
    $("#committee-empty").classList.add("hidden"); $("#committee-body").classList.remove("hidden");
    $("#risk-empty").classList.add("hidden"); $("#risk-body").classList.remove("hidden");
    const w = $("#bt-warning"); w.classList.remove("err");
    if (res.point_in_time_warning) { w.textContent = `⚠ ${res.point_in_time_warning}`; w.classList.remove("hidden"); } else w.classList.add("hidden");
    const staffedLLM = res.records[0] && res.records[0].strategies.some((s) => s.views.some((v) => (state.meta?.analysts || []).find((a) => a.name === v.analyst)?.kind === "llm"));
    $("#bt-status").innerHTML = `${res.fund} · ${res.start} → ${res.end} · ${res.metrics.n_periods} meetings · ${res.provider}` +
      (staffedLLM && !res.llm_used ? ` · <span class="warn">LLM personas abstained (no key configured) — committee ran on quant analysts + rules</span>` : res.llm_used ? ` · model ${esc(res.model || state.meta?.current_model || "")}` : "");
    render();
  }

  function render() {
    if (!state.result) return;
    renderResearch(); renderCommittee(); renderRisk();
  }

  function drawEquity(dates, nav, bnav, regime, capital) {
    lineChart($("#eq-chart"), {
      dates, series: [{ name: "Fund NAV", short: "Fund", cls: "fund", values: nav, area: "fund" }, { name: `Benchmark`, short: "Bench", cls: "bench", values: bnav }],
      baseline: capital, regime, height: 300, actionsEl: $("#eq-actions"), legendEl: $("#eq-legend"),
    });
  }

  function tile(label, value, delta, c = "") {
    return `<div class="tile"><div class="label">${label}</div><div class="value ${c}">${value}</div>${delta ? `<div class="delta">${delta}</div>` : ""}</div>`;
  }

  function renderResearch() {
    const r = state.result, m = r.metrics;
    $("#tiles").innerHTML = [
      tile("Total return", fmt.pct(m.total_return), `${r.benchmark} ${fmt.pct(m.benchmark_return)} · excess ${fmt.pct(m.excess_return)}`, cls(m.total_return)),
      tile("CAGR", fmt.pct(m.cagr), `${m.years.toFixed(1)} yrs · vol ${fmt.pct0(m.volatility)}`, cls(m.cagr)),
      tile("Sharpe", fmt.num(m.sharpe), `Sortino ${fmt.num(m.sortino)} · Calmar ${fmt.num(m.calmar)}`),
      tile("Max drawdown", fmt.pct0(-m.max_drawdown), `${m.max_drawdown_days} periods · ${r.benchmark} ${fmt.pct0(-m.benchmark_max_drawdown)}`, "loss"),
      tile("Alpha / beta", `${fmt.pct(m.alpha)}`, `β ${fmt.num(m.beta)} · IR ${fmt.num(m.information_ratio)}`, cls(m.alpha)),
      tile("Costs", fmt.money(m.total_costs), `${fmt.pct0(m.cost_drag, 2)} of capital · ${m.n_orders} orders`),
      tile("Hit rate", fmt.pct0(m.hit_rate, 0), `best ${fmt.pct(m.best_period)} · worst ${fmt.pct(m.worst_period)}`),
    ].join("");
    $("#eq-sub").textContent = `${fmt.money(r.capital)} starting capital · ${r.rebalance} meetings · costs ${r.records[0] ? "modelled" : ""} · shaded strips are the CIO's regime call`;
    drawEquity(r.dates, r.nav, r.benchmark_nav, r.regime, r.capital);

    // walk-forward
    const wf = (a, b) => a && b ? `<table><tr><th></th><th class="num">in-sample</th><th class="num">out-of-sample</th></tr>
      <tr><td>return</td><td class="num ${cls(a.total_return)}">${fmt.pct(a.total_return)}</td><td class="num ${cls(b.total_return)}">${fmt.pct(b.total_return)}</td></tr>
      <tr><td>Sharpe</td><td class="num">${fmt.num(a.sharpe)}</td><td class="num">${fmt.num(b.sharpe)}</td></tr>
      <tr><td>max drawdown</td><td class="num">${fmt.pct0(-a.max_drawdown)}</td><td class="num">${fmt.pct0(-b.max_drawdown)}</td></tr>
      <tr><td>beta</td><td class="num">${fmt.num(a.beta)}</td><td class="num">${fmt.num(b.beta)}</td></tr>
      <tr><td>excess vs ${r.benchmark}</td><td class="num ${cls(a.excess_return)}">${fmt.pct(a.excess_return)}</td><td class="num ${cls(b.excess_return)}">${fmt.pct(b.excess_return)}</td></tr></table>
      <div class="callout" style="margin-top:10px">${b.sharpe < a.sharpe * 0.5 ? "Out-of-sample decays sharply versus in-sample. Treat the headline numbers as optimistic." : b.sharpe >= a.sharpe ? "Out-of-sample holds up or improves — a good sign, not a proof." : "Some decay out-of-sample, within what a real strategy usually shows."}</div>`
      : `<div class="empty">window too short to split</div>`;
    $("#wf-table").innerHTML = wf(r.in_sample, r.out_of_sample);

    lineChart($("#dd-chart"), { dates: r.dates, series: [{ name: "Drawdown", short: "DD", cls: "bench", values: r.drawdown, area: "loss" }], baseline: 0, height: 180, format: (v) => fmt.pct0(v, 0), actionsEl: $("#dd-actions") });
    lineChart($("#ex-chart"), { dates: r.dates, series: [{ name: "Gross", cls: "fund", values: r.gross_exposure }, { name: "Net", cls: "s3", values: r.net_exposure }], baseline: 0, height: 180, format: (v) => fmt.pct0(v, 0), actionsEl: $("#ex-actions"), legendEl: $("#ex-legend") });
    lineChart($("#rs-chart"), { dates: r.dates, series: [{ name: "Rolling Sharpe", short: "Sharpe", cls: "fund", values: r.rolling_sharpe }], baseline: 0, height: 180, format: (v) => fmt.num(v, 1), actionsEl: $("#rs-actions") });

    const mc = r.monte_carlo;
    if (mc && mc.paths) {
      $("#mc-sub").textContent = `${mc.paths} bootstrapped paths of the realized returns. P(ending below capital) ${fmt.pct0(mc.prob_loss, 0)} · median max drawdown ${fmt.pct0(mc.median_max_drawdown)} · terminal p5 ${fmt.money(mc.terminal.p5)} / p50 ${fmt.money(mc.terminal.p50)} / p95 ${fmt.money(mc.terminal.p95)}`;
      lineChart($("#mc-chart"), { dates: r.dates, series: [{ name: "Median path", short: "p50", cls: "fund", values: mc.bands.p50 }, { name: "Realized", cls: "bench", values: r.nav }], bands: mc.bands, baseline: r.capital, height: 240, actionsEl: $("#mc-actions"), legendEl: $("#mc-legend") });
      $("#mc-legend").innerHTML += `<span><span class="sw" style="background:var(--fund);opacity:.25;height:10px"></span>p5–p95</span><span><span class="sw" style="background:var(--fund);opacity:.45;height:10px"></span>p25–p75</span>`;
    } else { $("#mc-sub").textContent = "not enough periods"; $("#mc-chart").innerHTML = ""; }

    const rows = Object.entries(m).map(([k, v]) => {
      const signedPct = /return|cagr|alpha|period/;
      const plainPct = /volatility|drawdown|hit_rate|exposure|turnover|tracking|drag/;
      const val = k === "total_costs" ? fmt.money(v) : /^n_|days|years/.test(k) ? String(v) : signedPct.test(k) ? fmt.pct(v) : plainPct.test(k) ? fmt.pct0(v) : fmt.num(v);
      return `<tr><td>${k.replaceAll("_", " ")}</td><td class="num">${val}</td></tr>`;
    }).join("");
    $("#metrics-table").innerHTML = `<div class="table-view"><table>${rows}</table></div>`;
  }

  // =====================================================================
  // Committee
  // =====================================================================
  const scrub = $("#scrub");
  scrub.oninput = () => { state.cycleIdx = +scrub.value; renderCommittee(); };
  $("#scrub-prev").onclick = () => { state.cycleIdx = Math.max(0, state.cycleIdx - 1); renderCommittee(); };
  $("#scrub-next").onclick = () => { state.cycleIdx = Math.min(state.records.length - 1, state.cycleIdx + 1); renderCommittee(); };

  function analystDisplay(name) { return (state.meta?.analysts || []).find((a) => a.name === name)?.display || name; }

  function renderCommittee() {
    const recs = state.records; if (!recs.length) return;
    scrub.max = recs.length - 1; scrub.value = state.cycleIdx;
    const rec = recs[state.cycleIdx], v = rec.verdict, cio = v.cio;
    $("#scrub-date").textContent = `${rec.as_of}  (${state.cycleIdx + 1}/${recs.length})`;
    $("#cio-title").textContent = `CIO memo — ${rec.as_of}`;
    $("#cio-source").textContent = cio.source === "llm" ? "LLM chair" : "rules chair";
    const expo = v.exposure_multiplier != null ? v.exposure_multiplier : cio.exposure_multiplier;
    $("#cio-memo").innerHTML = `${esc(cio.memo)}<div class="meta">regime <b>${cio.regime}</b> · exposure ×${expo.toFixed(2)}${v.board ? ` <span class="muted">(CIO asked ×${cio.exposure_multiplier.toFixed(2)})</span>` : ""} · NAV ${fmt.money(rec.nav)} · drawdown ${fmt.pct0(rec.drawdown)} · ${rec.orders.length} orders · costs ${fmt.money(rec.costs)}</div>`;
    $("#cio-kv").innerHTML = Object.keys(cio.overrides || {}).length ? `<dt>overrides</dt><dd>${Object.entries(cio.overrides).map(([t, why]) => `<div><b>${t}</b> — ${esc(why)}</div>`).join("")}</dd>` : "";
    renderBoard(v);
    $("#redteam").innerHTML = v.red_team.length ? v.red_team.map((n) => `<div class="note"><div class="head"><span>${n.ticker} <span class="muted" style="font-weight:400">consensus ${fmt.signed(n.consensus)}</span></span><span class="${n.haircut >= 0.4 ? "loss" : n.haircut >= 0.15 ? "warn" : "gain"}">haircut ${fmt.pct0(n.haircut, 0)}</span></div><div class="body">${esc(n.strongest_objection)}${n.what_would_change_my_mind ? `<div class="muted" style="margin-top:4px">would change my mind: ${esc(n.what_would_change_my_mind)}</div>` : ""}</div></div>`).join("") : `<div class="empty">no positions strong enough to attack</div>`;

    // analyst views grouped by pod
    let html = "";
    rec.strategies.forEach((s) => {
      html += `<h3 style="margin-top:12px">${esc(s.name)} <span class="muted" style="font-weight:400">· ${fmt.pct0(s.slice, 0)} of capital</span></h3>`;
      html += `<table><tr><th>ticker</th><th>analyst</th><th>stance</th><th>conviction</th><th class="num">conf</th><th class="num">horizon</th><th>thesis</th></tr>`;
      const sorted = [...s.views].sort((a, b) => a.ticker.localeCompare(b.ticker) || a.analyst.localeCompare(b.analyst));
      sorted.forEach((vw) => {
        const st = vw.abstained ? "abstained" : Math.abs(vw.conviction) < 0.1 ? "neutral" : vw.conviction > 0 ? "bullish" : "bearish";
        const w = Math.min(50, Math.abs(vw.conviction) * 50);
        html += `<tr><td class="mono">${vw.ticker}</td><td>${esc(analystDisplay(vw.analyst))}</td><td><span class="stance ${st}">${st}</span></td>
          <td><div class="bar" title="${fmt.signed(vw.conviction)}"><i class="${vw.conviction >= 0 ? "pos" : "neg"}" style="width:${w}%"></i></div></td>
          <td class="num">${vw.abstained ? "–" : fmt.pct0(vw.confidence, 0)}</td><td class="num">${vw.abstained ? "–" : vw.horizon_days + "d"}</td>
          <td style="max-width:520px">${esc(vw.thesis)}${vw.risks && vw.risks.length ? `<div class="muted" style="font-size:12px">risks: ${esc(vw.risks.join("; "))}</div>` : ""}</td></tr>`;
      });
      html += `</table>`;
      const pod = Object.entries(s.convictions).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 8).map(([t, c]) => `<span class="tag">${t} ${fmt.signed(c)}</span>`).join("");
      html += `<div style="margin:6px 0 4px"><span class="muted" style="font-size:12px">pod blend:</span> ${pod}</div>`;
    });
    $("#views-table").innerHTML = html;

    const tickers = Array.from(new Set([...Object.keys(v.convictions), ...Object.keys(rec.final_weights), ...Object.keys(rec.positions)])).sort();
    $("#weights-table").innerHTML = `<table><tr><th>ticker</th><th>sector</th><th class="num">final conviction</th><th class="num">target w</th><th class="num">after risk</th><th class="num">shares</th></tr>` +
      tickers.map((t) => { const fw = rec.final_weights[t] ?? 0, tw = rec.target_weights[t] ?? 0; return `<tr><td class="mono">${t}</td><td class="muted">${esc(rec.sectors[t] || "")}</td><td class="num ${cls(v.convictions[t])}">${fmt.signed(v.convictions[t])}</td><td class="num">${fmt.pct(tw)}</td><td class="num ${Math.abs(fw - tw) > 1e-6 ? "warn" : ""}">${fmt.pct(fw)}</td><td class="num">${rec.positions[t] ?? 0}</td></tr>`; }).join("") + `</table>`;

    $("#orders-table").innerHTML = rec.fills.length ? `<table><tr><th>side</th><th>ticker</th><th class="num">qty</th><th class="num">ref</th><th class="num">fill</th><th class="num">cost</th></tr>` +
      rec.fills.map((f, i) => `<tr><td class="${f.side === "buy" ? "gain" : "loss"}">${f.side}</td><td class="mono">${f.ticker}</td><td class="num">${f.quantity}</td><td class="num">${fmt.num(rec.orders[i]?.reference_price)}</td><td class="num">${fmt.num(f.price)}</td><td class="num">${fmt.money(f.commission + f.slippage_cost)}</td></tr>`).join("") + `</table>` : `<div class="empty">no trades — the book was inside its no-trade band</div>`;
    $("#risk-events").innerHTML = rec.risk_events.length ? rec.risk_events.map((e) => `<div class="note"><div class="head"><span>${e.limit}${e.ticker ? ` · ${e.ticker}` : ""}</span><span class="num">${fmt.num(e.before)} → ${fmt.num(e.after)}</span></div>${e.note ? `<div class="body">${esc(e.note)}</div>` : ""}</div>`).join("") : `<div class="muted" style="font-size:13px">no limits fired</div>`;
  }

  function renderBoard(v) {
    const card = $("#board-card"), b = v.board;
    if (!b || !b.votes.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const llm = b.votes.filter((x) => x.source === "llm").length;
    const models = new Set(b.votes.map((x) => x.model).filter(Boolean));
    $("#board-source").textContent = llm ? `${models.size} model${models.size === 1 ? "" : "s"} voting` : "rules board";
    const dir = b.stance === "cut risk" ? "loss" : b.stance === "add risk" ? "gain" : "";
    $("#board-resolution").innerHTML = `<div class="resolution">
        <div><div class="label muted" style="font-size:10.5px;letter-spacing:.1em;text-transform:uppercase">Resolution</div>
             <div class="big ${dir}">${fmt.pct0(b.exposure, 0)} deployed</div></div>
        <div class="arrow">CIO asked ${fmt.pct0(b.cio_proposal, 0)} →</div>
        <div class="text">${esc(b.summary)}</div></div>`;
    $("#board-votes").innerHTML = b.votes.map((x) => {
      const s = x.stance === "cut risk" ? "bearish" : x.stance === "add risk" ? "bullish" : "neutral";
      return `<div class="vote ${x.dissent ? "dissent" : ""}">
        <div class="who"><b>${esc(x.display)}</b><span class="stance ${s}">${esc(x.stance)}</span></div>
        <div class="exposure">${fmt.pct0(x.exposure_vote, 0)}</div>
        <div class="gauge"><i style="width:${Math.round(x.exposure_vote * 100)}%"></i></div>
        <div class="concern">${esc(x.concern)}</div>
        ${x.trim && x.trim.length ? `<div style="margin-top:6px">${x.trim.map((t) => `<span class="tag">trim ${esc(t)}</span>`).join("")}</div>` : ""}
        ${x.fallback_reason ? `<span class="model warn" title="${esc(x.fallback_reason)}">rules fallback · ${esc(x.model || "model")} unreachable</span>`
                             : x.model ? `<span class="model">${esc(x.model)}</span>` : ""}
      </div>`;
    }).join("");
  }

  // =====================================================================
  // Risk & book
  // =====================================================================
  function renderRisk() {
    const recs = state.records; if (!recs.length) return;
    const rec = recs[recs.length - 1], spec = state.funds.find((f) => f.name === rec.fund);
    const eq = rec.nav, pos = Object.entries(rec.positions);
    const gross = pos.reduce((a, [t, s]) => a + Math.abs(s * rec.marks[t]), 0) / eq, net = pos.reduce((a, [t, s]) => a + s * rec.marks[t], 0) / eq;
    const longs = pos.filter(([, s]) => s > 0).length, shorts = pos.filter(([, s]) => s < 0).length;
    const fired = recs.reduce((a, r) => a + r.risk_events.length, 0);
    $("#risk-tiles").innerHTML = [
      tile("NAV", fmt.money(eq), `cash ${fmt.money(rec.cash)}`),
      tile("Gross / net", `${fmt.pct0(gross, 0)} / ${fmt.pct(net, 0)}`, `${longs} long · ${shorts} short`),
      tile("Drawdown", fmt.pct0(-rec.drawdown), `soft ${spec ? fmt.pct0(spec.risk.drawdown_soft, 0) : "–"} · hard ${spec ? fmt.pct0(spec.risk.drawdown_hard, 0) : "–"}`, rec.drawdown < -0.05 ? "loss" : ""),
      tile("Regime", rec.verdict.cio.regime, `exposure ×${(rec.verdict.exposure_multiplier != null ? rec.verdict.exposure_multiplier : rec.verdict.cio.exposure_multiplier).toFixed(2)}${rec.verdict.board ? " · board" : ""}`),
      tile("Limits fired", String(fired), `across ${recs.length} meetings`),
    ].join("");
    $("#book-date").textContent = rec.as_of;
    $("#positions-table").innerHTML = pos.length ? `<table><tr><th>ticker</th><th>sector</th><th class="num">shares</th><th class="num">mark</th><th class="num">value</th><th class="num">weight</th></tr>` +
      pos.sort((a, b) => Math.abs(b[1] * rec.marks[b[0]]) - Math.abs(a[1] * rec.marks[a[0]])).map(([t, s]) => `<tr><td class="mono">${t}</td><td class="muted">${esc(rec.sectors[t] || "")}</td><td class="num ${cls(s)}">${s}</td><td class="num">${fmt.num(rec.marks[t])}</td><td class="num">${fmt.money(s * rec.marks[t])}</td><td class="num">${fmt.pct(s * rec.marks[t] / eq)}</td></tr>`).join("") + `</table>` : `<div class="empty">flat</div>`;
    const bySector = {};
    pos.forEach(([t, s]) => { const k = rec.sectors[t] || "Unknown"; bySector[k] = (bySector[k] || 0) + Math.abs(s * rec.marks[t]) / eq; });
    barChart($("#sector-chart"), Object.entries(bySector).sort((a, b) => b[1] - a[1]).map(([label, value]) => ({ label, value })), spec?.risk?.max_sector_pct);
    const all = []; recs.forEach((r) => r.risk_events.forEach((e) => all.push({ date: r.as_of, ...e })));
    const counts = {}; all.forEach((e) => counts[e.limit] = (counts[e.limit] || 0) + 1);
    $("#limits-table").innerHTML = all.length ? `<div style="margin-bottom:8px">${Object.entries(counts).map(([k, n]) => `<span class="tag">${k} × ${n}</span>`).join("")}</div><div class="table-view"><table><tr><th>date</th><th>limit</th><th>ticker</th><th class="num">before</th><th class="num">after</th><th>note</th></tr>` +
      all.slice().reverse().slice(0, 300).map((e) => `<tr><td class="mono">${e.date}</td><td>${e.limit}</td><td class="mono">${e.ticker || ""}</td><td class="num">${fmt.num(e.before)}</td><td class="num">${fmt.num(e.after)}</td><td class="muted">${esc(e.note || "")}</td></tr>`).join("") + `</table></div>` : `<div class="empty">no limit has fired</div>`;
  }

  // =====================================================================
  // Paper fund
  // =====================================================================
  $("#btn-run").onclick = async () => {
    const body = { fund: $("#p-fund").value, tickers: $("#p-tickers").value, date: $("#p-date").value || null, provider: $("#p-provider").value };
    $("#btn-run").disabled = true; $("#run-status").textContent = "committee sitting…";
    try {
      const job = await api("/api/run", { method: "POST", body: JSON.stringify(body) });
      if (job.status === "done") { $("#run-status").textContent = `done — NAV ${fmt.money(job.result.nav)} · ${job.result.orders.length} orders · regime ${job.result.verdict.cio.regime}`; $("#btn-run").disabled = false; await loadPaper(); return; }
      const poll = async () => {
        const j = await api(`/api/jobs/${job.id}`);
        if (j.status === "done") { $("#run-status").textContent = `done — NAV ${fmt.money(j.result.nav)} · ${j.result.orders.length} orders`; $("#btn-run").disabled = false; await loadPaper(); return; }
        if (j.status === "failed") { $("#run-status").innerHTML = `<span class="loss">${esc(j.error)}</span>`; $("#btn-run").disabled = false; return; }
        $("#run-status").textContent = j.message || j.status; setTimeout(poll, 500);
      };
      poll();
    } catch (e) { $("#run-status").innerHTML = `<span class="loss">${esc(e.message)}</span>`; $("#btn-run").disabled = false; }
  };
  $("#btn-reset").onclick = async () => {
    const f = $("#p-fund").value; if (!confirm(`Reset the paper book for ${f}? The ledger rows for this fund are deleted.`)) return;
    await api(`/api/ledger/${f}`, { method: "DELETE" }); loadPaper();
  };
  $("#p-fund").onchange = loadPaper;

  async function loadPaper() {
    const f = $("#p-fund").value; if (!f) return;
    const led = await api(`/api/ledger/${f}`);
    state.paper = led;
    const cycles = led.cycles;
    if (cycles.length < 2) {
      $("#paper-chart").innerHTML = `<div class="empty">${cycles.length ? `One meeting on the books (${cycles[0].as_of}, NAV ${fmt.money(cycles[0].nav)}). Run another cycle with a later date and the curve starts here.` : "No meetings yet — run a cycle to open the books."}</div>`;
    } else {
      lineChart($("#paper-chart"), { dates: cycles.map((c) => c.as_of), series: [{ name: "Paper NAV", short: "NAV", cls: "fund", values: cycles.map((c) => c.nav), area: "fund" }], baseline: cycles[0].nav, height: 240, regime: cycles.map((c) => c.regime || "neutral") });
    }
    // default the next as-of date to a week after the last meeting, so "run one cycle" naturally advances the book
    if (cycles.length) { const d = new Date(cycles[cycles.length - 1].as_of); d.setDate(d.getDate() + 7); $("#p-date").value = d.toISOString().slice(0, 10); }
    $("#paper-book").innerHTML = led.book ? `<dl class="kv"><dt>as of</dt><dd>${led.book.as_of}</dd><dt>cash</dt><dd>${fmt.money(led.book.cash)}</dd><dt>positions</dt><dd>${Object.keys(led.book.positions).length}</dd></dl>
      <table style="margin-top:8px"><tr><th>ticker</th><th class="num">shares</th><th class="num">avg cost</th></tr>${Object.values(led.book.positions).map((p) => `<tr><td class="mono">${p.ticker}</td><td class="num ${cls(p.shares)}">${p.shares}</td><td class="num">${fmt.num(p.avg_cost)}</td></tr>`).join("")}</table>` : `<div class="empty">no book yet — run a cycle</div>`;
    $("#paper-cycles").innerHTML = cycles.length ? `<table><tr><th>date</th><th>regime</th><th class="num">NAV</th><th class="num">cash</th><th class="num">orders</th><th class="num">costs</th><th></th></tr>` +
      cycles.slice().reverse().map((c) => `<tr><td class="mono">${c.as_of}</td><td>${c.regime || ""}</td><td class="num">${fmt.money(c.nav)}</td><td class="num">${fmt.money(c.cash)}</td><td class="num">${c.n_orders}</td><td class="num">${fmt.money(c.costs)}</td><td><button class="btn small" data-cycle="${c.id}">transcript</button></td></tr>`).join("") + `</table>` : `<div class="empty">no meetings yet</div>`;
    $$("button[data-cycle]", $("#paper-cycles")).forEach((b) => b.onclick = async () => {
      const recs = await Promise.all(cycles.map((c) => api(`/api/cycles/${c.id}`)));
      state.records = recs; state.cycleIdx = recs.findIndex((r) => r.id === b.dataset.cycle); state.source = "paper";
      $("#committee-empty").classList.add("hidden"); $("#committee-body").classList.remove("hidden");
      $("#risk-empty").classList.add("hidden"); $("#risk-body").classList.remove("hidden");
      renderCommittee(); renderRisk(); showView("committee");
    });
  }

  // =====================================================================
  // Mandates
  // =====================================================================
  async function loadMandates() {
    state.funds = await api("/api/funds");
    const list = $("#fund-list");
    list.innerHTML = state.funds.map((f) => `<div class="fund-item ${state.mandate === f.name ? "active" : ""}" data-name="${f.name}"><h3>${esc(f.name)}</h3><div class="muted" style="font-size:12.5px">${esc(f.description || f.error || "")}</div><div style="margin-top:6px">${(f.strategies || []).map((s) => `<span class="tag">${esc(s.title)} ${Math.round(s.weight * 100 / (f.strategies.reduce((a, x) => a + x.weight, 0)))}%</span>`).join("")}${f.uses_llm ? `<span class="tag">LLM</span>` : `<span class="tag">keyless</span>`}</div></div>`).join("");
    $$(".fund-item", list).forEach((d) => d.onclick = () => openMandate(d.dataset.name));
    if (!state.mandate && state.funds.length) openMandate(state.funds[0].name);
    $("#roster").innerHTML = (state.meta?.analysts || []).map((a) => `<div class="r"><b>${esc(a.display)}</b><span class="mono">${a.name}</span> · ${a.kind} · ${a.horizon_days}d<div class="muted" style="font-size:12px;margin-top:3px">${esc(a.summary)}</div></div>`).join("");
  }
  async function openMandate(name) {
    state.mandate = name;
    $$(".fund-item").forEach((d) => d.classList.toggle("active", d.dataset.name === name));
    try { const f = await api(`/api/funds/${name}`); $("#mandate-title").textContent = f.name; $("#mandate-yaml").value = f.yaml; $("#mandate-status").textContent = ""; }
    catch (e) { $("#mandate-status").innerHTML = `<span class="loss">${esc(e.message)}</span>`; }
  }
  $("#btn-save-fund").onclick = async () => {
    const y = $("#mandate-yaml").value, m = y.match(/^name:\s*(\S+)/m); const name = m ? m[1] : state.mandate;
    try { await api(`/api/funds/${name}`, { method: "PUT", body: JSON.stringify({ yaml: y }) }); $("#mandate-status").innerHTML = `<span class="gain">saved ${esc(name)}</span>`; state.mandate = name; await refreshFunds(); loadMandates(); }
    catch (e) { $("#mandate-status").innerHTML = `<span class="loss">${esc(e.message)}</span>`; }
  };
  $("#btn-delete-fund").onclick = async () => {
    if (!state.mandate || !confirm(`Delete mandate ${state.mandate}?`)) return;
    await api(`/api/funds/${state.mandate}`, { method: "DELETE" }); state.mandate = null; await refreshFunds(); loadMandates();
  };
  $("#btn-new-fund").onclick = () => {
    state.mandate = null; $("#mandate-title").textContent = "new mandate";
    $("#mandate-yaml").value = `name: my-fund\ndescription: describe the desk\nstrategies:\n  - name: core\n    weight: 1.0\n    analysts:\n      - name: trend\n      - name: lowvol\n        weight: 0.5\ncommittee:\n  red_team: true\n  cio: true\nportfolio:\n  sizing: vol_scaled\n  min_conviction: 0.1\n  rebalance_band: 0.015\nrisk:\n  max_position_pct: 0.15\n  max_gross_exposure: 1.0\n  max_sector_pct: 0.4\n  drawdown_soft: 0.1\n  drawdown_hard: 0.2\ncapital: 100000\nrebalance: weekly\nbenchmark: SPY\n`;
  };

  async function refreshFunds() {
    state.funds = await api("/api/funds");
    const opts = state.funds.filter((f) => !f.error).map((f) => `<option value="${f.name}">${f.name}${f.uses_llm ? "" : " (keyless)"}</option>`).join("");
    const keep = $("#f-fund").value, keepP = $("#p-fund").value, keepC = $("#c-fund").value;
    $("#f-fund").innerHTML = opts; $("#p-fund").innerHTML = opts; $("#c-fund").innerHTML = opts;
    if (keep) $("#f-fund").value = keep;
    if (keepP) $("#p-fund").value = keepP;
    $("#c-fund").value = keepC || (state.funds.find((f) => f.name === "council-live") ? "council-live" : $("#c-fund").value);
  }

  // =====================================================================
  // boot
  // =====================================================================
  async function boot() {
    try {
      state.meta = await api("/api/meta");
      const pill = $("#llm-pill");
      pill.classList.toggle("ok", state.meta.llm_available);
      $("#llm-text").textContent = state.meta.llm_available ? `model ${state.meta.current_model}` : `no LLM key — quant + rules only`;
      paintBudget(state.meta.budget);
      if (state.meta.budget && state.meta.budget.allow_llm === false) {
        const n = document.createElement("div"); n.innerHTML = budgetNotice(state.meta.budget);
        $("#view-council").insertBefore(n.firstChild, $("#view-council").firstChild);
      }
      if (state.meta.serverless && state.meta.llm_available) { const h = document.createElement("div"); h.className = "callout"; h.style.marginBottom = "14px"; h.textContent = "LLM committee on the hosted demo: each backtest must finish inside the 5-minute function limit — keep to ~6 tickers and ~1 year, or run locally for long studies." + (state.meta.durable ? "" : " The prompt cache here is warm-instance only, so repeated runs may re-spend."); $("#research-filters").insertAdjacentElement("afterend", h); }
      if (state.meta.serverless && !state.meta.durable) { const n = document.createElement("div"); n.className = "callout"; n.style.marginBottom = "14px"; n.textContent = "Hosted demo: this instance has no persistent disk — the paper ledger and saved mandates live only while the instance is warm. Run locally for a permanent track record."; $("#view-paper").insertBefore(n, $("#view-paper").firstChild); }
      $("#f-model").innerHTML = `<option value="">default (${state.meta.current_model})</option>` + state.meta.models.map((m) => `<option value="${m.id}" ${m.configured ? "" : "disabled"}>${m.display}${m.configured ? "" : " — key missing"}</option>`).join("");
      await refreshFunds();
      $("#p-date").value = "2025-06-30";
      $("#c-date").value = "2025-06-30";
      const syncProvider = () => { $("#provider-pill").textContent = `data: ${$("#c-fund").closest(".filters") && $("#view-council").classList.contains("active") ? $("#c-provider").value : $("#f-provider").value}`; };
      ["#c-provider", "#f-provider"].forEach((sel) => { const el = $(sel); if (el) el.onchange = syncProvider; });
      $$(".nav button").forEach((b) => b.addEventListener("click", syncProvider));
      syncProvider();
      const last = await api("/api/ledger");
      const bt = last.backtests && last.backtests[0];
      if (bt) { try { const res = await api(`/api/backtests/${bt.id}`); setResult(res, "backtest"); $("#f-fund").value = res.fund; $("#f-tickers").value = res.universe.join(","); $("#f-start").value = res.start; $("#f-end").value = res.end; $("#f-provider").value = res.provider; } catch {} }
      let view = "council"; try { view = localStorage.getItem("consilium-view") || view; } catch {}
      showView(view);
    } catch (e) { $("#bt-status").innerHTML = `<span class="loss">cannot reach the API: ${esc(e.message)}</span>`; }
  }
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { render(); if (state.paper) loadPaper(); }, 150); });
  boot();
})();
