"""Compact settings merge preferences without replacing existing owner grants."""
from pydantic import Field

from .config import AutonomyConfig
from .preferences import ProactivePreferences


class SettingsUpdate(ProactivePreferences):
    expectedRevision: int = Field(ge=0)
    enabled: bool


def query_settings(app, role_id):
    state = app.query(role_id)
    config = state['config']
    enabled = config['activeDecisionEnabled'] or config['driftEnabled']
    activity_note = ''
    if enabled:
        if not any(source['enabled'] for source in config['sourceGrants']) and not (
                config['driftAuthoring']['enabled'] or config['driftMeme']['enabled']):
            activity_note = '尚无内容来源或空闲任务，可在高级设置的开发者模式中添加。'
        elif state['runs'] and state['runs'][0]['status'] == 'drift_dependency_pending':
            activity_note = '上次空闲活动缺少可用任务，请在高级设置中检查任务与授权。'
    return {'settings': {**{key: config[key] for key in ProactivePreferences.model_fields},
            'revision': config['revision'], 'enabled': enabled},
        'modelReady': app.role_model_getter(role_id) is not None,
        'cleanupPending': state['cleanupPending'], 'problem': state['admissionError'], 'activityNote': activity_note}


def save_settings(app, role_id, update):
    previous = app.query(role_id)['config']
    if previous['revision'] != update.expectedRevision:
        raise ValueError('autonomy configuration revision changed')
    values = update.model_dump(exclude={'expectedRevision', 'enabled'})
    config = AutonomyConfig.model_validate({**previous, **values,
        'activeDecisionEnabled': update.enabled, 'driftEnabled': update.enabled,
        'inheritRoleModel': True, 'consentRevision': max(previous['consentRevision'], int(update.enabled))})
    app.configure(role_id, config, expected_revision=update.expectedRevision)
    return query_settings(app, role_id)
