from pydantic import BaseModel, Field


class BrowserUseConfig(BaseModel):
    headed: bool = False
    timeout_seconds: int = Field(default=45, ge=5, le=120)
