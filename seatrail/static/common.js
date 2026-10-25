// Shared helpers for every SeaTrial page.
const ST = {
  me: null,

  async api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json", "X-Requested-With": "seatrail" }, opts.headers || {});
    const res = await fetch(path, { method: opts.method || "GET", headers, credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401 && !opts.noRedirect) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new Error("sign in required");
    }
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  },

  esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
  money(c) { const f = c % 100 ? 2 : 0; return "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: f, maximumFractionDigits: f }); },
  num(v) { return typeof v === "number" ? v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : (v ?? ""); },
  pct(v) { return Math.round((v || 0) * 100) + "%"; },
  date(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; },
  label(s) { return String(s || "").replace(/_/g, " "); },

  toast(msg) {
    const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), 2600);
  },

  statusTag(s) {
    const cls = { approved: "ok", needs_review: "warn", rejected: "bad", submitted: "ok", checked: "gray",
      in_progress: "gray", listed: "ok", draft: "gray" }[s] || "gray";
    return `<span class="tag ${cls}">${ST.label(s)}</span>`;
  },

  // Top navigation. `roles` lists who can use this page. In demo mode the bar also has an account
  // switcher, and opening a page as the wrong role signs in as the first demo user who fits.
  async topbar(active, roles) {
    const cfg = await ST.api("/api/auth/config");
    let me = await ST.api("/api/me", { noRedirect: true }).catch(() => null);
    let demoUsers = [];
    if (cfg.demo_mode) {
      demoUsers = await ST.api("/api/demo/users");
      const fits = (u) => !roles || roles.includes(u.role);
      if (!me || !fits(me)) {
        const pick = demoUsers.find(fits);
        if (pick) me = await ST.api("/api/auth/demo-login", { method: "POST", body: { user_id: pick.id } });
      }
    }
    if (!me) { location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash); return new Promise(() => {}); }
    ST.me = me;
    const links = [["/app", "Home"], ["/expert", "Expert"], ["/market", "Marketplace"], ["/enterprise", "Enterprise"], ["/review", "Review"]];
    const bar = document.createElement("header");
    bar.className = "topbar";
    const allowed = demoUsers.filter((u) => !roles || roles.includes(u.role));
    bar.innerHTML = `
      <a class="logo" href="/">SeaTrial</a>
      <nav>${links.map(([h, t]) => `<a href="${h}" class="${h === active ? "on" : ""}">${t}</a>`).join("")}</nav>
      <div class="user">
        ${cfg.demo_mode && allowed.length ? `<span class="small">Demo</span>
          <select id="st-user" aria-label="Switch demo account">${allowed.map((u) => `<option value="${u.id}" ${u.id === me.id ? "selected" : ""}>${ST.esc(u.name)} · ${ST.label(u.role)}</option>`).join("")}</select>`
          : `<span class="small">${ST.esc(me.name)} · ${ST.label(me.role)}</span>`}
        <button id="st-logout" class="ghost">Sign out</button>
      </div>`;
    document.body.prepend(bar);
    const sel = bar.querySelector("#st-user");
    if (sel) sel.onchange = async (e) => {
      await ST.api("/api/auth/demo-login", { method: "POST", body: { user_id: Number(e.target.value) } });
      location.reload();
    };
    bar.querySelector("#st-logout").onclick = async () => { await ST.api("/api/auth/logout", { method: "POST" }); location.href = "/login"; };
    if (roles && !roles.includes(me.role)) {
      const main = document.querySelector("main");
      main.innerHTML = `<div class="notice warn">This page is for ${roles.map(ST.label).join(", ")} accounts. You are signed in as ${ST.esc(me.name)} (${ST.label(me.role)}).</div>`;
      return new Promise(() => {});
    }
    return me;
  },

  stepsHTML(steps, opts = {}) {
    return `<div class="steps">${steps.map((s) => {
      if (!s.action) return `<div class="step"><div class="thought">${ST.esc(s.thought)}</div></div>`;
      if (opts.hideViews && s.action.tool === "view") return "";
      const args = Object.entries(s.action.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ");
      const obs = s.observation == null ? "" : (typeof s.observation === "string" ? s.observation : JSON.stringify(s.observation));
      return `<div class="step">
        ${s.thought ? `<div class="thought">${ST.esc(s.thought)}</div>` : ""}
        <div class="act">${s.i}. ${ST.esc(s.action.tool)}(${ST.esc(args)}) <span class="muted">t=${s.t}s</span></div>
        ${opts.showObs && obs && obs !== "OK" ? `<div class="obs">${ST.esc(obs.slice(0, 600))}</div>` : ""}
      </div>`;
    }).join("")}</div>`;
  },

  reportHTML(r) {
    const j = r.judge || {};
    const rub = ["correctness", "specificity", "consistency", "coverage", "teachability"];
    const v = r.verification;
    return `
      <div class="row"><div><div class="score">${ST.pct(r.quality_score)}</div><div class="small muted">quality score (needs ${ST.pct(r.threshold)})</div></div>
        <div class="spacer"></div>
        ${r.can_submit ? '<span class="tag ok">Ready to submit</span>' : '<span class="tag warn">Not ready yet</span>'}</div>
      <div class="bar" style="margin:8px 0 12px"><span style="width:${Math.round(r.quality_score * 100)}%"></span></div>
      ${r.tips && r.tips.length ? `<div class="notice ${r.can_submit ? "" : "warn"}"><ul class="tight">${r.tips.map((t) => `<li>${ST.esc(t)}</li>`).join("")}</ul></div>` : ""}
      <h3>Checks</h3>
      <table>
        ${v ? `<tr><td>Automatic verification</td><td class="num">${ST.pct(v.score)}</td><td>${v.passed ? '<span class="tag ok">passed</span>' : '<span class="tag bad">not passed</span>'}</td></tr>` : ""}
        <tr><td>Reasoning coverage</td><td class="num">${ST.pct(r.process.coverage)}</td><td class="small muted">${r.process.explained}/${r.process.decisions} decisions explained</td></tr>
        <tr><td>Model review (${ST.esc(j.mode)})</td><td class="num">${ST.pct(j.score)}</td><td class="small muted">${ST.esc(j.note || j.model || "")}</td></tr>
        <tr><td>Personal data redacted</td><td class="num">${r.pii.total}</td><td class="small muted">${ST.esc(Object.entries(r.pii.redactions).map(([k, n]) => k + " ×" + n).join(", "))}</td></tr>
        <tr><td>Near duplicates</td><td class="num">${r.duplicates.length}</td><td></td></tr>
      </table>
      <h3>Reasoning rubric</h3>
      <table>${rub.map((k) => `<tr><td>${k}</td><td class="num">${j[k] ?? "-"}/5</td></tr>`).join("")}</table>
      ${r.flags.length ? `<p class="small">Flags: ${r.flags.map((f) => `<span class="tag gray">${ST.label(f)}</span>`).join(" ")}</p>` : ""}`;
  },
};
