#!/usr/bin/env node

const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const home = os.homedir();
// Match the dir, not "update.js": the narrower marker used to orphan the lifecycle hooks.
const MARKER = path.join(home, ".claude", "statusbar");
const shellQuote = (value) => `'${value.replace(/'/g, `'\\''`)}'`;
const quotedMarkerPrefix = shellQuote(MARKER).slice(0, -1);
const isOurs = (command) =>
  command.includes(MARKER) || command.includes(quotedMarkerPrefix);
// Same CLAUDE_CONFIG_DIR resolution as install.js: strip our hooks from the settings.json
// Claude Code actually reads.
const configDir = process.env.CLAUDE_CONFIG_DIR || path.join(home, ".claude");
const settingsPath = path.join(configDir, "settings.json");

// Tear down the desktop watcher LaunchAgent (best-effort; safe if absent).
const AGENT_LABEL = "com.local.claudestatusbar.watcher";
const agentPlist = path.join(home, "Library", "LaunchAgents", AGENT_LABEL + ".plist");
try { cp.execSync(`launchctl bootout gui/${process.getuid()}/${AGENT_LABEL}`, { stdio: "ignore" }); } catch {}
if (process.platform === "darwin") {
  try { cp.execSync("pkill -x ClaudeStatusBar", { stdio: "ignore" }); } catch {}
} else {
  // Stop the Linux app via its pid file, but only after confirming the pid still IS the app
  // (pid reuse must never kill an innocent process).
  try {
    const pid = parseInt(fs.readFileSync(path.join(MARKER, "app.pid"), "utf8"), 10);
    const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
    if (pid > 0 && (cmdline.includes("claude-status-bar") || cmdline.includes("main.py"))) {
      process.kill(pid, "SIGTERM");
    }
  } catch {}
}

if (!fs.existsSync(settingsPath)) { console.log("No settings.json; nothing to do."); process.exit(0); }

const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
for (const evt of Object.keys(settings.hooks || {})) {
  settings.hooks[evt] = (settings.hooks[evt] || [])
    .map((e) => ({ ...e, hooks: (e.hooks || []).filter((h) => !isOurs(h.command || "")) }))
    .filter((e) => (e.hooks || []).length > 0);
  if (settings.hooks[evt].length === 0) delete settings.hooks[evt];
}
fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
console.log("Removed status-bar hooks from", settingsPath);
