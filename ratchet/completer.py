"""completer — the LLM seam: a `Completion`, the `Completer` contract, and the default binding.

glean's only impure step is the model call, so it is injected as a `Completer`. This module owns the
contract and the shipped binding (the authed `claude` CLI), keeping glean's extract core pure and
offline-testable. A second binding (urllib → the Anthropic API) drops in here as a sibling function,
never touching the core (ADR-0004). Cost *policy* (`_PRICES`) travels with the binding that needs it.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

DEFAULT_MODEL = "haiku"        # cost-aware default (claude CLI alias); --model sonnet for sharper


class CompleterError(RuntimeError):
    """The LLM seam failed (after retries) — surfaced to the caller, never silently swallowed."""


@dataclass
class Completion:
    """One model response. `cost_usd`/tokens are provenance; the CLI reports cost directly, so the
    price table is only a fallback for a Completer that returns tokens but no cost."""
    text: str
    model: str
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


Completer = Callable[[str, str], Completion]  # (system_prompt, user_prompt) -> Completion

_PRICES = {  # $/MTok (input, output) — fallback only, used iff a Completion omits cost_usd
    "haiku": (1.0, 5.0), "claude-haiku-4-5": (1.0, 5.0),
    "sonnet": (3.0, 15.0), "claude-sonnet-4-6": (3.0, 15.0),
    "opus": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
}


def estimate_cost(c: Completion) -> float:
    """Cost from tokens × a price table — only when the Completer didn't report `cost_usd`."""
    price = _PRICES.get(c.model)
    if price is None or c.input_tokens is None or c.output_tokens is None:
        return 0.0
    return c.input_tokens / 1e6 * price[0] + c.output_tokens / 1e6 * price[1]


def cost_of(c: Completion) -> float:
    """A call's cost: the binding's reported `cost_usd` if it has one, else the price-table estimate.
    The LLM stages all account cost this way — one policy, one place."""
    return c.cost_usd if c.cost_usd is not None else estimate_cost(c)


# --- untrusted-output hygiene shared by the LLM stages ---------------------------------------

def clean_score(v, default: float = 0.0) -> float:
    """Clamp an untrusted model score into [0,1]; a non-numeric or non-finite (NaN/inf) value falls
    back to `default`. A scrubbed score, never a raw one, reaches the store — glean and dream share
    this exact contract, so it lives here once."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f)) if math.isfinite(f) else default


def parse_json_object(text: str) -> dict | None:
    """Recover the outermost JSON object from a model response, tolerating the ```json fence the CLI
    wraps results in. The fence is a property of the *binding's* output, so its handling lives with
    the binding, not in each stage. Returns None on anything malformed — the caller owns the empty
    case (no candidates / skip the chunk)."""
    s = text.strip()
    if s.startswith("```"):                       # strip a ```json ... ``` fence
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def make_cli_completer(model: str = DEFAULT_MODEL, *, timeout: int = 240, retries: int = 2,
                       backoff: float = 2.0, cwd: Path | None = None) -> Completer:
    """Bind the default extractor to the authed `claude` CLI. Print mode, JSON envelope (reports
    `total_cost_usd`), a replaced system prompt (no coding-agent prompt), `--max-turns 1`, and an
    **empty `--allowedTools` allowlist** so the model literally cannot call a tool. That last part is
    load-bearing: a transcript excerpt is full of tool calls, and with tools available the model
    sometimes calls one itself, burning the single turn → `error_max_turns`, exit 1, no result. An
    empty allowlist disables tool use *robustly* — no tool names to get wrong (naming them in
    `--disallowed-tools` breaks glean if the CLI ever renames a tool, and still doesn't stop the
    max-turns trip). cwd defaults to the data root — a CLAUDE.md-free, byte-stable dir keeps the
    cached prefix identical every call, so calls after the first read it at ~0.1x.

    Resilient: the JSON envelope is parsed even on a non-zero exit (it carries the real error), and a
    failing call is retried with backoff (transient overload/rate-limit). A call that still fails
    raises CompleterError, which glean isolates per-chunk — one bad call never aborts a run."""
    base = cwd or config.data_root()
    argv = ["claude", "-p", "--model", model, "--output-format", "json",
            "--max-turns", "1", "--allowedTools", ""]

    def complete(system: str, user: str) -> Completion:
        last = "no attempt"
        for attempt in range(retries + 1):
            try:
                proc = subprocess.run([*argv, "--system-prompt", system], input=user,
                                      capture_output=True, text=True, timeout=timeout, cwd=str(base))
            except (OSError, subprocess.TimeoutExpired) as e:
                last = f"{type(e).__name__}: {e}"
            else:
                env = {}
                if proc.stdout.strip():
                    try:
                        env = json.loads(proc.stdout)        # valid JSON even on error_max_turns etc.
                        if not isinstance(env, dict):        # a bare string/number/bool is not usable
                            env = {}
                    except json.JSONDecodeError:
                        env = {}
                if env and not env.get("is_error"):
                    usage = env.get("usage") or {}
                    return Completion(text=env.get("result", ""), model=model,
                                      cost_usd=env.get("total_cost_usd"),
                                      input_tokens=usage.get("input_tokens"),
                                      output_tokens=usage.get("output_tokens"))
                last = (f"error ({env.get('subtype')})" if env
                        else f"exit {proc.returncode}, no JSON: {proc.stderr.strip()[:160]!r}")
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
        raise CompleterError(f"claude CLI: {last}")

    return complete
