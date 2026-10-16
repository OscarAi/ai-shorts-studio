"""📈 Analytics page and the episode's Performance box: type in each platform's numbers at
day 1 / 7 / 30, see them side by side, and compare what works."""

import pandas as pd
import streamlit as st

from studio import analytics
from studio.models import COUNT_FIELDS, Status
from studio.platforms import PLATFORMS, posted_platforms
from ui.core import ACTOR, act, flash, open_episode, repo, show_flash, show_table, state


def numbers_form(ep, platform: str, key: str) -> None:
    """The platform's own boxes (empty = not shown there / not known)."""
    st.caption("Where to find them: " + analytics.WHERE[platform])
    with st.form(f"{key}_{ep.episode_id}_{platform}", clear_on_submit=True):
        labels = analytics.LABELS[platform]
        cols = st.columns(2)
        numbers = {}
        for i, (field, label) in enumerate(labels.items()):
            if field == "avg_view_sec":  # typed as the app shows it: 0:26
                numbers[field] = cols[i % 2].text_input(label, key=f"{key}_{platform}_{field}",
                                                        placeholder="0:26")
                continue
            whole = field in COUNT_FIELDS
            numbers[field] = cols[i % 2].number_input(
                label, min_value=0 if whole else 0.0,
                max_value=100.0 if field.endswith("_pct") else None,  # percentages
                value=None, step=1 if whole else 0.1, key=f"{key}_{platform}_{field}")
        note = st.text_input("Note (optional)", key=f"{key}_{platform}_note")
        if st.form_submit_button("Save numbers"):
            act(analytics.add_snapshot, repo, ep.episode_id, platform, numbers, actor=ACTOR,
                note=note, then=lambda saved: _numbers_saved(ep, platform, *saved))


def _numbers_saved(ep, platform: str, snap, warnings: list[str]) -> None:
    msg = f"Saved {PLATFORMS[platform]} numbers for {ep.episode_id} (day {snap.day})."
    flash("warning" if warnings else "success",
          msg + "".join(f"\n\n⚠️ {w}" for w in warnings))


def _latest_line(snap) -> str:
    labels = analytics.LABELS.get(snap.platform, {})
    parts = [f"{labels.get(f, f)} **{getattr(snap, f):g}**" for f in labels
             if getattr(snap, f) is not None]
    r = analytics.rates(snap)
    if r["shares_per_1k"] is not None:
        parts.append(f"shares / 1k views **{r['shares_per_1k']:g}**")
    return f"Day {snap.day}: " + " · ".join(parts)


def performance_section(ep) -> None:
    """Episode page, PUBLISHED: latest numbers per platform, the next check-in, Add numbers."""
    if ep.status != Status.PUBLISHED:
        return
    st.subheader("📈 Performance")
    platforms = posted_platforms(ep)
    if not platforms:
        st.caption("Add the video's link first; then you can track its numbers.")
        return
    snaps = repo.list_metrics(ep.episode_id)
    latest = analytics.latest(snaps)
    states = analytics.platform_states(ep)
    for p in platforms:
        go_live = states[p]
        if go_live.state == "scheduled":
            st.markdown(f"**{PLATFORMS[p]}**: 📅 goes live {go_live.when:%Y-%m-%d %H:%M}; numbers "
                        "after that.")
            continue
        age = analytics.age_days(ep, p)
        mine = [s for s in snaps if s.platform == p]
        checkpoint, due = analytics.next_checkin(mine, age)
        when = ("" if checkpoint is None else
                f" · **day-{checkpoint} numbers due now**" if due else
                f" · next check-in: day {checkpoint}")
        st.markdown(f"**{PLATFORMS[p]}** (live {analytics.days(age)}){when}")
        st.markdown(_latest_line(latest[p]) if p in latest else "No numbers yet.")
        with st.expander(f"Add {PLATFORMS[p]} numbers", expanded=due):
            numbers_form(ep, p, key="perf")


def page_analytics() -> None:
    st.header("📈 Analytics")
    st.caption("Read each video's numbers at about day 1, 7 and 30 and type them in here. "
               "Comparing videos at the same age is fair; rates (per 1,000 views) show what "
               "people liked, not only how many saw it.")
    show_flash()
    published = [e for e in repo.all_episodes()
                 if e.status == Status.PUBLISHED and analytics.live_platforms(e)]
    if not published:
        st.info("Numbers can be added once a video is published and has its link.")
        return

    st.subheader("Numbers to collect today")
    due = analytics.due_checkins(repo)
    if not due:
        st.caption("Nothing due. ✅")
    for c in due:
        st.markdown(f"- **{c.episode_id}** · {c.topic} · {PLATFORMS[c.platform]}: "
                    f"day-{c.checkpoint} numbers (live {analytics.days(c.age)}) · "
                    f"[open the video]({c.url})")

    st.subheader("Add numbers")
    by_id = {e.episode_id: e for e in sorted(published, key=lambda e: e.episode_id)}
    first = due[0] if due else None
    ids = list(by_id)
    c1, c2 = st.columns(2)
    ep_id = c1.selectbox("Video", ids, index=ids.index(first.episode_id) if first else 0,
                         format_func=lambda i: f"{i} · {by_id[i].metadata.topic}",
                         key="an_episode")
    platforms = analytics.live_platforms(by_id[ep_id])
    default = (platforms.index(first.platform)
               if first and first.episode_id == ep_id and first.platform in platforms else 0)
    platform = c2.selectbox("Platform", platforms, index=default, format_func=PLATFORMS.get,
                            key="an_platform")
    numbers_form(by_id[ep_id], platform, key="an")

    st.subheader("Your videos")
    rows = analytics.video_rows(repo)
    if rows:
        state["analytics_ids"] = [r["Episode"] for r in rows]
        st.caption("The newest numbers per video and platform. Click a row to open the episode.")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     key="analytics_table", on_select=_open_selected_row,
                     selection_mode="single-row")
    else:
        st.caption("No numbers saved yet.")

    st.subheader("Compare")
    with_data = [p for p in PLATFORMS if any(s.platform == p for s in repo.list_metrics())]
    if not with_data:
        st.caption("Save some numbers first.")
        return
    c1, c2, c3 = st.columns(3)
    by = c1.selectbox("Group by", list(analytics.GROUP_BY), format_func=analytics.GROUP_BY.get)
    platform = c2.selectbox("On", with_data, format_func=PLATFORMS.get, key="cmp_platform")
    day = c3.selectbox("At about day", analytics.CHECKPOINTS, index=1)
    table = analytics.compare(repo, by=by, platform=platform, day=day)
    show_table(table, "No numbers on this platform yet.")
    if table:
        name = analytics.GROUP_BY[by]
        st.bar_chart(pd.DataFrame(table), x=name, y="Median views", horizontal=True)
        st.caption(f"One platform at a time: views are counted differently on each. Groups with "
                   f"fewer than {analytics.TRUST_MIN_VIDEOS} videos are mostly luck; set a hook "
                   "style on new ideas (Edit idea) to learn which hooks work.")
    st.download_button("⬇️ Download all numbers (CSV)", analytics.export_csv(repo),
                       file_name="shorts_numbers.csv", mime="text/csv")


def _open_selected_row() -> None:
    rows = state["analytics_table"].selection.rows
    if rows:
        open_episode(state["analytics_ids"][rows[0]])
