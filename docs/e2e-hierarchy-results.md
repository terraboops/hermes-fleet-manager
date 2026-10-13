# Hierarchy end-to-end results

Run: `python3 tests/test_e2e_hierarchy.py` (27 scenarios, prints the table below; also found by
`python3 -m unittest discover -s tests -t .`).

Real: fleet_contract, fleet_governance, fleet_manager, and JSONL transcript files on disk.
Simulated: the implementer sessions, the clock, and the chief-of-staff entry point (no production one exists).

## Cases and observed outcomes

| Case | Observed |
|---|---|
| Happy path | 3 contracts delivered as user turns, 3 tokens verified from child transcripts, manager done token, 1 report with 3 transcript pointers |
| Forged token, never emitted | refused: token not emitted by the child session |
| Token only in the directive | refused: the directive is a user turn, not an emission |
| Cross-chain token (child A emits sibling B's token) | refused: token does not belong to this contract |
| Token from another goal; flat-protocol token | refused: token does not belong to this contract |
| Second result for a settled contract | refused: duplicate |
| Child claims done, acceptance exit=1 | manager done withheld; result is the manager's BLOCKED token |
| Done token with a fail verdict | contradicted |
| Child blocked | manager BLOCKED token; 1 human report names the child |
| Stalled child | check-in issued by the manager, delivered as a user turn, not repeated in the window; daemon defers only while the manager is alive |
| Dead child | manager escalates; nothing sent; 1 stopped-over-10-min event reaches the human |
| Undelivered marker (queued record) | refused: directive not proven delivered |
| Assistant echo of the marker | not counted as delivery |
| Self-approval (raiser, its manager) | SelfApprovalError; gate stays pending |
| Over-budget goal (3rd dispatch on 2 units) | BudgetExhausted; no node, contract or dispatch created |
| Depth 3 (implementer spawn, implementer->manager, depth-2 manager dispatch, 3-node chain) | DepthCapExceeded / ContractError |
| Reviewer is the producer | ReviewerNotIndependent; contract unsettled; ReviewGate refuses too |
| Out-of-scope work; boundary wider than manager | OutOfScope (flagged, no budget spent); ContractError |
| Classifier block, implementer -> manager -> chief | delivered to the human once; absorbed by no level; impl, manager, chief and reviewer approvals all refused; only the human resolves |
| Classifier block at 02:00 | held, not auto-resolved; delivered at 09:00 |
| Receipt | absorbed at first level, never reaches the human |
| Lossy summary (child says pass, transcript exit=1) | reviewer rejects from ground truth; report's pointer leads to the transcript showing it |
| Lossy summary (pointer redirected to a clean transcript) | refused: pointer is not the child's own transcript |
| Spot-check sampling | never empty; at rate 0.2 a given child was missed on 43 of 50 seeds |

## Mutation check
Disabling each of these makes the suite fail: human-only gate check, delivery check, emission check,
budget check, reviewer-independence check in ReviewGate.

## Gaps (filed)
- t_727b1e1d no production chief-of-staff entry point
- t_b489d402 manager does not escalate blocked/failed children; its own token is never emitted in a transcript
- t_be81f4b1 CLOSED at the library level: scripts/fleet_review.py (see "Review policy" below). Still not wired to a live manager.
- t_2d816936 director layer needs: budget roll-up, standing rulings, depth 3, boundary arbitration, shared reviewer pool
- t_119b3bf4 nothing is wired, committed or live; all effects are injected fakes

## Review policy (t_be81f4b1)
Decision: verify every child cheaply, sample only the expensive check.
- Tier 1, every child: TranscriptReviewer (plain code) compares the report with the child's own
  transcript: pointer is that transcript, done token emitted by the assistant, last test run did
  not fail, no edit after it. FAIL rejects; a sample never outvotes a contradiction.
- Tier 2, deep reviewer: mandatory when tier 1 is INCONCLUSIVE (no test evidence); else runs on a
  spot_check sample. No deep reviewer configured means INCONCLUSIVE is rejected.
- Each verdict goes through a per-child ReviewGate; independence checks session AND model
  (make_verify models=..., strict unless allow_same_model).
- Seeds 0..49, rate 0.2, one lying child of 5: sampling alone missed it on most seeds; the tiered
  verifier missed it on 0 (tests/test_fleet_review.py).
- Ledger "sibling chain" now means another child of the same manager; "foreign chain" is a chain
  outside it; "manager's own token" is the manager's token offered for a child.
- Limit: a Bash command that edits files is not tracked by tier 1; the sampled deep review covers it.
