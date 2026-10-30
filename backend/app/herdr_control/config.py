"""Existing-session settings; independent of owned Coding task configuration."""
from pydantic import BaseModel, ConfigDict, Field


class HerdrConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    executable: str = Field(default='herdr', min_length=1, title='Herdr 路径')
    session: str = Field(default='default', pattern=r'^[a-zA-Z0-9_-]+$', title='Herdr Session')
    cli_timeout_seconds: int = Field(default=45, ge=5, le=300, title='CLI 超时（秒）')
    task_timeout_seconds: int = Field(default=7200, ge=60, le=86400, title='代理追踪超时（秒）')
