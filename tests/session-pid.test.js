// The Linux /proc ancestry walk in hooks/update.js and hooks/lifecycle.js: a state file
// must record the session's `claude` process, not the transient shell Claude Code runs the
// hook command through (recording the shell made the app reap every session within a tick).
// The test builds the real tree: a binary literally named `claude` (a copy of node) spawns
// a shell, the shell runs the hook, and the walk has to skip the shell.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const updatePath = path.resolve(__dirname, "../hooks/update.js");
const lifecyclePath = path.resolve(__dirname, "../hooks/lifecycle.js");

const runUnderFakeClaude = (t, scriptPath, event, sessionId) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "claude status bar pid walk-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const bin = path.join(home, "bin");
  fs.mkdirSync(bin);
  const fakeClaude = path.join(bin, "claude");
  fs.copyFileSync(process.execPath, fakeClaude);
  fs.chmodSync(fakeClaude, 0o755);
  // The quit marker keeps update.js's self-heal from spawning a real app during the test.
  fs.mkdirSync(path.join(home, ".claude", "statusbar"), { recursive: true });
  fs.writeFileSync(path.join(home, ".claude", "statusbar", "quit-intent"), "");

  // fake claude -> /bin/sh -> node <hook>; the trailing `:` keeps sh from exec-replacing
  // itself, so the shell stays a live intermediate hop, as on a real Linux session.
  const inner = `
    const cp = require("node:child_process");
    console.log(process.pid);
    const r = cp.spawnSync("/bin/sh", ["-c", process.env.HOOK_CMD + "; :"], {
      input: JSON.stringify({ session_id: process.env.SID, cwd: "/" }),
    });
    process.exit(r.status ?? 1);
  `;
  const quote = (s) => `'${s.replace(/'/g, `'\\''`)}'`;
  const r = spawnSync(fakeClaude, ["-e", inner], {
    env: {
      HOME: home,
      PATH: bin, // no claude-status-bar on it: a launch attempt must fail quietly
      HOOK_CMD: `${quote(process.execPath)} ${quote(scriptPath)} ${event}`,
      SID: sessionId,
    },
    encoding: "utf8",
  });
  assert.equal(r.status, 0, r.stderr);
  const fakeClaudePid = parseInt(r.stdout, 10);
  const statePath = path.join(home, ".claude", "statusbar", "state.d", `${sessionId}.json`);
  return { fakeClaudePid, state: JSON.parse(fs.readFileSync(statePath, "utf8")) };
};

test("update.js records the claude ancestor's pid, not the hook shell's", { skip: process.platform !== "linux" }, (t) => {
  const { fakeClaudePid, state } = runUnderFakeClaude(t, updatePath, "prompt", "walk-test-update");
  assert.equal(state.pid, fakeClaudePid);
  assert.equal(state.state, "thinking");
});

test("lifecycle.js seeds the claude ancestor's pid too", { skip: process.platform !== "linux" }, (t) => {
  const { fakeClaudePid, state } = runUnderFakeClaude(t, lifecyclePath, "start", "walk-test-lifecycle");
  assert.equal(state.pid, fakeClaudePid);
  assert.equal(state.state, "idle");
});

// Run under Claude Code itself, the test runner's own ancestry contains a real `claude`
// process and the walk would rightly find it; the pid-0 case is only observable elsewhere (CI).
const hasClaudeAncestor = () => {
  let pid = process.ppid;
  for (let i = 0; i < 15 && pid > 1; i++) {
    try {
      if (fs.readFileSync(`/proc/${pid}/comm`, "utf8").trim() === "claude") return true;
      pid = parseInt((fs.readFileSync(`/proc/${pid}/status`, "utf8").match(/^PPid:\s*(\d+)/m) || [])[1], 10);
    } catch { return false; }
  }
  return false;
};

test("a tree with no claude ancestor records pid 0", { skip: process.platform !== "linux" || hasClaudeAncestor() }, (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "claude status bar no walk-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  fs.mkdirSync(path.join(home, ".claude", "statusbar"), { recursive: true });
  fs.writeFileSync(path.join(home, ".claude", "statusbar", "quit-intent"), "");
  const r = spawnSync(process.execPath, [updatePath, "prompt"], {
    env: { HOME: home, PATH: "" },
    input: JSON.stringify({ session_id: "no-claude", cwd: "/" }),
    encoding: "utf8",
  });
  assert.equal(r.status, 0, r.stderr);
  const state = JSON.parse(
    fs.readFileSync(path.join(home, ".claude", "statusbar", "state.d", "no-claude.json"), "utf8"),
  );
  assert.equal(state.pid, 0);
});
