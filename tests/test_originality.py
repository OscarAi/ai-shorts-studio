import pytest
from helpers import open_gate

from studio.agents import get_agent, run_mock, run_mock_pipeline
from studio.errors import ProtectedChangeError, StudioError, ValidationFailed
from studio.models import Category, Episode, Metadata, Status
from studio.originality import Verdict, evaluate
from studio.samples import sample_sections


def ep(eid, topic, angle="", status=Status.IDEA, **updates):
    base = Episode(episode_id=eid, status=status,
                   metadata=Metadata(topic=topic, story_angle=angle))
    return base.model_copy(update=updates)


def kinds(report):
    return {m.kind for m in report.matches}


# ---------------------------------------------------------------- engine
def test_empty_library_passes(settings):
    r = evaluate(ep("EP_1", "Anything"), [], settings)
    assert r.verdict == Verdict.PASS and "no other episodes" in r.notes[0]


def test_unrelated_ideas_pass(settings):
    r = evaluate(ep("EP_2", "Penicillin mold"), [ep("EP_1", "Great Wall of China")], settings)
    assert r.verdict == Verdict.PASS and r.matches == []


def test_identical_title_suggests_block(settings):
    a = ep("EP_1", "The accidental invention of the microwave")
    b = ep("EP_2", "The accidental invention of the microwave")
    r = evaluate(b, [a], settings)
    assert r.verdict == Verdict.SUGGEST_BLOCK and "near_duplicate_title" in kinds(r)


def test_same_subject_distinct_angle_is_not_flagged(settings):
    old = ep("EP_1", "Eiffel Tower secret apartment",
             "Gustave Eiffel kept a private flat at the top and hosted Thomas Edison")
    new = ep("EP_2", "Eiffel Tower hidden apartment secrets",
             "The flat was later rented to scientists for weather experiments")
    r = evaluate(new, [old], settings)
    assert r.verdict == Verdict.PASS
    assert "same_subject_distinct_angle" in kinds(r)


def test_overlapping_topic_without_angle_needs_review(settings):
    old = ep("EP_1", "Eiffel Tower secret apartment")
    new = ep("EP_2", "Eiffel Tower hidden apartment secrets")
    assert evaluate(new, [old], settings).verdict == Verdict.REVIEW


def test_rejected_ideas_are_ignored_by_default(settings):
    old = ep("EP_1", "The accidental invention of the microwave", status=Status.REJECTED)
    new = ep("EP_2", "The accidental invention of the microwave")
    assert evaluate(new, [old], settings).verdict == Verdict.PASS


def test_reused_claims_suggest_block_even_with_different_topics(settings):
    old = ep("EP_1", "Penicillin story").model_copy(update={"research": sample_sections()["research"]})
    new = ep("EP_2", "How mold saved soldiers").model_copy(update={"research": sample_sections()["research"]})
    r = evaluate(new, [old], settings)
    assert r.verdict == Verdict.SUGGEST_BLOCK and "heavy_claim_overlap" in kinds(r)


def test_reworded_script_is_caught(settings):
    script = sample_sections()["script"]
    old = ep("EP_1", "Penicillin story").model_copy(update={"script": script})
    new = ep("EP_2", "How mold saved soldiers").model_copy(update={"script": script})
    assert "near_duplicate_script" in kinds(evaluate(new, [old], settings))


def test_ai_judge_can_raise_a_review(settings):
    class FakeJudge:
        def similarity(self, a, b):
            return 0.95
    old = ep("EP_1", "Eiffel Tower secret apartment", "Eiffel kept a private flat at the top")
    new = ep("EP_2", "Eiffel Tower hidden apartment secrets", "Later rented to scientists")
    r = evaluate(new, [old], settings, judge=FakeJudge())
    assert r.verdict == Verdict.REVIEW and "semantic_similarity" in kinds(r)


# ------------------------------------------------- stored checks and gate
def test_check_is_stored_and_retrievable(repo):
    a = repo.create_episode("Penicillin mold", Category.INVENTIONS)
    report = repo.check_originality(a.episode_id)
    latest = repo.latest_originality(a.episode_id)
    assert latest["id"] == report.check_id and latest["verdict"] == "PASS"
    assert latest["report"].check_id == report.check_id


def test_cannot_start_research_without_a_check(repo):
    a = repo.create_episode("Penicillin mold")
    with pytest.raises(ValidationFailed) as err:
        repo.transition(a.episode_id, Status.RESEARCH, actor="x")
    assert err.value.issues[0].code == "ORIGINALITY_NOT_CHECKED"


def test_passing_check_opens_the_gate(repo):
    a = repo.create_episode("Penicillin mold", story_angle="The plate Fleming almost threw away")
    repo.check_originality(a.episode_id)
    assert repo.originality_gate_state(a.episode_id) == "pass"
    assert repo.transition(a.episode_id, Status.RESEARCH, actor="x").status == Status.RESEARCH


def test_flagged_idea_is_blocked_until_you_decide(repo):
    repo.create_episode("The accidental invention of the microwave")
    b = repo.create_episode("The accidental invention of the microwave",
                            story_angle="The engineer behind it, not the candy bar")
    report = repo.check_originality(b.episode_id)
    assert report.verdict == Verdict.SUGGEST_BLOCK
    with pytest.raises(ValidationFailed) as err:
        repo.transition(b.episode_id, Status.RESEARCH, actor="x")
    assert err.value.issues[0].code == "ORIGINALITY_NEEDS_DECISION"

    with pytest.raises(StudioError):  # a note is required for flagged ideas
        repo.decide_originality(report.check_id, "accept", actor="human")
    repo.decide_originality(report.check_id, "accept", actor="human", note="different story")
    assert repo.originality_gate_state(b.episode_id) == "accepted"
    assert repo.transition(b.episode_id, Status.RESEARCH, actor="x").status == Status.RESEARCH


def test_declined_idea_stays_blocked(repo):
    repo.create_episode("The accidental invention of the microwave")
    b = repo.create_episode("The accidental invention of the microwave")
    report = repo.check_originality(b.episode_id)
    repo.decide_originality(report.check_id, "reject", actor="human", note="too similar")
    with pytest.raises(ValidationFailed) as err:
        repo.transition(b.episode_id, Status.RESEARCH, actor="x")
    assert err.value.issues[0].code == "ORIGINALITY_DECLINED"


def test_editing_the_topic_makes_the_check_stale(repo):
    a = repo.create_episode("Penicillin mold")
    repo.check_originality(a.episode_id)
    a.metadata.topic = "A completely different topic"
    repo.save(a, actor="human", allowed_sections={"metadata"})
    assert repo.originality_gate_state(a.episode_id) == "stale"
    with pytest.raises(ValidationFailed):
        repo.transition(a.episode_id, Status.RESEARCH, actor="x")


def test_open_gate_helper_handles_flagged_ideas(repo):
    repo.create_episode("The accidental invention of the microwave")
    b = repo.create_episode("The accidental invention of the microwave")
    open_gate(repo, b.episode_id)
    assert repo.transition(b.episode_id, Status.RESEARCH, actor="x").status == Status.RESEARCH


# ------------------------------------- short topics and series boilerplate
PENICILLIN_EP = "Penicillin: the accidental mold that changed medicine"


def test_one_word_topic_inside_another_needs_review(settings):
    r = evaluate(ep("EP_2", "Penicillin"), [ep("EP_1", PENICILLIN_EP)], settings)
    assert r.verdict == Verdict.REVIEW and "topic_contained" in kinds(r)


def test_longer_topic_inside_another_suggests_block(settings):
    r = evaluate(ep("EP_2", "the accidental mold that changed medicine"),
                 [ep("EP_1", PENICILLIN_EP)], settings)
    assert r.verdict == Verdict.SUGGEST_BLOCK and "topic_contained" in kinds(r)


def test_contained_topic_with_distinct_angle_is_fine(settings):
    old = ep("EP_1", PENICILLIN_EP, "Fleming's messy lab and the lucky contaminated plate")
    new = ep("EP_2", "Penicillin", "How factories raced to mass-produce it for D-Day")
    r = evaluate(new, [old], settings)
    assert r.verdict == Verdict.PASS and "same_subject_distinct_angle" in kinds(r)


def test_series_boilerplate_is_not_a_duplicate(settings):
    old = ep("EP_1", "The accidental invention of the microwave")
    new = ep("EP_2", "The accidental invention of velcro")
    assert evaluate(new, [old], settings).verdict == Verdict.PASS


def test_short_topic_inside_a_mixed_title_needs_review(settings):
    r = evaluate(ep("EP_2", "Microwave"), [ep("EP_1", "Penicillin and the microwave oven")],
                 settings)
    assert r.verdict == Verdict.REVIEW


def test_ignore_words_are_configurable(settings):
    strict = settings.model_copy(update={"originality_ignore_words": []})
    old = ep("EP_1", "The accidental invention of the microwave")
    new = ep("EP_2", "The accidental invention of velcro")
    assert evaluate(new, [old], strict).verdict != Verdict.PASS


# ------------------------------- topic changes after the idea stage (bug fix)
def _at_research(repo, topic):
    a = repo.create_episode(topic)
    open_gate(repo, a.episode_id)
    return repo.transition(a.episode_id, Status.RESEARCH, actor="x")


def test_topic_changed_during_research_must_be_rechecked(repo):
    a = _at_research(repo, "Penicillin")
    a.metadata.topic = "Dumplings"
    repo.save(a, actor="human", allowed_sections={"metadata"})
    run_mock(repo, a.episode_id, get_agent("researcher"))
    with pytest.raises(ValidationFailed) as err:
        repo.transition(a.episode_id, Status.FACT_CHECK, actor="x")
    assert err.value.issues[0].code == "ORIGINALITY_STALE"

    repo.check_originality(a.episode_id, stage="research")  # a research-stage check counts
    assert repo.originality_gate_state(a.episode_id) == "pass"
    assert repo.transition(a.episode_id, Status.FACT_CHECK, actor="x").status == Status.FACT_CHECK


def test_new_topic_during_research_can_be_flagged(repo):
    repo.create_episode("The accidental invention of the microwave")
    b = _at_research(repo, "Velcro")
    b.metadata.topic = "The accidental invention of the microwave"
    repo.save(b, actor="human", allowed_sections={"metadata"})
    report = repo.check_originality(b.episode_id, stage="research")
    assert report.verdict == Verdict.SUGGEST_BLOCK
    with pytest.raises(ValidationFailed) as err:
        repo.transition(b.episode_id, Status.FACT_CHECK, actor="x")
    assert err.value.issues[0].code == "ORIGINALITY_NEEDS_DECISION"


def test_topic_is_locked_after_research(repo):
    a = repo.create_episode("Velcro")
    open_gate(repo, a.episode_id)
    a = run_mock_pipeline(repo, a.episode_id)  # now PRODUCTION
    with pytest.raises(ProtectedChangeError):
        repo.save(a.model_copy(update={"metadata": a.metadata.model_copy(
            update={"topic": "Something else"})}), actor="human")
    with pytest.raises(ProtectedChangeError):
        repo.save(a.model_copy(update={"metadata": a.metadata.model_copy(
            update={"story_angle": "New angle"})}), actor="human")
    ok = repo.save(a.model_copy(update={"metadata": a.metadata.model_copy(
        update={"hook_style": "question"})}), actor="human")  # other fields stay editable
    assert ok.metadata.hook_style == "question"
