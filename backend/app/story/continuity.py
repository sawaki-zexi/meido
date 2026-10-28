# Adapted from Shiori-Agent plugins/story at d3acaed186b172a78a5892e0307e9affd89759f0.
# Copyright (c) 2026 YinFengWindy; MIT. See SHIORI-LICENSE.txt.
"""Fail-closed structural guard for Director draft facts."""

from __future__ import annotations

from .errors import StoryInvalidOutputError
from .models import DirectorDraft, StoryContext


class ContinuityGuard:
    """Validate the currently supported, strongly typed Story draft surface."""

    def validate(self, draft: DirectorDraft, context: StoryContext) -> None:
        """Reject malformed facts before they can become a StoryBeat."""

        try:
            draft.validate()
        except ValueError as exc:
            raise StoryInvalidOutputError(str(exc)) from exc
        for beat in draft.beats:
            for change in beat.fact_changes:
                if not isinstance(change, dict):
                    raise StoryInvalidOutputError("FactChange 必须是对象")
                kind = str(change.get("kind") or "").strip()
                operation = str(change.get("operation") or "").strip()
                if kind == "state" and operation == "set":
                    continue
                if kind == "event" and operation == "record":
                    continue
                raise StoryInvalidOutputError(
                    "FactChange 只支持 state.set 或 event.record"
                )
