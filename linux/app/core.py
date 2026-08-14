"""Session-state logic shared by the Linux app, kept import-clean of GTK so tests run headless.

The semantics mirror StatusController in Sources/main.swift; the per-session JSON files in
~/.claude/statusbar/state.d/ written by hooks/update.js and hooks/lifecycle.js are the contract
between the hooks and every renderer. Field meanings are documented in those two scripts.
"""

import errno
import json
import os
import random
from dataclasses import dataclass, field

# Effective-state recovery caps (seconds): a working state older than this reads as idle even if
# no hook ever fired again (Esc and denied permissions fire no hook, freezing the file).
CAP_PERMISSION = 7200
CAP_WORKING = 900

# Reap fallback for pre-upgrade files that carry no pid: idle + older than this is dropped.
DEFAULT_STALE_PRUNE_AGE = 900

WORKING_STATES = ("thinking", "tool")


@dataclass
class Session:
    id: str = ""
    state: str = "idle"
    label: str = ""
    project: str = ""
    transcript: str = ""
    cwd: str = ""
    entrypoint: str = ""     # CLAUDE_CODE_ENTRYPOINT: "cli", "claude-desktop", …
    term_program: str = ""
    pid: int = 0             # the session's `claude` process; kill(pid, 0) drives liveness
    started: bool = False    # real activity happened (update.js flips it); gates desktop rows
    started_at: float = 0.0  # unix seconds the current turn began (0 = no clock)
    ts: float = 0.0          # last hook event
    eff: str = ""            # effective state, recomputed once per tick
    branch: str = ""         # git branch (short SHA when detached); "" outside a repo
    display_name: str = ""   # project, parent-qualified when two live sessions share a name


def parse_session(obj, sid):
    def num(v):
        return float(v) if isinstance(v, (int, float)) else 0.0
    return Session(
        id=sid,
        state=obj.get("state") or "idle",
        label=obj.get("label") or "",
        project=obj.get("project") or "",
        transcript=obj.get("transcript") or "",
        cwd=obj.get("cwd") or "",
        entrypoint=obj.get("entrypoint") or "",
        term_program=obj.get("term_program") or "",
        pid=int(num(obj.get("pid"))),
        started=bool(obj.get("started", False)),
        started_at=num(obj.get("startedAt")),
        ts=num(obj.get("ts")),
    )


def load_session_file(path, sid):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_session(json.load(f), sid)
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    """kill(pid, 0): EPERM means alive-but-not-ours, ESRCH means gone."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def last_turn_line(path, tail_bytes=8192):
    """Last actual turn line (a user/assistant message) of a transcript, tailing ~8KB.

    Claude Code appends bookkeeping lines (system/away_summary, last-prompt, ai-title, …)
    after an interrupt; those would hide the "interrupted by user" marker.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    for line in reversed(text.split("\n")):
        if '"type":"user"' in line or '"type":"assistant"' in line:
            return line
    return None


def effective_state(s, now, turn_line_reader=last_turn_line):
    """Raw state plus two recovery nets: an absolute age cap, and the transcript's
    "interrupted by user" marker. "done" collapses to idle."""
    if s.state in ("thinking", "tool", "permission"):
        cap = CAP_PERMISSION if s.state == "permission" else CAP_WORKING
        if now - s.ts > cap:
            return "idle"
        if s.transcript:
            last = turn_line_reader(s.transcript)
            if last and "interrupted by user" in last:
                return "idle"
        return s.state
    return "idle" if s.state == "done" else s.state


def should_reap(s, now, stale_prune_age=DEFAULT_STALE_PRUNE_AGE, alive=pid_alive):
    """A session leaves when its `claude` process is gone, so idle-but-open sessions stay.
    Pre-upgrade files (pid 0) fall back to the idle+age prune so they can't linger forever."""
    if s.pid > 0:
        return not alive(s.pid)
    return s.eff == "idle" and stale_prune_age > 0 and now - s.ts > stale_prune_age


def priority(eff):
    """Rank for surfacing: a session awaiting YOUR permission is never hidden behind one
    merely thinking."""
    if eff == "permission":
        return 2
    if eff in WORKING_STATES:
        return 1
    return 0


def pick_lead(sessions):
    """The single highest-priority session; ties broken by recency."""
    return max(sessions, key=lambda s: (priority(s.eff), s.ts), default=None)


def assign_display_names(sessions):
    """Same-named projects (two clones/worktrees of one repo) get a parent-folder qualifier
    ("work/myrepo" vs "tmp/myrepo"). Only non-empty cwds count as colliding locations."""
    cwds_by_project = {}
    for s in sessions:
        if s.project and s.cwd:
            cwds_by_project.setdefault(s.project, set()).add(s.cwd)
    for s in sessions:
        if s.cwd and len(cwds_by_project.get(s.project, ())) > 1:
            parent = os.path.basename(os.path.dirname(s.cwd))
            s.display_name = f"{parent}/{s.project}" if parent else s.project
        else:
            s.display_name = s.project


def session_name(s):
    if s.display_name:
        return s.display_name
    return s.project or "session"


def git_head_path(cwd, walk_limit=40):
    """<cwd>'s HEAD path, walking toward /. A worktree/submodule has .git as a FILE
    containing "gitdir: <path>". None for non-git dirs."""
    d = cwd
    for _ in range(walk_limit):
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return os.path.join(g, "HEAD")
        if os.path.isfile(g):
            try:
                with open(g, "r", encoding="utf-8") as f:
                    first = f.readline(4096).strip()
            except OSError:
                return None
            if first.startswith("gitdir: "):
                gd = first[len("gitdir: "):].strip()
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(d, gd))
                return os.path.join(gd, "HEAD")
            return None
        parent = os.path.dirname(d)
        if parent == d or not parent:
            return None
        d = parent
    return None


def branch_for_cwd(cwd):
    """HEAD is "ref: refs/heads/<branch>" on a branch, a bare hash when detached (-> short SHA).
    "" outside a repo or for anything unrecognized."""
    if not cwd:
        return ""
    head_path = git_head_path(cwd)
    if not head_path:
        return ""
    try:
        with open(head_path, "r", encoding="utf-8") as f:
            head = f.read(1024).strip()
    except OSError:
        return ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    if head.startswith("ref: "):
        return head.rsplit("/", 1)[-1]
    if 40 <= len(head) <= 64 and all(c in "0123456789abcdef" for c in head):
        return head[:7]
    return ""


def elapsed(secs):
    """"1m 1s" / "43s", the elapsed-clock style Claude Code itself uses."""
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def truncated(s, limit=20, keep=18):
    return s[:keep] + "…" if len(s) > limit else s


def version_newer(a, b):
    """Numeric component-wise compare so "0.0.10" > "0.0.9"."""
    def parts(v):
        out = []
        for p in v.split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else 0
        y = pb[i] if i < len(pb) else 0
        if x != y:
            return x > y
    return False


def pick_thinking_word(words, previous=None, rng=random):
    """A fresh word on each entry into "thinking", avoiding an immediate repeat."""
    if not words:
        return "Thinking"
    w = rng.choice(words)
    if len(words) > 1:
        while w == previous:
            w = rng.choice(words)
    return w


def working_label(s, use_thinking_words, word):
    if use_thinking_words and s.state == "thinking" and word:
        return word + "…"
    if s.label:
        return s.label
    return "Working…" if s.state == "tool" else "Thinking…"


def status_text(s, eff, use_thinking_words, word):
    if eff == "permission":
        return "Awaiting permission"
    if eff in WORKING_STATES:
        return working_label(s, use_thinking_words, word)
    return "Done" if s.state == "done" else "Idle"
