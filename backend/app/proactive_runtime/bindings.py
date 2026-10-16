"""Freeze role policy and exact saved model without persisting credentials."""
import hashlib
import json
from .context import freeze_context


def shape(value, *, role=False):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude={"modelConfig"} if role else set())
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def local_tool_names(role):
    """Interpret dedicated role switches consistently for Drift discovery/use."""
    policy = (role or {}).get('agentConfig', {})
    names = set(policy.get('enabledTools', []))
    if policy.get('memoryRecall', {}).get('enabled'):
        names.add('recall_memory')
    if policy.get('shell', {}).get('enabled'):
        names.add('shell')
    else:
        names.discard('shell')
    return names


def model_digest(value):
    payload = json.dumps(shape(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def freeze(app, role_id, config):
    identifiers = {identifier for enabled, identifier in (
        (config.activeDecisionEnabled, config.decisionModelConfigurationId),
        (config.driftEnabled, config.driftModelConfigurationId)) if enabled and identifier}
    return {"role": shape(app.role_getter(role_id), role=True),
            "context": freeze_context(app.context_reader, role_id),
            "models": {identifier: model_digest(app.model_getter(identifier)) for identifier in sorted(identifiers)}}


def current(app, role_id, frozen):
    return (shape(app.role_getter(role_id), role=True) == frozen.get("role")
        and all(app.model_getter(identifier) is not None and model_digest(app.model_getter(identifier)) == digest
                for identifier, digest in frozen.get("models", {}).items()))


def persona(frozen):
    role = frozen.get("role") or {}
    profile = role.get("profile", {})
    payload = {"name": role.get("name", ""), "profile": profile if isinstance(profile, dict) else {}}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:12000]
