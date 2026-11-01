"""Strict visible-output accounting, separate from display/billing counters.

Adapted from Shiori be6343d agent/prompting/output_usage.py.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class OutputTokenUsage:
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    visible_tokens: int | None = None
    unavailable_reason: str = 'missing_completion_usage'
    # Google reports candidates separately; observed thinking does not make
    # that explicit visible count ambiguous when its thoughts count is absent.
    includes_reasoning: bool = True

    def for_response(self, *, has_thinking: bool) -> 'OutputTokenUsage':
        if has_thinking and self.includes_reasoning and self.reasoning_tokens is None and not self.unavailable_reason:
            return replace(self, visible_tokens=None, unavailable_reason='missing_reasoning_usage')
        return self


def parse_output_usage(completion: object, reasoning: object = None, *, visible_only: bool = False) -> OutputTokenUsage:
    """Reject malformed counts before lossy display counters turn them into zero."""
    total = completion if type(completion) is int and completion >= 0 else None
    thought = reasoning if type(reasoning) is int and reasoning >= 0 else None
    reason = ''
    if total is None or total == 0:
        reason = 'missing_completion_usage' if completion is None else 'invalid_completion_usage'
    elif reasoning is not None and (thought is None or (not visible_only and thought >= total)):
        reason = 'invalid_reasoning_usage'
    visible = None if reason or total is None else total if visible_only else total - (thought or 0)
    if visible_only and total is not None:
        total += thought or 0
    return OutputTokenUsage(total, thought, visible, reason, not visible_only)
