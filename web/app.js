"use strict";

// The token travels in the page URL, so every call reuses it from there.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

let state = null;
let chosenProfile = null;
let planChecks = {};      // index -> bool, kept across polls
let planStamp = "";       // identity of the plan the checks belong to

async function get(path) {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN));
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function post(path, body) {
  const r = await fetch(path + "?t=" + encodeURIComponent(TOKEN), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (data.error) showError(data.error);
  return data;
}

function showError(msg) {
  const e = $("err");
  e.textContent = msg;
  e.classList.remove("hidden");
  setTimeout(() => e.classList.add("hidden"), 9000);
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function row(what, note, risk, opts = {}) {
  const r = el("div", "row" + (opts.kept ? " kept" : "") + (opts.onclick ? " clickable" : ""));
  if (opts.check !== undefined) {
    const c = el("input");
    c.type = "checkbox";
    c.checked = opts.check;
    c.addEventListener("change", () => opts.onCheck(c.checked));
    r.appendChild(c);
  }
  const body = el("div", "body");
  body.appendChild(el("div", "what", what));
  if (note) body.appendChild(el("div", "note", note));
  r.appendChild(body);
  if (risk) r.appendChild(el("span", "pill " + risk, risk));
  if (opts.button) {
    const b = el("button", "small ghost", opts.button);
    b.addEventListener("click", opts.onclick);
    r.appendChild(b);
  } else if (opts.onclick) {
    r.addEventListener("click", opts.onclick);
  }
  return r;
}

// --- rendering ------------------------------------------------------------

function renderDevices(list) {
  const box = $("devices");
  box.textContent = "";
  if (!list.length) {
    box.appendChild(row("No phone connected yet",
      "Plug one in over USB with USB debugging on, or look for one over Wi-Fi.",
      "", { kept: true }));
    return;
  }
  for (const d of list) {
    if (d.usable) {
      box.appendChild(row(d.label, d.serial, "",
        { button: state && state.serial === d.serial ? "selected" : "use this one",
          onclick: () => post("/api/pick", { serial: d.serial }) }));
    } else if (d.state === "unauthorized") {
      box.appendChild(row(d.label + " — waiting for you to allow it",
        "Unlock the phone and tap Allow on the “Allow USB debugging?” prompt, then press Look again.",
        "caution", { kept: true }));
    } else {
      box.appendChild(row(d.serial + " — " + d.state, "", "caution", { kept: true }));
    }
  }
}

function renderModes() {
  const box = $("modes");
  box.textContent = "";
  for (const [id, m] of Object.entries(state.modes)) {
    const c = el("button", "card" + (state.mode === id ? " on" : ""));
    c.appendChild(el("strong", null, m.label));
    c.appendChild(el("span", null, m.blurb));
    c.addEventListener("click", async () => { await post("/api/mode", { mode: id }); refresh(); });
    box.appendChild(c);
  }
}

function renderProfiles() {
  const box = $("profiles");
  box.textContent = "";
  for (const p of state.profiles) {
    const c = el("div", "card" + (chosenProfile === p.id ? " on" : ""));
    c.appendChild(el("strong", null, p.name));
    c.appendChild(el("span", null, p.summary));
    const tiers = el("div", "tiers");
    for (const t of p.intensities) {
      const b = el("button", "tier", t.label);
      b.title = t.blurb;
      b.addEventListener("click", () => {
        chosenProfile = p.id;
        post("/api/plan", { profile: p.id, intensity: t.id });
      });
      tiers.appendChild(b);
    }
    c.appendChild(tiers);
    box.appendChild(c);
  }
}

function renderPlan() {
  const p = state.plan;
  const sec = $("s-plan");
  if (!p) { sec.classList.add("hidden"); return; }
  sec.classList.remove("hidden");

  const stamp = p.label + ":" + p.actions.length + ":" + (p.actions[0] || {}).what;
  if (stamp !== planStamp) {
    planStamp = stamp;
    planChecks = {};
    for (const a of p.actions) planChecks[a.i] = a.preselect;
  }

  $("plansummary").textContent =
    `${p.title} — ${p.actions.length} proposed: ${p.by_risk.safe} safe, ` +
    `${p.by_risk.caution} caution, ${p.by_risk.risky} risky.`;

  const box = $("plan");
  box.textContent = "";
  for (const a of p.actions) {
    box.appendChild(row(a.what, a.note || a.source, a.risk, {
      check: !!planChecks[a.i],
      onCheck: (v) => { planChecks[a.i] = v; updateCount(); },
    }));
  }

  const skipWrap = $("skipped-wrap");
  if (p.skipped.length) {
    skipWrap.classList.remove("hidden");
    $("skipcount").textContent = p.skipped.length;
    const sb = $("skipped");
    sb.textContent = "";
    for (const s of p.skipped) sb.appendChild(row(s.target, s.why, "", { kept: true }));
  } else {
    skipWrap.classList.add("hidden");
  }
  updateCount();
}

function updateCount() {
  const n = Object.values(planChecks).filter(Boolean).length;
  $("selcount").textContent = n + " ticked";
  $("apply").disabled = n === 0 || !!state.busy;
}

function setAll(fn) {
  if (!state.plan) return;
  for (const a of state.plan.actions) planChecks[a.i] = fn(a);
  renderPlan();
}

function renderJournal() {
  const box = $("journal");
  box.textContent = "";
  if (!state.journal.length) {
    box.appendChild(row("Nothing has been changed on this phone yet", "", "", { kept: true }));
    return;
  }
  for (const r of state.journal) {
    box.appendChild(row(r.summary,
      r.count ? r.count + " changes can be put back" : "nothing reversible in this run",
      "", r.count ? { button: "put it back", onclick: () => post("/api/undo", { run: r.id }) }
                  : { kept: true }));
  }
}

async function renderCatalog() {
  const data = await get("/api/catalog");
  const box = $("catalog");
  box.textContent = "";
  const titles = {
    launcher: "Home screens", essential: "Replacements for what you turn off",
    store: "App stores", server: "Running things on the phone", useful: "Worth having",
  };
  const picked = new Set();
  for (const cat of ["launcher", "essential", "store", "server", "useful"]) {
    const group = data.apps.filter((a) => a.category === cat);
    if (!group.length) continue;
    box.appendChild(row("── " + (titles[cat] || cat), "", "", { kept: true }));
    for (const a of group) {
      let note = a.blurb || "";
      if (a.note) note += "  " + a.note;
      note += "  From " + a.origin + ".";
      if (a.ram_kb) note += `  Measured ${Math.round(a.ram_kb / 1024)} MB in memory.`;
      box.appendChild(row(a.name + (a.installed ? "   (already installed)" : ""), note, "", {
        check: false,
        onCheck: (v) => { v ? picked.add(a.id) : picked.delete(a.id); },
      }));
    }
  }
  $("install").onclick = () => {
    if (!picked.size) return showError("Tick something first.");
    post("/api/install", { apps: [...picked] });
    show("s-apps", false);
  };
}

function renderSteps() {
  const sec = $("s-progress");
  if (!state.steps.length) { sec.classList.add("hidden"); return; }
  sec.classList.remove("hidden");
  const t = $("progress-title");
  t.textContent = state.busy || (state.finished ? "Done" : "Progress");
  t.className = state.busy ? "spinner" : "";
  const box = $("steps");
  box.textContent = "";
  for (const s of state.steps) {
    box.appendChild(el("div", s.ok ? (s.done ? "done" : "") : "bad", s.text));
  }
  box.scrollTop = box.scrollHeight;
}

function show(id, on) { $(id).classList.toggle("hidden", !on); }

// --- polling --------------------------------------------------------------

async function refresh() {
  try {
    state = await get("/api/state");
  } catch (e) {
    $("deviceline").textContent = "Lost contact with the server. Is it still running?";
    return;
  }
  if (state.version) $("version").textContent = "Free My Android " + state.version + ". ";
  const d = state.device;
  $("deviceline").textContent = d
    ? `${d.name} · ${d.android} · ${d.installed} packages, ${d.disabled} already off` +
      (d.ram_mb ? ` · ${(d.ram_mb / 1024).toFixed(1)} GB RAM` : "") +
      (d.launcher ? ` · home screen ${d.launcher}` : "")
    : "No phone selected yet.";

  if (d && d.warnings && d.warnings.length) showError(d.warnings.join(" "));

  renderModes();
  show("s-mode", !!d);
  show("s-what", !!d);
  if (d) renderProfiles();
  renderPlan();
  renderJournal();
  renderSteps();
  updateCount();

  for (const b of ["scan", "battery", "apps", "undo", "install"]) {
    const n = $(b);
    if (n) n.disabled = !!state.busy;
  }
}

async function refreshDevices() {
  try { renderDevices((await get("/api/devices")).devices); }
  catch (e) { showError(String(e.message || e)); }
}

// --- wiring ---------------------------------------------------------------

$("refresh").onclick = refreshDevices;
$("scan").onclick = () => post("/api/scan");
$("battery").onclick = () => post("/api/battery");
$("undo").onclick = () => show("s-undo", true);
$("undo-close").onclick = () => show("s-undo", false);
$("apps").onclick = async () => { await renderCatalog(); show("s-apps", true); };
$("apps-close").onclick = () => show("s-apps", false);
$("sel-all").onclick = () => setAll(() => true);
$("sel-none").onclick = () => setAll(() => false);
$("sel-safe").onclick = () => setAll((a) => a.risk === "safe");

$("apply").onclick = () => {
  const indexes = Object.entries(planChecks).filter(([, v]) => v).map(([k]) => +k);
  const risky = state.plan.actions.filter((a) => indexes.includes(a.i) && a.risk === "risky");
  let msg = `Apply ${indexes.length} changes to ${state.device.name}?\n\n` +
            "Nothing is uninstalled. Every change is saved on this computer and " +
            "can be put back from Undo.";
  if (risky.length) {
    msg += `\n\n${risky.length} of them are marked risky:\n` +
           risky.slice(0, 8).map((a) => "  • " + a.what).join("\n") +
           (risky.length > 8 ? "\n  …" : "");
  }
  if (!confirm(msg)) return;
  post("/api/apply", { indexes });
};

refreshDevices();
refresh();
setInterval(refresh, 1200);
setInterval(refreshDevices, 5000);
