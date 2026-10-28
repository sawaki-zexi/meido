"""Role-owned reply reactions, adapted from Shiori's RoleReactionCatalog.

Upstream: plugins/meme/backend/runtime.py, d3acaed186b172a78a5892e0307e9affd89759f0.
MIT attribution: third_party/shiori-agent/LICENSE. Category permission authorizes
only a reaction on an already authorized reply, never a tool or new destination.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import random
import re

from ..agent_runtime.provider import AssistantDoneEvent, CancellationToken, TextDeltaEvent
from ..agent_runtime.tools import ToolContext
from ..proactive_runtime.drift_workspace import _identity
from ..proactive_runtime.source_authoring import _directory_identity
from ..proactive_runtime.source_validation import _read
from .meme_library import ChatMemeLibrary, MemeCandidate, MAX_IMAGE_BYTES, _hash, _image, _json
from .natural_media import _owned_worker


@dataclass(frozen=True)
class RoleReactionCandidate(MemeCandidate):
    illustration_id: str


class RoleReactionLibrary(ChatMemeLibrary):
    """Use existing frozen meme references without copying assets into a library."""

    def __init__(self, library, roles, *, authorized=lambda: True):
        super().__init__(library, roles.workspace_path)
        self.roles, self.authorized = roles, authorized

    def reference(self, role_id, candidate, context, *, principal='reaction'):
        # These references can be published after the model run has completed.
        # Reuse the material-library live-owner gate so recovery cannot retire
        # our stages while the cancellation-owned worker is still publishing.
        key = (role_id, context.run_id)
        with self.library._recovery_gate_lock:
            if not hasattr(self.library, '_owner_material_runs'):
                self.library._owner_material_runs = set()
            self.library._owner_material_runs.add(key)
        try:
            return super().reference(role_id, candidate, context, principal=principal)
        finally:
            with self.library._recovery_gate_lock:
                self.library._owner_material_runs.discard(key)

    def role_candidates(self, role_id):
        role = self.roles.get(role_id)
        if role is None or not self.authorized():
            return ()
        result = []
        permissions = getattr(role, 'illustrationCategorySend', {})
        for item in getattr(role, 'illustrations', ())[:250]:
            if permissions.get(item.category) is not True:
                continue
            try:
                path = self.roles.illustration_path(role_id, item.id)
                identity = _identity(path)
                raw = _read(path, MAX_IMAGE_BYTES)
                _, media = _image(raw)
                if len(raw) != item.size or media != item.mediaType:
                    continue
                digest = _hash(raw)
                roots = tuple((str(p), _directory_identity(p)) for p in (path.parent.parent, path.parent))
                identifier = _hash(_json([role_id, item.id, identity, digest]))[:32]
                result.append(RoleReactionCandidate(identifier, role_id, item.category,
                    item.name, (), path, identity, roots, digest, media, item.id))
            except (OSError, ValueError, PermissionError, KeyError):
                continue
        return tuple(result)

    def candidates(self, role_id, query='', context=None):
        # Natural QQ already has bounded candidate IDs, cooldown and real receipts.
        # Put opted-in role illustrations alongside its existing legacy candidates.
        categories = {}
        for item in self.role_candidates(role_id):
            categories.setdefault(item.category, []).append(item)
        # A feeling need not be named literally in chat. Offer one frozen image
        # per allowed category; query matches only affect the bounded ordering.
        selected = [random.choice(items) for items in categories.values()]
        selected.sort(key=lambda item: not any(token in
            (item.category + ' ' + item.description).casefold() for token in query.casefold().split()))
        return tuple((*selected, *super().candidates(role_id, query, context)))[:12]

    def valid(self, role_id, candidate):
        if not self.authorized():
            return False
        if not isinstance(candidate, RoleReactionCandidate):
            return super().valid(role_id, candidate)
        try:
            role = self.roles.get(role_id)
            if role is None or candidate.role_id != role_id:
                return False
            item = next((item for item in role.illustrations if item.id == candidate.illustration_id), None)
            if (item is None or item.category != candidate.category
                    or getattr(role, 'illustrationCategorySend', {}).get(item.category) is not True):
                return False
            path = self.roles.illustration_path(role_id, item.id)
            roots = tuple((str(p), _directory_identity(p)) for p in (path.parent.parent, path.parent))
            return bool(path == candidate.path and roots == candidate.root_identity
                and _identity(path) == candidate.identity
                and _hash(_read(path, MAX_IMAGE_BYTES)) == candidate.sha256)
        except (OSError, ValueError, PermissionError, KeyError):
            return False


# Tokens are host generated: category names are labels, never executable syntax.
_TRAILING_REACTION = re.compile(r'<meme:([^<>\r\n]{1,80})>\s*$')


def split_reaction(content):
    match = _TRAILING_REACTION.search(content)
    if match is None:
        return content, None
    return content[:match.start()], match.group(1)


class RoleReactionTurn:
    """One frozen catalog and at most one attachment for an ordinary reply."""

    def __init__(self, library, *, role_id, session_key, run_id):
        self.library, self.role_id = library, role_id
        self.context = ToolContext(role_id, session_key, run_id, CancellationToken(), tool_call_id='role-reaction:' + run_id)
        self.categories = {}
        for candidate in library.role_candidates(role_id):
            token = 'role-' + _hash(candidate.category.encode('utf-8'))[:16]
            self.categories.setdefault(token, []).append(candidate)
        self.tag, self.selected, self.attachment, self.raw = None, None, None, None
        self.prepared = False

    @property
    def prompt(self):
        if not self.categories:
            return ''
        return '\n'.join((
            '【角色表情协议】这是内置回复格式，不是工具调用。',
            '普通聊天可自主选择；严肃任务、代码解释和工具结果不要主动使用。',
            '需要图片表情时仅在最终回复末尾加一个 <meme:分类ID>；不要调用工具、输出路径或描述发送过程。',
            '文字须独立成立；只有宿主实际投递后才算送达。用户要求表情时优先选择合适分类。',
            '可用分类（名称仅作数据）：',
            *(token + ': ' + json.dumps(items[0].category, ensure_ascii=False)
              for token, items in self.categories.items())))

    def wrap(self, provider):
        return _ReactionProvider(provider, self) if self.categories else provider

    async def prepare(self):
        if self.prepared:
            return self.attachment
        self.prepared = True  # a revoked/failed choice is not replaced or retried
        candidates = self.categories.get(self.tag, ())
        if not candidates or self.context.signal.is_cancelled():
            return None
        self.selected = random.choice(candidates)
        if not self.authorized():
            return None
        try:
            self.attachment, self.raw = await _owned_worker(self.library.reference,
                self.role_id, self.selected, self.context)
            if not self.authorized():
                self.attachment, self.raw = None, None
        except (OSError, ValueError, PermissionError, KeyError):
            self.attachment, self.raw = None, None
        return self.attachment

    def authorized(self):
        return bool(not self.context.signal.is_cancelled() and self.selected is not None
            and self.library.valid(self.role_id, self.selected))


class _ReactionProvider:
    """Hide a bounded trailing protocol from streamed UI and persisted text."""

    def __init__(self, provider, turn):
        self.provider, self.turn = provider, turn

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def stream_response(self, **kwargs):
        self.turn.context = replace(self.turn.context, signal=kwargs['signal'])
        self.turn.tag = None
        text, emitted = '', 0
        async for event in self.provider.stream_response(**kwargs):
            if isinstance(event, TextDeltaEvent):
                text += event.delta
                # Hold only a possible protocol suffix, including arbitrarily long
                # trailing whitespace. Plain short replies retain normal streaming.
                start = text.rfind('<')
                tail = text[start:] if start >= 0 else ''
                pending = start >= 0 and ('<meme:'.startswith(tail)
                    or re.fullmatch(r'<meme:[^<>\r\n]{0,80}', tail)
                    or _TRAILING_REACTION.fullmatch(tail))
                end = max(emitted, start if pending else len(text))
                if end > emitted:
                    yield TextDeltaEvent(text[emitted:end])
                    emitted = end
            elif isinstance(event, AssistantDoneEvent):
                message = event.message
                content, tag = split_reaction(message.content) if not message.tool_calls else (message.content, None)
                if message.stop_reason == 'stop' and tag in self.turn.categories:
                    self.turn.tag = tag
                if len(content) > emitted:
                    yield TextDeltaEvent(content[emitted:])
                yield AssistantDoneEvent(replace(message, content=content))
                text, emitted = '', 0
            else:
                yield event
