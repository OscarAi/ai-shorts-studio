import pytest

from studio.models import ClaimStatus, ContentKind, Episode, Metadata, Sentence, Status
from studio.samples import sample_sections
from studio.validators import check_hook, has_errors, validate_for_transition


def build(**updates):
    ep = Episode(episode_id="EP_000001", metadata=Metadata(
        topic="Penicillin", story_angle="A messy lab bench that changed medicine"))
    return ep.model_copy(update={**sample_sections(), **updates})


def codes(issues):
    return {i.code for i in issues}


def test_complete_episode_passes_every_gate(settings):
    ep = build()
    issues = validate_for_transition(ep, Status.HUMAN_REVIEW, settings)
    assert not has_errors(issues)


def test_claim_without_source_blocks_fact_check(settings):
    ep = build()
    ep.research.claims[1].source_ids = []
    assert "CLAIM_NO_SOURCE" in codes(validate_for_transition(ep, Status.FACT_CHECK, settings))


def test_high_importance_claim_needs_two_publishers(settings):
    ep = build()
    ep.research.claims[0].source_ids = ["S001"]  # only one publisher
    assert "HIGH_CLAIM_WEAK_SOURCING" in codes(validate_for_transition(ep, Status.FACT_CHECK, settings))


def test_unapproved_fact_check_blocks_script(settings):
    ep = build()
    ep.fact_check.approved = False
    assert "FACT_CHECK_NOT_APPROVED" in codes(validate_for_transition(ep, Status.SCRIPT, settings))


def test_pending_claim_blocks_script(settings):
    ep = build()
    ep.research.claims[2].status = ClaimStatus.NEEDS_REVIEW
    assert "CLAIMS_PENDING" in codes(validate_for_transition(ep, Status.SCRIPT, settings))


def test_script_citing_unapproved_claim_blocks_storyboard(settings):
    ep = build()
    ep.research.claims[2].status = ClaimStatus.REJECTED
    assert "CLAIM_NOT_APPROVED" in codes(validate_for_transition(ep, Status.STORYBOARD, settings))


def test_script_citing_unknown_claim_blocks_storyboard(settings):
    ep = build()
    ep.script.segments[1].sentences[0] = Sentence(text="x", kind=ContentKind.FACTUAL, claim_ids=["C999"])
    assert "CLAIM_UNKNOWN" in codes(validate_for_transition(ep, Status.STORYBOARD, settings))


def test_script_too_long_blocks_storyboard(settings):
    ep = build()
    ep.script.segments[0].duration_sec = 40
    assert "DURATION_OUT_OF_RANGE" in codes(validate_for_transition(ep, Status.STORYBOARD, settings))


def test_segment_without_shot_blocks_production(settings):
    ep = build()
    ep.storyboard.shots = ep.storyboard.shots[:-1]
    found = codes(validate_for_transition(ep, Status.PRODUCTION, settings))
    assert "SEGMENT_NO_SHOT" in found


def test_shot_duration_mismatch_blocks_production(settings):
    ep = build()
    ep.storyboard.shots[0].duration_sec = 20
    assert "SHOT_DURATION_MISMATCH" in codes(validate_for_transition(ep, Status.PRODUCTION, settings))


def test_ungenerated_shot_blocks_qa(settings):
    ep = build()
    ep.storyboard.shots[0].status = "planned"
    assert "SHOT_NOT_READY" in codes(validate_for_transition(ep, Status.QA, settings))


def test_failed_qa_blocks_human_review(settings):
    ep = build()
    ep.qa.approved = False
    assert "QA_NOT_APPROVED" in codes(validate_for_transition(ep, Status.HUMAN_REVIEW, settings))


def test_publication_needs_title_and_platform(settings):
    ep = build(status=Status.APPROVED)
    ep.publication.title = ""
    ep.publication.platforms = []
    found = codes(validate_for_transition(ep, Status.PUBLISHED, settings))
    assert {"PUB_NO_TITLE", "PUB_NO_PLATFORM"} <= found


def test_backward_moves_are_not_validated(settings):
    ep = build(status=Status.QA)
    ep.qa.approved = False
    assert validate_for_transition(ep, Status.SCRIPT, settings) == []


# ------------------------------------------------- story angle (leaving IDEA)
def idea(topic="Velcro", angle=""):
    return Episode(episode_id="EP_000001", metadata=Metadata(topic=topic, story_angle=angle))


def test_an_idea_needs_a_story_angle(settings):
    assert "ANGLE_MISSING" in codes(validate_for_transition(idea(), Status.RESEARCH, settings))
    assert "ANGLE_MISSING" in codes(validate_for_transition(idea(angle="dog"), Status.RESEARCH,
                                                            settings))  # too short
    assert not validate_for_transition(idea(angle="Burrs on a dog after a mountain walk"),
                                       Status.RESEARCH, settings)


def test_angle_must_not_just_repeat_the_topic(settings):
    ep = idea(topic="The invention of Velcro", angle="the invention of velcro!")
    assert "ANGLE_SAME_AS_TOPIC" in codes(validate_for_transition(ep, Status.RESEARCH, settings))


def test_older_episodes_past_idea_are_not_blocked(settings):
    """Like EP_000003: no angle, already past IDEA, angle now locked."""
    ep = build().model_copy(update={"status": Status.STORYBOARD,
                                    "metadata": Metadata(topic="dumplings")})
    assert "ANGLE_MISSING" not in codes(validate_for_transition(ep, Status.PRODUCTION, settings))


def test_angle_rule_can_be_turned_off(settings):
    off = settings.model_copy(update={"min_story_angle_chars": 0})
    assert not validate_for_transition(idea(), Status.RESEARCH, off)


# ------------------------------------------------------------------ the hook
@pytest.mark.parametrize("hook, codes", [
    ("Some dumplings were never meant to be eaten.", []),
    ("Ketchup was once sold as a medicine.", []),
    ("History hides strange dumplings.", []),  # "hi" only matches the whole word
    ("What if your kitchen's favorite gadget started with a ruined snack?", ["HOOK_GENERIC"]),
    ("Did you know ketchup was medicine?", ["HOOK_GENERIC"]),
    ("Let's talk about the strangest dumplings ever found in a tomb in China.",
     ["HOOK_GENERIC"]),
    ("This small round dumpling found in a dry desert tomb in China is more than "
     "a thousand years old.", ["HOOK_TOO_LONG"]),
])
def test_hook_rules_are_warnings(settings, hook, codes):
    issues = check_hook(hook, settings)
    assert [i.code for i in issues] == codes
    assert all(i.severity == "warning" for i in issues)


def test_hook_length_rule_can_be_turned_off(settings):
    long_hook = " ".join(["word"] * 30)
    assert check_hook(long_hook, settings.model_copy(update={"hook_max_words": 0})) == []
