#!/usr/bin/env python3
"""
Daybook — a single-file, standard-library-only local task app.

Run:  python3 app.py          (opens your browser at http://localhost:8765)
      python3 app.py --no-browser

Data lives in tasks.json next to this file. Back it up by copying that file.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "tasks.json")
ATTACH_DIR = os.path.join(HERE, "attachments")
LOCK = threading.Lock()


# ---------------------------------------------------------------- storage ----

# Fields every task must have, with the default used when an older tasks.json
# predates the field. COMPATIBILITY RULE: new features may only ADD entries
# here — never rename, remove, or change the meaning of an existing field.
TASK_DEFAULTS = {
    "text": "", "project": "(no project)", "bucket": "week",
    "note": "", "waiting_on": "", "due": "",
    "created": "", "completed_at": None, "attachments": [], "attachment_labels": {},
}


def upgrade(data):
    """Bring a tasks.json written by any older version up to the current shape.

    Runs on every load, is idempotent, and never discards data — this is what
    lets a new app.py be dropped onto a machine with an old data file.
    """
    data.setdefault("projects", [])
    data.setdefault("tasks", [])
    for task in data["tasks"]:
        for field, default in TASK_DEFAULTS.items():
            if field not in task:
                task[field] = (list(default) if isinstance(default, list)
                               else dict(default) if isinstance(default, dict) else default)
        # "waiting" used to be a bucket; it is now a flag (waiting_on) on top of
        # the time buckets. Files from that era migrate to This Week, flag kept.
        if task.get("bucket") == "waiting":
            task["bucket"] = "week"
    data.setdefault("next_id", max([t["id"] for t in data["tasks"]], default=0) + 1)
    return data


def load_data():
    with LOCK:
        if not os.path.exists(DATA_FILE):
            data = upgrade({})  # first run starts empty
            _write(data)
            return data
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return upgrade(json.load(f))


def _write(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def save_data(data):
    with LOCK:
        _write(data)


# ------------------------------------------------------------------ server ----

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), ctype="text/html")
        elif self.path == "/api/state":
            self._send(200, load_data())
        else:
            self._send(404, {"error": "not found"})

    def _attach(self):
        """Store a raw uploaded file under attachments/<task id>/."""
        try:
            tid = int(parse_qs(urlparse(self.path).query).get("id", [""])[0])
        except ValueError:
            self._send(400, {"error": "bad id"})
            return
        name = os.path.basename(unquote(self.headers.get("X-Filename", ""))).strip()
        name = name.replace("/", "_").replace("\\", "_")[:150] or "attachment"
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send(400, {"error": "empty file"})
            return
        blob = self.rfile.read(length)
        data = load_data()
        task = next((t for t in data["tasks"] if t["id"] == tid), None)
        if task is None:
            self._send(404, {"error": "no such task"})
            return
        folder = os.path.join(ATTACH_DIR, str(tid))
        os.makedirs(folder, exist_ok=True)
        base, ext = os.path.splitext(name)
        candidate, n = name, 2
        while os.path.exists(os.path.join(folder, candidate)):
            candidate = "%s-%d%s" % (base, n, ext)
            n += 1
        with open(os.path.join(folder, candidate), "wb") as f:
            f.write(blob)
        task["attachments"].append(candidate)
        save_data(data)
        self._send(200, {"ok": True, "name": candidate})

    def _attachment_path(self, task, name):
        """Resolve an attachment to a real path inside ATTACH_DIR, or None."""
        if name not in task["attachments"]:
            return None
        path = os.path.realpath(os.path.join(ATTACH_DIR, str(task["id"]), name))
        if not path.startswith(os.path.realpath(ATTACH_DIR) + os.sep):
            return None
        return path

    def do_POST(self):
        if self.path.startswith("/api/attach"):
            self._attach()
            return
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "bad json"})
            return
        data = load_data()

        if self.path == "/api/add":
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "empty task"})
                return
            task = {
                "id": data["next_id"],
                "text": text,
                "project": body.get("project") or "(no project)",
                "bucket": body.get("bucket") or "week",
                "note": body.get("note") or "",
                "waiting_on": body.get("waiting_on") or "",
                "due": body.get("due") or "",
                "created": datetime.now().strftime("%Y-%m-%d"),
                "completed_at": None,
            }
            data["next_id"] += 1
            data["tasks"].append(task)
            save_data(data)
            self._send(200, {"ok": True, "id": task["id"]})

        elif self.path == "/api/update":
            tid = body.get("id")
            task = next((t for t in data["tasks"] if t["id"] == tid), None)
            if task is None:
                self._send(404, {"error": "no such task"})
                return
            for field in ("text", "project", "bucket", "note", "waiting_on", "due",
                          "completed_at", "attachment_labels"):
                if field in body:
                    task[field] = body[field]
            save_data(data)
            self._send(200, {"ok": True})

        elif self.path == "/api/delete":
            tid = body.get("id")
            before = len(data["tasks"])
            data["tasks"] = [t for t in data["tasks"] if t["id"] != tid]
            if len(data["tasks"]) == before:
                self._send(404, {"error": "no such task"})
                return
            save_data(data)
            self._send(200, {"ok": True})

        elif self.path == "/api/open":
            task = next((t for t in data["tasks"] if t["id"] == body.get("id")), None)
            path = self._attachment_path(task, body.get("name") or "") if task else None
            if path is None or not os.path.exists(path):
                self._send(404, {"error": "no such attachment"})
                return
            if hasattr(os, "startfile"):
                os.startfile(path)  # Windows: .msg opens in Outlook
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, path])
            self._send(200, {"ok": True})

        elif self.path == "/api/detach":
            task = next((t for t in data["tasks"] if t["id"] == body.get("id")), None)
            name = body.get("name") or ""
            path = self._attachment_path(task, name) if task else None
            if path is None:
                self._send(404, {"error": "no such attachment"})
                return
            task["attachments"].remove(name)
            task["attachment_labels"].pop(name, None)
            try:
                os.remove(path)
            except OSError:
                pass
            save_data(data)
            self._send(200, {"ok": True})

        elif self.path == "/api/project":
            name = (body.get("name") or "").strip()
            if name and name not in data["projects"]:
                data["projects"].append(name)
                save_data(data)
            self._send(200, {"ok": True})

        elif self.path == "/api/project-delete":
            name = (body.get("name") or "").strip()
            if name not in data["projects"]:
                self._send(404, {"error": "no such project"})
                return
            if any(t["project"] == name for t in data["tasks"]):
                self._send(400, {"error": "project still has tasks"})
                return
            data["projects"].remove(name)
            save_data(data)
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})


# ------------------------------------------------------------------- page ----

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daybook</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f5f5f6; --panel: #ffffff; --text: #1f2328; --muted: #6e747d;
    --line: #e3e5e8; --accent: #37587a; --accent-soft: #eaeff4;
    --done: #9aa0a8; --danger: #b0413a; --shadow: 0 1px 2px rgba(20,24,30,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181a; --panel: #1f2124; --text: #e6e8eb; --muted: #8e949c;
      --line: #2e3134; --accent: #85a7c6; --accent-soft: #273544;
      --done: #6a7077; --danger: #d97f70; --shadow: 0 1px 3px rgba(0,0,0,.35);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
  }
  .wrap { max-width: 780px; margin: 0 auto; padding: 24px 16px 80px; }
  header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 18px; }
  header h1 { font-size: 20px; margin: 0; font-weight: 650; }
  header .date { color: var(--muted); font-size: 14px; }

  nav { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 16px; }
  nav button {
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    padding: 7px 14px; border-radius: 999px; cursor: pointer; font-size: 14px;
  }
  nav button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  nav button .count { opacity: .65; font-size: 12px; margin-left: 4px; }

  .hidden { display: none !important; }
  .addtoggle {
    display: block; border: 1px dashed var(--line); background: transparent; color: var(--muted);
    padding: 8px 16px; border-radius: 10px; cursor: pointer; font-size: 14px; margin-bottom: 20px;
  }
  .addtoggle:hover { color: var(--accent); border-color: var(--accent); }
  .addbar {
    display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px; box-shadow: var(--shadow);
  }
  .addbar input[type=text] {
    width: 100%; border: none; background: transparent; color: var(--text);
    font-size: 15px; outline: none; padding: 4px 2px;
  }
  .addcontrols { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .addcontrols select, .addcontrols input, .addcontrols button {
    border: 1px solid var(--line); background: var(--bg); color: var(--text);
    border-radius: 8px; padding: 6px 10px; font-size: 13px; cursor: pointer;
  }
  .addcontrols input[type=text] { cursor: text; flex: 1 1 150px; }
  .addcontrols button.add { background: var(--accent); color: #fff; border-color: var(--accent); }
  .addcontrols button.closeadd { border: none; background: transparent; color: var(--muted); }

  .section-title { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); margin: 22px 0 8px; }
  .group-title { font-size: 14px; font-weight: 600; color: var(--text); margin: 20px 0 8px; }
  .empty { color: var(--muted); font-style: italic; padding: 14px 4px; }

  .task {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 12px; margin-bottom: 8px; box-shadow: var(--shadow);
  }
  .task .row { display: flex; align-items: flex-start; gap: 10px; }
  .task input[type=checkbox] { margin-top: 4px; width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
  .task .text { flex: 1; }
  .task.done .text .label { text-decoration: line-through; color: var(--done); }
  .task .label { font-weight: 500; }
  .task .tags { display: flex; gap: 12px; flex-wrap: wrap; align-items: baseline; margin-top: 3px; }
  .proj {
    font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
    color: var(--muted); white-space: nowrap;
  }
  .wait { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .due { font-size: 12px; font-weight: 500; color: var(--accent); white-space: nowrap; }
  .due.today { font-weight: 700; }
  .due.overdue { color: var(--danger); font-weight: 700; }
  .task.done .due { color: var(--done); font-weight: 400; }
  .task .meta { font-size: 12.5px; color: var(--muted); font-style: italic; margin-top: 2px; white-space: pre-wrap; }
  .atts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; }
  .att {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
    color: var(--accent); border: 1px solid var(--line); border-radius: 6px;
    padding: 2px 8px; cursor: pointer; max-width: 100%;
  }
  .att:hover { border-color: var(--accent); background: var(--accent-soft); }
  .att .attx {
    border: none; background: none; color: var(--muted); cursor: pointer;
    font-size: 12px; padding: 0 0 0 2px; opacity: 0; transition: opacity .12s;
  }
  .att:hover .attx { opacity: 1; }
  .att .attx:hover { color: var(--danger); }
  .task.drop { border-color: var(--accent); background: var(--accent-soft); }
  .task .controls {
    display: flex; gap: 4px; align-items: center; flex-shrink: 0;
    opacity: 0; transition: opacity .12s;
  }
  .task:hover .controls, .task:focus-within .controls,
  .task .controls:has(button.arm) { opacity: 1; }
  .task .controls select.mv {
    border: 1px solid var(--line); background: transparent; color: var(--muted);
    font-size: 11.5px; padding: 2px 4px; border-radius: 6px; cursor: pointer;
  }
  .task .controls button {
    border: 1px solid var(--line); background: transparent; color: var(--muted);
    font-size: 11.5px; padding: 2px 7px; border-radius: 6px; cursor: pointer;
  }
  .task .controls button:hover { color: var(--text); border-color: var(--muted); }
  .task .controls button.cur { background: var(--accent-soft); color: var(--accent); border-color: transparent; }
  .task .controls button.del:hover { color: var(--danger); border-color: var(--danger); }
  .task .controls button.del.arm, .task .controls button.del.arm:hover {
    background: var(--danger); border-color: var(--danger); color: #fff; font-weight: 600;
  }

  .editor { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 10px; display: grid; gap: 8px; }
  .editor textarea, .editor input {
    width: 100%; border: 1px solid var(--line); border-radius: 8px; background: var(--bg);
    color: var(--text); padding: 7px 9px; font: 13.5px/1.45 inherit; outline: none;
  }
  .editor textarea { resize: vertical; min-height: 54px; }
  .editor label { font-size: 12px; color: var(--muted); }
  .editor .hint { font-size: 12px; color: var(--muted); }
  .editor .attlist { display: grid; gap: 6px; }
  .editor .savebtn { justify-self: start; border: none; background: var(--accent); color: #fff;
    border-radius: 8px; padding: 6px 14px; cursor: pointer; font-size: 13px; }

  .pull { border: 1px dashed var(--line); background: transparent; color: var(--accent);
    font-size: 12px; padding: 2px 9px; border-radius: 999px; cursor: pointer; }

  .filterbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .filterbar select, .filterbar button {
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    border-radius: 8px; padding: 5px 10px; font-size: 13px; cursor: pointer;
  }
  .filterbar button.active { background: var(--accent); color: #fff; border-color: var(--accent); }

  .proj-card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 14px; margin-bottom: 12px; box-shadow: var(--shadow); }
  .proj-card h3 { margin: 0 0 6px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
  .projrm {
    margin-left: auto; border: 1px solid var(--line); background: transparent; color: var(--muted);
    font-size: 11.5px; padding: 2px 8px; border-radius: 6px; cursor: pointer;
  }
  .projrm:hover { color: var(--danger); border-color: var(--danger); }
  .projrm.arm { background: var(--danger); border-color: var(--danger); color: #fff; font-weight: 600; }
  .proj-card ul { margin: 6px 0 0; padding-left: 20px; }
  .proj-card li { margin: 3px 0; }
  .proj-card .b { font-size: 11px; color: var(--muted); margin-left: 6px; text-transform: uppercase; letter-spacing: .04em; }
  .allclear { color: var(--muted); font-style: italic; }
  .done-date { font-size: 12px; color: var(--muted); margin: 14px 0 6px; font-weight: 600; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Daybook</h1>
    <div class="date" id="today-date"></div>
  </header>

  <nav id="tabs"></nav>

  <button class="addtoggle" id="add-toggle" onclick="toggleAdd()">+ Add task</button>
  <form class="addbar hidden" id="add-form" onsubmit="addTask(); return false;">
    <input type="text" id="add-text" placeholder="What needs doing? (Enter to save)">
    <div class="addcontrols">
      <select id="add-project"></select>
      <select id="add-bucket">
        <option value="today">Today</option>
        <option value="week" selected>This Week</option>
        <option value="later">Later</option>
      </select>
      <input type="date" id="add-due" title="Deadline (optional)">
      <input type="text" id="add-wait" class="hidden" placeholder="Waiting on whom?">
      <button class="add" type="submit">Add</button>
      <button type="button" class="closeadd" onclick="toggleAdd()" title="Close (Esc)">Close</button>
    </div>
  </form>

  <main id="view"></main>

</div>

<script>
"use strict";
let state = { projects: [], tasks: [] };
let tab = "today";
const expanded = new Set();
let doneProject = "all", doneDays = 7;

const BUCKETS = [["today","Today"],["week","Week"],["later","Later"]];

const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pending = t => !t.completed_at;
const todayISO = () => new Date().toISOString().slice(0, 10);

function dueTag(t) {
  if (!t.due) return "";
  const label = new Date(t.due + "T00:00:00")
    .toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  if (t.completed_at) return `<span class="due">due ${label}</span>`;
  if (t.due < todayISO()) return `<span class="due overdue">overdue since ${label}</span>`;
  if (t.due === todayISO()) return `<span class="due today">due today</span>`;
  return `<span class="due">due ${label}</span>`;
}
const inBucket = b => state.tasks.filter(t => t.bucket === b && pending(t));

async function api(path, body) {
  const res = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)} : {});
  return res.json();
}
async function refresh() { state = await api("/api/state"); render(); }

// ---------- rendering ----------
function render() {
  renderTabs(); renderAddBar();
  const v = document.getElementById("view");
  if (tab === "today")        v.innerHTML = viewToday();
  else if (tab === "week")    v.innerHTML = viewGrouped("week", "Nothing queued for this week.");
  else if (tab === "waiting") v.innerHTML = viewWaiting();
  else if (tab === "later")   v.innerHTML = viewGrouped("later", "Nothing parked for later.");
  else if (tab === "projects") v.innerHTML = viewProjects();
  else if (tab === "done")    v.innerHTML = viewDone();
}

function renderTabs() {
  const counts = { today: inBucket("today").length, week: inBucket("week").length,
                   waiting: state.tasks.filter(t => pending(t) && t.waiting_on).length,
                   later: inBucket("later").length };
  const defs = [["today","Today"],["week","This Week"],["waiting","Waiting For"],["later","Later"],["projects","Projects"],["done","Done"]];
  document.getElementById("tabs").innerHTML = defs.map(([k, label]) => {
    const c = counts[k] !== undefined ? `<span class="count">${counts[k]}</span>` : "";
    return `<button class="${tab===k?"active":""}" onclick="setTab('${k}')">${label}${c}</button>`;
  }).join("");
}
function setTab(k) { tab = k; render(); syncAddBucket(); }

// The add form's bucket follows the tab you're on (Waiting/Projects/Done default
// to This Week); the "waiting on whom" field appears only on the Waiting tab.
function syncAddBucket() {
  const map = { today: "today", week: "week", later: "later" };
  document.getElementById("add-bucket").value = map[tab] || "week";
  document.getElementById("add-wait").classList.toggle("hidden", tab !== "waiting");
}

function renderAddBar() {
  const sel = document.getElementById("add-project");
  const cur = sel.value;
  sel.innerHTML = state.projects.map(p => `<option ${p===cur?"selected":""}>${esc(p)}</option>`).join("")
    + `<option value="__new__">+ New project…</option>`;
}

function taskRow(t, opts = {}) {
  const wait = opts.waitingView && t.waiting_on && !t.completed_at
    ? `<span class="wait">waiting on ${esc(t.waiting_on)}</span>` : "";
  const tagBits = [
    opts.hideProj ? "" : `<span class="proj">${esc(t.project)}</span>`,
    dueTag(t), wait,
  ].filter(Boolean).join("");
  const attLabel = name => (t.attachment_labels || {})[name] || name.replace(/\.(msg|eml)$/i, "");
  const atts = (t.attachments || []).map(name => {
    const isEmail = /\.(msg|eml)$/i.test(name);
    return `<span class="att" data-name="${esc(name)}" title="${esc(name)} — opens in ${isEmail ? "Outlook" : "default app"}">` +
      `${esc(attLabel(name))}` +
      `<button class="attx" data-name="${esc(name)}" title="Remove attachment">×</button></span>`;
  }).join("");
  const mover = `<select class="mv" title="Move to…" onchange="moveTask(${t.id}, this.value)">`
    + BUCKETS.map(([b, label]) => `<option value="${b}" ${t.bucket===b?"selected":""}>${label}</option>`).join("")
    + `</select>`;
  const editor = expanded.has(t.id) ? `
    <div class="editor">
      <div><label>Note</label><textarea id="note-${t.id}">${esc(t.note)}</textarea></div>
      <div><label>Deadline (optional)</label>
        <input type="date" id="due-${t.id}" value="${esc(t.due || "")}"></div>
      <div><label>Waiting on (optional)</label>
        <input id="wait-${t.id}" value="${esc(t.waiting_on)}"></div>
      ${(t.attachments || []).length ? `
      <div><label>Attachment names</label>
        <div class="attlist">
          ${t.attachments.map((name, i) =>
            `<input id="attlabel-${t.id}-${i}" value="${esc(attLabel(name))}">`).join("")}
        </div>
      </div>` : ""}
      <button class="savebtn" onclick="saveDetails(${t.id})">Save</button>
    </div>` : "";
  return `
  <div class="task ${t.completed_at ? "done" : ""}" data-tid="${t.id}">
    <div class="row">
      <input type="checkbox" ${t.completed_at ? "checked" : ""} onchange="toggleDone(${t.id})">
      <div class="text">
        <div class="label">${esc(t.text)}</div>
        ${tagBits ? `<div class="tags">${tagBits}</div>` : ""}
        ${atts ? `<div class="atts">${atts}</div>` : ""}
        ${t.note && !expanded.has(t.id) ? `<div class="meta">${esc(t.note)}</div>` : ""}
      </div>
      <div class="controls">
        ${opts.pull ? `<button class="pull" onclick="moveTask(${t.id},'today')">→ Today</button>` : mover}
        <button title="Edit note / deadline" onclick="toggleEdit(${t.id})">Edit</button>
        <button class="del ${pendingDelete===t.id ? "arm" : ""}" title="Delete"
          onclick="delTask(${t.id})">${pendingDelete===t.id ? "Delete?" : "×"}</button>
      </div>
    </div>
    ${editor}
  </div>`;
}

function viewToday() {
  if (!state.tasks.length)
    return `<div class="empty">Nothing here yet.</div>`;
  const isDue = t => !!(t.due && t.due <= todayISO());
  const dueFirst = (a, b) => (isDue(b) ? 1 : 0) - (isDue(a) ? 1 : 0)
    || String(a.due || "~").localeCompare(String(b.due || "~"));
  const todays = inBucket("today").sort(dueFirst);
  // anything due (or overdue) that wasn't picked for today surfaces onto the deck
  const surfaced = state.tasks.filter(t => pending(t) && isDue(t) && t.bucket !== "today")
    .sort((a, b) => a.due.localeCompare(b.due));
  const deck = surfaced.concat(inBucket("week").filter(t => !isDue(t)));
  const doneToday = state.tasks.filter(t => t.completed_at && String(t.completed_at).slice(0,10) === todayISO());
  let html = "";
  html += todays.length
    ? todays.map(t => taskRow(t)).join("")
    : `<div class="empty">Nothing picked for today.</div>`;
  if (deck.length) {
    html += `<div class="section-title">On deck this week</div>`;
    html += deck.map(t => taskRow(t, {pull: true})).join("");
  }
  if (doneToday.length) {
    html += `<div class="section-title">Done today</div>`;
    html += doneToday.map(t => taskRow(t)).join("");
  }
  return html;
}

function viewGrouped(bucket, emptyMsg) {
  const tasks = inBucket(bucket);
  if (!tasks.length) return `<div class="empty">${emptyMsg}</div>`;
  const byProj = {};
  tasks.forEach(t => (byProj[t.project] = byProj[t.project] || []).push(t));
  return Object.keys(byProj).sort().map(p =>
    `<div class="group-title">${esc(p)}</div>` + byProj[p].map(t => taskRow(t, {hideProj: true})).join("")
  ).join("");
}

function viewWaiting() {
  const tasks = state.tasks.filter(t => pending(t) && t.waiting_on)
    .sort((a, b) => a.waiting_on.localeCompare(b.waiting_on));
  if (!tasks.length) return `<div class="empty">Nothing waiting on anyone.</div>`;
  return tasks.map(t => taskRow(t, {waitingView: true})).join("");
}

function viewProjects() {
  if (!state.projects.length)
    return `<div class="empty">No projects yet.</div>`;
  return state.projects.map(p => {
    const open = state.tasks.filter(t => t.project === p && pending(t));
    const total = state.tasks.filter(t => t.project === p).length;
    const items = open.length
      ? `<ul>` + open.map(t => `<li>${esc(t.text)}<span class="b">${esc(t.bucket)}</span>${dueTag(t)}</li>`).join("") + `</ul>`
      : `<div class="allclear">Nothing pending.</div>`;
    const rm = total === 0
      ? `<button class="projrm ${pendingProjRm===p ? "arm" : ""}" data-proj="${esc(p)}">${pendingProjRm===p ? "Remove?" : "Remove"}</button>`
      : "";
    return `<div class="proj-card">
      <h3>${esc(p)} <span class="b">${open.length} open</span>${rm}</h3>${items}</div>`;
  }).join("");
}

function viewDone() {
  const cutoff = doneDays === 0 ? null : new Date(Date.now() - doneDays * 86400000);
  let done = state.tasks.filter(t => t.completed_at)
    .filter(t => doneProject === "all" || t.project === doneProject)
    .filter(t => !cutoff || new Date(t.completed_at) >= cutoff)
    .sort((a, b) => String(b.completed_at).localeCompare(String(a.completed_at)));
  const projOptions = `<option value="all">All projects</option>` +
    state.projects.map(p => `<option ${p===doneProject?"selected":""}>${esc(p)}</option>`).join("");
  const rangeBtn = (d, label) =>
    `<button class="${doneDays===d?"active":""}" onclick="doneDays=${d};render()">${label}</button>`;
  let html = `<div class="filterbar">
      <select onchange="doneProject=this.value;render()">${projOptions}</select>
      ${rangeBtn(7,"Last 7 days")}${rangeBtn(14,"Last 14 days")}${rangeBtn(0,"All time")}
    </div>
`;
  if (!done.length) return html + `<div class="empty">Nothing completed in this range.</div>`;
  let lastDate = "";
  done.forEach(t => {
    const d = String(t.completed_at).slice(0, 10);
    if (d !== lastDate) { html += `<div class="done-date">${d}</div>`; lastDate = d; }
    html += taskRow(t);
  });
  return html;
}

// ---------- actions ----------
async function addTask() {
  const input = document.getElementById("add-text");
  const text = input.value.trim();
  if (!text) return;
  let project = document.getElementById("add-project").value;
  if (project === "__new__") {
    project = (prompt("New project name:") || "").trim();
    if (!project) return;
    await api("/api/project", { name: project });
  }
  const bucket = document.getElementById("add-bucket").value;
  const due = document.getElementById("add-due").value;
  const waitEl = document.getElementById("add-wait");
  const waiting_on = waitEl.classList.contains("hidden") ? "" : waitEl.value.trim();
  await api("/api/add", { text, project, bucket, due, waiting_on });
  input.value = "";
  document.getElementById("add-due").value = "";
  document.getElementById("add-wait").value = "";
  await refresh();
  toggleAdd();  // collapse after adding
}

async function toggleDone(id) {
  const t = state.tasks.find(t => t.id === id);
  await api("/api/update", { id, completed_at: t.completed_at ? null : new Date().toISOString().slice(0, 19) });
  await refresh();
}

async function moveTask(id, bucket) {
  await api("/api/update", { id, bucket });
  await refresh();
}

let pendingDelete = null;
let pendingProjRm = null;
async function removeProject(name) {
  if (pendingProjRm === name) {
    pendingProjRm = null;
    await api("/api/project-delete", { name });
    await refresh();
    return;
  }
  pendingProjRm = name;
  render();
  setTimeout(() => { if (pendingProjRm === name) { pendingProjRm = null; render(); } }, 3500);
}

async function delTask(id) {
  if (pendingDelete === id) {
    pendingDelete = null;
    await api("/api/delete", { id });
    await refresh();
    return;
  }
  pendingDelete = id;
  render();
  setTimeout(() => { if (pendingDelete === id) { pendingDelete = null; render(); } }, 3500);
}

function toggleEdit(id) {
  expanded.has(id) ? expanded.delete(id) : expanded.add(id);
  render();
}

async function saveDetails(id) {
  const note = document.getElementById("note-" + id).value;
  const waiting_on = document.getElementById("wait-" + id).value;
  const due = document.getElementById("due-" + id).value;
  const task = state.tasks.find(t => t.id === id);
  const attachment_labels = {};
  (task.attachments || []).forEach((name, i) => {
    const input = document.getElementById(`attlabel-${id}-${i}`);
    if (!input) return;
    const v = input.value.trim();
    if (v && v !== name.replace(/\.(msg|eml)$/i, "")) attachment_labels[name] = v;
  });
  await api("/api/update", { id, note, waiting_on, due, attachment_labels });
  expanded.delete(id);
  await refresh();
}

function toggleAdd() {
  const form = document.getElementById("add-form");
  const btn = document.getElementById("add-toggle");
  const nowOpen = form.classList.toggle("hidden") === false;
  btn.classList.toggle("hidden", nowOpen);
  if (nowOpen) {
    syncAddBucket();
    document.getElementById("add-text").focus();
  }
}

// ---------- attachments: click to open, × to detach, drag-and-drop to add ----------
const viewEl = document.getElementById("view");
viewEl.addEventListener("click", async e => {
  const prm = e.target.closest(".projrm");
  if (prm) { removeProject(prm.dataset.proj); return; }
  const card = e.target.closest(".task");
  if (!card) return;
  const id = Number(card.dataset.tid);
  const x = e.target.closest(".attx");
  if (x) { await api("/api/detach", { id, name: x.dataset.name }); await refresh(); return; }
  const chip = e.target.closest(".att");
  if (chip) await api("/api/open", { id, name: chip.dataset.name });
});
viewEl.addEventListener("dragover", e => {
  const card = e.target.closest(".task");
  if (card) { e.preventDefault(); card.classList.add("drop"); }
});
viewEl.addEventListener("dragleave", e => {
  const card = e.target.closest(".task");
  if (card && !card.contains(e.relatedTarget)) card.classList.remove("drop");
});
viewEl.addEventListener("drop", async e => {
  const card = e.target.closest(".task");
  if (!card) return;
  e.preventDefault();
  card.classList.remove("drop");
  const id = Number(card.dataset.tid);
  for (const f of e.dataTransfer.files) {
    await fetch("/api/attach?id=" + id, {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(f.name) },
      body: f,
    });
  }
  await refresh();
});

// ---------- init ----------
document.getElementById("add-form").addEventListener("keydown", e => {
  if (e.key === "Escape") toggleAdd();
});
document.getElementById("today-date").textContent =
  new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
refresh();
</script>
</body>
</html>
"""


def backup_data():
    """Once per day, on first launch: snapshot tasks.json to tasks.backup.json.

    Restore by copying the backup over tasks.json. The date guard means a
    restart later the same day never overwrites the morning snapshot.
    """
    backup = os.path.join(HERE, "tasks.backup.json")
    if not os.path.exists(DATA_FILE):
        return
    if os.path.exists(backup):
        stamped = datetime.fromtimestamp(os.path.getmtime(backup)).strftime("%Y-%m-%d")
        if stamped == datetime.now().strftime("%Y-%m-%d"):
            return
    shutil.copy2(DATA_FILE, backup)


def main():
    backup_data()
    load_data()  # creates an empty tasks.json on first run
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("Daybook running at %s  (Ctrl+C to stop)" % url)
    print("Data file: %s" % DATA_FILE)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
