import asyncio

import pytest

from backend.app import network_tools


def test_outgoing_image_download_preserves_safe_network_failure(monkeypatch):
    async def rejected(*args, **kwargs):
        raise network_tools.NetworkToolError('http_error', '上游返回错误状态', '稍后重试', http_status=429)

    monkeypatch.setattr(network_tools, '_download', rejected)
    monkeypatch.setattr(network_tools.NetworkToolsConfig, 'from_environment',
        classmethod(lambda cls: cls()))
    with pytest.raises(ValueError, match='图片下载失败（http_error）') as caught:
        asyncio.run(network_tools.download_public_bytes('https://example.com/private-query?token=do-not-display'))
    assert 'example.com' not in str(caught.value)
    assert 'do-not-display' not in str(caught.value)
