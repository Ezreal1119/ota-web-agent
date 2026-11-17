import html
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from ota_agent.client import OTAClient
from ota_agent.config import Settings


def row(task_id=24306, remarks="PATRICK GMS-NOWIZARD", **overrides):
    data = {
        "id": task_id,
        "devType": "SQ68",
        "model": "DS",
        "standard": "PKG-XX",
        "osType": "Android",
        "osVersion": "13.25.0830.01",
        "highVersion": "",
        "osLanguage": "英文",
        "bagType": "完整更新包",
        "effective": 0,
        "remarks": remarks,
        "period": "00000000_00:00:00-00000000_00:00:00",
    }
    data.update(overrides)
    return data


class FakeConsole:
    """Stateful model of the observed console wire protocol; never touches the network."""

    def __init__(self):
        self.tasks = {24306: row(), 24108: row(24108, "OTHER TEAM")}
        self.rules = {24306: ["00772633000001-00772633000005"], 24108: ["00772633000008"]}
        self.requests = []
        self.writes = []
        self.query_count = 0
        self.login_count = 0
        self.expire_read = False
        self.expire_write = False
        self.save_timeout = False
        self.enable_timeout = False
        self.delete_timeout = False
        self.save_noop = False
        self.change_remarks_at_query = None
        self.change_rules_at_read = None
        self.rule_reads = 0
        self.empty_bad_page = False
        self.firmware_id = 24306
        self.create_enabled = False
        self.extra_created = False
        self.upload_failure = False
        self.login_action = "/UpdateConsole/Login.action;jsessionid=hidden-session"

    @staticmethod
    def multipart(request):
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + request.read()
        )
        result, files = {}, {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                files[name] = (part.get_filename(), part.get_payload(decode=True))
            else:
                result[name] = part.get_content().strip("\r\n")
        return result, files

    @staticmethod
    def login_page(action="/UpdateConsole/Login.action"):
        return httpx.Response(
            200,
            text=f'<form action="{action}"><input name="userId"><input name="password"></form>',
            headers={"content-type": "text/html"},
        )

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("login.jsp"):
            return self.login_page(self.login_action)
        if "Login.action" in path:
            self.login_count += 1
            return httpx.Response(
                200,
                text='<li url="versionManage_goVersionPage.action">OS管理</li>',
                headers={"content-type": "text/html"},
            )
        if path.endswith("userManage_logout.action"):
            return self.login_page()
        if path.endswith("versionManage_queryPgmList.action"):
            self.query_count += 1
            if self.expire_read:
                self.expire_read = False
                return self.login_page()
            if self.change_remarks_at_query == self.query_count:
                self.tasks[24306]["remarks"] = "OTHER TEAM"
            data = parse_qs(request.read().decode(), keep_blank_values=True)
            assert "osPgmVersion.osType" in data
            assert "currPage" in data and "pageSize" in data
            selected = list(self.tasks.values())
            for key, values in data.items():
                if key.startswith("osPgmVersion.") and values[0]:
                    field = key.split(".", 1)[1]
                    if field == "status":
                        selected = [r for r in selected if str(r["effective"]) == values[0]]
                    else:
                        selected = [r for r in selected if values[0].lower() in str(r.get(field, "")).lower()]
            page, size = int(data["currPage"][0]), int(data["pageSize"][0])
            return httpx.Response(
                200,
                json={
                    "total": len(selected),
                    "rows": [] if self.empty_bad_page else selected[(page - 1) * size : page * size],
                },
            )
        if path.endswith("versionManage_findRegulation.action"):
            self.rule_reads += 1
            task_id = int(request.url.params["regulationOsId"])
            if self.change_rules_at_read == self.rule_reads:
                self.rules[task_id].append("801625490773334")
            values = {
                "regulationOsId": str(task_id),
                "regularContent": ";".join(self.rules[task_id]),
                "dvcFlagReg": "1",
                "osPgmVersion": "",
            }
            fields = "".join(
                f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
                for k, v in values.items()
            )
            return httpx.Response(
                200,
                text=f'<form action="versionManage_addReg.action">{fields}</form>',
                headers={"content-type": "text/html"},
            )
        if path.endswith("versionManage_addReg.action"):
            self.writes.append("sn")
            if self.expire_write:
                return self.login_page()
            fields, _ = self.multipart(request)
            if not self.save_noop:
                self.rules[int(fields["regulationOsId"])] = fields["regularContent"].split(";")
            if self.save_timeout:
                raise httpx.ReadTimeout("upstream timeout", request=request)
            return httpx.Response(200, text="Saved")
        if path.endswith("versionManage_changeOsStatus.action"):
            self.writes.append("status")
            data = parse_qs(request.read().decode())
            self.tasks[int(data["osId"][0])]["effective"] = int(data["status"][0])
            if self.enable_timeout:
                raise httpx.ReadTimeout("upstream timeout", request=request)
            return httpx.Response(200, json="push_success" if data["jPushFlag"] == ["1"] else "true")
        if path.endswith("versionManage_batchDeletePgm.action"):
            self.writes.append("delete")
            data = parse_qs(request.read().decode())
            for value in data["Id"]:
                del self.tasks[int(value)]
            if self.delete_timeout:
                raise httpx.ReadTimeout("upstream timeout", request=request)
            return httpx.Response(200, json=len(data["Id"]))
        if path.endswith("versionManage_aotoComplete.action"):
            return httpx.Response(
                200,
                json={
                    "dvcType": json.dumps(["SQ68"]),
                    "hardware": json.dumps(["DS"]),
                    "customType": json.dumps(["PKG-XX"]),
                },
            )
        if path.endswith("versionManage_addPgm.action"):
            self.writes.append("create")
            fields, files = self.multipart(request)
            assert files["osPgmVersion.path"][0].endswith(".zip")
            if self.upload_failure:
                return httpx.Response(500)
            task = row(
                25000,
                fields["osPgmVersion.remarks"],
                osVersion=fields["osPgmVersion.osVersion"],
                effective=int(self.create_enabled),
                highVersion=fields["osPgmVersion.highVersion"],
                bagType="完整更新包" if fields["osPgmVersion.bagType"] == "NEW" else "增量更新包",
            )
            self.tasks[25000] = task
            self.rules[25000] = []
            if self.extra_created:
                self.tasks[25001] = {**task, "id": 25001}
            return httpx.Response(200, text="uploaded")
        if path.endswith("osUpdate"):
            task_id = self.firmware_id
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "datas": {
                        "version": "13.25.0830.01",
                        "updURL": f"https://ota-oss.urovo.com/example/{task_id}/patch.zip",
                        "md5Value": "ABC",
                    },
                    "isload": "true",
                },
            )
        raise AssertionError(f"Unexpected endpoint: {request.method} {path}")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        username="test-user",
        password="login-secret-value",
        p12_path=Path("unused.p12"),
        p12_password="p12-secret-value",
        api_token="example-test-token-" + "x" * 32,
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "data",
    )


@pytest.fixture
def fake():
    return FakeConsole()


@pytest.fixture
def transport(fake):
    return httpx.MockTransport(fake)


@pytest.fixture
def ota(settings, transport):
    with OTAClient(settings, transport=transport) as client:
        yield client


@pytest.fixture
def metadata():
    from ota_agent.models import CreateOTA

    return CreateOTA(
        os_type="Android",
        dev_type="SQ68",
        model="DS",
        standard="PKG-XX",
        version="13.26.1117.01",
        language="english",
        remarks="PATRICK TEST",
    )


@pytest.fixture
def package(tmp_path):
    import zipfile

    path = tmp_path / "firmware.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.txt", "mock firmware for tests only")
    return path


def records(directory):
    return [
        json.loads(line)
        for path in sorted(directory.glob("audit-*.jsonl"))
        for line in path.read_text().splitlines()
    ]
