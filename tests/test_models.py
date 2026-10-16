import pytest
from pydantic import ValidationError

from studio.models import (
    Claim,
    ContentKind,
    Episode,
    Metadata,
    Research,
    Script,
    Segment,
    SegmentRole,
    Sentence,
    Source,
)


def test_factual_sentence_requires_claim_id():
    with pytest.raises(ValidationError):
        Sentence(text="Fleming discovered it.", kind=ContentKind.FACTUAL)


def test_creative_sentence_needs_no_claim():
    assert Sentence(text="Wild, right?", kind=ContentKind.CREATIVE).claim_ids == []


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        Metadata(topic="x", surprise_field=1)


def test_bad_url_is_rejected():
    with pytest.raises(ValidationError):
        Source(source_id="S1", title="t", publisher="p", url="not a url", accessed_date="2026-01-01")


def test_duplicate_claim_ids_rejected():
    c = Claim(claim_id="C1", statement="a")
    with pytest.raises(ValidationError):
        Research(claims=[c, c])


def test_script_total_is_derived_from_segments():
    def seg(i, d):
        return Segment(segment_id=f"S{i}", role=SegmentRole.SETUP, duration_sec=d)
    assert Script(segments=[seg(1, 4), seg(2, 6.5)]).total_estimated_sec == 10.5


def test_duplicate_segment_ids_rejected():
    seg = Segment(segment_id="A", role=SegmentRole.HOOK, duration_sec=3)
    with pytest.raises(ValidationError):
        Episode(episode_id="EP_1", metadata=Metadata(topic="t"), script=Script(segments=[seg, seg]))
