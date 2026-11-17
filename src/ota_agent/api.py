import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from .audit import AuditLog
from .client import OTAClient
from .config import Settings
from .errors import AuditError, ConfigurationError, OTAError, ValidationError
from .models import CreateOTA


class SNChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entries: list[str] = Field(min_length=1, max_length=10000)
    dry_run: bool = False


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    push: bool = False
    dry_run: bool = False


class DeleteBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_ids: list[int] = Field(min_length=1, max_length=100)
    dry_run: bool = False


class FirmwareCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_version: str | None = None
    dvc_id: str | None = None


class ListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    os_type: str | None = None
    dev_type: str | None = None
    model: str | None = None
    standard: str | None = None
    version: str | None = None
    language: str | None = None
    package_type: str | None = None
    high_version: str | None = None
    status: str | None = Field(default=None, pattern="^[01]$")
    remarks: str | None = None
    start_time: str | None = None
    end_time: str | None = None

    def filters(self):
        mapping = {
            "os_type": "osType",
            "dev_type": "devType",
            "version": "osVersion",
            "language": "osLanguage",
            "package_type": "bagType",
            "high_version": "highVersion",
            "start_time": "startTime",
            "end_time": "endTime",
        }
        return {
            mapping.get(k, k): v
            for k, v in self.model_dump(exclude_none=True).items()
            if k not in {"page", "page_size"}
        }


def create_app(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    audit = AuditLog(settings.log_dir, settings.secrets)
    if len(settings.api_token) < 32:
        audit.event("api.startup", "failed", error="API token missing or shorter than 32 characters")
        raise ConfigurationError("HTTP 服务要求 OTA_API_TOKEN 至少 32 字符；运行 ota token 生成。")

    @asynccontextmanager
    async def lifespan(app):
        audit.event("api.startup", "succeeded")
        yield
        audit.event("api.shutdown", "succeeded")

    app = FastAPI(
        title="OTA 管理 API",
        version="0.1.0",
        description="删除仅允许当前备注包含 PATRICK 的任务。所有操作均落盘审计。",
        lifespan=lifespan,
    )
    bearer = HTTPBearer(auto_error=False)

    def valid_token(value: str) -> bool:
        # Compare bytes so non-ASCII input is rejected rather than raising TypeError.
        return secrets.compare_digest(value.encode(), settings.api_token.encode())

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if (
            not credentials
            or credentials.scheme.lower() != "bearer"
            or not valid_token(credentials.credentials)
        ):
            from fastapi import HTTPException

            raise HTTPException(401, "无效访问令牌", headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def request_audit(request: Request, call_next):
        operation_id = uuid4().hex
        request.state.operation_id = operation_id
        path = request.url.path[:256]
        details = {"method": request.method, "path": path}
        try:
            audit.event("api.request", "started", operation_id=operation_id, actor="http", **details)
            # Authenticate before parsing potentially large multipart bodies.
            header = request.headers.get("authorization", "")
            scheme, _, value = header.partition(" ")
            if path.startswith("/v1/") and (scheme.lower() != "bearer" or not valid_token(value)):
                response = JSONResponse(
                    {"error": "unauthorized", "message": "无效访问令牌"},
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            elif (
                request.headers.get("content-length", "0").isdigit()
                and int(request.headers.get("content-length", "0")) > settings.max_upload_bytes + 1024**2
            ):
                response = JSONResponse({"error": "upload_too_large", "message": "请求超过上传大小限制"}, 413)
            else:
                response = await call_next(request)
            audit.event(
                "api.request",
                "succeeded" if response.status_code < 400 else "failed",
                operation_id=operation_id,
                actor="http",
                http_status=response.status_code,
                **details,
            )
            response.headers["X-Request-ID"] = operation_id
            return response
        except AuditError as exc:
            return JSONResponse({"error": exc.code, "message": str(exc), "request_id": operation_id}, 503)
        except Exception:
            try:
                audit.event(
                    "api.request",
                    "failed",
                    operation_id=operation_id,
                    actor="http",
                    error="内部错误",
                    **details,
                )
            except AuditError:
                pass
            return JSONResponse(
                {
                    "error": "internal_error",
                    "message": "内部错误，请按请求 ID 查询本地日志。",
                    "request_id": operation_id,
                },
                500,
            )

    @app.exception_handler(OTAError)
    async def ota_error(request: Request, exc: OTAError):
        return JSONResponse(
            {"error": exc.code, "message": audit.scrub(str(exc)), "request_id": request.state.operation_id},
            exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "error": "invalid_input",
                "details": [{"loc": e["loc"], "type": e["type"], "message": e["msg"]} for e in exc.errors()],
                "request_id": request.state.operation_id,
            },
            422,
        )

    def client(request: Request):
        with OTAClient(
            settings, audit, actor="api:" + request.state.operation_id, transport=transport
        ) as ota:
            yield ota

    @app.get("/health", summary="进程存活检查，不连接 OTA 服务")
    def health():
        return {"status": "ok"}

    protected = [Depends(authenticate)]

    @app.get("/v1/otas", dependencies=protected, summary="分页查询 OTA")
    def list_otas(query: Annotated[ListQuery, Query()], ota: Annotated[OTAClient, Depends(client)]):
        return ota.list_otas(query.filters(), page=query.page, page_size=query.page_size)

    @app.get("/v1/options", dependencies=protected, summary="读取允许的设备、硬件、定制类型")
    def options(ota: Annotated[OTAClient, Depends(client)]):
        return ota.options()

    @app.post("/v1/otas/batch-delete", dependencies=protected, summary="批量删除；任一备注不符合则整批拦截")
    def batch_delete(body: DeleteBatch, ota: Annotated[OTAClient, Depends(client)]):
        return ota.delete_otas(body.task_ids, dry_run=body.dry_run)

    @app.post("/v1/otas", dependencies=protected, summary="上传 ZIP 新增任务；最终保持禁用")
    def create_ota(
        ota: Annotated[OTAClient, Depends(client)],
        metadata: Annotated[str, Form(description="CreateOTA 字段组成的 JSON 字符串")],
        package: Annotated[UploadFile, File()],
        dry_run: bool = False,
    ):
        try:
            config = CreateOTA.model_validate_json(metadata)
        except PydanticValidationError as exc:
            raise ValidationError(
                "metadata 无效：" + "; ".join(e["msg"] for e in exc.errors(include_input=False))
            ) from exc
        settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="upload-", dir=settings.state_dir) as directory:
            path = Path(directory) / "package.zip"
            size = 0
            try:
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    while chunk := package.file.read(1024**2):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise ValidationError("上传包超过大小限制。")
                        stream.write(chunk)
                return ota.create_ota(
                    config, path, dry_run=dry_run, upload_name=package.filename or "package.zip"
                )
            finally:
                package.file.close()

    @app.get("/v1/otas/{task_id}", dependencies=protected, summary="读取单条任务")
    def get_ota(task_id: int, ota: Annotated[OTAClient, Depends(client)]):
        return ota.get_ota(task_id)

    @app.delete("/v1/otas/{task_id}", dependencies=protected, summary="删除备注包含 PATRICK 的任务")
    def delete_ota(task_id: int, ota: Annotated[OTAClient, Depends(client)], dry_run: bool = False):
        return ota.delete_otas([task_id], dry_run=dry_run)

    @app.get("/v1/otas/{task_id}/sns", dependencies=protected, summary="读取 SN 规则")
    def get_sns(task_id: int, ota: Annotated[OTAClient, Depends(client)]):
        return ota.get_sns(task_id)

    @app.post("/v1/otas/{task_id}/sns/add", dependencies=protected, summary="增加 SN 或区间，保留已有规则")
    def add_sns(task_id: int, body: SNChange, ota: Annotated[OTAClient, Depends(client)]):
        return ota.change_sns(task_id, body.entries, dry_run=body.dry_run)

    @app.post(
        "/v1/otas/{task_id}/sns/remove", dependencies=protected, summary="移除 SN 或区间，必要时拆分区间"
    )
    def remove_sns(task_id: int, body: SNChange, ota: Annotated[OTAClient, Depends(client)]):
        return ota.change_sns(task_id, body.entries, remove=True, dry_run=body.dry_run)

    @app.put("/v1/otas/{task_id}/status", dependencies=protected, summary="设置启停；推送独立且默认关闭")
    def set_status(task_id: int, body: StatusChange, ota: Annotated[OTAClient, Depends(client)]):
        return ota.set_enabled(task_id, body.enabled, push=body.push, dry_run=body.dry_run)

    @app.post("/v1/otas/{task_id}/firmware", dependencies=protected, summary="获取设备更新 JSON，不下载 ZIP")
    def firmware(task_id: int, body: FirmwareCheck, ota: Annotated[OTAClient, Depends(client)]):
        return ota.firmware(task_id, current_version=body.current_version, dvc_id=body.dvc_id)

    return app
