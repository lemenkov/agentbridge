#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Measure whether a host can actually serve a local model (plan step E.5).

Reports **prefill separately from generation**, because prefill is what
decides the question: Claude Code's system prompt plus its tool definitions
run to tens of thousands of tokens, and the model cannot emit anything until
it has read them. A host with a respectable tokens/second figure can still be
unusable if it reads the prompt at ten tokens a second.

``num_ctx`` is sent in the request body rather than set as
``OLLAMA_CONTEXT_LENGTH`` in the service environment, so measuring a 64k
window needs neither a service restart nor root.

    python3 deploy/measure-backend.py qwen3-coder 200 8000 25000

The pass condition is in E.5: a trivial researcher task finishing in under
~120 s with a sensible answer. Record the numbers in the plan, including how
busy the machine was — contention that is written down is a caveat, and
contention that is not is a wrong answer.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

NS = 1_000_000_000

# The rule of thumb that turns a token target into a character count. Only
# used to size filler text, so it does not need to be exact — the real token
# count comes back from the server as prompt_eval_count.
CHARS_PER_TOKEN = 4

# Small prompts measure load time and generation; a prompt this size or under
# gets a fixed trivial string instead of filler.
TRIVIAL_MAX_TOKENS = 500


def filler(root: pathlib.Path, target_tokens: int) -> str:
    """A realistic prose-and-code mix, sized to roughly ``target_tokens``.

    Real repository text rather than random words: tokenisation of prose and
    of source differs enough to matter, and the prompt we care about is mostly
    both.
    """
    parts: list[str] = []
    for name in ("CLAUDE.md", "AGENT.md", "SECURITY.md", "README.md"):
        path = root / name
        if path.is_file():
            parts.append(path.read_text(errors="replace"))
    for path in sorted(root.glob("agentbridge/**/*.py")):
        parts.append(path.read_text(errors="replace"))
    if not parts:
        raise SystemExit(f"no source text found under {root}")
    text = "\n\n".join(parts)
    need = target_tokens * CHARS_PER_TOKEN
    while len(text) < need:
        text += "\n\n" + text
    return text[:need]


def run(api: str, model: str, prompt: str, num_ctx: int, num_predict: int,
        label: str, timeout: float) -> dict | None:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": 0,
        },
    }).encode()
    request = urllib.request.Request(
        api, data=body, headers={"Content-Type": "application/json"})
    wall = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"{label}: FAILED: {exc}")
        return None
    wall = time.monotonic() - wall

    prompt_tokens = payload.get("prompt_eval_count", 0)
    prefill_ns = payload.get("prompt_eval_duration", 0)
    gen_tokens = payload.get("eval_count", 0)
    gen_ns = payload.get("eval_duration", 0)
    result = {
        "label": label,
        "model": model,
        "num_ctx": num_ctx,
        "prompt_tokens": prompt_tokens,
        "prefill_s": prefill_ns / NS,
        "prefill_tps": prompt_tokens / (prefill_ns / NS) if prefill_ns else 0.0,
        "gen_tokens": gen_tokens,
        "gen_s": gen_ns / NS,
        "gen_tps": gen_tokens / (gen_ns / NS) if gen_ns else 0.0,
        "load_s": payload.get("load_duration", 0) / NS,
        "total_s": payload.get("total_duration", 0) / NS,
        "wall_s": wall,
    }
    print(f"{label}: {model} ctx={num_ctx}")
    print(f"  prompt    {result['prompt_tokens']:>7} tok  "
          f"{result['prefill_s']:>8.1f} s  {result['prefill_tps']:>7.1f} tok/s")
    print(f"  generated {result['gen_tokens']:>7} tok  "
          f"{result['gen_s']:>8.1f} s  {result['gen_tps']:>7.1f} tok/s")
    print(f"  load {result['load_s']:.1f} s   total {result['total_s']:.1f} s"
          f"   wall {result['wall_s']:.1f} s")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure prefill and generation for a local model (E.5).")
    parser.add_argument("model", help="model tag, e.g. qwen3-coder")
    parser.add_argument("sizes", nargs="*", type=int,
                        default=[200, 8000, 25000],
                        help="prompt sizes in tokens (default: 200 8000 25000)")
    parser.add_argument("--api", default="http://127.0.0.1:11434/api/generate")
    parser.add_argument("--num-ctx", type=int, default=65536,
                        help="context window, per request (default: 65536)")
    parser.add_argument("--num-predict", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--source", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent,
                        help="repository to draw filler text from")
    parser.add_argument("--json", type=pathlib.Path,
                        help="also write the results here")
    args = parser.parse_args(argv)

    results = []
    for size in args.sizes:
        if size > TRIVIAL_MAX_TOKENS:
            prompt, label = filler(args.source, size), f"{size}-token prompt"
        else:
            prompt, label = "Reply with exactly: OK", "short prompt"
        result = run(args.api, args.model, prompt, args.num_ctx,
                     args.num_predict, label, args.timeout)
        if result:
            results.append(result)
        print()

    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if len(results) == len(args.sizes) else 1


if __name__ == "__main__":
    sys.exit(main())
