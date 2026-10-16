"""Run an agent with an AI service, and let the AI fix its own mistakes before you see it.

  1. The same prompt as copy-paste (agent.build_prompt, including blocked sites) is sent.
  2. The answer is read and previewed exactly like a pasted one (agents.preview).
  3. If it can't be read, or breaks a rule the AI itself can fix (e.g. a script sentence that
     uses an unapproved fact), the problems are sent back: "fix these, answer again".
     At most `max_repairs` times. Link checks and originality are never sent back: those are yours.
  4. NOTHING is saved here. You look at the preview and click Save (save_ai_answer).
Every request is written to the AI log (repo.log_ai_call).

Safeguards: one run at a time in the app (a second click, a refreshed page or another tab is
told to wait instead of sending the same requests again), and each run has a budget of
requests and seconds (config/ai.yaml max_requests_per_run / max_seconds_per_run).
"""
import math
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field

from .agents import WORDS_PER_SEC, Agent, check_stage, parse_response, preview, run_agent
from .db import EpisodeRepository
from .errors import AgentOutputError, StudioError
from .llm import NoAIAvailable, answer_schema, load_ai_config
from .models import MOCK_URL_PREFIX, Episode, LinkStatus
from .validators import Issue

# Rule problems an AI can fix by answering again (per agent). Everything else is the human's.
# Some are warnings (a weak hook): the AI is asked once more, but they never block saving.
FIXABLE = {
    "researcher": {"NO_CLAIMS", "CLAIM_NO_SOURCE", "SOURCE_MISSING"},
    "fact_checker": set(),
    "scriptwriter": {"HOOK_EMPTY", "NO_SEGMENTS", "SEGMENT_EMPTY", "CLAIM_UNKNOWN",
                     "CLAIM_NOT_APPROVED", "NO_FACTUAL_SENTENCES", "DURATION_OUT_OF_RANGE",
                     "HOOK_TOO_LONG", "HOOK_GENERIC"},
    "storyboarder": {"NO_SHOTS", "TOO_MANY_SHOTS", "SHOT_NO_SEGMENT", "SHOT_UNKNOWN_SEGMENT",
                     "SHOT_NO_PROMPT", "SEGMENT_NO_SHOT", "SHOT_DURATION_MISMATCH"},
}
MAX_ANSWER_IN_REPAIR = 15000  # characters of the previous answer sent back


@dataclass
class Attempt:
    provider: str
    model: str
    seconds: float
    problems: list[str] = field(default_factory=list)


@dataclass
class AiResult:
    text: str                       # the final answer (goes into the paste box)
    episode: Episode | None         # the preview, or None if the answer can't be used
    issues: list[Issue]             # what still blocks the next step
    problems: list[str]             # why the answer can't be used (empty if it can)
    attempts: list[Attempt]
    reused: bool = False            # an earlier answer to the same question (no request)

    @property
    def usable(self) -> bool:
        return self.episode is not None

    @property
    def repairs(self) -> int:
        return len(self.attempts) - 1

    @property
    def summary(self) -> str:
        last = self.attempts[-1]
        if self.reused:
            return (f"Reused the earlier {last.provider} ({last.model}) answer to this exact "
                    "question (no request used)")
        seconds = round(sum(a.seconds for a in self.attempts), 1)
        fixes = (f" · {self.repairs} repair{'s' if self.repairs != 1 else ''}"
                 if self.repairs else "")
        return f"{last.provider} ({last.model}) answered in {seconds:g} s{fixes}"

    @property
    def note(self) -> str:
        """For the version history."""
        last = self.attempts[-1]
        return f"AI {last.provider} {last.model}, {len(self.attempts)} " \
               f"tr{'ies' if len(self.attempts) != 1 else 'y'}"


def word_budget_problems(ep: Episode, s) -> list[str]:
    """The AI guesses durations; Python counts words. A script whose words don't fit the
    allowed length would fail later, when the real narration is measured."""
    words = sum(len(x.text.split()) for g in ep.script.segments for x in g.sentences)
    low = math.ceil(s.min_duration_sec * WORDS_PER_SEC)
    high = math.floor(s.max_duration_sec * WORDS_PER_SEC)
    if low <= words <= high:
        return []
    return [f"WORD_COUNT: the narration has {words} words; it must have {low}-{high} words "
            f"(about {round(words / WORDS_PER_SEC)} s now; it must last {s.min_duration_sec:g}-"
            f"{s.max_duration_sec:g} s at 2.5 words per second). Rewrite to fit, and set each "
            "duration_sec to its words / 2.5."]


def invented_links(ep: Episode, fetcher, seen: dict) -> list[str]:
    """AIs often invent plausible URLs. Open each new source link once; pages that don't
    exist go back to the AI. (Blocked or slow sites are not its fault: you check those.)"""
    problems = []
    for src in ep.research.sources:
        if src.declined or src.url.startswith(MOCK_URL_PREFIX):
            continue
        if src.url not in seen:
            seen[src.url] = fetcher.fetch(src.url)
        r = seen[src.url]
        if r.link_status == LinkStatus.BROKEN:
            problems.append(f"BROKEN_LINK: {src.source_id} {src.url} does not exist ({r.note}). "
                            "Replace it with a page you are sure exists (an encyclopedia page "
                            "is fine for one source), or remove the source.")
    return problems


def _check(repo: EpisodeRepository, episode_id: str, agent: Agent, text: str,
           fetcher=None, seen: dict | None = None
           ) -> tuple[Episode | None, list[Issue], list[str]]:
    """(preview episode, blocking issues, problems the AI must fix)."""
    try:
        new, issues = preview(repo, episode_id, agent, parse_response(agent, text))
    except AgentOutputError as e:
        return None, [], list(e.problems)
    errors = [i for i in issues if i.severity == "error"]
    fixable = [f"{i.code}: {i.message}" for i in issues if i.code in FIXABLE[agent.name]]
    if agent.name == "scriptwriter":
        fixable += word_budget_problems(new, repo.settings)
    if agent.name == "researcher" and fetcher is not None:
        fixable += invented_links(new, fetcher, {} if seen is None else seen)
    return new, errors, fixable


def repair_prompt(prompt: str, answer: str, problems: list[str]) -> str:
    return (f"{prompt}\n\n---\nYour previous answer was:\n{answer[:MAX_ANSWER_IN_REPAIR]}\n\n"
            "It has these problems:\n" + "\n".join(f"- {p}" for p in problems) +
            "\n\nFix them and answer again with the COMPLETE corrected JSON only, in the same "
            "format as above. Do not add facts that are not in the approved list.")


def reuse_earlier(repo: EpisodeRepository, episode_id: str, agent: Agent, prompt: str
                  ) -> AiResult | None:
    """The same question was already answered well (nothing to fix): use that answer again
    instead of spending a request. Repair prompts start with the original prompt."""
    for call in reversed(repo.list_ai_calls(episode_id)):
        if call["agent"] != agent.name or not call["prompt"].startswith(prompt):
            continue
        if call["problems"]:
            continue
        new, issues, problems = _check(repo, episode_id, agent, call["answer"])
        if new is None or problems:
            continue
        return AiResult(call["answer"], new, issues, [],
                        [Attempt(call["provider"], call["model"], 0.0)], reused=True)
    return None


_RUNNING = threading.Lock()  # one AI run at a time in this app (all browser tabs share it)
_running_since: list[tuple[str, float]] = []  # (what, when) of the run holding the lock


def ask_agent(repo: EpisodeRepository, episode_id: str, agent: Agent, llm,
              max_repairs: int | None = None, *, fresh: bool = False,
              fetcher=None) -> AiResult:
    """Ask an AI for this stage's answer, with up to `max_repairs` self-repairs. Saves nothing.
    An identical earlier question with a good answer is reused (no request) unless `fresh`.
    With a `fetcher` (sources.HttpFetcher), the researcher's links are opened and invented
    ones are sent back for repair. Only one run at a time; each within its budget."""
    if not _RUNNING.acquire(blocking=False):
        what, since = _running_since[0] if _running_since else ("an AI step", time.monotonic())
        raise StudioError(f"The AI is still working on {what} (for {time.monotonic() - since:.0f} "
                          "s). Wait for it to finish: its answer appears there. No new request "
                          "was sent.")
    _running_since[:] = [(f"{episode_id} {agent.label.lower()}", time.monotonic())]
    try:
        budget = llm.run_budget() if hasattr(llm, "run_budget") else nullcontext()
        with budget:
            return _ask(repo, episode_id, agent, llm, max_repairs, fresh=fresh, fetcher=fetcher)
    finally:
        _running_since.clear()
        _RUNNING.release()


def _ask(repo: EpisodeRepository, episode_id: str, agent: Agent, llm,
         max_repairs: int | None, *, fresh: bool, fetcher) -> AiResult:
    seen: dict = {}
    if max_repairs is None:
        max_repairs = load_ai_config().max_repairs
    ep = repo.get_episode(episode_id)
    check_stage(agent, ep)
    base = agent.build_prompt(ep, repo.settings, repo.blocked_sites())
    schema = answer_schema(agent.output_model)
    if not fresh:
        earlier = reuse_earlier(repo, episode_id, agent, base)
        if earlier is not None:
            return earlier
    prompt, attempts = base, []
    result: AiResult | None = None
    for n in range(1, max_repairs + 2):
        try:
            reply = llm.complete(prompt, temperature=agent.temperature, schema=schema)
        except NoAIAvailable:
            if result is None:
                raise
            break  # keep the best answer so far
        new, issues, problems = _check(repo, episode_id, agent, reply.text, fetcher, seen)
        attempts.append(Attempt(reply.provider, reply.model, reply.seconds, problems))
        repo.log_ai_call(episode_id=episode_id, agent=agent.name, attempt=n,
                         provider=reply.provider, model=reply.model, seconds=reply.seconds,
                         prompt=prompt, answer=reply.text, problems=problems)
        unreadable = new is None
        result = AiResult(reply.text, new, issues, problems if unreadable else [], attempts)
        if not problems:
            break
        if len(attempts) > 1 and problems == attempts[-2].problems:
            break  # the same problems again: another request would be wasted
        prompt = repair_prompt(base, reply.text, problems)
    assert result is not None  # the first request either answers or raises
    return result


def save_ai_answer(repo: EpisodeRepository, episode_id: str, agent: Agent, result: AiResult, *,
                   actor: str) -> Episode:
    """Your decision to keep the AI's answer: saved like a pasted one (same rules)."""
    return run_agent(repo, episode_id, agent, parse_response(agent, result.text),
                     actor=actor, note=result.note)
