# 0023 — recency-trust weighting: entrenchment weighted by evidence valid-time

- Status: accepted — implemented 2026-06-28 (all 20 offline suites green, incl. the new `test_recency`)
- Date: 2026-06-28
- Supersedes: — (nothing). Refines ADR-0012's net-entrenchment gate.
- Superseded by: —
- Extends: ADR-0012 (net entrenchment — the count this re-weights) and ADR-0013 (recompute-on-read).

Code (`ratchet/dream.py` — `recency_weight`/`net_entrenchment`/`_session_valid_times`, `current_takeaways`,
`RECENCY_HALF_LIFE_DAYS`/`MATURITY_WEIGHT`; `ratchet/config.py` `age_days(stamp, now)`; `ratchet/review.py`
`importance`/`pending`/`incubating`) is the source of truth; this records the *why*.

## Context

This is trust-critical operability work for a **months-long backfill of OLD conversations**. dream's maturity
gate (ADR-0012) graduates a takeaway to human review when its **net entrenchment** — distinct supporting
sessions minus distinct contradicting sessions — crosses `MATURITY_SESSIONS`. That count is **time-blind**:
a session from two years ago counts exactly as much as one from today. So when we ingest a long backlog of
historical transcripts, two failure modes open, and both corrupt the most expensive thing in the system —
what reaches the human gate:

- **Re-entrenching the stale.** A belief that was true in 2024 and has since moved on gets re-corroborated by
  old sessions in the backlog and **re-matures** — the gate can't tell "still true" from "was true, found
  again in the archive."
- **Overturning the current.** A two-year-old contradiction lands with the **same demotion force** as a
  fresh one and un-graduates a belief that holds *today*.

The root cause is a **bi-temporal** confusion the count never models. Every artifact has two times: its
**transaction-time** (`fetched_at` — when *ratchet* ingested it) and its **valid-time** (when the conversation
*actually happened* — the session's date). A late backfill has a *recent* transaction-time but an *old*
valid-time, and entrenchment must weight by the **latter**. The count uses neither; Aging (ADR-0021) already
uses transaction-time for *queue order*; nothing yet uses valid-time for *trust*.

sulin's framing fixed the shape of the fix: **"disallow is too strong."** The instinct — refuse to let old
data move the gate — is a hard cutoff, and a cutoff is brittle (where's the line? what about evidence one day
past it?). The right primitive is **weight, not gate**: *newer information carries higher trust, on a
continuous curve*. Old corroboration still counts — just **less**.

## Decisions

### Weight each piece of evidence by its valid-time — an exponential decay

`recency_weight(valid_time, now) = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)`: weight **1.0 at age 0**, **0.5
at one half-life**, halving every half-life, → 0 as the evidence recedes. Exponential (not linear) because
"how much do I still trust this" is naturally a half-life question — there is no age at which evidence
abruptly becomes worthless, it just keeps decaying — and a half-life is the one parameter that needs no
arbitrary horizon.

A **missing or unparseable valid-time → weight 1.0** (treat as fresh). This is **recall-first**: the costly
error is silently DISCOUNTING a real learning we merely couldn't date (the invisible false-negative ADR-0005
warns of), so an undateable session is given full trust, never sunk on doubt. `config.age_days` already
degrades a missing/garbage stamp to `0.0` age, so the weight falls out as `1.0` with no special-case. A
future-dated stamp (clock skew) clamps to age 0 → 1.0 — we never *over*-weight.

### `net_entrenchment` — the recency-weighted replacement for `net_sessions` AT THE GATE

```
net_entrenchment(tk, now) =
    Σ recency_weight(valid_time(s)) over support sessions
  − Σ recency_weight(valid_time(s)) over contradiction sessions
```

It reads the same **session-ID lists** the count is derived from (`sessions_seen`;
`contradiction_evidence[].session_id` — ADR-0012's in-evidence session), so it is a strict re-weighting of
the same ground truth, not a new signal. The maturity gate (`current_takeaways`) moves from `net_sessions >=
MATURITY_SESSIONS` to **`net_entrenchment >= MATURITY_WEIGHT`** (a float bar). `review.importance` becomes
`net_entrenchment × confidence`, so the **review queue's order respects recency for free**, and the WEAKEN
belief-change (ADR-0012) is **recency-aware for free** because the contradiction side is weighted by the same
curve: a fresh contradiction subtracts ~1.0, a two-year-old one ~0.06.

`net_sessions` (the raw integer count) is **kept** — for audit, and as the human-legible "sessions to go"
shortfall in `incubating.needs`. The gate reads the weight; the count stays as the legible companion.

### Recompute-on-read from session valid-times — never stored (the facet ethos)

A session's valid-time is its **raw transcript's `origin_ref.mtime`** (the file's mtime `tap.read_origin`
already stamps = when the conversation happened). The lookup is **recompute-on-read** (ADR-0013): a takeaway
already lists its support/contradiction **session ids**, and `_session_valid_times` resolves each
`session_id → latest raw transcript blob → origin_ref.mtime` in one scan over the transcript metas — exactly
as `concepts._cleaned_facets` recomputes facets from evidence. **No date is written onto the takeaway.** This
is the load-bearing design choice: a stored date would desync the instant a transcript is re-tapped or a
clock corrected; deriving it keeps a single source of truth (the transcript meta) and means the weighting is
always evaluated against *current* knowledge of when things happened. A transcript appended-to has multiple
versions → keep the latest version's mtime (the same recency fold `latest_version` does). The map is built
**once per gate pass** and threaded into `net_entrenchment`/`importance`, so the sort pays the scan once, not
per takeaway. A session with no transcript / no mtime → weight 1.0 (fresh), per above.

### The self-sorting consequence — no timeless-vs-changing classification

The elegant payoff: we **do not** need to classify a learning as "timeless preference" vs "changeable fact."
A still-true preference keeps getting **RE-LIVED** — recent sessions re-corroborate it, sustaining its weight
above the bar. A moved-on fact simply **stops being re-evidenced**; its newest support recedes past the
half-life and its weight **fades on its own**. The same curve that discounts a stale backfill also lets a
genuinely-durable belief stay mature *as long as it keeps showing up*. Disuse is the signal; no taxonomy, no
per-takeaway flag.

### The two dials are UNTUNED — flagged at their definitions

- **`RECENCY_HALF_LIFE_DAYS = 180`** — "how fast does the world change": the age at which evidence is worth
  HALF a fresh piece. A months scale (~6mo) so a year-old corroboration counts ~¼, a two-year-old ~1⁄16. Too
  short and durable preferences churn out; too long and the backfill problem returns.
- **`MATURITY_WEIGHT = 1.5`** — the float review bar. Chosen to sit in the **integer gap**
  `[MATURITY_SESSIONS−1, MATURITY_SESSIONS)`: with FRESH evidence every weight ≈ 1, so `net_entrenchment` is
  integer-valued and `>= 1.5` is **byte-identical to the old `net_sessions >= 2`** — today's graduations are
  preserved exactly — yet it leaves headroom so *mildly*-aged-but-recent evidence (two sessions a few weeks
  old) still matures rather than falling under a rigid `2.0`.

Both want a **gold set** to fit against; each is a single module-level constant flagged UNTUNED at its
definition, retuned in one edit — the same posture as every weight in dream (ADR-0010 §8) and `AGING_LAMBDA`
(ADR-0021).

## Consequences

- **Good:** a months-long backfill can neither **re-entrench** a stale takeaway (old-only corroboration
  decays below the bar) nor **overturn** a current one (an aged contradiction barely subtracts) — pinned by
  `test_recency` (a) and (b). Back-compat is provable and tested (d): fresh evidence makes `net_entrenchment
  == net_sessions`, and the weighted gate graduates *exactly* the count gate's set, so every prior
  `test_dream`/`test_review`/`test_weaken` suite stays green **unmodified** (the only edit was two direct
  `importance()` count-dicts in `test_operability`, given `sessions_seen` lists so the fresh weight reproduces
  their old value — a shape fix, not a logic change). Recompute-on-read keeps the facet ethos: no stored date
  to desync. The weighting is recall-first (discount, never delete) and deterministic (injectable `now`).
- **Costs / known limits:** both dials are UNTUNED, awaiting a gold set — ship as operability knobs, not
  fitted defaults. Valid-time is the transcript's **mtime**, a good-but-imperfect proxy for "when the
  conversation happened" (a touched/copied file skews it; the same proxy caveat Aging notes for `fetched_at`).
  The gate now reads a session-ID list rather than a count, so a *malformed* takeaway with `support.sessions`
  set but `sessions_seen` empty reads as net 0 (real takeaways always carry both consistently —
  `support.sessions == len(set(sessions_seen))`). `incubating.needs` stays an integer session-shortfall on
  `net_sessions`, so a takeaway held below the bar **only by aged evidence** can read `needs 0` yet incubate;
  the honest signal there is "needs RECENT corroboration", which the weighted gate enforces even though the
  count display can't express it. The gate uses the weighted net **alone** — no raw-count floor — a
  deliberate simplification flagged for a second opinion (a hybrid `net_sessions >= k AND net_entrenchment >=
  w` would refuse to graduate on weight from a single re-lived session, but adds a second dial; deferred).

### The third temporal feature

ratchet now has three orthogonal uses of time, each on a different axis, and naming them keeps them from
colliding:

- **Aging** (ADR-0021) ages the BACKLOG *up* — transaction-time (`fetched_at`) raises a waiting item's queue
  *priority* so the long tail can't starve.
- **Recency-trust** (this ADR) weights EVIDENCE *down* — valid-time (session `mtime`) discounts old
  corroboration/contradiction at the *gate*.
- **Decay** (TODO) would flag QUIET concepts — a concept no recent session re-touches is a candidate for the
  gardener's attention (retire/refresh), the concept-layer analogue of this takeaway-layer fade.

Aging is about *fairness of attention*, recency-trust about *trust in a belief*, decay about *liveness of a
concept*. They share the `age_days` primitive (now `age_days(stamp, now)`) and nothing else.

## References

ADR-0012 (net entrenchment — the count this re-weights; the WEAKEN path now recency-aware for free). ADR-0013
(recompute-on-read — the facet ethos this follows: derive the date, never store it). ADR-0021 (Aging — the
sibling temporal feature on transaction-time, and the `age_days`/`fetched_at` primitive this extends with an
injectable `now`). ADR-0005 (recall-first — the false-negative is the costly error, so undateable evidence is
treated as fresh). ADR-0007 (`origin_ref`/`fetched_at`, the blob meta the valid-time is read from). ADR-0010
§8 (dream's untuned weights — the same pending-gold-set posture). Bi-temporal modeling (valid-time vs
transaction-time — Snodgrass). The acceptance test `tests/test_recency.py` (the decay curve; the two
backfill-pollution properties; the fresh-evidence back-compat to the count gate).
