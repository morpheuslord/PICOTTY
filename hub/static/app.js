/* Swarm Control — dashboard SPA.
 *
 * A buildless implementation of the "Swarm Control" design, wired to the hub's
 * REST + WebSocket API. When the hub is not reachable it drops into a local DEMO
 * mode pre-populated with the four homelab nodes, so the UI is always populated.
 *
 * Live data: registry + history come over REST on load, then the WebSocket keeps
 * everything current (node_up/down/updated, heartbeat, command_issued, result,
 * output, event, hub_stats). The browser holds no authoritative state.
 */
(() => {
  "use strict";

  // ---- tiny hyperscript --------------------------------------------------
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "style") e.setAttribute("style", v);
      else if (k === "class") e.className = v;
      else if (k.slice(0, 2) === "on" && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "value") e.value = v;
      else if (k === "checked") e.checked = !!v;
      else if (k === "disabled") e.disabled = !!v;
      else e.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      // Append real DOM nodes as-is; everything else becomes an escaped text
      // node. A string is NEVER parsed as HTML, so a value reaching here cannot
      // inject markup (no innerHTML sink anywhere in this helper) — XSS-safe.
      e.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return e;
  }
  const $ = (id) => document.getElementById(id);

  // ---- help affordance ---------------------------------------------------
  // Small, unobtrusive "?" that deep-links into help.html at a feature anchor
  // (opens in a new tab). Reused everywhere so the docs stay one click from every
  // control. `label` seeds the hover tooltip and the accessible name.
  function helpLink(anchor, label, opts) {
    const size = (opts && opts.size) || 15;
    return h("a", {
      href: "help.html#" + anchor, target: "_blank", rel: "noopener",
      "aria-label": "Help: " + (label || anchor),
      title: (label ? label + " — " : "") + "open the docs for this feature (new tab)",
      onClick: (e) => e.stopPropagation(),   // never trigger the control it sits on
      style: "flex:none;display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;width:" +
        size + "px;height:" + size + "px;font-size:11px;line-height:1;font-weight:600;color:var(--color-neutral-600);" +
        "border:1px solid var(--color-divider);border-radius:50%;text-decoration:none;cursor:help;user-select:none",
    }, "?");
  }

  // ---- time helpers ------------------------------------------------------
  const now = () => Date.now();
  function rel(ts) {
    const d = Math.max(0, Math.round((now() - ts) / 1000));
    if (d < 5) return "now";
    if (d < 60) return d + "s ago";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  }
  function hhmmss(ts) {
    const d = new Date(ts), p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }
  function uptimeStr(ms) {
    const up = Math.floor(ms / 1000);
    return Math.floor(up / 86400) + "d " + Math.floor((up % 86400) / 3600) + "h " + Math.floor((up % 3600) / 60) + "m";
  }

  // ---- REST --------------------------------------------------------------
  async function api(method, path, body) {
    const opt = { method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch("/api" + path, opt);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  const getJSON = (p) => api("GET", p);
  const postJSON = (p, b) => api("POST", p, b || {});
  const patchJSON = (p, b) => api("PATCH", p, b);
  const delJSON = (p) => api("DELETE", p);

  // ---- state -------------------------------------------------------------
  const CONSOLE_CAP = 400;
  const state = {
    demo: false,
    view: "nodes",
    ws: "connecting", // connecting | live | offline
    nodes: [],        // {id,label,group,ip,fw,status,rttMs,caps,lastSeen,inflight}
    consoles: {},     // id -> [{ts,kind,text}]
    history: [],      // {id,nodeId,text,status,ts}
    events: [],       // {ts,type,nodeId,detail}
    macros: [],       // {id,name,group,steps,runs,lastRun}
    selId: null,
    selMacro: null,
    query: "",
    statusFilter: "all",
    tab: "history",
    input: "",
    inputModes: {},   // nodeId -> "hid" | "serial" (per-node console input mode)
    sendNewline: true,
    charDelay: 12,
    autoscroll: true,
    wrap: true,
    leftOpen: true,
    rightOpen: true,
    hub: { uptime_ms: 0, nodes_online: 0, nodes_total: 0, bind: "", swarm_port: 9000, web_port: 8080, version: "" },
    settings: {},
    toasts: [],
    dialog: null,
    bridge: { enabled: false, bind: "", ports: [] }, // serial bridge map (phase 8)
    expectJobs: {},   // nodeId -> {job_id,step,total,phase,detail} (phase 5)
    runbooks: [],     // {id,name,yaml} (phase 9)
    runbookRuns: {},  // run_id -> {name,nodes:{id:status}} (phase 9)
    selRunbook: null,
    bundles: [],      // OTA firmware bundles [{name,files:[path],total_sha256,created_at}] (phase 12)
    otaJobs: {},      // nodeId -> {job_id,bundle,status,phase,sent_bytes,total_bytes,detail} (phase 12)
  };
  const ui = {};        // live DOM refs for the nodes view
  const KEY_ALIASES = { "↑": "UP_ARROW", "↓": "DOWN_ARROW", "→": "RIGHT_ARROW", "←": "LEFT_ARROW",
    "ESC": "ESCAPE", "DEL": "DELETE", "Enter": "ENTER", "Tab": "TAB" };

  const sel = () => state.nodes.find((n) => n.id === state.selId) || null;
  const selMacro = () => state.macros.find((m) => m.id === state.selMacro) || null;

  // ---- input mode (HID vs Serial) ----------------------------------------
  // Stored per node in state.inputModes, alongside the other in-memory UI prefs
  // (sendNewline, charDelay, wrap, …). Serial requires the node's firmware to
  // advertise the `serial_tx` capability; without it we fall back to HID.
  const nodeHasSerialTx = (n) => !!(n && String(n.caps || "").split(",").includes("serial_tx"));
  // OTA firmware update (phase 12): the node's firmware must advertise the `ota`
  // capability, checked the same way as serial_tx above.
  const nodeHasOta = (n) => !!(n && String(n.caps || "").split(",").includes("ota"));
  const inputMode = (id) => (id && state.inputModes[id]) || "hid";
  const setInputMode = (id, m) => { if (id) state.inputModes[id] = m; };
  const effectiveMode = (n) => (nodeHasSerialTx(n) && inputMode(n && n.id) === "serial") ? "serial" : "hid";

  // ---- prompt-state badge (phase 4) --------------------------------------
  // The hub classifies each online node's tail into a coarse prompt_state and
  // pushes changes over WS ({event:"node_state"}). We colour a tiny badge per
  // row; a null/absent state renders nothing.
  const PROMPT_BADGE = {
    panic:    { bg: "var(--color-accent)",      fg: "#faf8f6" },
    shell:    { bg: "#3f9e63",                   fg: "#f4fbf6" },
    login:    { bg: "#c98a00",                   fg: "#1b1918" },
    password: { bg: "#c98a00",                   fg: "#1b1918" },
    grub:     { bg: "#3a6ea5",                   fg: "#eef4fb" },
    booting:  { bg: "#3a6ea5",                   fg: "#eef4fb" },
  };
  function promptBadge(stateName, opts) {
    const c = PROMPT_BADGE[stateName];
    if (!c) return null;
    const pad = (opts && opts.big) ? "2px 8px" : "0 6px";
    const fs = (opts && opts.big) ? "11px" : "10px";
    return h("span", { title: "prompt: " + stateName,
      style: "flex:none;padding:" + pad + ";font-size:" + fs + ";font-weight:600;letter-spacing:0.02em;background:" + c.bg + ";color:" + c.fg }, stateName);
  }

  // A node is powered BY its target, so "node online" alone doesn't say the
  // MACHINE is up. The hub derives target ∈ up|down|unknown (USB host enumerated
  // / recent serial output). We surface it distinctly from the node dot.
  const TARGET_BADGE = {
    up:   { bg: "#3f9e63",              fg: "#f4fbf6", label: "machine up",   dot: "#3f9e63" },
    down: { bg: "var(--color-accent)",  fg: "#faf8f6", label: "machine dead", dot: "var(--color-accent)" },
  };
  function targetBadge(target, opts) {
    const c = TARGET_BADGE[target];
    // unknown / absent → a hollow grey dot (compact) or nothing (big), so an
    // old-firmware or quiet node doesn't falsely read as up or dead.
    const big = opts && opts.big;
    if (!c) {
      if (big) return null;
      return h("span", { title: "attached machine: unknown (node can't tell / old firmware)",
        style: "flex:none;width:8px;height:8px;border-radius:50%;border:2px solid #6f6a68;box-sizing:border-box" });
    }
    if (big) {
      return h("span", { title: "attached machine is " + (target === "up" ? "powered and running" : "off or hung (node still alive)"),
        style: "flex:none;padding:2px 8px;font-size:11px;font-weight:600;letter-spacing:0.02em;background:" + c.bg + ";color:" + c.fg }, c.label);
    }
    return h("span", { title: c.label + " — the attached machine",
      style: "flex:none;width:9px;height:9px;border-radius:50%;background:" + c.dot + (target === "down" ? ";animation:sc-pulse 1.4s infinite" : "") });
  }

  // ---- serial bridge (phase 8) -------------------------------------------
  const bridgePortFor = (nodeId) => {
    const row = (state.bridge.ports || []).find((p) => p.node_id === nodeId);
    return row ? row.port : null;
  };

  // ---- console helpers ---------------------------------------------------
  function pushLine(nodeId, kind, text) {
    const cur = state.consoles[nodeId] || (state.consoles[nodeId] = []);
    cur.push({ ts: now(), kind, text });
    if (cur.length > CONSOLE_CAP) cur.splice(0, cur.length - CONSOLE_CAP);
    if (state.view === "nodes" && nodeId === state.selId) {
      if (xtermActive()) xtermMeta(kind, text);
      else appendConsoleLine(cur[cur.length - 1]);
    }
  }

  // Streaming target output. Unlike pushLine (one COMPLETE line per call), serial
  // getty echo arrives as a byte stream — often a single character per output
  // frame — so making each frame its own line prints "ls" as "l\ns". Instead we
  // split only on real line feeds (\n) and CONTINUE the current line (flagged
  // `open`) until a \n closes it. Stray CR and escapes are cleaned at render.
  function pushOutputInto(nodeId, text, ts, render) {
    const cur = state.consoles[nodeId] || (state.consoles[nodeId] = []);
    text = String(text == null ? "" : text);
    if (!text) return;
    const parts = text.split("\n");            // K segments, K-1 line feeds between
    for (let i = 0; i < parts.length; i++) {
      const last = cur[cur.length - 1];
      if (i === 0 && last && last.kind === "out" && last.open) last.text += parts[i];
      else cur.push({ ts: ts, kind: "out", text: parts[i], open: true });
      if (i < parts.length - 1) cur[cur.length - 1].open = false;  // a \n ended this line
    }
    if (cur.length > CONSOLE_CAP) cur.splice(0, cur.length - CONSOLE_CAP);
    if (render && state.view === "nodes" && nodeId === state.selId) scheduleConsoleRebuild();
  }
  // Feed target output to the live surface: raw bytes to xterm (a real emulator),
  // or the coalesced DOM log fallback. The buffer is always kept for both.
  const pushOutput = (nodeId, text) => {
    pushOutputInto(nodeId, text, now(), !xtermActive());
    if (xtermActive() && nodeId === state.selId && ui.xterm) ui.xterm.write(String(text == null ? "" : text));
  };

  // Coalesce bursts (a screenful of output = many frames) into one DOM rebuild
  // per animation frame, so a chatty target never thrashes the console.
  let _consoleRebuildQueued = false;
  function scheduleConsoleRebuild() {
    if (_consoleRebuildQueued) return;
    _consoleRebuildQueued = true;
    requestAnimationFrame(() => { _consoleRebuildQueued = false; rebuildConsole(); });
  }
  const LINE_COLOR = { in: "#f0ece9", err: "#e0603c", sys: "#8c8683", out: "#c9c4c0" };
  const PREFIX = { in: "›", err: "!", sys: "·", out: "" };
  const PREFIX_COLOR = { in: "#d94f2b", err: "#e0603c", sys: "#6f6a68", out: "#6f6a68" };

  // Target shells emit raw terminal control sequences — colours, cursor moves,
  // bracketed-paste toggles (ESC[?2004h/l), screen clears (ESC[2J, ESC[H). This
  // pane is a log view, not a terminal emulator, and colours lines by message
  // kind rather than by embedded ANSI, so strip the sequences to plain text at
  // render time. The raw bytes are untouched in the DB and event stream; this
  // only affects what is drawn.
  const cleanTerm = (s) => String(s == null ? "" : s)
    .replace(/\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]/g, "")   // CSI: ESC [ … final byte
    .replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g, "")               // OSC: ESC ] … BEL/ST
    .replace(/\x1b[()#][0-9A-Za-z]/g, "")                        // charset designators: ESC ( B
    .replace(/\x1b[\x40-\x5a\x5c\x5e\x5f]/g, "")                 // other ESC-Fe (not [ or ], done above)
    .replace(/\r\n/g, "\n").replace(/\r/g, "")                   // normalise CRLF, drop stray CR
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");           // remaining C0 controls (keep \t, \n)

  function lineEl(l) {
    return h("div", { style: "display:flex;gap:10px;white-space:" + (state.wrap ? "pre-wrap" : "pre") },
      h("span", { style: "flex:none;color:#575352;user-select:none" }, hhmmss(l.ts)),
      h("span", { style: "flex:none;width:12px;user-select:none;color:" + PREFIX_COLOR[l.kind] }, PREFIX[l.kind] || ""),
      h("span", { style: "min-width:0;color:" + (LINE_COLOR[l.kind] || "#c9c4c0") }, cleanTerm(l.text)));
  }
  function appendConsoleLine(l) {
    if (!ui.term) return;
    ui.cursorRow ? ui.term.insertBefore(lineEl(l), ui.cursorRow) : ui.term.appendChild(lineEl(l));
    scrollDown();
  }
  function rebuildConsole() {
    if (!ui.term) return;
    ui.term.innerHTML = "";
    const lines = state.consoles[state.selId] || [];
    for (const l of lines) ui.term.appendChild(lineEl(l));
    const disabled = !sel() || sel().status !== "online";
    ui.cursorRow = h("div", { style: "display:flex;gap:10px" },
      h("span", { style: "flex:none;color:#575352;visibility:hidden" }, "00:00:00"),
      h("span", { style: "width:8px;height:15px;animation:sc-blink 1.1s step-end infinite;background:" + (disabled ? "#575352" : "#d94f2b") }));
    ui.term.appendChild(ui.cursorRow);
    if (ui.lineCount) ui.lineCount.textContent = lines.length + " lines buffered";
    scrollDown();
  }
  function scrollDown() {
    if (ui.term && state.autoscroll) requestAnimationFrame(() => { ui.term.scrollTop = ui.term.scrollHeight; });
  }

  // ---- xterm.js progressive enhancement (phase 3) ------------------------
  // Vendored-with-fallback: index.html references vendor/xterm.* which 404
  // harmlessly until an operator runs fetch-vendor.sh. Only when window.Terminal
  // is defined do we mount a real terminal; otherwise the DOM log above is used,
  // completely unchanged. ui.xterm holds the live instance (null when inactive).
  const xtermAvailable = () => typeof window.Terminal === "function";
  const xtermActive = () => !!ui.xterm;
  function xtermMeta(kind, text) {
    if (!ui.xterm) return;
    const pfx = kind === "in" ? "\x1b[38;5;209m› \x1b[0m" : kind === "err" ? "\x1b[31m! \x1b[0m" : kind === "sys" ? "\x1b[90m· \x1b[0m" : "";
    ui.xterm.write(pfx + String(text == null ? "" : text) + "\r\n");
  }
  function xtermReplay() {
    if (!ui.xterm) return;
    const lines = state.consoles[state.selId] || [];
    let s = "";
    for (const l of lines) {
      if (l.kind === "out") { s += l.text; if (!l.open) s += "\r\n"; }
      else { s += (l.kind === "in" ? "\x1b[38;5;209m› \x1b[0m" : l.kind === "err" ? "\x1b[31m! \x1b[0m" : "\x1b[90m· \x1b[0m") + l.text + "\r\n"; }
    }
    ui.xterm.write(s);
  }
  function disposeXterm() { if (ui.xterm) { try { ui.xterm.dispose(); } catch (e) {} } ui.xterm = null; ui.xtermFit = null; }
  function setupXterm() {
    disposeXterm();
    try {
      ui.xterm = new window.Terminal({
        convertEol: false, cursorBlink: true, scrollback: 5000, fontSize: 12.5,
        fontFamily: "ui-monospace,'SF Mono',Menlo,Consolas,monospace",
        theme: { background: "#1b1918", foreground: "#c9c4c0", cursor: "#d94f2b" },
      });
      const FA = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
      if (typeof FA === "function") { ui.xtermFit = new FA(); ui.xterm.loadAddon(ui.xtermFit); }
      ui.xterm.open(ui.xtermHost);
      if (ui.xtermFit) { try { ui.xtermFit.fit(); } catch (e) {} }
      xtermReplay();
      // Serial mode: stream keystrokes straight to the getty. HID mode leaves the
      // terminal read-only (the HID composer is the input path there).
      ui.xterm.onData((d) => {
        const n = sel();
        if (!(n && n.status === "online" && effectiveMode(n) === "serial")) return;
        // xterm auto-answers the target's terminal queries — a cursor-position
        // report (ESC[<row>;<col>R) for ESC[6n, a device-attributes reply
        // (ESC[?…c) for ESC[c. Those replies come back through onData; forwarding
        // them to the getty lands them at the shell as garbage like "168R". Strip
        // exactly those reply shapes before sending; real keystrokes (incl. arrow
        // keys ESC[A–D) never match, so typing is unaffected.
        const clean = d.replace(/\x1b\[[?>=0-9;]*[Rc]/g, "");
        if (clean) postSend(n.id, { data: clean });
      });
    } catch (e) { disposeXterm(); rebuildConsole(); }
  }
  // Single entry point used everywhere the console must (re)draw for the current
  // selection — picks the xterm path or the DOM-log path.
  function mountConsole() {
    if (ui.xtermHost && xtermAvailable()) setupXterm();
    else rebuildConsole();
  }
  function refreshConsole() {
    if (ui.xterm) { try { ui.xterm.reset(); } catch (e) {} xtermReplay(); }
    else rebuildConsole();
  }

  // ---- toasts + dialog ---------------------------------------------------
  let seq = 1;
  function toast(kind, text) {
    const id = "t" + (++seq);
    state.toasts.push({ id, kind, text });
    renderToasts();
    setTimeout(() => { state.toasts = state.toasts.filter((t) => t.id !== id); renderToasts(); }, 5200);
  }
  function renderToasts() {
    let box = $("toasts");
    if (!box) { box = h("div", { id: "toasts", style: "position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:50;max-width:360px" }); document.body.appendChild(box); }
    box.innerHTML = "";
    for (const t of state.toasts) {
      const bg = t.kind === "Failed" ? "var(--color-accent)" : "#2b2827";
      box.appendChild(h("div", { class: "elev-md", style: "background:" + bg + ";color:#faf8f6;padding:10px 14px;font-size:13px;display:flex;gap:10px;align-items:center" },
        h("span", { style: "font-weight:600;flex:none" }, t.kind),
        h("span", { style: "min-width:0" }, t.text),
        h("button", { style: "margin-left:auto;background:none;border:0;color:inherit;cursor:pointer;font-size:14px;line-height:1",
          onClick: () => { state.toasts = state.toasts.filter((x) => x.id !== t.id); renderToasts(); } }, "×")));
    }
  }
  function confirmDialog(title, body, confirmLabel, run) {
    state.dialog = { title, body, confirm: confirmLabel, run };
    renderDialog();
  }
  function renderDialog() {
    let d = $("dialog");
    if (d) d.remove();
    if (!state.dialog) return;
    const dg = state.dialog;
    const backdrop = h("div", { id: "dialog", class: "dialog-backdrop", style: "z-index:100" },
      h("div", { class: "dialog" },
        h("div", { class: "dialog-title" }, dg.title),
        h("div", { class: "dialog-body" }, dg.body),
        h("div", { class: "dialog-actions" },
          h("button", { class: "btn btn-secondary", onClick: () => { state.dialog = null; renderDialog(); } }, "Cancel"),
          h("button", { class: "btn btn-primary", onClick: () => { const r = dg.run; state.dialog = null; renderDialog(); r(); } }, dg.confirm))));
    document.body.appendChild(backdrop);
  }

  // ---- nav / shell -------------------------------------------------------
  function renderShell() {
    const app = $("app");
    app.innerHTML = "";
    app.appendChild(h("div", { style: "display:flex;flex-direction:column;height:100vh;background:var(--color-bg);color:var(--color-text);font-family:var(--font-body)" },
      renderNav(),
      h("div", { id: "view", style: "flex:1;display:flex;min-height:0" })));
    renderView();
  }

  function renderNav() {
    const NAV_TIP = { nodes: "Live node list, serial console and input composer", macros: "Reusable HID sequences replayed on any node",
      runbooks: "YAML expect flows run across a node group", events: "Hub journal and command audit", settings: "Hub configuration, safety toggles, alerts" };
    const links = [["nodes", "Nodes"], ["macros", "Macros"], ["runbooks", "Runbooks"], ["events", "Events"], ["settings", "Settings"]].map(([k, label]) =>
      h("a", { "aria-current": state.view === k ? "page" : null, title: NAV_TIP[k], onClick: (e) => { e.preventDefault(); switchView(k); } }, label));
    // Help opens the in-app docs in a new tab (not a view switch).
    links.push(h("a", { href: "help.html", target: "_blank", rel: "noopener", title: "Open the full in-app help & operator docs (new tab)",
      style: "display:inline-flex;align-items:center;gap:5px" }, "Help",
      h("span", { "aria-hidden": "true", style: "display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;font-size:11px;font-weight:600;border:1px solid var(--color-divider);border-radius:50%;color:var(--color-neutral-600)" }, "?")));
    const wsColor = state.ws === "live" ? "#3f9e63" : state.ws === "connecting" ? "#c98a00" : "#c94b39";
    const wsLabel = state.ws === "live" ? "WS live" : state.ws === "connecting" ? "connecting…" : "offline";
    const showToggles = state.view === "nodes";
    const stat = (label, val) => h("div", { style: "display:flex;flex-direction:column;line-height:1.25" },
      h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600)" }, label),
      h("span", { style: "font-size:13px;font-weight:600" }, val));
    return h("div", { class: "nav", style: "flex:none;gap:var(--space-4);flex-wrap:nowrap;overflow:hidden" },
      h("span", { class: "nav-brand", style: "display:flex;align-items:center;gap:10px;letter-spacing:0.02em;flex:none;white-space:nowrap;margin-right:0" },
        h("span", { style: "width:12px;height:12px;background:var(--color-accent);display:block" }), "SWARM CONTROL"),
      ...links,
      h("div", { style: "display:flex;align-items:center;gap:var(--space-4);margin-left:auto;flex:none;white-space:nowrap" },
        showToggles ? h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Show/hide the node list rail", onClick: () => { state.leftOpen = !state.leftOpen; renderView(); } }, state.leftOpen ? "‹ Nodes" : "› Nodes") : null,
        showToggles ? h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Show/hide the command history & events rail", onClick: () => { state.rightOpen = !state.rightOpen; renderView(); } }, state.rightOpen ? "Activity ›" : "Activity ‹") : null,
        stat("Fleet", state.hub.nodes_online + " / " + state.hub.nodes_total + " online"),
        stat("Hub uptime", uptimeStr(state.hub.uptime_ms)),
        h("div", { title: "Live update channel to the hub (green live · amber connecting · red offline / demo)", style: "display:flex;align-items:center;gap:7px;border:1px solid var(--color-divider);padding:5px 10px" },
          h("span", { style: "width:8px;height:8px;border-radius:50%;display:block;background:" + wsColor }),
          h("span", { style: "font-size:12px;font-weight:600" }, wsLabel))));
  }

  function refreshNav() {
    const nav = document.querySelector(".nav");
    if (nav) nav.replaceWith(renderNav());
  }

  function switchView(v) {
    state.view = v;
    refreshNav();
    renderView();
  }

  // ---- view router -------------------------------------------------------
  function renderView() {
    const v = $("view");
    if (!v) return;
    disposeXterm();   // tear down any live terminal before the view host is cleared
    for (const k in ui) delete ui[k];
    v.innerHTML = "";
    if (state.view === "nodes") v.appendChild(buildNodesView());
    else if (state.view === "macros") v.appendChild(buildMacrosView());
    else if (state.view === "runbooks") v.appendChild(buildRunbooksView());
    else if (state.view === "events") v.appendChild(buildEventsView());
    else if (state.view === "settings") v.appendChild(buildSettingsView());
  }

  // ---- Nodes view --------------------------------------------------------
  function buildNodesView() {
    const wrap = h("div", { style: "flex:1;display:flex;min-height:0" });
    if (state.leftOpen) wrap.appendChild(buildLeftRail());
    wrap.appendChild(buildCenter());
    if (state.rightOpen) wrap.appendChild(buildRightRail());
    setTimeout(() => { renderNodeList(); mountConsole(); }, 0);
    return wrap;
  }

  function buildLeftRail() {
    const filter = h("input", { class: "input", placeholder: "Filter by id, label, group…", title: "Filter the list by node id, label, group or IP (client-side)", value: state.query,
      onInput: (e) => { state.query = e.target.value; renderNodeList(); } });
    const seg = h("div", { class: "seg", style: "align-self:flex-start", title: "Show all nodes, or only online / offline" },
      ...["all", "online", "offline"].map((f) => h("label", { class: "seg-opt", title: "Show " + f + " nodes" },
        h("input", { type: "radio", name: "statusf", checked: state.statusFilter === f, onChange: () => { state.statusFilter = f; renderNodeList(); } }), f)));
    ui.nodeList = h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" });
    return h("div", { style: "flex:none;width:304px;display:flex;flex-direction:column;border-right:2px solid var(--color-divider);min-height:0" },
      h("div", { style: "flex:none;padding:var(--space-3);display:flex;flex-direction:column;gap:var(--space-2);border-bottom:2px solid var(--color-divider)" },
        h("div", { style: "display:flex;align-items:center;gap:6px" }, h("h6", { style: "margin:0" }, "Nodes"), helpLink("nodes", "Nodes & the node list")), filter, seg),
      ui.nodeList,
      h("div", { style: "flex:none;padding:var(--space-2) var(--space-3);border-top:2px solid var(--color-divider);font-size:11px;color:var(--color-neutral-600)" },
        "Swarm TCP :" + state.hub.swarm_port + " · Browser :" + state.hub.web_port + " · mgmt VLAN"));
  }

  function renderNodeList() {
    if (!ui.nodeList) return;
    const q = state.query.trim().toLowerCase();
    const visible = state.nodes.filter((n) => {
      if (state.statusFilter === "online" && n.status !== "online") return false;
      if (state.statusFilter === "offline" && n.status === "online") return false;
      if (!q) return true;
      return (n.id + " " + n.label + " " + n.group + " " + n.ip).toLowerCase().includes(q);
    });
    ui.nodeList.innerHTML = "";
    for (const n of visible) {
      const on = n.status === "online", isSel = n.id === state.selId;
      const row = h("div", { class: "sc-row", title: "Select " + n.id + (n.label ? " (" + n.label + ")" : "") + " — " + (on ? "online" : "offline") + "; opens its serial console",
        style: "cursor:pointer;padding:var(--space-2) var(--space-3);border-bottom:1px solid var(--color-divider);border-left:" +
          (isSel ? "3px solid var(--color-accent)" : "3px solid transparent") + ";background:" + (isSel ? "var(--color-surface)" : "transparent") + ";opacity:" + (on ? 1 : 0.55),
        onClick: () => selectNode(n.id) },
        h("div", { style: "display:flex;align-items:center;gap:8px" },
          h("span", { style: "width:9px;height:9px;border-radius:50%;flex:none;background:" + (on ? "#3f9e63" : "transparent") + ";border:" + (on ? "none" : "2px solid #8c8683") + ";animation:" + (n.inflight > 0 ? "sc-pulse 1s infinite" : "none") }),
          h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:600" }, n.id),
          h("span", { style: "font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--color-neutral-700)" }, n.label),
          (on ? targetBadge(n.target) : null),
          (on ? promptBadge(n.promptState) : null),
          h("span", { style: "margin-left:auto;font-size:11px;color:var(--color-neutral-600);flex:none" }, rel(n.lastSeen))),
        h("div", { style: "display:flex;align-items:center;gap:8px;margin-top:4px;padding-left:17px" },
          h("span", { class: "tag tag-neutral", style: "padding:1px 7px" }, n.group || "—"),
          h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--color-neutral-600)" }, n.ip || ""),
          n.inflight > 0 ? h("span", { class: "tag tag-outline", style: "padding:1px 7px;animation:sc-pulse 1.2s infinite" }, n.inflight + " inflight") : null));
      ui.nodeList.appendChild(row);
    }
    if (!visible.length) ui.nodeList.appendChild(h("div", { style: "padding:var(--space-4);font-size:12px;color:var(--color-neutral-600)" }, "No nodes match."));
  }

  function buildCenter() {
    const s = sel() || {};
    const disabled = s.status !== "online";
    ui.header = h("div", { style: "flex:none;display:flex;align-items:stretch;border-bottom:2px solid var(--color-divider)" });
    renderHeaderInto(ui.header);

    ui.lineCount = h("span", { style: "font-size:11px;color:var(--color-neutral-600)" }, "0 lines buffered");
    const termBar = h("div", { style: "flex:none;display:flex;align-items:center;gap:var(--space-2);padding:6px var(--space-4);background:var(--color-surface);border-bottom:1px solid var(--color-divider)" },
      h("h6", { style: "margin:0;font-size:11px" }, "Serial console"), helpLink("console", "The serial console"), ui.lineCount,
      h("div", { style: "margin-left:auto;display:flex;gap:var(--space-1)" },
        (ui.autoBtn = h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Follow the live tail (auto-disables when you scroll up to read back)", onClick: toggleAutoscroll }, state.autoscroll ? "Autoscroll on" : "Autoscroll off")),
        (ui.wrapBtn = h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Soft-wrap long lines vs. horizontal scroll", onClick: toggleWrap }, state.wrap ? "Wrap on" : "Wrap off")),
        h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Clear the on-screen buffer for this node only (hub-stored output is untouched)", onClick: clearConsole }, "Clear"),
        h("button", { class: "btn btn-ghost", style: "font-size:12px", title: "Download this node's full stored serial output as a text file", onClick: downloadLog }, "Download log")));

    ui.term = h("div", { class: "sc-term", style: "flex:1;min-height:0;overflow-y:auto;background:#1b1918;padding:var(--space-3) var(--space-4);font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;font-size:12.5px;line-height:1.6",
      onScroll: (e) => { const el = e.target; const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24; if (atEnd !== state.autoscroll) { state.autoscroll = atEnd; if (ui.autoBtn) ui.autoBtn.textContent = atEnd ? "Autoscroll on" : "Autoscroll off"; } } });

    // Phase 3: progressive xterm.js surface. When window.Terminal is present
    // (vendored), the real terminal mounts into ui.xtermHost and ui.term stays
    // detached as the fallback buffer. Without it, ui.term is the live surface.
    let surface = ui.term;
    if (xtermAvailable()) { ui.xtermHost = h("div", { class: "sc-term", style: "flex:1;min-height:0;overflow:hidden;background:#1b1918;padding:4px 6px" }); surface = ui.xtermHost; }

    ui.expectBar = h("div", { style: "flex:none" });   // phase 5 live expect progress
    renderExpectBar();

    ui.otaBar = h("div", { style: "flex:none" });       // phase 12 live OTA progress
    renderOtaBar();

    ui.composerHost = h("div", { style: "flex:none" });
    renderComposer();
    return h("div", { style: "flex:1;display:flex;flex-direction:column;min-width:0;min-height:0" },
      ui.header, termBar, surface, ui.expectBar, ui.otaBar, ui.composerHost);
  }

  // Rebuild the composer in place. Called on node select and on input-mode
  // change, since the composer's shape depends on the selected node's mode.
  function renderComposer() {
    if (!ui.composerHost) return;
    const s = sel() || {};
    ui.composerHost.innerHTML = "";
    ui.composerHost.appendChild(buildComposer(s.status !== "online"));
  }

  function renderHeaderInto(host) {
    host.innerHTML = "";
    const s = sel() || {};
    const online = s.status === "online";
    const disabled = !online;
    host.appendChild(h("div", { style: "flex:1 1 auto;min-width:0;overflow:hidden;padding:var(--space-3) var(--space-4);display:flex;flex-direction:column;justify-content:center;gap:2px;border-right:1px solid var(--color-divider)" },
      h("div", { style: "display:flex;align-items:baseline;gap:10px;min-width:0" },
        h("h3", { style: "margin:0;font-family:ui-monospace,Menlo,monospace" }, s.id || "—"),
        h("span", { style: "font-size:14px;color:var(--color-neutral-700);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0" }, s.label || ""),
        h("span", { class: "tag " + (online ? "tag-accent" : "tag-neutral"), title: "node (Pico) link status" }, online ? "online" : "offline"),
        (online ? targetBadge(s.target, { big: true }) : null),
        (online ? promptBadge(s.promptState, { big: true }) : null)),
      h("div", { style: "display:flex;flex-wrap:wrap;gap:2px var(--space-4);font-size:12px;color:var(--color-neutral-700);font-family:ui-monospace,Menlo,monospace;overflow:hidden" },
        h("span", {}, s.ip || ""), h("span", {}, "fw " + (s.fw || "")), h("span", {}, "rtt " + (online ? (s.rttMs != null ? s.rttMs + "ms" : "—") : "—")),
        h("span", {}, "caps " + (s.caps || "")), h("span", {}, "group " + (s.group || "")),
        h("span", {}, "kbd: " + (s.layout || "us")),                     // phase 1: read-only layout
        (bridgePortFor(s.id) != null                                     // phase 8: bridge endpoint
          ? h("span", { style: "color:var(--color-accent-700)" }, "serial bridge: " + (state.bridge.bind || "0.0.0.0") + ":" + bridgePortFor(s.id))
          : null))));
    const hasNode = !!s.id;
    host.appendChild(h("div", { style: "flex:none;margin-left:auto;display:flex;align-items:center;gap:var(--space-2);padding:6px var(--space-4);flex-wrap:wrap;justify-content:flex-end" },
      h("button", { class: "btn btn-secondary", title: "Round-trip the node and refresh its RTT", onClick: doPing }, "Ping"),
      h("button", { class: "btn btn-secondary", title: "Ask the node to flush its serial receive buffer to the hub", onClick: doRead }, "Read serial"),
      helpLink("ping-read-reboot", "Ping · Read · Reboot"),
      h("button", { class: "btn btn-secondary", title: "Build a wait-for-output expect job (login flows, guided steps)", disabled: !hasNode, onClick: () => openExpectBuilder(s.id) }, "Expect"),      // phase 5
      helpLink("expect", "Expect engine"),
      h("button", { class: "btn btn-secondary", title: "Stage commands to deliver when this node next connects", disabled: !hasNode, onClick: () => openQueueSheet(s.id) }, "Queue"),         // phase 6
      helpLink("offline-queue", "Offline command queue"),
      h("button", { class: "btn btn-secondary", title: "Replay this node's recorded serial session (asciicast)", disabled: !hasNode, onClick: () => openReplay(s.id) }, "Replay"),            // phase 7
      helpLink("session-recording", "Session recording & replay"),
      h("button", { class: "btn btn-secondary", title: "Expose this node's raw serial as a TCP port (minicom/PuTTY)", disabled: !hasNode, onClick: () => openBridgeSheet(s.id) }, "Bridge"),       // phase 8
      helpLink("serial-bridge", "Raw serial bridge"),
      (nodeHasOta(s) ? h("button", { class: "btn btn-secondary", title: "Push a firmware bundle to this node (chunked, checksummed, auto-revert)", disabled: !hasNode, onClick: () => openOtaSheet(s.id) }, "Firmware") : null),  // phase 12
      (nodeHasOta(s) ? helpLink("ota", "OTA firmware updates") : null),
      h("button", { class: "btn btn-secondary", style: "color:var(--color-accent-700)", title: "Reboot the Pico node (NOT the target machine) — drops its socket briefly", onClick: doRebootNode }, "Reboot node"),
      helpLink("ping-read-reboot", "Reboot node")));
  }

  const kicker = (t) => h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600);width:52px;flex:none" }, t);

  // The composer's shape depends on the selected node's input mode. HID mode is
  // the original keystroke composer, unchanged. Serial mode captures keystrokes
  // and streams them to the target's serial getty via `send` commands.
  function buildComposer(disabled) {
    const node = sel();
    const serialOk = nodeHasSerialTx(node);
    const mode = effectiveMode(node);
    const body = mode === "serial" ? buildSerialComposer(disabled) : buildHidComposer(disabled);
    return h("div", { style: "flex:none;border-top:2px solid var(--color-divider);padding:var(--space-3) var(--space-4);display:flex;flex-direction:column;gap:var(--space-2)" },
      buildModeRow(node, serialOk, mode), ...body);
  }

  // HID | Serial toggle. Serial is greyed with a tooltip when the node's
  // firmware doesn't advertise serial_tx.
  function buildModeRow(node, serialOk, mode) {
    const pick = (m) => {
      if (m === "serial" && !serialOk) return;
      setInputMode(node && node.id, m);
      flushSerial();               // don't leave buffered keystrokes on a mode flip
      renderComposer();
    };
    const opt = (m, label, allowed, tip) => h("label", { class: "seg-opt", title: tip || "",
      style: allowed ? "" : "opacity:0.45;cursor:not-allowed" },
      h("input", { type: "radio", name: "inputmode", checked: mode === m, disabled: !allowed, onChange: () => pick(m) }), label);
    const seg = h("div", { class: "seg", style: "align-self:flex-start" },
      opt("hid", "HID", true, "Keystrokes over USB HID — works at BIOS, GRUB and consoles with no serial getty."),
      opt("serial", "Serial", serialOk,
        serialOk ? "Bytes into the target's serial getty — a real interactive Linux login."
                 : "Unavailable: this node's firmware does not advertise serial_tx (older build)."));
    const wantsSerial = node && inputMode(node.id) === "serial";
    const note = h("span", { style: "font-size:11px;color:var(--color-neutral-600)" },
      mode === "serial"
        ? "keystrokes stream to the serial getty · echo returns in the console · no local echo"
        : (wantsSerial && !serialOk
            ? "Serial unavailable — firmware lacks serial_tx; using HID"
            : "HID keystrokes → keyboard console (tty1 / BIOS / GRUB)"));
    return h("div", { style: "display:flex;align-items:center;gap:var(--space-3);flex-wrap:wrap" }, kicker("Input"), seg, helpLink("input-modes", "Input modes: HID vs Serial"), note);
  }

  function buildHidComposer(disabled) {
    ui.composerInput = h("input", { class: "input", style: "flex:1;font-family:ui-monospace,Menlo,monospace", value: state.input, disabled,
      title: "HID mode: types this text as USB keystrokes onto the target's keyboard console (tty1 / BIOS / GRUB). Enter to send.",
      placeholder: disabled ? "node offline — commands will fail" : "type into " + (state.selId || "node") + " serial…",
      onInput: (e) => { state.input = e.target.value; }, onKeyDown: (e) => { if (e.key === "Enter" && state.input.trim()) sendText(); } });
    const KEY_TIP = { "Enter": "Press Enter", "Tab": "Press Tab (completion)", "ESC": "Press Escape", "↑": "Up arrow", "↓": "Down arrow", "DEL": "Delete", "F2": "F2 (often BIOS setup)", "F12": "F12 (often boot menu)" };
    const keys = ["Enter", "Tab", "ESC", "↑", "↓", "DEL", "F2", "F12"].map((k) =>
      h("button", { class: "btn btn-secondary", style: "padding:3px 9px;font-size:12px", title: (KEY_TIP[k] || k) + " — sent as an HID key", disabled, onClick: () => sendKey(k) }, k));
    const CHORD_TIP = { "CTRL+C": "Interrupt (SIGINT) on tty1", "CTRL+D": "EOF / logout on tty1", "CTRL+ALT+DEL": "⚠ Reboots the target machine (confirm prompt when enabled)",
      "CTRL+ALT+F2": "Switch to virtual terminal 2", "ALT+SysRq+B": "⚠ Immediate SysRq reboot — no clean shutdown" };
    const chords = [["CTRL+C", "inherit"], ["CTRL+D", "inherit"], ["CTRL+ALT+DEL", "var(--color-accent-700)"], ["CTRL+ALT+F2", "inherit"], ["ALT+SysRq+B", "var(--color-accent-700)"]]
      .map(([label, color]) => h("button", { class: "btn btn-secondary", style: "padding:3px 9px;font-size:12px;color:" + color, title: CHORD_TIP[label] || label, disabled, onClick: () => sendChord(label) }, label));
    const macros = state.macros.slice(0, 4).map((m) => h("button", { class: "btn btn-secondary", style: "padding:3px 9px;font-size:12px", title: "Run macro “" + m.name + "” on " + (state.selId || "this node"), disabled, onClick: () => runMacroOn(m.id, state.selId) }, m.name));
    return [
      h("div", { style: "display:flex;gap:var(--space-2)" }, ui.composerInput,
        h("button", { class: "btn btn-primary", title: "Type the text above onto the target as HID keystrokes", disabled, onClick: sendText }, "Send")),
      h("div", { style: "display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap" }, kicker("Keys"), helpLink("control-bytes", "Keys & chords"), ...keys,
        h("label", { title: "Append a newline after the typed text (i.e. press Enter)", style: "margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;color:var(--color-neutral-700)" },
          h("input", { type: "checkbox", checked: state.sendNewline, style: "accent-color:var(--color-accent)", onChange: (e) => { state.sendNewline = e.target.checked; } }), "append ⏎"),
        h("label", { title: "Per-character delay in ms — raise it for a target that drops fast keystrokes", style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--color-neutral-700)" }, "char delay",
          h("input", { class: "input", type: "number", min: "0", step: "5", value: state.charDelay, title: "Per-character HID delay (ms)", style: "width:64px;min-height:28px;padding:2px 6px",
            onChange: (e) => { state.charDelay = Number(e.target.value) || 0; } }), "ms")),
      h("div", { style: "display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap" }, kicker("Chords"), helpLink("control-bytes", "Control keys & chords"), ...chords),
      h("div", { style: "display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap" }, kicker("Macros"), helpLink("macros", "Macros"), ...macros),
    ];
  }

  function buildSerialComposer(disabled) {
    // The input is a capture surface: keystrokes are intercepted and streamed to
    // the getty (no local echo — the getty echoes back through `output`). The
    // field itself stays empty.
    ui.composerInput = h("input", { class: "input", style: "flex:1;font-family:ui-monospace,Menlo,monospace", value: "", disabled,
      title: "Serial mode: keystrokes stream straight into the target's serial getty. No local echo — the getty echoes back in the console.",
      placeholder: disabled ? "node offline — serial disabled" : "serial: keystrokes stream live to " + (state.selId || "node") + " (no local echo)",
      onKeyDown: serialKeydown, onPaste: serialPaste });
    const ctl = (label, fn, color, tip) => h("button", { class: "btn btn-secondary", style: "padding:3px 9px;font-size:12px" + (color ? ";color:" + color : ""), title: tip || label, disabled, onClick: fn }, label);
    const controls = [
      ctl("⏎ Enter", () => serialEnter(), null, "Send a carriage return (0d) — the getty expects CR"),
      ctl("⌫ Bksp", () => serialSendRaw("7f"), null, "Backspace / delete (raw 7f)"),
      ctl("⇥ Tab", () => serialSendData("\t"), null, "Tab (09) — shell completion"),
      ctl("Esc", () => serialSendRaw("1b"), null, "Escape (raw 1b)"),
      ctl("Ctrl-C", () => serialSendRaw("03"), "var(--color-accent-700)", "Interrupt the running command (raw 03)"),
      ctl("Ctrl-D", () => serialSendRaw("04"), "var(--color-accent-700)", "EOF / logout (raw 04)"),
      ctl("Ctrl-Z", () => serialSendRaw("1a"), "var(--color-accent-700)", "Suspend to background (raw 1a)"),
    ];
    return [
      h("div", { style: "display:flex;gap:var(--space-2)" }, ui.composerInput,
        h("button", { class: "btn btn-primary", title: "Send a carriage return (0d) to the serial getty", disabled, onClick: () => serialEnter() }, "Enter")),
      h("div", { style: "display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap" }, kicker("Serial"), helpLink("control-bytes", "Serial control bytes"), ...controls),
    ];
  }

  function buildRightRail() {
    const seg = h("div", { class: "seg", style: "margin:var(--space-3) 0 var(--space-3) var(--space-3);align-self:flex-start" },
      h("label", { class: "seg-opt", title: "Every command sent to a node and its outcome" }, h("input", { type: "radio", name: "railtab", checked: state.tab === "history", onChange: () => { state.tab = "history"; renderRail(); } }), "Command history"),
      h("label", { class: "seg-opt", title: "Live hub events — registrations, heartbeats, failures" }, h("input", { type: "radio", name: "railtab", checked: state.tab === "events", onChange: () => { state.tab = "events"; renderRail(); } }), "Events"));
    ui.rail = h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0;border-top:2px solid var(--color-divider)" });
    setTimeout(renderRail, 0);
    return h("div", { style: "flex:none;width:324px;display:flex;flex-direction:column;border-left:2px solid var(--color-divider);min-height:0" },
      h("div", { style: "display:flex;align-items:center;gap:8px" }, seg, helpLink("events", "Events & command audit")), ui.rail);
  }

  function renderRail() {
    if (!ui.rail) return;
    ui.rail.innerHTML = "";
    if (state.tab === "history") {
      for (const c of state.history) {
        const tagClass = c.status === "failed" || c.status === "timeout" ? "tag-accent" : c.status === "pending" || c.status === "sent" ? "tag-outline" : "tag-neutral";
        ui.rail.appendChild(h("div", { style: "padding:var(--space-2) var(--space-3);border-bottom:1px solid var(--color-divider)" },
          h("div", { style: "display:flex;align-items:center;gap:8px" },
            h("span", { class: "tag " + tagClass, style: "padding:1px 7px;flex:none" }, c.status),
            h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0" }, c.text)),
          h("div", { style: "display:flex;align-items:center;gap:8px;margin-top:3px" },
            h("span", { style: "font-size:11px;color:var(--color-neutral-600);font-family:ui-monospace,Menlo,monospace" }, c.id + " · " + c.nodeId),
            h("span", { style: "font-size:11px;color:var(--color-neutral-600)" }, rel(c.ts)),
            h("button", { class: "btn btn-ghost", style: "margin-left:auto;font-size:11px;padding:1px 4px", title: "Re-send this command to " + c.nodeId, onClick: () => { selectNode(c.nodeId); sendRaw(c.text, "text"); } }, "Re-run"))));
      }
      if (!state.history.length) ui.rail.appendChild(empty("No commands yet."));
    } else {
      for (const e of state.events) {
        const tagClass = e.type === "failed" || e.type === "offline" || e.type === "node_down" || e.type === "error" ? "tag-accent" : "tag-neutral";
        ui.rail.appendChild(h("div", { style: "padding:var(--space-2) var(--space-3);border-bottom:1px solid var(--color-divider);display:flex;gap:8px;align-items:baseline" },
          h("span", { style: "font-size:11px;color:var(--color-neutral-600);font-family:ui-monospace,Menlo,monospace;flex:none" }, hhmmss(e.ts)),
          h("div", { style: "min-width:0" },
            h("div", { style: "display:flex;gap:6px;align-items:center" },
              h("span", { class: "tag " + tagClass, style: "padding:0 6px;font-size:10px" }, e.type),
              h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:11px;font-weight:600" }, e.nodeId || "hub")),
            h("div", { style: "font-size:12px;color:var(--color-neutral-700);margin-top:1px" }, e.detail || ""))));
      }
      if (!state.events.length) ui.rail.appendChild(empty("No events yet."));
    }
  }
  const empty = (t) => h("div", { style: "padding:var(--space-4);font-size:12px;color:var(--color-neutral-600)" }, t);

  // ---- Macros view -------------------------------------------------------
  function stepDisplay(st) {
    if (typeof st === "string") { const p = st.split(" "); return { op: p[0], arg: p.slice(1).join(" ") }; }
    if (st.delay_ms != null) return { op: "WAIT", arg: st.delay_ms + "ms" };
    if (st.type === "type") return { op: "TYPE", arg: (st.text || "").replace(/\n/g, "⏎") };
    if (st.type === "keys") return { op: "KEY", arg: (st.chord || []).join("+") };
    return { op: "?", arg: JSON.stringify(st) };
  }
  function buildMacrosView() {
    const rows = h("tbody");
    for (const m of state.macros) {
      const isSel = m.id === state.selMacro;
      rows.appendChild(h("tr", { style: "cursor:pointer;background:" + (isSel ? "var(--color-surface)" : "transparent") + ";border-left:" + (isSel ? "3px solid var(--color-accent)" : "3px solid transparent"),
        onClick: () => { state.selMacro = m.id; renderView(); } },
        h("td", { style: "font-weight:600" }, m.name),
        h("td", {}, h("span", { class: "tag tag-neutral", style: "padding:1px 7px" }, m.group || "—")),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, (m.steps || []).length + " steps"),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, (m.runs || 0) + " runs"),
        h("td", { style: "font-size:12px;color:var(--color-neutral-700)" }, m.lastRun ? rel(m.lastRun) : "—"),
        h("td", {}, h("button", { class: "btn btn-secondary", style: "padding:2px 9px;font-size:12px", title: "Run “" + m.name + "” on the selected node (" + (state.selId || "none") + ")", onClick: (e) => { e.stopPropagation(); runMacroOn(m.id, state.selId); } }, "Run"))));
    }
    const left = h("div", { style: "flex:1 1 auto;min-width:0;display:flex;flex-direction:column;min-height:0;border-right:2px solid var(--color-divider)" },
      h("div", { style: "flex:none;display:flex;align-items:center;gap:var(--space-3);padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider)" },
        h("h4", { style: "margin:0" }, "Macros"), helpLink("macros", "Macros"),
        h("span", { style: "font-size:12px;color:var(--color-neutral-600)" }, "stored on the hub · replayed as HID + serial steps"),
        h("button", { class: "btn btn-primary", style: "margin-left:auto;flex:none", title: "Create a new reusable macro", onClick: () => openMacroEditor(null) }, "New macro")),
      state.macros.length
        ? h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" },
            h("table", { class: "table", style: "width:100%" },
              h("thead", {}, h("tr", {}, ...["Name", "Group", "Steps", "Runs", "Last run", ""].map((t) => h("th", {}, t)))), rows))
        : h("div", { style: "flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--color-neutral-600);padding:var(--space-4)" },
            h("div", { style: "font-size:13px;font-weight:600" }, "No macros yet"),
            h("div", { style: "font-size:12px;max-width:340px;text-align:center;line-height:1.5" }, "Build a reusable sequence — type commands, send keys, insert waits — then replay it on any node in one click."),
            h("button", { class: "btn btn-primary", onClick: () => openMacroEditor(null) }, "Create your first macro")));

    const m = selMacro();
    const stepList = h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" });
    (m ? m.steps : []).forEach((st, i) => {
      const d = stepDisplay(st);
      stepList.appendChild(h("div", { style: "display:flex;gap:10px;align-items:baseline;padding:var(--space-2) var(--space-4);border-bottom:1px solid var(--color-divider)" },
        h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--color-neutral-600);flex:none" }, String(i + 1).padStart(2, "0")),
        h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;font-weight:600;flex:none;width:52px;color:" + (d.op === "CHORD" || d.op === "KEY" ? "var(--color-accent-700)" : "var(--color-neutral-700)") }, d.op),
        h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;min-width:0;word-break:break-all" }, d.arg)));
    });
    const right = h("div", { style: "flex:none;width:360px;display:flex;flex-direction:column;min-height:0" },
      h("div", { style: "flex:none;padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider)" },
        h("div", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600)" }, "Step sequence"),
        h("h5", { style: "margin:2px 0 0" }, m ? m.name : "—"),
        h("span", { class: "tag tag-outline", style: "padding:1px 7px;margin-top:6px;display:inline-block" }, m ? (m.group || "—") : "—")),
      stepList,
      h("div", { style: "flex:none;padding:var(--space-3) var(--space-4);border-top:2px solid var(--color-divider);display:flex;gap:var(--space-2)" },
        h("button", { class: "btn btn-secondary", disabled: !m, title: "Edit this macro's steps", onClick: () => m && openMacroEditor(m) }, "Edit"),
        h("button", { class: "btn btn-ghost", disabled: !m, style: "color:var(--color-accent)", title: "Delete this macro (confirms first)", onClick: () => m && deleteMacroById(m.id, m.name) }, "Delete"),
        h("button", { class: "btn btn-primary", style: "margin-left:auto", disabled: !m || !state.selId, title: "Replay this macro on the selected node", onClick: () => m && runMacroOn(m.id, state.selId) }, "Run on " + (state.selId || "—"))));
    return h("div", { style: "flex:1;display:flex;min-height:0" }, left, right);
  }

  // ---- macro editor ------------------------------------------------------
  // Reusable HID sequence builder. Steps are the same shape the firmware's
  // run_sequence understands: {type:"type",text}, {type:"keys",chord[]},
  // {delay_ms}. Create -> POST /macros, edit -> PATCH /macros/{id}.
  function normalizeStep(st) {
    if (st == null) return null;
    if (typeof st === "string") {
      const p = st.split(" "), op = (p[0] || "").toLowerCase(), arg = p.slice(1).join(" ");
      if (op === "type") return { type: "type", text: arg };
      if (op === "key" || op === "keys" || op === "chord") return { type: "keys", chord: arg.split("+").map((x) => x.trim().toUpperCase()).filter(Boolean) };
      if (op === "wait" || op === "delay") return { delay_ms: parseInt(arg, 10) || 0 };
      return null;
    }
    if (st.delay_ms != null) return { delay_ms: st.delay_ms };
    if (st.type === "type") return { type: "type", text: st.text || "" };
    if (st.type === "keys") return { type: "keys", chord: st.chord || [] };
    return null;
  }

  async function refreshMacros(selectId) {
    try {
      const d = await getJSON("/macros");
      state.macros = (d.macros || []).map((m) => ({ id: m.id, name: m.name, group: m.group, steps: m.steps, runs: m.runs || 0, lastRun: m.ageMs != null ? now() - m.ageMs : null }));
      if (selectId != null && state.macros.find((m) => m.id === selectId)) state.selMacro = selectId;
      else if (!state.macros.find((m) => m.id === state.selMacro)) state.selMacro = state.macros.length ? state.macros[0].id : null;
    } catch (e) { /* keep current view */ }
    if (state.view === "macros") renderView();
  }

  function deleteMacroById(id, name) {
    confirmDialog("Delete macro?", "Delete “" + (name || "this macro") + "”? This cannot be undone.", "Delete", async () => {
      try { await delJSON("/macros/" + id); if (state.selMacro === id) state.selMacro = null; toast("Macro", "Deleted " + (name || "")); await refreshMacros(); }
      catch (e) { toast("Failed", "could not delete macro"); }
    });
  }

  const closeMacroEditor = () => { const el = $("macro-editor"); if (el) el.remove(); };

  function openMacroEditor(existing) {
    const draft = {
      id: existing ? existing.id : null,
      name: existing ? existing.name : "",
      group: existing ? (existing.group || "") : "",
      dangerous: existing ? !!existing.dangerous : false,
      steps: existing ? (existing.steps || []).map(normalizeStep).filter(Boolean) : [],
    };
    let stepKind = "type";

    const stepsHost = h("div", { class: "sc-scroll", style: "max-height:240px;overflow-y:auto;border:1px solid var(--color-divider);border-radius:6px" });
    function renderSteps() {
      stepsHost.innerHTML = "";
      if (!draft.steps.length) { stepsHost.appendChild(h("div", { style: "padding:16px;font-size:12px;color:var(--color-neutral-600);text-align:center" }, "No steps yet — add one below.")); return; }
      draft.steps.forEach((st, i) => {
        const d = stepDisplay(st);
        const swap = (delta) => { const j = i + delta; if (j < 0 || j >= draft.steps.length) return; const t = draft.steps[i]; draft.steps[i] = draft.steps[j]; draft.steps[j] = t; renderSteps(); };
        stepsHost.appendChild(h("div", { style: "display:flex;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--color-divider)" },
          h("span", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600);width:20px;flex:none" }, String(i + 1).padStart(2, "0")),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:12px;font-weight:600;width:46px;flex:none;color:" + (d.op === "KEY" ? "var(--color-accent-700)" : "var(--color-neutral-700)") }, d.op),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:12px;min-width:0;flex:1;word-break:break-all" }, d.arg),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px", title: "Move up", onClick: () => swap(-1) }, "↑"),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px", title: "Move down", onClick: () => swap(1) }, "↓"),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px;color:var(--color-accent)", title: "Remove", onClick: () => { draft.steps.splice(i, 1); renderSteps(); } }, "×")));
      });
    }

    const composerHost = h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" });
    function renderComposer() {
      composerHost.innerHTML = "";
      if (stepKind === "type") {
        const txt = h("input", { class: "input", style: "flex:1;min-width:180px;font-family:ui-monospace,monospace", placeholder: "text to type, e.g.  systemctl status pve" });
        let nl = true;
        const add = () => { const v = txt.value; if (!v && !nl) return; draft.steps.push({ type: "type", text: nl ? v + "\n" : v }); txt.value = ""; renderSteps(); txt.focus(); };
        txt.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
        composerHost.append(txt,
          h("label", { style: "display:flex;align-items:center;gap:5px;font-size:12px;color:var(--color-neutral-700)" }, h("input", { type: "checkbox", checked: true, style: "accent-color:var(--color-accent)", onChange: (e) => { nl = e.target.checked; } }), "append ⏎"),
          h("button", { class: "btn btn-secondary", onClick: add }, "Add"));
      } else if (stepKind === "keys") {
        const chord = h("input", { class: "input", style: "flex:1;min-width:160px;font-family:ui-monospace,monospace", placeholder: "key or chord, e.g.  ENTER  or  CTRL+ALT+DELETE" });
        const add = () => { const parts = chord.value.split("+").map((p) => p.trim().toUpperCase()).filter(Boolean); if (!parts.length) return; draft.steps.push({ type: "keys", chord: parts }); chord.value = ""; renderSteps(); chord.focus(); };
        chord.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
        const chips = ["ENTER", "TAB", "ESCAPE", "CTRL+C", "CTRL+D", "CTRL+ALT+DELETE"].map((k) => h("button", { class: "btn btn-ghost", style: "font-size:11px;padding:2px 7px", onClick: () => { chord.value = k; chord.focus(); } }, k));
        composerHost.append(chord, h("button", { class: "btn btn-secondary", onClick: add }, "Add"),
          h("div", { style: "flex-basis:100%;display:flex;gap:6px;flex-wrap:wrap;margin-top:2px" }, ...chips));
      } else {
        const ms = h("input", { class: "input", type: "number", min: "0", step: "50", value: "500", style: "width:120px" });
        composerHost.append(h("span", { style: "font-size:12px;color:var(--color-neutral-700)" }, "wait"), ms, h("span", { style: "font-size:12px;color:var(--color-neutral-700)" }, "ms"),
          h("button", { class: "btn btn-secondary", onClick: () => { draft.steps.push({ delay_ms: Math.max(0, Number(ms.value) || 0) }); renderSteps(); } }, "Add"));
      }
    }

    const kindSeg = h("div", { class: "seg", style: "align-self:flex-start" },
      ...[["type", "Type text"], ["keys", "Key / chord"], ["delay", "Wait"]].map(([k, label]) =>
        h("label", { class: "seg-opt" }, h("input", { type: "radio", name: "stepkind", checked: stepKind === k, onChange: () => { stepKind = k; renderComposer(); } }), label)));

    const nameInput = h("input", { class: "input", style: "flex:1;min-width:180px", value: draft.name, placeholder: "macro name, e.g.  Reboot into BIOS", onInput: (e) => { draft.name = e.target.value; } });
    const groupInput = h("input", { class: "input", style: "width:170px", value: draft.group, placeholder: "group (optional)", onInput: (e) => { draft.group = e.target.value; } });

    async function save() {
      const name = draft.name.trim();
      if (!name) { toast("Failed", "Give the macro a name"); nameInput.focus(); return; }
      if (!draft.steps.length) { toast("Failed", "Add at least one step"); return; }
      const payload = { name, steps: draft.steps, group: draft.group.trim(), dangerous: draft.dangerous };
      try {
        let id = draft.id;
        if (id == null) { const r = await postJSON("/macros", payload); id = r.id; }
        else await patchJSON("/macros/" + id, payload);
        closeMacroEditor();
        toast("Macro", (draft.id == null ? "Created " : "Saved ") + name);
        await refreshMacros(id);
      } catch (e) { toast("Failed", "could not save macro"); }
    }

    const card = h("div", { class: "dialog", style: "width:min(700px,94vw);max-height:90vh;display:flex;flex-direction:column", onClick: (e) => e.stopPropagation() },
      h("div", { class: "dialog-title" }, draft.id == null ? "New macro" : "Edit macro"),
      h("div", { class: "dialog-body", style: "display:flex;flex-direction:column;gap:14px;overflow-y:auto" },
        h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" }, kicker("Name"), nameInput, groupInput),
        h("label", { style: "display:flex;align-items:center;gap:8px;font-size:12px;color:var(--color-neutral-700);cursor:pointer" },
          h("input", { type: "checkbox", checked: draft.dangerous, style: "accent-color:var(--color-accent)", onChange: (e) => { draft.dangerous = e.target.checked; } }),
          "Dangerous — require a confirmation prompt before this macro runs (reboots, power keys, destructive commands)"),
        h("div", { style: "border-top:1px solid var(--color-divider)" }),
        h("div", { style: "display:flex;flex-direction:column;gap:8px" }, kicker("Steps"), stepsHost),
        h("div", { style: "display:flex;flex-direction:column;gap:8px" }, kindSeg, composerHost),
        h("div", { style: "font-size:11px;color:var(--color-neutral-600);line-height:1.5" }, "Steps run top-to-bottom as one HID sequence and stop on the first error. Reorder with ↑ ↓, remove with ×.")),
      h("div", { class: "dialog-actions" },
        (draft.id != null ? h("button", { class: "btn btn-ghost", style: "margin-right:auto;color:var(--color-accent)", onClick: () => { const nm = draft.name; closeMacroEditor(); deleteMacroById(draft.id, nm); } }, "Delete") : null),
        h("button", { class: "btn btn-secondary", onClick: closeMacroEditor }, "Cancel"),
        h("button", { class: "btn btn-primary", title: "Save this macro on the hub", onClick: save }, draft.id == null ? "Create macro" : "Save changes")));

    const backdrop = h("div", { id: "macro-editor", class: "dialog-backdrop", style: "z-index:120", onClick: closeMacroEditor }, card);
    document.body.appendChild(backdrop);
    renderSteps();
    renderComposer();
    nameInput.focus();
  }

  // ---- Events view -------------------------------------------------------
  function buildEventsView() {
    const evRows = h("tbody");
    for (const e of state.events) {
      const tagClass = e.type === "failed" || e.type === "offline" || e.type === "node_down" || e.type === "error" ? "tag-accent" : "tag-neutral";
      evRows.appendChild(h("tr", {},
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, hhmmss(e.ts)),
        h("td", { style: "font-size:12px;color:var(--color-neutral-700)" }, rel(e.ts)),
        h("td", {}, h("span", { class: "tag " + tagClass, style: "padding:1px 7px" }, e.type)),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;font-weight:600" }, e.nodeId || "hub"),
        h("td", { style: "font-size:13px" }, e.detail || "")));
    }
    const cmdRows = h("tbody");
    for (const c of state.history) {
      const tagClass = c.status === "failed" || c.status === "timeout" ? "tag-accent" : c.status === "pending" || c.status === "sent" ? "tag-outline" : "tag-neutral";
      cmdRows.appendChild(h("tr", {},
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, c.id),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;font-weight:600" }, c.nodeId),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, c.text),
        h("td", {}, h("span", { class: "tag " + tagClass, style: "padding:1px 7px" }, c.status)),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, hhmmss(c.ts)),
        h("td", { style: "font-size:12px;color:var(--color-neutral-700)" }, rel(c.ts))));
    }
    const head = (cols) => h("thead", {}, h("tr", {}, ...cols.map((t) => h("th", {}, t))));
    return h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" },
      h("div", { style: "padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider);display:flex;align-items:center;gap:var(--space-3)" },
        h("div", {}, h("div", { style: "display:flex;align-items:center;gap:8px" }, h("h4", { style: "margin:0" }, "Events"), helpLink("events", "Events & audit")),
          h("span", { style: "font-size:12px;color:var(--color-neutral-700)" }, "hub journal — registrations, heartbeats, stale sweeps and failures")),
        h("a", { class: "btn btn-secondary", style: "margin-left:auto;flex:none", title: "Download the full event log as CSV", href: "/api/events/export", target: "_blank" }, "Export CSV")),
      h("table", { class: "table", style: "width:100%" }, head(["Time", "Age", "Type", "Node", "Detail"]), evRows),
      h("div", { style: "padding:var(--space-3) var(--space-4);border-top:2px solid var(--color-divider);border-bottom:2px solid var(--color-divider)" },
        h("h4", { style: "margin:0" }, "Command audit"),
        h("span", { style: "font-size:12px;color:var(--color-neutral-700)" }, "every command sent, its target and outcome")),
      h("table", { class: "table", style: "width:100%" }, head(["Command id", "Node", "Payload", "Status", "Time", "Age"]), cmdRows));
  }

  // ---- Settings view -----------------------------------------------------
  function buildSettingsView() {
    const s = state.settings;
    const fields = [
      ["hubName", "Hub name", "text", s.hubName || "pi-hub-01"],
      ["bind", "Bind address", "text", state.hub.bind || ""],
      ["swarmPort", "Swarm TCP port", "text", String(state.hub.swarm_port)],
      ["webPort", "Web UI port", "text", String(state.hub.web_port)],
      ["heartbeat", "Heartbeat interval (s)", "number", Math.round((s.heartbeat_interval_ms || 5000) / 1000)],
      ["staleAfter", "Mark offline after (s)", "number", Math.round((s.stale_timeout_ms || 15000) / 1000)],
      ["outputRetention", "Output retention (days)", "number", s.output_retention_days || 30],
      ["eventRetention", "Event retention (days)", "number", s.event_retention_days || 90],
    ];
    const grid = h("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:0;border-bottom:2px solid var(--color-divider)" });
    const edited = {};
    for (const [key, label, type, val] of fields) {
      edited[key] = val;
      grid.appendChild(h("div", { class: "field", style: "padding:var(--space-3) var(--space-4);border-right:1px solid var(--color-divider);border-bottom:1px solid var(--color-divider)" },
        h("label", {}, label),
        h("input", { class: "input", type, value: val, onChange: (e) => { edited[key] = e.target.value; } })));
    }
    const toggles = [
      ["require_confirm_dangerous", "Require confirm for destructive keys", "CTRL+ALT+DEL, SysRq and reboot prompt first"],
      ["serial_bridge_enabled", "Serial bridge listeners", "Expose assigned nodes' raw serial over TCP (minicom -D tcp:hub:port)"],
      ["alerts_enabled", "Alerts", "Notify on node down / panic via webhook or ntfy"],
    ];
    const toggleWrap = h("div", { style: "padding:var(--space-3) var(--space-4)" },
      h("div", { style: "display:flex;align-items:center;gap:6px;margin-bottom:var(--space-2)" },
        h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600)" }, "Safety"), helpLink("settings", "Safety toggles")));
    for (const [key, label, hint] of toggles) {
      toggleWrap.appendChild(h("label", { style: "display:flex;gap:12px;align-items:flex-start;padding:var(--space-2) 0;border-bottom:1px solid var(--color-divider);cursor:pointer" },
        h("input", { type: "checkbox", checked: !!s[key], style: "accent-color:var(--color-accent);margin-top:3px;flex:none", onChange: (e) => { edited[key] = e.target.checked; } }),
        h("span", { style: "min-width:0" },
          h("span", { style: "display:block;font-size:14px;font-weight:600" }, label),
          h("span", { style: "display:block;font-size:12px;color:var(--color-neutral-700)" }, hint))));
    }
    // Alerts endpoints (phase 11) — plain text inputs bound into `edited`.
    edited.alerts_webhook_url = s.alerts_webhook_url || "";
    edited.alerts_ntfy_url = s.alerts_ntfy_url || "";
    const alertsBlock = h("div", { style: "padding:var(--space-3) var(--space-4);border-top:1px solid var(--color-divider)" },
      h("div", { style: "display:flex;align-items:center;gap:6px;margin-bottom:var(--space-2)" },
        h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600)" }, "Alert endpoints"), helpLink("alerts", "Alerts")),
      h("div", { class: "field", style: "margin-bottom:var(--space-3)" },
        h("label", {}, "Webhook URL (POST JSON)"),
        h("input", { class: "input", type: "text", value: edited.alerts_webhook_url, placeholder: "https://…", onInput: (e) => { edited.alerts_webhook_url = e.target.value; } })),
      h("div", { class: "field" },
        h("label", {}, "ntfy topic URL"),
        h("input", { class: "input", type: "text", value: edited.alerts_ntfy_url, placeholder: "https://ntfy.sh/your-topic", onInput: (e) => { edited.alerts_ntfy_url = e.target.value; } })));
    return h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" },
      h("div", { style: "padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider);display:flex;align-items:center;gap:var(--space-3)" },
        h("div", { style: "min-width:0" }, h("div", { style: "display:flex;align-items:center;gap:8px" }, h("h4", { style: "margin:0" }, "Settings"), helpLink("settings", "Settings")),
          h("span", { style: "font-size:12px;color:var(--color-neutral-700)" }, "hub configuration — live values apply immediately; port changes need a restart")),
        h("button", { class: "btn btn-primary", style: "margin-left:auto;flex:none", title: "Write the changed settings to the hub (PATCH /api/settings)", onClick: () => saveSettings(edited) }, "Save config")),
      grid, toggleWrap, alertsBlock);
  }

  async function saveSettings(edited) {
    const patch = {};
    if (edited.heartbeat != null) patch.heartbeat_interval_ms = Number(edited.heartbeat) * 1000;
    if (edited.staleAfter != null) patch.stale_timeout_ms = Number(edited.staleAfter) * 1000;
    if (edited.outputRetention != null) patch.output_retention_days = Number(edited.outputRetention);
    if (edited.eventRetention != null) patch.event_retention_days = Number(edited.eventRetention);
    if (edited.require_confirm_dangerous != null) patch.require_confirm_dangerous = !!edited.require_confirm_dangerous;
    if (edited.serial_bridge_enabled != null) patch.serial_bridge_enabled = !!edited.serial_bridge_enabled;   // phase 8
    if (edited.alerts_enabled != null) patch.alerts_enabled = !!edited.alerts_enabled;                        // phase 11
    if (edited.alerts_webhook_url != null) patch.alerts_webhook_url = String(edited.alerts_webhook_url);
    if (edited.alerts_ntfy_url != null) patch.alerts_ntfy_url = String(edited.alerts_ntfy_url);
    if (state.demo) { Object.assign(state.settings, patch); toast("Saved", "Demo mode — not persisted"); return; }
    try { const r = await patchJSON("/settings", patch); state.settings = r.settings; toast("Saved", "Hub config written"); }
    catch (e) { toast("Failed", "Could not save settings"); }
  }

  // ---- actions -----------------------------------------------------------
  function needsConfirm(label) {
    const dangerous = /CTRL\+ALT\+DEL|SysRq/i.test(label);
    return dangerous && state.settings.require_confirm_dangerous !== false;
  }
  function selectNode(id) {
    if (state.selId === id) return;
    if (!state.demo && state.selId) wsSend({ type: "unsubscribe", node_id: state.selId });
    state.selId = id;
    state.input = "";
    serialBuf = "";  // don't carry a half-typed serial line across node switches
    if (state.view === "nodes") { renderNodeList(); renderHeaderInto(ui.header); renderComposer(); renderExpectBar(); renderOtaBar(); refreshConsole(); }
    if (!state.demo) { wsSend({ type: "subscribe", node_id: id }); backfillNode(id); }
  }
  // Rebuild the composer to reflect the selected node's current online/mode/caps.
  function rebuildComposerState() { renderComposer(); }
  async function backfillNode(id) {
    try {
      const out = await getJSON("/nodes/" + encodeURIComponent(id) + "/output?limit=400");
      state.consoles[id] = [];
      for (const c of (out.chunks || [])) pushOutputInto(id, c.text, c.ts, false);
      const cmds = await getJSON("/nodes/" + encodeURIComponent(id) + "/commands?limit=40");
      // merge node-scoped commands into history view (keep global list too)
      if (state.view === "nodes" && id === state.selId) refreshConsole();
    } catch (e) { /* offline */ }
  }

  function addHistory(cmdId, nodeId, text, status) {
    state.history.unshift({ id: cmdId, nodeId, text, status, ts: now() });
    state.history = state.history.slice(0, 60);
    if (state.view === "nodes" && state.tab === "history") renderRail();
    if (state.view === "events") renderView();
  }
  function setHistoryStatus(cmdId, status) {
    const row = state.history.find((c) => c.id === cmdId);
    if (row) row.status = status;
    if (state.view === "nodes" && state.tab === "history") renderRail();
    if (state.view === "events") renderView();
  }

  const sendText = () => { const t = state.input.trim(); if (t) sendRaw(state.input, "text"); };
  const sendKey = (k) => sendRaw(k, "key");
  function sendChord(label) {
    if (needsConfirm(label)) return confirmDialog("Send " + label + "?", "This chord can reset or interrupt the target machine. Send it to " + (state.selId || "the node") + "?", "Send " + label, () => sendRaw(label, "chord"));
    sendRaw(label, "chord");
  }

  async function sendRaw(text, kind) {
    const node = sel();
    if (!node) return;
    if (kind === "text") { pushLine(node.id, "in", text); if (ui.composerInput) ui.composerInput.value = ""; state.input = ""; }
    else pushLine(node.id, "in", text);

    if (state.demo) return demoSend(node, text, kind);

    let body;
    if (kind === "key") { body = { chord: [KEY_ALIASES[text] || text.toUpperCase()] }; return dispatchKeys(node.id, body.chord, text); }
    if (kind === "chord") { const chord = text.split("+").map((p) => p.trim().toUpperCase()); return dispatchKeys(node.id, chord, text); }
    // text
    const payload = { type: "type", text: state.sendNewline ? text + "\n" : text, char_delay_ms: state.charDelay || 0 };
    try {
      const r = await postJSON("/nodes/" + encodeURIComponent(node.id) + "/cmd", payload);
      if (r.ok) addHistory(r.cmd_id, node.id, text, "sent");
      else { pushLine(node.id, "err", "dispatch failed: " + (r.detail || r.error)); toast("Failed", node.id + " · " + (r.error || "error")); }
    } catch (e) { pushLine(node.id, "err", "dispatch error"); toast("Failed", node.id + " · network error"); }
  }
  async function dispatchKeys(nodeId, chord, label) {
    try {
      const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/keys", { chord });
      if (r.ok) addHistory(r.cmd_id, nodeId, label, "sent");
      else toast("Failed", nodeId + " · " + (r.error || "error"));
    } catch (e) { toast("Failed", nodeId + " · network error"); }
  }

  // ---- serial mode: capture keystrokes, stream to the getty --------------
  // Printable keys accumulate and flush on a short debounce so fast typing
  // becomes a handful of `send` frames per second, not one per character.
  // Enter, Backspace, Tab, Esc and Ctrl-C/D/Z flush and send immediately.
  // Nothing is echoed locally — the getty's echo returns via the output stream.
  let serialBuf = "";
  let serialTimer = null;
  let serialChain = Promise.resolve();
  const SERIAL_CHUNK = 1000;  // UTF-16 units per send; << the hub's 4096-byte cap

  function scheduleSerialFlush() {
    if (!serialTimer) serialTimer = setTimeout(flushSerial, 40);
  }
  function flushSerial() {
    if (serialTimer) { clearTimeout(serialTimer); serialTimer = null; }
    const buf = serialBuf; serialBuf = "";
    if (buf) serialSendData(buf);
  }
  function serialEnter() {
    const node = sel();
    if (!node || node.status !== "online") return;
    serialBuf += "\r";   // getty expects CR; keep it atomic with pending text
    flushSerial();
  }
  function serialKeydown(e) {
    const node = sel();
    if (!node || node.status !== "online") return;
    if (e.metaKey) return;  // leave OS/browser shortcuts (Cmd-C/V/…) alone
    if (e.ctrlKey && !e.altKey) {
      const k = e.key.toLowerCase();
      if (k === "c") { e.preventDefault(); flushSerial(); serialSendRaw("03"); return; }
      if (k === "d") { e.preventDefault(); flushSerial(); serialSendRaw("04"); return; }
      if (k === "z") { e.preventDefault(); flushSerial(); serialSendRaw("1a"); return; }
      return;  // let other Ctrl combos (paste, reload, select-all) through
    }
    if (e.key === "Enter") { e.preventDefault(); serialEnter(); return; }
    if (e.key === "Backspace") { e.preventDefault(); flushSerial(); serialSendRaw("7f"); return; }
    if (e.key === "Tab") { e.preventDefault(); serialBuf += "\t"; scheduleSerialFlush(); return; }
    if (e.key === "Escape") { e.preventDefault(); flushSerial(); serialSendRaw("1b"); return; }
    if (e.key.length === 1) { e.preventDefault(); serialBuf += e.key; scheduleSerialFlush(); }
  }
  function serialPaste(e) {
    e.preventDefault();
    const node = sel();
    if (!node || node.status !== "online") return;
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (text) { flushSerial(); serialSendData(text); }
  }
  function serialSendData(text) {
    const node = sel();
    if (!node || !text) return;
    for (let i = 0; i < text.length; i += SERIAL_CHUNK) {
      const chunk = text.slice(i, i + SERIAL_CHUNK);
      enqueueSerial(() => postSend(node.id, { data: chunk }));
    }
  }
  function serialSendRaw(hex) {
    const node = sel();
    if (!node) return;
    enqueueSerial(() => postSend(node.id, { raw: hex }));
  }
  // Serialize sends so frames reach the hub in the exact order typed/pasted.
  function enqueueSerial(fn) { serialChain = serialChain.then(fn).catch(() => {}); return serialChain; }

  async function postSend(nodeId, payload) {
    if (state.demo) return demoSerial(nodeId, payload);
    try {
      const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/cmd", Object.assign({ type: "send" }, payload));
      // No local echo and no history entry per keystroke — the getty echoes back
      // through `output`; only surface failures.
      if (!r.ok) { pushLine(nodeId, "err", "serial send failed: " + (r.detail || r.error)); toast("Failed", nodeId + " · " + (r.error || "send")); }
    } catch (e) { pushLine(nodeId, "err", "serial send error"); toast("Failed", nodeId + " · serial network error"); }
  }
  function demoSerial(nodeId, payload) {
    // Simulate a getty echo so serial mode is legible in offline demo mode.
    let text = payload.data;
    if (text == null && payload.raw != null) text = payload.raw === "0d" ? "\r" : "";
    if (text) pushLine(nodeId, "out", text);
  }

  async function doPing() {
    const node = sel(); if (!node) return;
    if (state.demo) { pushLine(node.id, "sys", "ping → pong " + (node.rttMs || 3) + "ms"); return; }
    try { const r = await postJSON("/nodes/" + encodeURIComponent(node.id) + "/ping"); if (r.ok) { node.rttMs = r.rtt_ms; pushLine(node.id, "sys", "ping → pong " + r.rtt_ms + "ms"); renderHeaderInto(ui.header); } else { toast("Failed", node.id + " · ping — no pong"); } }
    catch (e) { toast("Failed", node.id + " · ping error"); }
  }
  async function doRead() {
    const node = sel(); if (!node) return;
    if (state.demo) { pushLine(node.id, "sys", "flushed serial buffer — " + (12 + Math.floor(Math.random() * 90)) + " bytes"); return; }
    try { await postJSON("/nodes/" + encodeURIComponent(node.id) + "/read"); pushLine(node.id, "sys", "read requested — flushing serial buffer"); }
    catch (e) { toast("Failed", node.id + " · read error"); }
  }
  function doRebootNode() {
    const node = sel(); if (!node) return;
    confirmDialog("Reboot node " + node.id + "?",
      "The Pico reboots and drops its swarm socket briefly. The attached machine (" + (node.label || "") + ") is not affected — HID and serial reattach on reconnect.",
      "Reboot node", async () => {
        pushLine(node.id, "sys", "reboot requested — socket closing");
        if (state.demo) return demoReboot(node);
        try { await postJSON("/nodes/" + encodeURIComponent(node.id) + "/reboot"); toast("Rebooting", node.id + " · back shortly"); }
        catch (e) { toast("Failed", node.id + " · reboot error"); }
      });
  }
  async function runMacroOn(macroId, nodeId) {
    if (!nodeId) { toast("Failed", "Select a node first"); return; }
    const m = state.macros.find((x) => x.id === macroId); if (!m) return;
    if (state.view !== "nodes") switchView("nodes");
    if (state.demo) { m.steps.forEach((st) => { const d = stepDisplay(st); pushLine(nodeId, "in", d.op + " " + d.arg); }); toast("Macro", m.name + " → " + nodeId); return; }
    try { const r = await postJSON("/macros/" + macroId + "/run", { node_ids: [nodeId] }); const d = (r.dispatched || [])[0]; if (d && d.cmd_id) addHistory(d.cmd_id, nodeId, "macro:" + m.name, "sent"); toast("Macro", m.name + " → " + nodeId); }
    catch (e) { toast("Failed", "macro run error"); }
  }

  const toggleAutoscroll = () => { state.autoscroll = !state.autoscroll; if (ui.autoBtn) ui.autoBtn.textContent = state.autoscroll ? "Autoscroll on" : "Autoscroll off"; scrollDown(); };
  const toggleWrap = () => { state.wrap = !state.wrap; if (ui.wrapBtn) ui.wrapBtn.textContent = state.wrap ? "Wrap on" : "Wrap off"; rebuildConsole(); };
  const clearConsole = () => { state.consoles[state.selId] = []; refreshConsole(); };
  function downloadLog() {
    if (state.demo) { toast("Saved", state.selId + "-console.log (demo)"); return; }
    window.open("/api/nodes/" + encodeURIComponent(state.selId) + "/output/download", "_blank");
  }

  // ---- reusable modal sheet ----------------------------------------------
  // Same look as the macro editor, factored for the phase 5-9 dialogs.
  function openSheet(id, title, bodyNode, actions, width) {
    closeSheet(id);
    const card = h("div", { class: "dialog", style: "width:min(" + (width || 700) + "px,94vw);max-height:90vh;display:flex;flex-direction:column", onClick: (e) => e.stopPropagation() },
      h("div", { class: "dialog-title" }, title),
      h("div", { class: "dialog-body", style: "display:flex;flex-direction:column;gap:14px;overflow-y:auto", id: id + "-body" }, bodyNode),
      h("div", { class: "dialog-actions" }, ...(actions || [])));
    const backdrop = h("div", { id, class: "dialog-backdrop", style: "z-index:120", onClick: () => closeSheet(id) }, card);
    document.body.appendChild(backdrop);
    return backdrop;
  }
  const closeSheet = (id) => { const el = $(id); if (el) el.remove(); };

  // ---- Expect builder + live progress (phase 5) --------------------------
  function renderExpectBar() {
    if (!ui.expectBar) return;
    ui.expectBar.innerHTML = "";
    const job = state.expectJobs[state.selId];
    if (!job) return;
    const TERMINAL = { done: 1, failed: 1, cancelled: 1, timeout: 1 };
    const running = !TERMINAL[job.status] && !TERMINAL[job.phase];
    const bad = job.status === "failed" || job.status === "timeout" || job.phase === "failed" || job.phase === "timeout";
    const pct = job.total ? Math.round((job.step / job.total) * 100) : 0;
    const col = bad ? "var(--color-accent)" : running ? "var(--color-accent-600)" : "#3f9e63";
    ui.expectBar.appendChild(h("div", { style: "border-top:1px solid var(--color-divider);padding:6px var(--space-4);display:flex;align-items:center;gap:var(--space-3);background:var(--color-surface)" },
      h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600);flex:none" }, "Expect"),
      h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;flex:none" }, "step " + job.step + "/" + job.total),
      h("div", { style: "flex:1;height:6px;background:var(--color-neutral-300);min-width:60px" }, h("div", { style: "height:100%;width:" + pct + "%;background:" + col })),
      h("span", { style: "font-size:11px;color:var(--color-neutral-700);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, (job.phase || "") + (job.detail ? " · " + job.detail : "")),
      running && job.job_id
        ? h("button", { class: "btn btn-ghost", style: "flex:none;font-size:11px;color:var(--color-accent)", onClick: () => cancelExpect(state.selId, job.job_id) }, "Cancel")
        : h("button", { class: "btn btn-ghost", style: "flex:none;font-size:11px", onClick: () => { delete state.expectJobs[state.selId]; renderExpectBar(); } }, "Dismiss")));
  }
  async function cancelExpect(nodeId, jobId) {
    if (state.demo) { const j = state.expectJobs[nodeId]; if (j) { j.status = "cancelled"; j.phase = "cancelled"; } renderExpectBar(); return; }
    try { await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/expect/" + encodeURIComponent(jobId) + "/cancel"); }
    catch (e) { toast("Failed", "could not cancel expect job"); }
  }

  // Steps are the exact shapes the hub's expect engine parses: action
  // ({type:send/type/keys}), {delay_ms}, or {wait_for:{regex,timeout_ms,on_timeout}}.
  function openExpectBuilder(nodeId) {
    const steps = [];
    const listHost = h("div", { class: "sc-scroll", style: "max-height:230px;overflow-y:auto;border:1px solid var(--color-divider)" });
    function expectStepLabel(st) {
      if (st.wait_for) return { op: "WAIT", arg: "/" + st.wait_for.regex + "/ " + st.wait_for.timeout_ms + "ms · " + st.wait_for.on_timeout };
      if (st.delay_ms != null) return { op: "DELAY", arg: st.delay_ms + "ms" };
      if (st.type === "send") return { op: "SEND", arg: (st.data || "").replace(/\r?\n/g, "⏎") };
      if (st.type === "type") return { op: "TYPE", arg: (st.text || "").replace(/\n/g, "⏎") };
      if (st.type === "keys") return { op: "KEYS", arg: (st.chord || []).join("+") };
      return { op: "?", arg: JSON.stringify(st) };
    }
    function renderList() {
      listHost.innerHTML = "";
      if (!steps.length) { listHost.appendChild(h("div", { style: "padding:14px;font-size:12px;color:var(--color-neutral-600);text-align:center" }, "No steps yet — add an action or a wait-for below.")); return; }
      steps.forEach((st, i) => {
        const d = expectStepLabel(st);
        const swap = (delta) => { const j = i + delta; if (j < 0 || j >= steps.length) return; const t = steps[i]; steps[i] = steps[j]; steps[j] = t; renderList(); };
        listHost.appendChild(h("div", { style: "display:flex;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--color-divider)" },
          h("span", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600);width:20px;flex:none" }, String(i + 1).padStart(2, "0")),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:12px;font-weight:600;width:52px;flex:none;color:" + (d.op === "WAIT" ? "var(--color-accent-700)" : "var(--color-neutral-700)") }, d.op),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:12px;min-width:0;flex:1;word-break:break-all" }, d.arg),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px", onClick: () => swap(-1) }, "↑"),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px", onClick: () => swap(1) }, "↓"),
          h("button", { class: "btn btn-ghost", style: "padding:0 6px;color:var(--color-accent)", onClick: () => { steps.splice(i, 1); renderList(); } }, "×")));
      });
    }
    // action composer
    const sendTxt = h("input", { class: "input", style: "flex:1;min-width:160px;font-family:ui-monospace,monospace", placeholder: "send text, e.g.  root\\n  (\\n = newline)" });
    const addSend = () => { const v = sendTxt.value; if (!v) return; steps.push({ type: "send", data: v.replace(/\\n/g, "\n").replace(/\\t/g, "\t") }); sendTxt.value = ""; renderList(); };
    const reInput = h("input", { class: "input", style: "flex:1;min-width:140px;font-family:ui-monospace,monospace", placeholder: "wait-for regex, e.g.  login:" });
    const toMs = h("input", { class: "input", type: "number", min: "0", step: "500", value: "8000", style: "width:96px" });
    const onTo = h("select", { class: "input", style: "width:110px" }, h("option", { value: "fail" }, "fail"), h("option", { value: "continue" }, "continue"));
    const addWait = () => { const rx = reInput.value; if (!rx) return; steps.push({ wait_for: { regex: rx, timeout_ms: Math.max(0, Number(toMs.value) || 0), on_timeout: onTo.value } }); reInput.value = ""; renderList(); };
    const delMs = h("input", { class: "input", type: "number", min: "0", step: "100", value: "500", style: "width:96px" });

    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--color-neutral-700)" }, h("span", {}, "Ordered steps run on "), h("b", {}, nodeId), h("span", {}, " — actions alternate with wait-for matches on the node's live output."), helpLink("expect", "Expect engine")),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Steps"), listHost),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--color-divider);padding-top:10px" },
        kicker("Send"), sendTxt, h("button", { class: "btn btn-secondary", onClick: addSend }, "Add send")),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        kicker("Wait"), reInput,
        h("label", { style: "font-size:12px;color:var(--color-neutral-700);display:flex;align-items:center;gap:5px" }, "timeout", toMs, "ms"),
        onTo, h("button", { class: "btn btn-secondary", onClick: addWait }, "Add wait-for")),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        kicker("Delay"), h("label", { style: "font-size:12px;color:var(--color-neutral-700);display:flex;align-items:center;gap:5px" }, "wait", delMs, "ms"),
        h("button", { class: "btn btn-secondary", onClick: () => { steps.push({ delay_ms: Math.max(0, Number(delMs.value) || 0) }); renderList(); } }, "Add delay")));

    async function run() {
      if (!steps.length) { toast("Failed", "Add at least one step"); return; }
      if (state.demo) { closeSheet("expect-sheet"); runExpectDemo(nodeId, steps.slice()); return; }
      try {
        const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/expect", { steps });
        if (r.ok) { state.expectJobs[nodeId] = { job_id: r.job_id, step: 0, total: steps.length, phase: "started", status: r.status || "running" }; if (nodeId === state.selId) renderExpectBar(); toast("Expect", "job started on " + nodeId); closeSheet("expect-sheet"); }
        else toast("Failed", r.detail || r.error || "expect rejected");
      } catch (e) { toast("Failed", "expect request error"); }
    }
    openSheet("expect-sheet", "Expect — " + nodeId, body,
      [h("button", { class: "btn btn-secondary", onClick: () => closeSheet("expect-sheet") }, "Cancel"),
       h("button", { class: "btn btn-primary", title: "Start the expect job on " + nodeId + " (one running job per node)", onClick: run }, "Run expect")]);
    renderList();
  }
  function runExpectDemo(nodeId, steps) {
    let i = 0; const total = steps.length;
    state.expectJobs[nodeId] = { job_id: "demo", step: 0, total, phase: "started", status: "running" };
    if (nodeId === state.selId) renderExpectBar();
    const tick = () => {
      const job = state.expectJobs[nodeId]; if (!job || job.status === "cancelled") return;
      if (i >= total) { job.phase = "done"; job.status = "done"; if (nodeId === state.selId) renderExpectBar(); return; }
      job.step = i + 1; job.phase = steps[i].wait_for ? "wait" : "action"; job.detail = ""; i++;
      if (nodeId === state.selId) renderExpectBar();
      setTimeout(tick, 700);
    };
    setTimeout(tick, 500);
  }

  // ---- Offline command queue (phase 6) -----------------------------------
  function openQueueSheet(nodeId) {
    const listHost = h("div", { class: "sc-scroll", style: "max-height:240px;overflow-y:auto;border:1px solid var(--color-divider)" });
    async function refreshList() {
      listHost.innerHTML = "";
      let queued = [];
      if (state.demo) queued = (state._demoQueue && state._demoQueue[nodeId]) || [];
      else { try { const r = await getJSON("/nodes/" + encodeURIComponent(nodeId) + "/queue"); queued = r.queued || []; } catch (e) { queued = []; } }
      if (!queued.length) { listHost.appendChild(h("div", { style: "padding:14px;font-size:12px;color:var(--color-neutral-600);text-align:center" }, "Nothing queued.")); return; }
      for (const q of queued) {
        const p = q.payload || {};
        const label = p.type === "type" ? "type " + (p.text || "") : p.type === "send" ? "send " + (p.data || p.raw || "") : p.type === "keys" ? "keys " + (p.chord || []).join("+") : (q.type || p.type || "?");
        listHost.appendChild(h("div", { style: "display:flex;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--color-divider)" },
          h("span", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600);flex:none" }, "q" + q.id),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:12px;min-width:0;flex:1;word-break:break-all" }, label),
          h("span", { style: "font-size:11px;color:var(--color-neutral-600);flex:none" }, q.expires_at ? "ttl " + Math.max(0, Math.round((q.expires_at - now()) / 1000)) + "s" : "no expiry"),
          h("button", { class: "btn btn-ghost", style: "flex:none;color:var(--color-accent);font-size:11px", onClick: () => cancelQueued(q.id) }, "Cancel")));
      }
    }
    async function cancelQueued(qid) {
      if (state.demo) { state._demoQueue[nodeId] = (state._demoQueue[nodeId] || []).filter((x) => x.id !== qid); return refreshList(); }
      try { await delJSON("/nodes/" + encodeURIComponent(nodeId) + "/queue/" + qid); refreshList(); } catch (e) { toast("Failed", "could not cancel"); }
    }
    const cmdTxt = h("input", { class: "input", style: "flex:1;min-width:180px;font-family:ui-monospace,monospace", placeholder: "command to type on connect, e.g.  systemctl status" });
    const ttlMin = h("input", { class: "input", type: "number", min: "0", step: "5", value: "60", style: "width:90px" });
    async function add() {
      const v = cmdTxt.value.trim(); if (!v) return;
      const command = { type: "type", text: v + "\n" };
      const ttl_ms = Math.max(0, Number(ttlMin.value) || 0) * 60000;
      if (state.demo) { state._demoQueue = state._demoQueue || {}; (state._demoQueue[nodeId] = state._demoQueue[nodeId] || []).push({ id: ++seq, type: "type", payload: command, expires_at: ttl_ms ? now() + ttl_ms : null }); cmdTxt.value = ""; return refreshList(); }
      try { const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/queue", { command, ttl_ms }); if (r.ok) { cmdTxt.value = ""; toast("Queued", "q" + r.id + " → " + nodeId); refreshList(); } else toast("Failed", r.detail || r.error); }
      catch (e) { toast("Failed", "queue request error"); }
    }
    const online = (sel() || {}).status === "online";
    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--color-neutral-700)" }, h("span", {}, online ? "Node is online — queued commands drain immediately on submit." : "Node is offline — commands are held and delivered when it reconnects."), helpLink("offline-queue", "Offline command queue")),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Pending"), listHost),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--color-divider);padding-top:10px" },
        kicker("Add"), cmdTxt, h("label", { title: "Time-to-live in minutes (0 = never expires)", style: "font-size:12px;color:var(--color-neutral-700);display:flex;align-items:center;gap:5px" }, "ttl", ttlMin, "min"),
        h("button", { class: "btn btn-secondary", title: "Enqueue this command for delivery on connect", onClick: add }, "Queue")));
    openSheet("queue-sheet", "Offline queue — " + nodeId, body,
      [h("button", { class: "btn btn-primary", onClick: () => closeSheet("queue-sheet") }, "Done")], 600);
    refreshList();
  }

  // ---- Session replay (phase 7) ------------------------------------------
  function openReplay(nodeId) {
    const url = "/api/nodes/" + encodeURIComponent(nodeId) + "/session.cast";
    const hasPlayer = typeof window.AsciinemaPlayer === "object" && window.AsciinemaPlayer && typeof window.AsciinemaPlayer.create === "function";
    let body;
    if (state.demo) {
      body = h("div", { style: "font-size:13px;color:var(--color-neutral-700)" }, "Replay is unavailable in demo mode (no hub recording). Live, this opens the node's console as an asciicast.");
    } else if (hasPlayer) {
      const host = h("div", { style: "min-height:300px;background:#1b1918" });
      body = h("div", { style: "display:flex;flex-direction:column;gap:8px" },
        h("div", { style: "font-size:12px;color:var(--color-neutral-700)" }, "Replaying the console recording for ", h("b", {}, nodeId), " at real speed."), host);
      setTimeout(() => { try { window.AsciinemaPlayer.create(url, host, { fit: "width", terminalFontSize: "13px" }); } catch (e) { host.appendChild(h("div", { style: "padding:12px;color:#e0603c;font-size:12px" }, "player error — " + e.message)); } }, 0);
    } else {
      body = h("div", { style: "display:flex;flex-direction:column;gap:10px;font-size:13px;color:var(--color-neutral-700)" },
        h("div", {}, "The asciinema player isn't vendored, so this can't play inline. Download the recording and play it locally with ", h("code", {}, "asciinema play " + nodeId + ".cast"), "."),
        h("a", { class: "btn btn-primary", href: url, target: "_blank", style: "align-self:flex-start" }, "Download .cast"));
    }
    openSheet("replay-sheet", "Session replay — " + nodeId, body,
      [h("a", { class: "btn btn-secondary", href: url, target: "_blank" }, "Download .cast"),
       h("button", { class: "btn btn-primary", onClick: () => closeSheet("replay-sheet") }, "Close")], 760);
  }

  // ---- Serial bridge assign/unassign (phase 8) ---------------------------
  async function refreshBridge() {
    if (state.demo) return;
    try { const r = await getJSON("/bridge"); state.bridge = { enabled: !!r.enabled, bind: r.bind || "", ports: r.ports || [] }; }
    catch (e) { /* keep */ }
  }
  function openBridgeSheet(nodeId) {
    const cur = bridgePortFor(nodeId);
    const portInput = h("input", { class: "input", type: "number", min: "1024", max: "65535", value: cur || "7001", style: "width:120px" });
    const info = h("div", { style: "font-size:12px;color:var(--color-neutral-700)" });
    function renderInfo() {
      info.innerHTML = "";
      if (!state.bridge.enabled) info.appendChild(h("div", { style: "color:var(--color-accent-700)" }, "Serial bridge is disabled hub-wide — enable it in Settings for listeners to bind."));
      if (cur != null) info.appendChild(h("div", {}, "Assigned to " + (state.bridge.bind || "0.0.0.0") + ":" + cur + " — connect with ",
        h("code", {}, "minicom -D tcp:" + (state.hub.bind || location.hostname) + ":" + cur)));
      else info.appendChild(h("div", {}, "No port assigned. Pick a TCP port to expose this node's raw serial."));
    }
    renderInfo();
    async function assign() {
      const port = Number(portInput.value) || 0;
      if (port < 1024 || port > 65535) { toast("Failed", "port must be 1024-65535"); return; }
      if (state.demo) { state.bridge.ports = (state.bridge.ports || []).filter((p) => p.node_id !== nodeId).concat([{ node_id: nodeId, port }]); closeSheet("bridge-sheet"); if (ui.header) renderHeaderInto(ui.header); toast("Bridge", nodeId + " → :" + port); return; }
      try { const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/bridge?port=" + port); if (r.ok) { await refreshBridge(); closeSheet("bridge-sheet"); if (ui.header) renderHeaderInto(ui.header); toast("Bridge", nodeId + " → :" + port); } else toast("Failed", r.detail || r.error); }
      catch (e) { toast("Failed", "bridge assign error"); }
    }
    async function unassign() {
      if (state.demo) { state.bridge.ports = (state.bridge.ports || []).filter((p) => p.node_id !== nodeId); closeSheet("bridge-sheet"); if (ui.header) renderHeaderInto(ui.header); return; }
      try { await delJSON("/nodes/" + encodeURIComponent(nodeId) + "/bridge"); await refreshBridge(); closeSheet("bridge-sheet"); if (ui.header) renderHeaderInto(ui.header); toast("Bridge", "port removed"); }
      catch (e) { toast("Failed", "bridge remove error"); }
    }
    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "display:flex;align-items:center;gap:6px" }, info, helpLink("serial-bridge", "Raw serial bridge")),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" }, kicker("Port"), portInput,
        h("button", { class: "btn btn-secondary", title: "Expose this node's raw serial on the chosen TCP port (1024-65535)", onClick: assign }, cur != null ? "Reassign" : "Assign")));
    openSheet("bridge-sheet", "Serial bridge — " + nodeId, body,
      [cur != null ? h("button", { class: "btn btn-ghost", style: "margin-right:auto;color:var(--color-accent)", title: "Remove this node's bridge port assignment", onClick: unassign }, "Unassign") : null,
       h("button", { class: "btn btn-primary", onClick: () => closeSheet("bridge-sheet") }, "Done")], 560);
  }

  // ---- OTA firmware updates (phase 12) -----------------------------------
  // status ∈ running|committing|committed|healthy|unconfirmed|failed. Terminal
  // states: healthy (success), failed, unconfirmed (flashed but never confirmed
  // back healthy). Progress is driven by the ota_progress WS event and mirrored
  // into state.otaJobs; a per-node bar in the console view + the update sheet
  // both read from there.
  const OTA_TERMINAL = { healthy: 1, failed: 1, unconfirmed: 1 };
  const otaTerminal = (s) => !!OTA_TERMINAL[s];
  const shortSha = (s) => (s ? String(s).slice(0, 10) : "—");
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(2) + " MB";
  }
  // Hook so an open update sheet redraws its progress on each ota_progress event.
  let otaSheetHooks = null;   // { nodeId, render } or null

  function otaColors(job) {
    const bad = job.status === "failed" || job.status === "unconfirmed" || job.phase === "failed";
    const ok = job.status === "healthy";
    return { bad, ok, running: !otaTerminal(job.status),
      col: bad ? "var(--color-accent)" : ok ? "#3f9e63" : "var(--color-accent-600)" };
  }
  const otaStatusLabel = (job) => job.status === "healthy" ? "healthy — update confirmed"
    : job.status === "failed" ? "failed" : job.status === "unconfirmed" ? "unconfirmed — did not report healthy"
    : (job.phase || job.status || "running");

  // Compact live bar in the console view (mirrors renderExpectBar).
  function renderOtaBar() {
    if (!ui.otaBar) return;
    ui.otaBar.innerHTML = "";
    const job = state.otaJobs[state.selId];
    if (!job) return;
    const c = otaColors(job);
    const pct = job.total_bytes ? Math.round((job.sent_bytes / job.total_bytes) * 100) : (c.ok ? 100 : 0);
    ui.otaBar.appendChild(h("div", { style: "border-top:1px solid var(--color-divider);padding:6px var(--space-4);display:flex;align-items:center;gap:var(--space-3);background:var(--color-surface)" },
      h("span", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600);flex:none" }, "OTA"),
      h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px;flex:none" }, job.bundle || "firmware"),
      h("div", { style: "flex:1;height:6px;background:var(--color-neutral-300);min-width:60px" }, h("div", { style: "height:100%;width:" + pct + "%;background:" + c.col })),
      h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--color-neutral-700);flex:none" }, fmtBytes(job.sent_bytes) + " / " + fmtBytes(job.total_bytes)),
      h("span", { style: "font-size:11px;color:var(--color-neutral-700);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, otaStatusLabel(job) + (job.detail ? " · " + job.detail : "")),
      !c.running
        ? h("button", { class: "btn btn-ghost", style: "flex:none;font-size:11px", onClick: () => { delete state.otaJobs[state.selId]; renderOtaBar(); } }, "Dismiss")
        : null));
  }

  // Larger progress block used inside the update sheet.
  function otaProgressBlock(job) {
    if (!job) return h("div", { style: "font-size:12px;color:var(--color-neutral-600)" }, "No update in progress.");
    const c = otaColors(job);
    const pct = job.total_bytes ? Math.round((job.sent_bytes / job.total_bytes) * 100) : (c.ok ? 100 : 0);
    return h("div", { style: "display:flex;flex-direction:column;gap:8px;border:1px solid var(--color-divider);padding:12px" },
      h("div", { style: "display:flex;align-items:baseline;gap:10px" },
        h("span", { style: "font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:600" }, job.bundle || "firmware"),
        h("span", { class: "tag " + (c.bad ? "tag-accent" : c.ok ? "tag-neutral" : "tag-outline"), style: "padding:1px 7px" }, job.status),
        h("span", { style: "margin-left:auto;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--color-neutral-600)" }, fmtBytes(job.sent_bytes) + " / " + fmtBytes(job.total_bytes) + " · " + pct + "%")),
      h("div", { style: "height:8px;background:var(--color-neutral-300)" }, h("div", { style: "height:100%;width:" + pct + "%;background:" + c.col })),
      h("div", { style: "font-size:12px;color:" + (c.bad ? "var(--color-accent)" : "var(--color-neutral-700)") }, otaStatusLabel(job) + (job.detail ? " · " + job.detail : "")));
  }

  async function refreshBundles() {
    if (state.demo) return;
    try { const r = await getJSON("/ota/bundles"); state.bundles = r.bundles || []; }
    catch (e) { /* keep */ }
  }

  // FileReader → base64 (strip the "data:...;base64," prefix the API doesn't want).
  function readFileB64(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => { const s = String(fr.result || ""); const i = s.indexOf(","); resolve(i >= 0 ? s.slice(i + 1) : s); };
      fr.onerror = () => reject(fr.error || new Error("read error"));
      fr.readAsDataURL(file);
    });
  }

  // Per-node "Update firmware" sheet: pick a bundle, push, watch live progress.
  function openOtaSheet(nodeId) {
    const node = findNode(nodeId) || {};
    let picked = null;
    const listHost = h("div", { class: "sc-scroll", style: "max-height:200px;overflow-y:auto;border:1px solid var(--color-divider)" });
    const progressHost = h("div", {});
    const updateBtn = h("button", { class: "btn btn-primary", title: "Flash the selected bundle to " + nodeId + " — it reboots and must report healthy to confirm", onClick: () => run() }, "Update firmware");

    function renderProgress() {
      progressHost.innerHTML = "";
      const job = state.otaJobs[nodeId];
      progressHost.appendChild(otaProgressBlock(job));
      const running = job && !otaTerminal(job.status);
      const online = (findNode(nodeId) || {}).status === "online";
      updateBtn.disabled = !!running || !online || !picked;
      updateBtn.textContent = running ? "Updating…" : "Update firmware";
    }
    function renderBundles() {
      listHost.innerHTML = "";
      if (!state.bundles.length) { listHost.appendChild(h("div", { style: "padding:14px;font-size:12px;color:var(--color-neutral-600);text-align:center" }, "No bundles yet — open “Manage bundles” to create one.")); return; }
      for (const b of state.bundles) {
        const on = b.name === picked;
        listHost.appendChild(h("label", { style: "display:flex;gap:10px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--color-divider);cursor:pointer;background:" + (on ? "var(--color-surface)" : "transparent") },
          h("input", { type: "radio", name: "otabundle", checked: on, style: "accent-color:var(--color-accent);flex:none", onChange: () => { picked = b.name; renderBundles(); renderProgress(); } }),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:13px;font-weight:600;flex:none" }, b.name),
          h("span", { style: "font-size:11px;color:var(--color-neutral-600);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, (b.files || []).join(", ")),
          h("span", { style: "margin-left:auto;font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600);flex:none" }, shortSha(b.total_sha256))));
      }
    }
    async function run() {
      if (!picked) { toast("Failed", "pick a bundle"); return; }
      if ((findNode(nodeId) || {}).status !== "online") { toast("Failed", nodeId + " is offline"); return; }
      if (state.demo) { runOtaDemo(nodeId, picked); return; }
      try {
        const r = await postJSON("/nodes/" + encodeURIComponent(nodeId) + "/ota", { bundle: picked });
        if (r.ok) {
          state.otaJobs[nodeId] = { job_id: r.job_id, bundle: picked, status: "running", phase: "begin", sent_bytes: 0, total_bytes: r.total_bytes || 0, detail: "" };
          renderProgress(); if (nodeId === state.selId) renderOtaBar();
          toast("OTA", "update started on " + nodeId);
          pollOta(nodeId, r.job_id);
        } else toast("Failed", r.detail || r.error || "ota rejected");
      } catch (e) { toast("Failed", "ota request error"); }
    }

    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--color-neutral-700)" }, h("span", {}, "Push a firmware bundle to "), h("b", {}, nodeId),
        h("span", {}, ". The Pico flashes, reboots and must report "), h("b", {}, "healthy"), h("span", {}, " to confirm; a canary rollout can update a whole group."), helpLink("ota", "OTA firmware updates")),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Bundle"), listHost),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Progress"), progressHost));

    openSheet("ota-sheet", "Update firmware — " + nodeId, body,
      [h("button", { class: "btn btn-ghost", style: "margin-right:auto", title: "Create, view and roll out firmware bundles", onClick: () => openBundleManager() }, "Manage bundles…"),
       h("button", { class: "btn btn-secondary", onClick: () => closeOtaSheet() }, "Close"),
       updateBtn]);
    // Wire the backdrop close (openSheet's own onClick) to also drop the hook.
    const bd = $("ota-sheet");
    if (bd) bd.addEventListener("click", (e) => { if (e.target === bd) otaSheetHooks = null; });
    otaSheetHooks = { nodeId, render: renderProgress };
    renderBundles();
    renderProgress();
  }
  function closeOtaSheet() { otaSheetHooks = null; closeSheet("ota-sheet"); }

  // Poll the job as a belt-and-suspenders backup to the ota_progress WS events
  // (which are the primary channel) until it reaches a terminal state.
  async function pollOta(nodeId, jobId) {
    if (state.demo) return;
    let job;
    try { const r = await getJSON("/nodes/" + encodeURIComponent(nodeId) + "/ota/" + encodeURIComponent(jobId)); job = r && r.job; }
    catch (e) { job = null; }
    if (job) {
      state.otaJobs[nodeId] = { job_id: job.job_id, bundle: job.bundle, status: job.status, phase: job.phase,
        sent_bytes: job.sent_bytes || 0, total_bytes: job.total_bytes || 0, detail: job.detail || "" };
      if (nodeId === state.selId && state.view === "nodes") renderOtaBar();
      if (otaSheetHooks && otaSheetHooks.nodeId === nodeId) otaSheetHooks.render();
      if (otaTerminal(job.status)) return;
    }
    setTimeout(() => pollOta(nodeId, jobId), 1500);
  }

  // Bundle manager + rollout sheet.
  function openBundleManager() {
    otaSheetHooks = null;
    closeSheet("ota-sheet");
    const nameInput = h("input", { class: "input", style: "flex:1;min-width:180px;font-family:ui-monospace,monospace", placeholder: "bundle name, e.g.  fw-1.1.0" });
    const fileInput = h("input", { class: "input", type: "file", multiple: "multiple", style: "flex:1;min-width:200px" });
    const listHost = h("div", { class: "sc-scroll", style: "max-height:200px;overflow-y:auto;border:1px solid var(--color-divider)" });

    function renderList() {
      listHost.innerHTML = "";
      if (!state.bundles.length) { listHost.appendChild(h("div", { style: "padding:14px;font-size:12px;color:var(--color-neutral-600);text-align:center" }, "No bundles yet.")); return; }
      for (const b of state.bundles) {
        listHost.appendChild(h("div", { style: "padding:8px 10px;border-bottom:1px solid var(--color-divider)" },
          h("div", { style: "display:flex;gap:8px;align-items:baseline" },
            h("span", { style: "font-family:ui-monospace,monospace;font-size:13px;font-weight:600" }, b.name),
            h("span", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600)" }, "sha " + shortSha(b.total_sha256)),
            h("span", { style: "margin-left:auto;font-size:11px;color:var(--color-neutral-600)" }, b.created_at ? rel(b.created_at) : "")),
          h("div", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-700);margin-top:3px;word-break:break-all" }, (b.files || []).join(" · "))));
      }
    }
    async function create() {
      const name = nameInput.value.trim();
      if (!name) { toast("Failed", "name the bundle"); nameInput.focus(); return; }
      const fl = fileInput.files;
      if (!fl || !fl.length) { toast("Failed", "pick at least one file"); return; }
      if (state.demo) {
        state.bundles.unshift({ name, files: Array.from(fl).map((f) => f.name), total_sha256: (Math.random().toString(16).slice(2) + Math.random().toString(16).slice(2)), created_at: now() });
        nameInput.value = ""; fileInput.value = ""; renderList(); renderRolloutBundles(); toast("Bundle", "created " + name + " (demo)"); return;
      }
      try {
        const files = [];
        for (const f of Array.from(fl)) files.push({ path: f.name, content_b64: await readFileB64(f) });
        const r = await postJSON("/ota/bundles", { name, files });
        if (r.ok) { nameInput.value = ""; fileInput.value = ""; await refreshBundles(); renderList(); renderRolloutBundles(); toast("Bundle", "created " + name); }
        else toast("Failed", r.detail || r.error || "bundle rejected");
      } catch (e) { toast("Failed", "bundle upload error"); }
    }

    // Rollout controls: pick ota-capable nodes (or a group) + stagger.
    const rollMode = { by: "nodes" };
    let rollBundle = state.bundles.length ? state.bundles[0].name : null;
    const rollBundleHost = h("div", { style: "display:flex;gap:6px;flex-wrap:wrap" });
    function renderRolloutBundles() {
      rollBundleHost.innerHTML = "";
      if (!state.bundles.length) { rollBundleHost.appendChild(h("span", { style: "font-size:12px;color:var(--color-neutral-600)" }, "create a bundle first")); rollBundle = null; return; }
      if (!state.bundles.find((b) => b.name === rollBundle)) rollBundle = state.bundles[0].name;
      for (const b of state.bundles) rollBundleHost.appendChild(h("label", { class: "seg-opt", style: "border:1px solid var(--color-divider);padding:3px 8px" },
        h("input", { type: "radio", name: "rollbundle", checked: b.name === rollBundle, onChange: () => { rollBundle = b.name; } }), b.name));
    }
    const otaNodes = state.nodes.filter((n) => nodeHasOta(n));
    const nodeChecks = otaNodes.map((n) => ({ n, cb: h("input", { type: "checkbox", style: "accent-color:var(--color-accent)" }) }));
    const groups = Array.from(new Set(otaNodes.map((n) => n.group).filter(Boolean)));
    const groupSel = h("select", { class: "input", style: "width:200px" }, h("option", { value: "" }, "— pick a group —"), ...groups.map((g) => h("option", { value: g }, g)));
    const stagger = h("input", { class: "input", type: "number", min: "0", step: "500", value: "3000", style: "width:100px" });
    const nodesBox = otaNodes.length
      ? h("div", { style: "display:flex;flex-direction:column;gap:4px;max-height:150px;overflow-y:auto;border:1px solid var(--color-divider);padding:8px" },
          ...nodeChecks.map(({ n, cb }) => h("label", { style: "display:flex;gap:8px;align-items:center;font-size:13px;font-family:ui-monospace,monospace" }, cb,
            h("span", { style: "width:8px;height:8px;border-radius:50%;background:" + (n.status === "online" ? "#3f9e63" : "#8c8683") }), n.id,
            h("span", { style: "color:var(--color-neutral-600)" }, n.group || ""))))
      : h("div", { style: "font-size:12px;color:var(--color-neutral-600);padding:8px" }, "No ota-capable nodes.");
    const targetHost = h("div", {});
    function renderTargets() { targetHost.innerHTML = ""; targetHost.appendChild(rollMode.by === "group" ? groupSel : nodesBox); }
    const targetSeg = h("div", { class: "seg", style: "align-self:flex-start" },
      ...[["nodes", "Pick nodes"], ["group", "Whole group"]].map(([k, label]) => h("label", { class: "seg-opt" },
        h("input", { type: "radio", name: "rolltarget", checked: rollMode.by === k, onChange: () => { rollMode.by = k; renderTargets(); } }), label)));

    async function rollout() {
      if (!rollBundle) { toast("Failed", "create/pick a bundle"); return; }
      const stagger_ms = Math.max(0, Number(stagger.value) || 0);
      let ids;
      if (rollMode.by === "group") {
        if (!groupSel.value) { toast("Failed", "pick a group"); return; }
        ids = otaNodes.filter((n) => n.group === groupSel.value).map((n) => n.id);
      } else {
        ids = nodeChecks.filter((x) => x.cb.checked).map((x) => x.n.id);
      }
      if (!ids.length) { toast("Failed", "select at least one node"); return; }
      if (state.demo) { closeSheet("bundle-manager"); ids.forEach((id, i) => setTimeout(() => runOtaDemo(id, rollBundle), i * 700)); toast("Rollout", "canary rollout of " + rollBundle + " → " + ids.length + " node(s)"); return; }
      try {
        const r = await postJSON("/bulk/ota", { node_ids: ids, bundle: rollBundle, stagger_ms });
        if (r.ok) { closeSheet("bundle-manager"); toast("Rollout", "staged canary rollout started — watch per-node progress"); ids.forEach((id) => { if (findNode(id)) state.otaJobs[id] = { job_id: null, bundle: rollBundle, status: "running", phase: "queued", sent_bytes: 0, total_bytes: 0, detail: "canary rollout" }; }); if (state.selId && ids.indexOf(state.selId) >= 0) renderOtaBar(); }
        else toast("Failed", r.detail || r.error || "rollout rejected");
      } catch (e) { toast("Failed", "rollout request error"); }
    }

    const body = h("div", { style: "display:flex;flex-direction:column;gap:14px" },
      h("div", { style: "display:flex;flex-direction:column;gap:8px" }, kicker("Bundles"), listHost),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--color-divider);padding-top:10px" },
        kicker("New"), nameInput),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        kicker("Files"), fileInput, h("button", { class: "btn btn-secondary", title: "Read the chosen files in-browser and store them as a firmware bundle", onClick: create }, "Create bundle")),
      h("div", { style: "font-size:11px;color:var(--color-neutral-600);line-height:1.5" }, "Files are read in the browser and base64-encoded into the bundle manifest on the hub."),
      h("div", { style: "border-top:1px solid var(--color-divider)" }),
      h("div", { style: "font-size:12px;color:var(--color-neutral-700)" }, h("b", {}, "Canary rollout"), " — updates one node, waits for it to come back ", h("b", {}, "healthy"), ", then staggers the rest. Follow each node's progress bar in its console."),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Bundle"), rollBundleHost),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("Target"), targetSeg, targetHost),
      h("label", { style: "font-size:12px;color:var(--color-neutral-700);display:flex;align-items:center;gap:6px" }, "stagger", stagger, "ms between nodes"),
      h("div", { style: "display:flex;align-items:center;gap:8px;justify-content:flex-end" }, helpLink("ota", "Canary rollout"), h("button", { class: "btn btn-primary", title: "Canary rollout: update one node, wait for healthy, then stagger the rest", onClick: rollout }, "Start rollout")));

    openSheet("bundle-manager", "Firmware bundles", body,
      [h("button", { class: "btn btn-primary", onClick: () => closeSheet("bundle-manager") }, "Done")], 720);
    renderList();
    renderRolloutBundles();
    renderTargets();
  }

  // Demo: simulate a byte-streamed flash that reboots and comes back healthy.
  function runOtaDemo(nodeId, bundle) {
    const total = 262144;   // ~256 KB fake firmware
    const job = { job_id: "ota_demo" + (++seq), bundle, status: "running", phase: "begin", sent_bytes: 0, total_bytes: total, detail: "" };
    state.otaJobs[nodeId] = job;
    const refresh = () => { if (nodeId === state.selId && state.view === "nodes") renderOtaBar(); if (otaSheetHooks && otaSheetHooks.nodeId === nodeId) otaSheetHooks.render(); };
    refresh();
    const step = Math.round(total / 12);
    const tick = () => {
      const j = state.otaJobs[nodeId]; if (!j || j.job_id !== job.job_id) return;
      if (j.sent_bytes < total) {
        j.sent_bytes = Math.min(total, j.sent_bytes + step); j.phase = "transfer"; j.detail = "flashing"; refresh();
        setTimeout(tick, 260);
      } else if (j.status === "running") {
        j.status = "committing"; j.phase = "commit"; j.detail = "writing image"; refresh();
        setTimeout(() => { j.status = "committed"; j.phase = "reboot"; j.detail = "rebooting into new image"; refresh();
          setTimeout(() => { j.status = "healthy"; j.phase = "confirmed"; j.detail = "node reported healthy"; refresh();
            pushLine(nodeId, "sys", "OTA " + bundle + " → healthy"); }, 1400); }, 900);
      }
    };
    setTimeout(tick, 300);
  }

  // ---- Runbooks view (phase 9) -------------------------------------------
  const RUNBOOK_PLACEHOLDER = "name: log-in\nsteps:\n  - wait_for: \"login:\"\n    timeout_ms: 30000\n  - send: \"root\\n\"\n  - wait_for: \"[Pp]assword:\"\n  - send: \"toor\\n\"\n  - wait_for: \"[#$] $\"\n";
  const selRunbook = () => state.runbooks.find((r) => r.id === state.selRunbook) || null;
  async function refreshRunbooks(selectId) {
    if (state.demo) { if (state.view === "runbooks") renderView(); return; }
    try {
      const d = await getJSON("/runbooks");
      state.runbooks = (d.runbooks || []).map((r) => ({ id: r.id, name: r.name, yaml: r.yaml }));
      if (selectId != null && state.runbooks.find((r) => r.id === selectId)) state.selRunbook = selectId;
      else if (!state.runbooks.find((r) => r.id === state.selRunbook)) state.selRunbook = state.runbooks.length ? state.runbooks[0].id : null;
    } catch (e) { /* keep */ }
    if (state.view === "runbooks") renderView();
  }
  function buildRunbooksView() {
    const rows = h("tbody");
    for (const rb of state.runbooks) {
      const isSel = rb.id === state.selRunbook;
      rows.appendChild(h("tr", { style: "cursor:pointer;background:" + (isSel ? "var(--color-surface)" : "transparent") + ";border-left:" + (isSel ? "3px solid var(--color-accent)" : "3px solid transparent"),
        onClick: () => { state.selRunbook = rb.id; renderView(); } },
        h("td", { style: "font-weight:600" }, rb.name),
        h("td", { style: "font-family:ui-monospace,Menlo,monospace;font-size:12px" }, (rb.yaml || "").split("\n").filter((l) => /^\s*-/.test(l)).length + " steps"),
        h("td", {}, h("button", { class: "btn btn-secondary", style: "padding:2px 9px;font-size:12px", title: "Run “" + rb.name + "” across chosen nodes or a group", onClick: (e) => { e.stopPropagation(); openRunbookRun(rb); } }, "Run"))));
    }
    const left = h("div", { style: "flex:1 1 auto;min-width:0;display:flex;flex-direction:column;min-height:0;border-right:2px solid var(--color-divider)" },
      h("div", { style: "flex:none;display:flex;align-items:center;gap:var(--space-3);padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider)" },
        h("h4", { style: "margin:0" }, "Runbooks"), helpLink("runbooks", "Runbooks"),
        h("span", { style: "font-size:12px;color:var(--color-neutral-600)" }, "YAML expect flows, run across nodes or a group"),
        h("button", { class: "btn btn-primary", style: "margin-left:auto;flex:none", title: "Create a new YAML runbook", onClick: () => openRunbookEditor(null) }, "New runbook")),
      state.runbooks.length
        ? h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" },
            h("table", { class: "table", style: "width:100%" },
              h("thead", {}, h("tr", {}, ...["Name", "Steps", ""].map((t) => h("th", {}, t)))), rows))
        : h("div", { style: "flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--color-neutral-600);padding:var(--space-4)" },
            h("div", { style: "font-size:13px;font-weight:600" }, "No runbooks yet"),
            h("div", { style: "font-size:12px;max-width:360px;text-align:center;line-height:1.5" }, "A runbook is a YAML expect flow — wait for prompts, send responses — that you can run across a whole group at once."),
            h("button", { class: "btn btn-primary", onClick: () => openRunbookEditor(null) }, "Create your first runbook")));

    // active runs pane
    const runsHost = h("div", { class: "sc-scroll", style: "flex:1;overflow-y:auto;min-height:0" });
    const runIds = Object.keys(state.runbookRuns);
    if (!runIds.length) runsHost.appendChild(empty("No active or recent runs."));
    for (const rid of runIds.reverse()) {
      const run = state.runbookRuns[rid];
      const nodes = run.nodes || {};
      const chips = Object.keys(nodes).map((nid) => {
        const st = nodes[nid];
        const col = st === "failed" || st === "rejected" ? "var(--color-accent)" : st === "done" ? "#3f9e63" : st === "running" || st === "queued" ? "var(--color-accent-600)" : "var(--color-neutral-500)";
        return h("span", { style: "display:inline-flex;gap:5px;align-items:center;font-size:11px;font-family:ui-monospace,monospace;padding:2px 7px;border:1px solid var(--color-divider)" },
          h("span", { style: "width:7px;height:7px;border-radius:50%;background:" + col }), nid, h("span", { style: "color:var(--color-neutral-600)" }, st));
      });
      runsHost.appendChild(h("div", { style: "padding:var(--space-2) var(--space-4);border-bottom:1px solid var(--color-divider)" },
        h("div", { style: "display:flex;gap:8px;align-items:baseline" },
          h("span", { style: "font-weight:600;font-size:13px" }, run.name || "runbook"),
          h("span", { style: "font-family:ui-monospace,monospace;font-size:11px;color:var(--color-neutral-600)" }, rid)),
        h("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:6px" }, ...chips)));
    }

    const rb = selRunbook();
    const right = h("div", { style: "flex:none;width:400px;display:flex;flex-direction:column;min-height:0" },
      h("div", { style: "flex:none;padding:var(--space-3) var(--space-4);border-bottom:2px solid var(--color-divider)" },
        h("div", { style: "font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:var(--color-neutral-600)" }, "Runs"),
        h("h5", { style: "margin:2px 0 0" }, "Live progress")),
      runsHost,
      h("div", { style: "flex:none;padding:var(--space-3) var(--space-4);border-top:2px solid var(--color-divider);display:flex;gap:var(--space-2)" },
        h("button", { class: "btn btn-secondary", disabled: !rb, title: "Edit this runbook's YAML", onClick: () => rb && openRunbookEditor(rb) }, "Edit"),
        h("button", { class: "btn btn-ghost", disabled: !rb, style: "color:var(--color-accent)", title: "Delete this runbook (confirms first)", onClick: () => rb && deleteRunbook(rb) }, "Delete"),
        h("button", { class: "btn btn-primary", style: "margin-left:auto", disabled: !rb, title: "Run this runbook across nodes or a group", onClick: () => rb && openRunbookRun(rb) }, "Run")));
    return h("div", { style: "flex:1;display:flex;min-height:0" }, left, right);
  }
  function openRunbookEditor(existing) {
    const draft = { id: existing ? existing.id : null, name: existing ? existing.name : "", yaml: existing ? existing.yaml : RUNBOOK_PLACEHOLDER };
    const nameInput = h("input", { class: "input", style: "flex:1;min-width:180px", value: draft.name, placeholder: "runbook name", onInput: (e) => { draft.name = e.target.value; } });
    const yamlArea = h("textarea", { class: "input", style: "min-height:280px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;white-space:pre;overflow:auto", value: draft.yaml, onInput: (e) => { draft.yaml = e.target.value; } });
    async function save() {
      const name = draft.name.trim(); if (!name) { toast("Failed", "Give the runbook a name"); nameInput.focus(); return; }
      if (state.demo) { if (draft.id == null) { draft.id = ++seq; state.runbooks.push({ id: draft.id, name, yaml: draft.yaml }); } else { const r = selRunbookById(draft.id); if (r) { r.name = name; r.yaml = draft.yaml; } } state.selRunbook = draft.id; closeSheet("runbook-editor"); renderView(); return; }
      try {
        let id = draft.id;
        if (id == null) { const r = await postJSON("/runbooks", { name, yaml: draft.yaml }); if (!r.ok) { toast("Failed", r.detail || r.error); return; } id = r.id; }
        else { const r = await patchJSON("/runbooks/" + id, { name, yaml: draft.yaml }); if (r && r.ok === false) { toast("Failed", r.detail || r.error); return; } }
        closeSheet("runbook-editor"); toast("Runbook", (draft.id == null ? "Created " : "Saved ") + name); await refreshRunbooks(id);
      } catch (e) { toast("Failed", "could not save runbook (check YAML)"); }
    }
    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" }, kicker("Name"), nameInput),
      h("div", { style: "display:flex;flex-direction:column;gap:6px" }, kicker("YAML"), yamlArea),
      h("div", { style: "font-size:11px;color:var(--color-neutral-600);line-height:1.5" }, "Steps: ", h("code", {}, "wait_for"), " (with optional ", h("code", {}, "timeout_ms"), "), ", h("code", {}, "send"), ", ", h("code", {}, "delay_ms"), ". Validated by the hub on save."));
    openSheet("runbook-editor", draft.id == null ? "New runbook" : "Edit runbook", body,
      [draft.id != null ? h("button", { class: "btn btn-ghost", style: "margin-right:auto;color:var(--color-accent)", onClick: () => { const r = selRunbookById(draft.id); closeSheet("runbook-editor"); if (r) deleteRunbook(r); } }, "Delete") : null,
       h("button", { class: "btn btn-secondary", onClick: () => closeSheet("runbook-editor") }, "Cancel"),
       h("button", { class: "btn btn-primary", title: "Validate the YAML and save this runbook", onClick: save }, draft.id == null ? "Create" : "Save")], 720);
  }
  const selRunbookById = (id) => state.runbooks.find((r) => r.id === id) || null;
  function deleteRunbook(rb) {
    confirmDialog("Delete runbook?", "Delete “" + rb.name + "”? This cannot be undone.", "Delete", async () => {
      if (state.demo) { state.runbooks = state.runbooks.filter((x) => x.id !== rb.id); if (state.selRunbook === rb.id) state.selRunbook = state.runbooks.length ? state.runbooks[0].id : null; renderView(); return; }
      try { await delJSON("/runbooks/" + rb.id); toast("Runbook", "Deleted " + rb.name); await refreshRunbooks(); } catch (e) { toast("Failed", "could not delete"); }
    });
  }
  function openRunbookRun(rb) {
    const mode = { by: "nodes" };
    const nodeChecks = state.nodes.map((n) => ({ n, cb: h("input", { type: "checkbox", style: "accent-color:var(--color-accent)" }) }));
    const groups = Array.from(new Set(state.nodes.map((n) => n.group).filter(Boolean)));
    const groupSel = h("select", { class: "input", style: "width:200px" }, h("option", { value: "" }, "— pick a group —"), ...groups.map((g) => h("option", { value: g }, g)));
    const stagger = h("input", { class: "input", type: "number", min: "0", step: "100", value: "0", style: "width:100px" });
    const nodesBox = h("div", { style: "display:flex;flex-direction:column;gap:4px;max-height:180px;overflow-y:auto;border:1px solid var(--color-divider);padding:8px" },
      ...nodeChecks.map(({ n, cb }) => h("label", { style: "display:flex;gap:8px;align-items:center;font-size:13px;font-family:ui-monospace,monospace" }, cb,
        h("span", { style: "width:8px;height:8px;border-radius:50%;background:" + (n.status === "online" ? "#3f9e63" : "#8c8683") }), n.id,
        h("span", { style: "color:var(--color-neutral-600)" }, n.group || ""))));
    const targetHost = h("div", {});
    function renderTargets() { targetHost.innerHTML = ""; targetHost.appendChild(mode.by === "group" ? groupSel : nodesBox); }
    const seg = h("div", { class: "seg", style: "align-self:flex-start" },
      ...[["nodes", "Pick nodes"], ["group", "Whole group"]].map(([k, label]) => h("label", { class: "seg-opt" },
        h("input", { type: "radio", name: "rbtarget", checked: mode.by === k, onChange: () => { mode.by = k; renderTargets(); } }), label)));
    async function run() {
      const body = { stagger_ms: Math.max(0, Number(stagger.value) || 0) };
      if (mode.by === "group") { if (!groupSel.value) { toast("Failed", "pick a group"); return; } body.group = groupSel.value; }
      else { const ids = nodeChecks.filter((x) => x.cb.checked).map((x) => x.n.id); if (!ids.length) { toast("Failed", "select at least one node"); return; } body.node_ids = ids; }
      if (state.demo) { closeSheet("runbook-run"); runRunbookDemo(rb, body); return; }
      try {
        const r = await postJSON("/runbooks/" + rb.id + "/run", body);
        if (r.ok) { state.runbookRuns[r.run_id] = { name: rb.name, nodes: {} }; (r.nodes || []).forEach((nid) => { state.runbookRuns[r.run_id].nodes[nid] = "queued"; }); toast("Runbook", rb.name + " → " + (r.nodes || []).length + " node(s)"); closeSheet("runbook-run"); if (state.view === "runbooks") renderView(); }
        else toast("Failed", r.detail || r.error);
      } catch (e) { toast("Failed", "runbook run error"); }
    }
    const body = h("div", { style: "display:flex;flex-direction:column;gap:12px" },
      h("div", { style: "font-size:12px;color:var(--color-neutral-700)" }, "Run ", h("b", {}, rb.name), " across:"),
      seg, targetHost,
      h("label", { style: "font-size:12px;color:var(--color-neutral-700);display:flex;align-items:center;gap:6px" }, "stagger", stagger, "ms between nodes"));
    openSheet("runbook-run", "Run runbook — " + rb.name, body,
      [h("button", { class: "btn btn-secondary", onClick: () => closeSheet("runbook-run") }, "Cancel"),
       h("button", { class: "btn btn-primary", title: "Start this runbook on the selected targets (up to 128 nodes)", onClick: run }, "Run")], 620);
    renderTargets();
  }
  function runRunbookDemo(rb, body) {
    const rid = "rb_demo" + (++seq);
    let ids = body.node_ids || [];
    if (body.group) ids = state.nodes.filter((n) => n.group === body.group).map((n) => n.id);
    const nodes = {}; ids.forEach((id) => { nodes[id] = "running"; });
    state.runbookRuns[rid] = { name: rb.name, nodes };
    if (state.view === "runbooks") renderView();
    setTimeout(() => { ids.forEach((id) => { nodes[id] = "done"; }); if (state.view === "runbooks") renderView(); }, 1600);
  }

  // ---- WebSocket ---------------------------------------------------------
  let ws = null;
  function wsSend(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = () => { state.ws = "live"; refreshNav(); if (state.selId) wsSend({ type: "subscribe", node_id: state.selId }); };
    ws.onclose = () => { state.ws = "offline"; refreshNav(); setTimeout(connectWS, 2000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) {} };
  }

  function findNode(id) { return state.nodes.find((n) => n.id === id); }
  function onEvent(ev) {
    switch (ev.event) {
      case "hub_stats":
        state.hub.uptime_ms = ev.uptime_ms; state.hub.nodes_online = ev.nodes_online; state.hub.nodes_total = ev.nodes_total; refreshNav(); break;
      case "node_up": {
        const meta = ev.meta || {}; let n = findNode(ev.id);
        const rec = mergeMeta(n, meta, ev.id);
        if (!n) state.nodes.push(rec);
        if (state.view === "nodes") {
          renderNodeList();
          if (ev.id === state.selId) {
            renderHeaderInto(ui.header);
            renderComposer();  // caps may have changed on reconnect
            // A node in the Serial pref that reconnects on older firmware falls
            // back to HID (effectiveMode handles it) — say so.
            if (inputMode(ev.id) === "serial" && !nodeHasSerialTx(findNode(ev.id)))
              toast("Serial", ev.id + " · firmware lacks serial_tx — using HID");
          }
        }
        recomputeFleet(); break;
      }
      case "node_down": { const n = findNode(ev.id); if (n) n.status = "offline"; if (state.view === "nodes") { renderNodeList(); if (ev.id === state.selId) { renderHeaderInto(ui.header); rebuildComposerState(); } } recomputeFleet(); break; }
      case "node_updated": { const n = findNode(ev.id); if (n) { if (ev.label != null) n.label = ev.label; if (ev.group != null) n.group = ev.group; if (ev.status) n.status = ev.status; } if (state.view === "nodes") renderNodeList(); break; }
      case "heartbeat": { const n = findNode(ev.id); if (n) { n.lastSeen = ev.ts || now(); if (ev.rtt_ms != null) n.rttMs = ev.rtt_ms; if (ev.target != null) n.target = ev.target; } if (state.view === "nodes") { renderNodeList(); if (ev.id === state.selId) renderHeaderInto(ui.header); } break; }
      case "command_issued": { const n = findNode(ev.id); if (n) n.inflight = (n.inflight || 0) + 1; if (state.view === "nodes") renderNodeList(); break; }
      case "result": {
        const n = findNode(ev.id); if (n) n.inflight = Math.max(0, (n.inflight || 0) - 1);
        setHistoryStatus(ev.cmd_id, ev.status === "ok" ? "done" : ev.status);
        if (ev.payload && ev.id === state.selId) pushLine(ev.id, "sys", String(ev.payload));
        if (state.view === "nodes") renderNodeList();
        break;
      }
      case "output": if (ev.id === state.selId) pushOutput(ev.id, ev.text); break;
      case "node_state": {   // phase 4: live prompt-state badge
        const n = findNode(ev.id); if (n) n.promptState = ev.prompt_state;
        if (state.view === "nodes") { renderNodeList(); if (ev.id === state.selId && ui.header) renderHeaderInto(ui.header); }
        break;
      }
      case "expect_progress": {   // phase 5
        state.expectJobs[ev.id] = { job_id: ev.job_id, step: ev.step, total: ev.total, phase: ev.phase, detail: ev.detail,
          status: (ev.phase === "done" || ev.phase === "failed" || ev.phase === "cancelled" || ev.phase === "timeout") ? ev.phase : "running" };
        if (ev.id === state.selId && state.view === "nodes") renderExpectBar();
        break;
      }
      case "ota_progress": {   // phase 12: live firmware update progress
        const prev = state.otaJobs[ev.id] || {};
        state.otaJobs[ev.id] = {
          job_id: ev.job_id, bundle: ev.bundle != null ? ev.bundle : prev.bundle,
          status: ev.status, phase: ev.phase, detail: ev.detail,
          sent_bytes: ev.sent_bytes != null ? ev.sent_bytes : prev.sent_bytes || 0,
          total_bytes: ev.total_bytes != null ? ev.total_bytes : prev.total_bytes || 0,
        };
        if (ev.id === state.selId && state.view === "nodes") renderOtaBar();
        if (otaSheetHooks && otaSheetHooks.nodeId === ev.id) otaSheetHooks.render();
        break;
      }
      case "runbook_progress": {   // phase 9
        state.runbookRuns[ev.run_id] = { name: ev.name, nodes: ev.nodes || {} };
        if (state.view === "runbooks") renderView();
        break;
      }
      case "event": {
        state.events.unshift({ ts: ev.ts, type: ev.type, nodeId: ev.node_id, detail: ev.detail });
        state.events = state.events.slice(0, 100);
        if (state.view === "events") renderView(); else if (state.view === "nodes" && state.tab === "events") renderRail();
        break;
      }
    }
  }
  function mergeMeta(existing, meta, id) {
    const rec = existing || { id, inflight: 0 };
    rec.label = meta.label || rec.label || "";
    rec.group = meta.group || rec.group || "";
    rec.ip = meta.ip || rec.ip || "";
    rec.fw = meta.fw_version || rec.fw || "";
    rec.status = meta.status || "online";
    rec.rttMs = meta.rtt_ms != null ? meta.rtt_ms : rec.rttMs;
    rec.caps = (meta.capabilities || []).join(",") || rec.caps || "";
    rec.layout = meta.layout || rec.layout || "us";
    rec.promptState = meta.prompt_state != null ? meta.prompt_state : rec.promptState;
    rec.target = meta.target != null ? meta.target : (rec.target || "unknown");
    rec.lastSeen = meta.last_seen || now();
    rec.inflight = meta.inflight || 0;
    return rec;
  }
  function recomputeFleet() {
    state.hub.nodes_total = state.nodes.length;
    state.hub.nodes_online = state.nodes.filter((n) => n.status === "online").length;
    refreshNav();
  }

  // ---- load --------------------------------------------------------------
  function apiNodeToRec(n) {
    return { id: n.id, label: n.label || "", group: n.group || "", ip: n.ip || "", fw: n.fw_version || "",
      status: n.status, rttMs: n.rtt_ms, caps: (n.capabilities || []).join(","), lastSeen: n.last_seen || now(), inflight: n.inflight || 0,
      layout: n.layout || "us", promptState: n.prompt_state || null, target: n.target || "unknown" };
  }
  async function loadLive() {
    const health = await getJSON("/health");
    state.hub.uptime_ms = health.uptime_ms; state.hub.version = health.version;
    state.hub.bind = health.bind; state.hub.swarm_port = health.swarm_port; state.hub.web_port = health.web_port;
    state.hub.nodes_online = health.nodes_online; state.hub.nodes_total = health.nodes_total;
    const [nodes, macros, settings, events, bridge, runbooks, bundles] = await Promise.all([
      getJSON("/nodes"), getJSON("/macros"), getJSON("/settings"), getJSON("/events?limit=60"),
      getJSON("/bridge").catch(() => ({})), getJSON("/runbooks").catch(() => ({})),
      getJSON("/ota/bundles").catch(() => ({})),
    ]);
    state.nodes = (nodes.nodes || []).map(apiNodeToRec);
    state.macros = (macros.macros || []).map((m) => ({ id: m.id, name: m.name, group: m.group, steps: m.steps, runs: 0, lastRun: null }));
    state.settings = settings.settings || {};
    state.events = (events.events || []).map((e) => ({ ts: e.ts, type: e.type, nodeId: e.node_id, detail: e.detail }));
    state.bridge = { enabled: !!bridge.enabled, bind: bridge.bind || "", ports: bridge.ports || [] };
    state.runbooks = (runbooks.runbooks || []).map((r) => ({ id: r.id, name: r.name, yaml: r.yaml }));
    state.selRunbook = state.runbooks.length ? state.runbooks[0].id : null;
    state.bundles = bundles.bundles || [];
    if (!state.selId && state.nodes.length) state.selId = (state.nodes.find((n) => n.status === "online") || state.nodes[0]).id;
  }

  // ---- demo mode (no hub) ------------------------------------------------
  // Shown only when the hub API is unreachable, so the interface still renders if
  // someone opens the file directly. It carries NO deployment specifics — every
  // real value (nodes, hub address, counts, settings) comes from the hub API at
  // runtime. To populate the offline demo with your own fleet, drop a gitignored
  // `demo.json` next to this file; otherwise this generic placeholder set is used.
  // The shared source therefore stays free of node names, IPs, and credentials.
  const GENERIC_DEMO = {
    hub: { uptime_ms: 3 * 3600e3, bind: "hub.local", swarm_port: 9000, web_port: 8080, version: "demo" },
    settings: { heartbeat_interval_ms: 5000, stale_timeout_ms: 15000, output_retention_days: 30, event_retention_days: 90, require_confirm_dangerous: true },
    nodes: [
      { id: "node-01", label: "example target one", group: "group-a", ip: "10.0.0.11", fw: "1.0.0", status: "online", rttMs: 3, ageMs: 2000, caps: "hid,cdc,serial_tx,ota", layout: "us", promptState: "login", target: "up" },
      { id: "node-02", label: "example target two", group: "group-a", ip: "10.0.0.12", fw: "1.0.0", status: "online", rttMs: 5, ageMs: 4000, caps: "hid,cdc,serial_tx,ota", layout: "de", promptState: "shell", target: "down" },
      { id: "node-03", label: "example target three (old fw)", group: "group-b", ip: "10.0.0.13", fw: "0.9.4", status: "offline", rttMs: null, ageMs: 8600e3, caps: "hid,cdc", layout: "us", promptState: null },
    ],
    consoles: {
      "node-01": [["sys", "node node-01 registered · fw 1.0.0 · caps hid,cdc"], ["out", "login: "]],
      "node-02": [["sys", "node node-02 registered · fw 1.0.0"], ["out", "$ "]],
    },
    macros: [
      { id: 1, name: "login: root", group: "shell", runs: 12, ageMs: 42000, steps: [{ type: "type", text: "root\n" }, { delay_ms: 400 }, { type: "type", text: "\n" }] },
      { id: 2, name: "safe reboot", group: "shell", runs: 4, ageMs: 9 * 3600e3, steps: [{ type: "type", text: "sync\n" }, { type: "type", text: "systemctl reboot\n" }] },
      { id: 3, name: "bios → boot menu", group: "boot", runs: 3, ageMs: 3 * 86400e3, steps: [{ type: "keys", chord: ["DELETE"] }, { delay_ms: 2000 }, { type: "keys", chord: ["RIGHT_ARROW"] }, { type: "keys", chord: ["ENTER"] }] },
    ],
    events: [
      { ageMs: 8000, type: "heartbeat", nodeId: "node-01", detail: "rtt 3ms · queue empty" },
      { ageMs: 64000, type: "node_up", nodeId: "node-02", detail: "registered · fw 1.0.0" },
    ],
    history: [{ id: "c-0001", nodeId: "node-01", text: "uptime", status: "done", ageMs: 42000 }],
    runbooks: [{ id: 1, name: "log-in", yaml: "name: log-in\nsteps:\n  - wait_for: \"login:\"\n    timeout_ms: 30000\n  - send: \"root\\n\"\n  - wait_for: \"[Pp]assword:\"\n  - send: \"toor\\n\"\n  - wait_for: \"[#$] $\"\n" }],
    bridge: { enabled: false, bind: "0.0.0.0", ports: [] },
    bundles: [{ name: "fw-1.1.0", files: ["firmware.uf2", "manifest.json"], total_sha256: "9f2c4b7a1e6d3f80c5a2b9e4d7f1a0c3b8e6d5f4a2c1b0e9d8f7a6c5b4e3d2f1", created_at: null, ageMs: 6 * 3600e3 }],
  };
  const DEMO_CHATTER = ["[  OK  ] Reached target Multi-User System.", "kernel: eth0: link up 1000Mbps full duplex", "systemd[1]: Started Session of user root.", "login: "];

  async function loadDemo() {
    state.demo = true;
    state.ws = "offline";
    let data = GENERIC_DEMO;
    try {
      const r = await fetch("demo.json", { cache: "no-store" });
      if (r.ok) data = await r.json();
    } catch (e) { /* no demo.json — use the generic placeholder set */ }
    applyDemo(data);
  }

  function applyDemo(d) {
    state.hub = Object.assign({ uptime_ms: 0, bind: "", swarm_port: 9000, web_port: 8080, version: "demo" }, d.hub || {});
    state.settings = d.settings || {};
    state.nodes = (d.nodes || []).map((n) => ({
      id: n.id, label: n.label || "", group: n.group || "", ip: n.ip || "", fw: n.fw || "",
      status: n.status || "online", rttMs: n.rttMs != null ? n.rttMs : null, caps: n.caps || "hid,cdc",
      lastSeen: now() - (n.ageMs || 0), inflight: 0,
      layout: n.layout || "us", promptState: n.promptState != null ? n.promptState : null, target: n.target || "unknown",
    }));
    state.bridge = Object.assign({ enabled: false, bind: "0.0.0.0", ports: [] }, d.bridge || {});
    state.bundles = (d.bundles || []).map((b) => ({ name: b.name, files: b.files || [], total_sha256: b.total_sha256,
      created_at: b.created_at != null ? b.created_at : (b.ageMs != null ? now() - b.ageMs : null) }));
    state.otaJobs = {};
    state.runbooks = (d.runbooks || []).map((r) => ({ id: r.id, name: r.name, yaml: r.yaml }));
    state.selRunbook = state.runbooks.length ? state.runbooks[0].id : null;
    state.runbookRuns = {};
    state.consoles = {};
    for (const k in (d.consoles || {})) {
      const arr = d.consoles[k];
      state.consoles[k] = arr.map((t, i, a) => ({ ts: now() - (a.length - i) * 9000, kind: t[0], text: t[1] }));
    }
    state.macros = (d.macros || []).map((m) => ({ id: m.id, name: m.name, group: m.group, steps: m.steps, runs: m.runs || 0, lastRun: m.ageMs != null ? now() - m.ageMs : null }));
    state.selMacro = state.macros.length ? state.macros[0].id : null;
    state.events = (d.events || []).map((e) => ({ ts: now() - (e.ageMs || 0), type: e.type, nodeId: e.nodeId, detail: e.detail }));
    state.history = (d.history || []).map((c) => ({ id: c.id, nodeId: c.nodeId, text: c.text, status: c.status, ts: now() - (c.ageMs || 0) }));
    recomputeFleet();
    const first = state.nodes.find((n) => n.status === "online") || state.nodes[0];
    state.selId = first ? first.id : null;
    // local simulation of chatter + heartbeats, generic across whatever nodes exist
    setInterval(() => {
      const n = findNode(state.selId); if (!n || n.status !== "online") return;
      pushLine(n.id, "out", DEMO_CHATTER[Math.floor(Math.random() * DEMO_CHATTER.length)]);
    }, 3000);
    setInterval(() => { state.nodes.forEach((n) => { if (n.status === "online") { n.lastSeen = now(); n.rttMs = Math.max(1, (n.rttMs || 3) + (Math.random() < 0.5 ? -1 : 1)); } }); if (state.view === "nodes") { renderNodeList(); if (ui.header) renderHeaderInto(ui.header); } }, 7000);
  }
  function demoSend(node, text, kind) {
    const cmdId = "c-" + (++seq).toString(16);
    addHistory(cmdId, node.id, text, "pending");
    node.inflight = (node.inflight || 0) + 1; if (state.view === "nodes") renderNodeList();
    setTimeout(() => {
      node.inflight = Math.max(0, node.inflight - 1); setHistoryStatus(cmdId, "done"); if (state.view === "nodes") renderNodeList();
      const t = text.trim();
      let out;
      if (/^uptime$/i.test(t)) out = " 09:41:22 up 5 days,  1 user,  load average: 0.31, 0.28, 0.22";
      else if (/^help$/i.test(t)) out = "commands: status  reboot  help";
      else if (kind !== "text") out = "[" + t + "] injected via HID";
      else out = t ? "-bash: " + t.split(" ")[0] + ": command not found" : "";
      if (out) pushLine(node.id, "out", out);
    }, 600);
  }
  function demoReboot(node) {
    node.status = "offline"; if (state.view === "nodes") { renderNodeList(); renderHeaderInto(ui.header); rebuildComposerState(); }
    state.events.unshift({ ts: now(), type: "node_down", nodeId: node.id, detail: "operator reboot — reconnecting" });
    toast("Rebooting", node.id + " · back in ~4s");
    setTimeout(() => { node.status = "online"; node.lastSeen = now(); pushLine(node.id, "sys", "node " + node.id + " re-registered · fw " + node.fw); state.events.unshift({ ts: now(), type: "node_up", nodeId: node.id, detail: "re-registered after operator reboot" }); if (state.view === "nodes") { renderNodeList(); renderHeaderInto(ui.header); rebuildComposerState(); } }, 4200);
  }

  // ---- boot --------------------------------------------------------------
  async function boot() {
    try {
      await loadLive();
      connectWS();
      if (state.selId) { /* subscribe happens on ws open */ }
    } catch (e) {
      console.warn("hub unreachable, entering demo mode:", e);
      await loadDemo();
    }
    renderShell();
    if (!state.demo && state.selId) backfillNode(state.selId);
    // Keep the xterm terminal sized to its container (phase 3; no-op without xterm).
    window.addEventListener("resize", () => { if (ui.xtermFit) { try { ui.xtermFit.fit(); } catch (e) {} } });
    setInterval(() => {
      // keep relative timestamps and fleet fresh
      if (state.view === "nodes") renderNodeList();
      refreshNav();
    }, 1000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
