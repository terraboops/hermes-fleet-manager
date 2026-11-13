# Laya parity fixtures

Cases for checking that a scoring backend reproduces the recorded MLX gate decisions.
Rebuild: `scripts/build_laya_parity_fixtures.py` (add `--check` to verify without writing).

## v1 is frozen

`v1/` never changes. The build is pinned to a cutoff timestamp (`cutoff_at` in the manifest)
because the source log is append-only. Any change to selection, tags or fields goes in `v2/`.
`manifest.json` carries `sha256_of_fixtures`; `source_row_count` reflects the log size on the
build day and will differ on a later rebuild, the fixtures hash will not.

## DEVIATIONS FROM THE REQUESTED CONTRACT (read first)

1. `text` is NOT the exact scorer input. The scorer receives `DOCTRINE + Situation: <context> +
   Event: <text> + "Events of this kind ... in the last 24 hours: N"` (laya_gate.py `classify`).
   The log stores only the Event string. The context is not logged, so the full input cannot be
   rebuilt. Only its length (`state_chars`) and N (`prior_24h`) survive. Both are added as tags
   (`state_chars:803`, `prior_24h:4`). A harness that scores `text` alone will NOT match the
   recorded scores, and a gap does not by itself mean the backend is wrong. Same text with
   different context gives different scores. To get real parity, log the full `state` (or its
   hash plus the text) going forward and cut a v2.
2. Only 307 rows qualify, not 1,854+. The log has 4,376 rows; 307 are lane-3 `laya` escalate
   scores. The rest are structural lanes (no score) or `respond` rows (a different question:
   top-class probability, threshold 0.55). Respond rows are excluded.
3. No identical-text dedup. Only 149 distinct texts exist (all `STALL`), and the same text
   legitimately carries different scores. Dedup is on (text, score, prior_24h, state_chars):
   0 rows removed.
4. Extra tags `prior_24h:N` and `state_chars:N`; extra manifest keys (`cutoff_at`,
   `scored_rows_in_cutoff`, `duplicates_removed`, `tag_counts`). Core fields are unchanged.
5. Coverage is thin: every case is a STALL event from 5 sessions. No other event family was
   ever scored by this lane.

## Schema findings (laya-gate-decisions.jsonl)

- Row: `id, at (epoch s), session, kind, text, expecting, verdict{...}`.
- Scored rows: `verdict.rule == "laya"`, `verdict.confidence` (float, rounded to 4 dp, the MLX
  P("interrupt her now")), `verdict.escalate` (decision), `ms`, `state_chars`, `family`,
  `prior_24h`, `enforced` (always false: shadow mode), `lane` 3.
- `kind` is empty on all scored rows; no model or version id is logged.

## Decision rule

`score >= 0.59` means pass (escalate/interrupt). Code: `ESCALATE_IF_AT_LEAST = 0.59`,
`conf >= ESCALATE_IF_AT_LEAST`. In the data the highest non-escalated score is 0.5886 and the
lowest escalated is 0.5912, so `>=` versus `>` cannot be told apart from the log (no score equals
0.59). Direction is pass-when-high. Inconsistent rows: 0 of 307. Scores are 4 dp, so a backend
inside 0.0001 of the threshold is noise.

## Privacy

Committed. Scored texts hold a fixed template, a neutral alias in place of the fleet session id
and an elapsed seconds count; no operator text. The session ids in the original log were replaced
by aliases (`STALL-CC-<A>-<id>`), which is the one change to the frozen v1 text. The `mlx_score`
in `v1/` is still the score the gate logged for the ORIGINAL text; the MLX reference scores in
`docs/laya-parity/baseline-mlx-v1.json` and `v1-mlxref/` were recomputed on the aliased text
(max shift 0.052). The repo gitignores `evals/laya-evals.jsonl` because that data holds transcripts and
rulings, which these rows do not. Excluded: all `respond` rows.

## Counts by tag (307 cases)

    high 7 | low 300 | near_threshold 110 (7 pass, 103 fail; |score-0.59| <= 0.05)
    inconsistent 0
