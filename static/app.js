/* CA Brain – single-file UI. No build step; everything renders through h(). */
"use strict";

// ------------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
  return el;
}
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const S = { boot: null, me: null, counts: {}, history: [] };

const can = p => !!(S.me && S.me.perms && S.me.perms.includes(p));

async function api(method, path, body, isForm) {
  const headers = {};
  if (body && !isForm) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: isForm ? body : body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth/")) { showAuth(); throw new Error("Please sign in"); }
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join("; ") : `Error ${res.status}`);
  }
  return data;
}
function toast(msg, err) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3200);
}
async function safely(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const today = () => new Date(S.boot.today + "T00:00:00");
const pd = s => new Date(s + "T00:00:00");
const fmtDate = (s, y) => { const d = pd(s); return `${d.getDate()} ${MON[d.getMonth()]}${y ? " " + d.getFullYear() : ""}`; };
function rel(days) {
  if (days === 0) return "Due today";
  if (days === 1) return "Tomorrow";
  if (days < 0) return `${-days} day${days === -1 ? "" : "s"} late`;
  if (days <= 14) return `In ${days} days`;
  return null;
}
const STAGE = {
  upcoming: "Period running", awaiting_docs: "Waiting on client", blocked: "Blocked", ready: "Ready to prepare", in_progress: "Preparing",
  review: "Partner review", approved: "Approved · file it", filed: "Filed", na: "Not applicable", not_tracked: "Not tracked",
};
const initials = n => n.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join("").toUpperCase();
const firstName = n => n.split(/\s+/).find(w => !/^(ca|cs|dr\.?|mr\.?|ms\.?|mrs\.?)$/i.test(w)) || n;
const greet = () => { const hr = new Date().getHours(); return hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening"; };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// ------------------------------------------------------------------ shell
const NAV = [
  ["today", "◎", "Today"], ["inbox", "⇪", "Inbox"], ["calendar", "▦", "Calendar"],
  ["clients", "◫", "Clients"], ["outbox", "✉", "Outbox"], ["privacy", "⛨", "Privacy"], ["settings", "⚙", "Settings"],
];
const privacyAdmin = () => can("manage_team") || can("approve");
function renderNav() {
  const route = location.hash.slice(2).split("/")[0] || "today";
  const badge = { inbox: S.counts.inbox, outbox: (S.counts.outbox || 0) + (S.counts.replies || 0), calendar: S.counts.overrides };
  fill($("#nav"), ...NAV.filter(([k]) => k !== "privacy" || privacyAdmin()).map(([k, ico, label]) =>
    h("a", { href: `#/${k}`, class: route === k ? "on" : "" }, h("span", { class: "ico" }, ico), h("span", { class: "label" }, label),
      badge[k] ? h("span", { class: "badge" + (k === "outbox" ? " soft" : "") }, badge[k]) : null)));
  $("#firm-name").textContent = S.boot.firm_name;
  fill($("#side-foot"),
    h("div", { class: "who" }, h("span", { class: "avatar" }, initials(S.me.name)),
      h("div", { class: "grow", style: "min-width:0" }, h("div", { class: "ellipsis", style: "font-weight:600" }, S.me.name),
        h("div", { class: "small muted" }, S.me.role === "partner" ? "Partner" : "Staff"))),
    h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "btn sm ghost", onclick: () => changePassword() }, "Password"),
      h("button", { class: "btn sm ghost right", onclick: () => safely(async () => { await api("POST", "/api/auth/logout"); S.me = null; showAuth(); }) }, "Sign out")),
    h("div", { class: "small muted", style: "margin-top:8px" }, h("a", { href: "/privacy", target: "_blank" }, "Privacy"), " · ", h("a", { href: "/terms", target: "_blank" }, "Terms")));
  const cur = S.boot.fys.find(f => f.current);
  fill($("#fy-chip"), h("a", { href: "#/calendar", class: "pill " + (cur.signed_off ? "approved" : "awaiting_docs") },
    cur.label, " · ", cur.signed_off ? "calendar signed off ✓" : "calendar needs partner sign-off"));
}

async function refreshCounts() {
  const d = await api("GET", "/api/dashboard");
  S.counts = d.counts;
  renderNav();
  return d;
}

// --------------------------------------------------------------- drawers
function drawer(title, body, footer) {
  closeDrawer();
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const dr = h("div", { class: "drawer", role: "dialog" },
    h("header", {}, h("h2", { style: "margin:0" }, title), h("button", { class: "btn ghost right", onclick: closeDrawer }, "✕")),
    h("div", { class: "body" }, body), footer ? h("footer", {}, footer) : null);
  document.body.append(scrim, dr);
  setTimeout(() => { const f = dr.querySelector("input,textarea,select"); f && f.focus(); }, 30);
  return dr;
}
function closeDrawer() { document.querySelectorAll(".scrim,.drawer").forEach(e => e.remove()); }

function ask(title, fields, cta = "Save") {
  return new Promise(resolve => {
    const els = {};
    const body = fields.map(f => h("label", { class: "field" }, f.label,
      els[f.name] = f.type === "textarea" ? h("textarea", { class: "input", rows: 4, placeholder: f.placeholder || "" }, f.value || "")
        : h("input", { class: "input", type: f.type || "text", value: f.value || "", placeholder: f.placeholder || "" })));
    const go = () => {
      const out = {};
      for (const f of fields) {
        out[f.name] = els[f.name].value.trim();
        if (f.required && !out[f.name]) { els[f.name].focus(); return toast(`${f.label} is required`, true); }
      }
      closeDrawer(); resolve(out);
    };
    const dr = drawer(title, body, [h("button", { class: "btn", onclick: () => { closeDrawer(); resolve(null); } }, "Cancel"),
      h("button", { class: "btn primary", onclick: go }, cta)]);
    dr.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") go(); });
  });
}

// ------------------------------------------------------------ obligation row
function ring(p) {
  const col = p >= 100 ? "var(--green)" : p >= 50 ? "var(--amber)" : "var(--red)";
  return h("div", { class: "ring", title: `${p}% of documents in`, style: `background:conic-gradient(${col} ${p * 3.6}deg, var(--line) 0)` },
    h("i", {}, p >= 100 ? "✓" : p + "%"));
}

async function act(o, key, after) {
  let extra = {};
  if (key === "chase") return openChase(o.client_id, after);
  if (key === "file") {
    const v = await ask(`Record filing · ${o.form} ${o.period_label}`, [
      { name: "ack_no", label: "Acknowledgement / ARN / SRN", required: true, placeholder: "e.g. AA2710260123456" },
      { name: "filed_on", label: "Filed on", type: "date", value: S.boot.today }], "Mark as filed");
    if (!v) return; extra = v;
  }
  if (["send_back", "mark_na", "reopen"].includes(key)) {
    const labels = { send_back: "What needs fixing?", mark_na: "Why is this not applicable?", reopen: "Why reopen? (e.g. revised return)" };
    const v = await ask(labels[key], [{ name: "note", label: "Reason", type: "textarea", required: true }], "Confirm");
    if (!v) return; extra = v;
  }
  await safely(async () => {
    const r = await api("POST", `/api/obligations/${o.id}/action`, { action: key, ...extra });
    toast(`${r.form} ${r.period_label} → ${STAGE[r.stage]}`);
    after && after();
  });
}

function obligationRow(o, after, opts = {}) {
  const wrap = h("div", { class: "item" });
  const days = o.days_left, r = rel(days);
  const done = ["filed", "na"].includes(o.status);
  const mine = o.next_action && (o.next_action.role !== "partner" || can("approve"));
  const primary = o.next_action && !done
    ? h("button", { class: "btn sm " + (mine ? "primary" : ""), disabled: !mine || null, title: mine ? "" : "Needs a partner",
        onclick: e => { e.stopPropagation(); act(o, o.next_action.key, after); } }, o.next_action.label)
    : done ? h("span", { class: "small muted" }, o.ack_no ? `Ack ${o.ack_no}` : "")
    : h("span", { class: "small muted" }, o.blockers.length ? "Waiting on " + o.blockers[0] : o.stage === "upcoming" ? `Docs after ${MON[+o.months[o.months.length - 1].slice(5) - 1]} closes` : "");
  wrap.append(
    ring(o.readiness),
    h("div", { class: "grow" },
      h("div", { class: "t1 ellipsis" }, opts.hideClient ? `${o.form} · ${o.period_label}` : o.client_name),
      h("div", { class: "t2 ellipsis" }, opts.hideClient ? o.rule_name : `${o.form} · ${o.period_label}`, o.assignee ? ` · ${o.assignee}` : "")),
    h("div", { class: "col-stage" }, h("span", { class: "pill " + o.stage }, STAGE[o.stage])),
    h("div", { class: "col-due" },
      h("div", { class: "when " + (done ? "" : days < 0 ? "late" : days <= 3 ? "soon" : "") }, done ? fmtDate(o.filed_on || o.due) : r || fmtDate(o.due)),
      h("div", { class: "t2" }, fmtDate(o.due, true), o.due_shifted ? " · extended" : "")),
    h("div", {}, primary));
  let open = false, det;
  wrap.addEventListener("click", () => {
    open = !open;
    if (!open) return det.remove();
    det = h("div", { class: "detail", onclick: e => e.stopPropagation() },
      o.missing.length ? h("div", {}, h("b", {}, o.stage === "upcoming" ? "Will be needed once the period closes" : "Still needed from client"),
        h("ul", {}, o.missing.map(m => h("li", {}, `${m.label} — ${m.period}`)))) : h("div", { class: "small" }, "✓ All documents in."),
      o.blockers.length ? h("div", { class: "small" }, "⛓ File first: ", o.blockers.join(", ")) : null,
      o.help ? h("div", { class: "small muted" }, o.help, o.penalty ? ` Penalty: ${o.penalty}` : "") : null,
      o.notes ? h("div", { class: "small" }, "📝 ", o.notes) : null,
      h("div", { class: "row wrap" },
        opts.hideClient ? null : h("a", { class: "btn sm", href: `#/clients/${o.client_id}` }, "Open client"),
        o.missing.length && !done && o.stage !== "upcoming" ? h("button", { class: "btn sm", onclick: () => openChase(o.client_id, after) }, "Ask client") : null,
        o.status === "open" && o.stage !== "ready" ? h("button", { class: "btn sm", onclick: () => act(o, "start", after) }, "Start anyway") : null,
        can("approve") && ["review", "approved"].includes(o.status) ? h("button", { class: "btn sm", onclick: () => act(o, "send_back", after) }, "Send back") : null,
        can("approve") && !done ? h("button", { class: "btn sm ghost", onclick: () => act(o, "mark_na", after) }, "Not applicable") : null,
        can("approve") && done ? h("button", { class: "btn sm ghost", onclick: () => act(o, "reopen", after) }, "Reopen") : null,
        h("select", { class: "input right", style: "width:auto", onchange: e => safely(async () => {
          await api("POST", `/api/obligations/${o.id}/assign`, { assignee_id: e.target.value ? +e.target.value : null });
          toast("Reassigned"); after && after();
        }) }, h("option", { value: "" }, "Unassigned"), S.boot.users.map(u => h("option", { value: u.id, selected: u.id === o.assignee_id }, u.name)))));
    wrap.append(det);
  });
  return wrap;
}

// ----------------------------------------------------------------- chase
async function openChase(clientId, after) {
  await safely(async () => {
    const m = await api("POST", `/api/clients/${clientId}/chase`);
    openMessage(m, after);
  });
}
function openMessage(m, after) {
  const ta = h("textarea", { class: "input msg-body", rows: 14 }, m.body);
  const save = () => api("POST", `/api/messages/${m.id}`, { action: "save", body: ta.value });
  const sent = async () => { await api("POST", `/api/messages/${m.id}`, { action: "sent", body: ta.value }); closeDrawer(); toast("Marked as sent"); refreshCounts(); after && after(); };
  const viaApi = async () => safely(async () => {
    await api("POST", `/api/messages/${m.id}`, { action: "send_whatsapp", body: ta.value });
    closeDrawer(); toast("Sent on WhatsApp"); refreshCounts(); after && after();
  });
  const mayDo = can("send_messages");
  drawer(`Document request · ${m.client_name}`, [
    h("div", { class: "hint" }, "Nothing is sent automatically. Edit if you like, then send."),
    m.api_send && !m.window_open ? h("div", { class: "banner warn", style: "margin:0" }, "This client hasn't messaged in 24 h — WhatsApp only allows an approved template from the office number. “Open WhatsApp” sends from your phone instead.") : null,
    mayDo ? null : h("div", { class: "banner warn", style: "margin:0" }, "You can edit this draft; someone with permission to message clients will send it."),
    ta,
    h("div", { class: "small muted" }, m.phone ? `WhatsApp: +91 ${m.phone}` : "No phone on file", " · ", m.email || "no email on file"),
  ], [
    h("button", { class: "btn danger ghost", onclick: async () => { await api("POST", `/api/messages/${m.id}`, { action: "discard" }); closeDrawer(); refreshCounts(); after && after(); } }, "Discard"),
    h("button", { class: "btn", onclick: async () => { await navigator.clipboard.writeText(ta.value).catch(() => {}); await save(); toast("Copied"); } }, "Copy"),
    m.email ? h("button", { class: "btn", onclick: async () => { await save(); location.href = `mailto:${m.email}?subject=${encodeURIComponent(m.subject || "")}&body=${encodeURIComponent(ta.value)}`; } }, "Email") : null,
    m.phone && mayDo ? h("button", { class: "btn" + (m.api_send ? "" : " primary"), onclick: async () => {
      await save(); window.open(`https://wa.me/91${m.phone}?text=${encodeURIComponent(ta.value)}`, "_blank");
      if (confirm("Did the WhatsApp message go out?")) sent();
    } }, "Open WhatsApp") : null,
    m.api_send && mayDo ? h("button", { class: "btn primary", onclick: viaApi }, "Send on WhatsApp") : null,
    mayDo ? h("button", { class: "btn" + (m.phone ? "" : " primary"), onclick: sent }, "Mark sent") : h("button", { class: "btn primary", onclick: async () => { await save(); closeDrawer(); toast("Draft saved"); } }, "Save draft"),
  ]);
}

// ----------------------------------------------------------------- TODAY
async function viewToday(page) {
  const d = await refreshCounts();
  if (!S.boot.has_clients) return viewWelcome(page);
  const filter = store.get("todayFilter", null);
  const c = d.counts;
  const mineFirst = can("approve") ? c.review : c.ready + c.in_progress + c.approved;
  const stats = [
    ["overdue", "red", c.overdue, "Overdue"], ["week", "amber", c.week, "Due in 7 days"],
    ["awaiting_docs", "", c.awaiting_docs, "Waiting on clients"], ["ready", "blue", c.ready + c.in_progress, "Ready / preparing"],
    ["review", "violet", c.review, "Partner review"], ["approved", "green", c.approved, "Approved · to file"],
  ];
  const match = o => !filter ? true : filter === "overdue" ? o.days_left < 0 : filter === "week" ? o.days_left >= 0 && o.days_left <= 7
    : filter === "ready" ? ["ready", "in_progress"].includes(o.stage) : o.stage === filter;
  const items = d.items.filter(match);
  const groups = [];
  if (can("approve") && !filter) {
    const r = items.filter(o => o.stage === "review");
    if (r.length) groups.push(["Needs your approval", r]);
  }
  const rest = items.filter(o => !(can("approve") && !filter && o.stage === "review"));
  groups.push(["Overdue", rest.filter(o => o.days_left < 0)], ["This week", rest.filter(o => o.days_left >= 0 && o.days_left <= 7)],
    ["Next 30 days", rest.filter(o => o.days_left > 7)]);
  const rerender = () => route();
  fill(page, 
    h("div", { class: "row wrap" },
      h("div", { class: "grow" }, h("h1", {}, `${greet()}, ${firstName(S.me.name)}.`),
        h("div", { class: "muted" }, mineFirst ? `${mineFirst} item${mineFirst === 1 ? " is" : "s are"} waiting on you. ` : "Nothing is waiting on you right now. ",
          c.inbox ? `${c.inbox} file${c.inbox === 1 ? "" : "s"} need a quick look in the Inbox.` : "", d.filed_this_month ? ` ${d.filed_this_month} filed this month.` : "")),
      h("button", { class: "btn", onclick: () => $("#file-input").click() }, "⇪ Drop files"),
      h("a", { class: "btn primary", href: "#/clients/new" }, "+ Client")),
    h("div", { class: "stats" }, stats.map(([k, col, n, label]) =>
      h("button", { class: `stat ${col} ${filter === k ? "on" : ""}`, onclick: () => { store.set("todayFilter", filter === k ? null : k); rerender(); } },
        h("b", {}, n), h("span", {}, label)))),
    filter ? h("div", { class: "row", style: "margin:-8px 0 14px" }, h("span", { class: "muted" }, `Showing: ${stats.find(s => s[0] === filter)[3]}`),
      h("button", { class: "btn sm ghost", onclick: () => { store.set("todayFilter", null); rerender(); } }, "Clear ✕")) : null,
    ...groups.filter(([, xs]) => xs.length).map(([title, xs]) => h("div", { class: "group" },
      h("div", { class: "row" }, h("h3", {}, title), h("span", { class: "muted small" }, xs.length)),
      h("div", { class: "card list" }, xs.map(o => obligationRow(o, rerender))))),
    items.length ? null : h("div", { class: "card empty" }, h("div", { class: "big" }, "🌤"), "Nothing here. Enjoy the calm."),
  );
}

function viewWelcome(page) {
  fill(page, h("div", { class: "card hero" },
    h("div", { class: "big" }, "₹"),
    h("h1", {}, "Your office's compliance brain"),
    h("p", { class: "muted" }, "Dump your client data once. It sorts every file, builds each client's FY compliance calendar and tells your team exactly what to do next."),
    h("div", { class: "steps" },
      h("div", { class: "card" }, h("b", {}, "1 · Add clients"), h("span", { class: "muted" }, "Paste a GSTIN or PAN — entity, state and filings are worked out for you. Or paste your whole client list.")),
      h("div", { class: "card" }, h("b", {}, "2 · Drop everything"), h("span", { class: "muted" }, "Folders, zips, WhatsApp downloads. Files are matched to client, type and month.")),
      h("div", { class: "card" }, h("b", {}, "3 · Follow the list"), h("span", { class: "muted" }, "Today shows what's due, what's missing and who needs to act. Partners approve before anything is filed."))),
    h("div", { class: "row", style: "justify-content:center" },
      h("a", { class: "btn primary", href: "#/clients/new" }, "+ Add first client"),
      h("a", { class: "btn", href: "#/clients/bulk" }, "Paste client list"),
      S.boot.can_load_demo ? h("button", { class: "btn ghost", onclick: () => safely(async () => {
        const r = await api("POST", "/api/demo"); toast("Demo office loaded"); await boot();
        if (r.logins.length) drawer("Demo team logins", [h("div", { class: "hint" }, "Sign in as these people in another browser to see the office from their side. Priya only sees clients assigned to her."),
          h("table", { class: "table" }, r.logins.map(l => h("tr", {}, h("td", {}, h("b", {}, l.name), h("div", { class: "small muted" }, l.role)), h("td", {}, l.email), h("td", {}, h("code", {}, l.password)))))]);
      }) }, "Try with demo data") : null)));
}

// ----------------------------------------------------------------- INBOX
function uploadXHR(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload");
    x.upload.onprogress = e => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => {
      let data = {}; try { data = JSON.parse(x.responseText); } catch { /* non-JSON error page */ }
      if (x.status === 401) { showAuth(); return reject(new Error("Please sign in")); }
      x.status < 300 ? resolve(data) : reject(new Error(typeof data.detail === "string" ? data.detail : `Upload failed (${x.status})`));
    };
    x.onerror = () => reject(new Error("Network error during upload — nothing was lost, try again"));
    x.send(fd);
  });
}

async function upload(files, clientId) {
  if (!files.length) return;
  const fd = new FormData();
  let bytes = 0;
  for (const f of files) { fd.append("files", f, f.webkitRelativePath || f.name); bytes += f.size; }
  if (clientId) fd.append("client_id", clientId);
  const bar = h("i", { style: "width:0%" });
  const label = h("span", {}, `Uploading ${files.length} file${files.length === 1 ? "" : "s"} (${(bytes / 1048576).toFixed(1)} MB)…`);
  const box = h("div", { class: "uploading card pad" }, label, h("div", { class: "progress", style: "margin-top:8px" }, bar));
  document.body.append(box);
  try {
    const r = await uploadXHR(fd, p => { bar.style.width = `${Math.round(p * 100)}%`; if (p >= 1) label.textContent = "Sorting files…"; });
    store.set("lastBatch", r);
    toast(`${r.auto} filed automatically${r.review ? ` · ${r.review} need a look` : ""}${r.reading ? ` · reading ${r.reading} scan${r.reading > 1 ? "s" : ""}` : ""}${r.duplicates ? ` · ${r.duplicates} already had` : ""}`);
    if (!clientId && !location.hash.startsWith("#/inbox")) location.hash = "#/inbox"; else route();
    return r;
  } catch (e) { toast(e.message, true); } finally { box.remove(); }
}

function periodPicker(d) {
  const months = [];
  for (const f of S.boot.fys) for (let i = 0; i < 12; i++) {
    const m = (3 + i) % 12 + 1, y = f.fy + (i >= 9 ? 1 : 0);
    months.push([`${y}-${String(m).padStart(2, "0")}`, `${MON[m - 1]} ${y}`]);
  }
  const mk = (val) => h("select", { class: "input" }, h("option", { value: "" }, "—"), months.map(([k, l]) => h("option", { value: k, selected: k === val }, l)));
  const a = mk(d.period_start), b = mk(d.period_end);
  const quick = h("select", { class: "input", onchange: e => {
    const v = e.target.value; if (!v) return;
    const [fy, q] = v.split(":"); const i = months.findIndex(m => m[0] === `${fy}-04`);
    const [s, n] = q === "FY" ? [0, 12] : [(+q - 1) * 3, 3];
    a.value = months[i + s][0]; b.value = months[i + s + n - 1][0];
  } }, h("option", { value: "" }, "Quick pick…"), S.boot.fys.map(f => [
    h("option", { value: `${f.fy}:FY` }, f.label), [1, 2, 3, 4].map(q => h("option", { value: `${f.fy}:${q}` }, `Q${q} ${f.label}`))]));
  return { el: h("div", { class: "grid3" }, a, b, quick), get: () => ({ period_start: a.value || null, period_end: b.value || a.value || null }) };
}

async function viewInbox(page) {
  const [review, clients] = await Promise.all([api("GET", "/api/documents?status=needs_review"), api("GET", "/api/clients")]);
  const counts = (await refreshCounts()).counts;
  clearTimeout(S.poll);
  if (counts.reading) S.poll = setTimeout(() => { if (location.hash.startsWith("#/inbox")) route(); }, 3000);
  const last = store.get("lastBatch", null);
  const drop = h("div", { class: "drop", onclick: () => $("#file-input").click() },
    h("div", { class: "big" }, "⇪"), h("h2", {}, "Drop everything here"),
    h("div", { class: "muted" }, "Folders, .zip exports, bank statements, registers, 26AS/AIS, Form 16, Tally exports, WhatsApp photos. ",
      "Each file is matched to a client, document type and month — you only check the doubtful ones."),
    h("div", { class: "row", style: "justify-content:center;margin-top:12px" },
      h("button", { class: "btn primary", onclick: e => { e.stopPropagation(); $("#file-input").click(); } }, "Choose files"),
      h("button", { class: "btn", onclick: e => { e.stopPropagation(); const i = h("input", { type: "file", webkitdirectory: true, multiple: true }); i.onchange = () => upload([...i.files]); i.click(); } }, "Choose a folder")));
  ["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));

  const rows = review.map(d => {
    const cSel = h("select", { class: "input" }, h("option", { value: "" }, "Which client?"), clients.map(c => h("option", { value: c.id, selected: c.id === d.client_id }, c.name)));
    const tSel = h("select", { class: "input" }, Object.entries(S.boot.doc_types).map(([k, l]) => h("option", { value: k, selected: k === d.doc_type }, l)));
    const per = periodPicker(d);
    const reading = ["pending", "running"].includes(d.ocr_status);
    const row = h("div", { class: "rv" + (reading ? " reading" : "") },
      h("div", {},
        h("a", { href: `/api/documents/${d.id}/file`, target: "_blank", class: "t1 ellipsis", style: "display:block;font-weight:600" }, d.filename.split("/").pop()),
        h("div", { class: "small muted ellipsis" }, d.reasons.filter(r => /No |Could not|Partial|please|Assumed|Wide/.test(r)).join(" · ") || d.reasons.join(" · ")),
        reading ? h("div", { class: "small", style: "color:var(--brand)" }, "🔎 Reading the scan…") : h("div", { class: "conf" }, h("i", { style: `width:${Math.round(d.confidence * 100)}%` })),
        d.source === "whatsapp" ? h("span", { class: "pill auth", style: "margin-top:4px" }, "via WhatsApp") : null),
      h("label", { class: "field" }, "Client", cSel),
      h("label", { class: "field" }, "Document", tSel),
      h("label", { class: "field" }, "Period (from · to)", per.el),
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => safely(async () => {
          await api("POST", `/api/documents/${d.id}/confirm`, { client_id: cSel.value ? +cSel.value : null, doc_type: tSel.value, ...per.get() });
          row.remove(); toast("Filed. I'll remember this pattern."); refreshCounts();
        }) }, "File it"),
        h("button", { class: "btn ghost", title: "Not a client document", onclick: () => safely(async () => {
          await api("POST", `/api/documents/${d.id}/reject`); row.remove(); refreshCounts();
        }) }, "Set aside")));
    return row;
  });

  fill(page, 
    h("div", { class: "row" }, h("h1", { class: "grow" }, "Inbox")),
    h("p", { class: "muted" }, "Everything clients send lands here first."),
    drop,
    counts.reading ? h("div", { class: "banner ok", style: "margin-top:16px" }, `🔎 Reading ${counts.reading} scanned page${counts.reading > 1 ? "s" : ""}… files will sort themselves as soon as the text is in.`) : null,
    last ? h("div", { class: "card pad", style: "margin-top:16px" },
      h("div", { class: "row" }, h("b", {}, `Last upload: ${last.total} files`), h("span", { class: "muted right small" }, `${last.auto} auto-filed · ${last.review} to check · ${last.duplicates} duplicates`)),
      h("div", { class: "progress", style: "margin-top:8px" }, h("i", { style: `width:${last.total ? Math.round(100 * (last.auto + last.duplicates) / last.total) : 0}%` })),
      last.skipped && last.skipped.length ? h("details", { style: "margin-top:8px" }, h("summary", { class: "small" }, `${last.skipped.length} skipped — see why`),
        h("ul", { class: "small muted" }, last.skipped.slice(0, 50).map(s => h("li", {}, h("b", {}, s.filename.split("/").pop()), " — ", s.reason)))) : null) : null,
    h("div", { class: "group", style: "margin-top:22px" },
      h("div", { class: "row" }, h("h3", {}, "Needs a quick look"), h("span", { class: "muted small" }, review.length)),
      review.length ? h("div", { class: "card" }, rows) : h("div", { class: "card empty" }, h("div", { class: "big" }, "✓"), "Every file is sorted.")),
  );
}

// -------------------------------------------------------------- CALENDAR
async function viewCalendar(page) {
  const month = store.get("calMonth", S.boot.today.slice(0, 7));
  const [cal, overrides] = await Promise.all([api("GET", `/api/calendar?month=${month}`), api("GET", "/api/overrides")]);
  await refreshCounts();
  const [y, m] = month.split("-").map(Number);
  const shift = n => { const d = new Date(y, m - 1 + n, 1); store.set("calMonth", `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`); route(); };
  const byDay = Object.fromEntries(cal.days.map(d => [d.date, d]));
  const first = new Date(y, m - 1, 1), lead = (first.getDay() + 6) % 7, nDays = new Date(y, m, 0).getDate();
  const cells = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map(d => h("div", { class: "dow" }, d));
  for (let i = 0; i < lead; i++) cells.push(h("div", { class: "day off" }));
  for (let dd = 1; dd <= nDays; dd++) {
    const key = `${month}-${String(dd).padStart(2, "0")}`, info = byDay[key];
    cells.push(h("div", { class: "day" + (key === S.boot.today ? " today" : "") }, h("span", { class: "num" }, dd),
      info ? info.groups.map(g => h("div", { class: `ev ${g.authority === "Income Tax" ? "" : g.authority} ${g.done === g.total ? "done" : ""}`,
        title: `${g.form}: ${g.done}/${g.total} done`, onclick: () => showDay(key, g) },
        h("span", { class: "ellipsis" }, g.form), h("span", {}, `${g.done}/${g.total}`))) : null));
  }
  const fyNow = S.boot.fys.find(f => f.current);
  const pending = overrides.filter(o => o.status === "proposed");
  fill(page, 
    h("div", { class: "row" }, h("h1", { class: "grow" }, "Compliance calendar"),
      h("button", { class: "btn", onclick: () => shift(-1) }, "‹"), h("b", { style: "min-width:120px;text-align:center" }, `${MON[m - 1]} ${y}`),
      h("button", { class: "btn", onclick: () => shift(1) }, "›"),
      h("button", { class: "btn ghost", onclick: () => { store.set("calMonth", S.boot.today.slice(0, 7)); route(); } }, "Today")),
    h("p", { class: "muted" }, "Built from each client's profile for ", S.boot.fys.map(f => f.label).join(" and "), ". Statutory dates by default — government extensions are added below and need a partner's OK."),
    fyNow.signed_off ? h("div", { class: "banner ok" }, `✓ ${fyNow.label} calendar signed off by a partner.`)
      : h("div", { class: "banner warn" }, h("span", { class: "grow" }, `${fyNow.label} calendar hasn't been reviewed by a partner yet. Check due dates and any extensions, then sign off.`),
        can("approve") ? h("button", { class: "btn primary", onclick: () => safely(async () => { await api("POST", `/api/calendar/${fyNow.fy}/signoff`); toast("Signed off"); await boot(); }) }, "Sign off") : null),
    h("div", { class: "cal" }, cells),
    h("div", { class: "group", style: "margin-top:26px" },
      h("div", { class: "row" }, h("h3", { class: "grow" }, "Due-date extensions"),
        h("button", { class: "btn sm", onclick: proposeOverride }, "+ Add extension")),
      h("div", { class: "hint", style: "margin-bottom:10px" }, "Tip: type it in the bar above — “extend GSTR-9 FY 2025-26 to 31 Jan 2027 per notification …”."),
      overrides.length ? h("div", { class: "card" }, h("table", { class: "table" },
        h("tr", {}, h("th", {}, "Compliance"), h("th", {}, "Period"), h("th", {}, "New due date"), h("th", { class: "hide-sm" }, "Reason"), h("th", {}, "")),
        overrides.map(o => h("tr", {},
          h("td", {}, o.rule_name, h("div", { class: "small muted" }, `${o.affected} open filings affected`)),
          h("td", {}, o.fy_label, o.period_key === "*" ? "" : ` · ${o.period_key}`),
          h("td", {}, fmtDate(o.new_due, true)), h("td", { class: "hide-sm small" }, o.reason),
          h("td", {}, o.status === "proposed"
            ? (can("approve") ? h("div", { class: "row" },
              h("button", { class: "btn sm primary", onclick: () => safely(async () => { await api("POST", `/api/overrides/${o.id}/approve`); toast("Calendar updated"); route(); }) }, "Approve"),
              h("button", { class: "btn sm ghost", onclick: () => safely(async () => { await api("POST", `/api/overrides/${o.id}/reject`); route(); }) }, "Reject"))
              : h("span", { class: "pill review" }, "Awaiting partner"))
            : h("span", { class: "pill " + (o.status === "approved" ? "approved" : "na") }, o.status)))))) : h("div", { class: "card empty" }, "No extensions recorded.")),
  );
  if (pending.length && can("approve")) toast(`${pending.length} extension${pending.length > 1 ? "s" : ""} waiting for your approval`);
}

function showDay(key, g) {
  const after = () => { closeDrawer(); route(); };
  drawer(`${g.form} · due ${fmtDate(key, true)}`, h("div", { class: "card list" }, g.items.map(o => obligationRow(o, after))));
}

async function proposeOverride() {
  const sel = h("select", { class: "input" }, S.boot.rules.map(r => h("option", { value: r.code }, `${r.name}`)));
  const fySel = h("select", { class: "input" }, S.boot.fys.map(f => h("option", { value: f.fy, selected: !f.current }, f.label)));
  const per = h("input", { class: "input", value: "*", placeholder: "* = all periods, or 2026-09, Q2, FY" });
  const due = h("input", { class: "input", type: "date" });
  const why = h("input", { class: "input", placeholder: "Notification / circular no. and date" });
  drawer("Add a due-date extension", [
    h("label", { class: "field" }, "Compliance", sel), h("div", { class: "grid2" }, h("label", { class: "field" }, "Financial year", fySel), h("label", { class: "field" }, "Period", per)),
    h("label", { class: "field" }, "New due date", due), h("label", { class: "field" }, "Reason / source", why),
    h("div", { class: "hint" }, "A partner approves before the calendar changes for every affected client."),
  ], [h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
    await api("POST", "/api/overrides", { rule_code: sel.value, fy: +fySel.value, period_key: per.value.trim() || "*", new_due: due.value, reason: why.value });
    closeDrawer(); toast("Sent for approval"); route();
  }) }, "Propose")]);
}

// --------------------------------------------------------------- CLIENTS
async function viewClients(page) {
  const clients = await api("GET", "/api/clients");
  await refreshCounts();
  const q = h("input", { class: "input", placeholder: "Search name, PAN or GSTIN", style: "max-width:320px" });
  const body = h("tbody");
  const draw = () => {
    const s = q.value.toLowerCase();
    fill(body, ...clients.filter(c => !s || [c.name, c.pan, c.gstin, ...c.aliases].join(" ").toLowerCase().includes(s)).map(c =>
      h("tr", { class: "click", onclick: () => location.hash = `#/clients/${c.id}` },
        h("td", {}, h("div", { class: "who" }, h("span", { class: "dot " + c.health }), h("div", {}, h("b", {}, c.name),
          h("div", { class: "small muted" }, S.boot.entity_types[c.entity_type], c.gst_scheme !== "none" ? ` · GST ${c.gst_scheme}` : "")))),
        h("td", { class: "hide-sm small" }, c.pan || "—", h("div", { class: "muted" }, c.gstin || "")),
        h("td", {}, c.overdue ? h("span", { class: "pill awaiting_docs", style: "background:var(--red-soft);color:var(--red)" }, `${c.overdue} overdue`) : null, " ",
          c.awaiting_docs ? h("span", { class: "pill awaiting_docs" }, `${c.awaiting_docs} waiting on docs`) : null,
          !c.overdue && !c.awaiting_docs ? h("span", { class: "small muted" }, "On track") : null,
          c.replies ? h("span", { class: "pill review", style: "margin-left:4px" }, `💬 ${c.replies}`) : null),
        h("td", { class: "hide-sm" }, c.next_due ? fmtDate(c.next_due) : "—"),
        h("td", { class: "hide-sm small muted" }, `${c.docs} files`))));
  };
  q.addEventListener("input", draw);
  draw();
  fill(page, 
    h("div", { class: "row wrap" }, h("h1", { class: "grow" }, "Clients"), q,
      can("export") && clients.length ? h("a", { class: "btn", href: "/api/export", title: "Every client's files as one organised zip" }, "⇩ Zip all") : null,
      can("manage_clients") ? h("a", { class: "btn", href: "#/clients/bulk" }, "Paste list") : null,
      can("manage_clients") ? h("a", { class: "btn primary", href: "#/clients/new" }, "+ Client") : null),
    h("div", { class: "card", style: "margin-top:16px;overflow:auto" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Client"), h("th", { class: "hide-sm" }, "PAN / GSTIN"), h("th", {}, "Health"), h("th", { class: "hide-sm" }, "Next due"), h("th", { class: "hide-sm" }, "Vault"))),
      body)),
    clients.length ? null : h("div", { class: "card empty", style: "margin-top:10px" }, "No clients yet."));
}

const FLAGS = [["tax_audit", "Tax audit"], ["tds", "Deducts TDS"], ["tcs", "Collects TCS"], ["payroll", "Payroll (PF/ESI)"],
  ["advance_tax", "Advance tax"], ["presumptive", "Presumptive (44AD/ADA)"], ["salaried", "Salary income"], ["transfer_pricing", "Transfer pricing"]];

function clientForm(c, onSaved) {
  c = Object.assign({ gst_scheme: "none", entity_type: "", aliases: [] }, c || {});
  const f = {};
  const inp = (k, ph, extra) => f[k] = h("input", Object.assign({ class: "input", value: c[k] ?? "", placeholder: ph || "" }, extra || {}));
  const gstin = inp("gstin", "27ABCDE1234F1Z5 — fills PAN, state, entity");
  const pan = inp("pan", "ABCDE1234F");
  const ent = f.entity_type = h("select", { class: "input" }, h("option", { value: "" }, "Work out from PAN"),
    Object.entries(S.boot.entity_types).map(([k, l]) => h("option", { value: k, selected: k === c.entity_type }, l)));
  const st = f.gst_state = h("select", { class: "input" }, h("option", { value: "" }, "—"),
    Object.entries(S.boot.states).map(([k, l]) => h("option", { value: k, selected: k === c.gst_state }, `${k} · ${l}`)));
  let scheme = c.gst_scheme;
  const seg = h("div", { class: "seg" });
  const drawSeg = () => fill(seg, ...[["none", "No GST"], ["monthly", "Monthly"], ["qrmp", "QRMP"], ["composition", "Composition"]].map(([k, l]) =>
    h("button", { class: scheme === k ? "on" : "", onclick: e => { e.preventDefault(); scheme = k; drawSeg(); preview(); } }, l)));
  drawSeg();
  const toggles = FLAGS.map(([k, l]) => {
    const cb = h("input", { type: "checkbox", checked: c[k] });
    const lab = h("label", { class: "toggle" + (c[k] ? " on" : "") }, cb, l);
    cb.addEventListener("change", () => { lab.classList.toggle("on", cb.checked); preview(); });
    f[k] = cb; return lab;
  });
  const pv = h("div", { class: "hint" });
  const ENT = { P: "individual", H: "huf", F: "firm", C: "company", T: "trust", A: "trust" };
  gstin.addEventListener("input", () => {
    const v = gstin.value.toUpperCase().trim();
    const m = v.match(/^(\d{2})([A-Z]{5}\d{4}[A-Z])[1-9A-Z]Z[0-9A-Z]$/);
    if (m) { pan.value = m[2]; st.value = m[1]; if (scheme === "none") { scheme = "monthly"; drawSeg(); } if (!ent.value) ent.value = ENT[m[2][3]] || ""; }
    preview();
  });
  pan.addEventListener("input", () => { const v = pan.value.toUpperCase(); if (/^[A-Z]{5}\d{4}[A-Z]$/.test(v) && !ent.value) ent.value = ENT[v[3]] || ""; preview(); });
  ent.addEventListener("change", preview);
  const turnover = inp("turnover_cr", "e.g. 3.5", { type: "number", step: "0.01", min: "0" });
  turnover.addEventListener("input", preview);
  function preview() {
    const e = ent.value || "individual", t = +turnover.value || 0, list = [];
    if (scheme === "monthly") list.push("GSTR-1 & 3B monthly"); if (scheme === "qrmp") list.push("GSTR-1/3B quarterly + PMT-06");
    if (scheme === "composition") list.push("CMP-08 + GSTR-4"); if (["monthly", "qrmp"].includes(scheme) && t > 2) list.push("GSTR-9");
    if (["monthly", "qrmp"].includes(scheme) && t > 5) list.push("GSTR-9C");
    if (f.tds.checked) list.push("TDS deposits + quarterly statements"); if (f.payroll.checked) list.push("PF/ESI monthly");
    if (f.advance_tax.checked) list.push("Advance tax"); if (f.tax_audit.checked) list.push("Tax audit");
    list.push("ITR"); if (e === "company") list.push("ROC (AOC-4, MGT-7, ADT-1, DPT-3, DIR-3 KYC)"); if (e === "llp") list.push("LLP Form 8 & 11");
    fill(pv, "📅 I'll schedule: ", list.join(" · "));
  }
  preview();
  const body = [
    h("label", { class: "field" }, "Client name", inp("name", "As per PAN")),
    h("div", { class: "grid2" }, h("label", { class: "field" }, "GSTIN", gstin), h("label", { class: "field" }, "PAN", pan)),
    h("div", { class: "grid2" }, h("label", { class: "field" }, "Entity", ent), h("label", { class: "field" }, "State (GST)", st)),
    h("label", { class: "field" }, "GST filing", seg),
    h("label", { class: "field" }, "Turnover (₹ crore)", turnover),
    h("div", { class: "row wrap" }, toggles),
    pv,
    h("h3", {}, "Contact"),
    h("div", { class: "grid3" }, h("label", { class: "field" }, "Name", inp("contact_name")), h("label", { class: "field" }, "WhatsApp", inp("phone", "98xxxxxxxx")), h("label", { class: "field" }, "Email", inp("email"))),
    h("div", { class: "grid2" },
      h("label", { class: "field" }, "Handled by", f.assignee_id = h("select", { class: "input" }, h("option", { value: "" }, "—"), S.boot.users.map(u => h("option", { value: u.id, selected: u.id === c.assignee_id }, u.name)))),
      h("label", { class: "field" }, "Also known as (for file matching)", f.aliases = h("input", { class: "input", value: (c.aliases || []).join(", "), placeholder: "RKS, Sharma Bros" }))),
    h("label", { class: "field" }, "TAN", inp("tan")),
  ];
  const save = () => safely(async () => {
    const payload = {
      name: f.name.value, gstin: gstin.value || null, pan: pan.value || null, tan: f.tan.value || null, entity_type: ent.value || null,
      gst_state: st.value || null, gst_scheme: scheme, turnover_cr: +turnover.value || 0, contact_name: f.contact_name.value || null,
      phone: f.phone.value || null, email: f.email.value || null, assignee_id: f.assignee_id.value ? +f.assignee_id.value : null,
      aliases: f.aliases.value.split(",").map(s => s.trim()).filter(Boolean),
    };
    FLAGS.forEach(([k]) => payload[k] = f[k].checked);
    const r = c.id ? await api("PUT", `/api/clients/${c.id}`, payload) : await api("POST", "/api/clients", payload);
    closeDrawer(); toast(c.id ? "Saved — calendar updated" : `${r.name} added · filings scheduled`);
    S.boot.has_clients = true; onSaved && onSaved(r);
  });
  drawer(c.id ? `Edit ${c.name}` : "New client", body, [h("button", { class: "btn", onclick: () => { closeDrawer(); if (!c.id) history.back(); } }, "Cancel"), h("button", { class: "btn primary", onclick: save }, c.id ? "Save" : "Add client")]);
}

function bulkClients() {
  const ta = h("textarea", { class: "input", rows: 12, placeholder: "One client per line, in plain words:\nSharma Traders 27ABCPS1234K1Z5 audit tds advance tax 9820012345\nDr Anjali Rao BXRPR2345M salaried advance tax\nIyer & Associates LLP 33AAJFI5678L1Z2 qrmp tds" });
  const out = h("div");
  let specs = [];
  const parse = debounce(() => safely(async () => {
    specs = await api("POST", "/api/clients/preview", { text: ta.value });
    fill(out, ...specs.map(s => h("div", { class: "row small", style: "padding:4px 0;border-bottom:1px solid var(--line)" },
      h("span", {}, s.error ? "⚠" : "✓"), h("b", {}, s.spec.name || "?"),
      h("span", { class: "muted ellipsis grow" }, s.error || [s.spec.pan, S.boot.entity_types[s.spec.entity_type], s.spec.gst_scheme !== "none" ? "GST " + s.spec.gst_scheme : "", ...FLAGS.filter(([k]) => s.spec[k]).map(x => x[1])].filter(Boolean).join(" · ")))));
  }), 400);
  ta.addEventListener("input", parse);
  drawer("Paste your client list", [h("div", { class: "hint" }, "Paste from Excel or type freely. GSTIN/PAN, scheme and flags are picked up from the words."), ta, out],
    [h("button", { class: "btn", onclick: () => { closeDrawer(); history.back(); } }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
      let ok = 0; const errs = [];
      for (const s of specs.filter(s => !s.error)) { try { await api("POST", "/api/clients", s.spec); ok++; } catch (e) { errs.push(`${s.spec.name}: ${e.message}`); } }
      closeDrawer(); toast(`${ok} client${ok === 1 ? "" : "s"} added`); errs.forEach(e => toast(e, true)); S.boot.has_clients = ok > 0 || S.boot.has_clients;
      location.hash = "#/clients";
    }) }, "Add all")]);
}

async function viewClient(page, id) {
  const d = await api("GET", `/api/clients/${id}`);
  const c = d.client;
  const tab = store.get("clientTab", "filings");
  const reload = () => route();
  const open = d.obligations.filter(o => !["filed", "na"].includes(o.status));
  const done = d.obligations.filter(o => ["filed", "na"].includes(o.status)).reverse();
  const unread = d.conversation.filter(m => m.direction === "in" && m.status === "received").length;
  const tabs = [["filings", `Filings (${open.length})`], ["vault", "Document vault"], ["history", `Filed (${done.length})`],
    ["messages", `Messages${unread ? ` (${unread} new)` : ""}`], ["privacy", "Privacy"], ["activity", "Activity"]];
  const up = h("input", { type: "file", multiple: true, class: "hidden", onchange: e => upload([...e.target.files], c.id) });
  let content;
  if (tab === "filings") {
    const soon = open.filter(o => o.days_left <= 45), later = open.filter(o => o.days_left > 45);
    const laterBox = h("div", { class: "card list hidden", style: "margin-top:8px" }, later.map(o => obligationRow(o, reload, { hideClient: true })));
    content = h("div", {},
      soon.length ? h("div", { class: "card list" }, soon.map(o => obligationRow(o, reload, { hideClient: true }))) : h("div", { class: "card empty" }, "Nothing due in the next 45 days."),
      later.length ? h("button", { class: "btn ghost", style: "margin-top:10px", onclick: e => { laterBox.classList.toggle("hidden"); e.target.textContent = laterBox.classList.contains("hidden") ? `Show ${later.length} later this year` : "Hide later filings"; } }, `Show ${later.length} later this year`) : null,
      laterBox);
  }
  if (tab === "history") content = done.length ? h("div", { class: "card list" }, done.map(o => obligationRow(o, reload, { hideClient: true }))) : h("div", { class: "card empty" }, "Nothing filed yet.");
  if (tab === "messages") content = h("div", { class: "card pad convo" }, d.conversation.length ? d.conversation.slice().reverse().map(m =>
    h("div", { class: "bubble " + (m.direction === "in" ? "in" : "out") },
      h("div", {}, m.body), h("div", { class: "small muted" }, (m.sent_at || m.created_at).replace("T", " ").slice(0, 16), m.direction === "out" ? ` · ${m.status}` : ""))) :
    h("span", { class: "muted" }, "No messages yet. Client replies on the office WhatsApp number show up here, and their attachments are filed automatically."));
  if (tab === "messages" && unread) d.conversation.filter(m => m.direction === "in" && m.status === "received").forEach(m => api("POST", `/api/messages/${m.id}`, { action: "read" }).catch(() => {}));
  if (tab === "privacy") content = await clientPrivacy(c, reload);
  if (tab === "activity") content = h("div", { class: "card pad" }, d.activity.length ? d.activity.map(a => h("div", { class: "small", style: "padding:5px 0;border-bottom:1px solid var(--line)" },
    h("span", { class: "muted" }, a.at.replace("T", " ").slice(0, 16), " · "), a.text)) : h("span", { class: "muted" }, "No activity yet."));
  if (tab === "vault") content = h("div", {}, d.unsorted.length ? h("div", { class: "banner warn" }, `${d.unsorted.length} file(s) for this client need a look in the Inbox.`, h("a", { class: "btn sm right", href: "#/inbox" }, "Open Inbox")) : null,
    d.vault.slice().reverse().map(v => h("div", { class: "group" }, h("h3", { style: "margin-bottom:8px" }, v.label),
      v.rows.length ? h("div", { class: "card vault" }, h("table", {},
        h("tr", {}, h("th", {}, "Document"), v.months.map(m => h("th", {}, m)), h("th", {}, "Files")),
        v.rows.map(r => h("tr", {}, h("td", {}, r.label), Object.values(r.months).map(y => h("td", {}, h("span", { class: "tick " + (y ? "y" : "n") }, y ? "✓" : "·"))),
          h("td", {}, r.docs.length ? h("a", { href: "#", onclick: e => { e.preventDefault(); showDocs(r.label, r.docs); } }, r.docs.length) : "—"))))) : h("div", { class: "card empty" }, "No documents for this year yet."))));
  fill(page, 
    h("a", { href: "#/clients", class: "small" }, "← All clients"),
    h("div", { class: "row wrap", style: "margin-top:8px" },
      h("div", { class: "avatar", style: "width:44px;height:44px;font-size:15px" }, initials(c.name)),
      h("div", { class: "grow" }, h("h1", {}, c.name, " ", consentPill(c)),
        h("div", { class: "muted small" }, [S.boot.entity_types[c.entity_type], c.pan, c.gstin, c.gst_scheme !== "none" ? `GST ${c.gst_scheme}` : null,
          c.gst_state ? S.boot.states[c.gst_state] : null, ...FLAGS.filter(([k]) => c[k]).map(x => x[1])].filter(Boolean).join(" · "))),
      up,
      h("button", { class: "btn", onclick: () => up.click() }, "⇪ Add files"),
      h("button", { class: "btn", onclick: () => openChase(c.id, reload) }, "✉ Ask for documents"),
      can("export") ? h("a", { class: "btn", href: `/api/clients/${c.id}/export`, title: "All files, sorted by FY and document type, with an index and the filing register" }, "⇩ Download zip") : null,
      can("manage_clients") ? h("button", { class: "btn", onclick: () => clientForm(c, reload) }, "Edit") : null),
    h("div", { class: "tabs" }, tabs.map(([k, l]) => h("button", { class: tab === k ? "on" : "", onclick: () => { store.set("clientTab", k); reload(); } }, l))),
    content);
}

function showDocs(label, docs) {
  drawer(label, h("div", { class: "card list" }, docs.map(dc => h("div", { class: "item", style: "grid-template-columns:1fr auto" },
    h("div", {}, h("a", { href: `/api/documents/${dc.id}/file`, target: "_blank", class: "t1" }, dc.filename.split("/").pop()),
      h("div", { class: "t2" }, dc.period_label || "", " · ", dc.status === "confirmed" ? "checked by a person" : `auto-filed (${Math.round(dc.confidence * 100)}%)`)),
    h("span", { class: "small muted" }, `${Math.max(1, Math.round(dc.size / 1024))} KB`)))));
}

// ---------------------------------------------------------------- OUTBOX
async function viewOutbox(page) {
  const msgs = await api("GET", "/api/messages");
  await refreshCounts();
  const inbound = msgs.filter(m => m.direction === "in" && m.status === "received");
  const out = msgs.filter(m => m.direction === "out");
  const markRead = m => safely(async () => { await api("POST", `/api/messages/${m.id}`, { action: "read" }); route(); });
  fill(page, h("h1", {}, "Outbox"),
    h("p", { class: "muted" }, "Document requests drafted by the brain, and what clients wrote back. Nothing leaves the office until someone here sends it.",
      S.boot.whatsapp ? " WhatsApp Business is connected — client replies and attachments arrive here automatically." : ""),
    inbound.length ? h("div", { class: "group" }, h("div", { class: "row" }, h("h3", {}, "New from clients"), h("span", { class: "muted small" }, inbound.length)),
      h("div", { class: "card list" }, inbound.map(m => h("div", { class: "item", style: "grid-template-columns:44px 1fr auto auto" },
        h("div", { class: "avatar" }, initials(m.client_name || "?")),
        h("div", { class: "grow" }, h("div", { class: "t1" }, m.client_name || `+91 ${m.from_phone} (not a client yet)`), h("div", { class: "t2" }, m.body)),
        m.client_id ? h("a", { class: "btn sm", href: `#/clients/${m.client_id}` }, "Open") : h("a", { class: "btn sm", href: "#/inbox" }, "Inbox"),
        h("button", { class: "btn sm ghost", onclick: e => { e.stopPropagation(); markRead(m); } }, "Done"))))) : null,
    h("div", { class: "group" }, h("h3", { style: "margin-bottom:8px" }, "Requests to clients"),
      out.length ? h("div", { class: "card list" }, out.map(m => h("div", { class: "item", style: "grid-template-columns:44px 1fr auto auto", onclick: () => openMessage(m, route) },
        h("div", { class: "avatar" }, initials(m.client_name || "?")),
        h("div", { class: "grow" }, h("div", { class: "t1" }, m.client_name), h("div", { class: "t2 ellipsis" }, (m.body || "").split("\n").filter(l => l.startsWith("•")).join("  "))),
        h("span", { class: "pill " + (m.status === "sent" ? "filed" : "awaiting_docs") }, m.status === "sent" ? `Sent ${m.sent_at.slice(5, 10).split("-").reverse().join("/")}` : "Draft"),
        h("span", { class: "small muted" }, m.created_at.slice(0, 10))))) : h("div", { class: "card empty" }, h("div", { class: "big" }, "✉"), "No drafts. Use “Ask client” on any filing.")));
}

// -------------------------------------------------------------- SETTINGS
async function viewSettings(page) {
  const [rules, act, users] = await Promise.all([api("GET", "/api/rules"), api("GET", "/api/activity"), api("GET", "/api/users")]);
  const admin = can("manage_team");
  const firm = h("input", { class: "input", value: S.boot.firm_name, disabled: !admin || null });
  const start = h("input", { class: "input", type: "date", value: S.boot.tracking_start, disabled: !admin || null });
  const hook = `${location.origin}/api/whatsapp/webhook`;
  const yes = (ok, a, b) => h("div", { class: "small", style: "padding:3px 0" }, ok ? "✓ " : "○ ", ok ? a : b);
  fill(page, h("h1", {}, "Settings"),
    h("div", { class: "grid2", style: "margin-top:16px;align-items:start" },
      h("div", { class: "card pad" }, h("h2", {}, "Office"),
        h("label", { class: "field" }, "Firm name (used in client messages)", firm),
        h("label", { class: "field", style: "margin-top:10px" }, "Track filings due from (older ones are treated as already handled)", start),
        admin ? h("button", { class: "btn primary", style: "margin-top:10px", onclick: () => safely(async () => { await api("POST", "/api/settings", { firm_name: firm.value, tracking_start: start.value || null }); toast("Saved"); boot(); }) }, "Save")
          : h("div", { class: "small muted", style: "margin-top:8px" }, "Only people who manage the team can change these.")),
      h("div", { class: "card pad" }, h("h2", {}, "Connections"),
        yes(S.boot.llm, "AI command bar connected (Claude). Every change it suggests still needs a click.", "AI command bar: built-in rules only. Set ANTHROPIC_API_KEY for free-form questions."),
        yes(S.boot.ocr.tesseract, "Scanned documents are read on this server (Tesseract).", "Tesseract not installed — scans are sorted by file name only."),
        yes(S.boot.ocr.vision, "Hard-to-read scans fall back to Claude vision (sends the file to Anthropic).", "Claude vision OCR off (set CABOT_VISION_OCR=1 to enable)."),
        yes(S.boot.whatsapp, "WhatsApp Business connected — replies and attachments flow in automatically.", "WhatsApp Business not connected — use “Open WhatsApp” links."),
        h("div", { class: "small muted", style: "margin-top:6px" }, "WhatsApp webhook URL for Meta: ", h("code", {}, hook)))),
    h("div", { class: "group", style: "margin-top:22px" },
      h("div", { class: "row" }, h("h3", { class: "grow" }, "Team & permissions"),
        admin ? h("button", { class: "btn sm primary", onclick: inviteUser }, "+ Invite") : null),
      h("div", { class: "card", style: "overflow:auto;margin-top:8px" }, h("table", { class: "table" },
        h("tr", {}, h("th", {}, "Person"), h("th", {}, "Role"), Object.keys(S.boot.perms).map(k => h("th", { class: "hide-sm", title: S.boot.perms[k], style: "text-align:center" }, PERM_SHORT[k])), h("th", {}, "")),
        users.map(u => teamRow(u, admin)))),
      h("div", { class: "small muted", style: "margin-top:6px" }, "Partners always have every permission. Hover a column for details.")),
    h("div", { class: "group" }, h("h3", { style: "margin-bottom:8px" }, "Compliance rules in the calendar"),
      h("div", { class: "card", style: "overflow:auto" }, h("table", { class: "table" },
        h("tr", {}, h("th", {}, "Compliance"), h("th", {}, "Authority"), h("th", { class: "hide-sm" }, "Notes")),
        rules.map(r => h("tr", {}, h("td", {}, h("b", {}, r.name)), h("td", {}, r.authority), h("td", { class: "small muted hide-sm" }, r.help, r.penalty ? ` Penalty: ${r.penalty}` : "")))))),
    h("div", { class: "group" }, h("h3", { style: "margin-bottom:8px" }, "Activity log"),
      h("div", { class: "card pad" }, act.slice(0, 40).map(a => h("div", { class: "small", style: "padding:4px 0;border-bottom:1px solid var(--line)" }, h("span", { class: "muted" }, a.at.replace("T", " ").slice(0, 16), " · "), a.text)))));
}

const PERM_SHORT = { approve: "Approve", manage_clients: "Clients", send_messages: "Message", all_clients: "All clients", export: "Export", manage_team: "Team" };

function teamRow(u, admin) {
  const isPartner = u.role === "partner";
  const save = patch => safely(async () => { await api("PATCH", `/api/users/${u.id}`, patch); toast("Access updated"); await boot(); });
  return h("tr", { style: u.active ? "" : "opacity:.5" },
    h("td", {}, h("div", { class: "who" }, h("span", { class: "avatar" }, initials(u.name)), h("div", {}, h("b", {}, u.name), u.email ? h("div", { class: "small muted" }, u.email)
      : admin ? h("a", { href: "#", class: "small", onclick: async e => { e.preventDefault();
          const v = await ask(`Sign-in email for ${u.name}`, [{ name: "email", label: "Email", type: "email", required: true }], "Save");
          if (v) save({ email: v.email }); } }, "Add sign-in email") : h("div", { class: "small muted" }, "no login yet")))),
    h("td", {}, admin && u.id !== S.me.id ? h("select", { class: "input", style: "width:auto", onchange: e => save({ role: e.target.value }) },
      ["staff", "partner"].map(r => h("option", { value: r, selected: r === u.role }, r === "partner" ? "Partner" : "Staff"))) : (isPartner ? "Partner" : "Staff")),
    Object.keys(S.boot.perms).map(k => h("td", { class: "hide-sm", style: "text-align:center" },
      h("input", { type: "checkbox", checked: u.perms.includes(k), disabled: !admin || isPartner || null, title: S.boot.perms[k],
        onchange: e => save({ perms: e.target.checked ? [...u.perms, k] : u.perms.filter(x => x !== k) }) }))),
    h("td", {}, admin && u.id !== S.me.id ? h("div", { class: "row" },
      h("button", { class: "btn sm ghost", onclick: () => safely(async () => {
        if (!confirm(`Reset ${u.name}'s password? They'll be signed out everywhere.`)) return;
        const r = await api("POST", `/api/users/${u.id}/reset`); showTempPassword(u.name, u.email, r.temp_password);
      }) }, "Reset password"),
      h("button", { class: "btn sm ghost" + (u.active ? " danger" : ""), onclick: () => save({ active: !u.active }) }, u.active ? "Deactivate" : "Reactivate")) : null));
}

function showTempPassword(name, email, pw) {
  drawer(`Sign-in for ${name}`, [
    h("div", { class: "hint" }, "Share this once, in person or on a call. It won't be shown again; they'll set their own password on first sign-in."),
    h("div", { class: "card pad" }, h("div", { class: "small muted" }, "Email"), h("b", {}, email), h("div", { class: "small muted", style: "margin-top:8px" }, "Temporary password"),
      h("div", { style: "font:600 22px ui-monospace,monospace;letter-spacing:.04em" }, pw)),
    h("button", { class: "btn", onclick: () => navigator.clipboard.writeText(`${location.origin}\nEmail: ${email}\nPassword: ${pw}`).then(() => toast("Copied")) }, "Copy sign-in details")]);
}

function inviteUser() {
  const name = h("input", { class: "input", placeholder: "Priya Shah" });
  const email = h("input", { class: "input", type: "email", placeholder: "priya@yourfirm.in" });
  const role = h("select", { class: "input" }, h("option", { value: "staff" }, "Staff"), h("option", { value: "partner" }, "Partner"));
  const DEFAULT = ["manage_clients", "send_messages", "all_clients", "export"];
  const boxes = Object.entries(S.boot.perms).map(([k, label]) => {
    const cb = h("input", { type: "checkbox", checked: DEFAULT.includes(k), value: k });
    return h("label", { class: "toggle" + (DEFAULT.includes(k) ? " on" : "") }, cb, label);
  });
  boxes.forEach(b => b.querySelector("input").addEventListener("change", e => b.classList.toggle("on", e.target.checked)));
  const permWrap = h("div", { style: "display:grid;gap:6px" }, boxes);
  role.addEventListener("change", () => permWrap.classList.toggle("hidden", role.value === "partner"));
  drawer("Invite a colleague", [
    h("div", { class: "grid2" }, h("label", { class: "field" }, "Name", name), h("label", { class: "field" }, "Email (their sign-in)", email)),
    h("label", { class: "field" }, "Role", role), h("div", { class: "small muted" }, "What can they do?"), permWrap,
  ], [h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
    const perms = boxes.map(b => b.querySelector("input")).filter(i => i.checked).map(i => i.value);
    const r = await api("POST", "/api/users", { name: name.value, email: email.value, role: role.value, perms });
    await boot(); showTempPassword(r.user.name, r.user.email, r.temp_password);
  }) }, "Invite")]);
}

function changePassword(forced) {
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const nw = h("input", { class: "input", type: "password", autocomplete: "new-password", placeholder: "At least 8 characters" });
  const go = () => safely(async () => {
    await api("POST", "/api/auth/password", { current: cur.value, new: nw.value });
    closeDrawer(); toast("Password changed"); if (forced) boot();
  });
  const dr = drawer(forced ? "Set your own password" : "Change password", [
    forced ? h("div", { class: "hint" }, "You signed in with a temporary password. Choose your own to continue.") : null,
    h("label", { class: "field" }, forced ? "Temporary password" : "Current password", cur), h("label", { class: "field" }, "New password", nw),
  ], [forced ? null : h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: go }, "Save")]);
  dr.addEventListener("keydown", e => e.key === "Enter" && go());
  if (forced) { document.querySelector(".scrim").onclick = null; dr.querySelector("header button").remove(); }
}

// --------------------------------------------------------------- PRIVACY
const CONSENT = { given: ["approved", "Consent ✓"], withdrawn: ["awaiting_docs", "Consent withdrawn"], pending: ["na", "Consent pending"] };
function consentPill(c) {
  const [cls, label] = CONSENT[c.consent_status || "pending"] || CONSENT.pending;
  return h("span", { class: `pill ${cls}`, style: "font-size:12px;vertical-align:middle", title: "DPDP consent status — see the Privacy tab" }, label);
}

async function clientPrivacy(c, reload) {
  const d = await api("GET", `/api/clients/${c.id}/privacy`);
  const sum = d.summary;
  const sendNotice = () => safely(async () => { const m = await api("POST", `/api/clients/${c.id}/privacy-notice`); openMessage(m, reload); });
  const recordConsent = () => {
    const ch = h("select", { class: "input" }, ["WhatsApp reply", "Signed engagement letter", "Email", "In person"].map(x => h("option", {}, x)));
    const boxes = d.purposes.map(p => { const cb = h("input", { type: "checkbox", checked: true, value: p }); return [cb, h("label", { class: "toggle on" }, cb, p)]; });
    const minor = h("input", { type: "checkbox", checked: !!c.is_minor });
    const guardian = h("input", { class: "input", value: c.guardian_name || "", placeholder: "Parent / lawful guardian giving consent" });
    const notes = h("input", { class: "input", placeholder: "e.g. replied “I agree” on 22 Oct" });
    drawer(`Record consent · ${c.name}`, [
      h("div", { class: "hint" }, "Record consent only after the client has received the privacy notice and agreed (DPDP s.5–6)."),
      h("label", { class: "field" }, "How was consent given?", ch),
      h("div", { class: "small muted" }, "Purposes consented to"), h("div", { style: "display:grid;gap:6px" }, boxes.map(b => b[1])),
      h("label", { class: "toggle" }, minor, "Client is a minor (under 18)"), h("label", { class: "field" }, "Guardian (required for minors — s.9)", guardian),
      h("label", { class: "field" }, "Notes", notes),
    ], [h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
      await api("POST", `/api/clients/${c.id}/consent`, { action: "given", channel: ch.value, notes: notes.value,
        purposes: boxes.filter(b => b[0].checked).map(b => b[0].value), is_minor: minor.checked, guardian_name: guardian.value });
      closeDrawer(); toast("Consent recorded"); reload();
    }) }, "Record consent")]);
  };
  const withdraw = async () => {
    const v = await ask(`Withdraw consent · ${c.name}`, [{ name: "notes", label: "How did they withdraw? (date, channel)", required: true }], "Record withdrawal");
    if (v) safely(async () => { await api("POST", `/api/clients/${c.id}/consent`, { action: "withdrawn", notes: v.notes }); toast("Withdrawal recorded — no more chasing or uploads for this client"); reload(); });
  };
  const logRequest = () => newRequest(c, reload);
  const erase = async () => {
    const v = await ask(`Erase ${c.name}'s personal data`, [{ name: "confirm", label: `Type “${c.name}” to confirm`, required: true }], "Erase");
    if (v) safely(async () => { const r = await api("POST", `/api/clients/${c.id}/erase`, { confirm_name: v.confirm }); drawer("Erasure done", h("p", {}, r.message)); });
  };
  return h("div", { style: "display:grid;gap:14px" },
    h("div", { class: "card pad" },
      h("div", { class: "row wrap" }, h("h2", { class: "grow", style: "margin:0" }, "Notice & consent ", consentPill(c)),
        h("button", { class: "btn sm", onclick: sendNotice }, "Send privacy notice"),
        h("button", { class: "btn sm primary", onclick: recordConsent }, "Record consent"),
        c.consent_status !== "withdrawn" ? h("button", { class: "btn sm ghost danger", onclick: withdraw }, "Withdraw") : null),
      h("div", { class: "small muted", style: "margin-top:6px" }, c.notice_at ? `Notice sent ${c.notice_at.slice(0, 10)}` : "Privacy notice not sent yet",
        c.is_minor ? ` · Minor — guardian: ${c.guardian_name || "not recorded"}` : ""),
      d.log.length ? h("table", { class: "table", style: "margin-top:8px" }, d.log.map(l => h("tr", {},
        h("td", { class: "small" }, l.at.replace("T", " ").slice(0, 16)), h("td", {}, l.action.replace("_", " ")), h("td", { class: "small muted" }, [l.channel, l.notes].filter(Boolean).join(" · ")), h("td", { class: "small muted" }, l.by_name || "")))) : null),
    h("div", { class: "card pad" },
      h("div", { class: "row wrap" }, h("h2", { class: "grow", style: "margin:0" }, "What we hold (access summary, s.11)"),
        can("export") ? h("a", { class: "btn sm", href: `/api/clients/${c.id}/export` }, "⇩ Copy of their data (zip)") : null),
      h("ul", { class: "small" }, sum.documents.map(x => h("li", {}, `${x.type}: ${x.count} file(s), ${x.covering}`))),
      h("div", { class: "small muted" }, "Shared with: ", sum.shared_with.join("; "))),
    h("div", { class: "card pad" },
      h("div", { class: "row wrap" }, h("h2", { class: "grow", style: "margin:0" }, "Rights requests"),
        h("button", { class: "btn sm", onclick: logRequest }, "+ Log a request"),
        can("manage_team") ? h("button", { class: "btn sm ghost danger", onclick: erase }, "Erase personal data") : null),
      d.requests.length ? h("table", { class: "table", style: "margin-top:8px" }, d.requests.map(r => h("tr", {},
        h("td", {}, r.kind), h("td", { class: "small" }, `received ${r.received_at.slice(0, 10)} · due ${r.due_at}`), h("td", {}, h("span", { class: "pill " + (r.status === "open" ? "awaiting_docs" : "filed") }, r.status)), h("td", { class: "small muted" }, r.response || "")))) :
        h("div", { class: "small muted", style: "margin-top:6px" }, "None. Log access, correction, erasure, withdrawal, grievance or nomination requests here so they're answered on time.")));
}

async function newRequest(client, after) {
  const kinds = { access: "Access to their data", correction: "Correction / update", erasure: "Erasure", withdraw: "Withdraw consent", grievance: "Grievance", nomination: "Nomination" };
  const kind = h("select", { class: "input" }, Object.entries(kinds).map(([k, l]) => h("option", { value: k }, l)));
  const who = h("input", { class: "input", value: client ? client.name : "", placeholder: "Name of the person asking" });
  const ch = h("select", { class: "input" }, ["WhatsApp", "Email", "Letter", "In person", "Phone"].map(x => h("option", {}, x)));
  const det = h("textarea", { class: "input", rows: 3, placeholder: "What exactly did they ask for?" });
  const rec = h("input", { class: "input", type: "date", value: S.boot.today });
  drawer("Log a data protection request", [
    h("div", { class: "hint" }, "Logged requests get a response deadline from your privacy settings and show on the Privacy page until closed."),
    h("label", { class: "field" }, "Type", kind), h("label", { class: "field" }, "Requested by", who),
    h("div", { class: "grid2" }, h("label", { class: "field" }, "Received via", ch), h("label", { class: "field" }, "Received on", rec)),
    h("label", { class: "field" }, "Details", det),
  ], [h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
    const r = await api("POST", "/api/privacy/requests", { kind: kind.value, client_id: client ? client.id : null, requester: who.value, details: det.value, channel: ch.value, received_at: rec.value });
    closeDrawer(); toast(`Logged — respond by ${fmtDate(r.due_at, true)}`); after && after();
  }) }, "Log request")]);
}

async function viewPrivacy(page) {
  if (!privacyAdmin()) return fill(page, h("div", { class: "card empty" }, "Only partners or team admins can see this page."));
  const d = await api("GET", "/api/privacy/overview");
  const st = d.settings, admin = can("manage_team");
  const f = {};
  const inp = (k, ph, type) => f[k] = h("input", { class: "input", value: st[k] ?? "", placeholder: ph || "", type: type || "text", disabled: !admin || null });
  const okCount = d.checklist.filter(x => x.ok).length;
  const closeReq = r => ask(`Close request #${r.id} (${r.kind})`, [{ name: "response", label: "What was done, or why it was declined", type: "textarea", required: true }], "Close")
    .then(v => v && safely(async () => { await api("POST", `/api/privacy/requests/${r.id}/close`, { response: v.response }); toast("Closed"); route(); }));
  const sel = new Set();
  fill(page,
    h("div", { class: "row wrap" }, h("h1", { class: "grow" }, "Privacy & DPDP"),
      h("a", { class: "btn", href: "/privacy", target: "_blank" }, "View privacy policy"), h("a", { class: "btn ghost", href: "/terms", target: "_blank" }, "Terms")),
    h("p", { class: "muted" }, "Your firm is the Data Fiduciary under the Digital Personal Data Protection Act, 2023. These controls help you meet its obligations; they don't make the firm compliant on their own. Have your privacy policy reviewed by a lawyer."),
    h("div", { class: "group" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "Checklist"), h("span", { class: "muted small" }, `${okCount}/${d.checklist.length} in place`)),
      h("div", { class: "card list", style: "margin-top:8px" }, d.checklist.map(x => h("div", { class: "item", style: "grid-template-columns:28px 1fr auto;cursor:default" },
        h("span", { style: `font-size:16px;color:var(--${x.ok ? "green" : "amber"})` }, x.ok ? "✓" : "!"),
        h("div", {}, h("div", { class: "t1" }, x.title), h("div", { class: "t2" }, x.detail)), h("span", { class: "small muted" }, x.law))))),
    h("div", { class: "grid2", style: "align-items:start" },
      h("div", { class: "card pad" }, h("h2", {}, "Published in your privacy policy"),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "Grievance officer", inp("officer_name", "CA Neha Kulkarni")), h("label", { class: "field" }, "Email", inp("officer_email", "privacy@yourfirm.in"))),
        h("div", { class: "grid2", style: "margin-top:8px" }, h("label", { class: "field" }, "Phone", inp("officer_phone")), h("label", { class: "field" }, "Courts at (jurisdiction)", inp("jurisdiction", "Mumbai"))),
        h("label", { class: "field", style: "margin-top:8px" }, "Office address", inp("firm_address")),
        h("div", { class: "grid3", style: "margin-top:8px" }, h("label", { class: "field" }, "Keep records (years after FY)", inp("retention_years", "8", "number")),
          h("label", { class: "field" }, "Respond to requests within (days, max 90)", inp("response_days", "30", "number")), h("label", { class: "field" }, "Policy last updated", inp("policy_updated", "", "date"))),
        h("label", { class: "field", style: "margin-top:8px" }, "Public address of this site (for links in notices)", inp("public_url", "https://brain.yourfirm.in")),
        admin ? h("button", { class: "btn primary", style: "margin-top:10px", onclick: () => safely(async () => {
          const body = {}; for (const [k, el] of Object.entries(f)) body[k] = el.type === "number" ? +el.value : el.value;
          await api("POST", "/api/privacy/settings", body); toast("Saved — the privacy policy is updated"); route();
        }) }, "Save") : null),
      h("div", { class: "card pad" }, h("div", { class: "row" }, h("h2", { class: "grow", style: "margin:0" }, "Personal data breaches"), h("button", { class: "btn sm danger", onclick: newBreach }, "+ Record a breach")),
        d.breaches.length ? d.breaches.map(b => breachCard(b)) : h("p", { class: "small muted" }, "None recorded. If personal data is lost, stolen, or seen by someone who shouldn't, record it here at once — the 72-hour clock starts when you find out."))),
    h("div", { class: "group", style: "margin-top:18px" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "Rights requests"), h("button", { class: "btn sm", onclick: () => newRequest(null, route) }, "+ Log a request")),
      d.requests.length ? h("div", { class: "card", style: "margin-top:8px;overflow:auto" }, h("table", { class: "table" },
        h("tr", {}, h("th", {}, "Request"), h("th", {}, "From"), h("th", {}, "Due"), h("th", {}, "Status"), h("th", {}, "")),
        d.requests.map(r => h("tr", {}, h("td", {}, r.kind_label, h("div", { class: "small muted" }, r.details)), h("td", {}, r.client_name || r.requester),
          h("td", { style: r.overdue ? "color:var(--red);font-weight:600" : "" }, fmtDate(r.due_at, true), r.overdue ? " · overdue" : ""),
          h("td", {}, h("span", { class: "pill " + (r.status === "open" ? "awaiting_docs" : "filed") }, r.status)),
          h("td", {}, r.status === "open" ? h("div", { class: "row" },
            r.client_id ? h("a", { class: "btn sm ghost", href: `#/clients/${r.client_id}` }, "Client") : null,
            h("button", { class: "btn sm", onclick: () => closeReq(r) }, "Close")) : h("span", { class: "small muted" }, r.response)))))) :
        h("div", { class: "card empty", style: "margin-top:8px" }, "No requests yet.")),
    h("div", { class: "group" }, h("div", { class: "row" }, h("h3", { class: "grow" }, "Retention review"),
      d.retention_due.length && admin ? h("button", { class: "btn sm danger", onclick: () => safely(async () => {
        const ids = sel.size ? [...sel] : d.retention_due.map(x => x.id);
        if (!confirm(`Permanently erase ${ids.length} file(s) past their retention period?`)) return;
        const r = await api("POST", "/api/privacy/retention/purge", { ids }); toast(`Erased ${r.erased} files`); route();
      }) }, "Erase selected") : null),
      d.retention_due.length ? h("div", { class: "card", style: "margin-top:8px;overflow:auto" }, h("table", { class: "table" },
        d.retention_due.slice(0, 200).map(x => h("tr", {}, h("td", {}, h("input", { type: "checkbox", onchange: e => e.target.checked ? sel.add(x.id) : sel.delete(x.id) })),
          h("td", {}, x.filename), h("td", {}, x.client_name || "—"), h("td", { class: "small muted" }, `kept until ${x.hold_until}`))))) :
        h("div", { class: "card empty", style: "margin-top:8px" }, `Nothing past the ${st.retention_years}-year retention period.`)));
}

function breachCard(b) {
  const stamp = (field, label) => b[field] ? h("div", { class: "small" }, "✓ ", label, " ", b[field].replace("T", " ").slice(0, 16)) :
    h("button", { class: "btn sm", onclick: () => safely(async () => { await api("POST", `/api/privacy/breaches/${b.id}`, { [field]: new Date().toISOString().slice(0, 16) }); route(); }) }, `Mark: ${label}`);
  return h("div", { style: "border-top:1px solid var(--line);padding:10px 0;display:grid;gap:6px" },
    h("div", { class: "row" }, h("b", { class: "grow" }, b.title), h("span", { class: "pill " + (b.status === "open" ? "awaiting_docs" : "filed") }, b.status)),
    h("div", { class: "small muted" }, `Found ${b.detected_at.replace("T", " ").slice(0, 16)} · `, b.board_report_at ? "report filed" :
      h("span", { style: `color:var(--${b.hours_left < 0 ? "red" : "amber"});font-weight:600` }, b.hours_left < 0 ? `72-hour report overdue by ${Math.abs(b.hours_left)} h` : `${b.hours_left} h left for the Board report`)),
    h("div", { class: "row wrap" }, stamp("board_intimated_at", "Board intimated"), stamp("board_report_at", "72-h report sent"), stamp("principals_notified_at", "Clients notified"),
      b.client_ids.length ? h("button", { class: "btn sm", onclick: () => safely(async () => { const r = await api("POST", `/api/privacy/breaches/${b.id}/notify`); toast(`${r.drafted} notices drafted in the Outbox`); }) }, "Draft client notices") : null,
      b.status === "open" ? h("button", { class: "btn sm ghost", onclick: () => safely(async () => { await api("POST", `/api/privacy/breaches/${b.id}`, { status: "closed" }); route(); }) }, "Close") : null));
}

async function newBreach() {
  const clients = await api("GET", "/api/clients");
  const title = h("input", { class: "input", placeholder: "e.g. Laptop with client files stolen" });
  const desc = h("textarea", { class: "input", rows: 3, placeholder: "What happened, how it was found" });
  const data = h("input", { class: "input", placeholder: "e.g. bank statements, PAN, contact details" });
  const when = h("input", { class: "input", type: "datetime-local", value: new Date().toISOString().slice(0, 16) });
  const picks = clients.map(c => { const cb = h("input", { type: "checkbox", value: c.id }); return [cb, h("label", { class: "toggle" }, cb, c.name)]; });
  drawer("Record a personal data breach", [
    h("div", { class: "banner warn", style: "margin:0" }, "Inform the Data Protection Board of India and each affected person without delay; send the Board a detailed report within 72 hours (DPDP s.8(6) and Rules)."),
    h("label", { class: "field" }, "What happened (short)", title), h("label", { class: "field" }, "Details", desc),
    h("label", { class: "field" }, "Personal data involved", data), h("label", { class: "field" }, "When you found out", when),
    h("div", { class: "small muted" }, "Affected clients"), h("div", { class: "row wrap" }, picks.map(p => p[1])),
  ], [h("button", { class: "btn", onclick: closeDrawer }, "Cancel"), h("button", { class: "btn primary", onclick: () => safely(async () => {
    await api("POST", "/api/privacy/breaches", { title: title.value, description: desc.value, data_affected: data.value, detected_at: when.value,
      client_ids: picks.filter(p => p[0].checked).map(p => +p[0].value) });
    closeDrawer(); toast("Recorded — 72-hour clock started"); route();
  }) }, "Record")]);
}

// ----------------------------------------------------------------- AUTH
async function showAuth() {
  clearTimeout(S.poll);
  const st = await fetch("/api/auth/state", { credentials: "same-origin" }).then(r => r.json()).catch(() => ({}));
  if (st.user && !st.setup_needed) return boot();
  closeDrawer();
  document.querySelector(".shell").classList.add("hidden");
  let screen = $("#auth");
  if (!screen) { screen = h("div", { id: "auth", class: "auth" }); document.body.append(screen); }
  const err = h("div", { class: "small", style: "color:var(--red);min-height:18px" });
  const field = (label, attrs) => { const i = h("input", Object.assign({ class: "input" }, attrs)); return [h("label", { class: "field" }, label, i), i]; };
  const submit = async (fn) => { err.textContent = ""; try { await fn(); history.replaceState(null, "", "#/today"); screen.remove(); document.querySelector(".shell").classList.remove("hidden"); await boot(); } catch (e) { err.textContent = e.message; } };
  const post = async (path, body) => {
    const r = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Something went wrong");
    return d;
  };
  let form;
  if (st.setup_needed) {
    const [fL, firm] = field("Firm name", { placeholder: "Kulkarni & Co., Chartered Accountants" });
    const [nL, name] = field("Your name", { placeholder: "CA Neha Kulkarni", autocomplete: "name" });
    const [eL, email] = field("Email (your sign-in)", { type: "email", autocomplete: "email" });
    const [pL, pw] = field("Password", { type: "password", autocomplete: "new-password", placeholder: "At least 8 characters" });
    const agree = h("input", { type: "checkbox" });
    form = [h("h1", {}, "Set up your office"), h("p", { class: "muted" }, "You'll be the first partner. You can invite your team next."),
      fL, nL, eL, pL,
      h("label", { class: "small", style: "display:flex;gap:8px;align-items:flex-start" }, agree,
        h("span", {}, "I accept the ", h("a", { href: "/terms", target: "_blank" }, "Terms of Use & Disclaimer"),
          " on behalf of the firm, and understand the firm is the Data Fiduciary responsible for DPDP Act compliance.")),
      err, h("button", { class: "btn primary", type: "submit" }, "Create office")];
    form.onsubmit = () => submit(() => post("/api/auth/setup", { firm_name: firm.value, name: name.value, email: email.value, password: pw.value, accept_terms: agree.checked }));
  } else {
    const [eL, email] = field("Email", { type: "email", autocomplete: "username" });
    const [pL, pw] = field("Password", { type: "password", autocomplete: "current-password" });
    form = [h("h1", {}, "Sign in"), h("p", { class: "muted" }, st.firm_name || ""), eL, pL, err, h("button", { class: "btn primary", type: "submit" }, "Sign in"),
      h("div", { class: "small muted" }, "Forgot your password? Ask a partner to reset it from Settings → Team."),
      h("div", { class: "small muted" }, "By signing in you agree to the ", h("a", { href: "/terms", target: "_blank" }, "Terms"), ". See our ",
        h("a", { href: "/privacy", target: "_blank" }, "Privacy & Data Protection Policy"), ".")];
    form.onsubmit = () => submit(() => post("/api/auth/login", { email: email.value, password: pw.value }));
  }
  const onsubmit = form.onsubmit;
  fill(screen, h("div", { class: "card auth-card" }, h("div", { class: "logo" }, h("div", { class: "logo-mark" }, "₹"), h("div", {}, "CA Brain", h("small", {}, "Company brain"))),
    h("form", { onsubmit: e => { e.preventDefault(); onsubmit(); } }, form)));
  setTimeout(() => { const i = screen.querySelector("input"); i && i.focus(); }, 20);
}

// ------------------------------------------------------------- ASK (brain)
const SUGGEST = ["What's due this week?", "What's overdue?", "Missing documents", "Needs approval", "Chase Sharma Traders"];
function setupAsk() {
  const input = $("#ask-input"), panel = $("#ask-panel");
  const close = () => panel.classList.add("hidden");
  const showSuggest = () => {
    fill(panel, h("div", { class: "small muted" }, "Try"), h("div", { class: "chips" }, SUGGEST.map(s => h("span", { class: "chip", onclick: () => { input.value = s; run(); } }, s))),
      h("div", { class: "small muted", style: "margin-top:10px" }, "Or instruct: “add client Mehta Exports 24AAACM1234Q1Z9 audit tds payroll”, “extend tax audit FY 2025-26 to 31 Oct 2026 per CBDT circular …”"));
    panel.classList.remove("hidden");
  };
  async function run() {
    const text = input.value.trim(); if (!text) return showSuggest();
    fill(panel, h("div", { class: "muted" }, "Thinking…")); panel.classList.remove("hidden");
    const r = await safely(() => api("POST", "/api/ask", { text, history: S.history }));
    if (!r) return close();
    S.history = [...S.history, { role: "user", content: text }, { role: "assistant", content: r.answer }].slice(-8);
    const after = () => { run(); route(); };
    fill(panel, 
      h("div", { class: "ask-answer" }, r.answer),
      r.items.length ? h("div", { class: "card list", style: "margin-top:10px;box-shadow:none" }, r.items.slice(0, 25).map(o => obligationRow(Object.assign({ missing: o.missing.map(m => ({ label: m, period: "" })), blockers: o.blockers, status: o.stage === "awaiting_docs" || o.stage === "ready" || o.stage === "blocked" ? "open" : o.stage, months: [] }, o), after))) : null,
      r.proposals.map(p => {
        const card = h("div", { class: "proposal" }, h("div", { class: "row" }, h("b", { class: "grow" }, p.summary),
          h("button", { class: "btn sm ghost", onclick: () => card.remove() }, "Dismiss"),
          h("button", { class: "btn sm primary", onclick: () => safely(async () => {
            const res = await api("POST", "/api/proposals/execute", p);
            toast(res.message); card.remove(); S.boot.has_clients = true;
            if (res.client_id) location.hash = `#/clients/${res.client_id}`; else route();
          }) }, "Confirm")),
          p.kind === "chase" ? h("pre", {}, p.payload.body) : null,
          p.kind === "add_client" ? h("pre", {}, Object.entries(p.payload).filter(([, v]) => v && v !== "none").map(([k, v]) => `${k.replace(/_/g, " ")}: ${v === true ? "yes" : v}`).join("\n")) : null,
          p.kind === "override_due" ? h("pre", {}, `Reason: ${p.payload.reason}\n${can("approve") ? "Applies immediately for every affected client." : "Will go to a partner for approval."}`) : null);
        return card;
      }),
      h("div", { class: "small muted", style: "margin-top:8px" }, r.via === "claude" ? "Answered with Claude · changes need your confirmation" : "Esc to close"));
  }
  input.addEventListener("focus", () => { if (!input.value) showSuggest(); });
  input.addEventListener("keydown", e => { if (e.key === "Enter") run(); if (e.key === "Escape") { close(); input.blur(); } });
  document.addEventListener("click", e => { if (!$("#ask").contains(e.target) && !e.target.closest(".drawer,.scrim")) close(); });
  document.addEventListener("keydown", e => {
    if ((e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey))) && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) { e.preventDefault(); input.focus(); }
    if (e.key === "Escape") closeDrawer();
  });
}

// --------------------------------------------------------- global drop zone
function setupDrop() {
  let depth = 0, overlay = null;
  const show = on => {
    if (on && !overlay) { overlay = h("div", { class: "drop-overlay" }, h("div", {}, "Drop to file into the brain")); document.body.append(overlay); }
    if (!on && overlay) { overlay.remove(); overlay = null; }
  };
  window.addEventListener("dragenter", e => { if (e.dataTransfer.types.includes("Files")) { depth++; show(true); } });
  window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) show(false); });
  window.addEventListener("dragover", e => e.preventDefault());
  window.addEventListener("drop", async e => {
    e.preventDefault(); depth = 0; show(false);
    const files = await filesFromDrop(e.dataTransfer);
    const m = location.hash.match(/^#\/clients\/(\d+)/);
    upload(files, m ? m[1] : null);
  });
  $("#file-input").addEventListener("change", e => { upload([...e.target.files]); e.target.value = ""; });
}
async function filesFromDrop(dt) {
  const entries = [...dt.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files];
  const out = [];
  const walk = (entry, path) => new Promise(res => {
    if (entry.isFile) entry.file(f => { out.push(new File([f], path + f.name, { type: f.type })); res(); }, res);
    else if (entry.isDirectory) {
      const reader = entry.createReader(), all = [];
      const read = () => reader.readEntries(async es => {
        if (!es.length) { for (const x of all) await walk(x, path + entry.name + "/"); return res(); }
        all.push(...es); read();
      }, res);
      read();
    } else res();
  });
  for (const en of entries) await walk(en, "");
  return out;
}

// ----------------------------------------------------------------- router
async function route() {
  const [r, arg] = location.hash.slice(2).split("/");
  const page = $("#page");
  renderNav();
  try {
    if (r === "inbox") await viewInbox(page);
    else if (r === "calendar") await viewCalendar(page);
    else if (r === "clients" && arg === "new") { await viewClients(page); clientForm(null, c => location.hash = `#/clients/${c.id}`); }
    else if (r === "clients" && arg === "bulk") { await viewClients(page); bulkClients(); }
    else if (r === "clients" && arg) await viewClient(page, arg);
    else if (r === "clients") await viewClients(page);
    else if (r === "outbox") await viewOutbox(page);
    else if (r === "settings") await viewSettings(page);
    else if (r === "privacy") await viewPrivacy(page);
    else await viewToday(page);
  } catch (e) { fill(page, h("div", { class: "card empty" }, "Couldn't load this page: ", e.message)); }
}

async function boot() {
  try { S.boot = await api("GET", "/api/bootstrap"); } catch { return; }  // 401 already showed the sign-in screen
  S.me = S.boot.me;
  $("#auth") && $("#auth").remove();
  document.querySelector(".shell").classList.remove("hidden");
  await route();
  if (S.me.must_change_pw) changePassword(true);
}

window.addEventListener("hashchange", () => { closeDrawer(); route(); });
setupAsk();
setupDrop();
boot();
