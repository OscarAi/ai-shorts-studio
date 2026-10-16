"""Episode sections for ideas: originality review and editing the idea."""

import streamlit as st

from studio.dashboard_data import GATE_STATE_TEXT, match_rows, stage_for
from studio.models import Category, Metadata, Status
from studio.originality import fingerprint
from ui.core import ACTOR, VERDICT_BADGE, act, repo, show_table


def originality_section(ep) -> None:
    """Latest originality verdict, its matches, and your accept/reject decision."""
    stage = stage_for(ep.status)
    st.subheader("Originality")
    check = repo.latest_originality(ep.episode_id, stage)
    if ep.status in (Status.IDEA, Status.RESEARCH):
        gate = GATE_STATE_TEXT[repo.originality_gate_state(ep.episode_id)]
        st.caption(f"Originality gate: **{gate}**")
    if st.button(f"Run originality check ({stage} stage)", key=f"orig_run_{ep.episode_id}"):
        act(repo.check_originality, ep.episode_id, stage=stage, actor=ACTOR)
    if check is None:
        st.caption("No check has been run at this stage yet.")
        return

    report = check["report"]
    st.markdown(f"{VERDICT_BADGE[report.verdict.value]} · compared with "
                f"{report.compared_against} episode(s) · checked {check['checked_at']} "
                f"· check #{check['id']}")
    show_table(match_rows(report), "No similar episodes found.")
    for note in report.notes:
        st.caption(f"Note: {note}")
    if report.verdict.value == "PASS":
        return

    if check["decision"]:
        st.markdown(f"Your decision: **{check['decision']}** by {check['decided_by']}"
                    + (f": {check['decision_note']}" if check["decision_note"] else ""))
    with st.form(f"orig_decide_{check['id']}"):
        decision = st.radio("Decision", ["accept", "reject"], horizontal=True,
                            index=1 if check["decision"] == "reject" else 0,
                            format_func={"accept": "Accept: different enough, go ahead",
                                         "reject": "Reject: not original enough"}.get)
        note = st.text_input("Note (required)", value=check["decision_note"] or "",
                             placeholder="Why is this different enough, or not?")
        if st.form_submit_button("Save decision", type="primary"):
            act(repo.decide_originality, check["id"], decision, actor=ACTOR, note=note,
                then=lambda _: st.toast(f"Recorded: {decision}"))


def _save_idea(ep, fields: dict) -> None:
    """Save the edited idea; a changed topic or angle is re-checked for originality at once."""
    metadata = Metadata.model_validate({**ep.metadata.model_dump(), **fields})
    new = repo.save(ep.model_copy(update={"metadata": metadata}), actor=ACTOR,
                    note="idea edited", allowed_sections={"metadata"})
    if fingerprint(new) != fingerprint(ep):
        repo.check_originality(ep.episode_id, stage=stage_for(new.status), actor=ACTOR)


def edit_idea_section(ep) -> None:
    """Topic, angle and the other idea fields. Only editable before the facts are fixed."""
    if ep.status not in (Status.IDEA, Status.RESEARCH):
        return
    m = ep.metadata
    with st.expander("Edit idea"), st.form(f"idea_{ep.episode_id}_{ep.version}"):
        topic = st.text_input("Topic", value=m.topic)
        angle = st.text_area("Story angle", value=m.story_angle,
                             help="What makes this video different. Used by the originality check.")
        c1, c2, c3 = st.columns(3)
        cats = [c.value for c in Category]
        category = c1.selectbox("Category", cats, index=cats.index(m.category.value))
        series = c2.text_input("Series", value=m.series)
        duration = c3.number_input("Target length (s)", min_value=5, max_value=600,
                                   value=m.target_duration_sec, step=1)
        hook_style = st.text_input("Hook style", value=m.hook_style,
                                   placeholder="question, shocking fact, mystery...")
        if st.form_submit_button("Save idea"):
            act(_save_idea, ep, {"topic": topic.strip(), "story_angle": angle.strip(),
                                 "category": category, "series": series.strip(),
                                 "target_duration_sec": int(duration),
                                 "hook_style": hook_style.strip()})
