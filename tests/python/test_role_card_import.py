import base64
import io
import json
import zipfile
from PIL import Image, PngImagePlugin

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app import role_card_import as import_module
from backend.app.role_card_import import RoleCardImportError, parse_role_card


def test_json_role_card_maps_tavern_fields_and_reports_unmapped_fields():
    result = parse_role_card("mio.json", json.dumps({
        "name": "Mio",
        "description": "简介",
        "personality": "温柔",
        "scenario": "图书馆",
        "first_mes": "你好",
        "tags": ["maid"],
    }).encode())

    assert result["draft"]["name"] == "Mio"
    assert result["draft"]["personality"] == "温柔"
    assert result["draft"]["profile"] == "简介"
    assert "scenario" in result["compatibility"]["discardedFields"]
    assert "first_mes" in result["compatibility"]["discardedFields"]
    assert result["compatibility"]["warnings"]


def test_json_v2_data_wrapper_is_supported():
    result = parse_role_card("mio.json", json.dumps({"spec": "chara_card_v2", "data": {"name": "Mio", "system_prompt": "守则"}}).encode())
    assert result["draft"]["name"] == "Mio"
    assert result["draft"]["behaviorRules"] == "守则"


def test_json_v1_fields_map_persona_and_scenario_and_report_greeting():
    result = parse_role_card("legacy.json", json.dumps({
        "char_name": "旧卡",
        "char_persona": "谨慎、体贴",
        "world_scenario": "雨夜的书房",
        "char_greeting": "主人，欢迎回来。",
    }, ensure_ascii=False).encode())

    assert result["draft"]["name"] == "旧卡"
    assert result["draft"]["personality"] == "谨慎、体贴"
    assert result["draft"]["profile"] == "雨夜的书房"
    assert "char_greeting" in result["compatibility"]["discardedFields"]


def test_invalid_json_and_unsupported_extension_are_rejected():
    with pytest.raises(RoleCardImportError, match="有效的 UTF-8 JSON"):
        parse_role_card("mio.json", b"not-json")
    with pytest.raises(RoleCardImportError, match="仅支持"):
        parse_role_card("mio.txt", b"{}")


def test_charx_requires_root_card_json_and_rejects_path_traversal():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("card.json", json.dumps({"name": "Mio", "description": "D"}))
        archive.writestr("assets/mio.png", b"png")
    result = parse_role_card("mio.charx", buffer.getvalue())
    assert result["draft"]["name"] == "Mio"
    assert result["assets"][0]["name"] == "assets/mio.png"

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../card.json", json.dumps({"name": "Mio"}))
    with pytest.raises(RoleCardImportError, match="不安全"):
        parse_role_card("mio.charx", unsafe.getvalue())


def test_charx_declared_embedded_asset_uses_archive_path_but_keeps_logical_name():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("card.json", json.dumps({
            "name": "Mio",
            "assets": [{"type": "icon", "name": "main", "uri": "embeded://assets/main.png", "ext": "png"}],
        }))
        archive.writestr("assets/main.png", b"png-data")

    result = parse_role_card("mio.charx", buffer.getvalue())
    assert result["assets"][0]["kind"] == "avatar"
    assert result["assets"][0]["name"] == "main"
    assert result["assets"][0]["size"] == len(b"png-data")
    assert result["compatibility"]["unsupportedResources"] == []


def test_charx_asset_preview_has_a_response_level_budget(monkeypatch):
    monkeypatch.setattr(import_module, "MAX_ASSET_PREVIEW_TOTAL_BYTES", 5)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("card.json", json.dumps({"name": "Mio"}))
        archive.writestr("assets/one.png", b"1234")
        archive.writestr("assets/two.png", b"5678")

    result = parse_role_card("mio.charx", buffer.getvalue())
    assert result["assets"][0]["dataUrl"]
    assert "dataUrl" not in result["assets"][1]


def test_png_without_role_metadata_is_rejected():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    raw = buffer.getvalue()
    with pytest.raises(RoleCardImportError, match="metadata"):
        parse_role_card("mio.png", raw)


def test_preview_api_returns_draft_without_creating_a_role():
    before = len(main.store.list())
    response = TestClient(main.app).post(
        "/api/role-card-import/preview",
        content=json.dumps({"name": "Preview", "description": "D"}).encode(),
        headers={"content-type": "application/octet-stream", "x-role-card-filename": "preview.json"},
    )
    assert response.status_code == 200
    assert response.json()["draft"]["name"] == "Preview"
    assert len(main.store.list()) == before


def test_v3_fields_follow_shiori_mapping_and_shiori_summary_extension():
    result = parse_role_card("v3.json", json.dumps({"spec": "chara_card_v3", "spec_version": "3.0", "data": {
        "name": "Mio", "description": "详细身份", "personality": "温柔",
        "system_prompt": "行为守则", "post_history_instructions": "回复限制", "nickname": "主人",
        "extensions": {"shiori": {"description": "卡片简介"}}, "character_book": {"entries": []},
        "mes_example": "{{roll}}", "assets": [{"type": "icon", "name": "main", "uri": "https://example.invalid/a.png", "ext": "png"}],
    }}, ensure_ascii=False).encode())
    assert result["draft"] == {"name": "Mio", "description": "卡片简介", "profile": "详细身份", "personality": "温柔", "behaviorRules": "行为守则", "responseConstraints": "回复限制", "nickname": "主人"}
    assert "character_book" in result["compatibility"]["discardedFields"]
    assert "{{roll}}" in result["compatibility"]["unsupportedMacros"]
    assert "https://example.invalid/a.png" in result["compatibility"]["unsupportedResources"]


@pytest.mark.parametrize("chunk_kind", ["text", "itext", "compressed"])
def test_png_metadata_prefers_ccv3_and_returns_real_image(chunk_kind):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("chara", base64.b64encode(json.dumps({"name": "V1", "description": "old"}).encode()).decode())
    value = base64.b64encode(json.dumps({"data": {"name": "V3", "description": "new"}}).encode()).decode()
    if chunk_kind == "itext":
        metadata.add_itxt("ccv3", value)
    else:
        metadata.add_text("ccv3", value, zip=chunk_kind == "compressed")
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG", pnginfo=metadata)
    result = parse_role_card("card.png", buffer.getvalue())
    assert result["draft"]["name"] == "V3"
    assert result["assets"][0]["kind"] == "avatar"
    assert result["assets"][0]["dataUrl"].startswith("data:image/png;base64,")
