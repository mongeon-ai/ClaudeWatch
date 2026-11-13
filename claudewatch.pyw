"""ClaudeWatch - a small always-on-top window that lists everything Claude is
running on this PC (sessions, their commands, subagents, scheduled runs,
leftovers) with CPU / RAM and a pause / resume / stop button per item.

Run:  pythonw claudewatch.pyw      (needs: pip install --user psutil)
"""
import glob
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
PAUSED_PATH = os.path.join(HERE, "paused.json")
HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
LOCAL = os.environ.get("LOCALAPPDATA", "")
ROAMING = os.environ.get("APPDATA", "")
SESSION_META_DIRS = [
    os.path.join(LOCAL, "Claude-Data", "*", "claude-code-sessions", "*", "*"),
    os.path.join(ROAMING, "Claude", "claude-code-sessions", "*", "*"),
]
DEFAULT_CONFIG = {
    "x": None, "y": None, "topmost": True, "mini": False,
    "refresh_seconds": 2,
    # Windows scheduled tasks whose action matches one of these are shown.
    "task_patterns": [r"E:\\AI", r"acme\.sh"],
    # always-on services (HouseDashboard runs as SYSTEM: can't be measured or paused from here)
    "ignore_tasks": ["HouseDashboard"],
}
SHELLS = {"cmd.exe", "conhost.exe", "pwsh.exe", "powershell.exe", "bash.exe",
          "sh.exe", "chcp.com", "openconsole.exe"}
NCPU = psutil.cpu_count() or 1
NO_WINDOW = 0x08000000
MYPID = os.getpid()

# ---------------------------------------------------------------- helpers


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass


def fmt_mem(b):
    if b >= 1 << 30:
        return "%.1f GB" % (b / (1 << 30))
    return "%d MB" % (b / (1 << 20))


def short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def is_app_exe(exe):
    e = (exe or "").lower()
    return ("\\windowsapps\\claude_" in e or "\\anthropicclaude\\" in e
            or e.endswith("\\claude\\claude.exe")) and "\\claude-code\\" not in e


# ---------------------------------------------------------------- collector


class Collector:
    """Builds a snapshot of Claude activity. Runs in a background thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.procs = {}       # pid -> psutil.Process (create_time checked)
        self.ctime = {}       # pid -> create_time
        self.cmd = {}         # (pid, ctime) -> cmdline list
        self.envsess = {}     # (pid, ctime) -> host session id or ""
        self.meta = {}        # host session id -> (mtime, dict)
        self.transcripts = {}  # cli session id -> path or None
        self.tasks = []       # windows scheduled tasks (matching patterns)
        self.tasks_at = 0
        self.task_lock = threading.Lock()
        self.last_cpu = {}    # pid -> cpu% measured this tick (per-core units)

    # -- per-process basics --------------------------------------------
    def proc(self, pid):
        p = self.procs.get(pid)
        if p is None:
            try:
                p = psutil.Process(pid)
                p.cpu_percent(None)
            except psutil.Error:
                return None
            self.procs[pid] = p
        return p

    def cmdline(self, pid):
        key = (pid, self.ctime.get(pid))
        if key not in self.cmd:
            try:
                self.cmd[key] = self.procs_cmd(pid)
            except psutil.Error:
                self.cmd[key] = []
        return self.cmd[key]

    def procs_cmd(self, pid):
        p = self.proc(pid)
        return p.cmdline() if p else []

    def host_session(self, pid):
        """CLAUDE_CODE_HOST_SESSION_ID of a process (only that one variable is kept)."""
        key = (pid, self.ctime.get(pid))
        if key not in self.envsess:
            val = ""
            try:
                p = self.proc(pid)
                if p:
                    val = p.environ().get("CLAUDE_CODE_HOST_SESSION_ID", "")
            except Exception:
                pass
            self.envsess[key] = val
        return self.envsess[key]

    def usage(self, pids):
        cpu = mem = 0.0
        for pid in pids:
            p = self.proc(pid)
            if not p:
                continue
            try:
                with p.oneshot():
                    c = p.cpu_percent(None)
                    self.last_cpu[pid] = c
                    cpu += c
                    mem += p.memory_info().rss
            except psutil.Error:
                pass
        return cpu / NCPU, mem

    # -- session metadata ----------------------------------------------
    def session_meta(self, host_id):
        if not host_id:
            return {}
        cached = self.meta.get(host_id)
        path = cached[2] if cached else None
        if not path or not os.path.exists(path):
            path = None
            for pat in SESSION_META_DIRS:
                hits = glob.glob(os.path.join(pat, host_id + ".json"))
                if hits:
                    path = hits[0]
                    break
            if not path:
                return {}
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return {}
        if cached and cached[0] == mt:
            return cached[1]
        if cached and time.time() - cached[3] < 10:
            return cached[1]  # metadata changes often; re-read at most every 10s
        info = {}
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for k in ("title", "cliSessionId", "scheduledTaskId", "cwd", "model"):
                m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*")' % k, text)
                if m:
                    info[k] = json.loads(m.group(1))
        except Exception:
            pass
        self.meta[host_id] = (mt, info, path, time.time())
        return info

    def transcript(self, cli_id):
        if not cli_id:
            return None
        if cli_id not in self.transcripts or self.transcripts[cli_id] is None:
            hits = glob.glob(os.path.join(PROJECTS_DIR, "*", cli_id + ".jsonl"))
            self.transcripts[cli_id] = hits[0] if hits else None
        return self.transcripts[cli_id]

    def subagents(self, transcript_path):
        out = []
        if not transcript_path:
            return out
        sub = os.path.join(transcript_path[:-6], "subagents")
        now = time.time()
        for meta_path in glob.glob(os.path.join(sub, "*.meta.json")):
            log = meta_path[: -len(".meta.json")] + ".jsonl"
            try:
                age = now - os.path.getmtime(log)
            except OSError:
                continue
            if age > 600:
                continue
            m = load_json(meta_path, {})
            if m.get("finished"):
                continue
            out.append({"desc": m.get("description") or m.get("agentType") or "subagent",
                        "type": m.get("agentType", ""), "age": age,
                        "id": os.path.basename(log)})
        return out

    # -- windows scheduled tasks (refreshed every 60 s in its own thread) ---
    def refresh_tasks(self):
        ps = (
            "Get-ScheduledTask | ?{ $_.TaskPath -notlike '\\Microsoft\\*' } | %{ "
            "$i = $_ | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue; "
            "[pscustomobject]@{ n=$_.TaskName; s=[string]$_.State; "
            "a=(($_.Actions | %{ \"$($_.Execute) $($_.Arguments)\" }) -join ' ; '); "
            "next= if($i -and $i.NextRunTime){ $i.NextRunTime.ToString('o') } else { '' } } "
            "} | ConvertTo-Json -Compress"
        )
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=60,
                                 creationflags=NO_WINDOW).stdout
            data = json.loads(out) if out.strip() else []
            if isinstance(data, dict):
                data = [data]
        except Exception:
            return
        pats = [re.compile(p, re.I) for p in self.cfg.get("task_patterns", [])]
        tasks = []
        for t in data:
            action = t.get("a") or ""
            if not any(p.search(action) for p in pats) or t.get("n") in self.cfg.get("ignore_tasks", []):
                continue
            # signature used to recognise the task's running process
            quoted = re.findall(r'"([^"]+)"', action)
            paths = [q for q in quoted if re.search(r"\.(py|pyw|sh|ps1|js|exe|bat|cmd)$", q, re.I)]
            sig = max(paths[1:] or paths, key=len) if paths else action.split(" ;")[0].strip()
            m = re.match(r'\s*"?([^"]*?\.exe)', action, re.I)
            exe = os.path.basename(m.group(1)).lower() if m else ""
            tasks.append({"name": t.get("n"), "state": t.get("s"), "action": action,
                          "sig": sig.lower(), "exe": exe, "next": t.get("next") or ""})
        with self.task_lock:
            self.tasks = tasks
            self.tasks_at = time.time()

    # -- the snapshot ------------------------------------------------------
    def snapshot(self):
        ppid, names, exes = {}, {}, {}
        self.last_cpu = {}
        for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            i = p.info
            pid = i["pid"]
            if self.ctime.get(pid) != i["create_time"]:
                self.procs.pop(pid, None)
                self.ctime[pid] = i["create_time"]
            ppid[pid] = i["ppid"]
            names[pid] = (i["name"] or "").lower()
        alive = set(ppid)
        for d in (self.procs, self.ctime):
            for pid in list(d):
                if pid not in alive:
                    d.pop(pid, None)
        if len(self.cmd) > 5000:
            self.cmd.clear()
            self.envsess.clear()

        kids = {}
        for pid, pp in ppid.items():
            if pp != pid:
                kids.setdefault(pp, []).append(pid)

        def tree(root):
            out, stack = [], [root]
            while stack:
                x = stack.pop()
                out.append(x)
                stack.extend(kids.get(x, []))
            return out

        def exe_of(pid):
            if pid not in exes:
                try:
                    exes[pid] = self.proc(pid).exe() if self.proc(pid) else ""
                except psutil.Error:
                    exes[pid] = ""
            return exes[pid]

        claimed = set(tree(MYPID))

        # Claude desktop app: main process + its claude.exe helpers
        app_main = None
        for pid, n in names.items():
            if n == "claude.exe" and is_app_exe(exe_of(pid)) and names.get(ppid.get(pid)) != "claude.exe":
                app_main = pid
                break

        sessions, app_side = [], []
        cli_pids = [pid for pid, n in names.items()
                    if n == "claude.exe" and not is_app_exe(exe_of(pid))
                    and "--type=" not in " ".join(self.cmdline(pid))]
        for cli in cli_pids:
            # skip a CLI nested inside another CLI's tree (it shows as a command there)
            anc, nested = ppid.get(cli), False
            for _ in range(30):
                if anc in cli_pids:
                    nested = True
                    break
                if anc is None or anc == ppid.get(anc):
                    break
                anc = ppid.get(anc)
            if nested:
                continue
            sessions.append(self.build_session(cli, tree, kids, names))
            claimed.update(tree(cli))

        app_item = None
        if app_main:
            app_tree = tree(app_main)
            helpers = [p for p in app_tree if p not in claimed and names.get(p) == "claude.exe"]
            claimed.update(helpers)
            cpu, mem = self.usage(helpers)
            # things the app launched itself: terminal tabs, preview servers, ...
            for p in app_tree:
                if p in claimed or names.get(p) in ("claude.exe", "conhost.exe"):
                    continue
                if names.get(ppid.get(p)) != "claude.exe":
                    continue
                sub = tree(p)
                claimed.update(sub)
                c, m = self.usage(sub)
                label, leaf = self.describe(p, sub, names)
                is_terminal = leaf is None and names.get(p) in ("pwsh.exe", "powershell.exe", "bash.exe", "cmd.exe")
                if is_terminal and c < 1 and len(sub) <= 2:
                    continue  # idle terminal tab: not interesting
                app_side.append(self.item("app:%d" % p, "proc", ("Terminal: " + label) if is_terminal else label,
                                          c, m, [p], len(sub), state=None, cmd=self.cmdline(leaf or p)))
            app_item = self.item("app", "app", "Claude desktop app", cpu, mem, [], len(helpers),
                                 state=None, children=app_side, can_act=False)

        # scheduled Windows tasks that are running now
        sched = []
        with self.task_lock:
            tasks = list(self.tasks)
        for t in tasks:
            roots = []
            for pid, n in names.items():
                if pid in claimed or (t["exe"] and n != t["exe"] and n not in ("python.exe", "pythonw.exe", "bash.exe")):
                    continue
                cl = " ".join(self.cmdline(pid)).lower()
                if t["sig"] and t["sig"] in cl:
                    roots.append(pid)
            roots = [r for r in roots if ppid.get(r) not in roots]
            if not roots:
                continue
            allp = []
            for r in roots:
                allp += tree(r)
            claimed.update(allp)
            c, m = self.usage(allp)
            started = min(self.ctime.get(r) or time.time() for r in roots)
            sched.append(self.item("task:" + t["name"], "task", t["name"], c, m, roots, len(allp),
                                   sub="Windows task, running %s" % ago(started), cmd=t["action"]))

        # leftovers: processes started from a Claude session that are no longer under it
        live_hosts = {s["host"]: s for s in sessions if s.get("host")}
        left_by = {}
        for pid, n in names.items():
            if pid in claimed or n in ("conhost.exe",):
                continue
            h = self.host_session(pid)
            if not h:
                continue
            if ppid.get(pid) in ppid and self.host_session(ppid[pid]) == h and ppid[pid] not in claimed:
                continue  # its parent is the leftover root
            left_by.setdefault(h, []).append(pid)
        leftovers = []
        for h, roots in left_by.items():
            for r in roots:
                sub = [x for x in tree(r) if x not in claimed]
                claimed.update(sub)
                c, m = self.usage(sub)
                label, leaf = self.describe(r, sub, names)
                if h in live_hosts:
                    live_hosts[h]["children"].append(self.item(
                        "det:%d" % r, "proc", label + "  (detached)", c, m, [r], len(sub),
                        cmd=self.cmdline(leaf or r)))
                    live_hosts[h]["cpu"] += c
                    live_hosts[h]["mem"] += m
                else:
                    title = self.session_meta(h).get("title") or h
                    leftovers.append(self.item("left:%d" % r, "proc", label, c, m, [r], len(sub),
                                               sub="from ended session: " + short(title, 40),
                                               cmd=self.cmdline(leaf or r)))

        with self.task_lock:
            nxt = sorted((t["next"], t["name"]) for t in self.tasks if t["next"])
        next_task = None
        for when, name in nxt:
            try:
                dt = datetime.fromisoformat(when[:19])
            except ValueError:
                continue
            if dt > datetime.now():
                next_task = (dt, name)
                break

        total_cpu = sum(i["cpu"] for i in sessions + sched + leftovers) + (app_item["cpu"] if app_item else 0)
        total_mem = sum(i["mem"] for i in sessions + sched + leftovers) + (app_item["mem"] if app_item else 0)
        vm = psutil.virtual_memory()
        return {
            "sessions": sorted(sessions, key=lambda s: (s["state"] == "idle", -s["cpu"])),
            "scheduled": sched, "leftovers": leftovers, "app": app_item,
            "total_cpu": total_cpu, "total_mem": total_mem,
            "pc_cpu": psutil.cpu_percent(None), "pc_mem_used": vm.total - vm.available,
            "pc_mem_total": vm.total, "next_task": next_task, "t": time.time(),
        }

    def item(self, key, kind, title, cpu, mem, roots, nproc, state="idle", sub="",
             children=None, can_act=True, cmd=None):
        return {"key": key, "kind": kind, "title": title, "cpu": cpu, "mem": mem,
                "roots": roots, "nproc": nproc, "state": state, "sub": sub,
                "children": children or [], "can_act": can_act and bool(roots),
                "cmd": cmd if isinstance(cmd, str) else " ".join(cmd or [])}

    def describe(self, root, sub, names):
        """A human label for a process subtree: its most active non-shell process."""
        leaves = [p for p in sub if names.get(p) not in SHELLS]
        leaf = None
        if leaves:
            best = -1
            for p in leaves:
                c = self.last_cpu.get(p, 0)
                if c > best:
                    best, leaf = c, p
        target = leaf or root
        cl = self.cmdline(target)
        name = (names.get(target) or "?").replace(".exe", "")
        args = []
        for a in cl[1:]:
            if a.startswith("$__claude"):
                continue
            if ("\\" in a or "/" in a) and len(a) > 25 and " " not in a:
                a = os.path.basename(a.rstrip("\\/")) or a
            args.append(a)
        if leaf is None:
            joined = " ".join(cl).lower()
            if "-command" in joined and "$__claude" in joined:
                return "PowerShell command", None
            if name in ("bash", "sh"):
                return "Bash command", None
        return short(name + " " + " ".join(args), 80), leaf

    def build_session(self, cli, tree, kids, names):
        host = self.host_session(cli)
        meta = self.session_meta(host)
        cli_id = meta.get("cliSessionId")
        tpath = self.transcript(cli_id)
        title = meta.get("title") or ("Terminal session in " + meta.get("cwd", "?") if not host else host)
        sub = []
        if meta.get("scheduledTaskId"):
            title = "Scheduled: " + meta["scheduledTaskId"]
        if meta.get("model"):
            sub.append(meta["model"].replace("claude-", ""))
        children = []
        for k in kids.get(cli, []):
            n = names.get(k, "")
            if n == "conhost.exe":
                continue
            st = tree(k)
            if MYPID in st:
                continue  # this monitor, when launched from a Claude session
            c, m = self.usage(st)
            label, leaf = self.describe(k, st, names)
            if "mcp" in " ".join(self.cmdline(k)).lower():
                label = "MCP server: " + label
            children.append(self.item("cmd:%d" % k, "proc", label, c, m, [k], len(st),
                                      cmd=self.cmdline(leaf or k)))
        cpu_own, mem_own = self.usage([cli])
        cpu = cpu_own + sum(c["cpu"] for c in children)
        mem = mem_own + sum(c["mem"] for c in children)
        for a in self.subagents(tpath):
            children.append(self.item("sub:" + a["id"], "subagent", "Subagent: " + a["desc"], 0, 0, [], 0,
                                      state="working" if a["age"] < 120 else "idle",
                                      sub=a["type"], can_act=False))
        age = time.time() - os.path.getmtime(tpath) if tpath and os.path.exists(tpath) else 9999
        state = "working" if age < 20 or cpu > 2 else "idle"
        if age < 9999:
            sub.append("active now" if age < 20 else "last activity " + ago(time.time() - age))
        children.sort(key=lambda c: -c["cpu"])
        it = self.item("sess:%d" % cli, "session", title, cpu, mem, [cli],
                       1 + sum(c["nproc"] for c in children), state=state,
                       sub=" \u00b7 ".join(sub), children=children)
        it["host"] = host
        return it


def ago(ts):
    s = max(0, time.time() - ts)
    if s < 60:
        return "%ds ago" % s
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh%02dm ago" % (s // 3600, (s % 3600) // 60)
    return "%dd ago" % (s // 86400)


# ---------------------------------------------------------------- process control


def all_procs(roots):
    out = []
    for r in roots:
        try:
            p = psutil.Process(r)
        except psutil.Error:
            continue
        out.append(p)
        try:
            out += p.children(recursive=True)
        except psutil.Error:
            pass
    return out


class Control:
    """Pause = suspend every process in the tree; we remember exactly what we
    suspended (on disk) so it can always be resumed, even after a restart."""

    def __init__(self):
        raw = load_json(PAUSED_PATH, {})
        self.paused = {}
        for key, lst in raw.items():
            alive = []
            for pid, ct in lst:
                try:
                    if abs(psutil.Process(pid).create_time() - ct) < 0.01:
                        alive.append((pid, ct))
                except psutil.Error:
                    pass
            if alive:
                self.paused[key] = alive
        self.save()

    def save(self):
        save_json(PAUSED_PATH, self.paused)

    def is_paused(self, key):
        return key in self.paused

    def pause(self, key, roots):
        done = []
        for p in all_procs(roots):
            if p.pid == MYPID:
                continue
            try:
                p.suspend()
                done.append((p.pid, p.create_time()))
            except psutil.Error:
                pass
        if done:
            self.paused[key] = done
            self.save()
        return len(done)

    def resume(self, key):
        n = 0
        for pid, ct in reversed(self.paused.pop(key, [])):
            try:
                p = psutil.Process(pid)
                if abs(p.create_time() - ct) < 0.01:
                    p.resume()
                    n += 1
            except psutil.Error:
                pass
        self.save()
        return n

    def resume_all(self):
        for key in list(self.paused):
            self.resume(key)

    def stop(self, key, roots):
        if key in self.paused:
            self.resume(key)  # a suspended process can be slow to die
        procs = [p for p in all_procs(roots) if p.pid != MYPID]
        for p in reversed(procs):
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=3)
        return len(procs)


# ---------------------------------------------------------------- UI

BG, BG2, BG3 = "#16161a", "#1f1f25", "#2a2a33"
FG, DIM = "#e8e6e3", "#8b8a93"
GREEN, AMBER, RED, BLUE = "#4cc38a", "#f0b429", "#ef5d5d", "#6ea8fe"
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI Semibold", 9)
FONT_S = ("Segoe UI", 8)
FONT_H = ("Segoe UI Semibold", 10)


def cpu_color(c):
    return RED if c >= 40 else AMBER if c >= 12 else FG


class Tooltip:
    def __init__(self, widget):
        self.w, self.tip, self.text = widget, None, ""
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None):
        if not self.text or self.tip:
            return
        x = self.w.winfo_rootx() + 10
        y = self.w.winfo_rooty() + self.w.winfo_height() + 2
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, bg="#000", fg=FG, font=FONT_S, justify="left",
                 wraplength=420, padx=6, pady=4).pack()

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Row:
    def __init__(self, app, parent):
        self.app, self.item, self.depth = app, None, 0
        self.f = tk.Frame(parent, bg=BG2)
        self.arrow = tk.Label(self.f, text=" ", bg=BG2, fg=DIM, font=FONT, width=2, cursor="hand2")
        self.dot = tk.Label(self.f, text="\u25cf", bg=BG2, font=FONT)
        self.txt = tk.Frame(self.f, bg=BG2)
        self.title = tk.Label(self.txt, bg=BG2, fg=FG, font=FONT_B, anchor="w")
        self.sub = tk.Label(self.txt, bg=BG2, fg=DIM, font=FONT_S, anchor="w")
        self.cpu = tk.Label(self.f, bg=BG2, fg=FG, font=FONT, width=5, anchor="e")
        self.mem = tk.Label(self.f, bg=BG2, fg=DIM, font=FONT, width=7, anchor="e")
        self.btn_p = tk.Label(self.f, bg=BG3, fg=FG, font=FONT, width=2, cursor="hand2")
        self.btn_s = tk.Label(self.f, text="\u2715", bg=BG3, fg=RED, font=FONT, width=2, cursor="hand2")
        self.arrow.pack(side="left")
        self.dot.pack(side="left")
        self.txt.pack(side="left", fill="x", expand=True, padx=(2, 4))
        self.title.pack(fill="x")
        self.btn_s.pack(side="right", padx=(2, 4), pady=2)
        self.btn_p.pack(side="right", padx=2, pady=2)
        self.mem.pack(side="right")
        self.cpu.pack(side="right")
        self.arrow.bind("<Button-1>", lambda e: app.toggle(self.item["key"]))
        self.btn_p.bind("<Button-1>", lambda e: app.on_pause(self.item))
        self.btn_s.bind("<Button-1>", lambda e: app.on_stop(self.item))
        self.tip_title = Tooltip(self.title)
        self.tip_p = Tooltip(self.btn_p)
        self.tip_s = Tooltip(self.btn_s)

    def update(self, it, depth, expanded, paused):
        self.item, self.depth = it, depth
        indent = 16 * depth
        self.arrow.config(text=("\u25be" if expanded else "\u25b8") if it["children"] else " ")
        self.arrow.pack_configure(padx=(indent, 0))
        state = "paused" if paused else it["state"]
        self.dot.config(fg={"working": GREEN, "paused": AMBER, "idle": DIM}.get(state, BLUE))
        width = 44 - depth * 3
        self.title.config(text=short(it["title"], width), font=FONT_B if depth == 0 else FONT,
                          fg=AMBER if paused else FG)
        sub = it["sub"]
        if paused:
            sub = "PAUSED" + (" \u00b7 " + sub if sub else "")
        if sub:
            self.sub.config(text=short(sub, width + 8))
            self.sub.pack(fill="x")
        else:
            self.sub.pack_forget()
        has_usage = it["kind"] != "subagent"
        self.cpu.config(text=("%d%%" % round(it["cpu"])) if has_usage else "", fg=cpu_color(it["cpu"]))
        self.mem.config(text=fmt_mem(it["mem"]) if has_usage else "")
        tip = it["title"]
        if it.get("cmd"):
            tip += "\n\n" + short(it["cmd"], 400)
        if it["roots"]:
            tip += "\n\n%d process(es), root PID %s" % (it["nproc"], ", ".join(map(str, it["roots"])))
        if it["kind"] == "subagent":
            tip += "\n\nRuns inside its session's process: pause or stop the session to pause or stop it."
        self.tip_title.text = tip
        if it["can_act"]:
            self.btn_p.config(text="\u25b6" if paused else "\u2016", fg=AMBER if paused else FG)
            self.tip_p.text = "Resume" if paused else "Pause (freeze every process in it; Resume continues exactly where it was)"
            self.tip_s.text = ("Stop this session (ends its current turn; send it a message to continue)"
                               if it["kind"] == "session" else "Stop (kill these processes)")
            self.btn_p.pack(side="right", padx=2, pady=2, before=self.mem)
            self.btn_s.pack(side="right", padx=(2, 4), pady=2, before=self.btn_p)
        else:
            self.btn_p.pack_forget()
            self.btn_s.pack_forget()


class App:
    def __init__(self):
        self.cfg = dict(DEFAULT_CONFIG, **load_json(CONFIG_PATH, {}))
        self.col = Collector(self.cfg)
        self.ctl = Control()
        self.q = queue.Queue()
        self.rows = {}
        self.order = []
        self.expanded = set()
        self.collapsed = set()
        self.snap = None

        r = self.root = tk.Tk()
        r.title("Claude activity")
        r.configure(bg=BG)
        r.attributes("-toolwindow", True)
        r.attributes("-topmost", bool(self.cfg["topmost"]))
        r.resizable(False, False)
        if self.cfg.get("x") is not None:
            r.geometry("+%d+%d" % (self.cfg["x"], self.cfg["y"]))
        r.protocol("WM_DELETE_WINDOW", self.close)

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=8, pady=(6, 2))
        self.h1 = tk.Label(head, text="Claude", bg=BG, fg=FG, font=FONT_H, anchor="w")
        self.h1.pack(side="left")
        self.b_mini = tk.Label(head, text="\u2013", bg=BG3, fg=FG, font=FONT, width=2, cursor="hand2")
        self.b_mini.pack(side="right", padx=(3, 0))
        self.b_pin = tk.Label(head, text="Top", bg=BG3, font=FONT_S, padx=4, cursor="hand2")
        self.b_pin.pack(side="right", padx=(3, 0))
        self.b_mini.bind("<Button-1>", lambda e: self.set_mini(not self.cfg["mini"]))
        self.b_pin.bind("<Button-1>", lambda e: self.set_topmost(not self.cfg["topmost"]))
        Tooltip(self.b_pin).text = "Keep this window above all others"
        Tooltip(self.b_mini).text = "Shrink to the summary line"
        self.h2 = tk.Label(r, bg=BG, fg=DIM, font=FONT_S, anchor="w", justify="left")
        self.h2.pack(fill="x", padx=8)
        self.body = tk.Frame(r, bg=BG, width=470)
        self.body.pack(fill="x", padx=6, pady=(4, 6))
        self.foot = tk.Label(r, bg=BG, fg=DIM, font=FONT_S, anchor="w")
        self.foot.pack(fill="x", padx=8, pady=(0, 6))
        self.sections = {}
        self.set_topmost(self.cfg["topmost"], save=False)
        self.set_mini(self.cfg["mini"], save=False)

        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.task_loop, daemon=True).start()
        r.after(200, self.poll)

    # -- background ----------------------------------------------------
    def loop(self):
        while True:
            t0 = time.time()
            try:
                self.q.put(self.col.snapshot())
            except Exception as e:  # never let the monitor die
                self.q.put({"error": repr(e)})
            time.sleep(max(0.5, self.cfg["refresh_seconds"] - (time.time() - t0)))

    def task_loop(self):
        while True:
            self.col.refresh_tasks()
            time.sleep(60)

    def poll(self):
        snap = None
        while not self.q.empty():
            snap = self.q.get_nowait()
        if snap:
            self.render(snap)
        self.root.after(300, self.poll)

    # -- header buttons --------------------------------------------------
    def set_topmost(self, on, save=True):
        self.cfg["topmost"] = on
        self.root.attributes("-topmost", on)
        self.b_pin.config(fg=GREEN if on else DIM)
        if save:
            self.save_cfg()

    def set_mini(self, on, save=True):
        self.cfg["mini"] = on
        if on:
            self.body.pack_forget()
            self.foot.pack_forget()
        else:
            self.body.pack(fill="x", padx=6, pady=(4, 6))
            self.foot.pack(fill="x", padx=8, pady=(0, 6))
        self.b_mini.config(text="+" if on else "\u2013")
        if save:
            self.save_cfg()

    def save_cfg(self):
        try:
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        except tk.TclError:
            pass
        save_json(CONFIG_PATH, {k: self.cfg[k] for k in DEFAULT_CONFIG})

    # -- rendering ---------------------------------------------------------
    def section(self, name):
        if name not in self.sections:
            self.sections[name] = tk.Label(self.body, text=name.upper(), bg=BG, fg=DIM,
                                           font=("Segoe UI Semibold", 7), anchor="w")
        return self.sections[name]

    def is_expanded(self, it):
        k = it["key"]
        if k in self.expanded:
            return True
        if k in self.collapsed:
            return False
        # by default open a row whose children are doing real work
        return any(c["cpu"] >= 5 or c["kind"] == "subagent" for c in it["children"])

    def toggle(self, key):
        it = self.find(key)
        if not it:
            return
        if self.is_expanded(it):
            self.expanded.discard(key)
            self.collapsed.add(key)
        else:
            self.collapsed.discard(key)
            self.expanded.add(key)
        self.render(self.snap)

    def find(self, key, items=None):
        if items is None:
            s = self.snap or {}
            items = s.get("sessions", []) + s.get("scheduled", []) + s.get("leftovers", []) + ([s["app"]] if s.get("app") else [])
        for it in items:
            if it["key"] == key:
                return it
            f = self.find(key, it["children"])
            if f:
                return f
        return None

    def render(self, snap):
        if "error" in snap:
            self.foot.config(text="monitor error: " + short(snap["error"], 80), fg=RED)
            return
        self.snap = snap
        n_work = sum(1 for s in snap["sessions"] if s["state"] == "working")
        n_paused = len(self.ctl.paused)
        self.h1.config(text="Claude  %d%% CPU \u00b7 %s" % (round(snap["total_cpu"]), fmt_mem(snap["total_mem"])),
                       fg=cpu_color(snap["total_cpu"]))
        bits = ["%d session%s (%d working)" % (len(snap["sessions"]), "" if len(snap["sessions"]) == 1 else "s", n_work)]
        if snap["scheduled"]:
            bits.append("%d scheduled running" % len(snap["scheduled"]))
        if n_paused:
            bits.append("%d paused" % n_paused)
        bits.append("PC %d%% CPU \u00b7 %s / %s" % (round(snap["pc_cpu"]), fmt_mem(snap["pc_mem_used"]),
                                                    fmt_mem(snap["pc_mem_total"])))
        self.h2.config(text="  \u00b7  ".join(bits))
        if snap["next_task"]:
            dt, name = snap["next_task"]
            day = "" if dt.date() == datetime.now().date() else dt.strftime("%a ")
            self.foot.config(text="Next scheduled: %s at %s%s" % (name, day, dt.strftime("%H:%M")), fg=DIM)
        else:
            self.foot.config(text="")
        if self.cfg["mini"]:
            return

        layout = []  # (kind, key, payload)

        def add(items, depth):
            for it in items:
                exp = bool(it["children"]) and self.is_expanded(it)
                layout.append(("row", it["key"], (it, depth, exp)))
                if exp:
                    add(it["children"], depth + 1)

        for name, items in (("Sessions", snap["sessions"]), ("Scheduled (Windows tasks)", snap["scheduled"]),
                            ("Left running by ended sessions", snap["leftovers"])):
            if items:
                layout.append(("sec", name, None))
                add(items, 0)
        if snap["app"]:
            layout.append(("sec", "App", None))
            add([snap["app"]], 0)
        if not snap["sessions"]:
            layout.insert(0, ("sec", "No Claude sessions running", None))

        keys = [(k, key) for k, key, _ in layout]
        for kind, key, payload in layout:
            if kind == "row":
                if key not in self.rows:
                    self.rows[key] = Row(self, self.body)
                it, depth, exp = payload
                self.rows[key].update(it, depth, exp, self.ctl.is_paused(key))
        if keys != self.order:
            for w in self.body.winfo_children():
                w.pack_forget()
            live = set()
            for kind, key, _ in layout:
                if kind == "sec":
                    self.section(key).pack(fill="x", padx=2, pady=(6, 1))
                else:
                    self.rows[key].f.pack(fill="x", pady=1)
                    live.add(key)
            for key in list(self.rows):
                if key not in live:
                    self.rows.pop(key).f.destroy()
            self.order = keys

    # -- actions -----------------------------------------------------------
    def on_pause(self, it):
        if not it or not it["can_act"]:
            return
        if self.ctl.is_paused(it["key"]):
            self.ctl.resume(it["key"])
        else:
            if it["kind"] == "session" and not messagebox.askokcancel(
                    "Pause session",
                    "Pause \u201c%s\u201d?\n\nEvery process in it freezes (0%% CPU, RAM stays). "
                    "Press \u25b6 to continue. If it was mid-reply for a long time, Claude may "
                    "need to retry that step when resumed." % it["title"], parent=self.root):
                return
            self.ctl.pause(it["key"], it["roots"])
        if self.snap:
            self.render(self.snap)

    def on_stop(self, it):
        if not it or not it["can_act"]:
            return
        if it["kind"] == "session":
            msg = ("Stop \u201c%s\u201d?\n\nThis kills its %d process(es) and ends whatever it is doing "
                   "right now. The conversation itself is kept; send it a message in the app to carry on."
                   % (it["title"], it["nproc"]))
        else:
            msg = "Stop \u201c%s\u201d?\n\nThis kills %d process(es)." % (it["title"], it["nproc"])
        if not messagebox.askyesno("Stop", msg, icon="warning", parent=self.root):
            return
        self.ctl.stop(it["key"], it["roots"])

    def close(self):
        if self.ctl.paused:
            ans = messagebox.askyesnocancel(
                "Paused items",
                "%d item(s) are still paused. Resume them before closing?\n\n"
                "(If you choose No they stay frozen; reopen this window to resume them.)" % len(self.ctl.paused),
                parent=self.root)
            if ans is None:
                return
            if ans:
                self.ctl.resume_all()
        self.save_cfg()
        self.root.destroy()


if __name__ == "__main__":
    # one window only
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "ClaudeWatchSingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            raise SystemExit
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    App().root.mainloop()
