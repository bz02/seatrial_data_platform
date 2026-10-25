const api = document.getElementById("api"), tok = document.getElementById("tok"), msg = document.getElementById("msg");
chrome.storage.local.get(["apiBase", "token"], (o) => { api.value = o.apiBase || ""; tok.value = o.token || ""; });
document.getElementById("save").onclick = async () => {
  try {
    const r = await fetch(api.value.replace(/\/$/, "") + "/api/capture/config", { headers: { Authorization: "Bearer " + tok.value } });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const cfg = await r.json();
    chrome.storage.local.set({ apiBase: api.value, token: tok.value });
    msg.textContent = `Connected as ${cfg.employee}. Recording is available on: ${cfg.allowed_hosts.join(", ")}. Reload those tabs.`;
  } catch (e) { msg.textContent = "Could not connect: " + e.message; }
};
