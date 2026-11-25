import plistlib
import unittest
from pathlib import Path

from sync_agentsmd_claudemd import schedule


class ScheduleTest(unittest.TestCase):
    def test_parse_every(self):
        self.assertEqual(schedule.parse_every("15m"), 900)
        self.assertEqual(schedule.parse_every("15min"), 900)
        self.assertEqual(schedule.parse_every("2h"), 7200)
        self.assertEqual(schedule.parse_every("daily"), 86400)
        self.assertEqual(schedule.parse_every("hourly"), 3600)
        for bad in ("soon", "0m", "30s", "-5m"):
            with self.assertRaises(ValueError):
                schedule.parse_every(bad)

    def test_systemd_units(self):
        units = schedule.systemd_units(["/opt/my tools/sync-agentsmd-claudemd"], 900,
                                       [Path("/h/.claude/CLAUDE.md")], {"CODEX_HOME": "/h/codex"})
        service = units["sync-agentsmd-claudemd.service"]
        self.assertIn('ExecStart="/opt/my tools/sync-agentsmd-claudemd" sync --quiet', service)
        self.assertIn("Environment=CODEX_HOME=/h/codex", service)
        self.assertIn("OnUnitActiveSec=900s", units["sync-agentsmd-claudemd.timer"])
        self.assertIn("PathChanged=/h/.claude/CLAUDE.md", units["sync-agentsmd-claudemd.path"])
        self.assertIn("OnCalendar=daily", schedule.systemd_units(["x"], 86400, [], {})["sync-agentsmd-claudemd.timer"])

    def test_uninstall_removes_units_under_the_old_name_too(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
                mock.patch("subprocess.run") as run:
            unit_dir = Path(tmp) / "systemd/user"
            unit_dir.mkdir(parents=True)
            for name in ("claude-md-for-codex", "sync-agentsmd-claudemd"):
                for ext in ("service", "timer", "path"):
                    (unit_dir / f"{name}.{ext}").write_text("x")
            notes = schedule.uninstall_systemd()
            self.assertEqual(len(notes), 2)
            self.assertEqual(list(unit_dir.iterdir()), [])
            self.assertTrue(run.called)

    def test_launchd_plist(self):
        data = plistlib.loads(schedule.launchd_plist(["/bin/tool"], 3600, [Path("/h/.claude/CLAUDE.md")], {},
                                                     Path("/h/log")))
        self.assertEqual(data["ProgramArguments"], ["/bin/tool", "sync", "--quiet"])
        self.assertEqual(data["StartInterval"], 3600)
        self.assertEqual(data["WatchPaths"], ["/h/.claude/CLAUDE.md"])

    def test_cron_line(self):
        self.assertTrue(schedule.cron_line(["/bin/tool"], 900, {}).startswith("*/15 * * * * /bin/tool sync --quiet #"))
        self.assertTrue(schedule.cron_line(["/bin/tool"], 7200, {}).startswith("7 */2 * * *"))
        self.assertTrue(schedule.cron_line(["/bin/tool"], 86400, {}).startswith("17 4 * * *"))


if __name__ == "__main__":
    unittest.main()
