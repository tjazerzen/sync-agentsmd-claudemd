"""Decide what Codex should read, compare it with what is on disk, and write the difference.

Two kinds of target:

* global: ~/.claude/CLAUDE.md (+ imports, + ~/.claude/rules) -> ~/.codex/AGENTS.md
* project: every folder under the configured roots that has CLAUDE.md, .claude/CLAUDE.md,
  CLAUDE.local.md or .claude/rules gets an AGENTS.md next to it, unless a hand-written
  AGENTS.md is already there. New files are listed in the repository's .git/info/exclude,
  so they never show up in `git status` or get committed.

A file this tool wrote carries a `claude-md-for-codex:generated` header. Files without it
are never changed or deleted.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import render as r

SKIP_DIRS = {"node_modules", "vendor", "__pycache__", "venv", "target", "dist", "bower_components"}
EXCLUDE_COMMENT = "# claude-md-for-codex: generated Codex copy of CLAUDE.md (local only)"
CODEX_DEFAULT_MAX_BYTES = 32 * 1024


@dataclass
class Env:
    home: Path
    claude_dir: Path
    codex_home: Path

    @classmethod
    def from_os(cls) -> "Env":
        home = Path.home()
        claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
        return cls(home, claude_dir, codex_home)


@dataclass
class Target:
    """One AGENTS.md this tool manages or inspects, and what to do with it."""

    kind: str  # "global" | "project"
    path: Path
    content: str | None  # what the file should contain (None: nothing to write)
    action: str  # ok | create | update | remove | blocked | shared | drift | skip
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return self.action in {"create", "update", "remove"}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _decide(path: Path, content: str | None) -> tuple[str, str]:
    """Action for a target we own (or would own): compare desired content with the file."""
    exists = path.exists() or path.is_symlink()
    current = _read(path) if exists else None
    if content is None:
        if exists and current is not None and r.is_generated(current) and not path.is_symlink():
            return "remove", "its Claude Code sources are gone"
        return "skip", ""
    if not exists:
        return "create", ""
    if path.is_symlink() or current is None or not r.is_generated(current):
        return "blocked", "a hand-written file is in the way"
    if current == content:
        return "ok", ""
    return "update", ""


# ---------------------------------------------------------------- global


def plan_global(env: Env) -> Target:
    target = env.codex_home / "AGENTS.md"
    extra = env.codex_home / "AGENTS.extra.md"
    col = r.Collection(home=env.home)
    claude_md = env.claude_dir / "CLAUDE.md"
    if claude_md.is_file():
        col.add(claude_md)
    for rule in _rule_files(env.claude_dir / "rules"):
        col.add(rule, rule=True)
    if extra.is_file():
        col.add(extra, note="(Codex-only instructions, kept in ~/.codex/AGENTS.extra.md.)")
    content = None
    if col.has_content():
        hdr = r.header(f"{col.label(claude_md)} and the files it imports",
                       f"Edit {col.label(claude_md)}; put Codex-only text in {col.label(extra)}.")
        content = r.render(col.sections, hdr)
    action, detail = _decide(target, content)
    if action == "blocked":
        detail = (f"{col.label(target)} is hand-written. Move it to {col.label(extra)} (it is appended "
                  "to the generated file), or run `claude-md-for-codex sync --adopt` to do that for you")
    warnings = list(col.warnings)
    override = env.codex_home / "AGENTS.override.md"
    if (_read(override) or "").strip():
        warnings.append(f"{col.label(override)} exists, so Codex reads it instead of {col.label(target)}")
    return Target("global", target, content, action, detail, warnings)


def _rule_files(rules_dir: Path) -> list[Path]:
    if not rules_dir.is_dir():
        return []
    return sorted(p for p in rules_dir.rglob("*.md") if p.is_file())


# ---------------------------------------------------------------- projects


def project_sources(folder: Path) -> list[tuple[Path, bool]]:
    """Claude Code files that apply in `folder`, in load order, as (path, is_rule)."""
    found: list[tuple[Path, bool]] = []
    for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
        if (folder / name).is_file():
            found.append((folder / name, False))
    found.extend((p, True) for p in _rule_files(folder / ".claude" / "rules"))
    if (folder / "CLAUDE.local.md").is_file():
        found.append((folder / "CLAUDE.local.md", False))
    return found


def find_folders(root: Path, max_depth: int, home: Path) -> list[Path]:
    """Folders under root that have Claude Code files or an AGENTS.md, skipping dependencies."""
    names = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude"}
    out: list[Path] = []
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        if folder != home and any(e.name in names for e in entries):
            out.append(Path(folder))
        if depth >= max_depth:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return sorted(out)


def _project_root(folder: Path) -> Path:
    """The enclosing git work tree (imports may not leave it), or the folder itself."""
    for parent in [folder, *folder.parents]:
        if (parent / ".git").exists():
            return parent
    return folder


def plan_project(folder: Path, env: Env, allow_external: bool = False, max_bytes: int = CODEX_DEFAULT_MAX_BYTES) -> Target | None:
    target = folder / "AGENTS.md"
    sources = project_sources(folder)
    current = _read(target) if target.is_file() else None
    ours = current is not None and r.is_generated(current) and not target.is_symlink()
    if not sources and not ours:
        return None

    col = r.Collection(home=env.home, label_base=folder,
                       allowed_root=None if allow_external else _project_root(folder))
    for path, is_rule in sources:
        col.add(path, rule=is_rule)

    if (target.exists() or target.is_symlink()) and not ours:
        return _inspect_hand_written(folder, target, col, env)

    content = None
    if col.has_content():
        names = ", ".join(p.relative_to(folder).as_posix() for p, _ in sources)
        content = r.render(col.sections, r.header(names, "Edit those files instead."))
    action, detail = _decide(target, content)
    warnings = list(col.warnings)
    if content is not None and len(content.encode()) > max_bytes:
        warnings.append(f"{len(content.encode())} bytes is over Codex's project_doc_max_bytes "
                        f"({max_bytes}); Codex will cut it off")
    return Target("project", target, content, action, detail, warnings)


def _inspect_hand_written(folder: Path, target: Path, col: r.Collection, env: Env) -> Target:
    """A hand-written AGENTS.md wins. Say whether Codex still sees all the Claude Code text."""
    try:
        agents_real = target.resolve()
    except OSError:
        agents_real = target
    missed: list[str] = []
    for s in col.sections:
        try:
            if s.path.resolve() == agents_real:
                continue
        except OSError:
            pass
        if r.residual_text(s.text, s.path.parent, env.home):
            missed.append(s.label)
    if not missed:
        return Target("project", target, None, "shared", "AGENTS.md is the shared source", col.warnings)
    return Target("project", target, None, "drift",
                  "Codex reads the hand-written AGENTS.md and misses: " + ", ".join(missed), col.warnings)


# ---------------------------------------------------------------- apply


def git_exclude(path: Path) -> str | None:
    """List path in its repository's .git/info/exclude. Returns the exclude file it edited."""
    folder = path.parent
    try:
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=20)
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None
        if git("check-ignore", "-q", str(path)).returncode == 0:
            return None
        common = git("rev-parse", "--git-common-dir").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    common_dir = Path(common) if os.path.isabs(common) else folder / common
    rel = os.path.relpath(path, top.stdout.strip()).replace(os.sep, "/")
    pattern = "/" + rel
    exclude = common_dir / "info" / "exclude"
    existing = _read(exclude) or ""
    if pattern in existing.splitlines():
        return None
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(f"{prefix}{EXCLUDE_COMMENT}\n{pattern}\n")
    return str(exclude)


def git_unexclude(path: Path) -> None:
    """Remove the exclude line git_exclude added for path (used by uninstall --purge)."""
    folder = path.parent
    try:
        top = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel", "--git-common-dir"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    if top.returncode != 0:
        return
    toplevel, common = top.stdout.strip().split("\n")[:2]
    common_dir = Path(common) if os.path.isabs(common) else folder / common
    exclude = common_dir / "info" / "exclude"
    lines = (_read(exclude) or "").split("\n")
    pattern = "/" + os.path.relpath(path, toplevel).replace(os.sep, "/")
    out: list[str] = []
    for i, line in enumerate(lines):
        if line == pattern and i > 0 and lines[i - 1] == EXCLUDE_COMMENT:
            out.pop()
            continue
        out.append(line)
    if out != lines:
        exclude.write_text("\n".join(out), encoding="utf-8")


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.claude-md-for-codex.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def apply(target: Target) -> None:
    if target.action in {"create", "update"} and target.content is not None:
        write_atomic(target.path, target.content)
        if target.kind == "project" and target.action == "create":
            git_exclude(target.path)
    elif target.action == "remove":
        target.path.unlink()


def adopt_global(env: Env) -> Path | None:
    """Move a hand-written ~/.codex/AGENTS.md to AGENTS.extra.md so sync can take over.

    A symlink (usually to ~/.claude/CLAUDE.md) is just removed: the generated file replaces it.
    """
    src, dst = env.codex_home / "AGENTS.md", env.codex_home / "AGENTS.extra.md"
    if src.is_symlink():
        src.unlink()
        return None
    if dst.exists():
        raise FileExistsError(f"{dst} already exists; merge {src} into it by hand")
    os.replace(src, dst)
    return dst


@dataclass
class Plan:
    targets: list[Target]

    @property
    def pending(self) -> list[Target]:
        return [t for t in self.targets if t.pending]


def build_plan(env: Env, roots: list[Path], *, include_global: bool = True, max_depth: int = 8,
               allow_external: bool = False, max_bytes: int = CODEX_DEFAULT_MAX_BYTES) -> Plan:
    targets: list[Target] = []
    if include_global:
        targets.append(plan_global(env))
    seen: set[Path] = set()
    for root in roots:
        for folder in find_folders(root, max_depth, env.home):
            if folder in seen:
                continue
            seen.add(folder)
            t = plan_project(folder, env, allow_external, max_bytes)
            if t is not None:
                targets.append(t)
    return Plan(targets)
