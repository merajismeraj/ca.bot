/* Fills the office's own details into the public legal pages; anything not yet set is highlighted. */
"use strict";
(async () => {
  const info = await fetch("/api/public/info").then(r => r.json()).catch(() => ({}));
  const missing = { firm_address: "[office address]", officer_name: "[grievance officer name]",
    officer_email: "[grievance officer email]", officer_phone: "[grievance officer phone]",
    jurisdiction: "[city of the office]", policy_updated: "[date]" };
  document.querySelectorAll("[data-k]").forEach(el => {
    const k = el.dataset.k, v = info[k];
    if (v === undefined || v === null || v === "") {
      el.textContent = missing[k] || `[${k}]`;
      el.classList.add("todo");
      el.title = "Set this in Privacy settings";
    } else {
      el.textContent = k === "policy_updated" ? new Date(v + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "long", year: "numeric" }) : v;
    }
  });
  if (info.firm_name) document.title = `${document.title} · ${info.firm_name}`;
  const ul = (sel, items) => { const u = document.querySelector(sel); if (!u) return; for (const t of items || []) { const li = document.createElement("li"); li.textContent = t; u.append(li); } };
  ul(".purposes", info.purposes);
  ul(".shared", info.shared_with);
  const enc = document.querySelector(".enc");
  if (enc) enc.textContent = info.encrypted ? "Client documents are encrypted at rest (AES-256)." : "Client documents are stored on access-controlled servers.";
  const toc = document.querySelector(".toc");
  if (toc) document.querySelectorAll("h2[id]").forEach(h => { const a = document.createElement("a"); a.href = `#${h.id}`; a.textContent = h.textContent; toc.append(a); });
})();
