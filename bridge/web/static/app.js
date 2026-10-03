// Chat page logic (see ../index.html). External file because the app's
// Content-Security-Policy blocks inline scripts.

const log = document.getElementById("log");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const healthEl = document.getElementById("health");

let busy = false;        // a chat request is running
let pipelineOk = false;  // last health check: every hop up
updateSend();

// One conversation per tab. sessionStorage survives a reload of this tab
// but not a new tab; guarded because storage can be blocked.
// crypto.getRandomValues works on plain HTTP (randomUUID needs HTTPS).
function makeId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
}
let sessionId;
try { sessionId = sessionStorage.getItem("sessionId") || makeId(); sessionStorage.setItem("sessionId", sessionId); }
catch { sessionId = makeId(); }

function add(cls, text) {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

// Each tool call becomes a collapsible line: name(args) -> ok/error,
// with the full JSON result inside.
function addTools(calls) {
  if (!calls.length) return;
  const box = document.createElement("div");
  box.className = "tools";
  for (const c of calls) {
    const d = document.createElement("details");
    const s = document.createElement("summary");
    s.textContent = `${c.name}(${JSON.stringify(c.args)}) → `;
    const status = document.createElement("span");
    status.textContent = c.result.ok ? "ok" : "error";
    if (!c.result.ok) status.className = "bad";
    s.appendChild(status);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(c.result, null, 2);
    d.append(s, pre);
    box.appendChild(d);
  }
  log.appendChild(box);
}

// FastAPI errors are {"detail": "text"}, or a list of problems for bad input.
function errorText(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail) && data.detail[0]) return data.detail[0].msg;
  return `HTTP ${status}`;
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (method === "POST") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("Logged out."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(errorText(data, r.status));
  return data;
}

function updateSend() {
  sendBtn.disabled = busy || !pipelineOk;
  sendBtn.title = pipelineOk ? "" : "A step of the pipeline is down (see the lights above).";
}

async function send() {
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = "";
  add("msg user", text);
  busy = true; updateSend();
  const thinking = add("thinking", "thinking…");
  try {
    const data = await request("POST", "/api/chat", { session_id: sessionId, message: text });
    thinking.remove();
    addTools(data.tool_calls);
    add("msg bot", data.reply || "(no reply)");
  } catch (e) {
    thinking.remove();
    add("err", "Error: " + e.message);
    checkHealth();
  } finally {
    busy = false; updateSend();
    input.focus();
    log.scrollTop = log.scrollHeight;
  }
}

// STOP is never disabled, whatever the lights say: it doesn't use the LLM.
async function stop() {
  try {
    await request("POST", "/api/stop");
    add("note", "STOP sent at " + new Date().toLocaleTimeString());
  } catch (e) {
    add("err", "STOP FAILED: " + e.message + " Use the backup STOP page or the physical stop.");
  }
}

async function newChat() {
  try { await request("POST", "/api/reset", { session_id: sessionId }); } catch {}
  log.textContent = "";
  add("note", "New conversation.");
}

async function logout() {
  try { await request("POST", "/api/logout"); } catch {}
  location.href = "/login";
}

// Status lights: one per hop (Ollama, model, robot server, serial, brain, token, tools).
const HOP_LABELS = { ollama: "Ollama", model: "Model", robot: "Robot link", token: "Token", tools: "Tools" };

async function checkHealth() {
  try {
    const data = await request("GET", "/api/health");
    healthEl.textContent = "";
    for (const h of data.hops) {
      const el = document.createElement("span");
      el.className = "hop " + (h.ok ? "ok" : "bad");
      el.textContent = HOP_LABELS[h.name] || h.name;
      el.title = h.ok ? `${h.detail} (${h.ms} ms)` : `${h.detail}\n${h.fix}`;
      healthEl.appendChild(el);
    }
    pipelineOk = data.ok;
  } catch (e) {
    healthEl.textContent = "Can't reach the chat app: " + e.message;
    pipelineOk = false;
  }
  updateSend();
}

sendBtn.addEventListener("click", send);
document.getElementById("stop").addEventListener("click", stop);
document.getElementById("new-chat").addEventListener("click", newChat);
document.getElementById("logout").addEventListener("click", logout);
input.addEventListener("keydown", (e) => {
  // Enter sends, Shift+Enter makes a new line.
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") stop(); });

request("GET", "/api/config").then(c => {
  document.getElementById("meta").textContent = `Model: ${c.model} · Debugging only. Esc sends STOP.`;
  document.getElementById("backup-stop").href = c.stop_page;
}).catch(() => {});

checkHealth();
setInterval(checkHealth, 10000);
input.focus();
