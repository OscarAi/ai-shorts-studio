"""Command line entry point.   Usage:  python cli.py <command> --help

Every command is a function registered with @command(name, help, *options): its options
sit right above its code. build_parser() turns that table into the argparse parser.
"""
import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from studio import analytics, declines, health, llm
from studio.agents import (
    AGENTS,
    agent_for_status,
    get_agent,
    parse_response,
    run_agent,
    run_mock,
    run_mock_pipeline,
)
from studio.ai_runner import ask_agent, save_ai_answer
from studio.assets import add_narration, add_shot_asset, made_today
from studio.config import PROJECT_ROOT, load_settings
from studio.dashboard_data import readiness
from studio.db import EpisodeRepository
from studio.errors import StudioError, ValidationFailed
from studio.guide import guide
from studio.ideas import build_ideas_prompt, create_from_idea, parse_ideas, screen_ideas
from studio.logging_utils import setup_logging
from studio.models import Category, Episode, Status
from studio.platforms import PLATFORMS, RULES, check_post, draft_post
from studio.render import build_video
from studio.review import (
    QA_CHECKS,
    add_links,
    automatic_qa,
    decline_source,
    default_description,
    mark_live,
    mark_published,
    mark_scheduled,
    set_claim_status,
    set_fact_check,
    set_publication,
    set_qa,
)
from studio.samples import sample_sections
from studio.sources import HttpFetcher, mark_source_verified, verify_sources
from studio.state_machine import STAGE_ORDER
from studio.subtitles import default_aligner, make_subtitles, read_cues
from studio.tools import Tools, load_tools
from studio.tts import VOICES, default_tts, generate_narration, use_narration

ME = "human"  # the actor recorded for everything typed in this terminal


# ------------------------------------------------------------------ the command table
@dataclass(frozen=True)
class Opt:
    flags: tuple[str, ...]
    kwargs: dict


def opt(*flags: str, **kwargs) -> Opt:
    """One argparse argument, written next to the command that uses it."""
    return Opt(flags, kwargs)


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    options: tuple[Opt, ...]
    run: Callable[[EpisodeRepository, argparse.Namespace], None]


COMMANDS: list[Command] = []


def command(name: str, help_text: str, *options: Opt):
    def register(fn):
        COMMANDS.append(Command(name, help_text, options, fn))
        return fn
    return register


EPISODE = opt("episode_id")
CATEGORIES = [c.value for c in Category]
STATUSES = [s.value for s in Status]
AGENT_NAMES = list(AGENTS)
PLATFORM_NAMES = list(PLATFORMS)


# ------------------------------------------------------------------------ helpers
def make_repo() -> EpisodeRepository:
    settings = load_settings()
    setup_logging(settings.log_dir, settings.log_level)
    repo = EpisodeRepository(settings)
    repo.auto_backup()
    return repo


def read_text(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise StudioError(f"File not found: {path}")
    return p.read_text(encoding="utf-8")


def read_bytes(path: str) -> tuple[bytes, str]:
    p = Path(path)
    if not p.is_file():
        raise StudioError(f"File not found: {path}")
    return p.read_bytes(), p.name


def print_blockers(repo: EpisodeRepository, episode_id: str) -> None:
    """After a save: what still stands between the episode and its next step (the same rules
    the dashboard's Continue button uses)."""
    target, issues = readiness(repo.get_episode(episode_id), repo)
    if target is None:
        return
    if not issues:
        print(f"Ready for {target.value}:  python cli.py advance {episode_id} {target.value}")
    for i in issues:
        print(f"  [{i.severity}] {i.code}: {i.message}")


def print_report(report) -> None:
    print(f"Originality verdict: {report.verdict.value}  "
          f"(compared with {report.compared_against} episode(s))")
    for m in report.matches:
        print(f"  [{m.severity:<6}] {m.episode_id} {m.kind}  score {m.score}  - {m.detail}")
    for n in report.notes:
        print(f"  note: {n}")
    if report.check_id is not None:
        print(f"Check id: {report.check_id}")


def tool_and_license(tools, args) -> tuple[str, str]:
    """--tool fills the made-with name and license note from config/tools.yaml."""
    if args.tool:
        tool = Tools.find(tools, args.tool)
        if tool is None:
            raise StudioError(f"Unknown tool '{args.tool}'. See: python cli.py tools")
        return tool.name, args.license or tool.license_note
    if not args.license:
        raise StudioError("Give --tool (see: python cli.py tools) or --license.")
    return args.made_with, args.license


# ------------------------------------------------------------------- episodes
@command("init", "Create the database")
def cmd_init(repo, args):
    print(f"Database ready at: {repo.db_path}")


@command("new", "Create a new episode idea",
         opt("--topic", required=True),
         opt("--category", choices=CATEGORIES, help="default: settings.yaml default_category"),
         opt("--series", default=""),
         opt("--angle", default="", help="what makes this video different (needed before "
                                         "research)"))
def cmd_new(repo, args):
    ep = repo.create_episode(args.topic, args.category or repo.settings.default_category,
                             series=args.series, actor=ME, story_angle=args.angle)
    print(f"Created {ep.episode_id}  [{ep.status.value}]  {ep.metadata.topic}")


@command("list", "List episodes",
         opt("--status", choices=STATUSES), opt("--category", choices=CATEGORIES),
         opt("--search"))
def cmd_list(repo, args):
    rows = repo.list_episodes(status=args.status, category=args.category, search=args.search)
    if not rows:
        print("No episodes found.")
        return
    for r in rows:
        print(f"{r['episode_id']}  {r['status']:<13} {r['category']:<11} "
              f"v{r['current_version']:<3} {r['topic']}")
    print(f"\n{len(rows)} episode(s).")


@command("show", "Show one episode",
         EPISODE, opt("--json", action="store_true", help="print the full record"))
def cmd_show(repo, args):
    ep = repo.get_episode(args.episode_id)
    if args.json:
        print(ep.model_dump_json(indent=2))
        return
    print(f"{ep.episode_id}  v{ep.version}  [{ep.status.value}]")
    print(f"Topic:    {ep.metadata.topic}")
    print(f"Series:   {ep.metadata.series or '-'}   Category: {ep.metadata.category.value}")
    print(f"Claims:   {len(ep.research.claims)}   Sources: {len(ep.research.sources)}")
    print(f"Script:   {len(ep.script.segments)} segments, {ep.script.total_estimated_sec}s")
    print(f"Shots:    {len(ep.storyboard.shots)}")
    if ep.rejection:
        print(f"Rejected: {ep.rejection.reason} (was {ep.rejection.from_status.value})")


@command("advance", "Move an episode to another status",
         EPISODE, opt("status", choices=STATUSES),
         opt("--reason", help="required when going backward or reviving"))
def cmd_advance(repo, args):
    ep = repo.transition(args.episode_id, args.status, actor=ME, reason=args.reason or "")
    print(f"{ep.episode_id} is now {ep.status.value} (v{ep.version})")


@command("reject", "Reject an episode (reason required)",
         EPISODE, opt("--reason", required=True))
def cmd_reject(repo, args):
    repo.transition(args.episode_id, Status.REJECTED, actor=ME, reason=args.reason)
    print(f"{args.episode_id} rejected: {args.reason}")


@command("next", "What to do next for an episode", EPISODE)
def cmd_next(repo, args):
    g = guide(repo.get_episode(args.episode_id), repo)
    step = f"Step {g.stage_number} of {len(STAGE_ORDER)} - " if g.stage_number else ""
    print(f"{args.episode_id}: {step}{g.title}")
    for s in g.steps:
        print(f"  [{'x' if s.done else ' '}] {s.label}" + ("  (optional)" if s.optional else ""))
    if g.ready:
        print(f"Ready:  python cli.py advance {args.episode_id} {g.target.value}")
    for i in g.blockers:
        print(f"  - {i.message}")
    if g.go_back_to:
        print(f"Fix it at {g.go_back_to.value}:  python cli.py advance {args.episode_id} "
              f'{g.go_back_to.value} --reason "fix: ..."')


@command("history", "Show every saved version of an episode", EPISODE)
def cmd_history(repo, args):
    for v in repo.list_versions(args.episode_id):
        print(f"v{v['version']:<3} {v['saved_at']}  {v['actor']:<8} {v['status']:<13} "
              f"{v['change_note']}")


# ---------------------------------------------------------------- originality
@command("originality", "Run the originality check on an episode",
         EPISODE, opt("--stage", choices=["idea", "research", "script"], default="idea"))
def cmd_originality(repo, args):
    report = repo.check_originality(args.episode_id, stage=args.stage, actor=ME)
    print_report(report)
    if report.verdict.value != "PASS":
        print(f"\nTo go ahead anyway:  python cli.py decide-originality {report.check_id} "
              f'accept --note "why this is different enough"')


@command("decide-originality", "Accept or reject a flagged idea",
         opt("check_id", type=int), opt("decision", choices=["accept", "reject"]),
         opt("--note", help="required when the verdict was not PASS"))
def cmd_decide(repo, args):
    repo.decide_originality(args.check_id, args.decision, actor=ME, note=args.note or "")
    print(f"Recorded: {args.decision}")


# ---------------------------------------------------------------------- agents
@command("prompt", "Print the prompt for an agent (to paste into a chat AI)",
         EPISODE, opt("agent", choices=AGENT_NAMES),
         opt("--out", help="write the prompt to this file instead of the screen"))
def cmd_prompt(repo, args):
    prompt = get_agent(args.agent).build_prompt(repo.get_episode(args.episode_id), repo.settings,
                                                repo.blocked_sites())
    if args.out:
        Path(args.out).write_text(prompt, encoding="utf-8")
        print(f"Prompt written to {args.out}. Paste it into a chat AI and save its answer.")
    else:
        print(prompt)


@command("paste", "Check and save an AI answer from a text file",
         EPISODE, opt("agent", choices=AGENT_NAMES), opt("answer_file"))
def cmd_paste(repo, args):
    agent = get_agent(args.agent)
    ep = run_agent(repo, args.episode_id, agent, parse_response(agent, read_text(args.answer_file)),
                   actor=ME, note="manual paste")
    print(f"Saved {agent.label.lower()} answer as v{ep.version}.")
    print_blockers(repo, args.episode_id)


@command("mock", "Fill the current stage with a [MOCK] answer (testing)",
         EPISODE, opt("agent", choices=AGENT_NAMES))
def cmd_mock(repo, args):
    ep = run_mock(repo, args.episode_id, get_agent(args.agent))
    print(f"Saved mock {args.agent} answer as v{ep.version}.")
    print_blockers(repo, args.episode_id)


@command("ai", "Run this stage's agent with an AI service (see ai-test)",
         EPISODE,
         opt("--save", action="store_true", help="keep the answer (like clicking Save)"),
         opt("--fresh", action="store_true",
             help="ask again even if this exact question was answered before"))
def cmd_ai(repo, args):
    """Run the current stage's agent with an AI service; save only with --save."""
    ep = repo.get_episode(args.episode_id)
    agent = agent_for_status(ep.status)
    if agent is None:
        raise StudioError(f"There is no AI step at {ep.status.value}.")
    router = llm.Router.from_config(usage=repo)
    print(f"Asking {', '.join(router.names) or 'an AI service'} for the {agent.label.lower()}...")
    result = ask_agent(repo, args.episode_id, agent, router, fresh=args.fresh,
                       fetcher=HttpFetcher())
    print(result.summary)
    for n, a in enumerate(result.attempts[:-1], start=1):
        print(f"  repair {n}: " + "; ".join(a.problems)[:300])
    if not result.usable:
        raise StudioError("The answer still can't be used:\n- " + "\n- ".join(result.problems))
    for i in result.issues:
        print(f"  [still to fix] {i.code}: {i.message}")
    if args.save:
        new = save_ai_answer(repo, args.episode_id, agent, result, actor=ME)
        print(f"Saved as v{new.version}.")
        print_blockers(repo, args.episode_id)
    else:
        print("Not saved. Look at it in the dashboard, or run again with --save to keep it.")


@command("ai-test", "Check the AI keys in .env (one tiny request each)")
def cmd_ai_test(repo, args):
    """One tiny request per AI service with a key; shows which models the key can use."""
    for r in llm.test_connections(usage=repo):
        if not r.has_key:
            print(f"-  {r.provider:<11} {r.problem}  (free key: {r.signup_url})")
            continue
        if r.ok:
            print(f"OK {r.provider:<11} answered with {r.model} in {r.seconds:g} s")
        else:
            print(f"!! {r.provider:<11} {r.problem}")
        print(f"   requests today: {r.used_today} of {r.daily_limit}")
        if r.available:
            print(f"   models this key can use ({len(r.available)}): "
                  + ", ".join(r.available[:12]) + (" ..." if len(r.available) > 12 else ""))


@command("mock-pipeline", "TESTING: fill stages with mocks up to PRODUCTION", EPISODE)
def cmd_mock_pipeline(repo, args):
    ep = run_mock_pipeline(repo, args.episode_id)
    print(f"{ep.episode_id} is now {ep.status.value} (v{ep.version}). "
          "Everything added is marked [MOCK].")


# ------------------------------------------------------------------------ facts
@command("claim", "Approve or reject a claim (only humans do this)",
         EPISODE, opt("claim_id"), opt("decision", choices=["approve", "reject", "reset"]),
         opt("--note"))
def cmd_claim(repo, args):
    ep = set_claim_status(repo, args.episode_id, args.claim_id, args.decision,
                          actor=ME, note=args.note or "")
    claim = next(c for c in ep.research.claims if c.claim_id == args.claim_id)
    print(f"{args.claim_id} is now {claim.status.value} (v{ep.version})")


@command("factcheck", "Approve the fact check (only humans do this)",
         EPISODE, opt("decision", choices=["approve", "unapprove"]), opt("--note"))
def cmd_factcheck(repo, args):
    ep = set_fact_check(repo, args.episode_id, args.decision == "approve",
                        actor=ME, note=args.note or "")
    print(f"Fact check {args.decision}d (v{ep.version})")
    print_blockers(repo, args.episode_id)


LINK_ICON = {"ok": "OK     ", "mock": "MOCK   ", "broken": "BROKEN ", "blocked": "BLOCKED",
             "error": "ERROR  ", "not_checked": "UNCHECK"}


@command("verify", "Open every cited link and find the supporting quotes", EPISODE)
def cmd_verify(repo, args):
    print("Opening each cited link (this can take a little while)...")
    ep = verify_sources(repo, args.episode_id, HttpFetcher())
    for x in ep.research.sources:
        mark = f"  checked by {x.verified_by}" if x.verified_by else ""
        print(f"  {LINK_ICON[x.link_status.value]} {x.source_id} {x.url}{mark}")
        if x.link_note:
            print(f"          note: {x.link_note}")
    for c in ep.research.claims:
        for e in c.evidence:
            print(f'  {c.claim_id} <- {e.source_id} (match {e.score:.0%}): "{e.quote}"')
    print_blockers(repo, args.episode_id)


@command("source-ok", "Vouch for a source you opened yourself (blocked sites)",
         EPISODE, opt("source_id"), opt("--note", required=True, help="what you checked"))
def cmd_source_ok(repo, args):
    mark_source_verified(repo, args.episode_id, args.source_id, actor=ME, note=args.note)
    print(f"{args.source_id} marked as checked by you.")
    print_blockers(repo, args.episode_id)


@command("decline", "Don't use a source; claims that relied only on it are rejected",
         EPISODE, opt("source_id"), opt("--reason", required=True),
         opt("--block-site", action="store_true",
             help="also never use this website again (all episodes)"))
def cmd_decline(repo, args):
    ep = repo.get_episode(args.episode_id)
    for line in declines.preview(ep, {args.source_id}).lines():
        print("  " + line)
    decline_source(repo, args.episode_id, args.source_id, reason=args.reason, actor=ME,
                   block_site=args.block_site)
    print(f"{args.source_id} declined." + (" Its site is blocked for future research too."
                                           if args.block_site else ""))
    print_blockers(repo, args.episode_id)


@command("blocked-sites", "Websites you never want as a source")
def cmd_blocked_sites(repo, args):
    sites = repo.blocked_sites()
    if not sites:
        print("No blocked sites.")
    for b in sites:
        print(f"{b['domain']:<30} {b['reason']}  ({b['blocked_by']}, {b['blocked_at'][:10]})")


@command("block-site", "Never use a website as a source",
         opt("site", help="e.g. example.com"), opt("--reason", required=True))
def cmd_block_site(repo, args):
    print(f"Blocked: {repo.block_site(args.site, reason=args.reason, actor=ME)}")


@command("unblock-site", "Allow a blocked website again", opt("site"))
def cmd_unblock_site(repo, args):
    print("Unblocked." if repo.unblock_site(args.site) else f"{args.site} was not blocked.")


# ------------------------------------------------------------------------ ideas
@command("ideas", "Prompt for an AI to suggest episode ideas",
         opt("--series", default=""), opt("--category", choices=CATEGORIES),
         opt("--theme", default=""), opt("--count", type=int, default=5),
         opt("--out", help="write the prompt to this file"))
def cmd_ideas(repo, args):
    prompt = build_ideas_prompt(repo, series=args.series, category=args.category or "",
                                theme=args.theme, count=args.count)
    if args.out:
        Path(args.out).write_text(prompt, encoding="utf-8")
        print(f"Prompt written to {args.out}. Paste it into a chat AI and save its answer, then:")
        print("  python cli.py ideas-paste answer.txt")
    else:
        print(prompt)


@command("ideas-paste", "Screen the AI's ideas for originality (and create one)",
         opt("answer_file"),
         opt("--create", type=int, help="number of the idea to turn into an episode"),
         opt("--series", default="", help="series for the created episode"))
def cmd_ideas_paste(repo, args):
    screened = screen_ideas(repo, parse_ideas(read_text(args.answer_file)))
    for n, (idea, report) in enumerate(screened, start=1):
        top = report.matches[0] if report.matches else None
        print(f"{n}. [{report.verdict.value}] {idea.topic}  ({idea.category.value}, "
              f"fact risk {idea.fact_risk}, already known {idea.familiarity})")
        if idea.story_angle:
            print(f"     angle: {idea.story_angle}")
        if top:
            print(f"     similar to {top.episode_id} \"{top.other_topic}\": {top.detail}")
    print()
    if args.create:
        idea, _ = screened[args.create - 1]
        ep, report = create_from_idea(repo, idea, series=args.series, actor=ME)
        print(f"Created {ep.episode_id} from idea {args.create}. "
              f"Originality: {report.verdict.value}")
    else:
        print("To create one:  python cli.py ideas-paste answer.txt --create 2")


# ------------------------------------------------------------------- production
@command("tools", "Free image/voice tools and their license notes")
def cmd_tools(repo, args):
    tools = load_tools()
    used = made_today(repo)
    for title, items in (("Image tools", tools.image_tools), ("Voice tools", tools.voice_tools)):
        print(f"{title}:")
        for t in items:
            flag = "  (RE-CHECK the terms)" if t.stale() else ""
            print(f"  - {t.name}{'  ' + t.url if t.url else ''}{flag}")
            if t.daily_limit:
                print(f"      today: {used.get(t.name, 0)} of {t.daily_limit} pictures used")
            print(f"      license note: {t.license_note}")
            if t.checked:
                print(f"      terms checked {t.checked}: {t.terms_url}")


@command("asset", "Attach a picture or clip to a shot (PRODUCTION)",
         EPISODE, opt("shot_id"), opt("file"),
         opt("--tool", help='e.g. "Adobe Firefly" (fills the license note; see: tools)'),
         opt("--made-with", default="", help="tool name, if not in the tools list"),
         opt("--license", help="where it came from / what the license allows"))
def cmd_asset(repo, args):
    data, name = read_bytes(args.file)
    made, lic = tool_and_license(load_tools().image_tools, args)
    ep = add_shot_asset(repo, args.episode_id, args.shot_id, data, name,
                        provider=made, license_note=lic, actor=ME)
    a = ep.assets[-1]
    print(f"{args.shot_id}: {name} stored as {a.asset_id} ({a.width}x{a.height}"
          + (f", {a.duration_sec}s" if a.duration_sec else "") + ")")
    still = [s.shot_id for s in ep.storyboard.shots if not s.asset_path]
    print("Shots still empty: " + (", ".join(still) if still else "none"))


@command("narration", "Attach the narration audio (PRODUCTION)",
         EPISODE, opt("file"),
         opt("--tool", help='e.g. "My own voice" or "Kokoro web demo" (see: tools)'),
         opt("--made-with", default="", help="tool name, if not in the tools list"),
         opt("--license"))
def cmd_narration(repo, args):
    data, name = read_bytes(args.file)
    made, lic = tool_and_license(load_tools().voice_tools, args)
    ep = add_narration(repo, args.episode_id, data, name, license_note=lic,
                       actor=ME, provider=made)
    print(f"Narration stored ({ep.assets[-1].duration_sec}s). "
          f"Allowed: {repo.settings.min_duration_sec:g}-{repo.settings.max_duration_sec:g}s.")


@command("voices", "Voices for the built-in narrator")
def cmd_voices(repo, args):
    for name, (_, label) in VOICES.items():
        print(f"  {name:<8} {label}")
    print("Use:  python cli.py narrate EP_000003 --voice heart --speed 1.0")


@command("narrate", "Generate the narration with Kokoro and attach it",
         EPISODE, opt("--voice", default="heart", choices=list(VOICES)),
         opt("--speed", type=float, default=1.0, help="0.8 (slower) to 1.2 (faster)"))
def cmd_narrate(repo, args):
    last = [-1]

    def progress(fraction, message):
        step = int(fraction * 10)
        if step != last[0]:  # print every 10%, not every megabyte
            last[0] = step
            print(f"  {message}")
    preview = generate_narration(repo, args.episode_id, voice=args.voice, speed=args.speed,
                                 tts=default_tts(repo, progress))
    ep = use_narration(repo, preview, actor=ME)
    lo, hi = repo.settings.min_duration_sec, repo.settings.max_duration_sec
    fits = ("fits" if repo.settings.length_ok(preview.seconds)
            else f"OUTSIDE {lo:g}-{hi:g}s: change --speed")
    print(f"Narration attached: {preview.seconds}s ({fits}). Voice: {VOICES[args.voice][1]}.")
    print(f"Listen: {repo.settings.data_dir / ep.assets[-1].path}")


@command("subtitles", "Time the script to the narration (word-exact)", EPISODE)
def cmd_subtitles(repo, args):
    ep = make_subtitles(repo, args.episode_id,
                        default_aligner(repo, lambda fraction, message: print(f"  {message}")),
                        actor=ME)
    cues = read_cues(repo, ep.assets[-1])
    print(f"{len(cues)} subtitles made. First ones:")
    for c in cues[:6]:
        print(f"  {c.start:6.2f}-{c.end:6.2f}  {c.text}")
    print(f"File for YouTube captions: {repo.settings.data_dir / ep.assets[-1].path}")


@command("render", "Build the final vertical video with FFmpeg", EPISODE)
def cmd_render(repo, args):
    print("Building the video with FFmpeg (this can take a minute)...")
    ep = build_video(repo, args.episode_id, actor=ME)
    v = ep.assets[-1]
    print(f"Video ready: {repo.settings.data_dir / v.path}  ({v.duration_sec}s, "
          f"{v.width}x{v.height})")
    print_blockers(repo, args.episode_id)


@command("qa", "Record your QA of the final video (QA stage)",
         EPISODE, opt("decision", choices=["approve", "reject"]),
         opt("--all-checked", action="store_true",
             help="confirms you checked every item on the QA list"),
         opt("--note"))
def cmd_qa(repo, args):
    ep = repo.get_episode(args.episode_id)
    for check in automatic_qa(ep, repo.settings):
        print(f"  automatic: {'OK' if check['ok'] else 'PROBLEM'} - {check['detail']}")
    if args.decision == "approve" and not args.all_checked:
        print("Watch the video, then confirm every item with --all-checked:")
        for label in QA_CHECKS.values():
            print(f"  [ ] {label}")
        return
    ticked = dict.fromkeys(QA_CHECKS, args.decision == "approve")
    ep = set_qa(repo, args.episode_id, ticked, actor=ME, note=args.note or "")
    print("QA approved." if ep.qa.approved else "QA not approved: " + "; ".join(ep.qa.issues))
    print_blockers(repo, args.episode_id)


# ------------------------------------------------------------------- publishing
@command("publish-info", "Title, description, hashtags, platforms",
         EPISODE, opt("--title", required=True),
         opt("--description", help="default: hook + sources + AI note"),
         opt("--hashtags", default=""),
         opt("--platform", action="append", required=True, choices=PLATFORM_NAMES),
         opt("--no-ai-label", action="store_true", help="only if NO AI visuals were used"))
def cmd_publish_info(repo, args):
    ep = repo.get_episode(args.episode_id)
    ep = set_publication(repo, args.episode_id, title=args.title,
                         description=args.description or default_description(ep, not args.no_ai_label),
                         hashtags=args.hashtags.replace(",", " ").split(),
                         platforms=args.platform, ai_disclosure=not args.no_ai_label,
                         actor=ME)
    print(f"Saved: {ep.publication.title}  ({', '.join(ep.publication.platforms)})")
    print(ep.publication.description)


@command("post", "The post text + tips for one platform",
         EPISODE, opt("--platform", required=True, choices=PLATFORM_NAMES))
def cmd_post(repo, args):
    ep = repo.get_episode(args.episode_id)
    text = ep.publication.captions.get(args.platform) or draft_post(ep, args.platform)
    if args.platform == "youtube_shorts":
        print(f"TITLE: {ep.publication.title or '(not set yet)'}")
        print()
    print(text)
    print()
    for w in check_post(args.platform, text):
        print(f"  ! {w}")
    for tip in RULES[args.platform].tips:
        print(f"  - {tip}")


@command("published", "After uploading: record the links, lock the episode",
         EPISODE, opt("--url", action="append", required=True))
def cmd_published(repo, args):
    ep = mark_published(repo, args.episode_id, args.url, actor=ME)
    print(f"{ep.episode_id} is PUBLISHED and locked. Links: {', '.join(ep.publication.urls)}")


@command("schedule", "Uploaded and scheduled: record when it goes live",
         EPISODE, opt("--platform", required=True, choices=PLATFORM_NAMES),
         opt("--at", required=True, help='local time, like "2026-10-14 18:00"'),
         opt("--url", action="append", required=True))
def cmd_schedule(repo, args):
    ep = mark_scheduled(repo, args.episode_id, {args.platform: args.at}, args.url, actor=ME)
    print(f"{ep.episode_id} is SCHEDULED: " + ", ".join(
        f"{PLATFORMS[p]} {w}" for p, w in ep.publication.schedule.items()))
    print(f"When it's live:  python cli.py live {ep.episode_id}")


@command("live", "A scheduled video is now public: mark it published", EPISODE)
def cmd_live(repo, args):
    ep = mark_live(repo, args.episode_id, actor=ME)
    print(f"{ep.episode_id} is PUBLISHED and locked.")


@command("add-link", "Add the link of another platform (after publishing)",
         EPISODE, opt("--url", action="append", required=True),
         opt("--posted", help='when it went live, "YYYY-MM-DD HH:MM" (default: now)'))
def cmd_add_link(repo, args):
    ep = add_links(repo, args.episode_id, args.url, actor=ME, posted_at=args.posted)
    print("Links: " + ", ".join(ep.publication.urls))


# -------------------------------------------------------------------- analytics
def watch_time(text: str) -> float | None:
    """--avg-view-sec as the apps show it: 0:26 or 26."""
    try:
        return analytics.parse_duration(text)
    except StudioError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


STAT_OPTIONS = {"views": int, "engaged_views": int, "likes": int, "comments": int,
                "shares": int, "saves": int, "followers_gained": int,
                "avg_view_sec": watch_time,  # "0:26" or 26
                "stayed_pct": float, "completion_pct": float}
STAT_FLAGS = {"stayed_pct": "--stayed", "completion_pct": "--completion",
              "avg_view_sec": "--avg-view-sec"}


@command("stats", "Save a published video's numbers on one platform",
         EPISODE, opt("--platform", required=True, choices=PLATFORM_NAMES),
         *(opt(STAT_FLAGS.get(name, "--" + name.replace("_", "-")), dest=name, type=kind)
           for name, kind in STAT_OPTIONS.items()),
         opt("--note", default=""))
def cmd_stats(repo, args):
    numbers = {k: getattr(args, k) for k in STAT_OPTIONS}
    snap, warnings = analytics.add_snapshot(repo, args.episode_id, args.platform, numbers,
                                            actor=ME, note=args.note)
    print(f"Saved {PLATFORMS[snap.platform]} numbers for {snap.episode_id} (day {snap.day}).")
    for w in warnings:
        print(f"  ! {w}")


@command("stats-due", "Which videos' numbers to collect today")
def cmd_stats_due(repo, args):
    due = analytics.due_checkins(repo)
    if not due:
        print("Nothing to collect today.")
    for c in due:
        print(f"{c.episode_id}  {PLATFORMS[c.platform]:<16} day-{c.checkpoint} numbers "
              f"(video is {analytics.days(c.age)} old)  {c.url}")
        print(f"    where: {analytics.WHERE[c.platform]}")


@command("stats-show", "All saved numbers of one episode", EPISODE)
def cmd_stats_show(repo, args):
    snaps = repo.list_metrics(args.episode_id)
    if not snaps:
        print("No numbers yet. Add them with: python cli.py stats EP_x --platform ... --views ...")
    for s in snaps:
        shown = ", ".join(f"{analytics.LABELS.get(s.platform, {}).get(f, f)} {getattr(s, f):g}"
                          for f in STAT_OPTIONS if getattr(s, f) is not None)
        per_1k = {k: v for k, v in analytics.rates(s).items()
                  if v is not None and k.endswith("_per_1k")}
        extra = ("  | per 1k views: " + ", ".join(f"{v:g} {k.removesuffix('_per_1k')}"
                                                  for k, v in per_1k.items())) if per_1k else ""
        print(f"{PLATFORMS.get(s.platform, s.platform):<16} day {s.day:>3}: {shown}{extra}")


# ------------------------------------------------------------------ housekeeping
@command("doctor", "Health check: tools, models, database, backups")
def cmd_doctor(repo, args):
    checks = health.run_checks(repo)
    for c in checks:
        print(f"{health.ICON[c.level]} {c.name:<16} {c.detail}")
        if c.fix and c.level != health.OK:
            print(f"   -> {c.fix}")
    print()
    print(health.summary(checks))
    if any(c.level == health.ERROR for c in checks):
        raise StudioError("The health check found a problem (see above).")


@command("backup", "Copy the database to data/backups")
def cmd_backup(repo, args):
    print(f"Backup written to: {repo.backup()}")


@command("export-schema", "Generate docs/episode.schema.json")
def cmd_export_schema(repo, args):
    out = PROJECT_ROOT / "docs" / "episode.schema.json"
    out.write_text(json.dumps(Episode.model_json_schema(), indent=2), encoding="utf-8")
    print(f"Schema written to: {out}")


@command("demo", "Create a sample episode to try things out")
def cmd_demo(repo, args):
    """Create a fully filled-in sample episode and walk it to HUMAN_REVIEW."""
    ep = repo.create_episode("Penicillin: the accidental mold that changed medicine",
                             Category.INVENTIONS, series="Invented by Accident", actor="demo",
                             story_angle="The messy lab bench Fleming almost cleaned up")
    ep = repo.save(ep.model_copy(update=sample_sections()), actor="demo",
                   note="filled in sample content")
    report = repo.check_originality(ep.episode_id, actor="demo")
    if report.verdict.value != "PASS":  # e.g. you ran the demo twice
        repo.decide_originality(report.check_id, "accept", actor="demo",
                                note="demo episode, accepted on purpose")
    for status in STAGE_ORDER[1:STAGE_ORDER.index(Status.HUMAN_REVIEW) + 1]:
        ep = repo.transition(ep.episode_id, status, actor="demo")
    print(f"Demo episode {ep.episode_id} is now at {ep.status.value}.")
    print(f"Try:  python cli.py show {ep.episode_id}   |   python cli.py history {ep.episode_id}")


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="AI Shorts Studio")
    sub = parser.add_subparsers(dest="command", required=True)
    for c in COMMANDS:
        sp = sub.add_parser(c.name, help=c.help)
        for o in c.options:
            sp.add_argument(*o.flags, **o.kwargs)
        sp.set_defaults(func=c.run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(make_repo(), args)
    except ValidationFailed as e:
        print(f"\nBlocked by rules:\n{e}", file=sys.stderr)
        return 2
    except StudioError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
