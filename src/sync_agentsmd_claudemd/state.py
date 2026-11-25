"""What the last sync saw, merge results, backups and the run lock.

Everything lives in $XDG_STATE_HOME/sync-agentsmd-claudemd (default ~/.local/state/...):

  state.json   per pair: hashes of CLAUDE.md, its Codex view and AGENTS.md after the last
               sync (so a later difference shows which side changed); files this tool
               created; recent merge failures
  merges/      every agent merge: the prompt's facts, the answer, the decision
  backups/     every file's previous content, before it is overwritten
  lock         held while a sync runs
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

BACKUP_DAYS = 30


LEGACY_NAME = "claude-md-for-codex"  # the tool's name before 0.3


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sync-agentsmd-claudemd"


def migrate_legacy_dirs(*new_dirs: Path) -> list[str]:
    """Move config and state folders kept under the old name to the new one (once)."""
    moved = []
    for new in new_dirs:
        old = new.with_name(LEGACY_NAME)
        if old.is_dir() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
            moved.append(f"moved {old} to {new}")
    return moved


def digest(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class State:
    def __init__(self, root: Path | None = None):
        self.root = root or state_dir()
        self.path = self.root / "state.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("pairs", {})
        self.data.setdefault("created", {})
        self.data.setdefault("failures", {})

    # pairs -----------------------------------------------------------

    def baseline(self, key: str) -> dict | None:
        return self.data["pairs"].get(key)

    def record(self, key: str, claude: str | None, view: str | None, agents: str | None) -> None:
        self.data["pairs"][key] = {"claude": digest(claude), "view": digest(view), "agents": digest(agents),
                                   "at": int(time.time())}

    def created(self, path: Path, text: str) -> None:
        self.data["created"][str(path)] = digest(text)

    # merge failures: do not retry the same pair of texts within a day -------

    def failed_recently(self, pair: str) -> str | None:
        entry = self.data["failures"].get(pair)
        if entry and time.time() - entry["at"] < 86400:
            return entry["error"]
        return None

    def failed(self, pair: str, error: str) -> None:
        self.data["failures"][pair] = {"at": int(time.time()), "error": error[:500]}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.data["failures"] = {k: v for k, v in self.data["failures"].items() if now - v["at"] < 7 * 86400}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # merges and backups ---------------------------------------------

    def save_merge(self, pair: str, record: dict) -> Path:
        folder = self.root / "merges"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%Y%m%dT%H%M%S')}-{pair}.json"
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        return path

    def cached_merge(self, pair: str) -> dict | None:
        """A successful earlier merge of exactly these two texts (other checkouts of one repo)."""
        folder = self.root / "merges"
        if not folder.is_dir():
            return None
        for path in sorted(folder.glob(f"*-{pair}.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("ok"):
                return record
        return None

    def backup(self, path: Path) -> Path | None:
        if not path.is_file():
            return None
        stamp = time.strftime("%Y%m%dT%H%M%S")
        dest = self.root / "backups" / stamp / str(path.resolve()).lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return dest

    def prune_backups(self) -> None:
        folder = self.root / "backups"
        if not folder.is_dir():
            return
        cutoff = time.time() - BACKUP_DAYS * 86400
        for entry in folder.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)
            except OSError:
                pass


@contextmanager
def run_lock(root: Path | None = None):
    """Yield True if this process holds the lock, False if another sync is running."""
    root = root or state_dir()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
