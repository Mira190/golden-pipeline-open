import datetime as dt

import pytest

from golden import cli, config, osvignores

TODAY = dt.date(2026, 10, 22)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def entry(**kw):
    fields = {"id": '"GHSA-1"', "reason": '"no fix yet, PLAT-9"', "ignoreUntil": "2026-12-01"}
    fields.update(kw)
    body = "".join(f"{k} = {v}\n" for k, v in fields.items() if v is not None)
    return "[[IgnoredVulns]]\n" + body


def test_no_config_means_no_exceptions(tmp_path):
    assert osvignores.check(tmp_path, TODAY) == []


def test_valid_exceptions_are_listed(tmp_path):
    write(tmp_path, "osv-scanner.toml", entry())
    write(tmp_path, "svc/osv-scanner.toml", entry(id='"OSV-2"', ignoreUntil="2026-11-01T00:00:00Z"))
    assert osvignores.check(tmp_path, TODAY) == [
        osvignores.Ignore("osv-scanner.toml", "GHSA-1", "no fix yet, PLAT-9", dt.date(2026, 12, 1)),
        osvignores.Ignore(
            "svc/osv-scanner.toml", "OSV-2", "no fix yet, PLAT-9", dt.date(2026, 11, 1)
        ),
    ]


def test_vendored_and_dependency_directories_are_skipped(tmp_path):
    write(tmp_path, "node_modules/pkg/osv-scanner.toml", "[[IgnoredVulns]]\n")
    write(tmp_path, ".venv/x/osv-scanner.toml", "garbage = [")
    assert osvignores.check(tmp_path, TODAY) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (entry(ignoreUntil=None), r"ignoreUntil is required"),
        (entry(ignoreUntil='"2026-12-01"'), r"ignoreUntil is required"),
        (entry(reason='"  "'), r"\(GHSA-1\): reason is required"),
        (entry(reason=None), r"reason is required"),
        (entry(id=None), r"IgnoredVulns\[0\]: id is required"),
        (entry(ignoreUntil="2027-01-21"), r"at most 90 days ahead"),
        ("IgnoredVulns = 3\n", r"must be an array of tables"),
        ("[[IgnoredVulns\n", r"osv-scanner\.toml"),
    ],
)
def test_bad_exceptions_are_rejected(tmp_path, text, message):
    write(tmp_path, "osv-scanner.toml", text)
    with pytest.raises(config.PolicyError, match=message):
        osvignores.check(tmp_path, TODAY)


def test_the_90_day_boundary(tmp_path):
    write(tmp_path, "osv-scanner.toml", entry(ignoreUntil=str(TODAY + dt.timedelta(days=90))))
    assert len(osvignores.check(tmp_path, TODAY)) == 1
    write(tmp_path, "osv-scanner.toml", entry(ignoreUntil=str(TODAY + dt.timedelta(days=91))))
    with pytest.raises(config.PolicyError, match="at most 90 days"):
        osvignores.check(tmp_path, TODAY)


def test_policy_check_lists_exceptions(repo, capsys):
    soon = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=30)
    write(repo.root, "osv-scanner.toml", entry(ignoreUntil=str(soon)))
    assert cli.main(["policy", "check"]) == 0
    out = capsys.readouterr().out
    assert "**Vulnerability exceptions**" in out
    assert f"- **GHSA-1** until {soon} (osv-scanner.toml): no fix yet, PLAT-9" in out
    assert "expired" not in out


def test_policy_check_marks_expired_exceptions(repo, capsys):
    write(repo.root, "osv-scanner.toml", entry(ignoreUntil="2020-01-01"))
    assert cli.main(["policy", "check"]) == 0
    assert "_(expired: enforced again)_" in capsys.readouterr().out


def test_policy_check_fails_on_a_permanent_exception(repo, capsys):
    write(repo.root, "osv-scanner.toml", entry(ignoreUntil=None))
    assert cli.main(["policy", "check"]) == 2
    assert "ignoreUntil is required" in capsys.readouterr().out


# ------------------------------------------------------------- found by tests/test_fuzz.py
@pytest.mark.parametrize(
    "content",
    [
        pytest.param("a = " + "[" * 5000 + "]" * 5000, id="deeply-nested"),
        pytest.param("[[IgnoredVulns]]\nid = " + "9" * 5000 + "\n", id="a-huge-integer"),
    ],
)
def test_a_file_the_toml_reader_gives_up_on_is_a_policy_error(tmp_path, content):
    write(tmp_path, "osv-scanner.toml", content)
    with pytest.raises(config.PolicyError, match=r"^osv-scanner\.toml: ") as raised:
        osvignores.check(tmp_path, TODAY)
    assert type(raised.value) is config.PolicyError


@pytest.mark.parametrize("content", [b"\xff\xfe", b"[[IgnoredVulns]]\n\x80", b"\xef\xbb\xbf\xff"])
def test_a_file_that_is_not_utf8_is_a_policy_error(tmp_path, content):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "osv-scanner.toml").write_bytes(content)
    with pytest.raises(config.PolicyError, match=r"^svc/osv-scanner\.toml: ") as raised:
        osvignores.check(tmp_path, TODAY)
    assert type(raised.value) is config.PolicyError


@pytest.mark.parametrize(
    "value", ["5", "true", '["GHSA-1"]', "{a = 1}", '""', '"  "', "2026-12-01"]
)
def test_an_advisory_id_that_is_not_text_is_not_an_id(tmp_path, value):
    # str() of a list or a number used to pass for an id.
    write(tmp_path, "osv-scanner.toml", entry(id=value))
    with pytest.raises(config.PolicyError, match=r"IgnoredVulns\[0\]: id is required"):
        osvignores.check(tmp_path, TODAY)


@pytest.mark.parametrize(
    "value", ["5", "true", '["no fix"]', "{a = 1}", '""', '"  "', "2026-12-01"]
)
def test_a_reason_that_is_not_text_is_not_a_reason(tmp_path, value):
    write(tmp_path, "osv-scanner.toml", entry(reason=value))
    with pytest.raises(config.PolicyError, match=r"\(GHSA-1\): reason is required"):
        osvignores.check(tmp_path, TODAY)


def test_the_id_and_the_reason_are_kept_as_written_apart_from_padding(tmp_path):
    write(tmp_path, "osv-scanner.toml", entry(id='"  GHSA-1"', reason='"  no fix \\n"'))
    (found,) = osvignores.check(tmp_path, TODAY)
    assert (found.id, found.reason) == ("  GHSA-1", "no fix")
