import json
from urllib.parse import parse_qs

import pytest

from ota_agent.errors import AuditError, ConflictError, GuardError, OTAError, UncertainWriteError

from .conftest import records, row


def test_add_then_enable_preserves_existing_and_no_push(ota, fake):
    result = ota.change_sns(24306, ["801625490773333"])
    assert result["verified"]
    assert "00772633000001-00772633000005" in fake.rules[24306]
    assert "801625490773333" in fake.rules[24306]
    assert ota.set_enabled(24306, True)["verified"]
    request = next(r for r in fake.requests if r.url.path.endswith("changeOsStatus.action"))
    assert parse_qs(request.content.decode())["jPushFlag"] == ["0"]
    assert fake.tasks[24108]["effective"] == 0


def test_duplicate_sn_does_not_write(ota, fake):
    fake.rules[24306] = ["801625490773333"]
    assert not ota.change_sns(24306, ["801625490773333"])["changed"]
    assert not fake.writes


def test_sn_conflict_blocks_write(ota, fake):
    fake.change_rules_at_read = 2
    with pytest.raises(ConflictError):
        ota.change_sns(24306, ["801625490773333"])
    assert not fake.writes


def test_last_sn_cannot_be_cleared(ota, fake):
    fake.rules[24306] = ["801625490773333"]
    with pytest.raises(GuardError):
        ota.change_sns(24306, ["801625490773333"], remove=True)
    assert not fake.writes


def test_empty_rules_cannot_enable(ota, fake):
    fake.rules[24306] = []
    with pytest.raises(GuardError):
        ota.set_enabled(24306, True)
    assert not fake.writes


def test_sn_timeout_is_read_back_not_replayed(ota, fake):
    fake.save_timeout = True
    result = ota.change_sns(24306, ["801625490773333"])
    assert result["recovered_by_readback"]
    assert fake.writes == ["sn"]


def test_failed_sn_save_is_not_reported_as_success(ota, fake):
    fake.save_noop = True
    with pytest.raises(UncertainWriteError):
        ota.change_sns(24306, ["801625490773333"])
    assert fake.writes == ["sn"]


def test_expired_write_session_is_not_replayed(ota, fake):
    fake.expire_write = True
    with pytest.raises(UncertainWriteError):
        ota.change_sns(24306, ["801625490773333"])
    assert fake.writes == ["sn"]
    assert fake.login_count == 2


def test_read_session_can_relogin(ota, fake):
    fake.expire_read = True
    assert ota.list_otas()["total"] == 2
    assert fake.login_count == 2


@pytest.mark.parametrize("remarks", ["OTHER TEAM", "Patrick", "patrick", "", None, 123])
def test_delete_requires_exact_case_marker(ota, fake, remarks):
    fake.tasks[24306]["remarks"] = remarks
    with pytest.raises(GuardError):
        ota.delete_otas([24306])
    assert "delete" not in fake.writes
    assert 24306 in fake.tasks


def test_batch_deletion_is_all_or_nothing_before_submission(ota, fake):
    with pytest.raises(GuardError):
        ota.delete_otas([24306, 24108])
    assert not fake.writes
    assert len(fake.tasks) == 2


def test_delete_rechecks_current_remarks(ota, fake):
    fake.change_remarks_at_query = 2
    with pytest.raises(GuardError):
        ota.delete_otas([24306])
    assert "delete" not in fake.writes


def test_eligible_delete_and_readback(ota, fake):
    assert ota.delete_otas([24306])["verified"]
    assert fake.writes == ["delete"]
    assert 24108 in fake.tasks


def test_delete_timeout_does_not_replay(ota, fake):
    fake.delete_timeout = True
    assert ota.delete_otas([24306])["verified"]
    assert fake.writes == ["delete"]


def test_dry_run_never_writes(ota, fake):
    assert ota.delete_otas([24306], dry_run=True)["dry_run"]
    assert ota.change_sns(24306, ["801625490773333"], dry_run=True)["dry_run"]
    assert ota.set_enabled(24306, True, dry_run=True)["dry_run"]
    assert fake.writes == []


def test_audit_failure_prevents_requests(ota, fake, monkeypatch):
    original = ota.audit.event

    def broken(action, status, **kwargs):
        if action == "ota.delete":
            raise AuditError("log failed")
        return original(action, status, **kwargs)

    monkeypatch.setattr(ota.audit, "event", broken)
    with pytest.raises(AuditError):
        ota.delete_otas([24306])
    assert fake.requests == []


def test_attempt_success_and_failure_are_logged_without_secrets(ota, settings):
    ota.list_otas()
    with pytest.raises(GuardError):
        ota.delete_otas([24108])
    logged = records(settings.log_dir)
    assert any(r["action"] == "ota.list" and r["status"] == "succeeded" for r in logged)
    assert any(r["action"] == "ota.delete" and r["status"] == "failed" for r in logged)
    raw = json.dumps(logged)
    assert all(s not in raw for s in settings.secrets)
    assert "hidden-session" not in raw
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in settings.log_dir.glob("*.jsonl"))


def test_empty_inconsistent_page_is_not_treated_as_absence(ota, fake):
    fake.empty_bad_page = True
    with pytest.raises(OTAError):
        ota.get_ota(24306)


def test_firmware_checks_task_id_and_never_downloads(ota, fake):
    result = ota.firmware(24306, current_version="13.25.0501.01")
    assert result["matched"]
    assert result["parameters"]["devType"] == "SQ68_DS_PKG-XX"
    assert result["parameters"]["osLanguage"] == "english"
    assert result["parameters"]["osVersion"] == "13.25.0501.01"
    assert all(r.url.host == "nota.urovo.com" for r in fake.requests)
    fake.firmware_id = 24108
    assert not ota.firmware(24306, current_version="13.25.0501.01")["matched"]


def test_upload_success_disables_new_task_if_needed(ota, fake, metadata, package):
    fake.create_enabled = True
    result = ota.create_ota(metadata, package)
    assert result["task"]["id"] == 25000
    assert result["task"]["effective"] == 0
    assert fake.writes == ["create", "status"]


def test_upload_ambiguity_does_not_touch_other_tasks(ota, fake, metadata, package):
    fake.extra_created = True
    fake.create_enabled = True
    with pytest.raises(UncertainWriteError):
        ota.create_ota(metadata, package)
    assert fake.writes == ["create"]


def test_upload_failure_is_not_success(ota, fake, metadata, package):
    fake.upload_failure = True
    with pytest.raises(UncertainWriteError):
        ota.create_ota(metadata, package)
    assert fake.writes == ["create"]


def test_duplicate_upload_is_blocked(ota, fake, metadata, package):
    fake.tasks[25000] = row(25000, "someone else's task", osVersion=metadata.version)
    with pytest.raises(ConflictError):
        ota.create_ota(metadata, package)
    assert not fake.writes


def test_enable_timeout_uses_readback_but_push_remains_unknown(ota, fake):
    fake.enable_timeout = True
    assert ota.set_enabled(24306, True)["verified"]
    fake.tasks[24306]["effective"] = 0
    with pytest.raises(UncertainWriteError):
        ota.set_enabled(24306, True, push=True)


def test_foreign_login_action_is_blocked_without_credentials(ota, fake):
    fake.login_action = "https://other.example/UpdateConsole/Login.action"
    with pytest.raises(GuardError):
        ota.list_otas()
    assert len(fake.requests) == 1


def test_prepared_audit_failure_stops_delete_before_submission(ota, fake, monkeypatch):
    original = ota.audit.event

    def unavailable(action, status, **kwargs):
        if action == "ota.delete" and status == "prepared":
            raise AuditError("disk full")
        return original(action, status, **kwargs)

    monkeypatch.setattr(ota.audit, "event", unavailable)
    with pytest.raises(AuditError):
        ota.delete_otas([24306])
    assert not fake.writes
    assert 24306 in fake.tasks


def test_project_write_lock_preserves_two_concurrent_sn_changes(settings, fake):
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    from ota_agent import OTAClient

    def add(value):
        with OTAClient(settings, transport=httpx.MockTransport(fake)) as client:
            return client.change_sns(24306, [value])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ["801625490773333", "801625490773334"]))
    assert all(r["verified"] for r in results)
    assert {"801625490773333", "801625490773334"}.issubset(fake.rules[24306])
    assert "00772633000001-00772633000005" in fake.rules[24306]
