"""Read CLAUDE.md files the way Claude Code does.

Claude Code follows `@path` imports and skips imports inside code. Codex follows none of
them: it reads AGENTS.md as plain text. `codex_view` turns a CLAUDE.md into the text Codex
should get: the file itself, then every file it imports.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

LEGACY_TAG = "claude-md-for-codex:generated"  # header of files written by version 0.1
IMPORT_BLOCK = "<!-- claude-md-for-codex: imported from "
MAX_IMPORT_HOPS = 4  # "Imported files can recursively import other files, with a maximum depth of four hops."

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
# `@` at the start of a line or after whitespace (so `user@example.com` is not an import);
# the path runs to the next unescaped whitespace, and `\ ` escapes a space.
_IMPORT = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s\\])+)")
_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""


def is_legacy_generated(text: str) -> bool:
    """A file written by version 0.1, which always regenerated AGENTS.md from CLAUDE.md."""
    return text.lstrip("﻿").startswith(f"<!-- {LEGACY_TAG}")


def _fence_state(line: str, fence: str | None) -> tuple[bool, str | None]:
    """Return (is_fence_line, new_fence) for one line, tracking ``` and ~~~ blocks."""
    m = _FENCE.match(line)
    if fence is None:
        return (True, m.group(1)) if m else (False, None)
    if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
        return True, None
    return False, fence


def strip_block_comments(text: str) -> str:
    """Drop block-level HTML comments (whole-line `<!-- ... -->`) outside code blocks."""
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        is_fence, fence_after = _fence_state(line, fence)
        if fence is not None or is_fence:
            out.append(line)
            fence = fence_after
            i += 1
            continue
        if line.lstrip().startswith("<!--"):
            j = i
            while j < len(lines) and "-->" not in lines[j]:
                j += 1
            if j < len(lines) and not lines[j].split("-->", 1)[1].strip():
                i = j + 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def find_imports(text: str) -> list[str]:
    """Return the raw `@path` import targets in text, outside code spans and code blocks."""
    found: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        is_fence, fence_after = _fence_state(line, fence)
        if fence is not None or is_fence:
            fence = fence_after
            continue
        masked = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        for m in _IMPORT.finditer(masked):
            raw = m.group(1)
            if raw[0] in "\"'":  # "A path wrapped in quotes isn't imported at all."
                continue
            found.append(raw.replace("\\ ", " "))
    return found


def resolve_import(raw: str, base_dir: Path, home: Path) -> Path | None:
    """Resolve an import the way Claude Code does: relative to the importing file's folder.

    Returns None unless the target is an existing file. A trailing punctuation mark
    (`see @notes.md.`) is dropped if the path does not exist with it.
    """
    candidates = [raw]
    trimmed = raw.rstrip(_TRAILING_PUNCTUATION)
    if trimmed and trimmed != raw:
        candidates.append(trimmed)
    for candidate in candidates:
        if candidate == "~" or candidate.startswith("~") and not candidate.startswith("~/"):
            continue
        if candidate.startswith("~/"):
            path = home / candidate[2:]
        elif os.path.isabs(candidate):
            path = Path(candidate)
        else:
            path = base_dir / candidate
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def residual_text(text: str, base_dir: Path, home: Path) -> str:
    """The text of a file once comments and resolvable imports are removed.

    Empty for a CLAUDE.md that only says `@AGENTS.md`: such a file adds nothing itself.
    """
    text = strip_block_comments(text)
    for raw in find_imports(text):
        if resolve_import(raw, base_dir, home) is not None:
            escaped = re.escape(raw.replace(" ", "\\ "))
            text = re.sub(r"(?:^|(?<=\s))@" + escaped + r"[" + re.escape(_TRAILING_PUNCTUATION) + r"]*", "", text)
    return text.strip()


def imports_file(text: str, base_dir: Path, home: Path, target: Path) -> str | None:
    """The import token (e.g. `@AGENTS.md`) in text that points at target, if any."""
    for raw in find_imports(text):
        found = resolve_import(raw, base_dir, home)
        if found is not None and _same(found, target):
            return "@" + raw.replace(" ", "\\ ")
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _label(path: Path, base: Path, home: Path) -> str:
    for b in (base, base.resolve()):
        try:
            return path.relative_to(b).as_posix()
        except ValueError:
            pass
    for h in (home, home.resolve()):
        try:
            return "~/" + path.relative_to(h).as_posix()
        except ValueError:
            pass
    return str(path)


def codex_view(claude_md: Path, text: str, home: Path, *, skip: Path | None = None,
               allowed_root: Path | None = None) -> tuple[str, list[str]]:
    """The text Codex should read for a CLAUDE.md: the file, then each file it imports.

    Without imports this is the file unchanged, so AGENTS.md is a plain copy. Each imported
    file is appended once (depth first, up to four hops) after an `imported from` line.
    `skip` (the sibling AGENTS.md) is never inlined. With `allowed_root`, imports that leave
    that folder are skipped with a warning, as Claude Code asks before following them.
    Returns (text, warnings).
    """
    base = claude_md.parent
    seen = {claude_md.resolve()}
    if skip is not None:
        seen.add(skip.resolve())
    blocks: list[str] = []
    warnings: list[str] = []

    def visit(path: Path, body: str, depth: int) -> None:
        targets = find_imports(body)
        if depth >= MAX_IMPORT_HOPS:
            if targets:
                warnings.append(f"{_label(path, base, home)}: imports deeper than {MAX_IMPORT_HOPS} hops were not followed")
            return
        for raw in targets:
            target = resolve_import(raw, path.parent, home)
            if target is None:
                continue
            real = target.resolve()
            if real in seen:
                continue
            if allowed_root is not None and not _inside(target, allowed_root):
                warnings.append(f"{_label(path, base, home)}: skipped @{raw} (outside the project; "
                                "pass --allow-external-imports to include it)")
                continue
            seen.add(real)
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                warnings.append(f"skipped @{raw}: {exc}")
                continue
            by = "" if path == claude_md else f" (imported by {_label(path, base, home)})"
            blocks.append(f"{IMPORT_BLOCK}{_label(target, base, home)}{by} -->\n{content.strip(chr(10))}\n")
            visit(target, content, depth + 1)

    visit(claude_md, text, 0)
    if not blocks:
        return text, warnings
    return text.rstrip("\n") + "\n\n" + "\n".join(blocks), warnings
