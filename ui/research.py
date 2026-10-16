"""Episode sections for research and fact check: agent work, sources, claim review."""

import json

import streamlit as st

from studio import declines, llm
from studio.agents import agent_for_status, parse_response, preview, run_agent
from studio.ai_runner import ask_agent
from studio.dashboard_data import (
    claim_rows,
    declined_rows,
    link_label,
    script_rows,
    shot_rows,
    source_rows,
    sources_needing_a_human,
)
from studio.errors import StudioError
from studio.models import Status
from studio.review import decline_source, set_claim_statuses, set_fact_check
from studio.sources import VERIFY_STATUSES, HttpFetcher, mark_source_verified, verify_sources
from ui.core import (
    ACTOR,
    act,
    flash,
    flash_error,
    repo,
    settings,
    show_error,
    show_flash,
    show_sources,
    show_table,
    state,
)

WORK = "work"  # where the Work section's messages appear


# ------------------------------------------------------------------- work: answers
def _work_keys(ep_id: str, agent) -> tuple[str, str, str]:
    base = f"work_{ep_id}_{agent.name}"
    return base, base + "_source", base + "_preview"


def _fill_box(ep_id: str, agent, kind: str) -> None:
    box, source, show = _work_keys(ep_id, agent)
    ep = repo.get_episode(ep_id)
    try:
        if kind == "mock":
            state[box] = agent.mock(ep, settings).model_dump_json(indent=2)
        else:
            state[box] = json.dumps(agent.current_answer(ep), indent=2, ensure_ascii=False)
    except StudioError as e:
        flash_error(e, WORK)
        return
    state[source], state[show] = kind, True


def _pasted(ep_id: str, agent) -> None:
    _, source, show = _work_keys(ep_id, agent)
    state[source] = "ai_edited" if state.get(source) in ("ai", "ai_edited") else "paste"
    state[show] = False


def make_llm():
    """The AI services with a key in .env (tests replace this with a FakeLLM)."""
    return llm.Router.from_config(usage=repo)


def _run_ai(ep_id: str, agent, fresh: bool = False) -> None:
    """Ask the AI (with self-repair) and put its answer in the box, previewed, NOT saved.
    An identical earlier question is answered from the AI log unless `fresh`."""
    box, source, show = _work_keys(ep_id, agent)
    router = make_llm()
    limit = getattr(router, "max_seconds_per_run", 0)
    try:
        with st.spinner(f"🤖 Asking the AI ({agent.label.lower()})... this can take up to "
                        f"{limit / 60:.0f} minutes. Please don't click again." if limit else
                        "🤖 Asking the AI..."):
            result = ask_agent(repo, ep_id, agent, router, fresh=fresh,
                               fetcher=HttpFetcher())  # invented links go back to the AI
    except StudioError as e:
        flash_error(e, WORK)
        return
    state[box], state[source], state[show] = result.text, "ai", True
    state[box + "_ai_note"] = result.note
    if result.usable:
        flash("success", f"{result.summary}. Check the preview, then Save.", WORK)
    else:
        flash("error", f"{result.summary}, but the answer still can't be used: "
              + "; ".join(result.problems[:3]) + ". Run it again, or fix it by hand below.", WORK)


def _save_answer(ep_id: str, agent) -> None:
    box, source, show = _work_keys(ep_id, agent)
    kind = state.get(source, "paste")
    actor = f"agent:{agent.name}" if kind == "mock" else ACTOR
    ai_note = state.get(box + "_ai_note", "AI")
    note = {"mock": "mock", "json": "edited as JSON", "ai": ai_note,
            "ai_edited": f"{ai_note}, then edited by hand"}.get(kind, "manual paste")
    try:
        new = run_agent(repo, ep_id, agent, parse_response(agent, state.get(box, "")),
                        actor=actor, note=note)
    except StudioError as e:
        flash_error(e, WORK)
        return
    state[box], state[show] = "", False
    flash("success", f"Saved the {agent.label.lower()} answer as v{new.version}.", WORK)


def preview_tables(agent, ep) -> None:
    if agent.name == "researcher":
        show_table(claim_rows(ep), "No claims.")
        show_table(source_rows(ep), "No sources.")
    elif agent.name == "fact_checker":
        show_table(claim_rows(ep), "No claims.")
        for issue in ep.fact_check.issues:
            st.markdown(f"- {issue}")
    elif agent.name == "scriptwriter":
        st.markdown(f"**Hook:** {ep.script.hook} · **Length:** {ep.script.total_estimated_sec}s")
        show_table(script_rows(ep), "No script.")
    else:
        total = round(sum(x.duration_sec for x in ep.storyboard.shots), 2)
        st.markdown(f"**Shots:** {len(ep.storyboard.shots)} · **Length:** {total}s "
                    f"(narration {ep.script.total_estimated_sec}s)")
        show_table(shot_rows(ep), "No shots.")


def _ai_buttons(ep, agent, box: str) -> None:
    router = make_llm()
    if not router.ready:
        st.caption("🤖 Want the AI to do this step for you? Add a free AI key to the .env "
                   "file (README: 'AI inside the app'; the Health page checks it). Until then: "
                   "copy-paste below.")
        return
    a1, a2 = st.columns([3, 2])
    a1.button(f"🤖 Run with AI ({', '.join(router.names)})", key=box + "_btn_ai",
              type="primary", on_click=_run_ai, args=(ep.episode_id, agent),
              help="Sends the prompt below to a free AI service, lets it fix its own "
                   "mistakes, and shows the answer here. Nothing is saved until you "
                   "click Save. The same question again reuses the earlier answer "
                   "(no request).")
    if any(c["agent"] == agent.name for c in repo.list_ai_calls(ep.episode_id)):
        a2.button("🔁 New AI answer", key=box + "_btn_ai_fresh", on_click=_run_ai,
                  args=(ep.episode_id, agent, True),
                  help="Ask again even if this exact question was answered before.")
    st.caption(f"AI requests today: {repo.ai_requests_today()} of "
               f"{llm.load_ai_config().daily_limit_total} (free services only; limits in "
               "config/ai.yaml)")


def work_section(ep) -> None:
    """Run with AI, or prompt -> paste the AI's answer -> preview -> save. Or a mock."""
    agent = agent_for_status(ep.status)
    if agent is None:
        return
    box, _, show = _work_keys(ep.episode_id, agent)
    st.subheader(f"Work: {agent.label}")
    show_flash(WORK)
    _ai_buttons(ep, agent, box)
    with st.expander("Or: copy this prompt into any chat AI and paste its answer below"):
        st.code(agent.build_prompt(ep, settings, repo.blocked_sites()), language=None,
                wrap_lines=True)
    st.text_area("The answer (from the AI, or pasted)", key=box, height=200,
                 on_change=_pasted, args=(ep.episode_id, agent),
                 placeholder='{"...": "the JSON the AI gave you"}')
    b1, b2, b3 = st.columns(3)
    if b1.button("Preview", key=box + "_btn_preview", type="primary"):
        state[show] = True
    b2.button("Use mock answer", key=box + "_btn_mock", on_click=_fill_box,
              args=(ep.episode_id, agent, "mock"), help="A [MOCK] answer for testing, free.")
    if agent.current_answer(ep) is not None:
        b3.button("Edit current as JSON", key=box + "_btn_json", on_click=_fill_box,
                  args=(ep.episode_id, agent, "json"))

    if not state.get(show) or not state.get(box, "").strip():
        return
    try:
        new, issues = preview(repo, ep.episode_id, agent, parse_response(agent, state[box]))
    except StudioError as e:
        show_error(e)
        return
    st.markdown("**Preview** (not saved yet)")
    preview_tables(agent, new)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        st.markdown("Still to fix before the next step (you can save now and fix later):")
        for i in errors:
            st.markdown(f"- `{i.code}` {i.message}")
    else:
        st.markdown("✅ Nothing blocks the next step.")
    st.button("Save answer", key=box + "_btn_save", type="primary",
              on_click=_save_answer, args=(ep.episode_id, agent))


# ---------------------------------------------------------------------- sources
def sources_section(ep) -> None:
    """Open every cited link, show its status, and let a human vouch for blocked pages."""
    if ep.status not in VERIFY_STATUSES or not ep.research.sources:
        return
    st.subheader("Sources")
    show_sources(source_rows(ep), "Every source is declined. Ask the researcher for new ones "
                                  "(Work).")
    if st.button("Verify sources (opens each cited link)", key=f"verify_{ep.episode_id}",
                 type="primary"):
        with st.spinner("Opening each cited link..."):
            act(verify_sources, repo, ep.episode_id, HttpFetcher())
    for s in sources_needing_a_human(ep):
        with st.form(f"vouch_{ep.episode_id}_{ep.version}_{s.source_id}"):
            st.markdown(f"**{s.source_id}** {link_label(s)}: [{s.title or s.url}]({s.url})"
                        + (f"  \n{s.link_note}" if s.link_note else ""))
            note = st.text_input("What did you check?",
                                 key=f"vouch_note_{ep.episode_id}_{s.source_id}",
                                 placeholder="Opened it: the page exists and supports the claims")
            if st.form_submit_button("✔ I checked this page myself"):
                act(mark_source_verified, repo, ep.episode_id, s.source_id, actor=ACTOR,
                    note=note)
    decline_box(ep)
    declined = declined_rows(ep)
    if declined:
        with st.expander(f"Declined sources ({len(declined)})"):
            show_table(declined, "")
    blocked_sites_box()


def decline_box(ep) -> None:
    """Don't use a source: claims that relied only on it are rejected (preview first)."""
    usable = {x.source_id: x for x in ep.research.sources if not x.declined}
    if not usable:
        return
    with st.expander("🚫 Decline a source (you don't want to use it)"):
        sid = st.selectbox("Source", list(usable), key=f"decl_pick_{ep.episode_id}",
                           format_func=lambda i: f"{i} · {usable[i].publisher} · "
                                                 f"{declines.site_of(usable[i].url)}")
        st.markdown("**What changes:**\n" + "\n".join(
            f"- {line}" for line in declines.preview(ep, {sid}).lines()))
        with st.form(f"decline_{ep.episode_id}_{ep.version}"):
            reason = st.text_input("Why? (kept in the history)",
                                   placeholder="e.g. tabloid / not a primary source")
            block = st.checkbox(f"Never use {declines.site_of(usable[sid].url)} again "
                                "(all future episodes)", key=f"decl_block_{ep.episode_id}")
            if st.form_submit_button("Decline this source"):
                act(decline_source, repo, ep.episode_id, sid, reason=reason, actor=ACTOR,
                    block_site=block)


def blocked_sites_box() -> None:
    """Sites the researcher is told not to use; their sources are declined automatically."""
    sites = repo.blocked_sites()
    with st.expander(f"Blocked sites ({len(sites)})"):
        st.caption("The researcher is told not to use these, and any source from them is "
                   "declined automatically.")
        for b in sites:
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{b['domain']}**: {b['reason']} "
                        f"({b['blocked_by']}, {b['blocked_at'][:10]})")
            if c2.button("Unblock", key=f"unblock_{b['domain']}"):
                act(repo.unblock_site, b["domain"])
        with st.form("block_site"):
            c1, c2 = st.columns(2)
            site = c1.text_input("Block a site", placeholder="example.com")
            reason = c2.text_input("Why?", key="block_site_reason")
            if st.form_submit_button("Block"):
                act(repo.block_site, site, reason=reason, actor=ACTOR)


# ----------------------------------------------------------------- claim review
REVIEW_OPTIONS = ["needs_review", "approved", "rejected"]
REVIEW_DECISION = {"needs_review": "reset", "approved": "approve", "rejected": "reject"}


def _save_review(ep, choices: dict[str, tuple[str, str]], approved: bool) -> None:
    """The whole review as one version (plus the fact-check tick, if it changed)."""
    status = {c.claim_id: c.status.value for c in ep.research.claims}
    changed = {cid: (REVIEW_DECISION[decision], note.strip())
               for cid, (decision, note) in choices.items()
               if decision != status[cid] or note.strip()}
    if changed:
        set_claim_statuses(repo, ep.episode_id, changed, actor=ACTOR)
    if approved != ep.fact_check.approved:
        set_fact_check(repo, ep.episode_id, approved, actor=ACTOR)


def claim_review_section(ep) -> None:
    """Only a human approves facts. The AI's suggestion is shown in each claim's notes."""
    if ep.status != Status.FACT_CHECK or not ep.research.claims:
        return
    st.subheader("Review claims")
    st.caption("Check each claim against its sources. The AI check is only a suggestion.")
    sources = {x.source_id: x for x in ep.research.sources}
    for issue in ep.fact_check.issues:
        st.markdown(f"- ⚠️ {issue}")
    k = f"rev_{ep.episode_id}_{ep.version}"
    with st.form(k):
        choices = {}
        for c in ep.research.claims:
            st.markdown(f"**{c.claim_id}** ({c.importance.value}): {c.statement}")
            links = " · ".join(f"[{sid} {sources[sid].publisher}]({sources[sid].url}) "
                               f"{link_label(sources[sid])}"
                               if sid in sources else f"{sid} (unknown)" for sid in c.source_ids)
            st.caption((links or "no sources") + (f"  \n{c.notes}" if c.notes else ""))
            for e in c.evidence:
                st.markdown(f"> **{e.source_id}** says (match {e.score:.0%}): “{e.quote}”")
            if not c.evidence:
                st.caption("No quote found yet. Run 'Verify sources', or read the source yourself.")
            col1, col2 = st.columns([1, 2])
            choices[c.claim_id] = (
                col1.selectbox("Decision", REVIEW_OPTIONS, key=f"{k}_{c.claim_id}",
                               index=REVIEW_OPTIONS.index(c.status.value)),
                col2.text_input("Note", key=f"{k}_note_{c.claim_id}",
                                placeholder="optional: what you checked"))
        approved = st.checkbox("Fact check approved: every claim used is checked",
                               value=ep.fact_check.approved, key=f"{k}_factcheck")
        if st.form_submit_button("Save review", type="primary"):
            act(_save_review, ep, choices, approved)
