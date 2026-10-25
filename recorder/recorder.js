/*
 * SeaTrial workflow recorder.
 *
 * Records what an employee does in their company's web apps (navigation, clicks, form inputs) plus
 * their spoken or typed reasoning, and sends it to the company's own SeaTrial server.
 *
 * Privacy rules enforced here, and again on the server:
 *  - Only runs on hosts in the org's allowlist.
 *  - Inputs matching the org's mask selectors (passwords, bank fields...) are sent as [MASKED].
 *  - Nothing is recorded until the employee presses Start, and Pause/Stop are always one click away.
 *
 * Used two ways: as the content script of the browser extension, or embedded with a <script> tag in
 * an internal app. Call SeaTrialRecorder.init({apiBase, token, storage?}).
 */
(function () {
  if (window.SeaTrialRecorder) return;

  const localStore = {
    get: async (k) => { try { return JSON.parse(localStorage.getItem("seatrail:" + k)); } catch (e) { return null; } },
    set: async (k, v) => { try { localStorage.setItem("seatrail:" + k, JSON.stringify(v)); } catch (e) {} },
  };

  const R = {
    cfg: null, policy: null, store: localStore, state: null, buffer: [], ui: null, speech: null,
    flushing: false,
  };

  async function api(path, method, body) {
    const res = await fetch(R.cfg.apiBase.replace(/\/$/, "") + path, {
      method: method || "GET",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + R.cfg.token },
      body: body ? JSON.stringify(body) : undefined,
      keepalive: method === "POST",
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function hostAllowed(hosts) {
    const h = location.hostname.toLowerCase();
    return (hosts || []).some((a) => h === a || h.endsWith("." + a));
  }

  function isMasked(el) {
    if (!el || !el.matches) return false;
    if (el.type === "password") return true;
    return (R.policy.mask_selectors || []).some((sel) => { try { return el.matches(sel); } catch (e) { return false; } });
  }

  function labelFor(el) {
    if (!el) return "";
    const aria = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("title"));
    if (aria) return aria;
    const own = (label) => {
      // The label's own words, without the text of any control nested inside it (e.g. select options).
      const c = label.cloneNode(true);
      c.querySelectorAll("input, select, textarea, datalist, button").forEach((n) => n.remove());
      return c.textContent.replace(/\s+/g, " ").trim();
    };
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return own(l);
    }
    const wrap = el.closest && el.closest("label");
    if (wrap) return own(wrap);
    return el.placeholder || el.name || "";
  }

  function describe(el) {
    if (!el || el === document) return null;
    const text = (el.innerText || el.value || "").trim().replace(/\s+/g, " ").slice(0, 80);
    return {
      tag: (el.tagName || "").toLowerCase(),
      role: el.getAttribute && el.getAttribute("role") || undefined,
      text: ["input", "textarea", "select"].includes((el.tagName || "").toLowerCase()) ? undefined : text,
      label: labelFor(el).slice(0, 80) || undefined,
      name: el.name || undefined,
      placeholder: el.placeholder || undefined,
    };
  }

  function page() { return { url: location.href, title: document.title }; }

  function now() { return R.state ? (Date.now() - R.state.startedAt) / 1000 : 0; }

  function push(type, payload) {
    if (!R.state || !R.state.recording) return;
    R.buffer.push({ type, ts: Math.round(now() * 10) / 10, payload: Object.assign({ page: page() }, payload) });
    R.state.count = (R.state.count || 0) + 1;
    render();
    if (R.buffer.length >= 20) flush();
  }

  async function flush() {
    if (R.flushing || !R.buffer.length || !R.state) return;
    R.flushing = true;
    const batch = R.buffer.splice(0, R.buffer.length);
    try {
      await api(`/api/capture/sessions/${R.state.sessionId}/events`, "POST", { events: batch });
      R.state.sent = (R.state.sent || 0) + batch.length;
    } catch (e) {
      R.buffer.unshift(...batch); // retry next tick
      status("Offline, will retry: " + e.message);
    } finally {
      R.flushing = false;
      await R.store.set("state", R.state);
    }
  }

  // ---------------------------------------------------------------- listeners
  function onClick(e) {
    if (R.ui && R.ui.host.contains(e.target)) return;
    const el = e.target.closest("button, a, [role=button], [role=tab], [role=menuitem], input[type=checkbox], input[type=radio], td, th, li") || e.target;
    push("click", { target: describe(el) });
  }
  function onChange(e) {
    const el = e.target;
    if (R.ui && R.ui.host.contains(el)) return;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") return push("select", { target: describe(el), value: el.options[el.selectedIndex]?.text || el.value });
    if (tag === "input" || tag === "textarea") {
      if (["checkbox", "radio", "button", "submit"].includes(el.type)) return;
      const masked = isMasked(el) || !R.policy.capture_input_values;
      push("input", { target: describe(el), value: masked ? "" : el.value, masked });
    }
  }
  function onKey(e) {
    if (R.ui && R.ui.host.contains(e.target)) return;
    if (e.key === "Enter") push("key", { key: e.key, target: describe(e.target) });
  }
  function onSubmit(e) { push("submit", { target: describe(e.submitter || e.target) }); }
  let lastUrl = null;
  function onNav() {
    if (location.href === lastUrl) return; // hashchange and popstate both fire for one navigation
    lastUrl = location.href;
    push("navigate", { url: location.href });
  }

  function listen(on) {
    const m = on ? "addEventListener" : "removeEventListener";
    document[m]("click", onClick, true);
    document[m]("change", onChange, true);
    document[m]("keydown", onKey, true);
    document[m]("submit", onSubmit, true);
    window[m]("hashchange", onNav);
    window[m]("popstate", onNav);
  }

  // ---------------------------------------------------------------- speech (think-aloud)
  function startSpeech() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { status("Voice not supported in this browser; type your notes instead."); return; }
    const sr = new SR();
    sr.continuous = true; sr.interimResults = false; sr.lang = navigator.language || "en-US";
    sr.onresult = (ev) => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        if (ev.results[i].isFinal) push("think", { text: ev.results[i][0].transcript.trim(), via: "voice" });
      }
    };
    sr.onend = () => { if (R.speech === sr && R.state && R.state.recording) sr.start(); };
    sr.start();
    R.speech = sr;
    render();
  }
  function stopSpeech() { const s = R.speech; R.speech = null; if (s) s.stop(); render(); }

  // ---------------------------------------------------------------- controls
  async function start(goal, workflow) {
    const res = await api("/api/capture/sessions", "POST", { goal, workflow });
    R.state = { sessionId: res.session_id, startedAt: Date.now(), recording: true, count: 0, sent: 0, goal, workflow };
    await R.store.set("state", R.state);
    listen(true);
    lastUrl = null; onNav();
    render();
  }
  async function pause() {
    if (!R.state) return;
    R.state.recording = !R.state.recording;
    listen(R.state.recording);
    if (!R.state.recording) stopSpeech();
    await R.store.set("state", R.state);
    render();
  }
  async function stop() {
    if (!R.state) return;
    R.state.recording = true; // allow the final flush
    await flush();
    await api(`/api/capture/sessions/${R.state.sessionId}/stop`, "POST", {});
    listen(false);
    stopSpeech();
    const sid = R.state.sessionId;
    R.state = null;
    await R.store.set("state", null);
    render();
    status(`Saved episode #${sid}. Review it in SeaTrial before submitting.`);
  }
  function note(text) { if (text.trim()) push("think", { text: text.trim(), via: "typed" }); }

  // ---------------------------------------------------------------- UI (shadow DOM, so page CSS can't break it)
  function status(msg) { if (R.ui) R.ui.root.querySelector(".msg").textContent = msg; }

  function buildUI() {
    const host = document.createElement("div");
    host.id = "seatrail-recorder";
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { all: initial; }
        .p { font: 13px/1.4 system-ui, sans-serif; width: 290px; background: #fff; color: #1d2433;
             border: 1px solid #d5dae3; border-radius: 12px; box-shadow: 0 8px 28px rgba(15,23,42,.18); overflow: hidden; }
        .h { display:flex; align-items:center; gap:8px; padding:10px 12px; background:#0f3d3e; color:#fff; font-weight:600; }
        .dot { width:9px; height:9px; border-radius:50%; background:#9aa5b1; }
        .dot.on { background:#ff4d4f; box-shadow:0 0 0 3px rgba(255,77,79,.3); }
        .b { padding: 10px 12px; display:grid; gap:8px; }
        input, select, textarea { font: inherit; width: 100%; box-sizing: border-box; padding: 6px 8px; border: 1px solid #c9d0db; border-radius: 6px; }
        textarea { resize: vertical; min-height: 44px; }
        .row { display:flex; gap:6px; }
        button { font: inherit; flex:1; padding: 7px 8px; border-radius: 6px; border: 1px solid #c9d0db; background:#f6f8fb; cursor:pointer; }
        button.primary { background:#0f766e; border-color:#0f766e; color:#fff; }
        button.danger { background:#fff1f0; border-color:#ffa39e; color:#a8071a; }
        .msg { color:#5b6475; font-size:12px; min-height: 16px; }
        .meta { color:#5b6475; font-size:12px; }
        .min { margin-left:auto; background:none; border:none; color:#fff; flex:0; cursor:pointer; font-size:16px; padding:0 4px; }
      </style>
      <div class="p">
        <div class="h"><span class="dot"></span><span class="title">SeaTrial recorder</span><button class="min" title="Minimize">–</button></div>
        <div class="b body"></div>
      </div>`;
    document.documentElement.appendChild(host);
    R.ui = { host, root };
    root.querySelector(".min").onclick = () => {
      const b = root.querySelector(".body"); b.style.display = b.style.display === "none" ? "grid" : "none";
    };
    render();
  }

  function render() {
    if (!R.ui) return;
    const body = R.ui.root.querySelector(".body");
    const dot = R.ui.root.querySelector(".dot");
    const rec = R.state && R.state.recording;
    dot.className = "dot" + (rec ? " on" : "");
    if (!R.state) {
      const wf = (R.policy.workflows || []).map((w) => `<option value="${w}">${w.replace(/_/g, " ")}</option>`).join("");
      body.innerHTML = `
        <div class="meta">Signed in as ${R.policy.employee}. Nothing is recorded until you press Start.</div>
        <input class="goal" placeholder="What are you about to do? (e.g. enter Acme invoice)" />
        <select class="wf"><option value="">Workflow (optional)</option>${wf}</select>
        <div class="row"><button class="primary start">Start recording</button></div>
        <div class="msg"></div>`;
      body.querySelector(".start").onclick = () =>
        start(body.querySelector(".goal").value, body.querySelector(".wf").value).catch((e) => status(e.message));
      return;
    }
    if (body.dataset.mode !== "rec") {
      body.dataset.mode = "rec";
      body.innerHTML = `
        <div class="meta stats"></div>
        <textarea class="note" placeholder="Say or type why you're doing this step. Enter to add."></textarea>
        <div class="row"><button class="mic"></button><button class="pause"></button></div>
        <div class="row"><button class="danger stop">Stop & save</button></div>
        <div class="msg"></div>`;
      const ta = body.querySelector(".note");
      ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); note(ta.value); ta.value = ""; } });
      body.querySelector(".mic").onclick = () => (R.speech ? stopSpeech() : startSpeech());
      body.querySelector(".pause").onclick = pause;
      body.querySelector(".stop").onclick = () => stop().catch((e) => status(e.message));
    }
    body.querySelector(".stats").textContent =
      `Episode #${R.state.sessionId} · ${rec ? "recording" : "paused"} · ${R.state.count || 0} steps`;
    body.querySelector(".mic").textContent = R.speech ? "Stop voice" : "Think aloud (voice)";
    body.querySelector(".pause").textContent = rec ? "Pause" : "Resume";
  }

  // ---------------------------------------------------------------- init
  async function init(cfg) {
    R.cfg = cfg;
    if (cfg.storage) R.store = cfg.storage;
    try {
      R.policy = await api("/api/capture/config");
    } catch (e) {
      console.warn("SeaTrial recorder disabled:", e.message);
      return { enabled: false, reason: e.message };
    }
    if (!hostAllowed(R.policy.allowed_hosts)) return { enabled: false, reason: "host not in allowlist" };
    R.state = await R.store.get("state");
    if (R.state && R.state.recording) { listen(true); onNav(); }
    buildUI();
    setInterval(flush, 2000);
    window.addEventListener("pagehide", flush);
    return { enabled: true };
  }

  window.SeaTrialRecorder = { init, start, stop, pause, note, flush, _r: R };
})();
