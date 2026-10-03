// Emergency STOP page logic (see ../stop.html).
const statusEl = document.getElementById("status");
const tokenEl = document.getElementById("token");

// Remember the token in this browser only. Storage can be blocked (private
// windows), so every access is guarded and the page still works by typing it.
function loadToken() { try { return localStorage.getItem("bridgeToken") || ""; } catch { return ""; } }
function saveToken(t) { try { localStorage.setItem("bridgeToken", t); } catch {} }
tokenEl.value = loadToken();

function show(text, cls) { statusEl.textContent = text; statusEl.className = cls; }

async function sendStop() {
  const token = tokenEl.value.trim();
  if (!token) { show("Enter the server token first.", "err"); tokenEl.focus(); return; }
  show("Sending STOP...", "");
  try {
    // Same endpoint the agent uses; the server checks the token.
    const r = await fetch("/tools/stop", {
      method: "POST",
      headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
      body: "{}",
    });
    if (r.status === 401) { show("Wrong token. Robot NOT stopped.", "err"); return; }
    if (!r.ok) { show("Server error " + r.status + ". Robot NOT stopped.", "err"); return; }
    const data = await r.json();
    if (data.ok) show("Stopped at " + new Date().toLocaleTimeString(), "ok");
    // `message` says which hop failed (serial cable, brain program...).
    else show("NOT stopped: " + (data.message || data.error || r.status), "err");
  } catch (e) {
    show("Server unreachable. Robot NOT stopped. Use the physical stop.", "err");
  }
}

document.getElementById("stop").addEventListener("click", sendStop);
document.getElementById("token-form").addEventListener("submit", (e) => {
  e.preventDefault();
  saveToken(tokenEl.value.trim());
  show("Token saved.", "ok");
});
// Keyboard shortcut, except while typing the token.
document.addEventListener("keydown", (e) => {
  if (document.activeElement === tokenEl) return;
  if (e.key === " " || e.key === "Escape") { e.preventDefault(); sendStop(); }
});
