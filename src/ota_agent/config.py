from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    username: str = field(repr=False)
    password: str = field(repr=False)
    p12_path: Path
    p12_password: str = field(repr=False)
    console_url: str = "https://nota.urovo.com:7003/UpdateConsole/"
    update_url: str = "https://nota.urovo.com:7000/Update/osUpdate"
    ca_file: Path | None = None
    timeout: float = 60
    upload_timeout: float = 1800
    log_dir: Path = Path("logs")
    state_dir: Path = Path("data")
    api_token: str = field(default="", repr=False)
    max_upload_bytes: int = 4 * 1024**3
    catalog_file: Path | None = None
    package_dir: Path = Path("data/packages")
    plan_ttl: int = 600

    def __post_init__(self):
        if not self.username or not self.password or not self.p12_password:
            raise ConfigurationError("请设置 OTA_USERNAME、OTA_PASSWORD、OTA_P12_PASSWORD。")
        for url in (self.console_url, self.update_url):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ConfigurationError("OTA 服务地址必须是无凭据、查询和片段的 HTTPS URL。")
        if not self.console_url.endswith("/"):
            raise ConfigurationError("OTA_CONSOLE_URL 必须以 / 结尾。")
        if urlsplit(self.console_url).hostname != urlsplit(self.update_url).hostname:
            raise ConfigurationError("管理与设备更新服务必须使用同一主机名。")
        if self.timeout <= 0 or self.upload_timeout <= 0 or self.max_upload_bytes <= 0 or self.plan_ttl <= 0:
            raise ConfigurationError("超时和上传大小限制必须大于零。")

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.password, self.p12_password, self.api_token) if s)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(override=False)
        try:
            return cls(
                username=os.getenv("OTA_USERNAME", ""),
                password=os.getenv("OTA_PASSWORD", ""),
                p12_path=Path(os.getenv("OTA_P12_PATH", "")),
                p12_password=os.getenv("OTA_P12_PASSWORD", ""),
                console_url=os.getenv("OTA_CONSOLE_URL", "https://nota.urovo.com:7003/UpdateConsole/"),
                update_url=os.getenv("OTA_UPDATE_URL", "https://nota.urovo.com:7000/Update/osUpdate"),
                ca_file=Path(os.environ["OTA_CA_FILE"]) if os.getenv("OTA_CA_FILE") else None,
                timeout=float(os.getenv("OTA_TIMEOUT", "60")),
                upload_timeout=float(os.getenv("OTA_UPLOAD_TIMEOUT", "1800")),
                log_dir=Path(os.getenv("OTA_LOG_DIR", "logs")),
                state_dir=Path(os.getenv("OTA_STATE_DIR", "data")),
                api_token=os.getenv("OTA_API_TOKEN", ""),
                max_upload_bytes=int(os.getenv("OTA_MAX_UPLOAD_BYTES", str(4 * 1024**3))),
                catalog_file=Path(os.environ["OTA_CATALOG_FILE"]) if os.getenv("OTA_CATALOG_FILE") else None,
                package_dir=Path(os.getenv("OTA_PACKAGE_DIR", "data/packages")),
                plan_ttl=int(os.getenv("OTA_PLAN_TTL", "600")),
            )
        except ValueError as exc:
            raise ConfigurationError("环境配置中的数字格式不正确。") from exc
