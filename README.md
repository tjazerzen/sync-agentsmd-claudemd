# sync-agentsmd-claudemd

Keep `CLAUDE.md` ([Claude Code](https://code.claude.com)) and `AGENTS.md`
([OpenAI Codex](https://github.com/openai/codex)) saying the same thing, in every project
and in your global instructions.

The tool looks at each folder that has either file and does the obvious thing:

| What it finds | What it does |
| --- | --- |
| only `CLAUDE.md` | copies it to `AGENTS.md`, with `@imports` resolved, since Codex cannot follow them |
| only `AGENTS.md` | copies it to `CLAUDE.md` |
| the same text, or a `CLAUDE.md` that is just `@AGENTS.md`, or a symlink between them | nothing |
| both, and only one changed since the last sync | copies the changed one over the other |
| both, and they differ in a way only a reader can settle | **merges them with a coding agent** (Claude Code or Codex, headless), using git history and the repository's default branch as evidence, without asking you |

The last case covers files that have drifted apart, files both sides edited since the last
sync, and the first time the tool sees a pair that differs. After a merge, both files
match, or CLAUDE.md becomes `@AGENTS.md` if that is the repository's convention. The next
change on one side is then copied without another merge.

The same rules apply to your global pair: `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.

It is about 1,700 lines of standard-library Python with no dependencies. It runs on Linux
and macOS.

## Install

Python 3.11 or newer. A merge needs [Claude Code](https://code.claude.com) or
[Codex](https://github.com/openai/codex) installed and signed in.

```sh
uv tool install git+https://github.com/tjazerzen/sync-agentsmd-claudemd
# or: pipx install git+https://github.com/tjazerzen/sync-agentsmd-claudemd

sync-agentsmd-claudemd status --root ~/code       # look first: what would happen where
sync-agentsmd-claudemd install --root ~/code      # then sync now and on a schedule
```

`install` does three things:

1. Saves the settings in `~/.config/sync-agentsmd-claudemd/config.toml`.
2. Syncs once.
3. Schedules later syncs:
   - **Linux:** a systemd user timer, plus a `.path` unit that syncs as soon as
     `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md` changes.
   - **macOS:** a launchd agent that does the same with `WatchPaths`.
   - **Elsewhere:** a crontab entry (`--scheduler cron`).

The default interval is one hour (`--every 15m`, `--every daily`, ...). Checking a folder of
a hundred repositories takes under a second; only merges take time, about 20 seconds each.

## The analysis

`status` changes nothing. It says what `sync` would do and why:

```text
$ sync-agentsmd-claudemd status --root ~/code
global   ~/.claude/CLAUDE.md + ~/.codex/AGENTS.md: only CLAUDE.md -> copy CLAUDE.md -> AGENTS.md
project  ~/code/api: only AGENTS.md -> copy AGENTS.md -> CLAUDE.md
project  ~/code/web: CLAUDE.md changed since the last sync -> copy CLAUDE.md over AGENTS.md
project  ~/code/old-branch: they differ and were never synced -> merge with a coding agent

41 pairs checked: 37 nothing to do, 2 copy ..., 1 ..., 1 merge with a coding agent
```

- `--all` also lists the pairs that are in sync.
- `--json` gives machine-readable output.
- The exit code is 1 when `sync` has work to do.

## How a merge works

The tool collects the evidence itself and gives it to the agent:

- **The texts:** both files, and how much of them is identical.
- **Each file's git history:**
  - the last commits that changed it, with dates, authors and messages;
  - how many commits touched it and when it was added;
  - whether it has uncommitted edits;
  - its modification time.
- **The default branch (for example `origin/main`):**
  - whether this checkout is already merged into it, and how far behind it is;
  - what both files look like there, for example "on origin/main, CLAUDE.md is only the
    line `@AGENTS.md`".
- **The last sync:** what changed since then.

The agent is told the rules:

- Keep every instruction that is still valid.
- When the files contradict each other, the version the evidence shows is current wins.
- An outdated earlier version loses to the newer file.
- Never invent instructions.

It answers in a fixed format:

```text
DECISION: merged - AGENTS.md is the maintained superset, so it is the base; the still-valid
reading pointers from CLAUDE.md were added.
CLAUDE_MD: import
<<<MERGED
...the merged file...
MERGED>>>
```

`CLAUDE_MD: import` means "write the merged text to AGENTS.md and make CLAUDE.md the single
line `@AGENTS.md`". The agent chooses it when that is the repository's convention.
`CLAUDE_MD: copy` writes the merged text to both files.

What keeps a merge safe:

- **The agent cannot touch anything.** It runs with no tools at all: `claude -p --tools ""`
  without hooks, MCP servers or saved sessions, or `codex exec` in a read-only sandbox.
  The tool writes the files itself.
- **Every overwritten file is backed up first**, in
  `~/.local/state/sync-agentsmd-claudemd/backups/`, and kept for 30 days.
- **Every merge is recorded**, with the prompt's decision and the full answer, in
  `~/.local/state/sync-agentsmd-claudemd/merges/`.
- **Bad answers are rejected.** An answer in the wrong format, one with conflict markers,
  or one far shorter than the inputs changes nothing. A pair that failed is not retried for
  a day.
- **Identical pairs are merged once.** Pairs with the same two texts (other checkouts or
  worktrees of one repository) reuse the first result.
- **At most 5 merges per run** by default (`--max-merges`, or `max_merges` in the config).
  `--no-merge` copies only.

Choose the agent with `--merger` or `merger` in the config:

- `auto` (the default) uses Claude Code if installed, otherwise Codex.
- `claude` or `codex` picks one.
- Any other value is a command that reads the prompt on stdin and prints the answer.

`--model sonnet` (or `model` in the config) picks the model.

## Other rules it follows

- **New files stay local.** A file the tool creates inside a git repository is listed in
  that repository's `.git/info/exclude`: a per-clone ignore list that is never committed.
  The new file never shows up in `git status` and is never committed by accident. Linked
  worktrees share the main checkout's exclude file. If you do want to commit it, run
  `git add -f`.
- **Existing files are edited in place.** If both files are tracked by git, a copy or a
  merge leaves an ordinary uncommitted change for you to commit or discard.
- **It never runs into a git operation.** A repository in the middle of a merge, rebase,
  cherry-pick or revert is skipped, and so is any file with conflict markers.
- **Symlinks keep working.** Writes go to the symlink's target. A symlink from one file to
  the other counts as in sync.
- **Project imports stay inside the project.** When the tool copies a CLAUDE.md for Codex,
  imports that leave the repository are skipped, as Claude Code asks before following them.
  This way a cloned repository cannot copy `@~/.ssh/config` into a file. Pass
  `--allow-external-imports` to include them. Your global CLAUDE.md may import anything.
- **Searching skips the noise.** The search ignores dot-folders, `node_modules`, `vendor`,
  `venv`, `target`, `dist` and `__pycache__`, and never treats your home folder as a project.

## How imports are resolved

A CLAUDE.md can pull other files in with `@path`; Codex reads `@path` as plain text. When
CLAUDE.md has imports, the AGENTS.md copy is the CLAUDE.md text followed by each imported
file, each after a `<!-- sync-agentsmd-claudemd: imported from docs/rules.md -->` line.

This follows Claude Code's
[import rules](https://code.claude.com/docs/en/memory#import-additional-files):

- `@path` counts at the start of a line or after a space, so `user@example.com` is not an
  import.
- It is ignored inside inline code and code blocks, when quoted, and when it is a mention
  like `@alice` that names no file.
- A relative path resolves against the importing file's folder; `~/` means your home.
- Imports nest up to four hops, and each file is included once.

If Codex edits such an AGENTS.md, the merge folds the edit back into CLAUDE.md and keeps the
`@import` lines.

## Commands

```text
sync-agentsmd-claudemd status [--all] [--json]   the analysis; exit 1 if sync has work to do
sync-agentsmd-claudemd sync [--dry-run]          copy and merge (--no-merge, --max-merges N,
                                              --merger, --model, --quiet for timers)
sync-agentsmd-claudemd render [DIR]              print the AGENTS.md text Codex gets for a CLAUDE.md
sync-agentsmd-claudemd install                   save settings, sync, schedule
sync-agentsmd-claudemd uninstall [--purge]       remove the schedule; --purge also deletes the files
                                              it created that you have not edited since
```

`status` and `sync` take `--root DIR` (repeatable) to override the saved folders, and
`--no-global` to leave your global pair alone.

## Limits worth knowing

- **Size.** Codex reads at most `project_doc_max_bytes` (32 KiB by default) of project
  instructions. `status` warns when an AGENTS.md copy is larger.
- **`AGENTS.override.md` hides AGENTS.md from Codex.** `status` warns about the global one.
- **Claude Code can read AGENTS.md too.** Since v2.1.277, Claude Code reads AGENTS.md when
  there is no CLAUDE.md in the working folder or above it. If you set its *Project
  instructions* option to `claude-md-and-agents-md`, it reads both and sees copied text
  twice. Keep the default.
- **Only CLAUDE.md and AGENTS.md are synced.** `CLAUDE.local.md`, `.claude/rules/`, skills,
  hooks, MCP servers and Claude Code's auto memory are not.

## Other ways to do this

Look at these first; for some setups one of them is enough.

**Built into the tools**

- **Write AGENTS.md and import it.** Keep the text in AGENTS.md and make CLAUDE.md the
  single line `@AGENTS.md`. This is the simplest setup for a repository you control, and
  this tool treats it as in sync.
- **Let Codex read CLAUDE.md.** Add `project_doc_fallback_filenames = ["CLAUDE.md"]` to
  `~/.codex/config.toml`, and Codex reads CLAUDE.md in folders without an AGENTS.md. It does
  not cover `~/.codex`, follow imports, or reconcile two files that both exist.
- **Symlink one file to the other.** Imports are still not followed, and Windows needs
  Developer Mode for symlinks.

**Rule-sync tools**

These are from their READMEs as of November 2026. All are MIT-licensed.

| Tool | How it works | Main difference from this tool |
| --- | --- | --- |
| [ruler](https://github.com/intellectronica/ruler) | Generates CLAUDE.md, AGENTS.md and many other agents' files from one `.ruler/` folder per project. | You move your rules into its format, and it does not read your existing files. Best when a team targets many agents. |
| [rulesync](https://github.com/dyoshikawa/rulesync) | Generates many agents' files from a `.rulesync/` source and can `import` an existing CLAUDE.md. | Same: its own source format, run by hand or in CI. |
| [agentsync](https://github.com/dallay/agentsync) | Generates from `.agents/` and symlinks CLAUDE.md to it. | Works on project files only. |
| [agent-rules-sync](https://pypi.org/project/agent-rules-sync/) | A daemon that syncs `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` both ways; the newest edit wins. | The closest match, but it works on whole files: the newest file replaces the other, with no merge. It also does not resolve imports. |

This tool's niche is existing files. It works with the CLAUDE.md and AGENTS.md files you
already have, wherever they are, and it reconciles them by reading them rather than by
timestamp.

## Upgrading from claude-md-for-codex

Before 0.3 this tool was called `claude-md-for-codex`. To upgrade:

```sh
uv tool install git+https://github.com/tjazerzen/sync-agentsmd-claudemd
sync-agentsmd-claudemd install      # moves the old settings and state, replaces the old timer
uv tool uninstall claude-md-for-codex
```

The first run moves `~/.config/claude-md-for-codex` and `~/.local/state/claude-md-for-codex`
to their new names. `install` removes the old systemd units, launchd agent or crontab entry
and sets up the new ones.

## Uninstall

```sh
sync-agentsmd-claudemd uninstall --purge   # schedule, created files you have not edited, settings
uv tool uninstall sync-agentsmd-claudemd
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

The tests build a fake home folder, real git repositories (worktrees and a remote included)
and a fake merger in a temporary folder. They never touch your files or call a real agent.

## License

MIT
