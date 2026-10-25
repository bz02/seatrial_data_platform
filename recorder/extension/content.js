// Content script: read the server URL and capture token saved by the popup, then start the recorder.
// The recorder itself checks the org's host allowlist and stays dormant everywhere else.
const chromeStore = {
  get: (k) => new Promise((r) => chrome.storage.local.get("st_" + k, (o) => r(o["st_" + k] ?? null))),
  set: (k, v) => new Promise((r) => chrome.storage.local.set({ ["st_" + k]: v }, r)),
};
chrome.storage.local.get(["apiBase", "token"], ({ apiBase, token }) => {
  if (apiBase && token) window.SeaTrailRecorder.init({ apiBase, token, storage: chromeStore });
});
