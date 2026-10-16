"""Smoke tests: run dashboard.py headless against a temporary database."""
import json

import pytest

st = pytest.importorskip("streamlit")
from helpers import open_gate, sample_episode, walk_to  # noqa: E402
from media import fake_subtitles, image, needs_ffmpeg, produced, tone  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402
from test_sources import ANSWER, PAGES  # noqa: E402

from studio.agents import get_agent, run_mock, run_mock_pipeline  # noqa: E402
from studio.assets import add_narration, add_shot_asset, narration_text  # noqa: E402
from studio.config import PROJECT_ROOT, load_settings  # noqa: E402
from studio.db import EpisodeRepository  # noqa: E402
from studio.models import LinkStatus, Status  # noqa: E402
from studio.sources import FetchResult, HttpFetcher  # noqa: E402
from studio.subtitles import FakeAligner, WhisperAligner, script_words  # noqa: E402
from studio.tts import FakeTTS, KokoroTTS  # noqa: E402

APP = str(PROJECT_ROOT / "dashboard.py")
# Generous: a click can build a real video with FFmpeg, and GitHub's machines are ~3x slower.
TIMEOUT = 180


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_DATA_DIR", str(tmp_path))
    st.cache_resource.clear()
    yield lambda: AppTest.from_file(APP, default_timeout=TIMEOUT)
    st.cache_resource.clear()


def test_empty_pipeline(app):
    at = app().run()
    assert not at.exception
    assert any("No episodes yet" in i.value for i in at.info)


def test_create_and_advance(app):
    at = app().run()
    at.sidebar.radio[0].set_value("New episode").run()
    next(x for x in at.text_input if x.label == "Topic").input("Microwave by accident")
    next(x for x in at.text_area if x.label.startswith("Story angle")).input(
        "A melted candy bar in an engineer's pocket")
    button(at, "Create").click().run()
    assert not at.exception
    assert any("Created EP_000001" in s.value for s in at.success)

    at.sidebar.radio[0].set_value("Episode").run()
    assert not at.exception
    assert at.header[0].value == "Microwave by accident"
    # Creating an idea runs the originality check; alone in the library it passes,
    # so IDEA -> RESEARCH is the default move and must succeed.
    next(b for b in at.button if b.label == "Apply").click().run()
    assert not at.exception
    assert "RESEARCH" in at.caption[0].value

    # RESEARCH -> FACT_CHECK is blocked by the rules (no claims) and shown as an error.
    next(b for b in at.button if b.label == "Apply").click().run()
    assert any("NO_CLAIMS" in e.value for e in at.error)
    assert "RESEARCH" in at.caption[0].value


def test_pipeline_lists_episodes(app):
    at = app().run()
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("Penicillin")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="test")
    at.run()
    assert not at.exception
    assert at.metric[0].value == "1"
    assert len(at.dataframe) == 1


def button(at, label):
    return next(b for b in at.button if b.label == label)


def flagged_idea(app):
    """Two identical ideas; open the second one on the Episode page."""
    repo = EpisodeRepository(load_settings())
    repo.create_episode("The accidental invention of the microwave")
    b = repo.create_episode("The accidental invention of the microwave",
                            story_angle="The engineer behind it, not the candy bar")
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    at.selectbox(key="episode_id").set_value(b.episode_id).run()
    button(at, "Run originality check (idea stage)").click().run()
    assert not at.exception
    return at


def test_flagged_idea_shows_matches_and_can_be_accepted(app):
    at = flagged_idea(app)
    assert any("SUGGEST_BLOCK" in m.value for m in at.markdown)
    assert any("needs your decision" in c.value for c in at.caption)
    assert len(at.dataframe) >= 1  # the matches table

    button(at, "Apply").click().run()
    assert any("ORIGINALITY_NEEDS_DECISION" in e.value for e in at.error)

    button(at, "Save decision").click().run()  # no note yet
    assert any("note" in e.value for e in at.error)

    next(r for r in at.radio if r.label == "Decision").set_value("accept")
    next(t for t in at.text_input if t.label == "Note (required)").input("Different angle: the inventor")
    button(at, "Save decision").click().run()
    assert not at.exception
    assert any("accepted by you" in c.value for c in at.caption)

    button(at, "Apply").click().run()
    assert not at.exception
    assert "RESEARCH" in at.caption[0].value


def test_rejected_idea_stays_blocked(app):
    at = flagged_idea(app)
    next(r for r in at.radio if r.label == "Decision").set_value("reject")
    next(t for t in at.text_input if t.label == "Note (required)").input("Too close to EP_000001")
    button(at, "Save decision").click().run()
    assert any("Declined by you" in c.value for c in at.caption)
    button(at, "Apply").click().run()
    assert any("ORIGINALITY_DECLINED" in e.value for e in at.error)
    assert "IDEA" in at.caption[0].value


# ------------------------------------------------------- Milestone 3: work
GOOD_RESEARCH = """Here you go:
```json
{"sources": [{"source_id": "S001", "title": "Velcro history", "publisher": "Museum A",
              "url": "https://example.com/a"},
             {"source_id": "S002", "title": "Mestral", "publisher": "Archive B",
              "url": "https://example.com/b"}],
 "claims": [{"claim_id": "C001", "statement": "George de Mestral studied burrs.",
             "importance": "high", "source_ids": ["S001", "S002"], "status": "approved"}]}
```"""


def episode_at(app, status_upto):
    """A fresh episode walked to RESEARCH (and optionally further with mocks)."""
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    repo.transition(ep.episode_id, Status.RESEARCH, actor="test")
    if status_upto == Status.FACT_CHECK:
        run_mock(repo, ep.episode_id, get_agent("researcher"))
        repo.transition(ep.episode_id, Status.FACT_CHECK, actor="test")
        run_mock(repo, ep.episode_id, get_agent("fact_checker"))
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert not at.exception
    return repo, ep.episode_id, at


def test_paste_preview_and_save_research(app):
    repo, eid, at = episode_at(app, Status.RESEARCH)
    box = f"work_{eid}_researcher"
    at.text_area(key=box).input("this is not json").run()
    at.button(key=box + "_btn_preview").click().run()
    assert any("No JSON object found" in e.value for e in at.error)

    at.text_area(key=box).input(GOOD_RESEARCH).run()
    at.button(key=box + "_btn_preview").click().run()
    assert not at.exception
    assert any("Preview" in m.value for m in at.markdown)
    at.button(key=box + "_btn_save").click().run()
    assert any("Saved the researcher answer" in s.value for s in at.success)
    saved = repo.get_episode(eid)
    assert saved.research.claims[0].status.value == "needs_review"  # never approved by AI
    assert repo.list_versions(eid)[-1]["change_note"] == "researcher: manual paste"


def test_mock_answer_through_the_dashboard(app):
    repo, eid, at = episode_at(app, Status.RESEARCH)
    box = f"work_{eid}_researcher"
    at.button(key=box + "_btn_mock").click().run()
    assert "[MOCK]" in at.text_area(key=box).value
    at.button(key=box + "_btn_save").click().run()
    assert not at.exception
    assert repo.list_versions(eid)[-1]["actor"] == "agent:researcher"


def test_claim_review_then_advance(app):
    repo, eid, at = episode_at(app, Status.FACT_CHECK)
    for sb in at.selectbox:
        if sb.key and sb.key.startswith(f"rev_{eid}_") and sb.key[-5:-3] == "_C":
            sb.set_value("approved")
    next(c for c in at.checkbox if c.key.endswith("_factcheck")).check()
    button(at, "Save review").click().run()
    assert not at.exception
    saved = repo.get_episode(eid)
    assert all(c.status.value == "approved" for c in saved.research.claims)
    assert saved.fact_check.approved
    assert repo.list_versions(eid)[-1]["actor"] == "human"
    button(at, "Apply").click().run()
    assert "SCRIPT" in at.caption[0].value


def test_edit_idea_rechecks_originality(app):
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("Penicillin")
    repo.check_originality(ep.episode_id)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    next(t for t in at.text_area if t.label == "Story angle").input("The factory race of 1943")
    button(at, "Save idea").click().run()
    assert not at.exception
    assert repo.get_episode(ep.episode_id).metadata.story_angle == "The factory race of 1943"
    assert repo.originality_gate_state(ep.episode_id) == "pass"  # re-checked automatically
    assert len(repo.list_originality_checks(ep.episode_id)) == 2


# --------------------------------------------- Milestone 4: verify sources
def test_verify_sources_and_vouch_in_dashboard(app, monkeypatch):
    monkeypatch.setattr(HttpFetcher, "fetch", lambda self, url: PAGES.get(
        url, FetchResult(LinkStatus.BROKEN, url, note="HTTP 404")))

    repo, eid, at = episode_at(app, Status.RESEARCH)
    box = f"work_{eid}_researcher"
    at.text_area(key=box).input(json.dumps(ANSWER)).run()
    at.button(key=box + "_btn_preview").click().run()
    at.button(key=box + "_btn_save").click().run()
    assert any(h.value == "Sources" for h in at.subheader)

    at.button(key=f"verify_{eid}").click().run()
    assert not at.exception
    links = {x.source_id: x.link_status for x in repo.get_episode(eid).research.sources}
    assert links["S004"] == LinkStatus.BROKEN and links["S002"] == LinkStatus.OK
    button(at, "Apply").click().run()
    assert any("SOURCE_BROKEN" in e.value for e in at.error)

    next(t for t in at.text_input if t.key == f"vouch_note_{eid}_S004").input("Works in my browser")
    button(at, "✔ I checked this page myself").click().run()
    assert repo.get_episode(eid).research.sources[2].verified_by == "human"
    button(at, "Apply").click().run()
    assert not at.exception
    assert "FACT_CHECK" in at.caption[0].value
    assert any("says (match" in m.value for m in at.markdown)  # evidence shown in review


# ------------------------------------------------ Milestone 4: topic scout
def test_suggest_ideas_screen_and_create(app):
    repo = EpisodeRepository(load_settings())
    at = app().run()
    at.sidebar.radio[0].set_value("New episode").run()
    at.text_input(key="ideas_series").input("Invented by Accident").run()
    button(at, "Use mock ideas").click().run()
    assert not at.exception
    labels = [b.label for b in at.button if b.key and b.key.startswith("idea_create_")]
    assert labels[0] == "Create this episode"
    assert labels[-1].startswith("Create anyway")  # the repeated Slinky idea is flagged
    at.button(key="idea_create_0").click().run()
    assert not at.exception
    assert any("Created EP_000001" in s.value for s in at.success)
    ep = repo.get_episode("EP_000001")
    assert ep.metadata.series == "Invented by Accident" and ep.metadata.story_angle
    assert repo.originality_gate_state("EP_000001") == "pass"


# ------------------------------------------------ Milestone 5: production
@needs_ffmpeg
def test_production_build_video_then_qa(app, tmp_path):
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    eid = ep.episode_id
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert any(h.value == "Production" for h in at.subheader)
    assert at.button(key=f"build_{eid}").disabled  # nothing uploaded yet

    for shot in ep.storyboard.shots:  # what the Upload button does
        add_shot_asset(repo, eid, shot.shot_id, image(tmp_path / f"{shot.shot_id}.png"),
                       f"{shot.shot_id}.png", provider="test", license_note="test", actor="human")
    add_narration(repo, eid, tone(tmp_path / "v.wav", 31), "v.wav", license_note="test",
                  actor="human")
    fake_subtitles(repo, eid, 31)
    at.run()
    assert not at.button(key=f"build_{eid}").disabled
    at.button(key=f"build_{eid}").click().run()
    assert not at.exception
    assert repo.get_episode(eid).assets[-1].asset_id == "final_v1"
    button(at, "Apply").click().run()
    assert not at.exception
    assert "QA" in at.caption[0].value


@needs_ffmpeg
def test_qa_publish_flow_in_dashboard(app, tmp_path):
    repo = EpisodeRepository(load_settings())
    eid = produced(repo, tmp_path)
    repo.transition(eid, Status.QA, actor="t")
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert any(h.value == "QA" for h in at.subheader)

    boxes = [c for c in at.checkbox if c.key and c.key.startswith(f"qa_{eid}_")]
    for c in boxes[:-1]:
        c.check()
    button(at, "Save QA").click().run()  # one box left unticked
    assert not repo.get_episode(eid).qa.approved
    assert any("QA not approved" in e.value for e in at.error)

    for c in at.checkbox:
        if c.key and c.key.startswith(f"qa_{eid}_"):
            c.check()
    button(at, "Save QA").click().run()
    assert repo.get_episode(eid).qa.approved
    button(at, "Apply").click().run()   # -> HUMAN_REVIEW
    button(at, "Apply").click().run()   # -> APPROVED
    assert "APPROVED" in at.caption[0].value

    next(t for t in at.text_input if t.label.startswith("Title")).input("Burrs on a dog")
    next(m for m in at.multiselect if m.label == "Platforms").set_value(["youtube_shorts"])
    button(at, "Save publication package").click().run()
    assert not at.exception
    labels = [b.label for b in at.get("download_button")]
    assert "⬇️ Download MP4" in labels and "⬇️ Subtitles (.srt)" in labels
    next(t for t in at.text_input if t.label == "YouTube Shorts link").input(
        "https://youtube.com/shorts/abc")
    button(at, "✅ Mark as published").click().run()
    assert not at.exception
    ep = repo.get_episode(eid)
    assert ep.status == Status.PUBLISHED and ep.publication.urls == ["https://youtube.com/shorts/abc"]


def test_tool_picker_fills_link_and_license(app):
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    run_mock_pipeline(repo, ep.episode_id)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    picker = at.selectbox(key=f"tool_{ep.episode_id}_SHOT01")
    assert picker.value == "Adobe Firefly"  # first tool by default
    assert any("Adobe Generative AI" in c.value for c in at.caption)
    picker.set_value("ChatGPT").run()
    assert any("OpenAI Terms of Use" in c.value for c in at.caption)
    narration_tools = at.selectbox(key=f"tool_{ep.episode_id}_voice").options
    assert "Kokoro (built in)" not in narration_tools and "Kokoro web demo" in narration_tools


@needs_ffmpeg
def test_generate_narration_preview_then_use(app, monkeypatch):
    # The real voice model is slow to load; the dashboard flow is what is tested here.
    monkeypatch.setattr(KokoroTTS, "synthesize",
                        lambda self, text, voice, speed: FakeTTS().synthesize(text, voice, speed))
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    eid = run_mock_pipeline(repo, ep.episode_id).episode_id
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    at.selectbox(key=f"narr_{eid}_voice").set_value("george")
    at.button(key=f"narr_{eid}_go").click().run()
    assert not at.exception
    assert any("not saved yet" in m.value for m in at.markdown)
    assert not repo.get_episode(eid).assets  # a preview is not saved

    at.button(key=f"narr_{eid}_use").click().run()
    assert not at.exception
    voice = repo.get_episode(eid).assets[-1]
    assert voice.provider == "Kokoro (george)" and "Apache-2.0" in voice.license_note


@needs_ffmpeg
def test_make_subtitles_in_dashboard(app, monkeypatch, tmp_path):
    def fake_words(self, audio_path):  # speech recognition replaced: the flow is tested here
        ep = EpisodeRepository(load_settings()).get_episode("EP_000001")
        return FakeAligner(script_words(narration_text(ep)), 0.5).words(audio_path)
    monkeypatch.setattr(WhisperAligner, "words", fake_words)
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    eid = run_mock_pipeline(repo, ep.episode_id).episode_id
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert any("Add the narration first" in c.value for c in at.caption)
    add_narration(repo, eid, tone(tmp_path / "v.wav", 31), "v.wav", license_note="test",
                  actor="human")
    at.run()
    at.button(key=f"subs_{eid}").click().run()
    assert not at.exception
    assert repo.get_episode(eid).assets[-1].kind.value == "captions"
    assert any("Match the current narration" in m.value for m in at.markdown)


# ------------------------------------------------ the "What to do now" box
def test_guide_box_blocks_then_continues(app):
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("Velcro")
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert any("Step 1 of 11" in m.value for m in at.markdown)
    assert any("🔵 **Idea**" in m.value for m in at.markdown)  # the stage path
    assert at.button(key=f"continue_{ep.episode_id}").disabled
    assert any(e.label.startswith("Why can't I continue yet?") for e in at.expander)

    ep = repo.get_episode(ep.episode_id)
    ep.metadata.story_angle = "Burrs on a dog after a mountain walk"
    repo.save(ep, actor="human", allowed_sections={"metadata"})
    repo.check_originality(ep.episode_id)
    at.run()
    go = at.button(key=f"continue_{ep.episode_id}")
    assert not go.disabled and go.label == "Continue to Research →"
    go.click().run()
    assert not at.exception
    assert repo.get_episode(ep.episode_id).status == Status.RESEARCH
    assert any("Step 2 of 11" in m.value for m in at.markdown)


@needs_ffmpeg
def test_back_to_production_button(app, tmp_path):
    """A video built before subtitles were required: one click goes back to fix it."""
    repo = EpisodeRepository(load_settings())
    eid = produced(repo, tmp_path)
    repo.transition(eid, Status.QA, actor="t")
    ep = repo.get_episode(eid)
    repo.save(ep.model_copy(update={"assets": [a for a in ep.assets
                                               if a.kind.value != "captions"]}),
              actor="t", allowed_sections={"assets"})
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    at.button(key=f"back_{eid}").click().run()
    assert not at.exception
    assert repo.get_episode(eid).status == Status.PRODUCTION
    assert "fix: Make the subtitles." in repo.list_versions(eid)[-1]["change_note"]


# --------------------------------------------------------- fewer clicks
def test_create_jumps_straight_to_the_episode(app):
    at = app().run()
    at.sidebar.radio[0].set_value("New episode").run()
    next(x for x in at.text_input if x.label == "Topic").input("Velcro")
    button(at, "Create").click().run()
    assert not at.exception
    assert at.sidebar.radio[0].value == "Episode"
    assert at.header[0].value == "Velcro"
    assert any("Created EP_000001. Originality: PASS." in s.value for s in at.success)


def test_recent_episodes_in_the_sidebar_open_them(app):
    repo = EpisodeRepository(load_settings())
    repo.create_episode("Velcro")
    repo.create_episode("Penicillin")
    at = app().run()
    at.sidebar.button(key="recent_EP_000001").click().run()
    assert at.sidebar.radio[0].value == "Episode" and at.header[0].value == "Velcro"


def test_pipeline_list_says_what_is_next(app):
    repo = EpisodeRepository(load_settings())
    repo.create_episode("Velcro")
    at = app().run()
    table = at.dataframe[0].value
    assert "what's next" in table.columns
    assert table["what's next"][0].startswith("Write the story angle")


# ------------------------------------------- scheduling and post texts
def approved_episode(repo):
    """The sample episode walked to APPROVED (no media needed: sample asset records)."""
    return walk_to(repo, sample_episode(repo), Status.APPROVED).episode_id


def test_schedule_then_its_live_then_add_link(app):
    import datetime as dt
    repo = EpisodeRepository(load_settings())
    eid = approved_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    yt = next(t for t in at.text_input if t.key and t.key.endswith("_s_url_youtube_shorts"))
    yt.input("https://youtube.com/shorts/abc")
    # Default date (today) and time (18:00). The test runner's date setter stores a date
    # *range* and is ignored inside forms, so the default is what gets submitted here.
    button(at, "📅 Mark as scheduled").click().run()
    assert not at.exception
    ep = repo.get_episode(eid)
    when = f"{dt.date.today():%Y-%m-%d} 18:00"
    assert ep.status == Status.SCHEDULED
    assert ep.publication.schedule == {"youtube_shorts": when}
    assert any(f"goes live {when}" in m.value for m in at.markdown)

    go = at.button(key=f"continue_{eid}")
    assert go.label == "✅ It's live: mark as published"
    go.click().run()
    assert repo.get_episode(eid).status == Status.PUBLISHED
    next(t for t in at.text_input if t.label.startswith("Add a link")).input(
        "https://www.tiktok.com/@me/video/1")
    button(at, "Add link").click().run()
    assert not at.exception
    assert "https://www.tiktok.com/@me/video/1" in repo.get_episode(eid).publication.urls


def test_post_text_per_platform_and_copywriter(app):
    repo = EpisodeRepository(load_settings())
    eid = approved_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert {"YouTube Shorts"} <= {t.label for t in at.tabs}  # sample platforms: youtube only
    box = next(t for t in at.text_area if t.key and t.key.endswith("_youtube_shorts"))
    assert box.value.startswith("What if one of medicine's biggest breakthroughs")
    box.input("My own YouTube text #history").run()
    next(b for b in at.button if b.key and b.key.endswith("_youtube_shorts_save")).click().run()
    assert repo.get_episode(eid).publication.captions["youtube_shorts"] == \
        "My own YouTube text #history"

    at.button(key=f"copy_{eid}_mock").click().run()
    at.button(key=f"copy_{eid}_use").click().run()
    assert not at.exception
    pub = repo.get_episode(eid).publication
    assert pub.title.startswith("[MOCK]") and pub.captions["youtube_shorts"].startswith("[MOCK]")


def test_shots_suggest_tools_by_daily_budget(app):
    repo = EpisodeRepository(load_settings())
    ep = repo.create_episode("The accidental invention of velcro")
    open_gate(repo, ep.episode_id)
    ep = run_mock_pipeline(repo, ep.episode_id)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert at.selectbox(key=f"tool_{ep.episode_id}_SHOT01").value == "Adobe Firefly"
    assert at.selectbox(key=f"tool_{ep.episode_id}_SHOT07").value == "ChatGPT"
    assert any("Today's pictures: Adobe Firefly 0/6 · ChatGPT 0/6" in c.value for c in at.caption)


# ------------------------------------------------------------------ analytics
def published_episode(repo):
    from studio.review import mark_published
    eid = approved_episode(repo)
    mark_published(repo, eid, ["https://youtube.com/shorts/abc"], actor="t")
    return eid


def test_analytics_page_before_anything_is_published(app):
    at = app().run()
    at.sidebar.radio[0].set_value("Analytics").run()
    assert not at.exception
    assert any("once a video is published" in i.value for i in at.info)


def test_analytics_page_add_numbers_then_compare(app):
    repo = EpisodeRepository(load_settings())
    eid = published_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Analytics").run()
    assert not at.exception
    assert any("Nothing due" in c.value for c in at.caption)  # went live a moment ago
    labels = [n.label for n in at.number_input if n.key.startswith("an_")]
    assert "Stayed to watch %" in labels and "Saves" not in labels  # YouTube's own boxes
    at.number_input(key="an_youtube_shorts_views").set_value(1200)
    at.number_input(key="an_youtube_shorts_likes").set_value(80)
    button(at, "Save numbers").click().run()
    assert not at.exception
    [snap] = repo.list_metrics(eid)
    assert (snap.views, snap.likes, snap.shares) == (1200, 80, None)
    assert any("Saved YouTube Shorts numbers" in s.value for s in at.success)
    assert len(at.dataframe) == 2  # your videos + compare
    assert at.get("download_button")


def test_episode_performance_box(app):
    repo = EpisodeRepository(load_settings())
    eid = published_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert any("Performance" in h.value for h in at.subheader)
    assert any("No numbers yet" in m.value for m in at.markdown)
    at.number_input(key="perf_youtube_shorts_views").set_value(300)
    at.number_input(key="perf_youtube_shorts_likes").set_value(500)  # swapped boxes
    button(at, "Save numbers").click().run()
    assert any("Swapped two boxes" in e.value for e in at.error)
    assert repo.list_metrics(eid) == []


def test_health_page(app):
    at = app().run()
    at.sidebar.radio[0].set_value("Health").run()
    assert not at.exception
    assert any("Database" in m.value for m in at.markdown)


# ------------------------------------------------------------ decline sources
def fact_check_episode(repo):
    return walk_to(repo, sample_episode(repo), Status.FACT_CHECK).episode_id


def test_decline_a_source_with_preview_then_unblock(app):
    repo = EpisodeRepository(load_settings())
    eid = fact_check_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    at.selectbox(key=f"decl_pick_{eid}").set_value("S002").run()
    assert not at.exception
    assert any("Will be rejected (no other source): C002" in m.value for m in at.markdown)
    next(t for t in at.text_input if t.label.startswith("Why? (kept")).input("not for us")
    next(c for c in at.checkbox if c.label.startswith("Never use acs.org")).check()
    button(at, "Decline this source").click().run()
    assert not at.exception
    ep = repo.get_episode(eid)
    s002 = next(x for x in ep.research.sources if x.source_id == "S002")
    assert s002.declined and s002.declined_note == "not for us"
    assert next(c for c in ep.research.claims if c.claim_id == "C002").status.value == "rejected"
    assert [b["domain"] for b in repo.blocked_sites()] == ["acs.org"]
    assert any(e.label.startswith("Declined sources (1)") for e in at.expander)
    at.button(key="unblock_acs.org").click().run()
    assert repo.blocked_sites() == []


def test_decline_needs_a_reason_in_the_dashboard(app):
    repo = EpisodeRepository(load_settings())
    eid = fact_check_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    button(at, "Decline this source").click().run()
    assert any("Say why" in e.value for e in at.error)
    assert not any(x.declined for x in repo.get_episode(eid).research.sources)


def test_schedule_youtube_and_instagram_live_now(app):
    repo = EpisodeRepository(load_settings())
    eid = approved_episode(repo)
    ep = repo.get_episode(eid)
    repo.save(ep.model_copy(update={"publication": ep.publication.model_copy(
        update={"platforms": ["youtube_shorts", "instagram_reels"]})}), actor="t")
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    k = next(t.key for t in at.text_input if t.key and t.key.endswith("_s_url_youtube_shorts"))
    base = k.removesuffix("_s_url_youtube_shorts")
    at.text_input(key=k).input("https://youtube.com/shorts/abc")
    at.text_input(key=f"{base}_s_url_instagram_reels").input("https://www.instagram.com/p/x/")
    at.radio(key=f"{base}_s_when_instagram_reels").set_value("Live now")
    button(at, "📅 Mark as scheduled").click().run()
    assert not at.exception
    ep = repo.get_episode(eid)
    assert ep.status == Status.PUBLISHED and list(ep.publication.schedule) == ["youtube_shorts"]
    assert any("instagram.com/p/x/ · ✅ live" in m.value for m in at.markdown)
    # The test runner can't change the date box, so YouTube got today 18:00: scheduled until
    # then, live after (both are shown correctly).
    from studio.analytics import platform_states
    yt_state, _ = platform_states(ep)["youtube_shorts"]
    mark = "📅 scheduled, goes live" if yt_state == "scheduled" else "✅ live since"
    assert any("youtube.com/shorts/abc · " + mark in m.value for m in at.markdown)


# ---------------------------------------------------------------- AI in the app
def researching_episode(repo):
    ep = repo.create_episode("The accidental invention of velcro",
                             story_angle="Burrs on a dog after a mountain walk")
    return walk_to(repo, ep.episode_id, Status.RESEARCH).episode_id


def test_without_a_key_the_work_section_explains_how(app):
    repo = EpisodeRepository(load_settings())
    eid = researching_episode(repo)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    assert not at.exception
    assert any("Add a free AI key" in c.value for c in at.caption)
    assert not [b for b in at.button if b.key == f"work_{eid}_researcher_btn_ai"]


def test_run_with_ai_repairs_previews_then_save(app, monkeypatch):
    import ui.research
    from studio.llm import FakeLLM
    repo = EpisodeRepository(load_settings())
    eid = researching_episode(repo)
    agent = get_agent("researcher")
    good = agent.mock(repo.get_episode(eid), repo.settings).model_dump_json()
    fake = FakeLLM(["Here you go: velcro facts!", good])
    monkeypatch.setattr(ui.research, "make_llm", lambda: fake)
    at = app().run()
    at.sidebar.radio[0].set_value("Episode").run()
    box = f"work_{eid}_researcher"
    at.button(key=box + "_btn_ai").click().run()
    assert not at.exception
    assert any("Fake (fake-model) answered in 0 s · 1 repair" in s.value for s in at.success)
    assert "[MOCK]" in at.text_area(key=box).value
    assert any("Preview" in m.value for m in at.markdown)
    assert repo.get_episode(eid).research.claims == []  # not saved yet
    assert at.button(key=box + "_btn_ai_fresh")  # an answer exists: a new one can be asked
    at.button(key=box + "_btn_save").click().run()
    assert repo.list_versions(eid)[-1]["change_note"] == "researcher: AI Fake fake-model, 2 tries"
    assert len(repo.list_ai_calls(eid)) == 2
    assert any("AI log" in t.label for t in at.tabs)
    assert any(c.value.startswith("AI requests today: 0 of 150") for c in at.caption)
