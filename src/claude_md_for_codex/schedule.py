"""Run `claude-md-for-codex sync` on a schedule: systemd user timer, launchd agent or cron.

Each scheduler also re-runs the sync as soon as ~/.claude/CLAUDE.md changes where it can
(a systemd .path unit, launchd WatchPaths), so the timer only has to catch the project
folders and imported files.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "claude-md-for-codex"
LAUNCHD_LABEL = "io.github.tjazerzen.claude-md-for-codex"
CRON_TAG = f"# {NAME} (managed; remove with `{NAME} uninstall`)"
PASSED_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME")


def parse_every(value: str) -> int:
    """'15m', '1h', '30min', 'hourly', 'daily' -> seconds."""
    value = value.strip().lower()
    named = {"hourly": 3600, "daily": 86400}
    if value in named:
        return named[value]
    m = re.fullmatch(r"(\d+)\s*(s|sec|m|min|h|hr|d|day)?", value)
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"cannot read interval {value!r}; use e.g. 15m, 1h or daily")
    unit = (m.group(2) or "s")[0]
    seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds < 60:
        raise ValueError("the interval must be at least one minute")
    return seconds


def command() -> list[str]:
    """How the scheduler should start this tool (the installed script if there is one)."""
    exe = shutil.which(NAME)
    if exe:
        return [exe]
    return [sys.executable, "-m", "claude_md_for_codex"]


def detect() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl"):
        probe = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
        if probe.stdout.strip() in {"running", "degraded", "starting"}:
            return "systemd"
    return "cron"


def _passed_env() -> dict[str, str]:
    return {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r'[\s"\\]', arg) else arg


# ---------------------------------------------------------------- systemd


def systemd_units(cmd: list[str], every: int, watch: list[Path], env: dict[str, str]) -> dict[str, str]:
    exec_start = " ".join(_systemd_quote(a) for a in [*cmd, "sync", "--quiet"])
    env_lines = "".join(f"Environment={_systemd_quote(f'{k}={v}')}\n" for k, v in env.items())
    service = (
        "[Unit]\n"
        "Description=Copy Claude Code instructions (CLAUDE.md) into AGENTS.md for Codex\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{env_lines}"
        f"ExecStart={exec_start}\n"
        "Nice=10\n"
    )
    if every == 86400:
        when = "OnCalendar=daily\nPersistent=true\n"
    elif every == 3600:
        when = "OnCalendar=hourly\nPersistent=true\n"
    else:
        when = f"OnBootSec=2min\nOnUnitActiveSec={every}s\n"
    timer = (
        "[Unit]\n"
        f"Description=Run {NAME} sync on a schedule\n\n"
        "[Timer]\n"
        f"{when}"
        "\n[Install]\n"
        "WantedBy=timers.target\n"
    )
    path_lines = "".join(f"PathChanged={p}\n" for p in watch)
    path_unit = (
        "[Unit]\n"
        f"Description=Run {NAME} sync when Claude Code user instructions change\n\n"
        "[Path]\n"
        f"{path_lines}"
        f"Unit={NAME}.service\n"
        "\n[Install]\n"
        "WantedBy=default.target\n"
    )
    return {f"{NAME}.service": service, f"{NAME}.timer": timer, f"{NAME}.path": path_unit}


def _systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def install_systemd(cmd: list[str], every: int, watch: list[Path]) -> list[str]:
    unit_dir = _systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in systemd_units(cmd, every, watch, _passed_env()).items():
        (unit_dir / name).write_text(text, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer", f"{NAME}.path"], check=True)
    notes = [f"systemd user units in {unit_dir}: {NAME}.timer (every {every}s), {NAME}.path (on change)"]
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                            capture_output=True, text=True)
    if linger.returncode == 0 and linger.stdout.strip() == "Linger=no":
        notes.append("user services stop when you log out; run `loginctl enable-linger` to keep the timer running")
    return notes


def uninstall_systemd() -> list[str]:
    unit_dir = _systemd_dir()
    names = [f"{NAME}.timer", f"{NAME}.path", f"{NAME}.service"]
    if not any((unit_dir / n).exists() for n in names):
        return []
    subprocess.run(["systemctl", "--user", "disable", "--now", *names[:2]], capture_output=True)
    for n in names:
        (unit_dir / n).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return [f"removed systemd units from {unit_dir}"]


# ---------------------------------------------------------------- launchd


def launchd_plist(cmd: list[str], every: int, watch: list[Path], env: dict[str, str], log: Path) -> bytes:
    spec: dict = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*cmd, "sync", "--quiet"],
        "StartInterval": every,
        "WatchPaths": [str(p) for p in watch],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    if env:
        spec["EnvironmentVariables"] = env
    return plistlib.dumps(spec)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def install_launchd(cmd: list[str], every: int, watch: list[Path]) -> list[str]:
    plist = _plist_path()
    log = Path.home() / "Library" / "Logs" / f"{NAME}.log"
    plist.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(launchd_plist(cmd, every, watch, _passed_env(), log))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    return [f"launchd agent {plist} (every {every}s and on change), log in {log}"]


def uninstall_launchd() -> list[str]:
    plist = _plist_path()
    if not plist.exists():
        return []
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    plist.unlink()
    return [f"removed {plist}"]


# ---------------------------------------------------------------- cron


def cron_line(cmd: list[str], every: int, env: dict[str, str]) -> str:
    minutes = every // 60
    if minutes < 60:
        when = f"*/{minutes} * * * *" if minutes > 1 else "* * * * *"
    elif minutes < 1440:
        when = f"7 */{max(1, minutes // 60)} * * *"
    else:
        when = "17 4 * * *"
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    return f"{when} {prefix}{' '.join(shlex.quote(a) for a in cmd)} sync --quiet {CRON_TAG}"


def _crontab() -> str:
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""


def _set_crontab(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def install_cron(cmd: list[str], every: int) -> list[str]:
    kept = [ln for ln in _crontab().splitlines() if CRON_TAG not in ln]
    kept.append(cron_line(cmd, every, _passed_env()))
    _set_crontab("\n".join(kept) + "\n")
    return ["crontab entry added (cron cannot watch files, so changes wait for the next run)"]


def uninstall_cron() -> list[str]:
    if not shutil.which("crontab"):
        return []
    current = _crontab()
    kept = [ln for ln in current.splitlines() if CRON_TAG not in ln]
    if kept == current.splitlines():
        return []
    _set_crontab("\n".join(kept) + "\n" if kept else "")
    return ["removed the crontab entry"]


def install(kind: str, every: int, watch: list[Path]) -> list[str]:
    cmd = command()
    if kind == "systemd":
        return install_systemd(cmd, every, watch)
    if kind == "launchd":
        return install_launchd(cmd, every, watch)
    if kind == "cron":
        return install_cron(cmd, every)
    raise ValueError(f"unknown scheduler {kind!r}")


def uninstall() -> list[str]:
    notes: list[str] = []
    if sys.platform == "darwin":
        notes += uninstall_launchd()
    elif shutil.which("systemctl"):
        notes += uninstall_systemd()
    notes += uninstall_cron()
    return notes
