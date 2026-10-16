import itertools

import pytest

from studio.errors import IllegalTransition
from studio.models import Status
from studio.state_machine import (
    ALLOWED,
    STAGE_ORDER,
    assert_transition,
    can_transition,
    is_backward,
    is_forward,
)

S = Status


def test_every_status_has_rules():
    assert set(ALLOWED) == set(Status)


def test_happy_path_is_fully_legal():
    for a, b in itertools.pairwise(STAGE_ORDER):
        assert can_transition(a, b)


def test_cannot_skip_stages():
    assert not can_transition(S.IDEA, S.SCRIPT)
    assert not can_transition(S.RESEARCH, S.PRODUCTION)
    with pytest.raises(IllegalTransition):
        assert_transition(S.IDEA, S.PUBLISHED)


def test_published_is_final():
    for s in Status:
        assert not can_transition(S.PUBLISHED, s)


def test_rejected_can_only_be_revived_to_idea():
    assert ALLOWED[S.REJECTED] == {S.IDEA}


def test_anything_unpublished_can_be_rejected():
    for s in Status:
        if s not in (S.PUBLISHED, S.REJECTED):
            assert can_transition(s, S.REJECTED)


def test_backward_and_forward():
    assert is_backward(S.QA, S.SCRIPT)
    assert is_forward(S.IDEA, S.RESEARCH)
    assert not is_backward(S.QA, S.REJECTED)
