#!/usr/bin/env node
// SessionStart/SessionEnd hooks. Usage: node lifecycle.js <start|end>  (hook JSON, incl. session_id, on stdin)

const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const BUNDLE_ID = "com.local.claudestatusbar";
const EXEC = "ClaudeStatusBar";
const MAC = process.platform === "darwin";
const dir = path.join(os.homedir(), ".claude", "statusbar");
const stateDir = path.join(dir, "state.d");
const pidFile = path.join(dir, "app.pid"); // Linux: the app holds an exclusive flock on it for life
const event = process.argv[2];

fs.mkdirSync(stateDir, { recursive: true });

// Same resolution as update.js: on Linux the hook's parent is a transient shell, so walk
// /proc ancestry to the session's actual `claude` process (0 = not found, age-pruned later).
const sessionPid = () => {
  if (MAC) return process.ppid;
  let pid = process.ppid;
  for (let i = 0; i < 10 && pid > 1; i++) {
    let comm = "";
    try { comm = fs.readFileSync(`/proc/${pid}/comm`, "utf8").trim(); } catch { return 0; }
    if (comm === "claude") return pid;
    try {
      const stat = fs.readFileSync(`/proc/${pid}/status`, "utf8");
      pid = parseInt((stat.match(/^PPid:\s*(\d+)/m) || [])[1], 10);
    } catch { return 0; }
  }
  return 0;
};
const running = () => {
  if (MAC) { try { cp.execSync(`pgrep -x ${EXEC}`, { stdio: "ignore" }); return true; } catch { return false; } }
  // Pure /proc reads, no subprocess: the pid file names the last app instance, and the cmdline
  // check keeps a reused pid from counting as ours. A stale file after a crash reads as not
  // running, which is what tells this hook leftover session files are stale, not live.
  try {
    const pid = parseInt(fs.readFileSync(pidFile, "utf8"), 10);
    if (!(pid > 0)) return false;
    const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
    return cmd.includes("claude-status-bar") || cmd.includes("app/main.py");
  } catch { return false; }
};
const launchApp = () => {
  const child = MAC ? cp.spawn("open", ["-g", "-b", BUNDLE_ID], { stdio: "ignore", detached: true })
                    : cp.spawn("claude-status-bar", [], { stdio: "ignore", detached: true });
  child.on("error", () => {}); // app not installed: a spawn error must not take the hook down
  child.unref();
};
const safeId = (s) => String(s || "").replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 64) || "unknown";

const writeAtomic = (file, obj) => {
  const tmp = file + "." + process.pid + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(obj));
  fs.renameSync(tmp, file);
};

let input = "", done = false;
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => run());
process.stdin.on("error", () => run());
setTimeout(run, 1000); // hooks always pipe stdin, but never hang the session

function run() {
  if (done) return; done = true;
  let id = "", cwd = "";
  try { const j = JSON.parse(input); id = j.session_id; cwd = j.cwd || ""; } catch {}
  id = safeId(id);
  const statePath = path.join(stateDir, id + ".json");

  // Off by default; CLAUDE_STATUSBAR_DEBUG=1 logs lifecycle activity next to update.js's log.
  if (process.env.CLAUDE_STATUSBAR_DEBUG === "1") {
    try {
      fs.appendFileSync(path.join(dir, "hooks.log"),
        `${new Date().toISOString()} [lifecycle:${event}] session=${id} running=${running()} files=${(() => { try { return fs.readdirSync(stateDir).length; } catch { return "?"; } })()}\n`);
    } catch {}
  }

  if (event === "start") {
    // A new session voids a prior explicit Quit (see update.js's self-relaunch suppress).
    try { fs.rmSync(path.join(dir, "quit-intent"), { force: true }); } catch {}
    // If the app isn't running, any leftover session files are stale (e.g. a prior
    // crash) — clear them so the count starts honest.
    if (!running()) { try { for (const f of fs.readdirSync(stateDir)) fs.rmSync(path.join(stateDir, f), { force: true }); } catch {} }
    // Seed an idle file: counts the session immediately, and clears any frozen state from a
    // resume (SessionStart fires on resume with no active turn).
    try {
      // started:false — a merely-opened conversation seeds this for launch + liveness but stays out of
      // the dropdown until it has real activity (update.js flips started:true on a prompt/tool).
      writeAtomic(statePath, { state: "idle", label: "", tool: "", project: cwd ? path.basename(cwd) : "", cwd, sessionId: id, transcript: "", entrypoint: process.env.CLAUDE_CODE_ENTRYPOINT || "", term_program: process.env.TERM_PROGRAM || "", pid: sessionPid(), started: false, startedAt: 0, ts: Math.floor(Date.now() / 1000) });
    } catch {}
    launchApp();
  } else if (event === "end") {
    // Removing the file drops this session from the aggregate — this is also what recovers a
    // frozen animation on force-quit (SessionEnd fires, but no Stop). No state rewrite needed.
    try { fs.rmSync(statePath, { force: true }); } catch {}
  }
  process.exit(0);
}
