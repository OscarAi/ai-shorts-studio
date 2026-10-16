"""The New episode page: AI topic suggestions or writing your own idea."""

import streamlit as st

from studio.errors import StudioError
from studio.ideas import build_ideas_prompt, create_from_idea, mock_ideas, parse_ideas, screen_ideas
from studio.models import Category
from ui.core import ACTOR, VERDICT_BADGE, flash, open_episode, repo, show_error, show_flash, state

OWN, IDEAS = "new_own", "new_ideas"  # where each tab's messages appear
CATEGORIES = [c.value for c in Category]


def default_category() -> int:
    """Where settings.yaml default_category (food) is in the list: preselected."""
    return CATEGORIES.index(repo.settings.default_category.value)


def _fill_mock_ideas() -> None:
    state["ideas_box"] = mock_ideas().model_dump_json(indent=2)


def _created(episode_id: str, verdict: str) -> None:
    """After creating: go straight to the new episode and say how originality went."""
    message = f"Created {episode_id}. Originality: {verdict}."
    if verdict != "PASS":
        message += " Review the matches below and accept or reject the idea."
    flash("success" if verdict == "PASS" else "warning", message)
    open_episode(episode_id)


def _create_idea(n: int) -> None:
    """A suggested idea becomes an episode (a callback, so it can switch pages)."""
    try:
        idea = parse_ideas(state.get("ideas_box", "")).ideas[n]
        ep, report = create_from_idea(repo, idea, series=state.get("ideas_series", "").strip(),
                                      actor=ACTOR)
    except (StudioError, ValueError) as e:
        flash("error", str(e), IDEAS)
        return
    _created(ep.episode_id, report.verdict.value)


def _create_own() -> None:
    """Runs when "Create" is pressed (a callback, so it can switch to the new episode)."""
    topic = state.get("new_topic", "").strip()
    if not topic:
        flash("error", "A topic is required.", OWN)
        return
    try:
        ep = repo.create_episode(topic, state.get("new_category", repo.settings.default_category),
                                 series=state.get("new_series", "").strip(), actor=ACTOR,
                                 story_angle=state.get("new_angle", "").strip())
    except (StudioError, ValueError) as e:
        flash("error", str(e), OWN)
        return
    report = repo.check_originality(ep.episode_id, actor=ACTOR)
    _created(ep.episode_id, report.verdict.value)


def write_your_own_tab() -> None:
    show_flash(OWN)
    with st.form("new_episode", clear_on_submit=True):
        st.text_input("Topic", key="new_topic",
                      placeholder="Ketchup was sold as medicine")
        st.text_area(
            "Story angle (needed before research)", key="new_angle",
            placeholder="In the 1830s a doctor sold tomato ketchup as a cure for indigestion",
            help="The specific twist THIS video tells. The same topic with a different angle can "
                 "be a different video; the scriptwriter and the originality check both use it.")
        st.selectbox("Category", CATEGORIES, key="new_category", index=default_category())
        st.text_input("Series (optional)", key="new_series", placeholder="Invented by Accident")
        st.form_submit_button("Create", type="primary", on_click=_create_own)


def suggest_ideas_tab() -> None:
    """The AI suggests topics; Python screens every one for originality before you pick."""
    c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
    series = c1.text_input("Series", key="ideas_series", placeholder="Invented by Accident")
    category = c2.selectbox("Category", ["any", *CATEGORIES], key="ideas_category",
                            index=default_category() + 1)
    theme = c3.text_input("Theme (optional)", key="ideas_theme",
                          placeholder="food once used as medicine, ancient recipes...")
    count = c4.number_input("Ideas", min_value=1, max_value=10, value=5, key="ideas_count")
    with st.expander("1. Copy this prompt into any chat AI"):
        st.code(build_ideas_prompt(repo, series=series.strip(),
                                   category="" if category == "any" else category,
                                   theme=theme.strip(), count=int(count)),
                language=None, wrap_lines=True)
    st.text_area("2. Paste its answer here", key="ideas_box", height=160,
                 placeholder='{"ideas": [...]}')
    st.button("Use mock ideas", on_click=_fill_mock_ideas, help="Free [MOCK] ideas for testing.")
    show_flash(IDEAS)
    if not state.get("ideas_box", "").strip():
        return
    try:
        screened = screen_ideas(repo, parse_ideas(state["ideas_box"]))
    except StudioError as e:
        show_error(e)
        return
    st.markdown("**3. Pick one** (checked against your library and each other):")
    for n, (idea, report) in enumerate(screened):
        with st.container(border=True):
            st.markdown(f"{VERDICT_BADGE[report.verdict.value]} · **{idea.topic}**")
            st.caption(f"Angle: {idea.story_angle or '-'} · Opens with: {idea.hook_style or '-'} · "
                       f"{idea.category.value} · fact risk: {idea.fact_risk} · "
                       f"already known: {idea.familiarity}"
                       + (f"  \n{idea.why_it_works}" if idea.why_it_works else ""))
            if report.matches:
                m = report.matches[0]
                st.caption(f"Similar to {m.episode_id} “{m.other_topic}”: {m.detail}")
            label = ("Create this episode" if report.verdict.value == "PASS"
                     else "Create anyway (you decide on originality)")
            st.button(label, key=f"idea_create_{n}", on_click=_create_idea, args=(n,))


def page_new() -> None:
    st.header("New episode")
    suggest, own = st.tabs(["💡 Suggest ideas", "✍️ Write your own"])
    with suggest:
        suggest_ideas_tab()
    with own:
        write_your_own_tab()
