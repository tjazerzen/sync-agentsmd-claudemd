"""Command line: sync, status, render, install, uninstall."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

from . import __version__, render as r, schedule, sync as s

DEFAULT_EVERY = "1h"
DEFAULT_MAX_DEPTH = 8


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claude-md-for-codex" / "config.toml"


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def save_config(cfg: dict) -> Path:
    def toml_value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(toml_value(x) for x in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# claude-md-for-codex settings (written by `claude-md-for-codex install`)"]
    lines += [f"{k} = {toml_value(v)}" for k, v in cfg.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def codex_max_bytes(env: s.Env) -> int:
    """project_doc_max_bytes from ~/.codex/config.toml, or Codex's default."""
    try:
        with (env.codex_home / "config.toml").open("rb") as fh:
            value = tomllib.load(fh).get("project_doc_max_bytes")
        return int(value) if value else s.CODEX_DEFAULT_MAX_BYTES
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return s.CODEX_DEFAULT_MAX_BYTES


def _roots(args, cfg) -> list[Path]:
    raw = args.root if args.root else cfg.get("roots", [])
    return [Path(p).expanduser().resolve() for p in raw]


def _plan(args, env: s.Env) -> s.Plan:
    cfg = load_config()
    return s.build_plan(
        env,
        _roots(args, cfg),
        include_global=not args.no_global,
        max_depth=args.max_depth or int(cfg.get("max_depth", DEFAULT_MAX_DEPTH)),
        allow_external=args.allow_external_imports or bool(cfg.get("allow_external_imports", False)),
        max_bytes=codex_max_bytes(env),
    )


def _show(env: s.Env, path: Path) -> str:
    try:
        return "~/" + path.relative_to(env.home).as_posix()
    except ValueError:
        return str(path)


VERBS = {"create": "created", "update": "updated", "remove": "removed"}


def cmd_sync(args) -> int:
    env = s.Env.from_os()
    if args.adopt:
        target = env.codex_home / "AGENTS.md"
        current = s._read(target) if target.exists() else None
        if (target.is_symlink() or (current is not None and not r.is_generated(current))):
            moved = s.adopt_global(env)
            if not args.quiet:
                print(f"moved {_show(env, target)} to {_show(env, moved)}" if moved else
                      f"removed the {_show(env, target)} symlink")
    plan = _plan(args, env)
    status = 0
    for t in plan.targets:
        if t.pending:
            if args.dry_run:
                print(f"would {t.action} {_show(env, t.path)}")
            else:
                s.apply(t)
                if not args.quiet:
                    print(f"{VERBS[t.action]} {_show(env, t.path)}")
        elif t.action == "blocked":
            print(f"skipped {_show(env, t.path)}: {t.detail}", file=sys.stderr)
            status = 2
        for w in t.warnings:
            if not args.quiet:
                print(f"warning: {_show(env, t.path)}: {w}", file=sys.stderr)
    if args.dry_run and plan.pending:
        status = status or 1
    if not args.quiet and not plan.pending and status == 0:
        print("everything is up to date")
    return status


LABELS = {
    "ok": "up to date",
    "create": "missing (sync will create it)",
    "update": "stale (sync will update it)",
    "remove": "orphaned (sync will remove it)",
    "blocked": "BLOCKED",
    "shared": "shared",
    "drift": "DRIFT",
    "skip": "nothing to do",
}


def cmd_status(args) -> int:
    env = s.Env.from_os()
    plan = _plan(args, env)
    for t in plan.targets:
        line = f"{t.kind:8} {_show(env, t.path)}: {LABELS[t.action]}"
        if t.detail:
            line += f" ({t.detail})"
        print(line)
        for w in t.warnings:
            print(f"         warning: {w}")
    if not load_config().get("roots") and not args.root:
        print("\nNo project roots configured: only the global file is checked. "
              "Add some with `claude-md-for-codex install --root ~/code`.")
    return 1 if plan.pending or any(t.action == "blocked" for t in plan.targets) else 0


def cmd_render(args) -> int:
    env = s.Env.from_os()
    if args.path is None:
        t = s.plan_global(env)
    else:
        folder = Path(args.path).expanduser().resolve()
        if folder.is_file():
            folder = folder.parent
        t = s.plan_project(folder, env, allow_external=args.allow_external_imports)
        if t is not None and t.content is None and t.action in {"shared", "drift"}:
            print(f"{_show(env, t.path)} is hand-written, so Codex reads it as is ({t.detail}).", file=sys.stderr)
            return 1
    if t is None or t.content is None:
        print("no Claude Code instructions found", file=sys.stderr)
        return 1
    sys.stdout.write(t.content)
    for w in t.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_install(args) -> int:
    env = s.Env.from_os()
    cfg = load_config()
    if args.root:
        cfg["roots"] = [str(Path(p).expanduser().resolve()) for p in args.root]
    if args.max_depth:
        cfg["max_depth"] = args.max_depth
    if args.allow_external_imports:
        cfg["allow_external_imports"] = True
    every_text = args.every or cfg.get("every", DEFAULT_EVERY)
    every = schedule.parse_every(every_text)
    cfg["every"] = every_text
    print(f"saved settings to {_show(env, save_config(cfg))}")

    sync_args = argparse.Namespace(root=None, no_global=False, dry_run=False, quiet=False, adopt=args.adopt,
                                   max_depth=None, allow_external_imports=False)
    status = cmd_sync(sync_args)

    kind = args.scheduler if args.scheduler != "auto" else schedule.detect()
    watch = [env.claude_dir / "CLAUDE.md", env.claude_dir / "rules", env.codex_home / "AGENTS.extra.md"]
    schedule.uninstall()
    for note in schedule.install(kind, every, watch):
        print(note)
    return status


def cmd_uninstall(args) -> int:
    env = s.Env.from_os()
    notes = schedule.uninstall()
    for note in notes:
        print(note)
    if not notes:
        print("no schedule was installed")
    if args.purge:
        cfg = load_config()
        roots = [Path(p).expanduser() for p in cfg.get("roots", [])]
        candidates = [env.codex_home / "AGENTS.md"]
        for root in roots:
            candidates += [f / "AGENTS.md" for f in s.find_folders(root, int(cfg.get("max_depth", DEFAULT_MAX_DEPTH)), env.home)]
        for path in candidates:
            text = s._read(path) if path.is_file() and not path.is_symlink() else None
            if text is not None and r.is_generated(text):
                path.unlink()
                if path != env.codex_home / "AGENTS.md":
                    s.git_unexclude(path)
                print(f"removed {_show(env, path)}")
        config_path().unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-md-for-codex",
        description="Let OpenAI Codex read your Claude Code instructions: copies CLAUDE.md files "
                    "(with @imports resolved) into the AGENTS.md files Codex reads.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def scope(sp):
        sp.add_argument("--root", action="append", metavar="DIR",
                        help="folder to search for project CLAUDE.md files (repeatable; default: from config)")
        sp.add_argument("--no-global", action="store_true", help="skip ~/.claude/CLAUDE.md -> ~/.codex/AGENTS.md")
        sp.add_argument("--max-depth", type=int, help=f"how deep to search each root (default {DEFAULT_MAX_DEPTH})")
        sp.add_argument("--allow-external-imports", action="store_true",
                        help="let project files import files outside their repository")

    sp = sub.add_parser("sync", help="write the AGENTS.md files Codex reads")
    scope(sp)
    sp.add_argument("--dry-run", action="store_true", help="show what would change; exit 1 if anything would")
    sp.add_argument("--quiet", action="store_true", help="print only problems")
    sp.add_argument("--adopt", action="store_true",
                    help="move a hand-written ~/.codex/AGENTS.md to ~/.codex/AGENTS.extra.md and take over")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("status", help="show what Codex sees for each file; exit 1 if a sync is needed")
    scope(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("render", help="print the AGENTS.md that would be generated (global, or for a folder)")
    sp.add_argument("path", nargs="?", help="a folder or its CLAUDE.md (default: your global CLAUDE.md)")
    sp.add_argument("--allow-external-imports", action="store_true")
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("install", help="save settings, sync now, and schedule regular syncs")
    sp.add_argument("--root", action="append", metavar="DIR", help="folder with your repositories (repeatable)")
    sp.add_argument("--every", help=f"how often to sync, e.g. 15m, 1h, daily (default {DEFAULT_EVERY})")
    sp.add_argument("--scheduler", choices=["auto", "systemd", "launchd", "cron"], default="auto")
    sp.add_argument("--max-depth", type=int)
    sp.add_argument("--allow-external-imports", action="store_true")
    sp.add_argument("--adopt", action="store_true", help="as for sync")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove the schedule")
    sp.add_argument("--purge", action="store_true",
                    help="also delete generated AGENTS.md files, their exclude lines, and the settings")
    sp.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
