# 0027 — the maturity bar is the reviewer's knob, not a hidden rule

- Status: accepted — implemented 2026-06-30 (all 21 offline suites green, incl. new `test_review` §1b)
- Date: 2026-06-30
- Supersedes: — (refines the GATE of ADR-0010/0012/0023, which stays; only its EXPOSURE changes)
- Superseded by: —

Code (`review.bar_status`; `review.pending`/`incubating` taking `maturity`; the `review --maturity` flag;
the `_print_queue`/`_print_incubating` rationale lines; the softened `glean.SYSTEM_PROMPT` relevance copy)
is the source of truth; this records the *why*.

## Context

sulin's standing principle (his CLAUDE.md): empower a capable model with *whys* — principles and expected
outcomes — not memorized rules, because "a person who understands the principle handles novel situations; a
person who memorized a rule cannot." He asked ratchet to embody the same thing it is built to cultivate.

The maturity gate was the counter-example. A takeaway surfaced to human review only if its recency-weighted
net entrenchment crossed `MATURITY_WEIGHT = 1.5` (≈ "seen in 2 recent sessions"). That number was a hidden
constant: a reviewer could not move it, `pending` exposed no knob, and an incubating takeaway showed only
"needs 1 more session(s)" — a count, with no score and no *reason*. It read as exactly the kind of
hard-and-fast rule sulin objects to: "If the AI thinks these claims should come up a few times before the
evidence hits a bar, fine, but I don't want a hard and fast rule."

The tension to respect: the gate is not arbitrary. The review queue is the human's scarce attention, and a
one-off lesson risks promoting a belief from a single moment. Corroboration across distinct, recent sessions
is genuine evidence of *durability*. So the answer is not "show everything" — it is to make the bar
**explained, transparent, and the reviewer's to move**, while keeping a sensible default.

## Decision

Keep the recency-weighted entrenchment **score** and its default bar; stop treating the bar as hidden and
fixed (sulin's choice among the options, 2026-06-30 — "keep score, you set the bar").

1. **You set the bar.** `review --pending`/`--incubating` take `--maturity BAR`, threaded into
   `dream.current_takeaways(min_weight=…)`. The default is unchanged (`MATURITY_WEIGHT`), so today's
   graduations are identical; lower it to review more, raise it to demand more corroboration.

2. **Rationale shown, not silence.** `bar_status(tk, bar)` returns the takeaway's `entrenchment` score, the
   `bar`, whether it's `mature`, and a one-line plain `rationale`. Every surfaced takeaway — pending AND
   incubating — carries it. A below-bar takeaway says *which* of the two ways it falls short (too few
   sessions yet → wait for recurrence; enough sessions but AGED evidence → needs recent corroboration),
   because the remedy differs. The footer on an empty/short queue points at `--maturity` and `--incubating`.

3. **Explain the concept, in the prompts too.** The glean relevance instruction was reworded from a rule
   ("never suppress a learning by calling it known") to the *why* behind it (the cost asymmetry: a real
   learning marked "known" sinks out of sight, a duplicate marked "novel" is cheaply de-duplicated later —
   so doubt resolves toward "novel"). This is the same turn ADR-0026 made in glean's main prompt.

## Why this shape

- **A bar you set, that shows its reasoning, is principle-driven — a hidden constant is not.** What sulin
  objected to was the rule being *hidden, unexplained, and unadjustable*, not that a threshold exists.
  Surfacing the score + reason and handing over the knob converts a rule into a judgment the operator makes
  with the principle in view.

- **Deterministic, free, no LLM judge.** The alternative — an LLM deciding graduation per takeaway — was
  considered and declined: it trades auditability and zero-cost for marginal judgment the human already
  applies at the gate. The score orders; the human, holding the principle and the rationale, decides.

- **The two views stay complementary at any bar.** `pending` and `incubating` both gate on the same
  `maturity`, so lowering the bar moves a takeaway from incubating into the queue with no re-mining — same
  score, a line the reviewer moved.

## Consequences

- `pending`/`incubating` items gain `entrenchment`/`bar`/`rationale` (and `mature` on pending). Additive —
  existing consumers (the `/ratchet-review` skill, `--json`) keep working; the skill can now show the score
  and reasoning, and offer the bar as a dial.
- The default behavior is unchanged, so no existing graduation flips; only the exposure is new.
- The `/ratchet-review` SKILL copy should be updated to use the surfaced rationale and mention `--maturity`
  (follow-up; the backend contract is in place).
