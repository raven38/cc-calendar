"""FastAPI app: JSON API, server-sent events, and the static frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from watchfiles import awatch

from . import __version__, gitinfo
from .logview import build_entries, find_entries, log_events
from .notes import NOTE_LIMIT, TAGS_PER_SESSION, Notes, NotesUnavailable
from .parser import SessionAcc
from .search import MIN_QUERY, SearchIndex, snippet
from .stats import log_stats
from .store import ClaudeDir, CodexDir, CollectorDir, Store
from .tools import tool_usage

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
SEARCH_TEXT_LIMIT = 4000
QUERY_LIMIT = 500
# Always revalidate: without this, browsers cache ES modules heuristically and keep
# running stale code after an upgrade.
NO_CACHE = {"Cache-Control": "no-cache"}
# Reject requests whose Host is not loopback: a page on another site could otherwise
# rebind its domain to 127.0.0.1 (DNS rebinding) and read transcripts through the browser.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
# Transcripts can hold HTML and Markdown that point anywhere (`![](https://…)`, a style with
# url(…)). Let the browser load nothing from other origins, so viewing a log never contacts
# another host.
CSP = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "frame-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


def project_name(s: SessionAcc) -> str:
    if s.cwd:
        return Path(s.cwd).name or s.cwd
    return s.project_dir.lstrip("-").rsplit("-", 1)[-1]


def summary(
    s: SessionAcc, live: dict | None, gap_ms: int, continued_from: str | None, notes: Notes
) -> dict:
    status, _ = s.state(live)
    cost, estimated = s.cost()
    models = s.models()
    cache_hit, cache_saved = s.cache_stats()
    search = " ".join([s.title(), *(p["text"][:300] for p in s.prompts)])[:SEARCH_TEXT_LIMIT]
    return {
        "id": s.session_id,
        "title": s.title(),
        "project": s.cwd or s.project_dir,
        "project_name": project_name(s),
        "source": s.source,
        "branch": s.git_branch,
        "status": status,
        "start": s.start,
        "end": s.end,
        "segments": s.segments(gap_ms),
        "density": s.density(),
        "cost_density": s.cost_density(),
        "marks": s.marks(),
        "prompt_count": len(s.prompts),
        "tokens": s.tokens(),
        "cost": round(cost, 4),
        "cost_estimated": estimated,
        "cache_hit": cache_hit,
        "cache_saved": round(cache_saved, 4),
        "model": models[0] if models else None,
        "effort": s.effort(),
        "version": s.version,  # Claude Code version
        "commit_list": [
            {"ts": c.get("ts"), "sha": c.get("sha"), "subject": c.get("subject")}
            for c in gitinfo.resolve_commits(s.commits)
        ],
        "repo_url": gitinfo.repo_url(s.cwd),
        "continued_in": s.continued_in,
        "continued_from": continued_from,
        "search": search,
        **notes.get(s.session_id),  # note, tags
    }


def canonical_summary(imported, gap_ms: int, notes: Notes) -> dict:
    """Adapt a canonical source to the existing list/calendar response shape."""
    session = imported.session
    events = imported.events
    times = sorted(event.ts for event in events if event.ts is not None)
    segments: list[list[int]] = []
    if times:
        segments = [[times[0], times[0]]]
        for timestamp in times[1:]:
            if timestamp - segments[-1][1] > gap_ms:
                segments.append([timestamp, timestamp])
            else:
                segments[-1][1] = timestamp
    source = imported.source
    tokens = source.tokens() if hasattr(source, "tokens") else None
    model = getattr(source, "model", None)
    effort = getattr(source, "effort", None)
    prompts = [event for event in events if event.kind == "user_message"]
    search = " ".join([session.title or "", *(event.text or "" for event in events if event.text)])[
        :SEARCH_TEXT_LIMIT
    ]
    return {
        "id": session.id,
        "title": session.title,
        "project": session.cwd or session.project_id,
        "project_name": imported.project.name if imported.project else session.project_id,
        "source": session.source_instance,
        "branch": session.branch,
        "status": session.status,
        "start": session.start,
        "end": session.end,
        "segments": segments,
        "density": dict(
            Counter(event.ts // (10 * 60 * 1000) for event in events if event.ts is not None)
        ),
        "cost_density": {},
        "marks": [],
        "prompt_count": len(prompts),
        "tokens": tokens,
        "cost": None,
        "cost_estimated": False,
        "cache_hit": None,
        "cache_saved": None,
        "model": model,
        "effort": effort,
        "version": None,
        "commit_list": [],
        "repo_url": None,
        "continued_in": None,
        "continued_from": None,
        "search": search,
        **notes.get(session.id),
    }


def canonical_detail(imported, gap_ms: int, notes: Notes) -> dict:
    out = canonical_summary(imported, gap_ms, notes)
    session = imported.session
    source = imported.source
    out.update(
        {
            "cwd": session.cwd,
            "models": [source.model] if getattr(source, "model", None) else [],
            "efforts": {source.effort: 1} if getattr(source, "effort", None) else {},
            "compactions": [],
            "permission_mode": None,
            "context_pct": None,
            "checks": {
                "turn_ended": None,
                "no_background": None,
                "clean_exit": None,
                "committed": None,
            },
            "prompts": [
                {
                    "uuid": event.id,
                    "ts": event.ts,
                    "text": event.text or "",
                    "kind": "prompt",
                    "commits": [],
                }
                for event in imported.events
                if event.kind == "user_message"
            ],
            "commits": [],
            "files": [],
            "prs": [],
            "subagents": [],
            "background": [],
            "cost_state": None,
            "canonical_events": [asdict(event) for event in imported.events],
            "canonical_project": asdict(imported.project) if imported.project else None,
        }
    )
    return out


def canonical_log_entry(event, index: int) -> dict:
    """Adapt canonical events to the transcript viewer's existing entry contract."""
    kind = {
        "user_message": "user",
        "assistant_message": "assistant",
        "tool_call": "tool_use",
        "tool_result": "tool_result",
        "api_error": "error",
        "status": "meta",
    }.get(event.kind, event.kind)
    entry = {
        "i": index,
        "ts": event.ts,
        "kind": kind,
        "text": event.text or event.tool_output or "",
        "role": event.role,
        "model": event.model,
    }
    if event.kind == "tool_call":
        entry.update(
            {
                "id": event.tool_call_id or f"canonical-tool-{index}",
                "name": event.tool_name or "unknown",
                "input": _canonical_log_value(event.tool_input),
            }
        )
    elif event.kind == "tool_result":
        entry["tool_use_id"] = event.tool_call_id
        entry["is_error"] = event.is_error
    elif event.kind in ("status", "api_error", "interrupt"):
        metadata = event.metadata or {}
        entry["text"] = metadata.get("provider_type") or metadata.get("status") or entry["text"]
    return entry


def _canonical_log_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def detail(
    s: SessionAcc, live: dict | None, gap_ms: int, continued_from: str | None, notes: Notes
) -> dict:
    out = summary(s, live, gap_ms, continued_from, notes)
    _, checks = s.state(live)
    commits = gitinfo.resolve_commits(s.commits)
    checks["committed"] = gitinfo.working_tree_clean(s.cwd)
    prompts = [dict(p, commits=[]) for p in s.prompts]
    for c in commits:
        owner = None
        for p in prompts:
            if p["ts"] is not None and c.get("ts") is not None and p["ts"] <= c["ts"]:
                owner = p
        if owner is not None:
            owner["commits"].append(c.get("sha") or c.get("subject"))
    out.update(
        {
            "cwd": s.cwd,
            "models": s.models(),
            "efforts": s.effort_mix(),
            "compactions": s.compactions,
            "permission_mode": s.permission_mode,
            "context_pct": s.context_pct(),
            "checks": checks,
            "prompts": prompts,
            "commits": commits,
            "files": [{"path": p, "count": n} for p, n in s.files.most_common()],
            "prs": list(s.prs.values()),
            "subagents": sorted(
                (sa.to_dict() for sa in s.subagents.values()), key=lambda d: d["start"] or 0
            ),
            "background": list(s.background.values()),
            "cost_state": {
                k: s.cost_state.get(k)
                for k in ("totalDuration", "totalLinesAdded", "totalLinesRemoved", "modelUsage")
            }
            if s.cost_state
            else None,
        }
    )
    return out


class ToolsQuery(BaseModel):
    start: int
    end: int
    sessions: list[str] = Field(max_length=100_000)


class NotesUpdate(BaseModel):
    note: str = Field("", max_length=NOTE_LIMIT * 2)  # checked exactly after trimming
    tags: list[str] = Field(default_factory=list, max_length=TAGS_PER_SESSION * 5)


def same_origin_json(request: Request) -> None:
    """Guard for writes: a page on another site must not be able to change notes.

    Requiring a JSON body makes browsers send a CORS preflight, which this server never
    answers with permission; an Origin other than this server is refused outright.
    """
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "expected application/json")
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).hostname not in ALLOWED_HOSTS):
        raise HTTPException(403, "cross-origin request refused")


class Broadcaster:
    def __init__(self) -> None:
        self.queues: set[asyncio.Queue] = set()

    def publish(self, payload: dict) -> None:
        for q in list(self.queues):
            q.put_nowait(payload)


def open_index(path: Path | None) -> SearchIndex | None:
    """The full-text index at `path`, else in memory; None if this SQLite cannot do it."""
    try:
        return SearchIndex(path)
    except (OSError, sqlite3.Error) as e:
        if path is not None:
            log.warning("keeping the search index in memory: cannot open %s: %s", path, e)
    try:
        return SearchIndex(None)
    except sqlite3.Error as e:  # no FTS5 or no trigram tokenizer (SQLite < 3.34)
        log.warning("full-text search unavailable: %s", e)
        return None


def create_app(
    dirs: Path | list[ClaudeDir],
    watch: bool = True,
    notes_path: Path | None = None,
    index_path: Path | None = None,
    codex_dirs: list[CodexDir] | None = None,
    collector_dirs: list[CollectorDir] | None = None,
) -> FastAPI:
    """`notes_path=None` and `index_path=None` keep notes and the search index in memory only
    (used by tests)."""
    store = Store(
        dirs,
        open_index(index_path),
        codex_dirs=codex_dirs,
        collector_dirs=collector_dirs,
    )
    notes = Notes(notes_path)
    broadcaster = Broadcaster()
    sessions_dirs = {d.sessions_dir for d in store.dirs}

    async def watcher() -> None:
        targets = [
            str(p) for d in store.dirs for p in (d.projects_dir, d.sessions_dir) if p.is_dir()
        ]
        if not targets:
            return
        async for changes in awatch(*targets, recursive=True):
            changed: set[str] = set()
            live_changed = False
            for _, raw in changes:
                path = Path(raw)
                if path.parent in sessions_dirs:
                    live_changed = True
                    continue
                if path.suffix == ".jsonl" or path.name.endswith(".meta.json"):
                    sid = await asyncio.to_thread(store.update_file, path)
                    if sid:
                        changed.add(sid)
            if changed or live_changed:
                broadcaster.publish({"sessions": sorted(changed), "live": live_changed})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(store.scan)
        task = asyncio.create_task(watcher()) if watch else None
        yield
        if task:
            task.cancel()
        if store.index is not None:
            store.index.close()

    app = FastAPI(title="cc-calendar", version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.middleware("http")
    async def content_security_policy(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        return response

    app.state.store = store
    app.state.notes = notes

    def get_session(sid: str) -> SessionAcc:
        s = store.sessions.get(sid)
        if s is None:
            raise HTTPException(404, "session not found")
        return s

    @app.get("/api/sessions")
    def list_sessions(gap: int = Query(15, ge=1, le=24 * 60)) -> dict:
        live = store.live_sessions()
        cont = store.continued_from()
        notes.refresh()
        with store.lock:
            items = [
                summary(s, live.get(s.session_id), gap * 60_000, cont.get(s.session_id), notes)
                for s in store.sessions.values()
                if s.start is not None
            ]
            items.extend(
                canonical_summary(imported, gap * 60_000, notes)
                for imported in store.codex_sessions.values()
                if imported.session.start is not None
            )
            items.extend(
                canonical_summary(imported, gap * 60_000, notes)
                for imported in store.collector_sessions.values()
                if imported.session.start is not None
            )
        return {
            "sessions": items,
            "claude_dirs": [{"name": d.name, "path": str(d.path)} for d in store.dirs],
            "sources": store.source_catalog(),
            "version": __version__,
            "tags": notes.all_tags(),
            "notes_error": notes.error,
        }

    @app.get("/api/search")
    def search(q: str = Query(..., max_length=QUERY_LIMIT)) -> dict:
        """Sessions whose transcripts contain `q`: per session, the number of matching
        messages, tool calls and outputs (and how many of them are in subagents), and the
        first of them."""
        if store.index is None:
            raise HTTPException(503, "full-text search needs SQLite 3.34 or later with FTS5")
        found = store.index.search(q)
        hits: dict[str, dict] = {}
        with store.lock:
            for f in found:
                s = store.sessions.get(f.session_id)
                if s is None:
                    continue
                # Hits in another copy of the session would not be in the log the viewer opens.
                sa = s.subagents.get(f.agent_id) if f.agent_id else None
                if f.path != (sa.path if f.agent_id else s.path):
                    continue
                hit = hits.get(f.session_id)
                count = f.count + (hit["count"] if hit else 0)
                in_subagents = (f.count if f.agent_id else 0) + (hit["in_subagents"] if hit else 0)
                if hit is None or (f.ts is not None and (hit["ts"] is None or f.ts < hit["ts"])):
                    hit = {
                        "ts": f.ts,
                        "kind": f.kind,
                        "name": f.name,
                        "agent": f.agent_id,
                        "snippet": snippet(f.text, q),
                    }
                hit["count"] = count
                hit["in_subagents"] = in_subagents
                hits[f.session_id] = hit
        # While transcripts are still being indexed, the hits are incomplete.
        return {"query": q, "min_length": MIN_QUERY, "pending": store.index.pending(), "hits": hits}

    @app.get("/api/sessions/{sid}")
    def session_detail(sid: str, gap: int = Query(15, ge=1, le=24 * 60)) -> dict:
        live = store.live_sessions()
        notes.refresh()
        with store.lock:
            imported = store.codex_sessions.get(sid) or store.collector_sessions.get(sid)
            if imported is not None:
                return canonical_detail(imported, gap * 60_000, notes)
            s = get_session(sid)
            out = detail(s, live.get(sid), gap * 60_000, store.continued_from().get(sid), notes)
        out["also_in"] = store.also_in(sid)
        return out

    @app.get("/api/sessions/{sid}/canonical")
    def canonical_session(sid: str) -> dict:
        imported = store.canonical_import(sid, store.live_sessions().get(sid))
        if imported is None:
            raise HTTPException(404, "session not found")
        return {
            "session": asdict(imported.session),
            "events": [asdict(event) for event in imported.events],
            "assignment": asdict(imported.assignment),
            "project": asdict(imported.project) if imported.project is not None else None,
        }

    @app.put("/api/sessions/{sid}/notes", dependencies=[Depends(same_origin_json)])
    async def update_notes(sid: str, body: NotesUpdate) -> dict:
        get_session(sid)
        try:
            out = await asyncio.to_thread(notes.set, sid, body.note, body.tags)
        except NotesUnavailable as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"could not save notes: {e}") from e
        # Other open tabs reload the list and this session's details.
        broadcaster.publish({"sessions": [sid], "live": False})
        return out

    def log_path(sid: str, agent: str | None) -> tuple[Path, str | None]:
        s = get_session(sid)
        if not agent:
            return Path(s.path), sid
        sa = s.subagents.get(agent)
        if sa is None or sa.path is None:
            raise HTTPException(404, "subagent log not found")
        return Path(sa.path), None

    @app.get("/api/sessions/{sid}/stats")
    def session_stats(sid: str, agent: str | None = None) -> dict:
        path, filter_sid = log_path(sid, agent)
        try:
            return log_stats(path, filter_sid)
        except OSError as e:
            raise HTTPException(404, "log file not readable") from e

    @app.get("/api/sessions/{sid}/log")
    def session_log(
        sid: str,
        agent: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(300, ge=1, le=2000),
        q: str | None = Query(None, max_length=QUERY_LIMIT),
    ) -> dict:
        imported = store.codex_sessions.get(sid) or store.collector_sessions.get(sid)
        if imported is not None:
            entries = [canonical_log_entry(event, i) for i, event in enumerate(imported.events)]
            return {
                "total": len(entries),
                "offset": offset,
                "entries": entries[offset : offset + limit],
                **({"events": []} if offset == 0 else {}),
            }
        path, filter_sid = log_path(sid, agent)
        try:
            entries = build_entries(path, filter_sid)
        except OSError as e:
            raise HTTPException(404, "log file not readable") from e
        out = {
            "total": len(entries),
            "offset": offset,
            "entries": entries[offset : offset + limit],
        }
        if offset == 0:
            # Lets the viewer jump to an event that is not loaded yet.
            out["events"] = log_events(entries)
            if q:
                # Where a full-text search matched, to jump there.
                out["matches"] = find_entries(entries, q)
        return out

    # POST: the filtered session ids can be too many for a query string.
    @app.post("/api/tools")
    def tools(q: ToolsQuery) -> dict:
        with store.lock:
            picked = [store.sessions[i] for i in q.sessions if i in store.sessions]
            return tool_usage(picked, q.start, q.end)

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue()
        broadcaster.queues.add(queue)

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                broadcaster.queues.discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers=NO_CACHE)

    app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")
    return app
