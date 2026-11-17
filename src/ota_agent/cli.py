from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError as PydanticValidationError

from .audit import AuditLog
from .client import OTAClient
from .config import Settings
from .errors import OTAError
from .models import CreateOTA


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OTA 管理命令行（HTTP API 使用 ota serve）")
    p.add_argument("--env-file", type=Path, help="环境文件路径；默认读取 .env，已有环境变量优先")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="生成 HTTP API 访问令牌，不写入日志内容")
    serve = sub.add_parser("serve", help="启动 HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    mcp = sub.add_parser("mcp", help="启动 MCP，供 AI Agent 调用")
    mcp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8001)
    mcp.add_argument("--allowed-host", action="append", help="HTTP MCP 允许的 Host，如 192.168.1.10:8001")
    query = sub.add_parser("list", help="查询 OTA")
    query.add_argument("--page", type=int, default=1)
    query.add_argument("--page-size", type=int, default=50)
    for flag in (
        "dev-type",
        "os-type",
        "model",
        "standard",
        "version",
        "language",
        "package-type",
        "remarks",
        "status",
        "start-time",
        "end-time",
    ):
        query.add_argument("--" + flag)
    sub.add_parser("options", help="读取平台候选配置")
    get = sub.add_parser("get", help="读取任务")
    get.add_argument("task_id", type=int)
    delete = sub.add_parser("delete", help="删除备注含 PATRICK 的任务")
    delete.add_argument("task_ids", nargs="+", type=int)
    delete.add_argument("--dry-run", action="store_true")
    for name in ("enable", "disable"):
        status = sub.add_parser(name)
        status.add_argument("task_id", type=int)
        status.add_argument("--dry-run", action="store_true")
        if name == "enable":
            status.add_argument("--push", action="store_true")
    sns = sub.add_parser("sns", help="SN 规则")
    sn_sub = sns.add_subparsers(dest="sn_action", required=True)
    for name in ("list", "add", "remove"):
        action = sn_sub.add_parser(name)
        action.add_argument("task_id", type=int)
        if name != "list":
            action.add_argument("entries", nargs="+")
            action.add_argument("--dry-run", action="store_true")
    create = sub.add_parser("create", help="上传 ZIP 新增任务")
    create.add_argument("--metadata", type=Path, required=True)
    create.add_argument("--package", type=Path, required=True)
    create.add_argument("--dry-run", action="store_true")
    firmware = sub.add_parser("firmware", help="查询固件 JSON，不下载 ZIP")
    firmware.add_argument("task_id", type=int)
    firmware.add_argument("--current-version")
    firmware.add_argument("--dvc-id")
    return p


def main(argv=None) -> int:
    # Parse errors are recorded too; no credentials are included in argv or error messages.
    load_dotenv(override=False)
    audit = AuditLog(Path(os.getenv("OTA_LOG_DIR", "logs")))
    try:
        args = parser().parse_args(argv)
        if args.env_file:
            load_dotenv(args.env_file, override=False)
        audit = AuditLog(Path(os.getenv("OTA_LOG_DIR", "logs")))
        if args.command == "token":
            audit.event("cli.token", "succeeded", actor="cli")
            print(secrets.token_urlsafe(32))
            return 0
        settings = Settings.from_env()
        audit = AuditLog(settings.log_dir, settings.secrets)
        with audit.operation("cli.command", "cli", command=args.command):
            if args.command == "mcp":
                from .mcp_server import create_mcp, create_mcp_http_app

                if args.transport == "stdio":
                    create_mcp(settings).run(transport="stdio")
                else:
                    import uvicorn

                    uvicorn.run(
                        create_mcp_http_app(settings, allowed_hosts=args.allowed_host),
                        host=args.host,
                        port=args.port,
                        access_log=False,
                    )
                return 0
            if args.command == "serve":
                import uvicorn

                from .api import create_app

                uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
                return 0
            with OTAClient(settings, audit, actor="cli") as ota:
                if args.command == "list":
                    from .api import ListQuery

                    names = ListQuery.model_fields.keys()
                    values = {k: v for k, v in vars(args).items() if k in names and v is not None}
                    query = ListQuery(**values)
                    result = ota.list_otas(query.filters(), page=query.page, page_size=query.page_size)
                elif args.command == "get":
                    result = ota.get_ota(args.task_id)
                elif args.command == "options":
                    result = ota.options()
                elif args.command == "delete":
                    result = ota.delete_otas(args.task_ids, dry_run=args.dry_run)
                elif args.command in ("enable", "disable"):
                    result = ota.set_enabled(
                        args.task_id,
                        args.command == "enable",
                        push=getattr(args, "push", False),
                        dry_run=args.dry_run,
                    )
                elif args.command == "sns":
                    if args.sn_action == "list":
                        result = ota.get_sns(args.task_id)
                    else:
                        result = ota.change_sns(
                            args.task_id,
                            args.entries,
                            remove=args.sn_action == "remove",
                            dry_run=args.dry_run,
                        )
                elif args.command == "create":
                    metadata = CreateOTA.model_validate_json(args.metadata.read_text())
                    result = ota.create_ota(metadata, args.package, dry_run=args.dry_run)
                elif args.command == "firmware":
                    result = ota.firmware(
                        args.task_id, current_version=args.current_version, dvc_id=args.dvc_id
                    )
                else:
                    raise RuntimeError("unknown command")
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SystemExit as exc:
        audit.event(
            "cli.arguments", "succeeded" if exc.code == 0 else "failed", actor="cli", exit_code=exc.code
        )
        return int(exc.code or 0)
    except Exception as exc:
        # Log configuration and file errors that occur before a client operation starts.
        try:
            audit.event(
                "cli.error",
                "failed",
                actor="cli",
                error_code=exc.code if isinstance(exc, OTAError) else type(exc).__name__,
            )
        except OTAError:
            pass
        if isinstance(exc, OTAError):
            message, code = str(exc), exc.code
        elif isinstance(exc, PydanticValidationError):
            message, code = "; ".join(e["msg"] for e in exc.errors(include_input=False)), "invalid_input"
        elif isinstance(exc, OSError):
            message, code = "无法读取本地文件或创建服务文件。", "local_file_error"
        else:
            message, code = "内部错误，请查看审计日志。", "internal_error"
        print(
            json.dumps({"error": code, "message": audit.scrub(message)}, ensure_ascii=False), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
