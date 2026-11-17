import json

import pytest
from fastapi.testclient import TestClient

from ota_agent.api import create_app

from .conftest import records


@pytest.fixture
def api(settings, transport):
    with TestClient(create_app(settings, transport=transport)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        yield client


def test_missing_token_blocks_before_upstream(api, fake, settings):
    api.headers.pop("Authorization")
    response = api.get("/v1/otas")
    assert response.status_code == 401
    assert not fake.requests
    assert any(r["action"] == "api.request" and r["status"] == "failed" for r in records(settings.log_dir))


def test_http_workflow_and_per_request_logs(api, fake, settings):
    response = api.get("/v1/otas", params={"dev_type": "SQ68", "remarks": "PATRICK"})
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.headers["X-Request-ID"]
    assert api.post("/v1/otas/24306/sns/add", json={"entries": ["801625490773333"]}).status_code == 200
    assert api.put("/v1/otas/24306/status", json={"enabled": True}).json()["verified"]
    assert api.get("/v1/otas/24306/sns").status_code == 200
    assert api.post("/v1/otas/24306/sns/remove", json={"entries": ["00772633000003"]}).status_code == 200
    assert api.put("/v1/otas/24306/status", json={"enabled": False}).json()["verified"]
    assert api.get("/v1/otas/24306").json()["effective"] == 0
    logged = records(settings.log_dir)
    assert any(r["actor"].startswith("api:") for r in logged)
    assert all(s not in json.dumps(logged) for s in settings.secrets)


def test_http_delete_guard_and_case_sensitive_check(api, fake):
    assert api.delete("/v1/otas/24108").status_code == 403
    fake.tasks[24306]["remarks"] = "Patrick"
    assert api.delete("/v1/otas/24306").status_code == 403
    assert not fake.writes


def test_http_batch_and_force_bypass_are_blocked(api, fake):
    assert api.post("/v1/otas/batch-delete", json={"task_ids": [24306, 24108]}).status_code == 403
    assert api.post("/v1/otas/batch-delete", json={"task_ids": [24108], "force": True}).status_code == 422
    assert not fake.writes


def test_http_delete_success(api, fake):
    assert api.delete("/v1/otas/24306").json()["verified"]
    assert 24108 in fake.tasks


def test_http_dry_run(api, fake):
    assert (
        api.post("/v1/otas/24306/sns/add", json={"entries": ["801625490773333"], "dry_run": True}).status_code
        == 200
    )
    assert api.put("/v1/otas/24306/status", json={"enabled": True, "dry_run": True}).status_code == 200
    assert api.delete("/v1/otas/24306?dry_run=true").status_code == 200
    assert not fake.writes


def test_invalid_input_is_422_and_logged_without_upstream(api, fake, settings):
    response = api.post("/v1/otas/24306/sns/add", json={"entries": [801625490773333]})
    assert response.status_code == 422
    assert not fake.requests
    assert api.put("/v1/otas/24306/status", json={"enabled": "false"}).status_code == 422
    assert any(r["details"].get("http_status") == 422 for r in records(settings.log_dir))


def test_http_multipart_upload(api, fake, metadata, package, settings):
    with package.open("rb") as stream:
        response = api.post(
            "/v1/otas",
            data={"metadata": metadata.model_dump_json()},
            files={"package": ("firmware.zip", stream, "application/zip")},
        )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["id"] == 25000
    assert not list(settings.state_dir.glob("upload-*"))


def test_firmware_http_returns_json_not_zip(api, fake):
    result = api.post("/v1/otas/24306/firmware", json={"current_version": "13.25.0501.01"})
    assert result.status_code == 200
    assert result.json()["matched"]


def test_openapi_contains_bearer_security_and_health_is_readonly(api, fake):
    schema = api.get("/openapi.json").json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert schema["paths"]["/v1/otas"]["get"]["security"]
    assert api.get("/health").json() == {"status": "ok"}
    assert not fake.requests


def test_multiline_remarks_round_trip_and_delete_preview(api, fake, settings):
    remarks = "PATRICK\r\n固件名：SQ68_UFS_GMS-NOWIZARD\r\n备注：\r\n- 只对 UFS 定制为空的设备有效。"
    fake.tasks[24306]["remarks"] = remarks
    assert api.get("/v1/otas/24306").json()["remarks"] == remarks
    assert api.delete("/v1/otas/24306?dry_run=true").status_code == 200
    assert not fake.writes
    assert any(r["action"] == "ota.get" for r in records(settings.log_dir))


def test_api_audit_unavailable_blocks_before_upstream(api, fake, monkeypatch):
    from ota_agent.audit import AuditLog
    from ota_agent.errors import AuditError

    original = AuditLog.event

    def unavailable(self, action, status, **kwargs):
        if action == "api.request" and status == "started":
            raise AuditError("disk unavailable")
        return original(self, action, status, **kwargs)

    monkeypatch.setattr(AuditLog, "event", unavailable)
    assert api.delete("/v1/otas/24306").status_code == 503
    assert not fake.requests
