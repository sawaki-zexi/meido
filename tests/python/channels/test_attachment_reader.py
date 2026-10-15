import asyncio

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
