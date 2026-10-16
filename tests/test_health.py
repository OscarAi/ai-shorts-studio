import os
from datetime import datetime, timedelta

from test_cli import run

from studio import health


def levels(checks):
    return {c.name: c.level for c in checks}


def test_a_fresh_database_is_healthy_but_has_no_backup(repo):
    repo.create_episode("Velcro")
    got = levels(health.run_checks(repo))
    assert got["Database"] == health.OK
    assert got["Backups"] == health.WARN


def test_backup_age_is_checked(repo):
    target = repo.backup()
    assert levels(health.run_checks(repo))["Backups"] == health.OK
    old = (datetime.now() - timedelta(days=10)).timestamp()
    os.utime(target, (old, old))
    [backups] = [c for c in health.run_checks(repo) if c.name == "Backups"]
    assert backups.level == health.WARN and "10 days old" in backups.detail


def test_tool_terms_go_stale(repo):
    got = health.run_checks(repo, now=datetime(2027, 9, 1))
    [licences] = [c for c in got if c.name == "Tool licences"]
    assert licences.level == health.WARN and "Adobe Firefly" in licences.detail


def test_library_versions_are_compared_with_the_lock(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text("# comment\nPyYAML==0.0.1\npydantic_core==0.0.2  # pinned\n", encoding="utf-8")
    assert health.locked_versions(lock) == {"pyyaml": "0.0.1", "pydantic-core": "0.0.2"}
    check = health._libraries(lock)
    assert check.level == health.WARN and "pyyaml" in check.detail
    assert health._libraries(tmp_path / "missing.txt").level == health.WARN


def test_summary_says_the_worst():
    ok = health.Check("a", health.OK, "")
    assert health.summary([ok]).startswith("✅")
    assert health.summary([ok, health.Check("b", health.WARN, "")]).startswith("⚠️ 1")
    assert health.summary([health.Check("c", health.ERROR, "")]).startswith("❌")


def test_cli_doctor(tmp_path):
    out = run(tmp_path, "doctor")
    assert "Database" in out.stdout and "Backups" in out.stdout
