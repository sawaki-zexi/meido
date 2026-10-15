import asyncio
import base64
import io
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend.app.agent_runtime.file_tools import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext


def context(role_id="role-a", signal=None):
    return ToolContext(role_id, f"role:{role_id}", "run-file", signal or CancellationToken())


def run(tool, arguments, *, ctx=None):
    return asyncio.run(tool.execute(arguments, ctx or context()))


def directory_link(link, target):
    if os.name == "nt":
        # Junctions exercise the actual Windows reparse-point boundary without
        # requiring SeCreateSymbolicLinkPrivilege on developer machines.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_read_file_pages_real_utf8_with_source_and_one_based_line_numbers(tmp_path: Path):
    (tmp_path / "notes.txt").write_bytes("第一行\n第二行\n第三行\n".encode())

    result = run(ReadFileTool("role-a", tmp_path), {"path": "notes.txt", "offset": 1, "limit": 1})

    assert not result.is_error
    assert "     2→第二行" in result.content
    assert "第一行" not in result.content and "第三行" not in result.content
    assert result.details["status"] == "succeeded"
    assert result.details["path"] == "notes.txt"
    assert result.details["nextOffset"] == 2
    assert result.details["totalLines"] == 3
    assert result.details["truncated"] is True


def test_write_creates_parents_and_overwrites_utf8_without_newline_translation(tmp_path):
    tool = WriteFileTool("role-a", tmp_path)
    result = run(tool, {"path": "nested/notes.txt", "content": "你好\r\nnext\n"})
    assert not result.is_error
    assert (tmp_path / "nested/notes.txt").read_bytes() == "你好\r\nnext\n".encode()
    assert result.details["bytesWritten"] == 13
    overwritten = run(tool, {"path": "nested/notes.txt", "content": "new"})
    assert not overwritten.is_error
    assert (tmp_path / "nested/notes.txt").read_bytes() == b"new"


def test_edit_exact_match_preserves_bom_and_crlf_and_returns_unified_diff(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma\r\n")

    result = run(EditFileTool("role-a", tmp_path), {
        "path": "notes.txt", "old_text": "alpha\nbeta", "new_text": "first\nsecond"
    })

    assert not result.is_error
    assert path.read_bytes() == b"\xef\xbb\xbffirst\r\nsecond\r\ngamma\r\n"
    assert "--- notes.txt (before)" in result.content
    assert "-alpha" in result.content and "+first" in result.content
    assert result.details["replacedCount"] == 1


def test_edit_ambiguity_and_missing_match_refuse_without_changing_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"same\nsame\n")
    tool = EditFileTool("role-a", tmp_path)

    ambiguous = run(tool, {"path": "notes.txt", "old_text": "same", "new_text": "new"})
    missing = run(tool, {"path": "notes.txt", "old_text": "absent", "new_text": "new"})

    assert ambiguous.is_error and ambiguous.details["status"] == "denied"
    assert ambiguous.details["matchCount"] == 2
    assert missing.is_error and "not found" in missing.content
    assert path.read_bytes() == b"same\nsame\n"
    all_matches = run(tool, {"path": "notes.txt", "old_text": "same", "new_text": "new", "replace_all": True})
    assert not all_matches.is_error
    assert all_matches.details["replacedCount"] == 2
    assert path.read_bytes() == b"new\nnew\n"


def test_list_dir_is_sorted_one_level_and_has_source(tmp_path):
    (tmp_path / "z-folder").mkdir()
    (tmp_path / "z-folder" / "hidden-child.txt").write_bytes(b"nested")
    (tmp_path / "b.txt").write_bytes(b"b")
    (tmp_path / "a.txt").write_bytes(b"a")

    result = run(ListDirTool("role-a", tmp_path), {"path": "."})

    assert not result.is_error
    assert result.details["path"] == "."
    assert result.content.index("a.txt") < result.content.index("b.txt") < result.content.index("z-folder")
    assert "📁 z-folder" in result.content and "📄 a.txt" in result.content
    assert "hidden-child" not in result.content


def test_read_image_uses_header_and_returns_real_ephemeral_image(tmp_path):
    data = io.BytesIO()
    Image.new("RGB", (2, 3), (128, 64, 32)).save(data, "PNG")
    (tmp_path / "picture.dat").write_bytes(data.getvalue())

    result = run(ReadFileTool("role-a", tmp_path), {"path": "picture.dat"})

    assert not result.is_error
    assert result.details["path"] == "picture.dat"
    assert len(result.images) == 1
    assert result.images[0].media_type == "image/png"
    assert base64.b64decode(result.images[0].data_base64) == data.getvalue()
    assert result.images[0].data_base64 not in result.content
    assert result.images[0].data_base64 not in str(result.details)
    text_only = run(ReadFileTool("role-a", tmp_path, multimodal=False), {"path": "picture.dat"})
    assert text_only.is_error and not text_only.images
    assert "vision" in text_only.content


@pytest.mark.parametrize("tool_type,arguments", [
    (ReadFileTool, {}), (ListDirTool, {}),
    (WriteFileTool, {"content": "overwrite"}),
    (EditFileTool, {"old_text": "private", "new_text": "changed"}),
])
def test_all_tools_refuse_parent_absolute_cross_role_and_forged_identity(tmp_path, tool_type, arguments):
    workspace = tmp_path / "role-a" / "workspace"
    workspace.mkdir(parents=True)
    outside = tmp_path / "role-b" / "workspace" / "private.txt"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"private")
    tool = tool_type("role-a", workspace)

    for target in ("../../role-b/workspace/private.txt", str(outside)):
        result = run(tool, {"path": target, **arguments})
        assert result.is_error and result.details["status"] == "denied"
    result = run(tool, {"path": ".", **arguments}, ctx=context("role-b"))
    assert result.is_error and result.details["status"] == "denied"
    result = run(tool, {"path": "private.txt", "role_id": "role-b", "workspace": str(outside.parent), **arguments})
    assert result.is_error and "unknown arguments" in result.content
    assert outside.read_bytes() == b"private"


@pytest.mark.parametrize("tool_type,arguments", [
    (ReadFileTool, {}), (ListDirTool, {}),
    (WriteFileTool, {"content": "overwrite"}),
    (EditFileTool, {"old_text": "private", "new_text": "changed"}),
])
def test_all_tools_refuse_symlink_file_directory_and_missing_child_escape(tmp_path, tool_type, arguments):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private")
    directory_link(workspace / "directory-link", outside)
    if os.name == "nt":
        os.link(outside / "private.txt", workspace / "file-link")
    else:
        (workspace / "file-link").symlink_to(outside / "private.txt")
    tool = tool_type("role-a", workspace)

    for target in ("directory-link", "directory-link/private.txt", "directory-link/new/secret.txt", "file-link"):
        result = run(tool, {"path": target, **arguments})
        assert result.is_error and result.details["status"] == "denied"
    assert not (outside / "new").exists()
    assert (outside / "private.txt").read_bytes() == b"private"


@pytest.mark.parametrize("tool_type,arguments", [
    (ReadFileTool, {}), (ListDirTool, {}),
    (WriteFileTool, {"content": "overwrite"}),
    (EditFileTool, {"old_text": "private", "new_text": "changed"}),
])
def test_cancelled_file_tool_returns_clear_error_without_side_effect(tmp_path, tool_type, arguments):
    path = tmp_path / "private.txt"
    path.write_bytes(b"private")
    signal = CancellationToken()
    signal.cancel()

    result = run(tool_type("role-a", tmp_path), {"path": "private.txt", **arguments}, ctx=context(signal=signal))

    assert result.is_error and result.details["status"] == "cancelled"
    assert path.read_bytes() == b"private"


def test_read_limits_are_explicit_for_lines_bytes_long_lines_and_scan_budget(tmp_path, monkeypatch):
    (tmp_path / "many.txt").write_bytes(b"x\n" * 450)
    (tmp_path / "wide.txt").write_bytes((b"x" * 5000 + b"\n") * 3)
    (tmp_path / "long.txt").write_bytes(b"x" * 25000 + b"\nsecond\n")
    tool = ReadFileTool("role-a", tmp_path)

    line_limited = run(tool, {"path": "many.txt"})
    assert line_limited.details["outputLines"] == 400
    assert line_limited.details["truncated"] is True
    assert len(line_limited.content) <= 12000
    byte_limited = run(tool, {"path": "wide.txt"})
    assert byte_limited.details["outputLines"] == 1
    assert byte_limited.details["truncatedBy"] == "byte_limit"
    long = run(tool, {"path": "long.txt"})
    assert "Line exceeds 10 KB" in long.content
    assert long.details["outputLines"] == 0
    assert long.details["truncatedBy"] == "long_line"
    monkeypatch.setattr("backend.app.agent_runtime.file_tools.MAX_SCAN_BYTES", 20)
    scan_limited = run(tool, {"path": "many.txt", "offset": 1000000})
    assert scan_limited.details["totalLines"] is None
    assert scan_limited.details["truncatedBy"] == "scan_limit"


def test_text_binary_invalid_utf8_and_invalid_edit_are_distinguished(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"a\0b")
    (tmp_path / "legacy.txt").write_bytes(b"caf\xe9\n")
    binary = run(ReadFileTool("role-a", tmp_path), {"path": "binary.dat"})
    legacy = run(ReadFileTool("role-a", tmp_path), {"path": "legacy.txt"})
    edit = run(EditFileTool("role-a", tmp_path), {"path": "legacy.txt", "old_text": "caf", "new_text": "cafe"})
    assert binary.is_error and "Binary file refused" in binary.content
    assert not legacy.is_error and legacy.details["hadDecodeErrors"]
    assert "Invalid UTF-8" in legacy.content
    assert edit.is_error and (tmp_path / "legacy.txt").read_bytes() == b"caf\xe9\n"


def test_file_mutation_io_limit_and_diff_limit_are_explicit(tmp_path, monkeypatch):
    tool = WriteFileTool("role-a", tmp_path)
    monkeypatch.setattr("backend.app.agent_runtime.file_tools.MAX_FILE_BYTES", 8)
    result = run(tool, {"path": "too-big.txt", "content": "你好你好"})
    assert result.is_error and result.details["status"] == "denied"
    assert result.details["truncatedBy"] == "file_limit"
    assert not (tmp_path / "too-big.txt").exists()

    monkeypatch.setattr("backend.app.agent_runtime.file_tools.MAX_FILE_BYTES", 4 * 1024 * 1024)
    (tmp_path / "diff.txt").write_bytes(b"before\n" * 1000)
    edit = run(EditFileTool("role-a", tmp_path), {"path": "diff.txt", "old_text": "before", "new_text": "after", "replace_all": True})
    assert not edit.is_error and edit.details["truncatedBy"] == "diff_limit"
    assert "Diff truncated" in edit.content
    assert len(edit.content) <= 12000
    assert (tmp_path / "diff.txt").read_bytes() == b"after\n" * 1000


def test_directory_limit_is_visible_and_empty_and_wrong_types_are_real(tmp_path):
    empty = run(ListDirTool("role-a", tmp_path), {"path": "."})
    assert not empty.is_error and "empty" in empty.content
    for index in range(410):
        (tmp_path / f"{index:03}.txt").write_bytes(b"")
    listed = run(ListDirTool("role-a", tmp_path), {"path": "."})
    assert listed.details["truncated"] and listed.details["outputEntries"] == 400
    assert "partial" in listed.content
    assert run(ReadFileTool("role-a", tmp_path), {"path": "."}).is_error
    assert run(ListDirTool("role-a", tmp_path), {"path": "000.txt"}).is_error
    assert run(WriteFileTool("role-a", tmp_path), {"path": ".", "content": "no"}).is_error
    assert run(EditFileTool("role-a", tmp_path), {"path": "missing.txt", "old_text": "a", "new_text": "b"}).is_error


@pytest.mark.parametrize("arguments", [
    {"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": "1"}, {"limit": False}, {"bogus": 1},
])
def test_read_rejects_malformed_arguments(tmp_path, arguments):
    (tmp_path / "notes.txt").write_bytes(b"data")
    assert run(ReadFileTool("role-a", tmp_path), {"path": "notes.txt", **arguments}).is_error


def test_absolute_path_inside_workspace_is_permitted_and_hardlinks_are_refused(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"real")
    assert not run(ReadFileTool("role-a", tmp_path), {"path": str(tmp_path / "notes.txt")}).is_error
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_bytes(b"private")
    try:
        os.link(outside, tmp_path / "linked.txt")
        result = run(ReadFileTool("role-a", tmp_path), {"path": "linked.txt"})
        assert result.is_error and result.details["status"] == "denied"
    finally:
        outside.unlink()


def test_running_read_observes_cancellation_without_waiting_for_full_scan(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x\n" * (8 * 1024 * 1024))

    async def cancel_read():
        signal = CancellationToken()
        task = asyncio.create_task(ReadFileTool("role-a", tmp_path).execute({"path": "large.txt", "offset": 8_000_000}, context(signal=signal)))
        await asyncio.sleep(.01)
        signal.cancel()
        return await task

    result = asyncio.run(cancel_read())
    assert result.is_error and result.details["status"] == "cancelled"


def test_concurrent_edits_from_distinct_tool_instances_do_not_lose_updates(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"first second\n")

    async def edit_together():
        return await asyncio.gather(
            EditFileTool("role-a", tmp_path).execute({"path": "notes.txt", "old_text": "first", "new_text": "ONE"}, context()),
            EditFileTool("role-a", tmp_path).execute({"path": "notes.txt", "old_text": "second", "new_text": "TWO"}, context()),
        )

    results = asyncio.run(edit_together())
    assert all(not result.is_error for result in results)
    assert (tmp_path / "notes.txt").read_bytes() == b"ONE TWO\n"
    assert not list(tmp_path.glob(".meido-*.tmp"))


@pytest.mark.parametrize("content,old,new,expected", [
    (b"a\nb\n", "a\nb", "c\nd", b"c\nd\n"),
    (b"a\r\nb\n", "a\nb", "c", None),
    (b"a\r\nb\r\n", "a", "c\nd", b"c\r\nd\r\nb\r\n"),
    (b"\xef\xbb\xbfa\n", "a", "b", b"\xef\xbb\xbfb\n"),
])
def test_edit_preserves_lf_mixed_endings_and_single_line_crlf_replacement(tmp_path, content, old, new, expected):
    path = tmp_path / "notes.txt"
    path.write_bytes(content)
    result = run(EditFileTool("role-a", tmp_path), {"path": "notes.txt", "old_text": old, "new_text": new})
    assert result.is_error is (expected is None)
    assert path.read_bytes() == (content if expected is None else expected)


def test_image_is_compressed_to_bounded_result_and_pixel_bombs_are_refused(tmp_path, monkeypatch):
    data = io.BytesIO()
    Image.new("RGB", (2000, 1000), "red").save(data, "PNG")
    (tmp_path / "large.png").write_bytes(data.getvalue())
    monkeypatch.setattr("backend.app.agent_runtime.file_tools.MAX_IMAGE_BASE64", 50000)
    # Add harmless PNG trailing bytes so that the input exceeds the direct
    # result budget while remaining a real decodable image.
    (tmp_path / "large.png").write_bytes(data.getvalue() + b"padding" * 10000)
    result = run(ReadFileTool("role-a", tmp_path), {"path": "large.png"})
    assert not result.is_error and result.details["compressed"]
    with Image.open(io.BytesIO(base64.b64decode(result.images[0].data_base64))) as image:
        assert image.size == (1568, 784)
    monkeypatch.setattr("backend.app.agent_runtime.file_tools.MAX_IMAGE_PIXELS", 10)
    rejected = run(ReadFileTool("role-a", tmp_path), {"path": "large.png"})
    assert rejected.is_error and "pixel limit" in rejected.content


@pytest.mark.parametrize("tool_type,arguments", [
    (ReadFileTool, {}), (ListDirTool, {}),
    (WriteFileTool, {"content": "overwrite"}),
    (EditFileTool, {"old_text": "private", "new_text": "changed"}),
])
def test_non_owner_turn_cannot_call_file_tool_even_with_authorized_role(tmp_path, tool_type, arguments):
    (tmp_path / "private.txt").write_bytes(b"private")
    tool = tool_type("role-a", tmp_path, is_owner=False)
    result = run(tool, {"path": "private.txt", **arguments})
    assert result.is_error and result.details["status"] == "denied"
    assert "owner" in result.content
    assert (tmp_path / "private.txt").read_bytes() == b"private"


def test_cancelling_one_file_operation_does_not_cancel_the_parent_run(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x\n" * (8 * 1024 * 1024))

    async def cancel_operation():
        signal = CancellationToken()
        task = asyncio.create_task(ReadFileTool("role-a", tmp_path).execute({"path": "large.txt", "offset": 8_000_000}, context(signal=signal)))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not signal.is_cancelled()

    asyncio.run(cancel_operation())


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace behavior")
@pytest.mark.parametrize("target", ["NUL", "CON.txt", "notes.txt:stream", "notes.txt.", "nested /notes.txt"])
def test_windows_namespace_aliases_and_alternate_streams_cannot_bypass_scope(tmp_path, target):
    result = run(WriteFileTool("role-a", tmp_path), {"path": target, "content": "no"})
    assert result.is_error and result.details["status"] == "denied"
    assert not (tmp_path / "notes.txt").exists()
