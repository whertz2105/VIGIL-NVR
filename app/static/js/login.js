(function () {
  const form = document.getElementById("login-form");
  const err = document.getElementById("login-err");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    const username = document.getElementById("login-user").value;
    const password = document.getElementById("login-pass").value;
    let r;
    try {
      r = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
    } catch (_) {
      err.textContent = "Network error — try again.";
      err.hidden = false;
      return;
    }
    if (r.ok) { location.href = "/settings"; return; }
    err.textContent = r.status === 429
      ? "Too many attempts. Wait a minute and try again."
      : "Invalid username or password.";
    err.hidden = false;
  });
})();
