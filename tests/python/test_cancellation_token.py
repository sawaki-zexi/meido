"""Linked cooperative cancellation is immediate and only flows to children."""
import pytest

from backend.app.agent_runtime import CancellationToken


def test_linked_token_reads_parent_cancellation_without_a_monitor_tick():
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    grandchild = CancellationToken(parent=child)
    independent = CancellationToken()
    assert not child.cancelled and not child.is_cancelled()
    child.raise_if_cancelled()
    parent.cancel()
    for token in (child, grandchild):
        assert token.cancelled and token.is_cancelled()
        with pytest.raises(RuntimeError, match="agent run cancelled"):
            token.raise_if_cancelled()
    assert not independent.cancelled


def test_cancelling_a_linked_child_does_not_cancel_its_parent_or_sibling():
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    sibling = CancellationToken(parent=parent)
    child.cancel()
    assert child.cancelled
    assert not parent.cancelled and not sibling.cancelled
    parent.raise_if_cancelled()
    sibling.raise_if_cancelled()


def test_linked_token_created_after_parent_cancellation_starts_cancelled():
    parent = CancellationToken()
    parent.cancel()
    child = CancellationToken(parent=parent)
    assert child.cancelled
