import base64
import io
import json
import zipfile

from PIL import Image
import pytest

from novelai_fixtures import novelai_environment  # noqa: F401


def png(width=64, height=64):
    output = io.BytesIO()
    Image.new('RGB', (width, height), '#171abc').save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('container', ['zip', 'json'])
def test_generated_images_are_complete_owned_attachments_and_import_is_replayable(tmp_path, container):
    from backend.app.novelai_generation.media import import_response
    from backend.app.chat_attachments import resolve_attachment
    first, second = png(), png(32, 64)
    spool = tmp_path / 'response.bin'
    if container == 'zip':
        with zipfile.ZipFile(spool, 'w') as archive:
            archive.writestr('../../not-a-path.png', first)
            archive.writestr('other.png', second)
        media_type = 'application/zip'
    else:
        spool.write_text(json.dumps({'images': [
            {'image': base64.b64encode(first).decode(), 'index': 0, 'seed': 171},
            {'image': base64.b64encode(second).decode(), 'index': 1, 'seed': 172},
        ]}), encoding='utf-8')
        media_type = 'application/json'
    root = tmp_path / 'role' / 'workspace'
    result = import_response(spool, media_type, root, 'attempt-171', max_pixels=4096)
    assert len(result) == 2
    assert all('path' not in item for item in result)
    assert [resolve_attachment(root, item['id'])['size'] for item in result] == [len(first), len(second)]
    assert import_response(spool, media_type, root, 'attempt-171', max_pixels=4096) == result
    assert len(list((root / 'attachments').glob('*.png'))) == 2
    assert not (tmp_path / 'not-a-path.png').exists()


def test_oversized_actual_image_is_rejected_before_becoming_an_attachment(tmp_path):
    from backend.app.novelai_generation.media import import_response
    spool = tmp_path / 'response.bin'
    spool.write_bytes(png(65, 65))
    with pytest.raises(ValueError):
        import_response(spool, 'image/png', tmp_path / 'role', 'attempt', max_pixels=4096)
    assert not list(tmp_path.rglob('*.png'))
