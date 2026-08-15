"""Interactive browser sessions — stateful tabs that survive across MCP tool calls.

Every existing tool is one-shot: acquire a tab, use it, release it within a
single call. An interactive flow (open -> click -> type -> screenshot) needs the
SAME page across calls, i.e. a pool tab held open between requests — exactly
the leak shape this codebase has been hardened against. This module makes that
safe.

Leak control (the acceptance criterion):
- Hard cap on concurrent sessions (``SESSION_MAX_SESSIONS``, default 4) so
  interactive use can never starve the search pool. Exceeding the cap raises
  ``SessionLimitError`` immediately — it never queues.
- Idle TTL (``SESSION_IDLE_TTL_SECS``, default 120) refreshed on every
  ``get_page()``; the background sweeper closes idle-expired sessions.
- Absolute lifetime (``SESSION_MAX_LIFETIME_SECS``, default 80) regardless of
  activity — a client polling forever cannot pin a tab.
- Exactly-once release: each session's tab is held by a dedicated owner task
  parked inside ``pool.acquire()``. The ONLY way the tab is released is that
  task exiting its async-with, which happens exactly once — normal close, idle
  expiry, lifetime cap, open failure, cancellation, or ``close_all()``.
  Double close is a harmless no-op.

Coexistence with the pool reaper (``browser.py`` force-closes any tab older
than ``_TAB_MAX_LIFETIME_SECS`` = 90s; the reaper closes the *page* but the
semaphore slot is only freed when the acquire() context exits):
- The default absolute lifetime is 80s (< 90s) so sessions always end on OUR
  terms — the owner task exits ``pool.acquire()`` cleanly, releasing page,
  context and semaphore slot before the reaper would fire.
- Defense in depth: should a page die under us anyway (operator raised the
  lifetime past the reaper window, browser restart/crash), ``get_page()`` and
  the sweeper detect the dead page, tear the session down (freeing the slot)
  and raise ``SessionExpiredError``. A dead page is NEVER handed back.

Wiring (orchestrator):
    from src.scraper.session import get_session_manager

    mgr = get_session_manager()
    sid = await mgr.open_session(pool, url="https://example.com")
    page = await mgr.get_page(sid)   # raises if unknown / expired / dead
    await mgr.close_session(sid)
    await mgr.close_all()            # in lifespan shutdown, before pool.stop()

Admin dashboard: ``mgr.stats()`` and ``mgr.list_sessions()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page

    from src.scraper.browser import BrowserPool

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SESSIONS = 4
_DEFAULT_IDLE_TTL_SECS = 120.0
# Kept UNDER browser.py's _TAB_MAX_LIFETIME_SECS (90s): the owner task exits
# pool.acquire() on its own terms before the pool reaper force-closes the tab.
_DEFAULT_MAX_LIFETIME_SECS = 80.0
_DEFAULT_SWEEP_INTERVAL_SECS = 5.0
_MIN_SWEEP_INTERVAL_SECS = 0.05  # floor: a 0/negative env value would hot-spin the loop
_OPEN_NAV_TIMEOUT_MS = 10_000  # matches the engines' single-navigation budget
_TERMINATE_WAIT_SECS = 5.0  # bound on waiting for the owner's tab teardown

_REASON_CLOSED = "closed"
_REASON_SHUTDOWN = "shutdown"
_REASON_OPEN_FAILED = "open-failed"
_REASON_IDLE = "idle-ttl-expired"
_REASON_LIFETIME = "max-lifetime-reached"
_REASON_DEAD = "tab-closed-externally"
_EXPIRY_REASONS = frozenset({_REASON_IDLE, _REASON_LIFETIME, _REASON_DEAD})
_REASON_TEXT = {
    _REASON_IDLE: "expired after being idle too long",
    _REASON_LIFETIME: "reached its maximum lifetime",
    _REASON_DEAD: "lost its browser tab (closed externally, e.g. by the pool reaper)",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class SessionError(RuntimeError):
    """Base error for interactive browser sessions."""


class SessionLimitError(SessionError):
    """Concurrent-session cap reached — close a session or raise the cap."""


class SessionNotFoundError(SessionError):
    """Session id is unknown (never opened, or already closed/expired)."""


class SessionExpiredError(SessionError):
    """Session ended (idle TTL, max lifetime, or its tab died) — reopen it."""


@dataclass
class _Session:
    session_id: str
    pool: BrowserPool
    created_at: float
    last_used_at: float
    idle_ttl: float
    max_lifetime: float
    ready: asyncio.Future[Page]
    close_event: asyncio.Event
    owner: asyncio.Task[None] | None = None
    page: Page | None = None
    url: str | None = None
    terminating: bool = False
    close_reason: str | None = None


class SessionManager:
    """Named interactive sessions, each owning exactly one pool tab."""

    def __init__(
        self,
        *,
        max_sessions: int | None = None,
        idle_ttl: float | None = None,
        max_lifetime: float | None = None,
        sweep_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_sessions = (
            max_sessions if max_sessions is not None else _env_int("SESSION_MAX_SESSIONS", _DEFAULT_MAX_SESSIONS)
        )
        self._idle_ttl = (
            idle_ttl if idle_ttl is not None else _env_float("SESSION_IDLE_TTL_SECS", _DEFAULT_IDLE_TTL_SECS)
        )
        self._max_lifetime = (
            max_lifetime
            if max_lifetime is not None
            else _env_float("SESSION_MAX_LIFETIME_SECS", _DEFAULT_MAX_LIFETIME_SECS)
        )
        self._sweep_interval = (
            sweep_interval
            if sweep_interval is not None
            else _env_float("SESSION_SWEEP_INTERVAL_SECS", _DEFAULT_SWEEP_INTERVAL_SECS)
        )
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._opened_total = 0
        self._closed_total = 0
        self._expired_total = 0
        self._rejected_total = 0
        self._dead_page_total = 0

    # ------------------------------------------------------------------ API

    async def open_session(self, pool: BrowserPool, url: str | None = None, ttl: float | None = None) -> str:
        """Open a new interactive session owning one pool tab; return its id.

        Raises ``SessionLimitError`` when the cap is full (immediately — never
        queues), or whatever ``pool.acquire()``/``goto`` raised (the tab is
        released before re-raising).
        """
        if ttl is not None and ttl <= 0:
            raise ValueError(f"ttl must be positive, got {ttl}")
        # Cap check + registration happen in one event-loop step (no await in
        # between), so concurrent opens cannot oversubscribe the cap.
        if len(self._sessions) >= self._max_sessions:
            self._rejected_total += 1
            raise SessionLimitError(
                f"interactive session cap reached ({len(self._sessions)}/{self._max_sessions}); "
                f"close a session first or raise SESSION_MAX_SESSIONS"
            )
        now = self._clock()
        session_id = uuid.uuid4().hex[:12]
        sess = _Session(
            session_id=session_id,
            pool=pool,
            created_at=now,
            last_used_at=now,
            idle_ttl=ttl if ttl is not None else self._idle_ttl,
            max_lifetime=self._max_lifetime,
            ready=asyncio.get_running_loop().create_future(),
            close_event=asyncio.Event(),
        )
        self._sessions[session_id] = sess
        sess.owner = asyncio.create_task(self._own_tab(sess), name=f"session-owner-{session_id}")
        try:
            page = await sess.ready
            if url is not None:
                await page.goto(url, wait_until="domcontentloaded", timeout=_OPEN_NAV_TIMEOUT_MS)
                sess.url = url
        except BaseException:
            # Any failure or cancellation here tears the session down so the
            # tab is released exactly once before the error propagates.
            await self._terminate(sess, reason=_REASON_OPEN_FAILED)
            raise
        sess.last_used_at = self._clock()  # idle clock starts once the page is ready
        self._opened_total += 1
        self._ensure_sweeper()
        logger.info(
            "[session] opened %s (url=%s, idle_ttl=%.0fs, active=%d/%d)",
            session_id,
            url,
            sess.idle_ttl,
            len(self._sessions),
            self._max_sessions,
        )
        return session_id

    async def get_page(self, session_id: str) -> Page:
        """Return the session's live page, refreshing its idle TTL.

        NEVER returns a closed/dead page: expiry and external tab death are
        detected here, the session is torn down (releasing its pool slot) and
        ``SessionExpiredError`` is raised instead.
        """
        sess = self._sessions.get(session_id)
        if sess is None:
            raise SessionNotFoundError(
                f"unknown session {session_id!r} — never opened, or already closed/expired; open a new session"
            )
        now = self._clock()
        reason = self._expiry_reason(sess, now)
        if reason is None and self._tab_dead(sess):
            self._dead_page_total += 1
            reason = _REASON_DEAD
        if reason is not None:
            await self._terminate(sess, reason=reason)
            raise SessionExpiredError(f"session {session_id} {_REASON_TEXT[reason]}; open a new session")
        sess.last_used_at = now
        assert sess.page is not None  # _tab_dead() ruled out None/closed
        return sess.page

    async def close_session(self, session_id: str) -> bool:
        """Close a session and release its tab. False if unknown/already closed."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return False
        return await self._terminate(sess, reason=_REASON_CLOSED)

    def session_info(self, session_id: str) -> dict[str, Any] | None:
        sess = self._sessions.get(session_id)
        return None if sess is None else self._info(sess, self._clock())

    def list_sessions(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [self._info(s, now) for s in self._sessions.values()]

    async def close_all(self) -> int:
        """Close every session and stop the sweeper — call on server shutdown."""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None and not sweeper.done():
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        closed = 0
        for sess in list(self._sessions.values()):
            if await self._terminate(sess, reason=_REASON_SHUTDOWN):
                closed += 1
        return closed

    async def sweep_expired(self) -> int:
        """One sweep pass: close idle-expired / over-lifetime / dead-tab sessions.

        Runs periodically in the background sweeper; also directly callable
        (tests, admin). Returns the number of sessions closed.
        """
        now = self._clock()
        closed = 0
        for sess in list(self._sessions.values()):
            reason = self._expiry_reason(sess, now)
            if reason is None and self._tab_dead(sess):
                self._dead_page_total += 1
                reason = _REASON_DEAD
            if reason is not None and await self._terminate(sess, reason=reason):
                closed += 1
        return closed

    def stats(self) -> dict[str, Any]:
        """Counters for the admin dashboard (active == opened - closed)."""
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": self._max_sessions,
            "opened_total": self._opened_total,
            "closed_total": self._closed_total,
            "expired_total": self._expired_total,
            "rejected_total": self._rejected_total,
            "dead_page_total": self._dead_page_total,
            "idle_ttl_secs": self._idle_ttl,
            "max_lifetime_secs": self._max_lifetime,
        }

    # ------------------------------------------------------------ internals

    async def _own_tab(self, sess: _Session) -> None:
        """Owner task: enters ``pool.acquire()`` and holds the tab until told to stop.

        The tab is released exactly once — when this task exits the async-with:
        on close_event (close / expiry / shutdown), on cancellation (opener
        cancelled pre-handoff, interpreter shutdown cancelling tasks), or when
        acquire itself fails. Every teardown path funnels through here.
        """
        try:
            async with sess.pool.acquire(label=f"session:{sess.session_id}", session=sess.session_id) as page:
                sess.page = page
                if sess.ready.done():
                    return  # opener vanished (cancelled) before handoff — release now
                sess.ready.set_result(page)
                await sess.close_event.wait()
        except asyncio.CancelledError:
            if not sess.ready.done():
                sess.ready.set_exception(SessionError(f"session {sess.session_id} was cancelled while opening"))
            raise
        except Exception as exc:  # acquire failed, or tab teardown raised
            if not sess.ready.done():
                sess.ready.set_exception(exc)
            else:
                logger.warning("[session] %s owner error: %s", sess.session_id, exc)
        finally:
            sess.page = None

    async def _terminate(self, sess: _Session, *, reason: str) -> bool:
        """Tear a session down. Idempotent; safe under concurrent double calls.

        The flag+event flip is synchronous, so once set the owner task WILL
        exit and release the tab even if this caller is itself cancelled
        mid-await — release never depends on the terminator surviving.
        """
        if sess.terminating:
            return False
        sess.terminating = True
        sess.close_reason = reason
        self._sessions.pop(sess.session_id, None)
        sess.close_event.set()
        owner = sess.owner
        if owner is not None:
            if sess.page is None and not owner.done():
                # Owner never completed the handoff — it is still inside
                # pool.acquire()'s __aenter__; unwind it instead of waiting.
                owner.cancel()
            # Bounded + shielded. Release is already guaranteed by the
            # close_event flip above, so this wait is pure bookkeeping: a
            # wedged page.close() must never block the closer, the sweeper or
            # shutdown, and our own cancellation must not abort the owner's
            # teardown. gather(return_exceptions=True) absorbs its outcome.
            waiter = asyncio.gather(owner, return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=_TERMINATE_WAIT_SECS)
            except asyncio.TimeoutError:
                logger.warning(
                    "[session] %s tab teardown still running after %.0fs — releasing asynchronously",
                    sess.session_id,
                    _TERMINATE_WAIT_SECS,
                )
        if reason != _REASON_OPEN_FAILED:
            self._closed_total += 1
        if reason in _EXPIRY_REASONS:
            self._expired_total += 1
        logger.info(
            "[session] closed %s (%s, active=%d/%d)",
            sess.session_id,
            reason,
            len(self._sessions),
            self._max_sessions,
        )
        return True

    def _expiry_reason(self, sess: _Session, now: float) -> str | None:
        if now - sess.created_at >= sess.max_lifetime:
            return _REASON_LIFETIME
        if now - sess.last_used_at >= sess.idle_ttl:
            return _REASON_IDLE
        return None

    @staticmethod
    def _tab_dead(sess: _Session) -> bool:
        if not sess.ready.done():
            return False  # still opening — the owner has not handed the page over yet
        if sess.owner is not None and sess.owner.done():
            return True  # owner exited => tab already released
        page = sess.page
        return page is None or bool(page.is_closed())

    def _info(self, sess: _Session, now: float) -> dict[str, Any]:
        age = now - sess.created_at
        idle = now - sess.last_used_at
        return {
            "session_id": sess.session_id,
            "url": sess.url,
            "alive": not self._tab_dead(sess),
            "age_secs": round(age, 1),
            "idle_secs": round(idle, 1),
            "idle_ttl_secs": sess.idle_ttl,
            "max_lifetime_secs": sess.max_lifetime,
            "expires_in_secs": round(max(0.0, min(sess.max_lifetime - age, sess.idle_ttl - idle)), 1),
        }

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="session-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(self._sweep_interval, _MIN_SWEEP_INTERVAL_SECS))
                await self.sweep_expired()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # the sweeper must never die
                logger.debug("[session] sweeper error: %s", exc)


_default_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Process-wide manager for MCP tool wiring (env config read at first use)."""
    global _default_manager
    if _default_manager is None:
        _default_manager = SessionManager()
    return _default_manager
