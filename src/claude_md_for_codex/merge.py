"""Merge a diverged CLAUDE.md and AGENTS.md with a headless coding agent.

The tool collects the facts (both texts, each file's git history, uncommitted edits,
modification times, what changed since the last sync) and asks the agent for one merged
file. The agent gets no tools: it cannot run commands or touch files, and the tool writes
the result itself, after taking a backup.

Mergers:
  claude  `claude -p` with no tools, no hooks, no MCP servers, nothing saved
  codex   `codex exec` in a read-only sandbox, nothing saved
  any other value: a shell-style command that reads the prompt on stdin and prints the answer
"""

from __future__ import annotations

import datetime
import difflib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXTRA_BIN_DIRS = ["~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "/usr/local/bin", "/opt/homebrew/bin"]
TIMEOUT = 900


class MergeError(Exception):
    pass


@dataclass
class MergeResult:
    decision: str  # claude | agents | merged
    reason: str
    content: str
    raw: str
    claude_md: str = "copy"  # copy: both files get the text; import: AGENTS.md gets it, CLAUDE.md imports it


def find_program(name: str) -> str | None:
    """Find a CLI even under a scheduler's minimal PATH."""
    extra = os.pathsep.join(os.path.expanduser(d) for d in EXTRA_BIN_DIRS)
    return shutil.which(name) or shutil.which(name, path=extra)


def merger_command(merger: str, model: str | None, workdir: Path) -> tuple[list[str], Path | None]:
    """(argv, file the answer is written to or None for stdout)."""
    if merger in ("auto", "claude", "codex"):
        names = ["claude", "codex"] if merger == "auto" else [merger]
        for name in names:
            exe = find_program(name)
            if not exe:
                continue
            if name == "claude":
                argv = [exe, "-p", "--output-format", "text", "--tools", "", "--no-session-persistence",
                        "--strict-mcp-config", "--settings", '{"disableAllHooks": true}']
                return argv + (["--model", model] if model else []), None
            out = workdir / "answer.md"
            argv = [exe, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                    "-C", str(workdir), "-o", str(out)]
            return argv + (["-m", model] if model else []) + ["-"], out
        raise MergeError(f"no merger found: install Claude Code or Codex, or set merger to a command "
                         f"(looked for {', '.join(names)})")
    return shlex.split(merger), None


def run_merger(prompt: str, merger: str = "auto", model: str | None = None, timeout: int = TIMEOUT) -> str:
    with tempfile.TemporaryDirectory(prefix="claude-md-for-codex-") as tmp:
        workdir = Path(tmp)
        argv, outfile = merger_command(merger, model, workdir)
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=workdir, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise MergeError(f"the merger took longer than {timeout} s") from None
        except OSError as exc:
            raise MergeError(f"could not start the merger: {exc}") from None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise MergeError(f"the merger exited with {proc.returncode}: {' | '.join(tail)}")
        if outfile is not None:
            try:
                return outfile.read_text(encoding="utf-8")
            except OSError:
                raise MergeError("the merger wrote no answer") from None
        return proc.stdout


# ---------------------------------------------------------------- facts


def _git(folder: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def file_facts(path: Path) -> str:
    """Git history and other signals for one file, as plain text for the prompt."""
    lines: list[str] = []
    try:
        st = path.stat()
        modified = datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes")
        lines.append(f"- size: {st.st_size} bytes; last modified on disk: {modified}")
    except OSError:
        pass
    folder = path.parent
    if _git(folder, "rev-parse", "--is-inside-work-tree") != "true":
        lines.append("- not in a git repository")
        return "\n".join(lines)
    if _git(folder, "ls-files", "--error-unmatch", path.name) is None:
        lines.append("- not tracked by git (a local file)")
        return "\n".join(lines)
    status = _git(folder, "status", "--porcelain", "--", path.name)
    lines.append("- has uncommitted edits" if status else "- no uncommitted edits")
    count = _git(folder, "rev-list", "--count", "HEAD", "--", path.name)
    added = _git(folder, "log", "--diff-filter=A", "--format=%ad", "--date=short", "--", path.name)
    if count:
        lines.append(f"- {count} commits touch it" + (f"; added {added.splitlines()[-1]}" if added else ""))
    log = _git(folder, "log", "-n", "8", "--date=short", "--format=%ad %an: %s", "--", path.name)
    if log:
        lines.append("- latest commits that changed it:")
        lines += [f"    {entry}" for entry in log.splitlines()]
    return "\n".join(lines)


def _default_branch(folder: Path) -> str | None:
    head = _git(folder, "rev-parse", "--abbrev-ref", "origin/HEAD")
    if head and head != "origin/HEAD":
        return head
    for name in ("origin/main", "origin/master"):
        if _git(folder, "rev-parse", "--verify", "--quiet", name) is not None:
            return name
    return None


def repo_facts(claude_path: Path, agents_path: Path, import_token: str) -> str:
    """How this checkout relates to the default branch, and both files there."""
    folder = claude_path.parent if claude_path.parent.name != ".claude" else claude_path.parent.parent
    if _git(folder, "rev-parse", "--is-inside-work-tree") != "true":
        return ""
    branch = _default_branch(folder)
    if branch is None:
        return "- the repository has no remote default branch to compare with"
    lines = []
    ahead = _git(folder, "rev-list", "--count", f"{branch}..HEAD")
    behind = _git(folder, "rev-list", "--count", f"HEAD..{branch}")
    if ahead == "0":
        lines.append(f"- this checkout is fully merged into {branch}" + (f" and {behind} commits behind it" if behind and behind != "0" else ""))
    elif ahead:
        lines.append(f"- this checkout has {ahead} commits not yet in {branch} ({behind or '?'} behind)")
    top = _git(folder, "rev-parse", "--show-toplevel")
    for label, path in (("CLAUDE.md", claude_path), ("AGENTS.md", agents_path)):
        if not top:
            break
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top)).replace(os.sep, "/")
        there = _git(folder, "show", f"{branch}:{rel}")
        if there is None:
            lines.append(f"- {label} does not exist on {branch}")
            continue
        try:
            here = path.read_text(encoding="utf-8").strip()
        except OSError:
            here = ""
        if there.strip() == here:
            lines.append(f"- {label} on {branch} is identical to this checkout's")
        elif label == "CLAUDE.md" and there.strip() == import_token:
            lines.append(f"- on {branch}, CLAUDE.md is only the line `{import_token}` (it imports AGENTS.md)")
        else:
            lines.append(f"- {label} on {branch} differs from this checkout's ({len(there.splitlines())} lines there)")
    return "\n".join(lines)


def overlap_facts(claude: str, agents: str) -> str:
    c, a = claude.splitlines(), agents.splitlines()
    sm = difflib.SequenceMatcher(None, c, a, autojunk=False)
    same = sum(block.size for block in sm.get_matching_blocks())
    return (f"- CLAUDE.md has {len(c)} lines, AGENTS.md has {len(a)} lines; {same} lines are identical "
            f"and in the same order (similarity {sm.ratio():.0%})")


# ---------------------------------------------------------------- prompt

PROMPT = """You are merging two instruction files for AI coding agents that should say the same thing: \
CLAUDE.md (read by Claude Code) and AGENTS.md (read by OpenAI Codex), {where}. They have diverged. \
Produce the one file both should contain from now on.

How to decide:
- Keep every instruction that is still valid in either file, without repeating it.
- When the files contradict each other, keep the version the evidence says is current: the file \
edited more recently, in later commits, with uncommitted edits, or the one that looks maintained. \
Drop the stale version.
- If one file is an outdated earlier version of the other (most of its content appears in the \
other, updated), the newer file wins; carry over only points from the older one that are still \
valid and missing from the newer one.
- Do not invent instructions or add comments about the merge. Keep the authors' wording, headings \
and order as far as possible.
- If an instruction only makes sense for one of the two tools, keep it and name the tool it is for.
- Facts about the repository's default branch count: a checkout that is already merged into it is \
stale, and the default branch shows the maintained version and the convention.
{special}
Facts:
{facts}

=== CLAUDE.md{claude_note} ===
{claude}
=== end of CLAUDE.md ===

=== AGENTS.md{agents_note} ===
{agents}
=== end of AGENTS.md ===

Answer in exactly this format and nothing else:
DECISION: <claude|agents|merged> - <one sentence: which file won or why they were combined>
CLAUDE_MD: <copy|import>
<<<MERGED
<the complete merged file>
MERGED>>>
"""

IMPORT_NOTE = """- Lines like `@path/to/file` in CLAUDE.md are imports: Claude Code reads the named file in \
their place. Keep them as they are. AGENTS.md is generated from CLAUDE.md with each imported file \
appended after a `<!-- claude-md-for-codex: imported from ... -->` line; leave those blocks out \
of your answer, and put any edit made inside them into the CLAUDE.md text instead.
"""

STUB_NOTE = """- CLAUDE.md imports AGENTS.md (`{token}`), so Claude Code reads AGENTS.md plus the rest of \
CLAUDE.md, while Codex reads only AGENTS.md. Your answer becomes the new AGENTS.md, and CLAUDE.md \
is reduced to the import line. Fold the rest of CLAUDE.md into it, including any blocks after a \
`<!-- claude-md-for-codex: imported from ... -->` line (other files CLAUDE.md imports), and do \
not include the import line itself.
"""


CLAUDE_MD_NOTE = """- CLAUDE_MD tells the tool what to write. `copy`: both files get your merged text. `import`: \
AGENTS.md gets it and CLAUDE.md becomes the single line `{token}`, so Claude Code reads AGENTS.md \
through an import. Choose `import` when CLAUDE.md already imports AGENTS.md or the repository's \
default branch does that; otherwise `copy`.
"""


def build_prompt(where: str, claude_path: Path, claude: str, agents_path: Path, agents: str, *,
                 history: str, stub_token: str | None, has_imports: bool, import_token: str = "@AGENTS.md") -> str:
    special = CLAUDE_MD_NOTE.format(token=import_token)
    if stub_token:
        special += STUB_NOTE.format(token=stub_token)
    elif has_imports:
        special += IMPORT_NOTE
    facts = "\n".join(f for f in [
        overlap_facts(claude, agents),
        history,
        repo_facts(claude_path, agents_path, import_token),
        "CLAUDE.md:",
        file_facts(claude_path),
        "AGENTS.md:",
        file_facts(agents_path),
    ] if f)
    return PROMPT.format(where=where, special=special, facts=facts, claude=claude.rstrip("\n"),
                         agents=agents.rstrip("\n"), claude_note=f" ({claude_path.name})" if claude_path.name != "CLAUDE.md" else "",
                         agents_note="")


_DECISION = re.compile(r"^\s*DECISION:\s*\**\s*(claude|agents|merged)\b\**\s*[-:–—]*\s*(.*)$", re.I | re.M)
_CLAUDE_MD = re.compile(r"^\s*CLAUDE_MD:\s*\**\s*(copy|import)\b", re.I | re.M)
_BODY = re.compile(r"^<<<MERGED[ \t]*\r?\n(.*?)\r?\n?^MERGED>>>[ \t]*$", re.S | re.M)
_CONFLICT = re.compile(r"^(<{7}|>{7})( |$)", re.M)


def parse_answer(raw: str, claude: str, agents: str) -> MergeResult:
    d = _DECISION.search(raw)
    b = _BODY.search(raw)
    if not d or not b:
        raise MergeError("the merger's answer was not in the expected format")
    content = b.group(1).strip("\n")
    fenced = re.fullmatch(r"```[a-z]*\n(.*)\n```", content, re.S)
    if fenced and not claude.lstrip().startswith("```") and not agents.lstrip().startswith("```"):
        content = fenced.group(1)
    if not content.strip():
        raise MergeError("the merger returned an empty file")
    if _CONFLICT.search(content):
        raise MergeError("the merger returned conflict markers")
    longest = max(len(claude.strip()), len(agents.strip()))
    if len(content) < 0.2 * longest:
        raise MergeError(f"the merged file ({len(content)} chars) is far shorter than the inputs ({longest} chars)")
    mode = _CLAUDE_MD.search(raw[:b.start()])
    return MergeResult(d.group(1).lower(), d.group(2).strip(), content + "\n", raw,
                       mode.group(1).lower() if mode else "copy")
