"""Cross-loop preparation preserves the original attachment error contract."""
import asyncio

import pytest

from backend.app.channels.base import UnsupportedTargetError
from backend.app.channels.host import ChannelHost


class _UnreadStore:
    def __getattr__(self, name):
        raise AssertionError('invalid frozen arguments reached the store: ' + name)


@pytest.mark.parametrize('frozen', [{}, [], {str(index): b'original' for index in range(5)}],
    ids=['empty-mapping', 'not-a-mapping', 'too-many-mappings'])
def test_invalid_frozen_mapping_preserves_original_error_before_dispatch(frozen):
    host = ChannelHost(_UnreadStore())
    with pytest.raises(UnsupportedTargetError, match='冻结附件必须引用原始附件 ID'):
        asyncio.run(host.account_send('role', 'account', 'target', 'message', actor='push',
            attachment_ids=('original',), frozen_attachment_bytes=frozen, authorize_source=lambda: True))
