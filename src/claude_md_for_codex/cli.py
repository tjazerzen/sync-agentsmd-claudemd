"""Command line: status, sync, render, install, uninstall."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from . import __version__, render as r, schedule, sync as s
from .state import State, digest, run_lock

DEFAULT_EVERY = "1h"
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_MERGES = 5


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
    lines += [f"{k} = {toml_value(v)}" for k, v in cfg.items() if v is not None]
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


def _plan(args, env: s.Env, state: State) -> list[s.Analysis]:
    cfg = load_config()
    raw_roots = args.root if args.root else cfg.get("roots", [])
    return s.build_plan(
        env,
        [Path(p).expanduser().resolve() for p in raw_roots],
        state,
        include_global=not args.no_global,
        max_depth=args.max_depth if args.max_depth is not None else int(cfg.get("max_depth", DEFAULT_MAX_DEPTH)),
        allow_external=args.allow_external_imports or bool(cfg.get("allow_external_imports", False)),
        max_bytes=codex_max_bytes(env),
    )


def _show(env: s.Env, path: Path) -> str:
    for home in (env.home, env.home.resolve()):
        try:
            return "~/" + path.relative_to(home).as_posix()
        except ValueError:
            continue
    return str(path)


def _where(env: s.Env, a: s.Analysis) -> str:
    if a.pair.kind == "global":
        return f"{_show(env, a.pair.claude)} + {_show(env, a.pair.agents)}"
    return _show(env, a.pair.folder)


def cmd_status(args) -> int:
    env = s.Env.from_os()
    plan = _plan(args, env, State())
    if args.json:
        rows = [{"kind": a.pair.kind, "folder": str(a.pair.folder), "claude": str(a.pair.claude),
                 "agents": str(a.pair.agents), "case": a.case, "action": a.action, "detail": a.detail,
                 "warnings": a.warnings} for a in plan]
        print(json.dumps(rows, indent=1))
    else:
        shown = [a for a in plan if a.pending or a.action == "blocked" or args.all]
        for a in shown:
            what = a.detail or s.CASES[a.case]
            arrow = f" -> {s.ACTIONS[a.action]}" if a.action != "none" else ""
            print(f"{a.pair.kind:8} {_where(env, a)}: {what}{arrow}")
            for w in a.warnings:
                print(f"         warning: {w}")
        counts: dict[str, int] = {}
        for a in plan:
            counts[a.action] = counts.get(a.action, 0) + 1
        summary = ", ".join(f"{n} {s.ACTIONS[k]}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        print(f"\n{len(plan)} pairs checked: {summary}" if plan else "no CLAUDE.md or AGENTS.md files found")
        if not args.all and len(shown) < len(plan):
            print("(pairs with nothing to do are hidden; --all shows them)")
        if not load_config().get("roots") and not args.root:
            print("No project folders configured: only the global pair was checked. "
                  "Add some with `claude-md-for-codex install --root ~/code`.")
    return 1 if any(a.pending for a in plan) else 0


def _options(args, cfg: dict, env: s.Env) -> s.Options:
    return s.Options(
        merger=args.merger or cfg.get("merger", "auto"),
        model=args.model or cfg.get("model") or None,
        merges_allowed=0 if args.no_merge else (args.max_merges if args.max_merges is not None
                                                 else int(cfg.get("max_merges", DEFAULT_MAX_MERGES))),
        git_exclude=bool(cfg.get("git_exclude", True)),
        home=env.home,
    )


def cmd_sync(args) -> int:
    env = s.Env.from_os()
    cfg = load_config()
    with run_lock() as held:
        if not held:
            if not args.quiet:
                print("another sync is running")
            return 0
        state = State()
        plan = _plan(args, env, state)
        opts = _options(args, cfg, env)
        status = 0
        did = 0
        for a in plan:
            if args.dry_run:
                if a.pending:
                    print(f"would {s.ACTIONS[a.action]}: {_where(env, a)} ({s.CASES[a.case]})")
                continue
            report = s.apply(a, state, opts)
            if not report:
                continue
            failed = report.startswith(("merge failed", "merge skipped", "skipped"))
            if failed:
                status = 2
            if a.pending or a.action == "blocked":
                did += 1
                if failed or not args.quiet:
                    print(f"{_where(env, a)}: {report}", file=sys.stderr if failed else sys.stdout)
            for w in a.warnings:
                if not args.quiet:
                    print(f"warning: {_where(env, a)}: {w}", file=sys.stderr)
        if not args.dry_run:
            state.prune_backups()
            state.save()
        if args.dry_run:
            return 1 if any(a.pending for a in plan) else 0
        if not args.quiet and did == 0:
            print("everything is in sync")
        return status


def cmd_render(args) -> int:
    env = s.Env.from_os()
    if args.path is None:
        pair = s.global_pair(env)
    else:
        folder = Path(args.path).expanduser().resolve()
        pair = s.project_pair(folder.parent if folder.is_file() else folder, args.allow_external_imports)
    if pair is None or not pair.claude.is_file():
        print("no CLAUDE.md found", file=sys.stderr)
        return 1
    text = pair.claude.read_text(encoding="utf-8")
    view, warnings = r.codex_view(pair.claude, text, env.home, skip=pair.agents, allowed_root=pair.allowed_root)
    sys.stdout.write(view)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_install(args) -> int:
    env = s.Env.from_os()
    cfg = load_config()
    if args.root:
        cfg["roots"] = [str(Path(p).expanduser().resolve()) for p in args.root]
    for key in ("max_depth", "merger", "model", "max_merges"):
        if getattr(args, key) is not None:
            cfg[key] = getattr(args, key)
    if args.allow_external_imports:
        cfg["allow_external_imports"] = True
    every_text = args.every or cfg.get("every", DEFAULT_EVERY)
    every = schedule.parse_every(every_text)
    cfg["every"] = every_text
    print(f"saved settings to {_show(env, save_config(cfg))}")

    sync_args = argparse.Namespace(root=None, no_global=False, dry_run=False, quiet=False, max_depth=None,
                                   allow_external_imports=False, merger=None, model=None, max_merges=None,
                                   no_merge=False)
    status = cmd_sync(sync_args)

    kind = args.scheduler if args.scheduler != "auto" else schedule.detect()
    watch = [env.claude_dir / "CLAUDE.md", env.codex_home / "AGENTS.md"]
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
        state = State()
        for path_text, sha in sorted(state.data.get("created", {}).items()):
            path = Path(path_text)
            text = s._read(path) if path.is_file() and not path.is_symlink() else None
            if text is None:
                continue
            if digest(text) != sha:
                print(f"kept {_show(env, path)}: it was edited after this tool created it")
                continue
            path.unlink()
            s.git_unexclude(path)
            print(f"removed {_show(env, path)}")
        config_path().unlink(missing_ok=True)
        (state.root / "state.json").unlink(missing_ok=True)
        print(f"backups and merge records stay in {_show(env, state.root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-md-for-codex",
        description="Keep CLAUDE.md (Claude Code) and AGENTS.md (Codex) in step: copy whichever exists "
                    "to the other, and merge them with a coding agent when both changed.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def scope(sp):
        sp.add_argument("--root", action="append", metavar="DIR",
                        help="folder to search for CLAUDE.md / AGENTS.md (repeatable; default: from config)")
        sp.add_argument("--no-global", action="store_true", help="leave ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md alone")
        sp.add_argument("--max-depth", type=int, help=f"how deep to search each root (default {DEFAULT_MAX_DEPTH})")
        sp.add_argument("--allow-external-imports", action="store_true",
                        help="let project files import files outside their repository")

    sp = sub.add_parser("status", help="analyse every pair and say what sync would do; exit 1 if anything")
    scope(sp)
    sp.add_argument("--all", action="store_true", help="also list pairs that are in sync")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("sync", help="copy and merge so every pair matches")
    scope(sp)
    sp.add_argument("--dry-run", action="store_true", help="show what would change; exit 1 if anything would")
    sp.add_argument("--quiet", action="store_true", help="print only problems")
    sp.add_argument("--no-merge", action="store_true", help="copy only; leave pairs that need a merge")
    sp.add_argument("--max-merges", type=int, help=f"agent merges per run (default {DEFAULT_MAX_MERGES})")
    sp.add_argument("--merger", help="auto (default), claude, codex, or a command that reads the prompt on stdin")
    sp.add_argument("--model", help="model for the merger, e.g. sonnet")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("render", help="print what Codex gets for a CLAUDE.md (imports resolved)")
    sp.add_argument("path", nargs="?", help="a folder or its CLAUDE.md (default: your global CLAUDE.md)")
    sp.add_argument("--allow-external-imports", action="store_true")
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("install", help="save settings, sync now, and schedule regular syncs")
    sp.add_argument("--root", action="append", metavar="DIR", help="folder with your repositories (repeatable)")
    sp.add_argument("--every", help=f"how often to sync, e.g. 15m, 1h, daily (default {DEFAULT_EVERY})")
    sp.add_argument("--scheduler", choices=["auto", "systemd", "launchd", "cron"], default="auto")
    sp.add_argument("--max-depth", type=int)
    sp.add_argument("--merger")
    sp.add_argument("--model")
    sp.add_argument("--max-merges", type=int)
    sp.add_argument("--allow-external-imports", action="store_true")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove the schedule")
    sp.add_argument("--purge", action="store_true",
                    help="also delete files this tool created (if unedited), their exclude lines, and the settings")
    sp.set_defaults(func=cmd_uninstall)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
