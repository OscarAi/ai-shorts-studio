"""The Episode page: guide box, stage sections, other moves and details."""

import streamlit as st

from studio.dashboard_data import (
    allowed_moves,
    claim_rows,
    episode_summary,
    script_rows,
    shot_rows,
    source_rows,
)
from studio.guide import SHORT_NAMES, guide, stepper
from studio.models import Status
from studio.state_machine import STAGE_ORDER
from ui.analytics import performance_section
from ui.core import ACTOR, act, repo, settings, show_flash, show_sources, show_table, state
from ui.idea import edit_idea_section, originality_section
from ui.production import final_video_player, production_section
from ui.publish import publication_section, qa_section
from ui.research import claim_review_section, sources_section, work_section


def _moved(new) -> None:
    st.toast(f"{new.episode_id} is now at {SHORT_NAMES.get(new.status, new.status.value)}")


def guide_box(ep) -> None:
    """What to do now, with ticks, and ONE button to move on when the rules agree."""
    g = guide(ep, repo)
    with st.container(border=True):
        step = f"Step {g.stage_number} of {len(STAGE_ORDER)} · " if g.stage_number else ""
        st.markdown(f"#### {step}{g.title}")
        for s in g.steps:
            mark = "✅" if s.done else ("▫️" if s.optional else "⬜")
            st.markdown(f"{mark} {s.label}" + (" *(optional)*" if s.optional else ""))
        for w in g.warnings:
            st.caption(f"⚠️ {w.message}")
        if g.target is None:
            return
        name = SHORT_NAMES.get(g.target, g.target.value)
        label = ("✅ It's live: mark as published" if ep.status == Status.SCHEDULED
                 else f"Continue to {name} →")
        if st.button(label, key=f"continue_{ep.episode_id}", type="primary",
                     disabled=not g.ready):
            act(repo.transition, ep.episode_id, g.target, actor=ACTOR, then=_moved)
        if g.go_back_to is not None:
            back = SHORT_NAMES[g.go_back_to]
            st.info(f"This can only be fixed at {back}: " + " ".join(i.message for i in g.blockers))
            if st.button(f"↩ Back to {back} to fix this", key=f"back_{ep.episode_id}"):
                act(repo.transition, ep.episode_id, g.go_back_to, actor=ACTOR,
                    reason="fix: " + "; ".join(i.message for i in g.blockers))
        if g.blockers:
            with st.expander(f"Why can't I continue yet? ({len(g.blockers)})"):
                for i in g.blockers:
                    st.markdown(f"- {i.message}")
                    st.caption(i.code)


def other_moves(ep) -> None:
    """Going back, rejecting or reviving: less common, so folded away. A reason is required."""
    moves = allowed_moves(ep.status)
    if not moves:
        return
    with st.expander("Other moves (go back, reject, revive)"), \
            st.form(f"move_{ep.episode_id}_{ep.version}", clear_on_submit=True):
        labels = {m["status"]: m["status"].value
                  + (" (reason required)" if m["needs_reason"] else "") for m in moves}
        targets = [m["status"] for m in moves]
        target = guide(ep, repo).target
        to = st.selectbox("Move to", targets, format_func=labels.get,
                          index=targets.index(target) if target in targets else 0)
        reason = st.text_input("Reason")
        if st.form_submit_button("Apply", type="primary"):
            act(repo.transition, ep.episode_id, to, actor=ACTOR, reason=reason, then=_moved)


def details(ep) -> None:
    """Numbers, the full research / script / storyboard, history, the AI log and the JSON."""
    st.divider()
    st.subheader("Details")
    s = episode_summary(ep)
    cols = st.columns(5)
    cols[0].metric("Claims approved", f"{s['approved_claims']}/{s['claims']}")
    cols[1].metric("Sources", s["sources"])
    cols[2].metric("Script length", f"{s['script_sec']}s",
                   help=f"Allowed: {settings.min_duration_sec}-{settings.max_duration_sec}s")
    cols[3].metric("Shots", f"{s['shots']} ({s['shot_sec']}s)")
    cols[4].metric("Asset cost", f"${s['cost_usd']:.2f}")
    research, script, storyboard, history, ai_log, raw = st.tabs(
        ["Research", "Script", "Storyboard", "History", "AI log", "JSON"])
    with research:
        st.markdown("**Claims**")
        show_table(claim_rows(ep), "No claims yet.")
        st.markdown("**Sources**")
        show_sources(source_rows(ep), "No sources yet.")
        if ep.research.open_questions:
            st.markdown("**Open questions**")
            for q in ep.research.open_questions:
                st.markdown(f"- {q}")
    with script:
        if ep.script.hook:
            st.markdown(f"**Hook:** {ep.script.hook}")
        show_table(script_rows(ep), "No script yet.")
    with storyboard:
        show_table(shot_rows(ep), "No storyboard yet.")
    with history:
        for title, rows, empty in (
                ("Versions", repo.list_versions(ep.episode_id), "No versions."),
                ("Events", repo.list_events(ep.episode_id), "No events."),
                ("Rejections", repo.list_rejections(ep.episode_id), "Never rejected."),
                ("Originality checks", repo.list_originality_checks(ep.episode_id),
                 "Never checked.")):
            st.markdown(f"**{title}**")
            show_table(rows, empty)
    with ai_log:
        ai_log_tab(ep)
    with raw:
        st.json(ep.model_dump(mode="json"), expanded=False)


def ai_log_tab(ep) -> None:
    calls = repo.list_ai_calls(ep.episode_id)
    if not calls:
        st.caption("No AI requests yet.")
    for c in reversed(calls):
        mark = "⚠️ " if c["problems"] else "✅ "
        with st.expander(f"{mark}{c['ts'][:16].replace('T', ' ')} · {c['agent']} · try "
                         f"{c['attempt']} · {c['provider']} {c['model']} · {c['seconds']:g} s"):
            if c["problems"]:
                st.markdown("**Sent back to fix:**\n" + "\n".join(
                    f"- {p}" for p in c["problems"].splitlines()))
            st.markdown("**Answer**")
            st.code(c["answer"][:20000], language="json", wrap_lines=True)
            st.markdown("**Prompt**")
            st.code(c["prompt"][:20000], language=None, wrap_lines=True)


def page_episode() -> None:
    rows = repo.list_episodes()
    options = [r["episode_id"] for r in rows]
    if not options:
        st.info("No episodes yet. Create one under **New episode**.")
        return
    if state.episode_id not in options:
        state.episode_id = options[0]
    labels = {r["episode_id"]: f"{r['episode_id']} · {r['topic']} · {r['status'].title()}"
              for r in rows}
    st.selectbox("Episode", options, key="episode_id", format_func=labels.get)
    show_flash()

    ep = repo.get_episode(state.episode_id)
    st.header(ep.metadata.topic)
    st.caption(f"{ep.episode_id} · v{ep.version} · **{ep.status.value}** · "
               f"{ep.metadata.category.value}"
               + (f" · series: {ep.metadata.series}" if ep.metadata.series else ""))
    st.markdown(stepper(ep.status))
    if ep.rejection:
        st.error(f"Rejected from {ep.rejection.from_status.value}: {ep.rejection.reason}")
    guide_box(ep)

    originality_section(ep)
    edit_idea_section(ep)
    work_section(ep)
    sources_section(ep)
    claim_review_section(ep)
    production_section(ep)
    if ep.status in (Status.QA, Status.HUMAN_REVIEW, Status.APPROVED, Status.PUBLISHED):
        st.subheader("Final video")
        final_video_player(ep)
    qa_section(ep)
    publication_section(ep)
    performance_section(ep)

    other_moves(ep)
    details(ep)
