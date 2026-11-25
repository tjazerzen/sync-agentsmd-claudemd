import tempfile
import unittest
from pathlib import Path

from claude_md_for_codex import render as r


class FindImportsTest(unittest.TestCase):
    def test_finds_imports_at_line_start_and_after_space(self):
        text = "@AGENTS.md\nSee @docs/style.md for style.\n- @~/.claude/me.md"
        self.assertEqual(r.find_imports(text), ["AGENTS.md", "docs/style.md", "~/.claude/me.md"])

    def test_ignores_emails_and_quoted_paths(self):
        self.assertEqual(r.find_imports('mail user@example.com or "@quoted.md" or @"also.md"'), [])

    def test_ignores_code_spans_and_fenced_blocks(self):
        text = "Use `@not-this.md` here.\n```\n@not-in-fence.md\n```\n~~~md\n@nor-this.md\n~~~\n@yes.md"
        self.assertEqual(r.find_imports(text), ["yes.md"])

    def test_inner_fence_of_other_length_does_not_close(self):
        self.assertEqual(r.find_imports("````\n```\n@inside.md\n````\n@after.md"), ["after.md"])

    def test_escaped_space(self):
        self.assertEqual(r.find_imports(r"@Design\ Docs/api.md"), ["Design Docs/api.md"])


class StripCommentsTest(unittest.TestCase):
    def test_strips_block_comments_only(self):
        text = "a\n<!-- hidden -->\nb <!-- inline stays -->\n<!--\nmulti\n-->\nc"
        self.assertEqual(r.strip_block_comments(text), "a\nb <!-- inline stays -->\nc")

    def test_keeps_comments_in_code_blocks(self):
        text = "```html\n<!-- keep -->\n```"
        self.assertEqual(r.strip_block_comments(text), text)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def view(self, path, **kw):
        return r.codex_view(path, path.read_text(), self.home, **kw)

    def test_without_imports_the_view_is_the_file_unchanged(self):
        c = self.write("p/CLAUDE.md", "Ask @alice, mail bob@example.com.\n")
        self.assertEqual(self.view(c), ("Ask @alice, mail bob@example.com.\n", []))

    def test_imports_are_appended_once_in_claude_order(self):
        c = self.write("p/CLAUDE.md", "Top\n@a.md\n@sub/b.md\n@a.md\n")
        self.write("p/a.md", "A text")
        self.write("p/sub/b.md", "B imports @../a.md and @c.md")
        self.write("p/sub/c.md", "C text")
        text, _ = self.view(c)
        self.assertTrue(text.startswith("Top\n@a.md\n@sub/b.md\n@a.md\n\n"))
        self.assertEqual(text.count(r.IMPORT_BLOCK), 3)
        self.assertLess(text.index("imported from a.md"), text.index("imported from sub/b.md"))
        self.assertIn("imported from sub/c.md (imported by sub/b.md) -->\nC text", text)

    def test_sibling_agents_md_is_never_inlined(self):
        c = self.write("p/CLAUDE.md", "@AGENTS.md\nmore")
        a = self.write("p/AGENTS.md", "agents")
        self.assertEqual(self.view(c, skip=a)[0], "@AGENTS.md\nmore")

    def test_cycle_and_depth_limit(self):
        for i in range(7):
            self.write(f"d/f{i}.md", f"level {i}\n@f{i + 1}.md\n@f0.md")
        text, warnings = self.view(self.root / "d/f0.md")
        self.assertEqual(text.count(r.IMPORT_BLOCK), 4)
        self.assertTrue(any("4 hops" in w for w in warnings))

    def test_tilde_and_trailing_punctuation(self):
        self.write("home/notes.md", "N")
        c = self.write("p/CLAUDE.md", "Read @~/notes.md.")
        self.assertIn("imported from ~/notes.md -->\nN", self.view(c)[0])

    def test_allowed_root_blocks_outside_imports(self):
        self.write("home/.aws/credentials", "secret")
        self.write("outside.md", "x")
        c = self.write("repo/CLAUDE.md", "@~/.aws/credentials\n@../outside.md")
        text, warnings = self.view(c, allowed_root=self.root / "repo")
        self.assertNotIn("secret", text)
        self.assertEqual(len(warnings), 2)

    def test_imports_file_and_residual(self):
        a = self.write("p/AGENTS.md", "shared")
        self.assertEqual(r.imports_file("@./AGENTS.md\n", self.root / "p", self.home, a), "@./AGENTS.md")
        self.assertIsNone(r.imports_file("@other.md", self.root / "p", self.home, a))
        self.assertEqual(r.residual_text("@AGENTS.md\n<!-- note -->\n", self.root / "p", self.home), "")
        self.assertEqual(r.residual_text("@AGENTS.md\nextra", self.root / "p", self.home), "extra")

    def test_legacy_marker(self):
        self.assertTrue(r.is_legacy_generated("<!-- claude-md-for-codex:generated from CLAUDE.md -->\nx"))
        self.assertFalse(r.is_legacy_generated("# Notes about claude-md-for-codex:generated files"))


if __name__ == "__main__":
    unittest.main()
