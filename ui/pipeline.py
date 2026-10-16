"""The Pipeline page: counts per stage and the list of episodes."""

import altair as alt
import pandas as pd
import streamlit as st

from studio.dashboard_data import pipeline_counts
from studio.guide import guide
from studio.models import Category
from ui.core import open_episode, repo, state


def series_color() -> str:
    theme = getattr(getattr(st.context, "theme", None), "type", None)
    return "#3987e5" if theme == "dark" else "#2a78d6"


def page_pipeline() -> None:
    st.header("Pipeline")
    counts = pipeline_counts(repo)
    by_status = dict(counts)
    total = sum(by_status.values())
    in_progress = total - by_status["PUBLISHED"] - by_status["REJECTED"]

    cols = st.columns(5)
    cols[0].metric("Episodes", total)
    cols[1].metric("In progress", in_progress)
    cols[2].metric("Awaiting human review", by_status["HUMAN_REVIEW"])
    cols[3].metric("Published", by_status["PUBLISHED"])
    cols[4].metric("Rejected", by_status["REJECTED"])

    if total == 0:
        st.info("No episodes yet. Create one under **New episode**, "
                "or run `python cli.py demo` for a sample.")
        return

    df = pd.DataFrame(counts, columns=["Status", "Episodes"])
    chart = (
        alt.Chart(df, title="Episodes by status")
        .mark_bar(color=series_color(), cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                  width={"band": 0.6})
        .encode(
            x=alt.X("Status:N", sort=[s for s, _ in counts], title=None,
                    axis=alt.Axis(labelAngle=-35, ticks=False, domain=False)),
            y=alt.Y("Episodes:Q", title=None,
                    axis=alt.Axis(tickMinStep=1, format="d", gridOpacity=0.4, domain=False)),
            tooltip=["Status", "Episodes"],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, width="stretch")

    st.subheader("Episodes")
    f1, f2, f3 = st.columns([1, 1, 2])
    status = f1.selectbox("Status", ["All"] + [s for s, _ in counts])
    category = f2.selectbox("Category", ["All"] + [c.value for c in Category])
    search = f3.text_input("Search topic, series or ID")
    rows = repo.list_episodes(status=None if status == "All" else status,
                              category=None if category == "All" else category,
                              search=search or None)
    if not rows:
        st.write("No episodes match these filters.")
        return
    for r in rows:  # what each episode needs next, in plain words
        r["what's next"] = guide(repo.get_episode(r["episode_id"]), repo).headline
    table = pd.DataFrame(rows)[["episode_id", "status", "what's next", "topic", "category",
                                "series", "updated_at"]]
    state["pipeline_ids"] = list(table["episode_id"])
    st.caption("Click a row to open the episode.")
    st.dataframe(table, hide_index=True, width="stretch", key="pipeline_table",
                 on_select=_open_selected_row, selection_mode="single-row")


def _open_selected_row() -> None:
    rows = state["pipeline_table"].selection.rows
    if rows:
        open_episode(state["pipeline_ids"][rows[0]])
