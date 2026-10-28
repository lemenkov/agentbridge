// SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// The fleet page. Two rules hold throughout:
//
// 1. Every string that came from an agent — output bodies, tool names, task
//    ids — is written with textContent, never innerHTML and never into an
//    attribute. Agent output is untrusted: it is whatever a cloned repository
//    or a fetched web page told an agent to say. This is what keeps a prompt
//    injection from becoming script in the operator's browser.
// 2. The DOM is rebuilt from state, not patched from events. Events update
//    state and schedule a render, so a missed or duplicated event cannot
//    leave the page permanently wrong.

"use strict";

const LANE_WINDOW_MS = 30 * 60 * 1000;
const MAX_EVENTS = 200;
const MAX_TICKS = 60;

const state = {
  workers: [],
  queues: [],
  totals: null,
  recent: [],
  // task_id -> {worker, start, end, rc}
  tasks: new Map(),
  // worker -> [{ts, tool}]
  ticks: new Map(),
  events: [],
  user: null,
};

let source = null;
let renderQueued = false;

// ---------------------------------------------------------------- utilities

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, String(value));
  }
  return node;
}

// The page may have been opened with credentials in the URL — the
// "user:pass@host" form — in which case document.baseURI carries them.
// fetch() REFUSES such a URL outright, so a relative fetch silently never
// happens; and anything that logs or displays the URL would be echoing the
// password. Strip the userinfo before either can occur.
function sameOrigin(path) {
  const url = new URL(path, document.baseURI);
  url.username = "";
  url.password = "";
  return url;
}

function parseTs(value) {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function durationText(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

function tokenText(row) {
  const parts = [row.input_tokens, row.output_tokens].filter(
    (n) => typeof n === "number");
  return parts.length ? parts.join("/") : "";
}

function costText(row) {
  if (typeof row.cost_usd !== "number") return "";
  return `$${row.cost_usd.toFixed(4)}`;
}

// ------------------------------------------------------------------ render

function render() {
  renderQueued = false;
  renderSummary();
  renderTopology();
  renderLanes();
  renderRecent();
}

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(render);
}

function renderSummary() {
  const target = document.getElementById("summary");
  target.textContent = "";
  if (!state.totals) {
    target.textContent = "no data";
    return;
  }
  const byState = state.totals.by_state || {};
  const pieces = Object.keys(byState).sort().map((k) => `${k} ${byState[k]}`);
  const usage = state.totals.usage_7d || {};
  const cost = typeof usage.cost_usd === "number"
    ? `$${usage.cost_usd.toFixed(4)}${usage.cost_estimated ? " est" : ""}`
    : "—";
  const summary =
    `${state.user || "?"} · ${state.totals.workers}/${state.totals.max_workers} workers` +
    (pieces.length ? ` (${pieces.join(", ")})` : "") +
    ` · 7d: ${usage.tasks || 0} tasks, ${cost}`;
  target.textContent = summary;
}

function renderTopology() {
  const root = document.getElementById("topology");
  root.textContent = "";

  const left = el("div");
  const orchestrator = el("div", "node");
  orchestrator.appendChild(el("div", "name", "orchestrator"));
  orchestrator.appendChild(el("div", "meta", state.user || ""));
  left.appendChild(orchestrator);
  root.appendChild(left);

  const middle = el("div");
  if (!state.queues.length) {
    middle.appendChild(el("div", "empty", "queue depths unavailable"));
  }
  for (const queue of state.queues) {
    const node = el("div", "node");
    node.appendChild(el("div", "name", queue.name));
    const meta = el("div", "meta");
    const depth = el("span", "depth" + (queue.ready > 0 ? " hot" : ""),
                     `ready ${queue.ready}`);
    meta.appendChild(depth);
    meta.appendChild(document.createTextNode(
      ` · unacked ${queue.unacked} · consumers ${queue.consumers}`));
    node.appendChild(meta);
    middle.appendChild(node);
  }
  root.appendChild(middle);

  const right = el("div");
  if (!state.workers.length) {
    right.appendChild(el("div", "empty", "no workers"));
  }
  for (const worker of state.workers) {
    const node = el("div", "node");
    node.dataset.state = worker.state || "unknown";
    node.dataset.agent = worker.agent || "";
    node.appendChild(el("div", "name", worker.agent || "?"));

    const bits = [worker.state || "?"];
    if (worker.role) bits.push(worker.role);
    if (worker.model) bits.push(worker.model);
    if (worker.state === "working") {
      const started = parseTs(worker.task_started);
      if (started !== null) bits.push(`for ${durationText((Date.now() - started) / 1000)}`);
      if (worker.last_tool) bits.push(`tool ${worker.last_tool}`);
    } else if (typeof worker.last_rc === "number") {
      bits.push(`last rc ${worker.last_rc}`);
    }
    if (typeof worker.tasks_done === "number") bits.push(`${worker.tasks_done} done`);
    // One textContent write, so a hostile tool name cannot become markup.
    node.appendChild(el("div", "meta", bits.join(" · ")));
    right.appendChild(node);
  }
  root.appendChild(right);
}

function laneRows() {
  const names = new Set(state.workers.map((w) => w.agent));
  for (const task of state.tasks.values()) {
    if (task.worker) names.add(task.worker);
  }
  return [...names].filter(Boolean).sort();
}

function renderLanes() {
  const root = document.getElementById("lanes");
  root.textContent = "";
  const rows = laneRows();
  if (!rows.length) {
    root.appendChild(el("div", "empty", "nothing in the last 30 minutes"));
    return;
  }
  const now = Date.now();
  const from = now - LANE_WINDOW_MS;

  for (const name of rows) {
    const lane = el("div", "lane");
    lane.appendChild(el("div", "who", name));

    const svg = svgEl("svg", {viewBox: "0 0 1000 22", preserveAspectRatio: "none"});
    const x = (ms) => Math.max(0, Math.min(1000, ((ms - from) / LANE_WINDOW_MS) * 1000));

    for (const [taskId, task] of state.tasks) {
      if (task.worker !== name) continue;
      const start = task.start ?? from;
      const end = task.end ?? now;
      if (end < from) continue;
      const left = x(start);
      const width = Math.max(2, x(end) - left);
      const klass = task.end === null || task.end === undefined
        ? "bar-running"
        : (task.rc === 0 ? "bar-ok" : "bar-bad");
      const bar = svgEl("rect",
        {x: left, y: 4, width, height: 14, rx: 2, class: klass});
      // A tooltip, as text: never an attribute that could be broken out of.
      const title = svgEl("title", {});
      title.textContent =
        `${taskId} · ${durationText(((task.end ?? now) - start) / 1000)}` +
        (task.rc === null || task.rc === undefined ? " · running" : ` · rc ${task.rc}`);
      bar.appendChild(title);
      svg.appendChild(bar);
    }

    for (const tick of state.ticks.get(name) || []) {
      if (tick.ts < from) continue;
      const mark = svgEl("rect", {x: x(tick.ts), y: 0, width: 1, height: 4,
                                 class: "tick"});
      const title = svgEl("title", {});
      title.textContent = tick.tool || "progress";
      mark.appendChild(title);
      svg.appendChild(mark);
    }

    svg.appendChild(svgEl("line", {x1: 1000, y1: 0, x2: 1000, y2: 22,
                                   class: "now-line"}));
    lane.appendChild(svg);
    root.appendChild(lane);
  }
}

function renderRecent() {
  const body = document.querySelector("#recent tbody");
  body.textContent = "";
  if (!state.recent.length) {
    const row = el("tr");
    const cell = el("td", "empty", "no finished tasks yet");
    cell.colSpan = 9;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const row of state.recent) {
    const tr = el("tr");
    tr.dataset.rc = String(row.rc ?? "");
    const cells = [
      [row.task_id || "", ""],
      [row.worker || "", ""],
      [row.role || "", ""],
      [row.model || "", ""],
      [durationText((row.duration_ms || 0) / 1000), "num"],
      [row.num_turns ?? "", "num"],
      [tokenText(row), "num"],
      [costText(row), "num"],
      [row.rc ?? "", ""],
    ];
    for (const [text, klass] of cells) tr.appendChild(el("td", klass, text));
    body.appendChild(tr);
  }
}

// ------------------------------------------------------------------ events

function noteEvent(kind, event) {
  const when = event && event.ts ? event.ts : new Date().toISOString();
  const bits = [];
  if (event) {
    for (const key of ["agent_id", "task_id", "tool", "phase", "to", "rc"]) {
      if (event[key] !== undefined && event[key] !== null) {
        bits.push(`${key}=${event[key]}`);
      }
    }
    if (event.body) bits.push(event.body);
    if (event.error) bits.push(event.error);
  }
  state.events.unshift({when, kind, text: bits.join(" ")});
  state.events.length = Math.min(state.events.length, MAX_EVENTS);

  const list = document.getElementById("events");
  const item = el("li");
  item.appendChild(el("span", "when", when.replace("T", " ").replace("+00:00", "Z")));
  item.appendChild(document.createTextNode(" "));
  item.appendChild(el("span", "what", kind));
  if (bits.length) {
    item.appendChild(document.createTextNode(" "));
    // textContent: a body containing <img src=x onerror=…> stays text.
    item.appendChild(el("span", null, bits.join(" ")));
  }
  list.prepend(item);
  while (list.childElementCount > MAX_EVENTS) list.lastElementChild.remove();
}

function flashWorker(agentId) {
  if (!agentId) return;
  const node = document.querySelector(`.node[data-agent="${CSS.escape(agentId)}"]`);
  if (!node) return;
  node.classList.remove("flash");
  // Force a reflow so the animation restarts on a repeat event.
  void node.offsetWidth;
  node.classList.add("flash");
}

function applySnapshot(data) {
  state.user = data.user || null;
  state.workers = data.workers || [];
  state.queues = data.queues || [];
  state.totals = data.totals || null;
  state.recent = data.recent || state.recent;
  for (const row of state.recent) {
    if (!row.task_id || state.tasks.has(row.task_id)) continue;
    const end = parseTs(row.ts_end);
    const start = end !== null && typeof row.duration_ms === "number"
      ? end - row.duration_ms
      : parseTs(row.ts_start);
    state.tasks.set(row.task_id, {
      worker: row.worker, start, end, rc: row.rc ?? null,
    });
  }
  scheduleRender();
}

function onEvent(kind, event) {
  const worker = state.workers.find((w) => w.agent === event.agent_id);
  switch (kind) {
    case "dispatch":
      if (event.task_id) {
        state.tasks.set(event.task_id,
          {worker: null, start: Date.now(), end: null, rc: null});
      }
      break;
    case "start": {
      const task = state.tasks.get(event.task_id) || {};
      state.tasks.set(event.task_id, {
        worker: event.agent_id || task.worker || null,
        start: parseTs(event.ts) ?? Date.now(),
        end: null, rc: null,
      });
      if (worker) {
        worker.state = "working";
        worker.task_id = event.task_id;
        worker.task_started = event.ts;
        worker.last_tool = null;
      }
      flashWorker(event.agent_id);
      break;
    }
    case "progress": {
      const list = state.ticks.get(event.agent_id) || [];
      list.push({ts: parseTs(event.ts) ?? Date.now(), tool: event.tool});
      if (list.length > MAX_TICKS) list.splice(0, list.length - MAX_TICKS);
      state.ticks.set(event.agent_id, list);
      if (worker && event.tool) worker.last_tool = event.tool;
      break;
    }
    case "done": {
      const task = state.tasks.get(event.task_id) || {start: Date.now()};
      task.worker = task.worker || event.agent_id || null;
      task.end = Date.now();
      task.rc = typeof event.rc === "number" ? event.rc : null;
      state.tasks.set(event.task_id, task);
      if (worker) {
        worker.state = "idle";
        worker.task_id = null;
        worker.task_started = null;
        worker.last_rc = task.rc;
        worker.tasks_done = (worker.tasks_done || 0) + 1;
      }
      // The ledger is authoritative for the table; ask for a fresh snapshot.
      refreshSnapshot();
      break;
    }
    case "waiting":
    case "waiting_for_input":
      if (worker) worker.state = "idle";
      break;
    default:
      break;
  }
  noteEvent(kind, event);
  scheduleRender();
}

// ------------------------------------------------------------- connection

function setConnection(label, stateName) {
  const node = document.getElementById("conn");
  node.textContent = label;
  node.dataset.state = stateName;
}

async function refreshSnapshot() {
  // Built from the document's own base, so it cannot depend on how the page
  // was reached: trailing slash or not, via a redirect, or behind a proxy
  // that rewrote the path.
  const url = sameOrigin("snapshot.json");
  try {
    const response = await fetch(url.href, {headers: {Accept: "application/json"}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    applySnapshot(await response.json());
    // Reported on the page itself: a silent failure here leaves the recent
    // tasks table stale while everything else looks healthy, and diagnosing
    // that should not require opening DevTools. The PATH only — a URL could
    // carry credentials, and this log is read, copied and pasted.
    noteEvent("snapshot", {phase: "ok"});
  } catch (err) {
    setConnection("snapshot failed", "lost");
    noteEvent("snapshot", {to: url.pathname,
                           error: String((err && err.message) || err)});
  }
}

function connect() {
  if (source) source.close();
  const bodies = document.getElementById("bodies").checked;
  source = new EventSource(sameOrigin(bodies ? "events?bodies=1" : "events").href);

  source.addEventListener("open", () => setConnection("live", "live"));
  source.addEventListener("snapshot", (e) => {
    setConnection("live", "live");
    applySnapshot(JSON.parse(e.data));
  });
  for (const kind of ["dispatch", "start", "progress", "output", "waiting",
                      "waiting_for_input", "done", "error"]) {
    source.addEventListener(kind, (e) => {
      let payload = {};
      try {
        payload = JSON.parse(e.data);
      } catch (err) {
        payload = {};
      }
      onEvent(kind, payload);
    });
  }
  // EventSource reconnects by itself; a fresh snapshot on reconnect is
  // cheaper and more correct than replaying a backlog of events.
  source.addEventListener("error", () => setConnection("reconnecting…", "lost"));
}

document.getElementById("bodies").addEventListener("change", connect);
// Worker "for 12s" and the now-line move without any event arriving.
setInterval(scheduleRender, 1000);
refreshSnapshot().then(connect);
