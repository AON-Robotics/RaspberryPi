// Login page logic (see ../login.html). Sends the team password to
// /api/login, which sets an HttpOnly cookie the page itself can't read.

const form = document.getElementById("login-form");
const password = document.getElementById("password");
const errEl = document.getElementById("error");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.textContent = "";
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password.value }),
    });
    if (r.ok) { location.href = "/"; return; }
    const data = await r.json().catch(() => ({}));
    errEl.textContent = typeof data.detail === "string" ? data.detail : `HTTP ${r.status}`;
  } catch {
    errEl.textContent = "Can't reach the chat app.";
  }
  password.select();
});

password.focus();
