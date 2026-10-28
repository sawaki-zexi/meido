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
            'inheritedModelId': config.decisionModelConfigurationId if config.inheritRoleModel else None,
            "context": freeze_context(app.context_reader, role_id),
            "history": app.history_factory(role_id).snapshot() if app.history_factory else None,
            "models": {identifier: model_digest(app.model_getter(identifier)) for identifier in sorted(identifiers)}}


def current(app, role_id, frozen):
    if app.history_factory and (frozen.get('history') is None
            or not app.history_factory(role_id, snapshot=frozen['history']).authorized()):
        return False
    if frozen.get('inheritedModelId'):
        model = shape(app.role_model_getter(role_id))
        if model is None or model.get('id') != frozen['inheritedModelId']:
            return False
    return (shape(app.role_getter(role_id), role=True) == frozen.get("role")
        and all(app.model_getter(identifier) is not None and model_digest(app.model_getter(identifier)) == digest
                for identifier, digest in frozen.get("models", {}).items()))


def persona(frozen):
    role = frozen.get("role") or {}
    profile = role.get("profile", {})
    payload = {"name": role.get("name", ""), "profile": profile if isinstance(profile, dict) else {}}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:12000]


def history_view(app, origin):
    if app.history_factory is None:
        return None
    frozen = app.frozen_bindings(origin).get('history')
    if frozen is None:
        raise PermissionError('autonomous history was not authorized at admission')
    return app.history_factory(origin.role_id, snapshot=frozen)
