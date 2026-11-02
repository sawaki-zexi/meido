import io
from pathlib import Path
import struct
import zipfile

from fastapi.testclient import TestClient
import pytest

from backend.app import chat_attachments, main
from backend.app.chat_attachments import resolve_attachment, save_attachment
from backend.app.models import RoleInput, RoleProfile
from backend.app.role_store import RoleStore


MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XML = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Document marker</w:t></w:r></w:p></w:body></w:document>'


def docx(document=XML, *, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        if document is not None:
            archive.writestr("word/document.xml", document)
    return output.getvalue()


def invalid_docx(kind):
    if kind == "missing":
        return docx(None)
    if kind == "xml":
        return docx(b"<w:document>not closed")
    if kind == "encoding":
        return docx(b'<?xml version="1.0" encoding="unknown-encoding"?><document/>')
    if kind == "not-zip":
        return b"not a ZIP document"
    if kind == "deflate":
        damaged = bytearray(docx(compression=zipfile.ZIP_DEFLATED))
        with zipfile.ZipFile(io.BytesIO(damaged)) as archive:
            offset = archive.getinfo("word/document.xml").header_offset
        name_size, extra_size = struct.unpack_from("<HH", damaged, offset + 26)
        damaged[offset + 30 + name_size + extra_size] = 7  # Reserved DEFLATE block type.
        return bytes(damaged)
    damaged = bytearray(docx())
    damaged[damaged.index(b"Document marker")] ^= 1
    return bytes(damaged)


@pytest.fixture
def upload_host(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="测试角色", profile=RoleProfile(profile="设定")))
    monkeypatch.setattr(main, "store", roles)
    client = TestClient(main.app, raise_server_exceptions=False)
    root = roles.workspace_path(role.id)
    yield client, role.id, root
    client.close()


def upload(client, role_id, data):
    return client.post(f"/api/roles/{role_id}/attachments", content=data,
                       headers={"content-type": MIME, "x-attachment-filename": "notes.docx"})


def files(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("kind", ["crc", "missing", "xml", "encoding", "not-zip", "deflate"])
def test_invalid_docx_is_rejected_without_new_files_and_can_be_retried(upload_host, kind):
    client, role_id, root = upload_host
    save_attachment(root, "existing.txt", "text/plain", b"Keep this existing attachment")
    before = files(root)

    response = upload(client, role_id, invalid_docx(kind))

    assert response.status_code == 415, response.text
    assert files(root) == before
    detail = response.json()["detail"]
    assert "DOCX" in detail and "重新导出" in detail and "TXT" in detail
    assert str(root) not in detail and "Traceback" not in detail
    assert upload(client, role_id, docx()).status_code == 200
    assert all((root / path).read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_valid_docx_roundtrips_raw_bytes_and_keeps_public_metadata(upload_host, compression):
    client, role_id, root = upload_host
    data = docx(compression=compression)
    response = upload(client, role_id, data)

    assert response.status_code == 200
    public = response.json()
    assert set(public) == {"id", "name", "mediaType", "size", "kind"}
    assert public["name"] == "notes.docx" and public["mediaType"] == MIME
    assert public["size"] == len(data) and public["kind"] == "file"
    internal = resolve_attachment(root, public["id"])
    assert internal["text"] == "Document marker"
    assert client.get(f"/api/roles/{role_id}/attachments/{public['id']}").content == data
    assert len(files(root)) == 2


def test_docx_keeps_existing_text_output_limit(tmp_path):
    text = "字" * (chat_attachments.MAX_ATTACHMENT_TEXT + 20)
    data = docx(XML.replace(b"Document marker", text.encode()), compression=zipfile.ZIP_DEFLATED)
    item = save_attachment(tmp_path, "long.docx", MIME, data)
    assert item["text"] == text[:chat_attachments.MAX_ATTACHMENT_TEXT]


@pytest.mark.parametrize("excess", [0, 1])
def test_docx_body_limit_counts_expanded_bytes_not_compressed_upload(upload_host, monkeypatch, excess):
    client, role_id, root = upload_host
    document = XML.replace(b"Document marker", ("汉" * 1000).encode())
    data = docx(document, compression=zipfile.ZIP_DEFLATED)
    assert len(data) < len(document)
    monkeypatch.setattr(chat_attachments, "MAX_DOCX_XML_BYTES", len(document) - excess)
    before = files(root)
    response = upload(client, role_id, data)
    if excess:
        assert response.status_code == 413
        assert "正文展开" in response.json()["detail"]
        assert files(root) == before
    else:
        assert response.status_code == 200
        assert resolve_attachment(root, response.json()["id"])["text"] == "汉" * 1000


def test_docx_reads_only_body_with_bounded_requests(upload_host, monkeypatch):
    client, role_id, root = upload_host
    data = docx(compression=zipfile.ZIP_DEFLATED)
    opened_members = []
    read_sizes = []
    open_member = zipfile.ZipFile.open

    def observed_open(archive, member, *args, **kwargs):
        opened_members.append(member.filename if isinstance(member, zipfile.ZipInfo) else member)
        source = open_member(archive, member, *args, **kwargs)
        read = source.read

        def bounded_read(size=-1):
            read_sizes.append(size)
            assert 0 < size <= chat_attachments.MAX_DOCX_XML_BYTES + 1
            return read(size)

        source.read = bounded_read
        return source

    monkeypatch.setattr(zipfile.ZipFile, "open", observed_open)
    response = upload(client, role_id, data)
    assert response.status_code == 200
    assert opened_members == ["word/document.xml"]
    assert read_sizes
    assert len(files(root)) == 2


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_docx_rejects_non_opc_body_compression_before_reading(upload_host, monkeypatch, compression):
    client, role_id, root = upload_host
    data = docx(compression=compression)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: pytest.fail("Unsupported body must not be decompressed"))
    response = upload(client, role_id, data)
    assert response.status_code == 415
    assert files(root) == {}


def test_encrypted_docx_body_is_rejected_without_writing(upload_host):
    client, role_id, root = upload_host
    data = bytearray(docx())
    # A synthetic encrypted flag is enough to exercise rejection before any read.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        body = archive.getinfo("word/document.xml")
        struct.pack_into("<H", data, body.header_offset + 6, 1)
    central = data.rfind(b"PK\x01\x02")  # The body is the final member in this fixture.
    struct.pack_into("<H", data, central + 8, 1)
    response = upload(client, role_id, bytes(data))
    assert response.status_code == 415
    assert files(root) == {}


@pytest.mark.parametrize("data", [b"not a ZIP document", invalid_docx("crc")])
def test_explicit_output_docx_never_requests_document_parsing(tmp_path, monkeypatch, data):
    monkeypatch.setattr(chat_attachments, "_extract_text", lambda *args: pytest.fail("Sending output does not request extraction"))
    item = save_attachment(tmp_path, "output.docx", MIME, data, output=True)
    assert item["text"] == ""
    assert Path(item["path"]).read_bytes() == data
    assert save_attachment(tmp_path, "output.docx", MIME, data, output=True)["id"] == item["id"]
    assert len(files(tmp_path)) == 2
