import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_md_for_codex import cli
from claude_md_for_codex import render as r
from claude_md_for_codex import sync as s


class Sandbox(unittest.TestCase):
    """A fake home with ~/.claude, ~/.codex and a code folder; git ignores the real config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.code = self.home / "code"
        self.code.mkdir(parents=True)
        self.env = s.Env(self.home, self.home / ".claude", self.home / ".codex")
        gitconfig = base / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout

    def repo(self, name: str) -> Path:
        path = self.code / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        return path

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class GlobalTest(Sandbox):
    def test_creates_then_is_idempotent(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "Be brief.\n@~/notes/style.md\n")
        self.write(self.home / "notes/style.md", "Use metric units.")
        t = s.plan_global(self.env)
        self.assertEqual(t.action, "create")
        s.apply(t)
        text = (self.env.codex_home / "AGENTS.md").read_text()
        self.assertIn("Be brief.", text)
        self.assertIn("<!-- source: ~/notes/style.md (imported by ~/.claude/CLAUDE.md) -->", text)
        self.assertEqual(s.plan_global(self.env).action, "ok")

    def test_update_and_remove(self):
        src = self.write(self.env.claude_dir / "CLAUDE.md", "one")
        s.apply(s.plan_global(self.env))
        src.write_text("two")
        t = s.plan_global(self.env)
        self.assertEqual(t.action, "update")
        s.apply(t)
        src.unlink()
        t = s.plan_global(self.env)
        self.assertEqual(t.action, "remove")
        s.apply(t)
        self.assertFalse((self.env.codex_home / "AGENTS.md").exists())

    def test_never_overwrites_hand_written_file(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "claude")
        self.write(self.env.codex_home / "AGENTS.md", "my codex notes")
        t = s.plan_global(self.env)
        self.assertEqual(t.action, "blocked")
        code, _, err = self.run_cli("sync")
        self.assertEqual(code, 2)
        self.assertIn("hand-written", err)
        self.assertEqual((self.env.codex_home / "AGENTS.md").read_text(), "my codex notes")

    def test_adopt_moves_hand_written_file_to_extra(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "claude text")
        self.write(self.env.codex_home / "AGENTS.md", "codex only text")
        code, _, _ = self.run_cli("sync", "--adopt")
        self.assertEqual(code, 0)
        text = (self.env.codex_home / "AGENTS.md").read_text()
        self.assertIn("claude text", text)
        self.assertIn("codex only text", text)
        self.assertEqual((self.env.codex_home / "AGENTS.extra.md").read_text(), "codex only text")

    def test_adopt_replaces_a_symlink(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "claude text\n@more.md")
        self.write(self.env.claude_dir / "more.md", "imported")
        self.env.codex_home.mkdir()
        (self.env.codex_home / "AGENTS.md").symlink_to(self.env.claude_dir / "CLAUDE.md")
        self.assertEqual(s.plan_global(self.env).action, "blocked")
        self.assertEqual(self.run_cli("sync", "--adopt")[0], 0)
        self.assertFalse((self.env.codex_home / "AGENTS.md").is_symlink())
        self.assertIn("imported", (self.env.codex_home / "AGENTS.md").read_text())

    def test_user_rules_and_override_warning(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "base")
        self.write(self.env.claude_dir / "rules/py.md", "---\npaths: ['**/*.py']\n---\nType hints.")
        self.write(self.env.codex_home / "AGENTS.override.md", "override")
        t = s.plan_global(self.env)
        self.assertIn("Type hints.", t.content)
        self.assertIn("`**/*.py`", t.content)
        self.assertTrue(any("AGENTS.override.md" in w for w in t.warnings))

    def test_extra_file_survives_a_deleted_claude_md(self):
        src = self.write(self.env.claude_dir / "CLAUDE.md", "claude")
        self.write(self.env.codex_home / "AGENTS.extra.md", "codex only")
        s.apply(s.plan_global(self.env))
        src.unlink()
        t = s.plan_global(self.env)
        self.assertEqual(t.action, "update")
        self.assertIn("codex only", t.content)
        self.assertNotIn("claude", t.content.split("-->", 1)[1])

    def test_nothing_to_do_without_claude_md(self):
        self.assertEqual(s.plan_global(self.env).action, "skip")


class ProjectTest(Sandbox):
    def test_generates_agents_md_and_excludes_it_from_git(self):
        repo = self.repo("app")
        self.write(repo / "CLAUDE.md", "Run `make test`.\n@docs/rules.md")
        self.write(repo / "docs/rules.md", "No force pushes.")
        self.write(repo / "CLAUDE.local.md", "My sandbox URL.")
        plan = s.build_plan(self.env, [self.code], include_global=False)
        [t] = plan.targets
        self.assertEqual(t.action, "create")
        s.apply(t)
        text = (repo / "AGENTS.md").read_text()
        self.assertIn("No force pushes.", text)
        self.assertIn("My sandbox URL.", text)
        self.assertIn("/AGENTS.md", (repo / ".git/info/exclude").read_text())
        self.assertNotIn("AGENTS.md", self.git(repo, "status", "--porcelain"))

    def test_nested_folder_exclude_uses_repo_relative_path(self):
        repo = self.repo("mono")
        self.write(repo / "packages/api/CLAUDE.md", "API rules")
        for t in s.build_plan(self.env, [self.code], include_global=False).pending:
            s.apply(t)
        self.assertIn("/packages/api/AGENTS.md", (repo / ".git/info/exclude").read_text().splitlines())
        self.assertEqual(self.git(repo, "status", "--porcelain", "--untracked-files=all").strip(),
                         "?? packages/api/CLAUDE.md")

    def test_worktree_exclude_goes_to_the_shared_git_dir(self):
        repo = self.repo("wt-main")
        self.write(repo / "README", "x")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-qm", "init")
        wt = self.code / "wt-branch"
        self.git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.write(wt / "CLAUDE.md", "branch rules")
        for t in s.build_plan(self.env, [wt], include_global=False).pending:
            s.apply(t)
        self.assertIn("/AGENTS.md", (repo / ".git/info/exclude").read_text())
        self.assertNotIn("AGENTS.md", self.git(wt, "status", "--porcelain"))

    def test_shared_source_is_left_alone(self):
        repo = self.repo("shared")
        self.write(repo / "AGENTS.md", "team rules")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\n")
        [t] = s.build_plan(self.env, [self.code], include_global=False).targets
        self.assertEqual(t.action, "shared")

    def test_symlinked_claude_md_is_shared(self):
        repo = self.repo("linked")
        self.write(repo / "AGENTS.md", "team rules")
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        [t] = s.build_plan(self.env, [self.code], include_global=False).targets
        self.assertEqual(t.action, "shared")

    def test_drift_is_reported_not_fixed(self):
        repo = self.repo("drift")
        self.write(repo / "AGENTS.md", "team rules")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\nAlso: deploy on Fridays never.")
        self.write(repo / "CLAUDE.local.md", "personal")
        [t] = s.build_plan(self.env, [self.code], include_global=False).targets
        self.assertEqual(t.action, "drift")
        self.assertIn("CLAUDE.md", t.detail)
        self.assertIn("CLAUDE.local.md", t.detail)
        self.assertFalse(t.pending)
        self.assertEqual((repo / "AGENTS.md").read_text(), "team rules")

    def test_orphaned_generated_file_is_removed(self):
        repo = self.repo("orphan")
        src = self.write(repo / "CLAUDE.md", "rules")
        for t in s.build_plan(self.env, [self.code], include_global=False).pending:
            s.apply(t)
        src.unlink()
        [t] = s.build_plan(self.env, [self.code], include_global=False).targets
        self.assertEqual(t.action, "remove")
        s.apply(t)
        self.assertFalse((repo / "AGENTS.md").exists())

    def test_project_imports_outside_the_repo_are_skipped(self):
        self.write(self.home / ".ssh/config", "Host secret")
        repo = self.repo("evil")
        self.write(repo / "CLAUDE.md", "rules\n@~/.ssh/config")
        [t] = s.build_plan(self.env, [self.code], include_global=False).targets
        self.assertNotIn("Host secret", t.content)
        self.assertTrue(any("outside the project" in w for w in t.warnings))
        [t] = s.build_plan(self.env, [self.code], include_global=False, allow_external=True).targets
        self.assertIn("Host secret", t.content)

    def test_skips_dependencies_hidden_folders_and_home(self):
        repo = self.repo("deps")
        self.write(repo / "node_modules/pkg/CLAUDE.md", "dependency")
        self.write(repo / ".cache/CLAUDE.md", "hidden")
        self.write(self.env.claude_dir / "CLAUDE.md", "global")
        folders = s.find_folders(self.home, 8, self.home)
        self.assertEqual(folders, [])

    def test_size_warning(self):
        repo = self.repo("big")
        self.write(repo / "CLAUDE.md", "x" * 100)
        [t] = s.build_plan(self.env, [self.code], include_global=False, max_bytes=50).targets
        self.assertTrue(any("project_doc_max_bytes" in w for w in t.warnings))


class CliTest(Sandbox):
    def test_status_and_sync_round_trip(self):
        repo = self.repo("cli")
        self.write(repo / "CLAUDE.md", "cli rules")
        self.write(self.env.claude_dir / "CLAUDE.md", "global rules")
        code, out, _ = self.run_cli("status", "--root", str(self.code))
        self.assertEqual(code, 1)
        self.assertIn("missing", out)
        code, out, _ = self.run_cli("sync", "--dry-run", "--root", str(self.code))
        self.assertEqual(code, 1)
        self.assertFalse((repo / "AGENTS.md").exists())
        code, out, _ = self.run_cli("sync", "--root", str(self.code))
        self.assertEqual(code, 0)
        self.assertIn("created ~/code/cli/AGENTS.md", out)
        self.assertIn("created ~/.codex/AGENTS.md", out)
        code, out, _ = self.run_cli("status", "--root", str(self.code))
        self.assertEqual(code, 0, out)

    def test_render_prints_without_writing(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "global rules")
        code, out, _ = self.run_cli("render")
        self.assertEqual(code, 0)
        self.assertIn("global rules", out)
        self.assertFalse((self.env.codex_home / "AGENTS.md").exists())

    def test_roots_come_from_config(self):
        repo = self.repo("cfg")
        self.write(repo / "CLAUDE.md", "cfg rules")
        cli.save_config({"roots": [str(self.code)], "every": "1h"})
        self.assertEqual(cli.load_config()["roots"], [str(self.code)])
        self.run_cli("sync")
        self.assertTrue(r.is_generated((repo / "AGENTS.md").read_text()))

    def test_codex_max_bytes_from_codex_config(self):
        self.write(self.env.codex_home / "config.toml", "project_doc_max_bytes = 65536\n")
        self.assertEqual(cli.codex_max_bytes(s.Env.from_os()), 65536)

    def test_purge_removes_only_generated_files(self):
        repo = self.repo("purge")
        self.write(repo / "CLAUDE.md", "rules")
        other = self.repo("handwritten")
        self.write(other / "AGENTS.md", "keep me")
        cli.save_config({"roots": [str(self.code)]})
        self.run_cli("sync")
        with mock.patch("claude_md_for_codex.schedule.uninstall", return_value=[]):
            self.run_cli("uninstall", "--purge")
        self.assertFalse((repo / "AGENTS.md").exists())
        self.assertNotIn("/AGENTS.md", (repo / ".git/info/exclude").read_text())
        self.assertEqual((other / "AGENTS.md").read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
