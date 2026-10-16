"""Shared helpers: the database handle, page state, messages, small widgets.

Every button that changes something goes through act(): do it, then refresh the page, or
show why it was refused, in the same plain words everywhere.
"""
from collections.abc import Callable
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from studio.config import load_settings
from studio.db import EpisodeRepository
from studio.errors import AgentOutputError, StudioError, ValidationFailed
from studio.logging_utils import setup_logging


@st.cache_resource
def get_repo() -> EpisodeRepository:
    settings = load_settings()
    setup_logging(settings.log_dir, settings.log_level)
    repo = EpisodeRepository(settings)
    repo.auto_backup()  # the dashboard is usually left open: also checked on each page load
    return repo


class _Current:
    """Forwards to the CURRENT repository on every use.

    Streamlit re-runs dashboard.py on every click, but these ui modules stay imported.
    A plain `repo = get_repo()` here would keep the first database forever (e.g. across
    tests that each use a temporary database); this forwarder always asks get_repo().
    """

    def __init__(self, pick):
        self._pick = pick

    def __getattr__(self, name):
        return getattr(self._pick(get_repo()), name)


repo: EpisodeRepository = _Current(lambda r: r)  # type: ignore[assignment]
settings = _Current(lambda r: r.settings)
state = st.session_state


ACTOR = "human"
BUILT_IN_VOICE = "Kokoro (built in)"  # the voice tool generated inside the app (studio/tts.py)
VERDICT_BADGE = {"PASS": "✅ **PASS**", "REVIEW": "⚠️ **REVIEW**",
                 "SUGGEST_BLOCK": "⛔ **SUGGEST_BLOCK**"}


def open_episode(episode_id: str) -> None:
    state.episode_id = episode_id
    state.page = "Episode"


# ---------------------------------------------------------------- errors and messages
def error_text(e: Exception) -> str:
    """Why something was refused, in plain words (the rule's code is kept for reference)."""
    if isinstance(e, AgentOutputError):
        return "The answer could not be used:\n\n" + "\n".join(
            f"{n}. {p}" for n, p in enumerate(e.problems, start=1))
    if isinstance(e, ValidationFailed):
        return "Not yet:\n\n" + "\n".join(f"- {i.message} (`{i.code}`)" for i in e.issues)
    if isinstance(e, ValidationError):
        return "\n".join(f"- {err['loc'][0]}: {err['msg']}" for err in e.errors())
    return str(e)


def show_error(e: Exception) -> None:
    st.error(error_text(e))


_SHOW = {"success": st.success, "warning": st.warning, "error": st.error}


def flash(kind: str, message: str, slot: str = "page") -> None:
    """A message shown once after the next refresh, in a place on the page (`slot`)."""
    state[f"flash_{slot}"] = (kind, message)


def flash_error(e: Exception, slot: str = "page") -> None:
    flash("error", error_text(e), slot)


def show_flash(slot: str = "page") -> None:
    kind, message = state.pop(f"flash_{slot}", (None, None))
    if kind:
        _SHOW.get(kind, st.info)(message)


def act(fn: Callable, *args, then: Callable[[Any], None] | None = None, **kwargs) -> None:
    """Run a change from a button or form: refresh the page when it worked, show the reason
    when the rules refused. `then(result)` runs just before the refresh (e.g. to flash a
    message or remember a choice)."""
    try:
        result = fn(*args, **kwargs)
    except (StudioError, ValidationError) as e:
        show_error(e)
        return
    if then is not None:
        then(result)
    st.rerun()


# ---------------------------------------------------------------------- small widgets
def show_table(rows: list[dict], empty: str) -> None:
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption(empty)


def show_sources(rows: list[dict], empty: str) -> None:
    """The sources table, with clickable links."""
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("url")})
    else:
        st.caption(empty)


def length_mark(seconds: float | None) -> str:
    ok = settings.length_ok(seconds)
    return f"{'✅' if ok else '❌'} **{seconds}s**" + ("" if ok else (
        f" (must be {settings.min_duration_sec:g}-{settings.max_duration_sec:g}s)"))
