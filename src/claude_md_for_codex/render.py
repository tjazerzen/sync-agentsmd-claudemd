"""Read Claude Code instruction files the way Claude Code does and render them for Codex.

Claude Code follows `@path` imports, skips imports inside code, and strips block-level
HTML comments. Codex does none of that: it reads AGENTS.md as plain text. This module
collects a set of Claude Code files plus everything they import, and renders them as one
Markdown document with a `<!-- source: ... -->` line before each file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

GENERATED_TAG = "claude-md-for-codex:generated"
MAX_IMPORT_HOPS = 4  # "Imported files can recursively import other files, with a maximum depth of four hops."

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
# `@` at the start of a line or after whitespace (so `user@example.com` is not an import);
# the path runs to the next unescaped whitespace, and `\ ` escapes a space.
_IMPORT = re.compile(r"(?:^|(?<=\s))@((?:\\ |[^\s\\])+)")
_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def is_generated(text: str) -> bool:
    """True when the text is a file this tool wrote: its first line is the generated header."""
    return text.lstrip("\ufeff").startswith(f"<!-- {GENERATED_TAG}")


def _fence_state(line: str, fence: str | None) -> tuple[bool, str | None]:
    """Return (is_fence_line, new_fence) for one line, tracking ``` and ~~~ blocks."""
    m = _FENCE.match(line)
    if fence is None:
        return (True, m.group(1)) if m else (False, None)
    if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
        return True, None
    return False, fence


def strip_block_comments(text: str) -> str:
    """Drop block-level HTML comments (whole-line `<!-- ... -->`) outside code blocks.

    Claude Code strips these before it shows a file to the model; comments that share a
    line with other text, and anything inside a fenced code block, are kept.
    """
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


def parse_rule_paths(text: str) -> tuple[list[str], str]:
    """Split a `.claude/rules/*.md` file into (`paths:` globs, body without frontmatter)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return [], text
    globs: list[str] = []
    lines = m.group(1).splitlines()
    for i, line in enumerate(lines):
        key = re.match(r"^paths\s*:\s*(.*)$", line)
        if not key:
            continue
        value = key.group(1).strip()
        if value.startswith("["):
            globs = [g.strip().strip("'\"") for g in value.strip("[]").split(",") if g.strip()]
        elif value:
            globs = [value.strip("'\"")]
        else:
            for item in lines[i + 1:]:
                entry = re.match(r"^\s*-\s*(.+?)\s*$", item)
                if entry:
                    globs.append(entry.group(1).strip("'\""))
                elif item.strip():
                    break
        break
    return globs, text[m.end():]


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


@dataclass
class Section:
    path: Path
    label: str
    text: str
    imported_by: str | None = None
    note: str | None = None


@dataclass
class Collection:
    """Files gathered from a set of entry points, in the order Claude Code shows them."""

    home: Path
    label_base: Path | None = None
    allowed_root: Path | None = None  # imports outside it are skipped (project files)
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _seen: set[Path] = field(default_factory=set)

    def label(self, path: Path) -> str:
        if self.label_base is not None:
            try:
                return path.relative_to(self.label_base).as_posix()
            except ValueError:
                pass
        try:
            return "~/" + path.relative_to(self.home).as_posix()
        except ValueError:
            return str(path)

    def add(self, path: Path, *, rule: bool = False, note: str | None = None) -> None:
        self._visit(path, depth=0, imported_by=None, rule=rule, note=note)

    def _visit(self, path: Path, depth: int, imported_by: str | None, rule: bool, note: str | None) -> None:
        try:
            real = path.resolve()
        except OSError:
            return
        if real in self._seen:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self.warnings.append(f"skipped {self.label(path)}: not UTF-8 text")
            return
        except OSError as exc:
            self.warnings.append(f"skipped {self.label(path)}: {exc.strerror or exc}")
            return
        if is_generated(raw):
            return
        self._seen.add(real)
        if rule:
            globs, raw = parse_rule_paths(raw)
            if globs and note is None:
                listed = ", ".join(f"`{g}`" for g in globs)
                note = (f"(Claude Code applies these rules only to files matching {listed}. "
                        "Follow them when you work on those files.)")
        text = strip_block_comments(raw)
        self.sections.append(Section(path, self.label(path), text, imported_by, note))
        if depth >= MAX_IMPORT_HOPS:
            if find_imports(text):
                self.warnings.append(f"{self.label(path)}: imports deeper than {MAX_IMPORT_HOPS} hops were not followed")
            return
        for target_raw in find_imports(text):
            target = resolve_import(target_raw, path.parent, self.home)
            if target is None:
                continue
            if self.allowed_root is not None and not _inside(target, self.allowed_root):
                self.warnings.append(
                    f"{self.label(path)}: skipped @{target_raw} (outside the project; "
                    "pass --allow-external-imports to include it)")
                continue
            self._visit(target, depth + 1, self.label(path), rule=False, note=None)

    def has_content(self) -> bool:
        return any(s.text.strip() for s in self.sections)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def render(sections: list[Section], header: str) -> str:
    """Render sections as one Markdown document that starts with the generated-file header."""
    parts = [header]
    for s in sections:
        body = s.text.strip("\n")
        if not body.strip():
            continue
        source = f"<!-- source: {s.label}" + (f" (imported by {s.imported_by})" if s.imported_by else "") + " -->"
        parts.append(source + "\n" + (s.note + "\n\n" if s.note else "") + body)
    return "\n\n".join(parts) + "\n"


def header(sources: str, edit_hint: str) -> str:
    return (f"<!-- {GENERATED_TAG} from {sources}. Do not edit this file: "
            f"claude-md-for-codex overwrites it. {edit_hint} -->")
