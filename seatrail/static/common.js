// Shared helpers for every SeaTrail page.
const ST = {
  userId() { try { return localStorage.getItem("st_user"); } catch (e) { return null; } },
  setUser(id) { try { localStorage.setItem("st_user", id); } catch (e) {} },

  async api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    const uid = ST.userId();
    if (uid) headers["X-User-Id"] = uid;
    const res = await fetch(path, { method: opts.method || "GET", headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  },

  esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
  money(c) { return "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 }); },
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

  // Top navigation with a demo user switcher. `roles` limits the switcher to people who can use this page.
  async topbar(active, roles) {
    const users = await ST.api("/api/demo/users");
    const allowed = users.filter((u) => !roles || roles.includes(u.role));
    let me = allowed.find((u) => String(u.id) === ST.userId());
    if (!me && allowed.length) { me = allowed[0]; ST.setUser(me.id); }
    const links = [["/", "Home"], ["/expert", "Expert"], ["/market", "Marketplace"], ["/enterprise", "Enterprise"], ["/review", "Review"]];
    const bar = document.createElement("header");
    bar.className = "topbar";
    bar.innerHTML = `
      <a class="logo" href="/">SeaTrail</a>
      <nav>${links.map(([h, t]) => `<a href="${h}" class="${h === active ? "on" : ""}">${t}</a>`).join("")}</nav>
      <div class="user"><span class="small">Demo user</span>
        <select id="st-user">${allowed.map((u) => `<option value="${u.id}" ${me && u.id === me.id ? "selected" : ""}>${ST.esc(u.name)} · ${ST.label(u.role)}</option>`).join("")}</select>
      </div>`;
    document.body.prepend(bar);
    bar.querySelector("#st-user").onchange = (e) => { ST.setUser(e.target.value); location.reload(); };
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
