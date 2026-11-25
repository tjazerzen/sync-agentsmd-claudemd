import plistlib
import unittest
from pathlib import Path

from claude_md_for_codex import schedule


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
        units = schedule.systemd_units(["/opt/my tools/claude-md-for-codex"], 900,
                                       [Path("/h/.claude/CLAUDE.md")], {"CODEX_HOME": "/h/codex"})
        service = units["claude-md-for-codex.service"]
        self.assertIn('ExecStart="/opt/my tools/claude-md-for-codex" sync --quiet', service)
        self.assertIn("Environment=CODEX_HOME=/h/codex", service)
        self.assertIn("OnUnitActiveSec=900s", units["claude-md-for-codex.timer"])
        self.assertIn("PathChanged=/h/.claude/CLAUDE.md", units["claude-md-for-codex.path"])
        self.assertIn("OnCalendar=daily", schedule.systemd_units(["x"], 86400, [], {})["claude-md-for-codex.timer"])

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
