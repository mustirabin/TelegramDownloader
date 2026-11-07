"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, url, body) {
  const opt = { method, headers: { "X-TGDL": "1" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3500);
}
const safe = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };
const fmtSize = (n) => n == null ? "?" : n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
const fmtEta = (s) => s == null ? "" : s > 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : s > 60 ? `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`;
const pad = (n) => String(n ?? 0).padStart(2, "0");
const fileName = (it) => it.rel_path ? it.rel_path.split("/").pop() : (it.original_name || it.link_url || `message ${it.file_msg_id}`);

// ---------------- tabs ----------------
let current = "library";
function showTab(name) {
  current = name;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("hidden", p.id !== "tab-" + name));
  if (name === "library") loadLibrary();
  if (name === "downloads") loadDownloads();
  if (name === "settings") loadSettings();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$$("dialog [data-close]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); b.closest("dialog").close(); }));

// ---------------- status ----------------
let status = null;
const STATE_TEXT = { ready: "Connected", not_configured: "Set API credentials", need_phone: "Log in needed", need_code: "Enter login code",
  need_password: "Enter 2FA password", error: "Telegram error" };
async function refreshStatus() {
  try { status = await api("GET", "/api/status"); } catch { return; }
  const tg = status.telegram, el = $("#tgState");
  el.textContent = tg.state === "ready" ? `● ${tg.me?.name || "Connected"}` : `● ${STATE_TEXT[tg.state] || tg.state}`;
  el.className = "state " + (tg.state === "ready" ? "ready" : "bad");
  el.title = tg.error || "";
  const n = (status.counts.queued || 0) + (status.counts.downloading || 0);
  $("#activeBadge").textContent = n; $("#activeBadge").classList.toggle("hidden", !n);
  const sc = tg.scan || {};
  $("#scanStatus").textContent = sc.running ? `Scanning… ${sc.chats} chats, ${sc.messages} messages, ${sc.found} new files` :
    sc.chats != null ? `Last scan: ${sc.chats} chats, ${sc.messages} messages, ${sc.found} new files${sc.error ? " — " + sc.error : ""}` : "";
  if (current === "settings" && renderLogin._state !== tg.state) renderLogin();
}

// ---------------- library ----------------
let lib = { series: [], movies: [], unsorted: [] }, sel = { type: "series", idx: 0 };
const loadLibrary = safe(async () => { lib = await api("GET", "/api/library"); renderLibrary(); });
$("#libSearch").addEventListener("input", () => renderLibrary());
$("#openLibrary").addEventListener("click", safe(() => api("POST", "/api/library/open")));

function matches(q, ...vals) { return !q || vals.some((v) => String(v ?? "").toLowerCase().includes(q)); }
function renderLibrary() {
  const q = $("#libSearch").value.trim().toLowerCase();
  const series = lib.series.map((s, i) => ({ s, i })).filter(({ s }) =>
    matches(q, s.title) || s.seasons.some((se) => se.episodes.some((e) => matches(q, e.rel_path, e.original_name))));
  const movies = lib.movies.filter((m) => matches(q, m.title, m.year, m.rel_path, m.original_name));
  const unsorted = lib.unsorted.filter((m) => matches(q, m.rel_path, m.original_name, m.caption));
  let nav = `<div class="group">Series (${series.length})</div>`;
  nav += series.map(({ s, i }) => `<div class="entry ${sel.type === "series" && sel.idx === i ? "sel" : ""}" data-type="series" data-idx="${i}">
      <span dir="auto">${esc(s.title)}</span><span class="n">${s.episodes}</span></div>`).join("") || `<div class="entry muted">none yet</div>`;
  nav += `<div class="group">Other</div>
    <div class="entry ${sel.type === "movies" ? "sel" : ""}" data-type="movies"><span>Movies</span><span class="n">${movies.length}</span></div>
    <div class="entry ${sel.type === "unsorted" ? "sel" : ""}" data-type="unsorted"><span>Unsorted</span><span class="n">${unsorted.length}</span></div>`;
  $("#libNav").innerHTML = nav;
  $$("#libNav .entry[data-type]").forEach((e) => e.addEventListener("click", () => {
    sel = { type: e.dataset.type, idx: +e.dataset.idx || 0 }; renderLibrary();
  }));

  let html = "";
  if (sel.type === "series") {
    const s = lib.series[sel.idx];
    if (!s) html = `<div class="empty">No series yet. Send a video or a t.me link to your account in a private chat, or paste a link in Downloads.</div>`;
    else {
      html = `<h2 dir="auto">${esc(s.title)}</h2>`;
      for (const se of s.seasons) {
        const eps = se.episodes.filter((e) => matches(q, s.title, e.rel_path, e.original_name));
        if (!eps.length) continue;
        html += `<div class="season">Season ${se.season}</div>` + itemTable(eps, (e) => `S${pad(e.season)}E${pad(e.episode)}`);
      }
    }
  } else if (sel.type === "movies") {
    html = `<h2>Movies</h2>` + (movies.length ? itemTable(movies, (m) => esc(m.title) + (m.year ? ` (${m.year})` : "")) : `<div class="empty">No movies yet.</div>`);
  } else {
    html = `<h2>Unsorted</h2><p class="muted small">Files whose name didn't look like an episode or movie. Use “Move” to file them.</p>` +
      (unsorted.length ? itemTable(unsorted, (m) => esc(fileName(m))) : `<div class="empty">Nothing unsorted.</div>`);
  }
  $("#libContent").innerHTML = html;
  bindItemActions($("#libContent"));
}
function itemTable(items, label) {
  return `<table><tbody>${items.map((it) => `<tr data-id="${it.id}">
    <td style="width:22%" dir="auto"><b>${label(it)}</b></td>
    <td><div class="name" dir="auto">${esc(fileName(it))}</div><div class="sub">${fmtSize(it.size)} · ${new Date((it.completed_at || it.created_at) * 1000).toLocaleString()}${it.via_sender ? " · from " + esc(it.via_sender) : ""}${it.exists === false ? ' · <span class="err">file missing</span>' : ""}</div></td>
    <td class="actions">
      <button class="small" data-act="play">Play</button>
      <button class="small ghost" data-act="open" title="Open in your default player">Open</button>
      <button class="small ghost" data-act="reveal" title="Show in Explorer">Folder</button>
      <a href="/api/items/${it.id}/stream?download=true"><button class="small ghost">Save</button></a>
      <button class="small ghost" data-act="edit">Move</button>
      <button class="small danger" data-act="delete">Delete</button>
    </td></tr>`).join("")}</tbody></table>`;
}
function findItem(id) {
  for (const s of lib.series) for (const se of s.seasons) for (const e of se.episodes) if (e.id === id) return e;
  return [...lib.movies, ...lib.unsorted].find((m) => m.id === id);
}
function bindItemActions(root) {
  $$("button[data-act]", root).forEach((b) => b.addEventListener("click", safe(async () => {
    const id = +b.closest("tr").dataset.id, it = findItem(id);
    const act = b.dataset.act;
    if (act === "play") {
      $("#playerTitle").textContent = fileName(it);
      $("#player").src = `/api/items/${id}/stream`; $("#playerDlg").showModal(); $("#player").play().catch(() => {});
    } else if (act === "open") await api("POST", `/api/items/${id}/open`);
    else if (act === "reveal") await api("POST", `/api/items/${id}/open?reveal=true`);
    else if (act === "edit") openEdit(it);
    else if (act === "delete") {
      if (!confirm(`Delete "${fileName(it)}" from disk?`)) return;
      await api("DELETE", `/api/items/${id}?delete_file=true`); toast("Deleted"); loadLibrary();
    }
  })));
}
$("#playerDlg").addEventListener("close", () => { const v = $("#player"); v.pause(); v.removeAttribute("src"); v.load(); });

let editing = null;
function openEdit(it) {
  editing = it; const f = $("#editForm");
  $("#editFile").textContent = (it.original_name || "") + (it.caption ? "\n" + it.caption.slice(0, 200) : "");
  f.kind.value = it.kind || "unsorted"; f.title.value = it.title || ""; f.season.value = it.season ?? "";
  f.episode.value = it.episode ?? ""; f.year.value = it.year ?? ""; toggleEp(); $("#editDlg").showModal();
}
function toggleEp() { const k = $("#editForm").kind.value; $$("#editForm .ep").forEach((e) => e.classList.toggle("hidden", k !== "episode")); }
$("#editForm").kind.addEventListener("change", toggleEp);
$("#editSave").addEventListener("click", safe(async (e) => {
  e.preventDefault(); const f = $("#editForm");
  const it = await api("PATCH", `/api/items/${editing.id}`, { kind: f.kind.value, title: f.title.value, season: f.season.value, episode: f.episode.value, year: f.year.value });
  $("#editDlg").close(); toast("Moved to " + it.rel_path); loadLibrary();
}));

// ---------------- downloads ----------------
const loadDownloads = safe(async () => {
  const items = await api("GET", "/api/downloads?filter=" + $("#dlFilter").value);
  $("#dlEmpty").classList.toggle("hidden", items.length > 0);
  $("#dlBody").innerHTML = items.map((it) => {
    const pct = it.size ? Math.min(100, (it.bytes_done || 0) * 100 / it.size) : 0;
    const prog = it.status === "downloading" ? `<div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
        <div class="sub">${pct.toFixed(1)}% · ${fmtSize(it.bytes_done)} / ${fmtSize(it.size)}${it.speed ? ` · ${(it.speed / 1e6).toFixed(2)} MB/s · ETA ${fmtEta(it.eta)}` : ""}</div>`
      : it.status === "done" ? `<div class="sub">${fmtSize(it.size)}</div>` : it.size ? `<div class="sub">${fmtSize(it.bytes_done)} / ${fmtSize(it.size)}</div>` : "";
    const acts = [];
    if (["queued", "downloading", "new"].includes(it.status)) acts.push(`<button class="small ghost" data-dl="cancel">Cancel</button>`);
    if (it.status === "new") acts.push(`<button class="small" data-dl="retry">Download</button>`);
    if (["failed", "cancelled", "skipped"].includes(it.status)) acts.push(`<button class="small" data-dl="retry">Retry</button>`);
    if (it.status !== "done") acts.push(`<button class="small danger" data-dl="remove">Remove</button>`);
    return `<tr data-id="${it.id}"><td class="muted">${it.id}</td>
      <td><div class="name" dir="auto">${esc(fileName(it))}</div>${it.link_url ? `<div class="sub">${esc(it.link_url)}</div>` : ""}</td>
      <td dir="auto">${esc(it.via_sender || "")}</td>
      <td><span class="pill ${esc(it.status)}">${esc(it.status)}</span>${it.error ? `<div class="err">${esc(it.error)}</div>` : ""}</td>
      <td>${prog}</td><td class="actions">${acts.join("")}</td></tr>`;
  }).join("");
  $$("#dlBody button[data-dl]").forEach((b) => b.addEventListener("click", safe(async () => {
    const id = b.closest("tr").dataset.id, a = b.dataset.dl;
    if (a === "remove") await api("DELETE", `/api/downloads/${id}`); else await api("POST", `/api/downloads/${id}/${a}`);
    loadDownloads();
  })));
});
$("#dlFilter").addEventListener("change", loadDownloads);
$("#addLink").addEventListener("click", safe(async () => {
  const url = $("#linkInput").value.trim(); if (!url) return;
  const it = await api("POST", "/api/downloads", { url }); $("#linkInput").value = "";
  toast(it.status === "done" ? "Already in library: " + it.rel_path : `Queued #${it.id}`); $("#dlFilter").value = "active"; loadDownloads();
}));
$("#linkInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#addLink").click(); });
$("#scanBtn").addEventListener("click", safe(async () => {
  const r = await api("POST", "/api/scan", { limit: +$("#scanLimit").value || 100 }); toast(`Scanning last ${r.limit} messages of each private chat…`);
}));

// ---------------- settings ----------------
let settings = null;
const loadSettings = safe(async () => {
  settings = await api("GET", "/api/settings");
  $("#s_api_id").value = settings.api_id ?? "";
  $("#s_api_hash").placeholder = settings.api_hash_set ? "(saved — leave empty to keep)" : "paste api_hash";
  $("#s_library_dir").value = settings.library_dir;
  $("#s_max_concurrent_downloads").value = settings.max_concurrent_downloads;
  for (const k of ["auto_download", "include_saved_messages", "include_bots", "download_all_documents"]) $("#s_" + k).checked = settings[k];
  $$("input[name=watch_mode]").forEach((r) => (r.checked = r.value === settings.watch_mode));
  $("#s_allowed_chats").value = settings.allowed_chats.join("\n");
  $("#s_video_extensions").value = settings.video_extensions.join(", ");
  await refreshStatus(); renderLogin();
});
$("#saveApi").addEventListener("click", safe(async () => {
  const body = { api_id: $("#s_api_id").value }; const h = $("#s_api_hash").value.trim(); if (h) body.api_hash = h;
  await api("PUT", "/api/settings", body); $("#s_api_hash").value = ""; await api("POST", "/api/telegram/reconnect");
  toast("Saved"); loadSettings();
}));
$("#saveSettings").addEventListener("click", safe(async () => {
  const r = await api("PUT", "/api/settings", {
    library_dir: $("#s_library_dir").value, max_concurrent_downloads: $("#s_max_concurrent_downloads").value,
    auto_download: $("#s_auto_download").checked, include_saved_messages: $("#s_include_saved_messages").checked,
    include_bots: $("#s_include_bots").checked, download_all_documents: $("#s_download_all_documents").checked,
    watch_mode: $("input[name=watch_mode]:checked")?.value || "all_private", allowed_chats: $("#s_allowed_chats").value,
    video_extensions: $("#s_video_extensions").value,
  });
  $("#saveMsg").textContent = r.changed.length ? "Saved: " + r.changed.join(", ") : "No changes"; loadSettings();
}));

function renderLogin() {
  const tg = status?.telegram || { state: "not_configured" }; renderLogin._state = tg.state;
  const box = $("#loginBox"); let html = "";
  if (tg.error) html += `<p class="err">${esc(tg.error)}</p>`;
  if (tg.state === "ready") html += `<div class="me">Logged in as <b dir="auto">${esc(tg.me.name)}</b>${tg.me.username ? " (@" + esc(tg.me.username) + ")" : ""}<br><span class="muted small">Watching private chats for videos and t.me links.</span></div>
      <button class="danger" id="logoutBtn">Log out</button>`;
  else if (tg.state === "not_configured") html += `<p class="muted">Enter your api_id and api_hash below first.</p>`;
  else if (tg.state === "need_phone") html += `<div class="login-step"><label>Phone number (international format)<input id="l_phone" placeholder="+9725XXXXXXXX"></label><button id="l_go">Send login code</button></div>`;
  else if (tg.state === "need_code") html += `<div class="login-step"><label>Code Telegram sent to ${esc(tg.phone)} (check the Telegram app)<input id="l_code" autocomplete="one-time-code"></label><button id="l_go">Log in</button></div>`;
  else if (tg.state === "need_password") html += `<div class="login-step"><label>Two-step verification password<input id="l_pw" type="password"></label><button id="l_go">Log in</button></div>`;
  else html += `<button id="l_retry">Reconnect</button>`;
  box.innerHTML = html;
  const go = $("#l_go");
  if (go) {
    const input = $("input", box); input.focus();
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
    go.addEventListener("click", safe(async () => {
      go.disabled = true;
      try {
        const v = input.value.trim();
        if (tg.state === "need_phone") await api("POST", "/api/auth/phone", { phone: v });
        else if (tg.state === "need_code") await api("POST", "/api/auth/code", { code: v });
        else await api("POST", "/api/auth/password", { password: input.value });
      } finally { go.disabled = false; }
      await refreshStatus(); renderLogin();
    }));
  }
  $("#logoutBtn")?.addEventListener("click", safe(async () => {
    if (!confirm("Log out this Telegram session?")) return; await api("POST", "/api/auth/logout"); await refreshStatus(); renderLogin();
  }));
  $("#l_retry")?.addEventListener("click", safe(async () => { await api("POST", "/api/telegram/reconnect"); await refreshStatus(); renderLogin(); }));
}

// ---------------- polling ----------------
let lastDone = null;
async function tick() {
  await refreshStatus();
  if (current === "downloads") await loadDownloads();
  const done = status?.counts?.done || 0;
  if (current === "library" && lastDone !== null && done !== lastDone) loadLibrary();
  lastDone = done;
  setTimeout(tick, 1500);
}
(async () => {
  await refreshStatus();
  if (status && status.telegram.state !== "ready") showTab("settings"); else showTab("library");
  tick();
})();
