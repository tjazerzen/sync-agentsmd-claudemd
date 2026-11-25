# claude-md-for-codex

Let [OpenAI Codex](https://github.com/openai/codex) read the instructions you wrote for
[Claude Code](https://code.claude.com).

Claude Code reads `CLAUDE.md`. Codex reads `AGENTS.md`. Since v2.1.277, Claude Code
reads `AGENTS.md` when there is no `CLAUDE.md` in the working folder or above it, so a
setup that starts from AGENTS.md already works in both. The other direction does not work out of the box:

- Codex never reads your personal `~/.claude/CLAUDE.md`.
- Codex does not follow `@path` imports. It sees the literal text `@docs/rules.md`.
- Codex ignores `CLAUDE.local.md`, `.claude/CLAUDE.md` and `.claude/rules/`.

`claude-md-for-codex` writes AGENTS.md copies of those files on a schedule, and as soon
as your global CLAUDE.md changes:

| Claude Code reads | Codex gets |
| --- | --- |
| `~/.claude/CLAUDE.md`, the files it `@imports`, and `~/.claude/rules/**/*.md` | `~/.codex/AGENTS.md` |
| `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/**/*.md` and `CLAUDE.local.md` in a folder, plus their imports | `AGENTS.md` in that folder, kept out of git |

It has no dependencies: about 1,100 lines of standard-library Python.

## Install

Python 3.11 or newer. Works on Linux and macOS.

```sh
uv tool install git+https://github.com/tjazerzen/claude-md-for-codex
# or: pipx install git+https://github.com/tjazerzen/claude-md-for-codex

claude-md-for-codex install --root ~/code
```

`install` does three things:

1. Saves the settings in `~/.config/claude-md-for-codex/config.toml`: the folders to
   search for projects, here `~/code`, and how often to sync.
2. Syncs once.
3. Schedules later syncs:
   - **Linux:** a systemd user timer, plus a `.path` unit that syncs again the moment
     `~/.claude/CLAUDE.md` or `~/.claude/rules/` changes.
   - **macOS:** a launchd agent with `WatchPaths` that does the same.
   - **Elsewhere:** a crontab entry (`--scheduler cron`).

The default interval is one hour; change it with `--every 15m`, `--every daily` and so on.
A sync of a folder with a hundred repositories takes well under a second, so a short
interval costs nothing.

Without `--root`, only the global file is synced.

## Commands

```text
claude-md-for-codex status      what Codex sees for each file; exits 1 if a sync is needed
claude-md-for-codex sync        write the files now (--dry-run to preview, --quiet for timers)
claude-md-for-codex render [DIR]  print the AGENTS.md that would be generated, without writing
claude-md-for-codex install     save settings, sync, and schedule (see above)
claude-md-for-codex uninstall   remove the schedule (--purge: also generated files and settings)
```

`sync` and `status` take `--root DIR` (repeatable) to override the saved folders, and
`--no-global` to leave `~/.codex/AGENTS.md` out.

Example `status` output:

```text
global   ~/.codex/AGENTS.md: up to date
project  ~/code/api/AGENTS.md: up to date
project  ~/code/web/AGENTS.md: shared (AGENTS.md is the shared source)
project  ~/code/old/AGENTS.md: DRIFT (Codex reads the hand-written AGENTS.md and misses: CLAUDE.md)
```

## Rules it follows

- **One way only.** The Claude Code files are the source. Edit those; the generated files
  are overwritten. Two-way sync needs conflict rules, and nobody wants to debug them.
- **It never touches a file it did not write.** Every generated file starts with a
  `claude-md-for-codex:generated` comment. A file without that comment is never changed,
  moved or deleted. The one exception is `sync --adopt`, described below, which you run
  yourself.
- **A hand-written AGENTS.md wins.** In a folder where AGENTS.md already exists, the tool
  only reports what Codex sees:
  - `shared` means CLAUDE.md is just `@AGENTS.md` (or a symlink to it), so both tools read
    the same text.
  - `DRIFT` means CLAUDE.md, CLAUDE.local.md or a rule file has text that Codex does not see.
    Merge it by hand.
- **Generated project files stay out of git.** When it creates `AGENTS.md` inside a
  repository, it adds the path to that repository's `.git/info/exclude`: a per-clone ignore
  list that is never committed. It never changes `.gitignore` or `git status`, and the
  file is never committed. Linked worktrees share the main checkout's exclude file. If the
  upstream project later adds a real AGENTS.md, `git pull` replaces the local copy (git
  treats ignored files as disposable), and from then on the tool leaves it alone.
- **Orphans are cleaned up.** If a folder loses its CLAUDE.md, the tool deletes the
  generated AGENTS.md there.
- **Project imports stay inside the project.** Claude Code asks before a project file
  imports something outside the repository. This tool skips such imports, with a warning,
  so a cloned repository cannot copy `@~/.ssh/config` into a file. Pass
  `--allow-external-imports` (or set it in the config) to include them. Your global
  `~/.claude/CLAUDE.md` may import anything.

## How the conversion works

It follows Claude Code's
[documented rules](https://code.claude.com/docs/en/memory#import-additional-files):

- **Imports.** An import is `@path` at the start of a line or after whitespace, so
  `user@example.com` is not one. A relative path resolves against the importing file's
  folder, and `~/` against your home.
- **What is not an import.** Anything inside inline code or a fenced code block, a quoted
  path, and a mention like `@alice` that does not name an existing file.
- **Depth.** Imports nest up to four hops. Each file is included once, which also breaks
  cycles.
- **Order.** As Claude Code shows them: each file, then the files it imports.
- **Layout.** Every file is preceded by a `<!-- source: path -->` line, so Codex (and you)
  can see where each instruction came from.
- **Comments.** Block-level HTML comments (`<!-- ... -->` on their own lines) are removed,
  as Claude Code removes them.
- **Load order.** In a project folder: `CLAUDE.md`, `.claude/CLAUDE.md`, the rules, then
  `CLAUDE.local.md`.
- **Path-scoped rules.** Codex has no equivalent of a rule with `paths:` frontmatter, so
  such a rule is kept with a note: *"Claude Code applies these rules only to files matching
  `src/**/*.ts`. Follow them when you work on those files."*
- **Searching.** The search skips dot-folders, `node_modules`, `vendor`, `venv`, `target`,
  `dist` and `__pycache__`, and does not treat your home folder as a project.

### Codex-only instructions

`~/.codex/AGENTS.md` now belongs to the tool. Put instructions meant only for Codex in
`~/.codex/AGENTS.extra.md`; they are added at the end. Codex itself does not read that
file, so nothing appears twice.

If you already have a hand-written `~/.codex/AGENTS.md`, `sync` refuses to replace it.
`claude-md-for-codex sync --adopt` (or `install --adopt`) moves it to `AGENTS.extra.md`
first. If it is a symlink to your CLAUDE.md, it removes the symlink.

## Limits worth knowing

- **Size.** Codex reads at most `project_doc_max_bytes` (32 KiB by default) of project
  instructions. `status` warns when a generated file is larger. Raise the limit in
  `~/.codex/config.toml` if you need to.
- **Nested files.** Codex loads AGENTS.md files from the repository root down to the folder
  it started in. Claude Code loads nested CLAUDE.md files when it touches files in that
  folder. The generated nested AGENTS.md files are there for Codex to find, but when it reads
  them is up to Codex.
- **`AGENTS.override.md` hides AGENTS.md from Codex.** If a folder (or `~/.codex`) has an
  `AGENTS.override.md`, Codex reads that instead of AGENTS.md. `status` warns about the
  global one.
- **Both tools reading AGENTS.md.** If you set Claude Code's *Project instructions* to
  `claude-md-and-agents-md`, Claude Code also reads the generated AGENTS.md next to your
  CLAUDE.md and sees everything twice. Leave it at the default,
  `claude-md-or-agents-md`. The same happens with a folder that has only
  `.claude/rules/` and no CLAUDE.md anywhere above it.
- **Not converted:**
  - Claude Code's auto memory (`~/.claude/projects/*/memory`).
  - Skills, hooks, MCP servers and settings.
  - Managed-policy CLAUDE.md files.

## Other ways to do this

Look at these first; for many setups one of them is enough.

**Built into the tools**

- **Write AGENTS.md and import it.** Keep the text in `AGENTS.md` and make `CLAUDE.md`
  the single line `@AGENTS.md`. Both tools then read the same file. This is the simplest
  setup for a repository you control. It does not cover `~/.claude/CLAUDE.md`.
- **Let Claude Code read AGENTS.md.** From v2.1.277, Claude Code reads `AGENTS.md` when
  there is no CLAUDE.md in the working folder or above it. See *Project instructions* in `/config`.
- **Let Codex read CLAUDE.md.** Add `project_doc_fallback_filenames = ["CLAUDE.md"]` to
  `~/.codex/config.toml`, and Codex reads CLAUDE.md in folders that have no AGENTS.md.
  It does not apply to `~/.codex`, follow imports, or read CLAUDE.local.md and rules.
  It combines fine with this tool.
- **Symlink.** Make `AGENTS.md` a symlink to `CLAUDE.md`, or the reverse. Imports are
  still not followed, and Windows needs Developer Mode for symlinks.
- **Codex `SessionStart` hook.** It could print your CLAUDE.md as extra context at the
  start of each session. Hook output is capped (`additionalContextLimit`), and you have to
  trust each hook definition in Codex first.

**Rule-sync tools**

These are from their READMEs as of November 2026. All are MIT-licensed.

| Tool | How it works | Main difference from this tool |
| --- | --- | --- |
| [ruler](https://github.com/intellectronica/ruler) | Generates CLAUDE.md, AGENTS.md and many other agents' files from one `.ruler/` folder per project. | You move your rules into its format, it does not read `~/.claude/CLAUDE.md`, and it does not resolve imports. Best when a whole team targets many agents. |
| [rulesync](https://github.com/dyoshikawa/rulesync) | Generates many agents' files from a `.rulesync/` source; can `import` an existing CLAUDE.md. | Same: its own source format, run by hand or in CI. |
| [agentsync](https://github.com/dallay/agentsync) | Generates from `.agents/`, symlinking CLAUDE.md to it. | Project-level only. |
| [agent-rules-sync](https://pypi.org/project/agent-rules-sync/) | A daemon that syncs `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` both ways; the newest edit wins. | The closest match. It is two-way, so an edit on either side can replace the other. It does not resolve imports. It also syncs skills, MCP servers and settings. |

This tool exists for one narrow case. You write for Claude Code and want Codex to see
the same thing, without changing how you write, committing generated files, or risking a
hand-written AGENTS.md.

## Uninstall

```sh
claude-md-for-codex uninstall --purge   # schedule, generated files, exclude lines, settings
uv tool uninstall claude-md-for-codex
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

The tests build a fake home folder and real git repositories (worktrees included) in a
temporary folder. They never touch your own files.

## License

MIT
