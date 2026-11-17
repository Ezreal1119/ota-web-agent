from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from . import sn
from .audit import AuditLog
from .config import Settings
from .errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    GuardError,
    NotFoundError,
    OTAError,
    UncertainWriteError,
    ValidationError,
)
from .forms import parse_form
from .models import CreateOTA
from .tls import build_ssl_context

FILTERS = {
    "osType",
    "devType",
    "model",
    "standard",
    "osVersion",
    "osLanguage",
    "bagType",
    "highVersion",
    "status",
    "remarks",
    "startTime",
    "endTime",
}


def language_code(value: str) -> str:
    values = {"英文": "english", "中文": "chinese", "english": "english", "chinese": "chinese"}
    if value not in values:
        raise ValidationError("无法识别任务的语言字段。")
    return values[value]


class OTAClient:
    """One authenticated console session. Use a fresh instance per HTTP request/thread."""

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog | None = None,
        *,
        actor: str = "sdk",
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self.audit = audit or AuditLog(settings.log_dir, settings.secrets)
        self.actor = actor
        self._logged_in = False
        self._write_depth = 0
        self._hints: dict[int, dict] = {}
        with self.audit.operation("client.open", actor):
            self._http = httpx.Client(
                verify=build_ssl_context(settings) if transport is None else True,
                transport=transport,
                timeout=settings.timeout,
                follow_redirects=False,
                trust_env=False,
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except OTAError:
            if exc is None:
                raise

    def close(self):
        try:
            if self._logged_in:
                with self.audit.operation("logout", self.actor):
                    self._raw("GET", "userManage_logout.action")
                self._logged_in = False
        finally:
            self._http.close()

    def _url(self, path: str) -> str:
        url = urljoin(self.settings.console_url, path)
        actual, expected = urlsplit(url), urlsplit(self.settings.console_url)
        if (
            actual.scheme != expected.scheme
            or actual.netloc != expected.netloc
            or not actual.path.startswith(expected.path)
        ):
            raise GuardError("上游地址或重定向离开配置的 OTA 管理服务，已拦截。")
        return url

    def _raw(
        self, method: str, path: str, *, write=False, data=None, files=None, timeout=None
    ) -> httpx.Response:
        url = self._url(path)
        endpoint = re.sub(r";jsessionid=.*", "", urlsplit(url).path)
        with self.audit.operation(
            "http.console", self.actor, method=method, endpoint=endpoint, write=write
        ) as log:
            kwargs = {}
            if files is not None:
                kwargs["files"] = files
            elif data is not None:
                kwargs.update(
                    content=urlencode(data).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            if timeout is not None:
                kwargs["timeout"] = timeout
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if write:
                    raise UncertainWriteError(
                        "写请求传输失败，结果未知；需读回核实，不会自动重试提交。"
                    ) from exc
                raise OTAError("无法连接 OTA 管理服务。") from exc
            log["http_status"] = response.status_code
            if response.status_code >= 400:
                if write:
                    raise UncertainWriteError(f"写请求返回 HTTP {response.status_code}，需读回核实。")
                raise OTAError(f"OTA 管理服务返回 HTTP {response.status_code}。")
            return response

    @staticmethod
    def _login_page(response: httpx.Response) -> bool:
        location = response.headers.get("location", "").lower()
        return (
            "login.jsp" in location
            or "login.action" in location
            or (
                "text" in response.headers.get("content-type", "")
                and re.search(r"name=[\"\']userId[\"\']", response.text, re.I) is not None
                and re.search(r"name=[\"\']password[\"\']", response.text, re.I) is not None
            )
        )

    def login(self):
        with self.audit.operation("login", self.actor):
            self._http.cookies.clear()
            self._logged_in = False
            form = parse_form(self._raw("GET", "login.jsp").text)
            if not form.action or "Login.action" not in form.action:
                raise AuthenticationError("登录表单与预期不符。")
            response = self._raw(
                "POST",
                form.action,
                data={"userId": self.settings.username, "password": self.settings.password},
            )
            for _ in range(5):
                if not response.is_redirect:
                    break
                response = self._raw("GET", urljoin(str(response.url), response.headers.get("location", "")))
            if (
                response.is_redirect
                or self._login_page(response)
                or "versionManage_goVersionPage.action" not in response.text
            ):
                raise AuthenticationError("OTA 登录失败或账号无 OS 管理入口。")
            self._logged_in = True

    def _request(self, method: str, path: str, *, write=False, **kwargs) -> httpx.Response:
        if not self._logged_in:
            self.login()
        response = self._raw(method, path, write=write, **kwargs)
        if self._login_page(response):
            self._logged_in = False
            if write:
                raise UncertainWriteError("提交时会话已过期，未重放写请求；需读回核实。")
            self.login()
            response = self._raw(method, path, **kwargs)
            if self._login_page(response):
                raise AuthenticationError("重新登录后会话仍不可用。")
        if response.is_redirect:
            location = urljoin(str(response.url), response.headers.get("location", ""))
            self._url(location)  # Validate even when not following a write response.
            if not write:
                raise OTAError("读取请求出现未预期重定向。")
        return response

    @staticmethod
    def _json(response: httpx.Response):
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise OTAError("上游响应不是有效 JSON。") from exc

    @contextmanager
    def _write_lock(self):
        # Composite operations use the same single-thread-owned client and hold this lock across steps.
        if self._write_depth:
            self._write_depth += 1
            try:
                yield
            finally:
                self._write_depth -= 1
            return
        try:
            self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                self.settings.state_dir / "write.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
        except OSError as exc:
            raise ConfigurationError("无法创建写操作锁。") from exc
        with os.fdopen(fd, "a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self._write_depth = 1
            try:
                yield
            finally:
                self._write_depth = 0

    def _query(self, filters=None, page=1, page_size=50) -> dict:
        filters = filters or {}
        if set(filters) - FILTERS:
            raise ValidationError("存在不支持的查询字段。")
        if type(page) is not int or type(page_size) is not int or not 1 <= page_size <= 100 or page < 1:
            raise ValidationError("page 必须大于零，page_size 必须为 1–100。")
        if any(not isinstance(v, str) for v in filters.values()):
            raise ValidationError("查询字段必须为字符串。")
        params = {"currPage": str(page), "pageSize": str(page_size)}
        params.update({"osPgmVersion." + k: filters.get(k, "") for k in FILTERS})
        data = self._json(self._request("POST", "versionManage_queryPgmList.action", data=params))
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise OTAError("上游查询结果缺少 rows。")
        try:
            data["total"] = int(data["total"])
            if data["total"] < 0 or any(
                not isinstance(row, dict) or type(row.get("id")) is not int for row in data["rows"]
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise OTAError("上游任务列表结构不正确。") from exc
        if not data["rows"] and data["total"] > (page - 1) * page_size:
            raise OTAError("上游返回空分页但总数仍有记录，无法核实任务是否存在。")
        for row in data["rows"]:
            self._hints[row["id"]] = {
                k: row[k]
                for k in ("devType", "model", "standard", "osVersion")
                if isinstance(row.get(k), str)
            }
        return data

    def list_otas(self, filters=None, *, page=1, page_size=50) -> dict:
        with self.audit.operation(
            "ota.list", self.actor, filters=filters or {}, page=page, page_size=page_size
        ) as log:
            data = self._query(filters, page, page_size)
            log.update(total=data["total"], ids=[r["id"] for r in data["rows"]])
            return data

    def _matching(self, filters=None):
        seen = set()
        for page in range(1, 10001):
            data = self._query(filters, page, 100)
            for row in data["rows"]:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    yield row
            if not data["rows"] or page * 100 >= data["total"]:
                return
        raise OTAError("查询记录过多，无法完整核实。")

    def _get(self, task_id: int) -> dict:
        if type(task_id) is not int or task_id <= 0:
            raise ValidationError("task_id 必须为正整数。")
        hints = self._hints.get(task_id)
        for row in self._matching(hints):
            if row["id"] == task_id:
                return row
        # Cached selectors may have changed outside this tool; re-scan instead of claiming deletion.
        if hints:
            self._hints.pop(task_id, None)
            for row in self._matching():
                if row["id"] == task_id:
                    return row
        raise NotFoundError(f"未找到任务 {task_id}。")

    def get_ota(self, task_id: int) -> dict:
        with self.audit.operation("ota.get", self.actor, task_id=task_id) as log:
            row = self._get(task_id)
            log.update(remarks=row.get("remarks"), effective=row.get("effective"))
            return row

    def _rules(self, task_id: int) -> tuple[dict, list[str]]:
        form = parse_form(
            self._request("GET", f"versionManage_findRegulation.action?regulationOsId={task_id}").text
        )
        if form.fields.get("regulationOsId") != str(task_id) or "regularContent" not in form.fields:
            raise OTAError("SN 表单任务 ID 或字段不正确。")
        if form.fields.get("dvcFlagReg") != "1":
            raise GuardError("只支持具有指定 SN 的设备模式，当前规则模式不可修改。")
        entries = [s for s in form.fields["regularContent"].split(";") if s]
        return form.fields, entries

    def get_sns(self, task_id: int) -> dict:
        with self.audit.operation("sn.get", self.actor, task_id=task_id) as log:
            self._get(task_id)
            _, entries = self._rules(task_id)
            log["entries"] = entries
            return {"task_id": task_id, "mode": 1, "entries": entries}

    def change_sns(self, task_id: int, entries: list[str], *, remove=False, dry_run=False) -> dict:
        action = "sn.remove" if remove else "sn.add"
        with (
            self.audit.operation(
                action, self.actor, task_id=task_id, requested=entries, dry_run=dry_run
            ) as log,
            self._write_lock(),
        ):
            self._get(task_id)
            fields, before = self._rules(task_id)
            after = sn.change(before, entries, remove=remove)
            changed = sn.canonical(before) != after
            if not after:
                raise GuardError("平台未验证空 SN 名单行为，禁止清空最后一个 SN；可单独禁用任务。")
            result = {
                "task_id": task_id,
                "before": before,
                "after": after,
                "changed": changed,
                "dry_run": dry_run,
            }
            log.update(result)
            if dry_run or not changed:
                return result
            latest_fields, latest_entries = self._rules(task_id)
            if latest_fields != fields or latest_entries != before:
                raise ConflictError("SN 规则已被其他操作修改，本次未提交。")
            fields["regularContent"] = ";".join(after)
            self.audit.event(
                action, "prepared", actor=self.actor, task_id=task_id, before=before, after=after
            )
            failure = None
            try:
                self._request(
                    "POST",
                    "versionManage_addReg.action",
                    write=True,
                    files=[(k, (None, v)) for k, v in fields.items()],
                )
            except UncertainWriteError as exc:
                failure = exc
            try:
                _, saved = self._rules(task_id)
                if sn.canonical(saved) != after:
                    raise UncertainWriteError("SN 写入后读回不一致，请检查任务规则；不会重复提交。")
            except (OTAError, ValidationError) as exc:
                raise UncertainWriteError("无法确认 SN 保存结果，请查询任务规则后再操作。") from exc
            result["verified"] = True
            if failure:
                result["recovered_by_readback"] = True
            return result

    def set_enabled(self, task_id: int, enabled: bool, *, push=False, dry_run=False) -> dict:
        with (
            self.audit.operation(
                "ota.set_enabled", self.actor, task_id=task_id, enabled=enabled, push=push, dry_run=dry_run
            ) as log,
            self._write_lock(),
        ):
            if type(enabled) is not bool or type(push) is not bool:
                raise ValidationError("enabled 和 push 必须是布尔值。")
            if push and not enabled:
                raise ValidationError("禁用操作不能同时请求推送。")
            before = self._get(task_id)
            if type(before.get("effective")) is not int or before["effective"] not in (0, 1):
                raise OTAError("任务启停状态无法识别。")
            if enabled:
                _, rules = self._rules(task_id)
                if not rules:
                    raise GuardError("必须先配置 SN 才能启用任务。")
            changed = before["effective"] != int(enabled)
            result = {
                "task_id": task_id,
                "enabled": enabled,
                "changed": changed,
                "dry_run": dry_run,
                "push_requested": push,
            }
            log.update(result)
            if not changed:
                if push:
                    raise ConflictError("任务已启用；不能以重复启用方式隐式推送。")
                return result
            if dry_run:
                return result
            self.audit.event(
                "ota.set_enabled",
                "prepared",
                actor=self.actor,
                task_id=task_id,
                before=before["effective"],
                enabled=enabled,
                push=push,
            )
            try:
                response = self._request(
                    "POST",
                    "versionManage_changeOsStatus.action",
                    write=True,
                    data={
                        "osId": str(task_id),
                        "status": str(int(enabled)),
                        "osVersion": before["osVersion"],
                        "jPushFlag": str(int(push)),
                    },
                )
            except UncertainWriteError:
                if push:
                    raise UncertainWriteError(
                        "启用或推送结果未知；状态可查询，推送不可通过状态核实，勿重复提交。"
                    ) from None
            else:
                if push and self._json(response) != "push_success":
                    raise UncertainWriteError("状态已提交，但上游未确认推送成功；请检查，不要重复推送。")
            try:
                saved = self._get(task_id)
                if saved.get("effective") != int(enabled):
                    raise UncertainWriteError("启停提交后状态不符合目标。")
            except OTAError as exc:
                raise UncertainWriteError("无法确认启停结果，请查询任务状态后再操作。") from exc
            result["verified"] = True
            return result

    @staticmethod
    def _guard_delete(row: dict):
        remarks = row.get("remarks")
        if not isinstance(remarks, str) or "PATRICK" not in remarks:
            raise GuardError(f"任务 {row.get('id')} 的当前备注不包含 PATRICK，禁止删除。")

    def delete_otas(self, task_ids: list[int], *, dry_run=False) -> dict:
        with (
            self.audit.operation("ota.delete", self.actor, task_ids=task_ids, dry_run=dry_run) as log,
            self._write_lock(),
        ):
            if not task_ids or len(task_ids) > 100 or any(type(i) is not int or i <= 0 for i in task_ids):
                raise ValidationError("请提供 1–100 个有效任务 ID。")
            if len(set(task_ids)) != len(task_ids):
                raise ValidationError("删除任务 ID 不得重复。")
            snapshots = [self._get(i) for i in task_ids]
            for row in snapshots:
                self._guard_delete(row)
            log["before"] = snapshots
            result = {"task_ids": task_ids, "dry_run": dry_run, "deleted": False}
            if dry_run:
                log.update(result)
                return result
            # Recheck every ID immediately before submitting the batch. Never rely on list-screen remarks.
            final_snapshots = [self._get(task_id) for task_id in task_ids]
            for row in final_snapshots:
                self._guard_delete(row)
            self.audit.event(
                "ota.delete", "prepared", actor=self.actor, task_ids=task_ids, before=final_snapshots
            )
            try:
                self._request(
                    "POST",
                    "versionManage_batchDeletePgm.action",
                    write=True,
                    data=[("Id", str(i)) for i in task_ids],
                )
            except UncertainWriteError:
                pass  # Verify absence rather than replaying a potentially completed deletion.
            remaining = []
            try:
                for task_id in task_ids:
                    try:
                        self._get(task_id)
                        remaining.append(task_id)
                    except NotFoundError:
                        pass
            except OTAError as exc:
                raise UncertainWriteError(
                    "删除请求已发送，但无法完整核实结果；请查询任务，不要直接重试。"
                ) from exc
            if remaining:
                raise UncertainWriteError(f"部分或全部任务仍存在：{remaining}，不会重复提交删除。")
            result.update(deleted=True, verified=True)
            log.update(result)
            return result

    def _options(self) -> dict[str, list[str]]:
        data = self._json(self._request("POST", "versionManage_aotoComplete.action", data={}))
        result = {}
        try:
            for key in ("dvcType", "hardware", "customType"):
                values = json.loads(data[key]) if isinstance(data[key], str) else data[key]
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ValueError
                result[key] = values
        except (TypeError, KeyError, ValueError) as exc:
            raise OTAError("设备配置候选列表格式不正确。") from exc
        return result

    def options(self) -> dict:
        with self.audit.operation("ota.options", self.actor) as log:
            data = self._options()
            log.update({k: len(v) for k, v in data.items()})
            return data

    def create_ota(
        self, metadata: CreateOTA, package: Path, *, dry_run=False, upload_name: str | None = None
    ) -> dict:
        with (
            self.audit.operation(
                "ota.create", self.actor, metadata=metadata.model_dump(), dry_run=dry_run
            ) as log,
            self._write_lock(),
        ):
            package = Path(package)
            name = Path(upload_name or package.name).name
            if not name.lower().endswith(".zip") or not package.is_file():
                raise ValidationError("升级包必须是存在的 ZIP 文件。")
            size = package.stat().st_size
            if size <= 0 or size > self.settings.max_upload_bytes or not zipfile.is_zipfile(package):
                raise ValidationError("升级包不是有效 ZIP 或超出上传大小限制。")
            options = self._options()
            if (
                metadata.dev_type not in options["dvcType"]
                or metadata.model not in options["hardware"]
                or metadata.standard not in options["customType"]
            ):
                raise ValidationError("设备型号、硬件配置或定制类型不在平台候选列表中。")
            selectors = {
                "osType": metadata.os_type,
                "devType": metadata.dev_type,
                "model": metadata.model,
                "standard": metadata.standard,
                "osVersion": metadata.version,
            }
            existing = list(self._matching(selectors))
            if any(
                language_code(r["osLanguage"]) == metadata.language
                and r.get("bagType")
                in ({"完整更新包", "NEW"} if metadata.package_type == "NEW" else {"增量更新包", "ADD"})
                and r.get("highVersion", "") == metadata.high_version
                for r in existing
            ):
                raise ConflictError("相同设备配置、版本、语言和包类型的任务已存在，未重复上传。")
            with package.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            log.update(file_name=name, file_size=size, sha256=digest)
            if dry_run:
                return {
                    "dry_run": True,
                    "metadata": metadata.model_dump(),
                    "file_name": name,
                    "sha256": digest,
                }
            previous_ids = {r["id"] for r in existing}
            self.audit.event(
                "ota.create",
                "prepared",
                actor=self.actor,
                metadata=metadata.model_dump(),
                file_name=name,
                file_size=size,
                sha256=digest,
            )
            with package.open("rb") as stream:
                files = [(k, (None, v)) for k, v in metadata.console_fields().items()]
                files.append(("osPgmVersion.path", (name, stream, "application/zip")))
                try:
                    self._request(
                        "POST",
                        "versionManage_addPgm.action",
                        write=True,
                        files=files,
                        timeout=self.settings.upload_timeout,
                    )
                except UncertainWriteError:
                    pass
            try:
                created = [
                    r
                    for r in self._matching(selectors)
                    if r["id"] not in previous_ids
                    and r.get("devType") == metadata.dev_type
                    and r.get("model") == metadata.model
                    and r.get("standard") == metadata.standard
                    and r.get("osVersion") == metadata.version
                    and r.get("osType") == metadata.os_type
                    and r.get("bagType")
                    in ({"完整更新包", "NEW"} if metadata.package_type == "NEW" else {"增量更新包", "ADD"})
                    and r.get("highVersion", "") == metadata.high_version
                    and r.get("remarks", "") == metadata.remarks
                    and language_code(r["osLanguage"]) == metadata.language
                ]
                if len(created) != 1:
                    raise UncertainWriteError("无法唯一确定新建任务；请查询核实，不要重复上传。")
                row = created[0]
                log["created_id"] = row["id"]
                if row.get("effective") == 1:
                    # New tasks are left disabled until their SN rules have been explicitly configured.
                    self.audit.event(
                        "ota.create.disable_new",
                        "prepared",
                        actor=self.actor,
                        task_id=row["id"],
                        before=1,
                        enabled=False,
                    )
                    self._request(
                        "POST",
                        "versionManage_changeOsStatus.action",
                        write=True,
                        data={
                            "osId": str(row["id"]),
                            "status": "0",
                            "osVersion": metadata.version,
                            "jPushFlag": "0",
                        },
                    )
                row = self._get(row["id"])
                if row.get("effective") != 0:
                    raise UncertainWriteError("新任务已创建，但无法确认其处于禁用状态，请立即核实。")
            except OTAError as exc:
                raise UncertainWriteError(
                    "上传已提交；新任务或其禁用状态无法确认，请查询核实，不要重复上传。"
                ) from exc
            result = {"task": row, "sha256": digest, "verified": True, "dry_run": False}
            log["result_task_id"] = row["id"]
            return result

    def firmware(
        self, task_id: int, *, current_version: str | None = None, dvc_id: str | None = None
    ) -> dict:
        with self.audit.operation(
            "ota.firmware", self.actor, task_id=task_id, current_version=current_version, dvc_id=dvc_id
        ) as log:
            row = self._get(task_id)
            source = current_version or row.get("highVersion")
            if not source or not re.fullmatch(r"\d+(?:\.\d+){3}", source):
                raise ValidationError("必须提供设备当前版本 current_version；增量包可默认使用 highVersion。")
            if not dvc_id:
                _, entries = self._rules(task_id)
                if not entries:
                    raise ValidationError("任务没有 SN 规则，请明确提供 dvc_id。")
                dvc_id = entries[0].split("-")[0]
            params = {
                "protocol": "http",
                "dvcId": dvc_id,
                "osType": row["osType"],
                "osLanguage": language_code(row["osLanguage"]),
                "osVersion": source,
                "networkType": "null",
                "devType": f"{row['devType']}_{row['model']}_{row['standard']}",
                "isload": "true",
                "protocolVer": "02",
                "clientVersion": "1.7.1",
            }
            with self.audit.operation(
                "http.update", self.actor, task_id=task_id, parameters=params
            ) as request_log:
                try:
                    response = self._http.get(self.settings.update_url, params=params)
                except httpx.HTTPError as exc:
                    raise OTAError("设备更新接口连接失败。") from exc
                request_log["http_status"] = response.status_code
                if response.status_code != 200:
                    raise OTAError(f"设备更新接口返回 HTTP {response.status_code}。")
                raw = self._json(response)
            if not isinstance(raw, dict) or not isinstance(raw.get("datas"), dict):
                raise OTAError("设备更新接口 JSON 结构不正确。")
            datas = raw["datas"]
            url = datas.get("updURL", "")
            # Versions can be duplicated across tasks; demand the task ID in the known path convention too.
            matched = bool(
                raw.get("code") == 0
                and datas.get("version") == row["osVersion"]
                and isinstance(url, str)
                and str(task_id) in urlsplit(url).path.split("/")
            )
            result = {"task_id": task_id, "matched": matched, "parameters": params, "response": raw}
            log.update(matched=matched, returned_version=datas.get("version"))
            return result
