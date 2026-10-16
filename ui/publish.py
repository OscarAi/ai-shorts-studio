"""Episode sections after production: QA checklist and the publication package."""

from datetime import time as dtime

import streamlit as st

from studio.assets import abs_path
from studio.copywriter import build_copy_prompt, mock_copy, parse_copy
from studio.models import AssetKind, Status, latest_asset
from studio.platforms import (
    PLATFORMS,
    RULES,
    TITLE_MAX,
    check_post,
    draft_post,
    platform_of,
    platform_states,
)
from studio.review import (
    QA_CHECKS,
    add_links,
    automatic_qa,
    default_description,
    mark_published,
    mark_scheduled,
    set_captions,
    set_publication,
    set_qa,
)
from studio.timeutil import local_now_text
from ui.core import ACTOR, act, flash, repo, settings, show_flash, state

PUBLISH_STATUSES = (Status.HUMAN_REVIEW, Status.APPROVED, Status.SCHEDULED, Status.PUBLISHED)


# ------------------------------------------------------------------------------ QA
def _qa_saved(new) -> None:
    if new.qa.approved:
        flash("success", "QA approved. Move on to HUMAN_REVIEW.", "qa")
    else:
        flash("error", "QA not approved: " + "; ".join(new.qa.issues), "qa")


def qa_section(ep) -> None:
    """Python's own checks, then a human watches the video and ticks every box."""
    if ep.status != Status.QA:
        return
    st.subheader("QA")
    show_flash("qa")
    for check in automatic_qa(ep, settings):
        st.markdown(f"{'✅' if check['ok'] else '❌'} Automatic: {check['detail']}")
    if ep.qa.issues:
        st.warning("Last QA: " + " · ".join(ep.qa.issues))
    k = f"qa_{ep.episode_id}_{ep.version}"
    with st.form(k):
        st.caption("Watch the whole video (above) with sound, then tick only what is true.")
        ticks = {key: st.checkbox(label, key=f"{k}_{key}") for key, label in QA_CHECKS.items()}
        note = st.text_input("Note", key=f"{k}_note",
                             placeholder="what you watched for / what is wrong")
        if st.form_submit_button("Save QA", type="primary"):
            act(set_qa, repo, ep.episode_id, ticks, actor=ACTOR, note=note, then=_qa_saved)


# ------------------------------------------------------------------------- publish
def publication_section(ep) -> None:
    """Package, one post text per platform, downloads, then scheduled / published + links."""
    if ep.status not in PUBLISH_STATUSES:
        return
    st.subheader("Publish")
    if ep.status in (Status.HUMAN_REVIEW, Status.APPROVED):
        package_form(ep)
    if ep.publication.platforms:
        post_texts(ep)
    if ep.status == Status.HUMAN_REVIEW:
        st.caption("When you are happy with the video, continue to Approved to download it.")
        return
    downloads(ep)
    if ep.status == Status.APPROVED:
        go_live_forms(ep)
    else:
        links_and_schedule(ep)


def package_form(ep) -> None:
    pub = ep.publication
    k = f"pub_{ep.episode_id}_{ep.version}"
    with st.form(k):
        title = st.text_input(f"Title (max {TITLE_MAX} characters)", value=pub.title,
                              key=f"{k}_title")
        hashtags = st.text_input("Hashtags", value=" ".join(pub.hashtags), key=f"{k}_tags",
                                 placeholder="#history #food #dumplings")
        platforms = st.multiselect("Platforms", list(PLATFORMS), default=pub.platforms,
                                   format_func=PLATFORMS.get, key=f"{k}_platforms")
        ai = st.checkbox("This video uses AI-generated visuals (label it)", value=pub.ai_disclosure,
                         key=f"{k}_ai", help="Required on the platforms for realistic AI "
                                             "visuals; recommended for any AI visuals.")
        if st.form_submit_button("Save publication package", type="primary"):
            act(set_publication, repo, ep.episode_id, title=title,
                description=pub.description or default_description(ep, ai),
                hashtags=[hashtags], platforms=platforms, ai_disclosure=ai, actor=ACTOR)


def post_texts(ep) -> None:
    """One ready-to-paste text per platform, with its limits, checks and tips."""
    st.markdown("**Post text for each platform**")
    platforms = ep.publication.platforms
    for platform, tab in zip(platforms, st.tabs([PLATFORMS[p] for p in platforms]), strict=True):
        with tab:
            rules = RULES[platform]
            saved = ep.publication.captions.get(platform)
            key = f"cap_{ep.episode_id}_{ep.version}_{platform}"
            if platform == "youtube_shorts":
                st.markdown(f"Title: **{ep.publication.title or '(save a title above)'}**")
            text = st.text_area("Text", value=saved or draft_post(ep, platform), height=220,
                                key=key, help="Pre-filled from your approved content. Edit freely.")
            st.caption(f"{len(text)} / {rules.text_max} characters"
                       + ("" if saved else " · draft (not saved yet)"))
            for w in check_post(platform, text):
                st.warning(w)
            with st.expander("Copy"):
                st.code(text, language=None, wrap_lines=True)
            with st.expander(f"Tips for {PLATFORMS[platform]}"):
                for tip in rules.tips:
                    st.markdown(f"- {tip}")
            if st.button("Save this text", key=f"{key}_save"):
                act(set_captions, repo, ep.episode_id, {platform: text}, actor=ACTOR)
    copywriter_box(ep)


def _fill_mock_copy(box: str, episode_id: str) -> None:
    state[box] = mock_copy(repo.get_episode(episode_id)).model_dump_json(indent=2)


def _use_copy(ep, answer: str) -> None:
    """The copywriter's answer fills the post texts (only for the chosen platforms)."""
    out = parse_copy(answer)
    captions = {p: t for p, t in out.captions.items() if p in ep.publication.platforms}
    set_captions(repo, ep.episode_id, captions, actor=ACTOR, hashtags=out.hashtags,
                 title=out.youtube_title if ep.status != Status.PUBLISHED else None)


def copywriter_box(ep) -> None:
    box = f"copy_{ep.episode_id}"
    with st.expander("✨ Make it catchier with a chat AI (optional)"):
        st.caption("The prompt contains only your approved facts and asks the AI to add none. "
                   "Its answer only fills the texts above; you still read and save them.")
        st.code(build_copy_prompt(ep), language=None, wrap_lines=True)
        st.text_area("Paste the AI's answer", key=box, height=150)
        c1, c2 = st.columns(2)
        c2.button("Use mock texts", key=f"{box}_mock", on_click=_fill_mock_copy,
                  args=(box, ep.episode_id))
        if c1.button("Use these texts", key=f"{box}_use", type="primary"):
            act(_use_copy, ep, state.get(box, ""))


def downloads(ep) -> None:
    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    captions = latest_asset(ep, AssetKind.CAPTIONS)
    c1, c2 = st.columns(2)
    if video and abs_path(repo, video).exists():
        c1.download_button("⬇️ Download MP4", data=abs_path(repo, video).read_bytes(),
                           file_name=f"{ep.episode_id}_{video.asset_id}.mp4", mime="video/mp4",
                           key=f"dl_{ep.episode_id}")
    if captions and abs_path(repo, captions).exists():
        c2.download_button("⬇️ Subtitles (.srt)", data=abs_path(repo, captions).read_bytes(),
                           file_name=f"{ep.episode_id}.srt", mime="text/plain",
                           key=f"dl_srt_{ep.episode_id}",
                           help="YouTube lets you upload this as the video's captions.")


# ------------------------------------------------------------------- go live, links
def go_live_forms(ep) -> None:
    """After uploading: scheduled for later (some platforms may be live already), or
    everything public now."""
    pub = ep.publication
    if not pub.platforms:
        st.info("Choose the platforms and save the package first.")
        return
    st.caption("Upload the MP4 to each platform yourself with its text above, then record it here.")
    k = f"live_{ep.episode_id}_{ep.version}"
    scheduled, now = st.tabs(["📅 Scheduled (some can be live now)", "✅ All published now"])
    with scheduled, st.form(f"{k}_sched"):
        rows = {}
        for p in pub.platforms:
            st.markdown(f"**{PLATFORMS[p]}**")
            c1, c2, c3, c4 = st.columns([3, 1.3, 1, 1])
            rows[p] = (c1.text_input("Link", key=f"{k}_s_url_{p}", placeholder="https://..."),
                       c2.radio("When", ["Scheduled", "Live now"], key=f"{k}_s_when_{p}",
                                horizontal=True),
                       c3.date_input("Date", key=f"{k}_s_date_{p}"),
                       c4.time_input("Time", value=dtime(18, 0), key=f"{k}_s_time_{p}"))
        st.caption("Only platforms with a link are recorded. Times are your local time. "
                   "Posted right away (e.g. an app without scheduling)? Choose \"Live now\": "
                   "the episode then counts as published, the others keep their times.")
        if st.form_submit_button("📅 Mark as scheduled", type="primary"):
            filled = {p: row for p, row in rows.items() if row[0].strip()}
            act(mark_scheduled, repo, ep.episode_id,
                {p: f"{d:%Y-%m-%d} {t:%H:%M}" for p, (_, when, d, t) in filled.items()
                 if when == "Scheduled"},
                [row[0] for row in filled.values()], actor=ACTOR,
                live_now=[p for p, row in filled.items() if row[1] == "Live now"])
    with now, st.form(f"{k}_now"):
        links = [st.text_input(f"{PLATFORMS[p]} link", key=f"{k}_url_{p}",
                               placeholder="https://...") for p in pub.platforms]
        if st.form_submit_button("✅ Mark as published"):
            act(mark_published, repo, ep.episode_id, links, actor=ACTOR)


def links_and_schedule(ep) -> None:
    """SCHEDULED / PUBLISHED: where it is, and add the links of platforms posted on later."""
    pub = ep.publication
    if ep.status == Status.PUBLISHED and pub.published_at:
        st.markdown(f"Published {pub.published_at[:16].replace('T', ' ')} (UTC)")
    states = platform_states(ep)
    for url in pub.urls:
        p = platform_of(url)
        go_live = states.get(p)
        if go_live is not None and go_live.state == "scheduled":
            mark = f" · 📅 scheduled, goes live {go_live.when:%Y-%m-%d %H:%M}"
        else:
            since = go_live.when if go_live else None
            mark = " · ✅ live" + (f" since {since:%Y-%m-%d %H:%M}" if since else "")
        st.markdown(f"- {PLATFORMS.get(p, 'Link')}: {url}{mark}")
    posted = {platform_of(u) for u in pub.urls}
    missing = [PLATFORMS[p] for p in pub.platforms if p not in posted]
    with st.form(f"addlink_{ep.episode_id}_{ep.version}"):
        url = st.text_input("Add a link" + (f" (still missing: {', '.join(missing)})"
                                            if missing else ""), placeholder="https://...")
        posted_at = st.text_input("It went live / goes live on (your time, YYYY-MM-DD HH:MM)",
                                  value=local_now_text(),
                                  help="Now or earlier = live. A later time = scheduled. "
                                       "Analytics counts the video's age (day 1, 7, 30) from "
                                       "this.")
        if st.form_submit_button("Add link"):
            act(add_links, repo, ep.episode_id, [url], actor=ACTOR, posted_at=posted_at)
