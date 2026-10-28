# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Agent drivers: the seam between a wrapped agent's output and the bus.

A driver owns two things the rest of the bridge should not know about: the
flags that make a particular agent emit machine-readable output, and the
parsing of that output into **agent-neutral events**. The bridge publishes
those events and never looks at a line of agent output itself.

Two drivers exist. ``claude-text`` is the original behaviour — every stdout
line is an ``output`` event — and works with any command. ``claude-stream-json``
parses ``claude --print --output-format stream-json --verbose``, which yields
tool-call progress and real token/cost figures.

**Events never carry an agent's intermediate text.** A ``progress`` event
names the tool being called and nothing else: not the tool's input, not
assistant text, and above all not thinking blocks. The task's answer reaches
the bus exactly once, from the final ``result`` event, so ``collect`` folds
output the same way for both drivers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Event grammar of `claude --print --output-format stream-json --verbose`,
# verified against claude 2.1.309 on 2026-10-28 (plan M-6). A trimmed capture
# is in tests/fixtures/stream-json.jsonl.
#
#   system/init             model, apiKeySource, session_id, tools, …
#   system/commands_changed, system/thinking_tokens   (ignored)
#   assistant               message.content: text | thinking | tool_use blocks
#   user                    message.content: tool_result blocks (ignored)
#   rate_limit_event        rate_limit_info (ignored)
#   result/success|error    result (final text), usage, total_cost_usd,
#                           num_turns, duration_ms, is_error, modelUsage
#
# Field names that differ from the plan's guess: the cost is `total_cost_usd`
# (not `cost_usd`), and the cache counters are `cache_read_input_tokens` /
# `cache_creation_input_tokens` (not `cache_*_tokens`).


@dataclass(frozen=True)
class Event:
    """One thing worth putting on the bus.

    ``output`` carries agent text for ``{agent}.output``; ``progress`` carries
    a tool name or a phase for ``{agent}.progress``, which the collector is
    not bound to.
    """

    kind: str
    body: str | None = None
    tool: str | None = None
    phase: str | None = None


class TextDriver:
    """Every non-empty stdout line is output. Works with any wrapped command."""

    name = "claude-text"
    extra_args: tuple[str, ...] = ()

    def feed(self, line: str) -> list[Event]:
        text = line.rstrip()
        return [Event("output", body=text)] if text else []

    def finish(self) -> tuple[dict | None, str | None]:
        return None, None


class StreamJsonDriver:
    """Parses claude's ``stream-json`` events into output, progress and metrics."""

    name = "claude-stream-json"
    extra_args: tuple[str, ...] = ("--output-format", "stream-json", "--verbose")

    def __init__(self) -> None:
        self._metrics: dict | None = None
        self._model: str | None = None
        self._key_source: str | None = None
        self._unparsed = 0

    def feed(self, line: str) -> list[Event]:
        text = line.strip()
        if not text:
            return []
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict):
            # Not our grammar: forward it rather than swallow it, and count it
            # so `done` can say the stream was not clean.
            self._unparsed += 1
            return [Event("output", body=line.rstrip())]
        return self._dispatch(event)

    def _dispatch(self, event: dict) -> list[Event]:
        kind = event.get("type")
        if kind == "system":
            if event.get("subtype") == "init":
                self._model = event.get("model")
                self._key_source = event.get("apiKeySource")
            return []
        if kind == "assistant":
            return self._assistant(event)
        if kind == "result":
            return self._result(event)
        # user (tool_result), rate_limit_event, and anything added later.
        return []

    def _assistant(self, event: dict) -> list[Event]:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        events = [
            Event("progress", tool=block.get("name"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
            and isinstance(block.get("name"), str)
        ]
        if events:
            return events
        # A turn that only thought or spoke. Say that something happened,
        # without the text: it is intermediate, and the final result event
        # carries the answer.
        if any(isinstance(b, dict) and b.get("type") in ("text", "thinking")
               for b in content):
            return [Event("progress", phase="assistant")]
        return []

    def _result(self, event: dict) -> list[Event]:
        usage = event.get("usage") or {}
        models = sorted(event.get("modelUsage") or {})
        self._metrics = {
            # One model is the normal case; several means subagents or a
            # fallback, and then the hint from init is the wrong answer.
            "model": models[0] if len(models) == 1 else (self._model or None),
            "models": models if len(models) > 1 else None,
            "num_turns": event.get("num_turns"),
            "agent_duration_ms": event.get("duration_ms"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "cost_usd": event.get("total_cost_usd"),
            # With a subscription credential apiKeySource is "none" and the
            # cost is claude's own notional figure, not money billed.
            "cost_estimated": self._key_source != "ANTHROPIC_API_KEY",
            "key_source": self._key_source,
            "subtype": event.get("subtype"),
            "is_error": bool(event.get("is_error")),
        }
        self._metrics = {k: v for k, v in self._metrics.items() if v is not None}
        text = event.get("result")
        if isinstance(text, str) and text.strip():
            return [Event("output", body=text)]
        return []

    def finish(self) -> tuple[dict | None, str | None]:
        if self._metrics is None:
            return None, (
                f"stream ended without a result event "
                f"({self._unparsed} unparsable lines)"
                if self._unparsed else "stream ended without a result event"
            )
        metrics = dict(self._metrics)
        if self._unparsed:
            metrics["unparsed_lines"] = self._unparsed
        return metrics, None


DRIVERS = {d.name: d for d in (TextDriver, StreamJsonDriver)}
DEFAULT_DRIVER = TextDriver.name


def make_driver(name: str):
    """A fresh driver instance per task — drivers hold per-task state."""
    try:
        return DRIVERS[name]()
    except KeyError:
        raise ValueError(
            f"unknown driver {name!r}: use one of {', '.join(sorted(DRIVERS))}"
        ) from None
