"""Streamlit dashboard.   Usage:  streamlit run dashboard.py

A thin screen over the same repository the CLI uses. Every change still goes through
EpisodeRepository, so the state machine, validators and audit log apply exactly as in
the CLI. The pages and sections live in the ui/ package; what to show is computed in
studio/ (dashboard_data.py, guide.py).
"""
import streamlit as st

from ui.analytics import page_analytics
from ui.core import open_episode, repo, state
from ui.episode import page_episode
from ui.health import page_health
from ui.new_episode import page_new
from ui.pipeline import page_pipeline

PAGES = {"Pipeline": page_pipeline, "Episode": page_episode, "New episode": page_new,
         "Analytics": page_analytics, "Health": page_health}

st.set_page_config(page_title="Shorts Studio", page_icon="🎬", layout="wide")
state.setdefault("page", "Pipeline")
state.setdefault("episode_id", None)

with st.sidebar:
    st.title("🎬 Shorts Studio")
    st.radio("Go to", list(PAGES), key="page", label_visibility="collapsed")
    recent = sorted(repo.list_episodes(), key=lambda r: r["updated_at"], reverse=True)[:5]
    if recent:
        st.caption("Recent episodes")
        for r in recent:
            st.button(f"{r['episode_id'][-3:]} · {r['topic'][:22]} · {r['status'].title()}",
                      key=f"recent_{r['episode_id']}", on_click=open_episode,
                      args=(r["episode_id"],), width="stretch")
    st.divider()
    st.caption(f"Database: `{repo.db_path}`")
    if st.button("Back up database", width="stretch"):
        st.success(f"Backup written to `{repo.backup()}`")

repo.auto_backup()  # once a day (does nothing if today's copy exists)
PAGES[state.page]()
