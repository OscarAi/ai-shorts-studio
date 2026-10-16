"""AI services: request shape, error handling, fallback order, keys never leaked. No network."""
import json

import pytest

from studio import llm
from studio.config import load_env

KEY = "sk-secret-123456"
OK_BODY = {"choices": [{"message": {"content": '{"ok": true}'}}]}


def cfg(*providers):
    return llm.AiConfig(providers=list(providers))


def provider(name="Gemini", models=("m1", "m2"), json_mode=True, key_env="GEMINI_API_KEY"):
    return llm.ProviderConfig(name=name, base_url=f"https://{name.lower()}.example/v1/",
                              key_env=key_env, models=list(models), json_mode=json_mode)


class FakeSend:
    """Plays back (status, body) per call and records what was sent."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        status, payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return status, json.dumps(payload).encode()


def test_the_request_is_an_openai_style_chat_with_json_mode():
    send = FakeSend((200, OK_BODY))
    client = llm.OpenAICompatible(provider(), KEY, send=send)
    reply = client.complete("Give JSON", "m1")
    assert reply.text == '{"ok": true}' and (reply.provider, reply.model) == ("Gemini", "m1")
    call = send.calls[0]
    assert call["url"] == "https://gemini.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["body"]["model"] == "m1" and call["body"]["response_format"]["type"] == "json_object"
    assert call["body"]["messages"][-1] == {"role": "user", "content": "Give JSON"}


@pytest.mark.parametrize("status, body, error", [
    (429, {"error": {"message": "Quota exceeded"}}, llm.RateLimited),
    (401, {"error": {"message": "bad"}}, llm.BadKey),
    (400, [{"error": {"message": "Please pass a valid API key"}}], llm.BadKey),  # Gemini's way
    (404, {"error": {"message": "model not found"}}, llm.ModelMissing),
    (400, {"error": {"message": "bad field"}}, llm.BadRequest),
    (503, {"error": {"message": "overloaded"}}, llm.Unavailable),
])
def test_errors_become_plain_categories(status, body, error):
    client = llm.OpenAICompatible(provider(), KEY, send=FakeSend((status, body)))
    with pytest.raises(error):
        client.complete("x", "m1")


def test_the_key_never_appears_in_an_error():
    leaky = {"error": {"message": f"key {KEY} is not valid"}}
    client = llm.OpenAICompatible(provider(), KEY, send=FakeSend((401, leaky)))
    with pytest.raises(llm.BadKey) as e:
        client.complete("x", "m1")
    assert KEY not in str(e.value) and "***" in str(e.value)


def test_router_moves_on_model_by_model_then_service_by_service():
    send = FakeSend((429, {"error": {"message": "quota"}}),   # Gemini m1: out of requests
                    (404, {"error": {"message": "nope"}}),    # Gemini m2: no such model
                    (200, OK_BODY))                           # Groq g1: answers
    env = {"GEMINI_API_KEY": KEY, "GROQ_API_KEY": "k2"}
    router = llm.Router.from_config(cfg(provider(), provider("Groq", ["g1"], key_env="GROQ_API_KEY")),
                                    env=env, send=send)
    reply = router.complete("x")
    assert (reply.provider, reply.model) == ("Groq", "g1")
    assert router.tried == ["Gemini m1: out of free requests for now",
                            "Gemini m2: model not available"]


def test_a_refused_key_skips_the_rest_of_that_service():
    send = FakeSend((401, {"error": {"message": "bad"}}), (200, OK_BODY))
    env = {"GEMINI_API_KEY": KEY, "GROQ_API_KEY": "k2"}
    router = llm.Router.from_config(cfg(provider(), provider("Groq", ["g1"], key_env="GROQ_API_KEY")),
                                    env=env, send=send)
    assert router.complete("x").provider == "Groq" and len(send.calls) == 2  # m2 not tried


def test_json_mode_refused_is_retried_without_it():
    send = FakeSend((400, {"error": {"message": "response_format not supported"}}), (200, OK_BODY))
    router = llm.Router.from_config(cfg(provider()), env={"GEMINI_API_KEY": KEY}, send=send)
    assert router.complete("x").model == "m1"
    assert "response_format" not in send.calls[1]["body"]


def test_no_keys_or_nobody_answers_gives_a_plain_message():
    router = llm.Router.from_config(cfg(provider()), env={})
    assert not router.ready
    with pytest.raises(llm.NoAIAvailable, match=r"Add a free key to the .env file"):
        router.complete("x")
    send = FakeSend((429, {}), (429, {}))
    busy = llm.Router.from_config(cfg(provider()), env={"GEMINI_API_KEY": KEY}, send=send)
    with pytest.raises(llm.NoAIAvailable, match="No AI service answered"):
        busy.complete("x")


def test_connection_test_reports_each_service():
    models = {"data": [{"id": "models/m1"}, {"id": "models/other"}]}
    send = FakeSend((200, models), (200, OK_BODY))
    reports = llm.test_connections(cfg(provider(), provider("Groq", ["g1"], key_env="GROQ_API_KEY")),
                                   env={"GEMINI_API_KEY": KEY}, send=send)
    gem, groq = reports
    assert gem.ok and gem.model == "m1" and gem.available == ["m1", "other"]
    assert not groq.has_key and "GROQ_API_KEY" in groq.problem


def test_shipped_ai_config_is_valid():
    c = llm.load_ai_config()
    assert [p.name for p in c.providers] == ["Gemini", "Groq", "OpenRouter"]
    assert all(p.signup_url and p.terms_url and p.data_note for p in c.providers)


def test_env_file_reader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nGEMINI_API_KEY="abc"\nexport GROQ_API_KEY=def\nEMPTY=\n'
                   "ALREADY=file\n", encoding="utf-8")
    monkeypatch.delenv("SHORTS_NO_DOTENV")
    monkeypatch.setenv("ALREADY", "real")
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    import os
    try:
        assert load_env(env) == ["GEMINI_API_KEY", "GROQ_API_KEY"]
        assert os.environ["GEMINI_API_KEY"] == "abc" and os.environ["ALREADY"] == "real"
    finally:  # never leave a key behind for later tests
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY"):
            os.environ.pop(k, None)


def test_cli_ai_test_without_keys(tmp_path):
    from test_cli import run
    out = run(tmp_path, "ai-test")
    assert out.returncode == 0 and "no key: add GEMINI_API_KEY" in out.stdout


# ------------------------------------------------------------ protection against costs
class CountingUsage:
    """In-memory stand-in for the repository's ai_usage table."""

    def __init__(self, used=None):
        self.used, self.rows = dict(used or {}), []

    def ai_requests_today(self, provider=None):
        return sum(self.used.values()) if provider is None else self.used.get(provider, 0)

    def record_ai_request(self, *, provider, model, kind, outcome):
        self.rows.append((provider, model, kind, outcome))
        self.used[provider] = self.used.get(provider, 0) + 1


def two_services(send, usage, **extra):
    c = llm.AiConfig(providers=[provider(), provider("Groq", ["g1"], key_env="GROQ_API_KEY")],
                     **extra)
    return llm.Router.from_config(c, env={"GEMINI_API_KEY": KEY, "GROQ_API_KEY": "k2"},
                                  send=send, usage=usage)


def test_a_service_at_its_daily_limit_is_not_even_asked():
    send = FakeSend((200, OK_BODY))
    usage = CountingUsage({"Gemini": 100})  # Gemini's default daily_limit is 100
    reply = two_services(send, usage).complete("x")
    assert reply.provider == "Groq" and len(send.calls) == 1
    assert send.calls[0]["url"].startswith("https://groq.example")


def test_the_total_daily_limit_stops_everything_before_sending():
    send = FakeSend()
    router = two_services(send, CountingUsage({"Gemini": 3, "Groq": 2}), daily_limit_total=5)
    with pytest.raises(llm.NoAIAvailable, match="daily limit of 5 AI requests is reached"):
        router.complete("x")
    assert send.calls == []


def test_every_request_is_counted_even_failed_ones():
    usage = CountingUsage()
    two_services(FakeSend((429, {}), (503, {}), (200, OK_BODY)), usage).complete("x")
    assert [(p, o) for p, _, _, o in usage.rows] == [("Gemini", "429"), ("Gemini", "503"),
                                                    ("Groq", "200")]


def test_answers_have_a_length_ceiling():
    send = FakeSend((200, OK_BODY))
    two_services(send, None, max_output_tokens=3000).complete("x")
    assert send.calls[0]["body"]["max_tokens"] == 3000


def test_a_short_per_minute_limit_is_waited_out_once():
    waits = []
    body = [{"error": {"message": "Quota exceeded. Please retry in 3.2s."}}]
    router = two_services(FakeSend((429, body), (200, OK_BODY)), None)
    router.sleep = waits.append
    assert router.complete("x").provider == "Gemini" and waits == [4.2]


def test_a_long_wait_is_not_waited_for():
    waits = []
    body = {"error": {"message": "Daily quota. Please retry in 3600s."}}
    router = two_services(FakeSend((429, body), (429, body), (200, OK_BODY)), None)
    router.sleep = waits.append
    assert router.complete("x").provider == "Groq" and waits == []


def test_retry_after_reads_both_styles():
    assert llm.retry_after(b'{"retryDelay": "17s"}') == 17
    assert llm.retry_after(b"Please try again in 7.66s.") == 7.66
    assert llm.retry_after(b"nothing here") is None


def test_only_free_openrouter_models_unless_paid_is_allowed():
    paid = llm.ProviderConfig(name="OpenRouter", base_url="https://openrouter.example/v1/",
                              key_env="OPENROUTER_API_KEY", models=["x/expensive-model"],
                              free_model_suffix=":free")
    with pytest.raises(ValueError, match="is not a free model"):
        llm.AiConfig(providers=[paid])
    assert llm.AiConfig(providers=[paid], allow_paid=True)


def test_the_repository_counts_todays_requests(repo):
    repo.record_ai_request(provider="Gemini", model="m1", kind="chat", outcome="200")
    repo.record_ai_request(provider="Groq", model="g1", kind="chat", outcome="429")
    with repo._tx() as conn:  # one from yesterday-ish: not counted
        conn.execute("INSERT INTO ai_usage(ts, provider, model, kind, outcome) "
                     "VALUES ('2001-01-01T00:00:00+00:00','Gemini','m1','chat','200')")
    assert repo.ai_requests_today() == 2 and repo.ai_requests_today("Gemini") == 1


class FailingUsage(CountingUsage):
    """Like CountingUsage, and says which models failed recently."""

    def __init__(self, recently_failed):
        super().__init__()
        self.recently_failed = set(recently_failed)

    def ai_recent_failure(self, provider, model, minutes, outcomes):
        return (provider, model) in self.recently_failed


def test_a_model_that_just_failed_is_tried_last():
    send = FakeSend((200, OK_BODY))
    usage = FailingUsage({("Gemini", "m1")})  # m1 was busy a few minutes ago
    reply = two_services(send, usage).complete("x")
    assert reply.model == "m2" and len(send.calls) == 1  # no request, no wait on m1


def test_if_everything_failed_recently_it_still_tries():
    send = FakeSend((200, OK_BODY))
    usage = FailingUsage({("Gemini", "m1"), ("Gemini", "m2"), ("Groq", "g1")})
    assert two_services(send, usage).complete("x").model == "m1"


def test_the_repository_remembers_recent_failures(repo):
    repo.record_ai_request(provider="Gemini", model="m1", kind="chat", outcome="503")
    assert repo.ai_recent_failure("Gemini", "m1", 10, ("503",))
    assert not repo.ai_recent_failure("Gemini", "m2", 10, ("503",))
    assert not repo.ai_recent_failure("Gemini", "m1", 10, ("404",))


# --------------------------------------------------- one run never runs away
BUSY = (503, {"error": {"message": "overloaded"}})


def test_a_run_stops_at_its_request_budget():
    """Every model busy: the run stops after max_requests_per_run, not after every model x mode."""
    send = FakeSend(*[BUSY] * 20)
    router = llm.Router.from_config(
        cfg(provider(models=("m1", "m2", "m3")),
            provider("Groq", models=("g1", "g2"), key_env="GROQ_API_KEY")).model_copy(
            update={"max_requests_per_run": 3}),
        env={"GEMINI_API_KEY": KEY, "GROQ_API_KEY": KEY}, send=send)
    with router.run_budget(), pytest.raises(llm.RunLimit) as e:
        router.complete("x")
    assert len(send.calls) == 3
    assert "already sent 3 requests" in str(e.value) and "Gemini m1: not reachable" in str(e.value)
    assert all(c.budget is None for c in router.clients)  # only for that run
    send.responses = [(200, OK_BODY)]  # the next run (no budget left over) works
    assert router.complete("x").text == '{"ok": true}'


def test_a_hanging_service_only_gets_the_time_left():
    now = [0.0]
    timeouts = []

    def hang(method, url, headers, body, timeout):  # nothing comes back: the whole timeout
        timeouts.append(timeout)
        now[0] += timeout
        raise llm.Unavailable("could not connect (timed out)")
    router = llm.Router.from_config(cfg(provider(models=("m1", "m2", "m3"))).model_copy(
        update={"timeout_sec": 60, "max_seconds_per_run": 100}),
        env={"GEMINI_API_KEY": KEY}, send=hang)
    with router.run_budget(clock=lambda: now[0]), pytest.raises(llm.RunLimit, match="100 s"):
        router.complete("x")
    assert timeouts == [60, 40]  # the 2nd waits only for what is left; no 3rd request
