from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PERIOD = "00000000_00:00:00-00000000_00:00:00"


class CreateOTA(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    os_type: Literal["Android", "WinCE", "Linux", "OpenHarmony"]
    dev_type: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    standard: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^\d+(?:\.\d+){3}$", max_length=20)
    language: Literal["english", "chinese"]
    package_type: Literal["NEW", "ADD"] = "NEW"
    high_version: str = Field(default="", max_length=20)
    period: str = DEFAULT_PERIOD
    force: bool = False
    silent: bool = False
    immediate: bool = False
    remind: str = Field(default="", max_length=10000)
    remarks: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def validate_package(self):
        import re
        from datetime import datetime

        if self.package_type == "ADD" and not re.fullmatch(r"\d+(?:\.\d+){3}", self.high_version):
            raise ValueError("增量包必须填写四段基础版本 high_version")
        if not re.fullmatch(r"\d{8}_\d{2}:\d{2}:\d{2}-\d{8}_\d{2}:\d{2}:\d{2}", self.period):
            raise ValueError("period 必须是 YYYYMMDD_HH:MM:SS-YYYYMMDD_HH:MM:SS")
        for part in self.period.split("-"):
            day, clock = part.split("_")
            datetime.strptime(clock, "%H:%M:%S")
            if day != "00000000":
                datetime.strptime(day, "%Y%m%d")
        return self

    def console_fields(self) -> dict[str, str]:
        fields = {
            "osType": self.os_type,
            "devType": self.dev_type,
            "model": self.model,
            "standard": self.standard,
            "osVersion": self.version,
            "osLanguage": self.language,
            "bagType": self.package_type,
            "highVersion": self.high_version,
            "period": self.period,
            "isForce": str(int(self.force)),
            "isSilent": str(int(self.silent)),
            "isImmediate": str(int(self.immediate)),
            "jPushFlag": "0",
            "remind": self.remind,
            "remarks": self.remarks,
        }
        return {"osPgmVersion." + key: value for key, value in fields.items()}
