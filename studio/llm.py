"""Talking to AI services (Gemini, Groq, OpenRouter) through their OpenAI-compatible chat API.

Standard library only. Keys come from environment variables (the .env file), are sent only in
the Authorization header, and never appear in errors, logs or the AI log.

  OpenAICompatible  one service: complete(prompt, model) and list_models()
  Router            tries the services and models in config/ai.yaml order; moves on when one
                    is out of free requests, busy, or doesn't have the model
  FakeLLM           scripted answers for tests (no network)

Protection against costs (config/ai.yaml):
  * daily_limit per service and daily_limit_total: the app stops asking when reached
    (counted from every request it sent today, including failed ones and connection tests)
  * allow_paid: false -> only free models (e.g. OpenRouter ids ending in ":free")
  * max_output_tokens: a ceiling on every answer's length
  * max_requests_per_run / max_seconds_per_run: one click ("Run with AI") never sends more
    than this many requests or waits longer than this, however many models and repairs it
    tries (Router.run_budget); a hanging model can't keep the page busy for minutes

Better answers, fewer repairs:
  * structured outputs: services that support it get the answer's JSON Schema (generated from
    the same Pydantic model that checks the answer), so they can only produce that shape;
    otherwise JSON mode, otherwise plain text (each refused mode falls back to the next)
  * a temperature per agent (low for facts, higher for the script)
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import PROJECT_ROOT, load_yaml_model
from .errors import StudioError

AI_PATH = PROJECT_ROOT / "config" / "ai.yaml"
SYSTEM = ("You help a small, fact-checked video channel. Follow the instructions exactly and "
          "answer with the requested JSON only: no explanations, no markdown.")


# ------------------------------------------------------------------ settings
class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    base_url: str = Field(pattern=r"^https://\S+/$")
    key_env: str = Field(min_length=1)
    models: list[str] = Field(min_length=1)
    json_mode: bool = False
    structured_outputs: bool = False      # accepts response_format json_schema
    daily_limit: int = Field(100, ge=1)   # requests per day the app may send to this service
    free_model_suffix: str = ""           # e.g. ":free": with allow_paid false, only these models
    signup_url: str = ""
    terms_url: str = ""
    data_note: str = ""
    checked: date | None = None


class AiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_repairs: int = Field(2, ge=0, le=5)
    timeout_sec: float = Field(90, gt=0)
    max_output_tokens: int = Field(8192, ge=256)
    daily_limit_total: int = Field(150, ge=1)
    max_wait_sec: float = Field(20, ge=0)  # wait this long for a per-minute limit to pass
    max_requests_per_run: int = Field(8, ge=1)      # one click: at most this many requests
    max_seconds_per_run: float = Field(180, gt=0)   # ... and at most this long in all
    allow_paid: bool = False
    providers: list[ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _free_only(self) -> "AiConfig":
        if self.allow_paid:
            return self
        for p in self.providers:
            paid = [m for m in p.models
                    if p.free_model_suffix and not m.endswith(p.free_model_suffix)]
            if paid:
                raise ValueError(f"{p.name}: {', '.join(paid)} is not a free model (free ones end "
                                 f"in '{p.free_model_suffix}'). allow_paid is false, so only "
                                 "free models may be listed.")
        return self


def load_ai_config(path: Path | str | None = None) -> AiConfig:
    return load_yaml_model(Path(path or AI_PATH), AiConfig)


# ------------------------------------------------------------- answer schema
# Validation details some services reject in a response schema; Pydantic still checks them
# when the answer is read, so the schema only has to describe the SHAPE.
_SCHEMA_DROP = {"title", "default", "pattern", "minLength", "maxLength", "minimum", "maximum",
                "exclusiveMinimum", "exclusiveMaximum"}


def answer_schema(model: type[BaseModel]) -> dict:
    """The JSON Schema of an answer model, simplified for structured output: $refs inlined,
    titles/defaults/validation details dropped, `X | None` written as X (optional anyway)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def clean(node, properties: bool = False):
        if isinstance(node, list):
            return [clean(x) for x in node]
        if not isinstance(node, dict):
            return node
        if properties:  # {field name: schema}: names are kept as they are
            return {name: clean(schema) for name, schema in node.items()}
        if "$ref" in node:
            return clean(defs[node["$ref"].rsplit("/", 1)[-1]])
        options = [x for x in node.get("anyOf", []) if x.get("type") != "null"]
        if "anyOf" in node and len(options) == 1:
            return clean({**{k: v for k, v in node.items() if k != "anyOf"}, **options[0]})
        return {k: clean(v, properties=k == "properties") for k, v in node.items()
                if k not in _SCHEMA_DROP}

    return clean(raw)


# -------------------------------------------------------------------- errors
class AIError(StudioError):
    """A service could not answer. `reason` is a short word for summaries."""
    reason = "error"
    retry_after: float | None = None


class RateLimited(AIError):
    reason = "out of free requests for now"


class BadKey(AIError):
    reason = "the key was refused"


class ModelMissing(AIError):
    reason = "model not available"


class BadRequest(AIError):
    reason = "request refused"


class Unavailable(AIError):
    reason = "not reachable"


class DailyLimit(AIError):
    reason = "the app's daily limit for it is reached"


class NoAIAvailable(StudioError):
    pass


class RunLimit(NoAIAvailable):
    """This run used its requests or its time: stop instead of trying more models."""


class RunBudget:
    """One run (a click on "Run with AI", with its fallbacks and repairs): at most `requests`
    requests and `seconds` seconds in all. Each request may only wait for the time left."""

    def __init__(self, requests: int, seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.requests, self.seconds, self.clock = requests, seconds, clock
        self.sent, self.deadline = 0, clock() + seconds

    def spend(self) -> float:
        """Count one request before it is sent; returns how long it may take."""
        if self.sent >= self.requests:
            raise RunLimit(f"this run already sent {self.requests} requests "
                           "(max_requests_per_run in config/ai.yaml)")
        left = self.deadline - self.clock()
        if left < 5:  # not enough time for an answer
            raise RunLimit(f"this run reached its {self.seconds:g} s limit "
                           "(max_seconds_per_run in config/ai.yaml)")
        self.sent += 1
        return left


# --------------------------------------------------------------------- usage
class Usage(Protocol):
    """Counts and records requests (the repository implements this with the ai_usage table)."""

    def ai_requests_today(self, provider: str | None = None) -> int: ...

    def ai_recent_failure(self, provider: str, model: str, minutes: float,
                          outcomes: tuple[str, ...]) -> bool: ...

    def record_ai_request(self, *, provider: str, model: str, kind: str,
                          outcome: str) -> None: ...


# ---------------------------------------------------------------------- HTTP
# send(method, url, headers, body_or_None, timeout) -> (status, body_bytes)
Send = Callable[[str, str, dict, dict | None, float], tuple[int, bytes]]


def http_send(method: str, url: str, headers: dict, body: dict | None,
              timeout: float) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise Unavailable(f"could not connect ({reason})") from None


@dataclass
class Reply:
    text: str
    provider: str
    model: str
    seconds: float


def _short(body: bytes, key: str) -> str:
    """The service's own error message, short, with the key removed just in case."""
    try:
        data = json.loads(body)
        err = data[0] if isinstance(data, list) else data
        msg = (err.get("error") or {}) if isinstance(err, dict) else {}
        text = msg.get("message", "") if isinstance(msg, dict) else str(msg)
    except (ValueError, AttributeError, IndexError, KeyError):
        text = body[:300].decode("utf-8", errors="replace")
    text = " ".join(str(text).split())[:300]
    return text.replace(key, "***") if key else text


_RETRY = re.compile(r'(?:retryDelay"\s*:\s*"|try again in |retry in )(\d+(?:\.\d+)?)\s*s',
                    re.IGNORECASE)


def retry_after(body: bytes) -> float | None:
    """'Please retry in 17.3s' / "retryDelay": "17s" -> 17.3 (per-minute limits say this)."""
    m = _RETRY.search(body.decode("utf-8", errors="replace"))
    return float(m.group(1)) if m else None


class OpenAICompatible:
    def __init__(self, cfg: ProviderConfig, key: str, *, send: Send = http_send,
                 timeout: float = 90, usage: Usage | None = None, max_tokens: int = 8192,
                 total_limit: int | None = None):
        self.cfg, self._key, self._send, self.timeout = cfg, key, send, timeout
        self.usage, self.max_tokens, self.total_limit = usage, max_tokens, total_limit
        self.budget: RunBudget | None = None  # set by Router.run_budget for one run
        self.name = cfg.name

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                "User-Agent": "shorts-studio/1.0"}

    def _guard(self) -> None:
        """Refuse BEFORE sending when today's limits are reached (protects against costs)."""
        if self.usage is None:
            return
        if self.total_limit and self.usage.ai_requests_today() >= self.total_limit:
            raise DailyLimit(f"the app's daily limit of {self.total_limit} AI requests is "
                             "reached (it resets at midnight; see daily_limit_total in "
                             "config/ai.yaml)")
        used = self.usage.ai_requests_today(self.name)
        if used >= self.cfg.daily_limit:
            raise DailyLimit(f"{self.name}: {used}/{self.cfg.daily_limit} requests used today")

    def _request(self, method: str, url: str, body: dict | None, model: str,
                 kind: str) -> tuple[int, bytes]:
        self._guard()
        timeout = self.timeout if self.budget is None else min(self.timeout, self.budget.spend())
        try:
            status, raw = self._send(method, url, self._headers(), body, timeout)
        except Unavailable as e:
            self._record(model, kind, "unreachable")
            raise Unavailable(f"{self.name}: {e}") from None
        self._record(model, kind, str(status))
        return status, raw

    def _record(self, model: str, kind: str, outcome: str) -> None:
        if self.usage is not None:
            self.usage.record_ai_request(provider=self.name, model=model, kind=kind,
                                         outcome=outcome)

    def _check(self, status: int, body: bytes, model: str = "") -> None:
        if status == 200:
            return
        where = f"{self.name}" + (f" ({model})" if model else "")
        detail = _short(body, self._key)
        wrong_key = status in (401, 403) or (status == 400 and "api key" in detail.lower())
        kind = (BadKey if wrong_key else ModelMissing if status == 404
                else RateLimited if status == 429 else BadRequest if status in (400, 422)
                else Unavailable)
        error = kind(f"{where}: HTTP {status} {detail}".strip())
        if status == 429:
            error.retry_after = retry_after(body)
        raise error

    def modes(self, schema: dict | None) -> list[str]:
        """Answer formats to try, best first: the exact JSON shape, any JSON, plain text."""
        return ([*(["schema"] if schema and self.cfg.structured_outputs else []),
                 *(["json"] if self.cfg.json_mode else [])] + ["text"])

    def complete(self, prompt: str, model: str, *, mode: str | None = None,
                 temperature: float = 0.3, schema: dict | None = None) -> Reply:
        """One request. `mode`: "schema", "json" or "text" (default: JSON if supported)."""
        mode = mode or ("json" if self.cfg.json_mode else "text")
        body = {"model": model, "temperature": temperature, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}]}
        if mode == "schema" and schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": schema, "strict": False}}
        elif mode == "json":
            body["response_format"] = {"type": "json_object"}
        start = time.monotonic()
        status, raw = self._request("POST", self.cfg.base_url + "chat/completions", body,
                                    model, "chat")
        self._check(status, raw, model)
        try:
            text = json.loads(raw)["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise Unavailable(f"{self.name} ({model}): an answer in an unexpected format") from None
        return Reply(text, self.name, model, round(time.monotonic() - start, 1))

    def list_models(self) -> list[str]:
        status, raw = self._request("GET", self.cfg.base_url + "models", None, "", "models")
        self._check(status, raw)
        try:
            data = json.loads(raw).get("data", [])
        except ValueError:
            return []
        return sorted(str(m.get("id", "")).removeprefix("models/") for m in data if m.get("id"))


def make_clients(cfg: AiConfig, env: Mapping[str, str], send: Send, usage: Usage | None,
                 timeout: float | None = None) -> list[OpenAICompatible]:
    return [OpenAICompatible(p, env[p.key_env].strip(), send=send,
                             timeout=timeout or cfg.timeout_sec, usage=usage,
                             max_tokens=cfg.max_output_tokens, total_limit=cfg.daily_limit_total)
            for p in cfg.providers if env.get(p.key_env, "").strip()]


# -------------------------------------------------------------------- router
# A model that just failed is skipped for a while (it is tried last instead), so a busy or
# hanging model doesn't cost a request and a minute on every click.
COOLDOWNS = (  # (minutes, outcomes recorded in ai_usage)
    (10, ("503", "500", "502", "504", "unreachable", "429")),
    (24 * 60, ("404",)),
)


def cooling_down(client: "OpenAICompatible", model: str) -> bool:
    usage = client.usage
    if usage is None or not hasattr(usage, "ai_recent_failure"):
        return False
    return any(usage.ai_recent_failure(client.name, model, minutes, outcomes)
               for minutes, outcomes in COOLDOWNS)


@dataclass
class Router:
    """Tries every configured service and model in order until one answers. Models that
    failed recently are tried last."""
    clients: list[OpenAICompatible]
    max_wait_sec: float = 20
    sleep: Callable[[float], None] = time.sleep
    tried: list[str] = field(default_factory=list)   # what happened on the last call
    max_requests_per_run: int = 8
    max_seconds_per_run: float = 180

    @classmethod
    def from_config(cls, cfg: AiConfig | None = None, *, env: Mapping[str, str] | None = None,
                    send: Send = http_send, usage: Usage | None = None) -> "Router":
        """`usage` (the repository) counts requests and enforces the daily limits."""
        cfg = cfg or load_ai_config()
        environ: Mapping[str, str] = os.environ if env is None else env
        return cls(make_clients(cfg, environ, send, usage), max_wait_sec=cfg.max_wait_sec,
                   max_requests_per_run=cfg.max_requests_per_run,
                   max_seconds_per_run=cfg.max_seconds_per_run)

    @contextmanager
    def run_budget(self, clock: Callable[[], float] = time.monotonic) -> Iterator[RunBudget]:
        """Every request inside the `with` block shares one RunBudget."""
        budget = RunBudget(self.max_requests_per_run, self.max_seconds_per_run, clock)
        for c in self.clients:
            c.budget = budget
        try:
            yield budget
        finally:
            for c in self.clients:
                c.budget = None

    @property
    def ready(self) -> bool:
        return bool(self.clients)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.clients]

    def complete(self, prompt: str, *, temperature: float = 0.3,
                 schema: dict | None = None) -> Reply:
        if not self.clients:
            raise NoAIAvailable("No AI service is set up yet. Add a free key to the .env file "
                                "(see the README, 'AI inside the app'), or use copy-paste.")
        self.tried = []
        fresh = [(c, m) for c in self.clients for m in c.cfg.models if not cooling_down(c, m)]
        resting = [(c, m) for c in self.clients for m in c.cfg.models if cooling_down(c, m)]
        skip: set[str] = set()  # services to skip (limit reached or key refused)
        for client, model in fresh + resting:
            if client.name in skip:
                continue
            try:
                return self._one(client, model, prompt, self.max_wait_sec, self.sleep,
                                 temperature=temperature, schema=schema)
            except RunLimit as e:
                raise RunLimit(f"Stopped asking: {e}. Tried: " + "; ".join(self.tried)
                               + ". Try again later or use copy-paste.") from None
            except DailyLimit as e:
                self.tried.append(str(e))
                skip.add(client.name)  # counts per service: skip its other models too
            except BadKey as e:
                self.tried.append(f"{client.name}: {e.reason}")
                skip.add(client.name)  # same key for every model: skip this service
            except AIError as e:
                self.tried.append(f"{client.name} {model}: {e.reason}")
        raise NoAIAvailable("No AI service answered (" + "; ".join(self.tried) + "). Try again "
                            "later, add another free key, or use copy-paste.")

    @staticmethod
    def _one(client: OpenAICompatible, model: str, prompt: str, max_wait: float = 0,
             sleep: Callable[[float], None] = time.sleep, *, temperature: float = 0.3,
             schema: dict | None = None) -> Reply:
        """One model: the best answer format it accepts (a refused format falls back to the
        next), and one wait when a per-minute limit says "retry in 17s"."""
        modes = client.modes(schema)
        for n, mode in enumerate(modes):
            def ask(mode=mode):
                return client.complete(prompt, model, mode=mode, temperature=temperature,
                                       schema=schema)
            try:
                try:
                    return ask()
                except RateLimited as e:  # waiting once beats a weaker model
                    if e.retry_after is None or e.retry_after > max_wait:
                        raise
                    sleep(e.retry_after + 1)
                    return ask()
            except BadRequest:
                if n == len(modes) - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------- fake
class FakeLLM:
    """Answers from a list, in order (for tests). Records every prompt it was sent."""

    def __init__(self, answers: list[str], name: str = "Fake"):
        self.answers, self.name = list(answers), name
        self.prompts: list[str] = []
        self.ready, self.names = True, [name]
        self.options: list[dict] = []  # temperature / schema of each request

    def complete(self, prompt: str, **options) -> Reply:
        self.prompts.append(prompt)
        self.options.append(options)
        if not self.answers:
            raise NoAIAvailable("FakeLLM has no answers left.")
        return Reply(self.answers.pop(0), self.name, "fake-model", 0.0)


# ------------------------------------------------------------- connection test
@dataclass
class ConnectionReport:
    provider: str
    has_key: bool
    ok: bool = False
    model: str = ""          # the configured model that answered
    seconds: float = 0.0
    available: list[str] = field(default_factory=list)  # models this key can use
    problem: str = ""
    signup_url: str = ""
    used_today: int = 0
    daily_limit: int = 0


def test_connections(cfg: AiConfig | None = None, *, env: Mapping[str, str] | None = None,
                     send: Send = http_send, usage: Usage | None = None
                     ) -> list[ConnectionReport]:
    """For every service: is there a key, which models can it use, and does a tiny request
    get an answer? (Costs two small requests per service; counted like any other.)"""
    cfg = cfg or load_ai_config()
    environ: Mapping[str, str] = os.environ if env is None else env
    clients = {c.name: c for c in make_clients(cfg, environ, send, usage,
                                                timeout=min(cfg.timeout_sec, 30))}
    out = []
    for p in cfg.providers:
        client = clients.get(p.name)
        rep = ConnectionReport(p.name, client is not None, signup_url=p.signup_url,
                               daily_limit=p.daily_limit)
        if client is None:
            rep.problem = f"no key: add {p.key_env}=... to .env"
            out.append(rep)
            continue
        try:
            rep.available = client.list_models()
        except (BadKey, DailyLimit) as e:
            rep.problem = f"{e.reason} ({e})"
            out.append(rep)
            continue
        except AIError:
            pass  # some services don't list models; the request below still tells us
        for model in p.models:
            try:
                reply = Router._one(client, model, 'Answer with this JSON and nothing else: '
                                                   '{"ok": true}')
                rep.ok, rep.model, rep.seconds, rep.problem = True, model, reply.seconds, ""
                break
            except AIError as e:
                rep.problem = f"{model}: {e}"
        if usage is not None:
            rep.used_today = usage.ai_requests_today(p.name)
        out.append(rep)
    return out
