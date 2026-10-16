"""All custom errors in one place, so messages are consistent and easy to find."""


class StudioError(Exception):
    """Base class for every error this app raises on purpose."""


class EpisodeNotFound(StudioError):
    pass


class IllegalTransition(StudioError):
    pass


class ImmutableEpisodeError(StudioError):
    """Raised when someone tries to edit a PUBLISHED episode."""


class ConcurrentEditError(StudioError):
    """Raised when the episode changed since you loaded it (stale copy)."""


class ProtectedChangeError(StudioError):
    """Raised when an approved claim would be silently changed or removed."""


class SectionOwnershipError(StudioError):
    """Raised when a save touches sections the caller is not allowed to edit."""


class AgentOutputError(StudioError):
    """Raised when an agent's answer (mock or pasted) can't be read or doesn't fit the format."""

    def __init__(self, problems):
        self.problems = list(problems)
        lines = "\n".join(f"  {n}. {p}" for n, p in enumerate(self.problems, start=1))
        super().__init__(f"The answer could not be used:\n{lines}")


class ValidationFailed(StudioError):
    """Raised when deterministic rules block a transition. Holds the issue list."""

    def __init__(self, issues):
        self.issues = list(issues)
        lines = "\n".join(f"  - [{i.code}] {i.message}" for i in self.issues)
        super().__init__(f"Validation failed:\n{lines}")
