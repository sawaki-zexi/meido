import asyncio

import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.attachment_tool import ReadChannelAttachmentTool


def test_attachment_reader_only_reads_host_selected_current_turn_files(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree", encoding="utf-8")
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "allowed", "name": "notes.txt", "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())
    result = asyncio.run(tool.execute({"attachment_id": "allowed", "offset": 1, "limit": 1}, context))
    assert result.content.startswith("two\n")
    assert asyncio.run(tool.execute({"attachment_id": str(path)}, context)).is_error
    assert asyncio.run(tool.execute({"attachment_id": "allowed"}, ToolContext("other", "thread", "run", CancellationToken()))).is_error
    assert asyncio.run(tool.execute({"attachment_id": "allowed"}, ToolContext("role", "other-thread", "run", CancellationToken()))).is_error


def test_attachment_reader_pages_long_lines_without_exposing_other_files(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("x" * 9000, encoding="utf-8")
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "allowed", "name": "notes.md", "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())
    result = asyncio.run(tool.execute({"attachment_id": "allowed", "character_offset": 2000}, context))
    assert result.content.startswith("x" * 2000 + "\n")
    assert "character_offset=4000" in result.content


@pytest.mark.parametrize("text", ["中" * 4000, "😀" * 2001, "x" * 8001], ids=["chinese", "four-byte", "ascii"])
def test_attachment_reader_automatically_pages_utf8_long_lines_without_losing_text(tmp_path, text):
    path = tmp_path / "long.txt"
    path.write_text(text, encoding="utf-8")
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "long", "name": path.name, "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())

    pages = []
    for start in range(0, len(text), 2000):
        arguments = {"attachment_id": "long"}
        if start:
            arguments["character_offset"] = start
        result = asyncio.run(tool.execute(arguments, context))
        assert not result.is_error, result.content
        body, footer = result.content.rsplit("\n", 1)
        assert body == text[start:start + 2000]
        assert len(body.encode("utf-8")) <= 10_000
        pages.append(body)
        if start + 2000 < len(text):
            assert f"character_offset={start + 2000}" in footer
        else:
            assert "已读到文件末尾" in footer
            assert "character_offset=" not in footer
    assert "".join(pages) == text


def test_attachment_reader_keeps_line_pages_at_the_byte_threshold_and_handles_eof(tmp_path):
    path = tmp_path / "boundary.md"
    path.write_bytes(("😀" * 2000 + "\r\nsecond line").encode())
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "boundary", "name": path.name, "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())
    result = asyncio.run(tool.execute({"attachment_id": "boundary", "offset": 1, "limit": 1}, context))
    assert result.content == "second line\n[行 1–2/2]"
    eof = asyncio.run(tool.execute({"attachment_id": "boundary", "character_offset": 99999}, context))
    length = len("😀" * 2000 + "\r\nsecond line")
    assert eof.content == f"\n[字符 {length}–{length}/{length}；已读到文件末尾]"


@pytest.mark.parametrize("data", [b"\xff", b"a\0b", b"x" * (2 * 1024 * 1024 + 1)], ids=["invalid-utf8", "nul", "oversize"])
def test_attachment_reader_rejects_nontext_and_oversized_files(tmp_path, data):
    path = tmp_path / "bad.txt"
    path.write_bytes(data)
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "bad", "name": path.name, "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())
    result = asyncio.run(tool.execute({"attachment_id": "bad"}, context))
    assert result.is_error
    assert "无法读取" in result.content


def test_attachment_reader_character_pages_keep_utf8_bom_crlf_and_argument_limits(tmp_path):
    path = tmp_path / "notes.txt"
    text = "中" * 3000 + "\r\n尾页"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    tool = ReadChannelAttachmentTool("role", "thread", [{"id": "notes", "name": path.name, "path": str(path)}])
    context = ToolContext("role", "thread", "run", CancellationToken())
    first = asyncio.run(tool.execute({"attachment_id": "notes"}, context))
    tail = asyncio.run(tool.execute({"attachment_id": "notes", "character_offset": 2000}, context))
    assert first.content.rsplit("\n", 1)[0] + tail.content.rsplit("\n", 1)[0] == text
    for arguments in ({"offset": 1}, {"character_offset": 0, "limit": 1}, {"character_offset": -1}, {"limit": 401}, {"offset": True}):
        assert asyncio.run(tool.execute({"attachment_id": "notes", **arguments}, context)).is_error
