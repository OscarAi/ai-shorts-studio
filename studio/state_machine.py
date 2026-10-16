"""The episode life cycle. Python decides what is allowed - never an AI.

Forward path:  IDEA > RESEARCH > FACT_CHECK > SCRIPT > STORYBOARD > PRODUCTION
               > QA > HUMAN_REVIEW > APPROVED > (SCHEDULED) > PUBLISHED
SCHEDULED is optional: an APPROVED episode can also be published straight away.
Going back is allowed (to fix problems) but needs a written reason.
REJECTED can be reached from anywhere except PUBLISHED, and can be revived to IDEA.
PUBLISHED is final.
"""
from .errors import IllegalTransition
from .models import Status

S = Status

# The happy path, in order. Used to tell "forward" from "backward".
STAGE_ORDER = [
    S.IDEA, S.RESEARCH, S.FACT_CHECK, S.SCRIPT, S.STORYBOARD,
    S.PRODUCTION, S.QA, S.HUMAN_REVIEW, S.APPROVED, S.SCHEDULED, S.PUBLISHED,
]

ALLOWED: dict[Status, set[Status]] = {
    S.IDEA:         {S.RESEARCH, S.REJECTED},
    S.RESEARCH:     {S.FACT_CHECK, S.REJECTED},
    S.FACT_CHECK:   {S.SCRIPT, S.RESEARCH, S.REJECTED},
    S.SCRIPT:       {S.STORYBOARD, S.FACT_CHECK, S.RESEARCH, S.REJECTED},
    S.STORYBOARD:   {S.PRODUCTION, S.SCRIPT, S.REJECTED},
    S.PRODUCTION:   {S.QA, S.STORYBOARD, S.REJECTED},
    S.QA:           {S.HUMAN_REVIEW, S.PRODUCTION, S.STORYBOARD, S.SCRIPT, S.REJECTED},
    S.HUMAN_REVIEW: {S.APPROVED, S.QA, S.PRODUCTION, S.STORYBOARD, S.SCRIPT, S.REJECTED},
    S.APPROVED:     {S.SCHEDULED, S.PUBLISHED, S.HUMAN_REVIEW, S.REJECTED},
    S.SCHEDULED:    {S.PUBLISHED, S.APPROVED, S.REJECTED},
    S.PUBLISHED:    set(),
    S.REJECTED:     {S.IDEA},
}


def can_transition(current: Status, target: Status) -> bool:
    return target in ALLOWED[current]


def assert_transition(current: Status, target: Status) -> None:
    if not can_transition(current, target):
        allowed = ", ".join(sorted(s.value for s in ALLOWED[current])) or "none (final state)"
        raise IllegalTransition(
            f"Cannot move from {current.value} to {target.value}. Allowed next states: {allowed}."
        )


def is_backward(current: Status, target: Status) -> bool:
    """True when moving to an earlier stage (a revision). Needs a reason."""
    return (
        current in STAGE_ORDER
        and target in STAGE_ORDER
        and STAGE_ORDER.index(target) < STAGE_ORDER.index(current)
    )


def next_stage(status: Status) -> Status | None:
    """The next step on the happy path, or None for PUBLISHED / REJECTED."""
    if status not in STAGE_ORDER or status == STAGE_ORDER[-1]:
        return None
    return STAGE_ORDER[STAGE_ORDER.index(status) + 1]


def is_forward(current: Status, target: Status) -> bool:
    return (
        current in STAGE_ORDER
        and target in STAGE_ORDER
        and STAGE_ORDER.index(target) > STAGE_ORDER.index(current)
    )
