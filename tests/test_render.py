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

    def test_unclosed_inner_fence_of_other_char_does_not_close(self):
        text = "````\n```\n@inside.md\n````\n@after.md"
        self.assertEqual(r.find_imports(text), ["after.md"])

    def test_escaped_space(self):
        self.assertEqual(r.find_imports(r"@Design\ Docs/api.md"), ["Design Docs/api.md"])


class StripCommentsTest(unittest.TestCase):
    def test_strips_block_comments_only(self):
        text = "a\n<!-- hidden -->\nb <!-- inline stays -->\n<!--\nmulti\n-->\nc"
        self.assertEqual(r.strip_block_comments(text), "a\nb <!-- inline stays -->\nc")

    def test_keeps_comments_in_code_blocks(self):
        text = "```html\n<!-- keep -->\n```"
        self.assertEqual(r.strip_block_comments(text), text)

    def test_comment_followed_by_text_is_kept(self):
        text = "<!-- note --> visible"
        self.assertEqual(r.strip_block_comments(text), text)


class RulePathsTest(unittest.TestCase):
    def test_block_list(self):
        globs, body = r.parse_rule_paths("---\npaths:\n  - \"src/**/*.ts\"\n  - lib/*.py\n---\nBody\n")
        self.assertEqual(globs, ["src/**/*.ts", "lib/*.py"])
        self.assertEqual(body, "Body\n")

    def test_inline_list_and_scalar(self):
        self.assertEqual(r.parse_rule_paths("---\npaths: [a/**, 'b/*.md']\n---\nx")[0], ["a/**", "b/*.md"])
        self.assertEqual(r.parse_rule_paths("---\npaths: a/**\n---\nx")[0], ["a/**"])

    def test_no_frontmatter(self):
        self.assertEqual(r.parse_rule_paths("# Rule\n"), ([], "# Rule\n"))


class CollectionTest(unittest.TestCase):
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

    def test_imports_follow_the_file_in_claude_order_and_once(self):
        main = self.write("p/CLAUDE.md", "Top\n@a.md\n@sub/b.md\n@a.md\n")
        self.write("p/a.md", "A text")
        self.write("p/sub/b.md", "B imports @../a.md and @c.md")
        self.write("p/sub/c.md", "C text")
        col = r.Collection(home=self.home, label_base=self.root / "p")
        col.add(main)
        self.assertEqual([s.label for s in col.sections], ["CLAUDE.md", "a.md", "sub/b.md", "sub/c.md"])
        self.assertEqual(col.sections[3].imported_by, "sub/b.md")

    def test_import_cycle_terminates(self):
        a = self.write("a.md", "@b.md")
        self.write("b.md", "@a.md")
        col = r.Collection(home=self.home)
        col.add(a)
        self.assertEqual(len(col.sections), 2)

    def test_depth_limit_is_four_hops(self):
        for i in range(7):
            self.write(f"f{i}.md", f"level {i}\n@f{i + 1}.md")
        col = r.Collection(home=self.home, label_base=self.root)
        col.add(self.root / "f0.md")
        self.assertEqual([s.label for s in col.sections], [f"f{i}.md" for i in range(5)])
        self.assertTrue(any("four" in w or "4 hops" in w for w in col.warnings))

    def test_tilde_and_trailing_punctuation(self):
        self.write("home/notes.md", "N")
        main = self.write("p/CLAUDE.md", "Read @~/notes.md.")
        col = r.Collection(home=self.home)
        col.add(main)
        self.assertEqual([s.label for s in col.sections][1], "~/notes.md")

    def test_mentions_that_are_not_files_are_left_alone(self):
        main = self.write("p/CLAUDE.md", "Ask @someone or @team-lead.")
        col = r.Collection(home=self.home)
        col.add(main)
        self.assertEqual(len(col.sections), 1)

    def test_allowed_root_blocks_outside_imports(self):
        self.write("home/.aws/credentials", "secret")
        main = self.write("repo/CLAUDE.md", "@~/.aws/credentials\n@../outside.md")
        self.write("outside.md", "x")
        col = r.Collection(home=self.home, allowed_root=self.root / "repo")
        col.add(main)
        self.assertEqual(len(col.sections), 1)
        self.assertEqual(len(col.warnings), 2)

    def test_generated_files_are_never_sources(self):
        self.write("p/AGENTS.md", r.header("CLAUDE.md", "x") + "\nold copy")
        main = self.write("p/CLAUDE.md", "@AGENTS.md\nReal")
        col = r.Collection(home=self.home)
        col.add(main)
        self.assertEqual(len(col.sections), 1)

    def test_rule_with_paths_gets_a_note(self):
        rule = self.write("p/.claude/rules/ts.md", "---\npaths: [src/**]\n---\nUse strict mode.")
        col = r.Collection(home=self.home)
        col.add(rule, rule=True)
        self.assertIn("`src/**`", col.sections[0].note)
        self.assertEqual(col.sections[0].text, "Use strict mode.")

    def test_render_layout(self):
        main = self.write("p/CLAUDE.md", "<!-- private note -->\nHello\n@x.md")
        self.write("p/x.md", "X")
        col = r.Collection(home=self.home, label_base=self.root / "p")
        col.add(main)
        out = r.render(col.sections, r.header("CLAUDE.md", "Edit it."))
        self.assertTrue(r.is_generated(out))
        self.assertNotIn("private note", out)
        self.assertIn("<!-- source: CLAUDE.md -->\nHello\n@x.md", out)
        self.assertIn("<!-- source: x.md (imported by CLAUDE.md) -->\nX", out)

    def test_only_a_leading_header_marks_a_generated_file(self):
        self.assertTrue(r.is_generated(r.header("CLAUDE.md", "x") + "\nbody"))
        self.assertFalse(r.is_generated("# Notes\nWe use claude-md-for-codex:generated files.\n"))

    def test_residual_text(self):
        self.write("p/AGENTS.md", "shared")
        self.assertEqual(r.residual_text("@AGENTS.md\n", self.root / "p", self.home), "")
        self.assertEqual(r.residual_text("@AGENTS.md\nextra", self.root / "p", self.home), "extra")
        self.assertEqual(r.residual_text("@missing.md", self.root / "p", self.home), "@missing.md")


if __name__ == "__main__":
    unittest.main()
