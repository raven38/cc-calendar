"""In-memory index of all sessions, updated incrementally as transcript files grow."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .canonical.claude_code import CanonicalImport, ClaudeCodeAdapter
from .canonical.codex_local import CodexLocalAdapter
from .parser import SessionAcc
from .search import SearchIndex

log = logging.getLogger(__name__)


@dataclass
class FileState:
    offset: int = 0
    inode: int | None = None


@dataclass(frozen=True)
class ClaudeDir:
    """A Claude Code config directory and the short name it is shown under."""

    name: str
    path: Path

    @property
    def projects_dir(self) -> Path:
        return self.path / "projects"

    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"


@dataclass(frozen=True)
class CodexDir:
    """A Codex home directory explicitly selected for local read-only import."""

    name: str
    path: Path

    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"

    @property
    def archived_sessions_dir(self) -> Path:
        return self.path / "archived_sessions"


class Store:
    def __init__(
        self,
        dirs: Path | list[ClaudeDir],
        index: SearchIndex | None = None,
        codex_dirs: list[CodexDir] | None = None,
    ):
        self.dirs = [ClaudeDir("local", dirs)] if isinstance(dirs, Path) else list(dirs)
        self.codex_dirs = list(codex_dirs or [])
        self.index = index  # full-text index, told about every transcript that changes
        # The same session can be in several directories (e.g. synced copies): every copy
        # is indexed, and `sessions` holds the one with the newest activity.
        self.copies: dict[str, dict[str, SessionAcc]] = {}  # sid -> dir name -> session
        self.sessions: dict[str, SessionAcc] = {}
        self.codex_sessions: dict[str, CanonicalImport] = {}
        self.files: dict[Path, FileState] = {}
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ scanning

    def scan(self) -> None:
        with self.lock:
            for d in self.dirs:
                projects = d.projects_dir
                if not projects.is_dir():
                    log.warning("no projects directory at %s", projects)
                    continue
                for path in sorted(projects.glob("*/*.jsonl")):
                    self.update_file(path)
                for path in sorted(projects.glob("*/*/subagents/*.meta.json")):
                    self.update_file(path)
                for path in sorted(projects.glob("*/*/subagents/*.jsonl")):
                    self.update_file(path)
            self._scan_codex()
            if self.index is not None:
                # Forget transcripts Claude Code has deleted since the last run.
                keep = {str(p) for p in self.files}
                self.index.prune([d.projects_dir for d in self.dirs], keep)

    def _scan_codex(self) -> None:
        """Read explicitly selected Codex rollout files without enabling any network source."""
        for source in self.codex_dirs:
            adapter = CodexLocalAdapter(source_instance=source.name)
            roots = (source.sessions_dir, source.archived_sessions_dir)
            for root in roots:
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*.jsonl")):
                    try:
                        imported = adapter.import_jsonl(path)
                    except (OSError, ValueError) as exc:
                        log.warning("could not read Codex rollout %s: %s", path, exc)
                        continue
                    if imported is None:
                        continue
                    current = self.codex_sessions.get(imported.session.id)
                    if current is None or (imported.session.end or -1) >= (
                        current.session.end or -1
                    ):
                        self.codex_sessions[imported.session.id] = imported

    def source_catalog(self) -> list[dict[str, str]]:
        """Return the local sources selected for this store, for UI selectors."""
        return [
            *({"name": d.name, "path": str(d.path), "kind": "claude-code"} for d in self.dirs),
            *({"name": d.name, "path": str(d.path), "kind": "codex"} for d in self.codex_dirs),
        ]

    def classify(self, path: Path) -> tuple[ClaudeDir, str, str, str | None] | None:
        """-> (dir, kind, session_id, agent_id) for a path under a projects/, or None."""
        for d in self.dirs:
            try:
                rel = path.relative_to(d.projects_dir)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) == 2 and parts[1].endswith(".jsonl"):
                return d, "main", parts[1][: -len(".jsonl")], None
            if len(parts) == 4 and parts[2] == "subagents" and parts[3].startswith("agent-"):
                name = parts[3][len("agent-") :]
                if name.endswith(".meta.json"):
                    return d, "meta", parts[1], name[: -len(".meta.json")]
                if name.endswith(".jsonl"):
                    return d, "sub", parts[1], name[: -len(".jsonl")]
            return None
        return None

    def update_file(self, path: Path) -> str | None:
        """Read new bytes from `path`; return the affected session id."""
        info = self.classify(path)
        if info is None:
            return None
        d, kind, sid, agent_id = info
        with self.lock:
            try:
                return self._update(d, kind, sid, agent_id, path)
            finally:
                self._pick(sid)

    def _update(
        self, d: ClaudeDir, kind: str, sid: str, agent_id: str | None, path: Path
    ) -> str | None:
        session = self._session(d, sid, path)
        if kind == "meta":
            try:
                session.apply_subagent_meta(agent_id, json.loads(path.read_text()))
            except (OSError, ValueError):
                pass
            return sid
        try:
            st = path.stat()
        except OSError:  # deleted or dangling symlink
            return None
        state = self.files.get(path)
        if state is None or st.st_size < state.offset or st.st_ino != state.inode:
            if state is not None and kind == "main":
                # Rewritten from scratch: rebuild the session but keep subagent data.
                subagents = session.subagents
                session = self._session(d, sid, path, reset=True)
                session.subagents = subagents
            state = FileState(0, st.st_ino)
            self.files[path] = state
        if st.st_size == state.offset:
            return sid
        if self.index is not None:
            self.index.enqueue(path, sid, None if kind == "main" else agent_id)
        for rec in self._read_new(path, state):
            if kind == "main":
                session.feed(rec)
            else:
                session.feed_subagent(agent_id, str(path), rec)
        return sid

    def _session(self, d: ClaudeDir, sid: str, path: Path, reset: bool = False) -> SessionAcc:
        copies = self.copies.setdefault(sid, {})
        s = copies.get(d.name)
        if s is None or reset:
            project_dir = path.relative_to(d.projects_dir).parts[0]
            main_path = d.projects_dir / project_dir / f"{sid}.jsonl"
            s = SessionAcc(
                session_id=sid, path=str(main_path), project_dir=project_dir, source=d.name
            )
            copies[d.name] = s
        return s

    def _pick(self, sid: str) -> None:
        """Show the copy with the newest activity; ties go to the earlier directory."""
        copies = self.copies.get(sid)
        if not copies:
            return
        if len(copies) == 1:
            self.sessions[sid] = next(iter(copies.values()))
            return
        ordered = [copies[d.name] for d in self.dirs if d.name in copies]
        self.sessions[sid] = max(ordered, key=lambda s: s.end if s.end is not None else -1)

    def also_in(self, sid: str) -> list[str]:
        """Names of the other directories that hold a copy of the shown session."""
        with self.lock:
            shown = self.sessions.get(sid)
            return [
                d.name
                for d in self.dirs
                if d.name in self.copies.get(sid, {}) and (shown is None or d.name != shown.source)
            ]

    def canonical_import(self, sid: str, live: dict | None = None) -> CanonicalImport | None:
        """Return a read-only canonical projection of a stored Claude session.

        The existing ``SessionAcc`` remains the live analytics source. Events are
        projected from the selected transcript copy only when this method is
        requested, so canonicalization cannot mutate or replace the existing index.
        """
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                return None
            adapter = ClaudeCodeAdapter(source_instance=f"claude-code-{session.source or 'local'}")
            try:
                imported = adapter.import_jsonl(Path(session.path), live=live)
            except OSError:
                imported = adapter.from_acc(session, live=live)
            else:
                # Preserve the selected, incrementally parsed accumulator for
                # existing analytics and only use the fresh read for events.
                imported = adapter.from_acc(session, imported.events, live=live)
            return imported

    @staticmethod
    def _read_new(path: Path, state: FileState):
        with open(path, "rb") as f:
            f.seek(state.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        state.offset += end + 1
        for line in data[: end + 1].splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec

    # ------------------------------------------------------------------ live status

    def live_sessions(self) -> dict[str, dict]:
        """sessionId -> live process info, for Claude Code processes that are still alive."""
        out: dict[str, dict] = {}
        for d in self.dirs:
            if not d.sessions_dir.is_dir():
                continue
            for p in d.sessions_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                pid, sid = data.get("pid"), data.get("sessionId")
                if not isinstance(pid, int) or not sid or sid in out or not _pid_alive(pid):
                    continue
                # A synced directory from another machine can name a PID that happens to be
                # alive here; the recorded start time tells the two processes apart.
                if not _same_process(pid, data.get("procStart")):
                    continue
                out[sid] = data
        return out

    # ------------------------------------------------------------------ continuation

    def continued_from(self) -> dict[str, str]:
        with self.lock:
            return {s.continued_in: s.session_id for s in self.sessions.values() if s.continued_in}


PROC_START_TTL = 60.0
_proc_starts: dict[int, tuple[float, str | None]] = {}


def _same_process(pid: int, proc_start: object) -> bool:
    """Whether `pid` was started at `proc_start` (as `ps -o lstart` prints it, in UTC).

    Records without a start time, or a `ps` that cannot answer, fall back to the PID alone.
    """
    if not isinstance(proc_start, str) or not proc_start.strip():
        return True
    actual = _proc_start(pid)
    return actual is None or actual.split() == proc_start.split()


def _proc_start(pid: int) -> str | None:
    now = time.monotonic()
    hit = _proc_starts.get(pid)
    if hit is not None and now - hit[0] < PROC_START_TTL:
        return hit[1]
    try:
        res = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
        start = res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        start = ""
    _proc_starts[pid] = (now, start or None)
    return start or None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
