import json
import urllib.error

import pytest

from golden import cli, optout

LABEL = "no-tests-needed"


def payload(labels=(LABEL,), author="alice", **extra):
    pr = {"number": 7, "user": {"login": author}, "labels": [{"name": n} for n in labels]}
    return {"pull_request": pr, **extra}


def events(*appliers, label=LABEL):
    return [{"event": "labeled", "label": {"name": label}, "actor": {"login": a}} for a in appliers]


def fetch_from(pages):
    calls = []

    def fetch(path):
        calls.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return pages[page - 1] if page <= len(pages) else []

    fetch.calls = calls
    return fetch


def decide(p, fetch, event="pull_request", label=LABEL):
    return optout.decide(p, event, label, "o/r", fetch)


def test_no_label_or_no_input_means_not_requested():
    never = fetch_from([])
    assert decide(payload(labels=()), never) == optout.NOT_REQUESTED
    assert decide(payload(), never, label="") == optout.NOT_REQUESTED
    assert never.calls == []


def test_label_applied_by_a_reviewer_counts():
    d = decide(payload(), fetch_from([events("bob")]))
    assert d == optout.Decision(
        report_only=True, note=f"label `{LABEL}` applied by @bob (PR by @alice)"
    )


def test_label_applied_by_the_author_is_ignored():
    d = decide(payload(), fetch_from([events("alice")]))
    assert not d.report_only
    assert "the PR author (@alice) applied it" in d.note


def test_most_recent_application_wins():
    # A reviewer applied it, the author removed and re-applied it: the author's counts.
    other = [{"event": "labeled", "label": {"name": "bug"}, "actor": {"login": "bob"}}]
    assert not decide(payload(), fetch_from([events("bob") + other + events("alice")])).report_only
    assert decide(payload(), fetch_from([events("alice") + events("bob")])).report_only


def test_labeled_event_names_the_applier_without_the_api():
    never = fetch_from([])
    p = payload(label={"name": LABEL}, sender={"login": "bob"})
    assert decide(p, never).report_only
    assert never.calls == []
    p = payload(label={"name": LABEL}, sender={"login": "alice"})
    assert not decide(p, never).report_only


def test_another_labels_event_still_checks_the_api():
    p = payload(labels=(LABEL, "bug"), label={"name": "bug"}, sender={"login": "bob"})
    assert not decide(p, fetch_from([events("alice")])).report_only


def test_merge_group_payload_never_reads_sender():
    p = payload(label={"name": LABEL}, sender={"login": "bob"})
    assert not decide(p, fetch_from([events("alice")]), event="merge_group").report_only


@pytest.mark.parametrize(
    "fetch",
    [
        lambda path: None,  # API unreachable or refused
        lambda path: {"message": "Resource not accessible by integration"},
        lambda path: [],  # no labeled event found
        lambda path: [{"event": "labeled", "label": {"name": LABEL}, "actor": None}],
        lambda path: ["junk"],
    ],
)
def test_unverifiable_label_fails_closed(fetch):
    d = decide(payload(), fetch)
    assert not d.report_only
    assert "could not verify who applied it" in d.note


def test_missing_author_fails_closed():
    p = payload()
    del p["pull_request"]["user"]
    assert not decide(p, fetch_from([events("bob")])).report_only


def test_applier_pages_through_events():
    filler = [{"event": "commented"}] * 100
    fetch = fetch_from([filler, events("bob")])
    assert optout.applier("o/r", 7, LABEL, fetch) == "bob"
    assert fetch.calls == [
        "/repos/o/r/issues/7/events?per_page=100&page=1",
        "/repos/o/r/issues/7/events?per_page=100&page=2",
    ]


def test_applier_gives_up_after_max_pages():
    fetch = fetch_from([events("bob") + [{"event": "x"}] * 99] * optout.MAX_PAGES)
    assert optout.applier("o/r", 7, LABEL, fetch) is None
    assert len(fetch.calls) == optout.MAX_PAGES


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_api_get(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["auth"], seen["timeout"] = (
            request.full_url,
            request.get_header("Authorization"),
            timeout,
        )
        return FakeResponse(b'[{"event": "labeled"}]')

    monkeypatch.setattr(optout.urllib.request, "urlopen", fake_urlopen)
    assert optout.api_get("/repos/o/r", "tok") == [{"event": "labeled"}]
    assert seen == {"url": "https://api.github.com/repos/o/r", "auth": "Bearer tok", "timeout": 10}
    optout.api_get("/x", None)
    assert seen["auth"] is None


@pytest.mark.parametrize(
    "failure", [urllib.error.URLError("down"), TimeoutError(), ValueError("bad json")]
)
def test_api_get_failures_return_none(monkeypatch, failure):
    def boom(request, timeout):
        raise failure

    monkeypatch.setattr(optout.urllib.request, "urlopen", boom)
    assert optout.api_get("/x", "tok") is None


def plan_env(repo, monkeypatch, tmp_path, event):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GOLDEN_INPUTS", json.dumps({"skip-presence-label": LABEL}))


def test_cli_plan_honours_a_reviewer_label(repo, monkeypatch, tmp_path, capsys):
    base = repo.commit({"README.md": "x"})
    event = payload(label={"name": LABEL}, sender={"login": "bob"})
    event["pull_request"]["base"] = {"sha": base}
    plan_env(repo, monkeypatch, tmp_path, event)
    assert cli.main(["plan", "--language", "python"]) == 0
    out = capsys.readouterr().out
    assert f"::notice::label `{LABEL}` applied by @bob" in out
    assert json.loads(out[out.index("{") :])["presence-report-only"] == "true"


def test_cli_plan_without_repository_never_calls_the_api(repo, monkeypatch, tmp_path, capsys):
    base = repo.commit({"README.md": "x"})
    event = payload()
    event["pull_request"]["base"] = {"sha": base}
    plan_env(repo, monkeypatch, tmp_path, event)

    def forbidden(*args, **kwargs):
        raise AssertionError("no API call without GITHUB_REPOSITORY")

    monkeypatch.setattr(optout.urllib.request, "urlopen", forbidden)
    assert cli.main(["plan", "--language", "python"]) == 0
    out = capsys.readouterr().out
    assert "::warning::label `no-tests-needed` ignored: could not verify" in out
    assert json.loads(out[out.index("{") :])["presence-report-only"] == "false"


def test_cli_plan_reads_the_api_with_the_token(repo, monkeypatch, tmp_path, capsys):
    base = repo.commit({"README.md": "x"})
    event = payload()
    event["pull_request"]["base"] = {"sha": base}
    plan_env(repo, monkeypatch, tmp_path, event)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen = []
    monkeypatch.setattr(optout, "api_get", lambda path, token: seen.append(token) or events("bob"))
    assert cli.main(["plan", "--language", "python"]) == 0
    assert seen == ["tok"]
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{") :])["presence-report-only"] == "true"
