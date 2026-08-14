"""Headless tests for the Linux app's session-state logic (linux/app/core.py) and the
PIL icon pipeline (no GTK, no display). Run: python3 -m unittest discover linux/tests"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="csb-test-cache-")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="csb-test-config-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import core
import main as app_main


def sess(**kw):
    s = core.Session()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class EffectiveState(unittest.TestCase):
    def test_working_fresh_stays(self):
        s = sess(state="thinking", ts=1000)
        self.assertEqual(core.effective_state(s, now=1100, turn_line_reader=lambda p: None), "thinking")

    def test_working_past_cap_collapses(self):
        s = sess(state="tool", ts=1000)
        self.assertEqual(core.effective_state(s, now=1000 + 901, turn_line_reader=lambda p: None), "idle")

    def test_permission_has_longer_cap(self):
        s = sess(state="permission", ts=1000)
        self.assertEqual(core.effective_state(s, now=1000 + 7100, turn_line_reader=lambda p: None), "permission")
        self.assertEqual(core.effective_state(s, now=1000 + 7201, turn_line_reader=lambda p: None), "idle")

    def test_done_collapses_to_idle(self):
        self.assertEqual(core.effective_state(sess(state="done", ts=1000), now=1001), "idle")

    def test_interrupt_marker_collapses(self):
        s = sess(state="thinking", ts=1000, transcript="/t")
        reader = lambda p: '{"type":"user","content":"[Request interrupted by user]"}'
        self.assertEqual(core.effective_state(s, now=1001, turn_line_reader=reader), "idle")


class LastTurnLine(unittest.TestCase):
    def test_skips_bookkeeping_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"assistant","content":"interrupted by user"}\n')
            f.write('{"type":"system","subtype":"away_summary"}\n')
            f.write('{"type":"last-prompt"}\n')
            path = f.name
        self.addCleanup(os.unlink, path)
        line = core.last_turn_line(path)
        self.assertIn('"type":"assistant"', line)
        self.assertIn("interrupted by user", line)


class Reaping(unittest.TestCase):
    def test_live_pid_stays(self):
        s = sess(pid=os.getpid(), eff="idle", ts=0)
        self.assertFalse(core.should_reap(s, now=1e12))

    def test_dead_pid_reaps_even_when_working(self):
        s = sess(pid=2 ** 22 + 12345, eff="thinking", ts=1e12)
        self.assertTrue(core.should_reap(s, now=1e12, alive=lambda pid: False))

    def test_pidless_falls_back_to_idle_age(self):
        s = sess(pid=0, eff="idle", ts=1000)
        self.assertTrue(core.should_reap(s, now=1000 + 901, stale_prune_age=900))
        self.assertFalse(core.should_reap(s, now=1000 + 100, stale_prune_age=900))
        s.eff = "thinking"
        self.assertFalse(core.should_reap(s, now=1000 + 901, stale_prune_age=900))


class Lead(unittest.TestCase):
    def test_permission_beats_working_beats_idle(self):
        idle = sess(id="a", eff="idle", ts=300)
        working = sess(id="b", eff="thinking", ts=200)
        perm = sess(id="c", eff="permission", ts=100)
        self.assertEqual(core.pick_lead([idle, working, perm]).id, "c")
        self.assertEqual(core.pick_lead([idle, working]).id, "b")
        self.assertEqual(core.pick_lead([idle]).id, "a")
        self.assertIsNone(core.pick_lead([]))

    def test_recency_breaks_ties(self):
        a = sess(id="a", eff="thinking", ts=100)
        b = sess(id="b", eff="tool", ts=200)
        self.assertEqual(core.pick_lead([a, b]).id, "b")


class DisplayNames(unittest.TestCase):
    def test_collision_gets_parent_qualifier(self):
        a = sess(project="repo", cwd="/home/u/work/repo")
        b = sess(project="repo", cwd="/home/u/tmp/repo")
        c = sess(project="other", cwd="/home/u/other")
        core.assign_display_names([a, b, c])
        self.assertEqual(a.display_name, "work/repo")
        self.assertEqual(b.display_name, "tmp/repo")
        self.assertEqual(c.display_name, "other")

    def test_empty_cwd_never_forces_qualifier(self):
        a = sess(project="repo", cwd="/home/u/work/repo")
        b = sess(project="repo", cwd="")
        core.assign_display_names([a, b])
        self.assertEqual(a.display_name, "repo")


class Branch(unittest.TestCase):
    def test_branch_detached_worktree_and_nongit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/fix-auth\n")
            self.assertEqual(core.branch_for_cwd(str(repo)), "fix-auth")
            sub = repo / "deep" / "er"
            sub.mkdir(parents=True)
            self.assertEqual(core.branch_for_cwd(str(sub)), "fix-auth")

            (repo / ".git" / "HEAD").write_text("a" * 40 + "\n")
            self.assertEqual(core.branch_for_cwd(str(repo)), "a" * 7)

            wt = Path(td) / "wt"
            wt.mkdir()
            gitdir = Path(td) / "gitdir"
            gitdir.mkdir()
            (gitdir / "HEAD").write_text("ref: refs/heads/wt-branch\n")
            (wt / ".git").write_text(f"gitdir: {gitdir}\n")
            self.assertEqual(core.branch_for_cwd(str(wt)), "wt-branch")

            plain = Path(td) / "plain"
            plain.mkdir()
            self.assertEqual(core.branch_for_cwd(str(plain)), "")


class Formatting(unittest.TestCase):
    def test_elapsed(self):
        self.assertEqual(core.elapsed(43), "43s")
        self.assertEqual(core.elapsed(61), "1m 1s")
        self.assertEqual(core.elapsed(-5), "0s")

    def test_truncated(self):
        self.assertEqual(core.truncated("short"), "short")
        self.assertEqual(core.truncated("x" * 25, 20, 18), "x" * 18 + "…")

    def test_version_newer(self):
        self.assertTrue(core.version_newer("0.0.10", "0.0.9"))
        self.assertFalse(core.version_newer("0.4.4", "0.4.4"))
        self.assertTrue(core.version_newer("0.5", "0.4.4"))

    def test_status_text(self):
        self.assertEqual(core.status_text(sess(state="permission"), "permission", True, None),
                         "Awaiting permission")
        self.assertEqual(core.status_text(sess(state="thinking"), "thinking", True, "Percolating"),
                         "Percolating…")
        self.assertEqual(core.status_text(sess(state="tool", label="Editing"), "tool", True, None),
                         "Editing")
        self.assertEqual(core.status_text(sess(state="done"), "idle", True, None), "Done")

    def test_pick_thinking_word_avoids_repeat(self):
        for _ in range(50):
            self.assertEqual(core.pick_thinking_word(["a", "b"], previous="a"), "b")

    def test_bar_label_counts_working_sessions(self):
        self.assertEqual(core.bar_label("Percolating…", 1, True), "Percolating…")
        self.assertEqual(core.bar_label("Percolating…", 3, True), "Percolating…  ×3")
        self.assertEqual(core.bar_label("Percolating…", 3, True, "1m 2s"), "Percolating…  ×3  1m 2s")
        # A permission lead keeps its message unambiguous: no count suffix.
        self.assertEqual(core.bar_label("Awaiting permission", 2, False), "Awaiting permission")
        self.assertEqual(core.bar_label("", 0, False), "")


class Extras(unittest.TestCase):
    """The Linux-side policy functions: what to notify, which ink Auto picks, and the
    waybar payload."""

    def test_waybar_payload(self):
        self.assertEqual(core.waybar_payload([], now=1000),
                         {"text": "", "class": "idle", "tooltip": ""})
        idle = sess(id="a", eff="idle", state="idle", project="quiet", ts=100)
        self.assertEqual(core.waybar_payload([idle], now=1000)["text"], "❯")
        work = sess(id="b", eff="tool", state="tool", label="Editing", project="repo",
                    started_at=940, ts=990)
        p = core.waybar_payload([idle, work], now=1000)
        self.assertEqual(p["text"], "✳ Editing  1m 0s")
        self.assertEqual(p["class"], "working")
        self.assertIn("✳ repo  1m 0s", p["tooltip"])
        self.assertIn("❯ quiet", p["tooltip"])
        perm = sess(id="c", eff="permission", state="permission", project="ask", ts=995)
        p = core.waybar_payload([idle, work, perm], now=1000)
        self.assertEqual(p["text"], "⚠ Awaiting permission")
        self.assertEqual(p["class"], "permission")

    def test_terminal_for_pid(self):
        tree = {100: (99, "claude"), 99: (98, "bash"), 98: (97, "gnome-terminal-"), 97: (1, "systemd")}
        comm, ppid = (lambda p: tree[p][1]), (lambda p: tree[p][0])
        self.assertEqual(core.terminal_for_pid(100, comm, ppid)["desktop"], "org.gnome.Terminal.desktop")
        # Chain ending at init with no terminal met: no route.
        bare = {100: (1, "claude")}
        self.assertIsNone(core.terminal_for_pid(100, lambda p: bare[p][1], lambda p: bare[p][0]))
        # A vanished /proc entry mid-walk reads as no route, not an error.
        self.assertIsNone(core.terminal_for_pid(100, comm, lambda p: 12345))

    def test_auto_icon_color(self):
        self.assertEqual(core.auto_icon_color("ubuntu:GNOME", "'default'"), "white")
        self.assertEqual(core.auto_icon_color("KDE", "'prefer-dark'"), "white")
        self.assertEqual(core.auto_icon_color("KDE", "prefer-dark"), "white")
        self.assertEqual(core.auto_icon_color("KDE", "'default'"), "black")
        self.assertEqual(core.auto_icon_color("", ""), "black")

    def test_notify_plan(self):
        self.assertEqual(core.notify_plan("off", [("proj", 500)]), [])
        self.assertEqual(core.notify_plan("done", [("proj", 90), ("quick", 5)]),
                         [("proj finished", "The turn ran 1m 30s")])  # short turns stay quiet
        # "all" is a value the retired multi-mode setting could have saved; it included
        # turn end, so it still counts as on.
        self.assertEqual(core.notify_plan("all", [("proj", 90)]),
                         [("proj finished", "The turn ran 1m 30s")])
        self.assertEqual(core.notify_plan("permission", [("proj", 90)]), [])  # retired value


class ParseSession(unittest.TestCase):
    def test_defaults_and_types(self):
        s = core.parse_session({}, "x")
        self.assertEqual((s.state, s.pid, s.started, s.ts), ("idle", 0, False, 0.0))
        s = core.parse_session({"state": "tool", "pid": 4242, "started": True,
                               "startedAt": 1000, "ts": 1001.5}, "y")
        self.assertEqual((s.state, s.pid, s.started_at, s.ts), ("tool", 4242, 1000.0, 1001.5))
        self.assertEqual(core.parse_session({"pid": "bogus"}, "z").pid, 0)


class Icons(unittest.TestCase):
    def test_render_all_styles_and_colors(self):
        icons = app_main.IconSet("test")
        self.assertEqual(len(icons.ensure("web", "orange")), 8)
        self.assertEqual(len(icons.ensure("code", "white")), 30)
        self.assertEqual(len(icons.ensure("crab", "black")), 20)
        for name in (icons.resting("web", "orange"), icons.resting("crab", "orange"), icons.dot()):
            self.assertTrue((icons.dir / f"{name}.png").exists(), name)

    def test_custom_gif_becomes_an_animation(self):
        from PIL import Image
        app_main.ANIM_DIR.mkdir(parents=True, exist_ok=True)
        colors = [(255, 0, 0, 255), (0, 255, 0, 255), (20, 20, 20, 255)]
        frames = [Image.new("RGBA", (20, 20), c) for c in colors]
        frames[0].save(app_main.ANIM_DIR / "pet.gif", save_all=True,
                       append_images=frames[1:], duration=80, loop=0)
        icons = app_main.IconSet("test-gif")
        self.assertIn("pet", icons.custom)
        names = icons.ensure("gif:pet", "orange")
        self.assertEqual(len(names), 3)
        self.assertTrue(all((icons.dir / f"{n}.png").exists() for n in names))
        self.assertAlmostEqual(icons.fps("gif:pet"), 12.5, delta=0.1)
        self.assertEqual(icons.resting("gif:pet", "white"), icons.ensure("gif:pet", "white")[0])

    def test_crab_template_eyes_become_holes(self):
        from PIL import Image
        icons = app_main.IconSet("test")
        icons.ensure("crab", "white")
        orange = Image.open(icons.assets / "crab-0.png").convert("RGBA")
        white = Image.open(icons.dir / "csb-crab-white-0.png").convert("RGBA")
        # The template transform turns dark opaque source pixels into holes (alpha 0),
        # so the crab's eyes read as negative space.
        src, out = orange.load(), white.load()
        holes = sum(
            1
            for y in range(orange.size[1])
            for x in range(orange.size[0])
            if src[x, y][3] > 200 and out[x, y][3] == 0
        )
        self.assertGreater(holes, 0)


if __name__ == "__main__":
    unittest.main()
