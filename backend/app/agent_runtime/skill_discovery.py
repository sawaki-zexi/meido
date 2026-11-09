"""Small always-visible read tools over host-issued ordinary skill references."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .skills import _hash_bounded_file, _path_has_link
from .tools import AgentToolResult, ToolDefinition
from .types import ToolResultMessage, ImageContent
from .usage_anchor import estimate_tokens
from ..skill_library import dependencies

SKILL_TOOL_NAMES = frozenset({"skill_list", "skill_read"})
SKILL_GUIDANCE = (
    "[技能目录与使用约定]\n"
    "按当前任务选择确实适用的方法；普通闲聊直接回复，无需读取技能。目录描述不是正文，"
    "需要使用时先 skill_read，再通过当前已获准工具执行。读取成功不代表任务完成。"
    "显式 $id/skillIds 已由宿主处理；仅手动技能不能自行调用。"
    "目录不完整时用 skill_list 分页查询。Skill 不增加工具、MCP、环境凭据或脚本执行权限。\n"
    "主人明确要求导入附件或保存方法时：先查已有相近技能，读取附件或已有方法，"
    "通过文件工具保存到 skills/<skill-id>/SKILL.md，再 read_file 读回检查格式、正文和资源，"
    "只有真实保存并核验后才报告保存成功；单独上传附件不安装。名称相同先确认更新对象，保留已有资源。"
    "新的简单格式为 --- 后的 name: lowercase-id 与 description: 适用场景和排除条件，"
    "再 --- 后写输入、步骤、核验与失败处理。可写 allow_implicit_invocation: false 限制手动使用。"
    "不要把未经验证的尝试写成验证成功，不在每轮结束固定创作技能。保存属于当前角色，公共发布需主人在管理页操作。"
)


@dataclass(frozen=True)
class SkillInput:
    catalog: str
    # Only bodies actually selected this run; not a directory version chain.
    guidance: tuple[tuple[str, str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()


class SkillRun:
    def __init__(self, library, *, role_id, session_key, run_id, prompt, explicit_ids=(),
                 capacity=None, max_output=2048, safety_margin=0, authorized=lambda: True, dependency_check=None):
        self.library, self.role_id, self.session_key, self.run_id = library, role_id, session_key, run_id
        self.authorized, self.dependency_check = authorized, dependency_check or library.dependency_check
        self.settings, self.registry, self.rows = library.view()
        self.explicit_ids = frozenset(explicit_ids)
        self.catalog_tokens = max(64, int(capacity * .02)) if capacity else None
        self.catalog_chars = None if capacity else 8000
        self.body_tokens = min(24000, max(128, (capacity - max_output - safety_margin) // 3)) if capacity else 4000
        self.entries = {library.reference(item): item for item in self.registry.skills}
        self.initial_policies = {ref: library.policy(item, self.settings) for ref, item in self.entries.items()}
        self.loaded = {}  # (ref, resource) -> (hash, complete text)
        self.visible = {}
        self.image_resources = set()
        self.preloaded = set()
        self.facts = []
        self.diagnostics = []
        selected_ids = self.explicit_ids | {item.skill_id for item in self.registry.skills
            if library.policy(item, self.settings) == 'constant'}
        for skill_id in sorted(selected_ids):
            skill = next((item for item in self.registry.skills if item.skill_id == skill_id or item.name == skill_id), None)
            if skill is None:
                disabled = any(item["id"] == skill_id and not item["enabled"] for item in self.rows)
                invalid = next((item for item in self.registry.diagnostics if item.get('skillId') == skill_id and item.get('error')), None)
                self.diagnostics.append({"skillId": skill_id, "status": "disabled" if disabled else "unknown",
                    "error": "skill is disabled" if disabled else invalid['error'] if invalid else "skill is not registered"})
                if invalid and not disabled:
                    self.diagnostics[-1]['status'] = 'unavailable'
                continue
            ref = library.reference(skill)
            try:
                text = self._body(skill)
                self.loaded[ref, ""] = (skill.content_hash, text)
                self.preloaded.add(ref)
                self.facts.append(self._fact(ref, "", skill.content_hash, "preloaded"))
            except (ValueError, OSError, UnicodeError) as error:
                self.diagnostics.append({"skillId": skill_id, "status": "unavailable", "error": str(error)})

    def _fact(self, ref, resource, digest, mode="read"):
        return {"ref": ref, "resource": resource, "contentHash": digest, "mode": mode, "runId": self.run_id,
            "roleId": self.role_id, "sessionKey": self.session_key}

    def _budget(self, text):
        # Complete instructions or a bounded error, never half an instruction.
        if estimate_tokens(text) > self.body_tokens:
            raise ValueError("skill content exceeds this run's read budget; use a larger context or shorten the skill")

    def _body(self, skill):
        missing = dependencies(skill, self.dependency_check)
        if missing:
            raise ValueError("skill dependencies are unavailable: " + "; ".join(missing))
        body = skill.materialize().body
        text = (f"[Skill {skill.skill_id}; ref={self.library.reference(skill)}; hash={skill.content_hash}]\n{body}\n"
                + "Supporting resources: " + json.dumps([item.to_dict() for item in skill.supporting_files], ensure_ascii=False))
        self._budget(text)
        return text

    def _denial(self, ref, *, settings=None, registry=None):
        if not self.authorized():
            return "skill source authorization was revoked"
        if ref not in self.entries:
            return "unknown skill reference for this role and run"
        if settings is None:
            settings, registry, _ = self.library.view()
        if not settings["enabled"]:
            return "skill discovery was disabled for this role"
        current = next((item for item in registry.skills if self.library.reference(item) == ref), None)
        if current is None:
            return "skill was disabled, removed or overridden"
        original = self.entries[ref]
        if current.content_hash != original.content_hash or current.supporting_files != original.supporting_files:
            return "skill changed after discovery; start a new turn to use its new version"
        policy = self.library.policy(current, settings)
        if (policy == "manual" or self.initial_policies[ref] == 'manual') and original.skill_id not in self.explicit_ids and original.name not in self.explicit_ids:
            return "manual-only skill requires explicit activation in the current owner input"
        if current.trust_level in {"installed", "external"} and original.skill_id not in self.explicit_ids:
            return "skill trust requires explicit activation"
        return None

    def page(self, *, offset=0, limit=20, query="", initial=False):
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("offset/limit are out of range")
        if len(query) > 120:
            raise ValueError("query exceeds 120 characters")
        settings, registry, _ = self.library.view()
        if not self.authorized() or not settings["enabled"]:
            raise ValueError("skill source authorization was revoked or discovery disabled")
        selected = []
        for ref, skill in self.entries.items():
            if self.initial_policies[ref] == "manual" or self._denial(ref, settings=settings, registry=registry):
                continue
            if query and query.casefold() not in f"{skill.skill_id} {skill.name} {skill.description}".casefold():
                continue
            missing = dependencies(skill, self.dependency_check)
            description = skill.description
            if initial and len(description) > 240:
                description = description[:160] + ' … ' + description[-80:]
            selected.append({"ref": ref, "id": skill.skill_id, "name": skill.name or skill.skill_id,
                "description": description,
                "source": skill.source, "scope": "current_role" if skill.source == "role" else "shared",
                "policy": self.initial_policies[ref], "status": "unavailable" if missing else "available",
                **({"problems": missing} if missing else {})})
        result = {"skills": [], "total": len(selected), "offset": offset, "nextOffset": None, "incomplete": False}
        for row in selected[offset:offset + limit]:
            candidate = {**result, "skills": [*result["skills"], row]}
            raw = json.dumps(candidate, ensure_ascii=False)
            if (initial and ((self.catalog_tokens and estimate_tokens(raw) + 40 > self.catalog_tokens)
                    or self.catalog_chars and len(raw) + 100 > self.catalog_chars)):
                break
            if not initial and estimate_tokens(raw) > max(512, self.body_tokens):
                if not result['skills']:
                    row = {**row, 'description': row['description'][:120] + ' …', 'descriptionTruncated': True}
                else:
                    break
            result["skills"].append(row)
        consumed = offset + len(result["skills"])
        if consumed < len(selected):
            result.update(nextOffset=consumed, incomplete=True)
        return result

    def capture(self):
        """Capture current authorization off-loop; rendering itself does no I/O."""
        try:
            settings, registry, _ = self.library.view()
            catalog = SKILL_GUIDANCE + "\n" + json.dumps(self.page(initial=True, limit=30), ensure_ascii=False)
        except (KeyError, ValueError, OSError):
            return SkillInput("[技能目录不可用：来源许可、角色或设置已变化；不得恢复旧技能]")
        guidance, diagnostics = [], []
        for (ref, resource), (digest, text) in tuple(self.loaded.items()):
            denial = self._denial(ref, settings=settings, registry=registry)
            if denial:
                diagnostics.append(f"Skill {ref}: {denial}")
            else:
                guidance.append((ref + ("/" + resource if resource else ""), digest, text))
        diagnostics.extend("Skill unavailable: " + json.dumps(item, ensure_ascii=False) for item in self.diagnostics)
        return SkillInput(catalog, tuple(guidance), tuple(diagnostics))

    def prompt_for(self, messages, captured):
        visible = set(self._window_receipts(messages))
        blocks = [captured.catalog, *captured.diagnostics]
        blocks.extend(text for ref, digest, text in captured.guidance if (ref, digest) not in visible)
        return "\n\n".join(blocks)

    def _window_receipts(self, messages):
        visible = {}
        for message in messages:
            if not isinstance(message, ToolResultMessage) or message.is_error or not isinstance(message.details, Mapping):
                continue
            fact = message.details.get("skillRead")
            if (isinstance(fact, Mapping) and fact.get('roleId') == self.role_id
                    and fact.get('sessionKey') == self.session_key and fact.get("complete") is True
                    and isinstance(fact.get('ref'), str)):
                identity = fact['ref'] + ("/" + fact["resource"] if fact.get("resource") else "")
                visible[identity, fact.get('contentHash')] = message.content
        return visible

    def commit_window(self, messages):
        # Called only for the request that will actually be sent. Candidate
        # budget probes cannot turn a hidden historical body into loaded state.
        self.visible = self._window_receipts(messages)

    def read(self, ref, resource=""):
        denial = self._denial(ref)
        if denial:
            raise ValueError(denial)
        skill = self.entries[ref]
        missing = dependencies(skill, self.dependency_check)
        if missing:
            raise ValueError('skill dependencies are unavailable: ' + '; '.join(missing))
        # Check the full bundle even for reuse: an edited resource is never
        # silently substituted under an earlier run's hash.
        skill.materialize()
        key = ref, resource
        digest = next((item.content_hash for item in skill.supporting_files if item.path == resource), None) if resource else skill.content_hash
        identity = ref + ('/' + resource if resource else '')
        if key in self.image_resources and (identity, digest) not in self.visible:
            self.loaded.pop(key, None)  # an image removed by compaction must be read again
        if key not in self.loaded and (identity, digest) in self.visible:
            self.loaded[key] = digest, self.visible[identity, digest]
            self.facts.append(self._fact(ref, resource, digest, 'reused'))
        if key in self.loaded:
            digest, _ = self.loaded[key]
            return {"status": "succeeded", "alreadyLoaded": True,
                "skillRead": {**self._fact(ref, resource, digest), "complete": False},
                "content": "Skill guidance already loaded in this run; continue using its existing instructions."}
        if resource:
            if (ref, "") not in self.loaded:
                raise ValueError("read the skill body before requesting its resources")
            entry = next((item for item in skill.supporting_files if item.path == resource), None)
            if entry is None:
                raise ValueError("resource is not in the skill's authorized resource index")
            path = Path(skill.root_path) / entry.path
            if _path_has_link(path) or not path.resolve().is_relative_to(Path(skill.root_path).resolve()):
                raise ValueError("skill resource path is not authorized")
            size, digest = _hash_bounded_file(path)
            if digest != entry.content_hash or size != entry.size:
                raise ValueError("skill resource changed after discovery")
            with path.open('rb') as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) != entry.size or hashlib.sha256(raw).hexdigest() != entry.content_hash:
                raise ValueError('skill resource changed while reading')
            try:
                text = raw.decode('utf-8')
                images = ()
            except UnicodeError:
                from PIL import Image, UnidentifiedImageError
                try:
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.width * image.height > 40_000_000:
                            raise ValueError('skill resource image exceeds pixel limit')
                        media_type = Image.MIME.get(image.format)
                        image.verify()
                    if not media_type:
                        raise ValueError('skill resource has no supported image representation')
                except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                    raise ValueError('binary skill resource is neither UTF-8 text nor a supported image') from None
                images = (ImageContent(media_type, base64.b64encode(raw).decode('ascii'), 'high'),)
                text = f'[Skill image resource {resource} provided. If the image is no longer in the current window, read this resource again before inspecting it.]'
                self.image_resources.add(key)
            self._budget(text)
        else:
            text, digest = self._body(skill), skill.content_hash
            images = ()
        if not self.authorized():
            raise ValueError("skill source authorization was revoked")
        self.loaded[key] = digest, text
        fact = self._fact(ref, resource, digest)
        self.facts.append(fact)
        return {"status": "succeeded", "skillRead": {**fact, "complete": True}, "content": text, 'images': images}


class SkillListTool:
    definition = ToolDefinition("skill_list", "List current role's automatic skill candidates. Paginate incomplete catalogs; optional literal name/description filter. This does not read instructions or grant tools.",
        {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30}, "query": {"type": "string", "maxLength": 120}},
         "additionalProperties": False}, source="builtin:skills", timeout_seconds=15)

    def __init__(self, state):
        self.state = state

    async def execute(self, arguments, context, on_update=None):
        return await _execute(self.state, context, lambda: {"status": "succeeded", "content":
            json.dumps(self.state.page(offset=arguments.get("offset", 0), limit=arguments.get("limit", 20),
                query=arguments.get("query", "")), ensure_ascii=False)})


class SkillReadTool:
    definition = ToolDefinition("skill_read", "Read complete instructions for a host-issued skill ref; optionally read one relative resource from its returned index after reading the body. Manual-only skills require this turn's explicit owner activation. Never accepts host paths.",
        {"type": "object", "properties": {"ref": {"type": "string", "maxLength": 256},
            "resource": {"type": "string", "maxLength": 512}}, "required": ["ref"], "additionalProperties": False},
        source="builtin:skills", timeout_seconds=15)

    def __init__(self, state):
        self.state = state

    async def execute(self, arguments, context, on_update=None):
        return await _execute(self.state, context, lambda: self.state.read(arguments["ref"], arguments.get("resource", "")))


async def _execute(state, context, operation):
    try:
        context.signal.raise_if_cancelled()
        if (context.role_id, context.session_key, context.run_id) != (state.role_id, state.session_key, state.run_id):
            raise ValueError("skill context does not match its host run")
        value = await asyncio.to_thread(operation)
        context.signal.raise_if_cancelled()
        content = value.pop("content")
        images = value.pop('images', ())
        return AgentToolResult(content, value, images=images)
    except (ValueError, KeyError, OSError, UnicodeError) as error:
        message = str(error)
        return AgentToolResult("Skill unavailable: " + message, {"status": "failed", "error": message}, is_error=True)
