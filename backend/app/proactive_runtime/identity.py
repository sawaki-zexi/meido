"""Host-created autonomous identity, separate from an ordinary user run."""
from dataclasses import asdict, dataclass
import json

from ..task_runtime import TaskOrigin


@dataclass(frozen=True, slots=True)
class AutonomyOrigin:
    role_id: str
    tick_id: str
    grant_revision: int
    generation: int
    allowed_modes: tuple[str, ...]

    def public(self):
        return {"roleId": self.role_id, "tickId": self.tick_id,
                "grantRevision": self.grant_revision, "generation": self.generation,
                "allowedModes": list(self.allowed_modes)}

    def serialized(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def restore(cls, value: str):
        fields = json.loads(value)
        fields["allowed_modes"] = tuple(fields["allowed_modes"])
        return cls(**fields)

    def task_origin(self):
        return TaskOrigin(self.role_id, f"autonomy:{self.role_id}", parent_run_id=self.tick_id,
            capability_version=str(self.grant_revision),
            source_scope_id=f"autonomy:{self.role_id}:{self.grant_revision}:{self.tick_id}",
            source_authority_json=self.serialized())
