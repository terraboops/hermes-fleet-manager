#!/usr/bin/env python3
"""Review of child results: a transcript-reading reviewer and a tiered verifier.

Spec: docs/agent-hierarchy.md section 5 (REVIEW). Builds on fleet_contract (Ledger,
Report) and fleet_governance (ReviewGate, spot_check, make_verify). Additive.

POLICY (decided here, enforced in code, tested in tests/test_fleet_review.py)

  Sampling alone does not catch a misreport: at rate 0.2 a lying child slipped through
  on 43 of 50 seeds. So the check is two tiers.

  TIER 1  Every child, no exception. TranscriptReviewer reads the child's own transcript
          and compares it with the report. It is plain code: no model call, no cost
          worth sampling away. It returns PASS, FAIL or INCONCLUSIVE.
  TIER 2  A `deep` reviewer (an independent session, or a model) that costs real
          tokens. It is mandatory when tier 1 is INCONCLUSIVE, and runs on a
          spot_check sample of the PASS cases to catch what a plain reader cannot.

  Outcomes:  tier 1 FAIL -> rejected (a contradiction is never outvoted by a sample).
             tier 1 INCONCLUSIVE -> deep decides; no deep reviewer -> rejected.
             tier 1 PASS, in the sample -> deep must also pass.
             tier 1 PASS, not in the sample -> accepted.
  Every verdict goes through a per-child ReviewGate, so the done token for that child
  exists only after an independent pass.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import fleet_contract as fc
import fleet_governance as fg

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"

TEST_CMD_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|ctest)\b"
    r"|\b(npm|yarn|pnpm)\s+(run\s+)?test\b"
    r"|\b(cargo|go)\s+test\b"
    r"|\bmake\s+(test|check)\b"
    r"|\bpython3?\s+\S*tests?/\S+\.py\b")
FAIL_TEXT_RE = re.compile(
    r"\b[1-9]\d*\s+(failed|failures?|errors?)\b|\bFAILED\b|\bTraceback \(most recent")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


@dataclass(frozen=True)
class Finding:
    outcome: str            # PASS | FAIL | INCONCLUSIVE
    reasons: tuple = ()

    @property
    def note(self):
        return "; ".join(self.reasons)[:fc.MAX_VERDICT_CHARS]


# ---------------------------------------------------------------- transcript reading

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def timeline(path):
    """Ordered events from a transcript: ('test', cmd, failed) / ('edit', file).

    A test event is a tool_use whose command looks like a test run, joined by
    tool_use_id to its tool_result. A run with no result yet is skipped: no evidence.
    """
    uses, events = {}, []
    for obj in fc._records(path):
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and msg.get("role") == "assistant":
                name, inp = b.get("name"), b.get("input") or {}
                if name in EDIT_TOOLS:
                    events.append(("edit", inp.get("file_path") or inp.get("path") or name))
                elif name == "Bash" and TEST_CMD_RE.search(inp.get("command") or ""):
                    uses[b.get("id")] = inp.get("command")
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                text = _text_of(b.get("content"))
                failed = bool(b.get("is_error")) or bool(FAIL_TEXT_RE.search(text))
                events.append(("test", uses[b["tool_use_id"]], failed))
    return events


class TranscriptReviewer(object):
    """Tier 1. Compares a child's report with what its transcript shows.

    Checks, in order; the first failing check is the verdict:
      1. the report pointer is the child's OWN transcript (not another session's)
      2. the done token was emitted by the assistant, alone on a line
      3. a test run exists in the transcript (else INCONCLUSIVE: no evidence)
      4. the LAST test run did not fail (the report said pass, the transcript says not)
      5. no file was edited after that last run (the evidence is stale)
    A command that mutates files through Bash is not tracked; deep review covers that.
    """

    def __init__(self, transcript_of: Callable):
        self.transcript_of = transcript_of

    def review(self, child, report) -> Finding:
        path = self.transcript_of(child.owner_session)
        if not path or not os.path.exists(path):
            return Finding(INCONCLUSIVE, ("no transcript for the child",))
        pointer = report.pointer
        if not pointer.startswith("transcript:") or \
                os.path.realpath(pointer[len("transcript:"):]) != os.path.realpath(path):
            return Finding(FAIL, ("pointer is not the child's own transcript",))
        if fc.emitted_by_assistant(path, child.done_token) < 1:
            return Finding(FAIL, ("done token not emitted by the child",))
        events = timeline(path)
        tests = [i for i, e in enumerate(events) if e[0] == "test"]
        if not tests:
            return Finding(INCONCLUSIVE, ("no test run in the transcript",))
        last = tests[-1]
        if events[last][2]:
            return Finding(FAIL, ("report says pass; last test run failed",))
        edits = [e[1] for e in events[last + 1:] if e[0] == "edit"]
        if edits:
            return Finding(FAIL, ("edited after the last passing run: %s" % edits[0],))
        return Finding(PASS, ("last test run passed, nothing edited after",))

    def __call__(self, child, report):
        return self.review(child, report)


# ---------------------------------------------------------------- tiered verification

@dataclass
class TieredVerifier(object):
    """A Ledger.verify callable: tier 1 on every child, tier 2 where policy demands.

      reviewer_session   the independent session whose verdicts these are
      producer_of        child -> producing session
      universe           () -> all child contract ids; the sample is drawn from it once
      tier1              child, report -> Finding
      deep               child, report -> bool, optional
      rate / seed        spot_check parameters for the PASS cases
      models             optional (reviewer_model, model_of) for the independence check
    """
    reviewer_session: str
    producer_of: Callable
    universe: Callable
    tier1: Callable
    deep: Optional[Callable] = None
    rate: float = 0.3
    seed: Optional[int] = None
    models: Optional[tuple] = None
    allow_same_model: bool = False
    gates: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    _sample: Optional[list] = None

    def sample(self):
        if self._sample is None:
            self._sample = fg.spot_check(self.universe(), self.rate, self.seed)
        return self._sample

    def _independence(self, child):
        if self.producer_of(child) == self.reviewer_session:
            raise fg.ReviewerNotIndependent(
                "reviewer %r produced %s" % (self.reviewer_session, child.contract_id))
        if self.models:
            rmodel, model_of = self.models
            if model_of(child) == rmodel:
                if not self.allow_same_model:
                    raise fg.ReviewerNotIndependent(
                        "reviewer and producer of %s run the same model %r"
                        % (child.contract_id, rmodel))
                self.log.append((child.contract_id, "same-model", rmodel))

    def __call__(self, child, report):
        self._independence(child)
        cid = child.contract_id
        gate = self.gates.setdefault(
            cid, fg.ReviewGate(cid, self.producer_of(child), child.done_token))
        f = self.tier1(child, report)
        ran_deep, passed = False, None
        if f.outcome == FAIL:
            passed, why = False, "tier 1: " + f.note
        else:
            # With no deep reviewer there is nothing to sample; only INCONCLUSIVE needs one.
            need_deep = f.outcome == INCONCLUSIVE or \
                (self.deep is not None and cid in self.sample())
            if not need_deep:
                passed, why = True, "tier 1: " + f.note
            elif self.deep is None:
                passed, why = False, "tier 1 inconclusive, no deep reviewer: " + f.note
            else:
                ran_deep = True
                passed = bool(self.deep(child, report))
                why = "deep review %s (%s)" % ("passed" if passed else "failed",
                                              "inconclusive" if f.outcome == INCONCLUSIVE
                                              else "sampled")
        gate.submit(fg.Verdict(cid, self.reviewer_session, passed, why[:fc.MAX_VERDICT_CHARS]))
        self.log.append((cid, f.outcome, ran_deep, passed))
        return gate.done_token() is not None


def build_verify(reviewer_session, producer_of, transcript_of, universe, deep=None,
                 rate=0.3, seed=None, models=None, allow_same_model=False):
    """The production Ledger.verify: transcript reader on every child, deep on a sample."""
    return TieredVerifier(reviewer_session, producer_of, universe,
                          TranscriptReviewer(transcript_of), deep, rate, seed, models,
                          allow_same_model)
