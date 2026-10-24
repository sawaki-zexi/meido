"""Owner-facing preferences, within the existing finite runtime ceilings."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProactivePreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    profile: Literal['daily', 'quiet'] = 'daily'
    agentMaxSteps: int = Field(default=35, ge=1, le=35)
    agentContentLimit: int = Field(default=5, ge=1, le=5)
    agentWebFetchMaxChars: int = Field(default=8000, ge=256, le=8000)
    driftMaxSteps: int = Field(default=20, ge=1, le=20)
    driftMinIntervalHours: float = Field(default=3, ge=3, le=168)

    @property
    def decision_interval(self):
        return 7200 if self.profile == 'quiet' else 1800
