import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sync_agentsmd_claudemd import cli
from sync_agentsmd_claudemd import sync as s
from sync_agentsmd_claudemd.state import State

FAKE_MERGER = """
import os, sys, pathlib
prompt = sys.stdin.read()
log = pathlib.Path(os.environ["FAKE_MERGER_LOG"])
log.write_text(log.read_text() + prompt + "\\n=====\\n" if log.exists() else prompt + "\\n=====\\n")
mode = os.environ.get("FAKE_MERGER_MODE", "ok")
if mode == "fail":
    sys.exit(3)
if mode == "bad":
    print("I would rather not.")
    sys.exit(0)
print("DECISION: merged - kept both rules")
print("CLAUDE_MD: " + os.environ.get("FAKE_CLAUDE_MD", "copy"))
print("<<<MERGED")
print(os.environ.get("FAKE_MERGED", "# Rules\\nmerged rule one\\nmerged rule two"))
print("MERGED>>>")
"""


class Sandbox(unittest.TestCase):
    """A fake home with ~/.claude, ~/.codex and ~/code; git and the state dir are isolated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.code = self.home / "code"
        self.code.mkdir(parents=True)
        self.env = s.Env(self.home, self.home / ".claude", self.home / ".codex")
        gitconfig = self.base / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
        script = self.base / "fake_merger.py"
        script.write_text(FAKE_MERGER)
        self.merger_log = self.base / "merger.log"
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_MERGER_LOG": str(self.merger_log),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "FAKE_MERGER_MODE", "FAKE_MERGED", "FAKE_CLAUDE_MD"):
            os.environ.pop(var, None)
        self.merger = f"{sys.executable} {script}"

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

    def commit(self, repo: Path, message: str) -> None:
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", message)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def sync(self, *extra: str) -> tuple[int, str, str]:
        return self.run_cli("sync", "--root", str(self.code), "--no-global", "--merger", self.merger, *extra)

    def merges_run(self) -> int:
        return self.merger_log.read_text().count("\n=====\n") if self.merger_log.exists() else 0

    def case(self, folder: Path) -> tuple[str, str]:
        plan = s.build_plan(self.env, [self.code], State(), include_global=False)
        [a] = [a for a in plan if a.pair.folder == folder]
        return a.case, a.action


class CopyTest(Sandbox):
    def test_only_claude_md_is_copied_to_agents_md_and_kept_out_of_git(self):
        repo = self.repo("app")
        self.write(repo / "CLAUDE.md", "Run make test.\n")
        self.commit(repo, "add CLAUDE.md")
        self.assertEqual(self.case(repo), ("claude-only", "create-agents"))
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual((repo / "AGENTS.md").read_text(), "Run make test.\n")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        self.assertEqual(self.case(repo), ("same", "none"))

    def test_only_agents_md_is_copied_to_claude_md(self):
        repo = self.repo("lib")
        self.write(repo / "AGENTS.md", "Use tabs.\n")
        self.sync()
        self.assertEqual((repo / "CLAUDE.md").read_text(), "Use tabs.\n")
        self.assertIn("/CLAUDE.md", (repo / ".git/info/exclude").read_text().splitlines())

    def test_imports_are_inlined_for_codex(self):
        repo = self.repo("imp")
        self.write(repo / "CLAUDE.md", "Main\n@docs/rules.md\n")
        self.write(repo / "docs/rules.md", "No force pushes.")
        self.sync()
        agents = (repo / "AGENTS.md").read_text()
        self.assertIn("Main\n@docs/rules.md", agents)
        self.assertIn("imported from docs/rules.md -->\nNo force pushes.", agents)
        self.assertEqual(self.case(repo), ("same", "none"))
        (repo / "docs/rules.md").write_text("Force pushes only to your own branches.")
        self.assertEqual(self.case(repo), ("claude-changed", "update-agents"))
        self.sync()
        self.assertIn("only to your own branches", (repo / "AGENTS.md").read_text())

    def test_after_a_sync_an_edit_on_either_side_is_copied_without_a_merge(self):
        repo = self.repo("edits")
        self.write(repo / "CLAUDE.md", "v1\n")
        self.sync()
        (repo / "CLAUDE.md").write_text("v2 from claude\n")
        self.assertEqual(self.case(repo), ("claude-changed", "update-agents"))
        self.sync()
        self.assertEqual((repo / "AGENTS.md").read_text(), "v2 from claude\n")
        (repo / "AGENTS.md").write_text("v3 from codex\n")
        self.assertEqual(self.case(repo), ("agents-changed", "update-claude"))
        self.sync()
        self.assertEqual((repo / "CLAUDE.md").read_text(), "v3 from codex\n")
        self.assertEqual(self.merges_run(), 0)
        backups = list((self.home / ".local/state/sync-agentsmd-claudemd/backups").rglob("*.md"))
        self.assertEqual(len(backups), 2)

    def test_writes_go_through_symlinks(self):
        repo = self.repo("linkedfile")
        shared = self.write(self.base / "dotfiles/claude.md", "v1\n")
        (repo / "CLAUDE.md").symlink_to(shared)
        self.sync()
        (repo / "AGENTS.md").write_text("v2\n")
        self.sync()
        self.assertTrue((repo / "CLAUDE.md").is_symlink())
        self.assertEqual(shared.read_text(), "v2\n")


class InSyncTest(Sandbox):
    def test_claude_md_importing_agents_md_is_in_sync(self):
        repo = self.repo("shared")
        self.write(repo / "AGENTS.md", "team rules")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\n")
        self.assertEqual(self.case(repo), ("shared", "none"))

    def test_symlinked_pair_is_in_sync(self):
        repo = self.repo("linked")
        self.write(repo / "AGENTS.md", "team rules")
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.assertEqual(self.case(repo), ("linked", "none"))

    def test_identical_files_are_in_sync(self):
        repo = self.repo("same")
        self.write(repo / "AGENTS.md", "rules\n")
        self.write(repo / "CLAUDE.md", "rules\n\n")
        self.assertEqual(self.case(repo), ("same", "none"))


class MergeTest(Sandbox):
    def test_never_synced_and_different_files_are_merged_using_git_history(self):
        repo = self.repo("diverged")
        self.write(repo / "CLAUDE.md", "# Rules\nclaude rule\n")
        self.write(repo / "AGENTS.md", "# Rules\nagents rule\n")
        self.commit(repo, "add both instruction files")
        self.assertEqual(self.case(repo), ("unsynced", "merge"))
        code, out, _ = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn("merged - kept both rules", out)
        self.assertIn(f"merged with {os.path.basename(sys.executable)}: merged", out)
        merged = "# Rules\nmerged rule one\nmerged rule two\n"
        self.assertEqual((repo / "CLAUDE.md").read_text(), merged)
        self.assertEqual((repo / "AGENTS.md").read_text(), merged)
        prompt = self.merger_log.read_text()
        self.assertIn("add both instruction files", prompt)
        self.assertIn("claude rule", prompt)
        self.assertIn("agents rule", prompt)
        self.assertEqual(self.case(repo), ("same", "none"))
        records = list((self.home / ".local/state/sync-agentsmd-claudemd/merges").glob("*.json"))
        self.assertTrue(json.loads(records[0].read_text())["ok"])

    def test_merger_can_make_claude_md_import_agents_md_like_the_default_branch(self):
        origin = self.base / "origin.git"
        self.git(self.base, "init", "-q", "--bare", str(origin))
        repo = self.repo("stale")
        self.write(repo / "AGENTS.md", "# Rules\nnew rule\n")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\n")
        self.commit(repo, "CLAUDE.md imports AGENTS.md")
        self.git(repo, "remote", "add", "origin", str(origin))
        self.git(repo, "push", "-q", "origin", "main")
        self.git(repo, "fetch", "-q", "origin")
        self.git(repo, "checkout", "-q", "-b", "old", "HEAD")
        self.write(repo / "CLAUDE.md", "# Rules\nold rule\n")
        self.commit(repo, "old full CLAUDE.md")
        self.git(repo, "reset", "-q", "--soft", "HEAD~1")
        os.environ["FAKE_CLAUDE_MD"] = "import"
        self.assertEqual(self.case(repo), ("unsynced", "merge"))
        self.sync()
        prompt = self.merger_log.read_text()
        self.assertIn("on origin/main, CLAUDE.md is only the line `@AGENTS.md`", prompt)
        self.assertIn("fully merged into origin/main", prompt)
        self.assertEqual((repo / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertEqual((repo / "AGENTS.md").read_text(), "# Rules\nmerged rule one\nmerged rule two\n")
        self.assertEqual(self.case(repo), ("shared", "none"))

    def test_both_changed_since_the_last_sync_is_merged(self):
        repo = self.repo("both")
        self.write(repo / "CLAUDE.md", "# Rules\nbase\n")
        self.sync()
        (repo / "CLAUDE.md").write_text("# Rules\nbase\nclaude edit\n")
        (repo / "AGENTS.md").write_text("# Rules\nbase\ncodex edit\n")
        self.assertEqual(self.case(repo), ("both-changed", "merge"))

    def test_claude_md_importing_agents_md_plus_extra_text(self):
        repo = self.repo("stub")
        self.write(repo / "AGENTS.md", "# Rules\nteam rule\n")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\nClaude-only extra rule\n")
        self.assertEqual(self.case(repo), ("stub-extra", "merge"))
        self.sync()
        self.assertEqual((repo / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertEqual((repo / "AGENTS.md").read_text(), "# Rules\nmerged rule one\nmerged rule two\n")
        self.assertIn("CLAUDE.md imports AGENTS.md", self.merger_log.read_text())

    def test_edit_to_generated_agents_md_folds_back_into_claude_md_with_imports(self):
        repo = self.repo("fold")
        self.write(repo / "CLAUDE.md", "# Rules\nmain\n@docs/x.md\n")
        self.write(repo / "docs/x.md", "imported rule")
        self.sync()
        (repo / "AGENTS.md").write_text((repo / "AGENTS.md").read_text() + "\ncodex added this\n")
        self.assertEqual(self.case(repo), ("agents-changed", "merge"))
        os.environ["FAKE_MERGED"] = "# Rules\nmain\ncodex added this\n@docs/x.md"
        self.sync()
        self.assertEqual((repo / "CLAUDE.md").read_text(), "# Rules\nmain\ncodex added this\n@docs/x.md\n")
        self.assertIn("imported from docs/x.md -->\nimported rule", (repo / "AGENTS.md").read_text())
        self.assertIn("Lines like `@path/to/file`", self.merger_log.read_text())

    def test_identical_pairs_in_other_checkouts_reuse_the_merge(self):
        for name in ("wt1", "wt2", "wt3"):
            repo = self.repo(name)
            self.write(repo / "CLAUDE.md", "# Rules\nold\n")
            self.write(repo / "AGENTS.md", "# Rules\nnew\n")
        code, out, _ = self.sync()
        self.assertEqual(self.merges_run(), 1)
        self.assertEqual(out.count("reused an earlier merge"), 2)

    def test_failed_merge_changes_nothing_and_is_not_retried_today(self):
        repo = self.repo("bad")
        self.write(repo / "CLAUDE.md", "# Rules\nold claude\n")
        self.write(repo / "AGENTS.md", "# Rules\nold agents\n")
        os.environ["FAKE_MERGER_MODE"] = "bad"
        code, _, err = self.sync()
        self.assertEqual(code, 2)
        self.assertIn("not in the expected format", err)
        self.assertEqual((repo / "CLAUDE.md").read_text(), "# Rules\nold claude\n")
        os.environ["FAKE_MERGER_MODE"] = "ok"
        _, _, err = self.sync()
        self.assertIn("failed within the last day", err)
        self.assertEqual(self.merges_run(), 1)

    def test_merge_budget_and_no_merge(self):
        repo = self.repo("budget")
        self.write(repo / "CLAUDE.md", "# Rules\na\n")
        self.write(repo / "AGENTS.md", "# Rules\nb\n")
        _, out, _ = self.sync("--no-merge")
        self.assertIn("merge postponed", out)
        self.assertEqual(self.merges_run(), 0)

    def test_suspiciously_short_merge_is_rejected(self):
        repo = self.repo("short")
        self.write(repo / "CLAUDE.md", "# Rules\n" + "important rule\n" * 40)
        self.write(repo / "AGENTS.md", "# Rules\n" + "other rule\n" * 40)
        os.environ["FAKE_MERGED"] = "tiny"
        code, _, err = self.sync()
        self.assertEqual(code, 2)
        self.assertIn("far shorter", err)


class SafetyTest(Sandbox):
    def test_conflict_markers_block_the_pair(self):
        repo = self.repo("conflict")
        self.write(repo / "CLAUDE.md", "a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n")
        self.write(repo / "AGENTS.md", "b")
        self.assertEqual(self.case(repo), ("conflict", "blocked"))

    def test_git_operation_in_progress_is_skipped(self):
        repo = self.repo("rebasing")
        self.write(repo / "CLAUDE.md", "rules")
        (repo / ".git/MERGE_HEAD").write_text("0" * 40)
        code, _, err = self.sync()
        self.assertEqual(code, 2)
        self.assertIn("git merge is in progress", err)
        self.assertFalse((repo / "AGENTS.md").exists())

    def test_project_imports_outside_the_repo_are_skipped(self):
        self.write(self.home / ".ssh/config", "Host secret")
        repo = self.repo("evil")
        self.write(repo / "CLAUDE.md", "rules\n@~/.ssh/config")
        self.sync()
        self.assertNotIn("Host secret", (repo / "AGENTS.md").read_text())

    def test_legacy_generated_files_are_replaced_or_removed(self):
        repo = self.repo("legacy")
        self.write(repo / "CLAUDE.md", "rules\n")
        self.write(repo / "AGENTS.md", "<!-- claude-md-for-codex:generated from CLAUDE.md -->\n\nold\n")
        orphan = self.repo("orphan")
        self.write(orphan / "AGENTS.md", "<!-- claude-md-for-codex:generated from CLAUDE.md -->\n\nold\n")
        self.assertEqual(self.case(repo), ("legacy", "update-agents"))
        self.sync()
        self.assertEqual((repo / "AGENTS.md").read_text(), "rules\n")
        self.assertFalse((orphan / "AGENTS.md").exists())

    def test_symlinked_paths_still_get_a_repo_relative_exclude(self):
        repo = self.repo("real")
        self.write(repo / "sub/CLAUDE.md", "rules")
        link = self.home / "link"
        link.symlink_to(self.code)
        self.run_cli("sync", "--root", str(link), "--no-global")
        self.assertIn("/sub/AGENTS.md", (repo / ".git/info/exclude").read_text().splitlines())
        self.assertEqual(self.git(repo, "status", "--porcelain", "--untracked-files=all").strip(), "?? sub/CLAUDE.md")

    def test_worktree_exclude_goes_to_the_shared_git_dir(self):
        repo = self.repo("wt-main")
        self.write(repo / "README", "x")
        self.commit(repo, "init")
        wt = self.code / "wt-branch"
        self.git(repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.write(wt / "CLAUDE.md", "branch rules")
        self.run_cli("sync", "--root", str(wt), "--no-global")
        self.assertIn("/AGENTS.md", (repo / ".git/info/exclude").read_text())
        self.assertNotIn("AGENTS.md", self.git(wt, "status", "--porcelain"))

    def test_skips_dependencies_hidden_folders_and_home(self):
        repo = self.repo("deps")
        self.write(repo / "node_modules/pkg/CLAUDE.md", "dependency")
        self.write(repo / ".cache/CLAUDE.md", "hidden")
        self.write(self.env.claude_dir / "CLAUDE.md", "global")
        self.assertEqual(s.find_folders(self.home, 8, self.home), [])
        link = self.base / "home-link"
        link.symlink_to(self.home)
        self.assertEqual(s.find_folders(link, 8, self.home), [])


class BehindTest(Sandbox):
    """A checkout that is only behind its default branch gets the fix from a pull, not from us."""

    def stale_checkout(self, upstream_claude: str, upstream_agents: str) -> Path:
        origin = self.base / "origin.git"
        self.git(self.base, "init", "-q", "--bare", str(origin))
        repo = self.repo("main-checkout")
        self.write(repo / "CLAUDE.md", "# Rules\nold claude rule\n")
        self.write(repo / "AGENTS.md", "# Rules\nold agents rule\n")
        self.commit(repo, "two different instruction files")
        self.git(repo, "branch", "feature")
        self.write(repo / "CLAUDE.md", upstream_claude)
        self.write(repo / "AGENTS.md", upstream_agents)
        self.commit(repo, "fix the instruction files")
        self.git(repo, "remote", "add", "origin", str(origin))
        self.git(repo, "push", "-q", "origin", "main")
        self.git(repo, "fetch", "-q", "origin")
        wt = self.code / "wt-feature"
        self.git(repo, "worktree", "add", "-q", str(wt), "feature")
        return wt

    def test_stale_worktree_is_left_alone_when_the_default_branch_is_in_sync(self):
        wt = self.stale_checkout("@AGENTS.md\n", "# Rules\nnew rule\n")
        self.assertEqual(self.case(wt), ("behind", "none"))
        code, out, err = self.sync()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.merges_run(), 0)
        self.assertEqual(self.git(wt, "status", "--porcelain"), "")
        _, out, _ = self.run_cli("status", "--root", str(self.code), "--no-global")
        self.assertIn("1 behind their default branch (left alone)", out)
        self.git(wt, "merge", "-q", "origin/main")
        self.assertEqual(self.case(wt), ("shared", "none"))

    def test_identical_copies_on_the_default_branch_count_as_in_sync(self):
        wt = self.stale_checkout("# Rules\nnew rule\n", "# Rules\nnew rule\n")
        self.assertEqual(self.case(wt), ("behind", "none"))

    def test_a_local_edit_is_synced_as_usual(self):
        wt = self.stale_checkout("@AGENTS.md\n", "# Rules\nnew rule\n")
        (wt / "CLAUDE.md").write_text("# Rules\nold claude rule\nmy new rule\n")
        self.assertEqual(self.case(wt), ("unsynced", "merge"))

    def test_merges_as_usual_when_the_default_branch_differs_too(self):
        wt = self.stale_checkout("# Rules\nclaude v2\n", "# Rules\nagents v2\n")
        self.assertEqual(self.case(wt), ("unsynced", "merge"))

    def test_untracked_local_copies_are_still_updated(self):
        wt = self.stale_checkout("@AGENTS.md\n", "# Rules\nnew rule\n")
        self.write(wt / "sub/CLAUDE.md", "sub v1\n")
        self.sync()  # creates sub/AGENTS.md as a local, excluded copy
        (wt / "sub/CLAUDE.md").write_text("sub v2\n")
        self.assertEqual(self.case(wt / "sub"), ("claude-changed", "update-agents"))


class TrackedLaterTest(Sandbox):
    def test_a_pull_replaces_the_local_copy_and_purge_keeps_the_tracked_file(self):
        repo = self.repo("pulled")
        self.write(repo / "sub/CLAUDE.md", "sub rules\n")
        self.commit(repo, "nested CLAUDE.md")
        cli.save_config({"roots": [str(self.code)]})
        self.run_cli("sync", "--no-global")
        self.assertEqual((repo / "sub/AGENTS.md").read_text(), "sub rules\n")  # local copy, excluded
        self.git(repo, "checkout", "-q", "-b", "upstream")
        self.git(repo, "add", "-f", "sub/AGENTS.md")
        self.write(repo / "sub/CLAUDE.md", "@AGENTS.md\n")
        self.commit(repo, "commit AGENTS.md, CLAUDE.md imports it")
        self.git(repo, "checkout", "-q", "main")
        self.write(repo / "sub/AGENTS.md", "sub rules\n")  # the local copy is back on main
        self.git(repo, "merge", "-q", "upstream")  # git replaces the ignored copy
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        with mock.patch("sync_agentsmd_claudemd.schedule.uninstall", return_value=[]):
            _, out, _ = self.run_cli("uninstall", "--purge")
        self.assertIn("git tracks it now", out)
        self.assertTrue((repo / "sub/AGENTS.md").exists())
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_sync_forgets_created_files_once_git_tracks_them(self):
        repo = self.repo("tracked")
        self.write(repo / "CLAUDE.md", "rules\n")
        self.sync()
        state = State()
        self.assertIn(str(repo / "AGENTS.md"), state.data["created"])
        self.git(repo, "add", "-f", "AGENTS.md")
        self.sync()
        self.assertNotIn(str(repo / "AGENTS.md"), State().data["created"])


class GlobalTest(Sandbox):
    def test_global_claude_md_is_copied_for_codex(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "Be brief.\n@~/notes/style.md\n")
        self.write(self.home / "notes/style.md", "Use metric units.")
        code, out, _ = self.run_cli("sync")
        self.assertEqual(code, 0)
        agents = (self.env.codex_home / "AGENTS.md").read_text()
        self.assertIn("Be brief.", agents)
        self.assertIn("imported from ~/notes/style.md -->\nUse metric units.", agents)

    def test_global_agents_md_is_copied_for_claude(self):
        self.write(self.env.codex_home / "AGENTS.md", "Codex rules\n")
        self.run_cli("sync")
        self.assertEqual((self.env.claude_dir / "CLAUDE.md").read_text(), "Codex rules\n")

    def test_override_warning(self):
        self.write(self.env.claude_dir / "CLAUDE.md", "x")
        self.write(self.env.codex_home / "AGENTS.override.md", "override")
        [a] = s.build_plan(self.env, [], State())
        self.assertTrue(any("AGENTS.override.md" in w for w in a.warnings))


class CliTest(Sandbox):
    def test_status_exit_codes_and_json(self):
        repo = self.repo("cli")
        self.write(repo / "CLAUDE.md", "cli rules")
        code, out, _ = self.run_cli("status", "--root", str(self.code), "--no-global")
        self.assertEqual(code, 1)
        self.assertIn("only CLAUDE.md -> copy CLAUDE.md -> AGENTS.md", out)
        code, out, _ = self.run_cli("status", "--root", str(self.code), "--no-global", "--json")
        self.assertEqual(json.loads(out)[0]["action"], "create-agents")
        self.sync()
        self.assertEqual(self.run_cli("status", "--root", str(self.code), "--no-global")[0], 0)

    def test_dry_run_changes_nothing(self):
        repo = self.repo("dry")
        self.write(repo / "CLAUDE.md", "rules")
        code, out, _ = self.sync("--dry-run")
        self.assertEqual(code, 1)
        self.assertFalse((repo / "AGENTS.md").exists())

    def test_roots_and_merger_come_from_config(self):
        repo = self.repo("cfg")
        self.write(repo / "CLAUDE.md", "# Rules\nx\n")
        self.write(repo / "AGENTS.md", "# Rules\ny\n")
        cli.save_config({"roots": [str(self.code)], "merger": self.merger, "max_merges": 1})
        self.assertEqual(cli.load_config()["max_merges"], 1)
        self.run_cli("sync", "--no-global")
        self.assertEqual(self.merges_run(), 1)

    def test_settings_and_state_under_the_old_name_are_moved(self):
        old_cfg = self.write(self.home / ".config/claude-md-for-codex/config.toml", 'roots = ["/x"]\n')
        self.write(self.home / ".local/state/claude-md-for-codex/state.json", '{"pairs": {"k": {}}}')
        _, _, err = self.run_cli("status", "--no-global")
        self.assertIn("moved", err)
        self.assertFalse(old_cfg.exists())
        self.assertEqual(cli.load_config()["roots"], ["/x"])
        self.assertIn("k", State().data["pairs"])

    def test_purge_also_removes_exclude_lines_written_under_the_old_name(self):
        repo = self.repo("old-exclude")
        self.write(repo / "AGENTS.md", "x")
        exclude = repo / ".git/info/exclude"
        exclude.write_text("keep\n# claude-md-for-codex: local copy of CLAUDE.md / AGENTS.md\n/AGENTS.md\n")
        s.git_unexclude(repo / "AGENTS.md")
        self.assertEqual(exclude.read_text(), "keep\n")

    def test_size_warning_counts_the_agents_md_files_above(self):
        repo = self.repo("big")
        self.write(repo / "AGENTS.md", "a" * 20000 + "\n")
        self.write(repo / "CLAUDE.md", "@AGENTS.md\n")
        self.write(repo / "sub/CLAUDE.md", "b" * 15000 + "\n")
        self.commit(repo, "big instruction files")
        _, out, _ = self.run_cli("status", "--root", str(self.code), "--no-global")
        self.assertIn("AGENTS.md here and in the folders above come to 35002 bytes", out)
        self.write(self.env.codex_home / "config.toml", "project_doc_max_bytes = 65536\n")
        _, out, _ = self.run_cli("status", "--root", str(self.code), "--no-global")
        self.assertNotIn("project_doc_max_bytes", out)

    def test_codex_max_bytes_from_codex_config(self):
        self.write(self.env.codex_home / "config.toml", "project_doc_max_bytes = 65536\n")
        self.assertEqual(cli.codex_max_bytes(s.Env.from_os()), 65536)

    def test_purge_removes_only_unedited_created_files(self):
        a = self.repo("purge-a")
        self.write(a / "CLAUDE.md", "rules a")
        b = self.repo("purge-b")
        self.write(b / "AGENTS.md", "rules b")
        cli.save_config({"roots": [str(self.code)]})
        self.run_cli("sync", "--no-global")
        (b / "CLAUDE.md").write_text("edited by hand")
        with mock.patch("sync_agentsmd_claudemd.schedule.uninstall", return_value=[]):
            _, out, _ = self.run_cli("uninstall", "--purge")
        self.assertFalse((a / "AGENTS.md").exists())
        self.assertNotIn("/AGENTS.md", (a / ".git/info/exclude").read_text())
        self.assertTrue((b / "CLAUDE.md").exists())
        self.assertIn("kept", out)


if __name__ == "__main__":
    unittest.main()
