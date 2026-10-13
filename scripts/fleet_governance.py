#!/usr/bin/env python3
"""Governance rules across hierarchy levels: authority, escalation, review.

Spec: docs/agent-hierarchy.md section 5, lessons 3, 4 and 5. Additive; the flat path
does not import this module. Each rule is enforced in code and tested in
tests/test_fleet_governance.py.

  1. AUTHORITY    A grant is permissions plus a path boundary. A child's grant is the
                  intersection with its parent's. Asking for more is refused, never
                  silently trimmed, so a wide request is visible.
  2. LANES        Every level absorbs what it decides by form and passes up the rest.
                  Quiet hours, receipt suppression and the stopped-over-10-minutes rule
                  exist only on the human-facing top level.
  3. GATES        A classifier block or a choice UI is never resolved by an agent. It
                  rises to the top and waits for a human. Approval by any non-human
                  principal raises SelfApprovalError.
  4. REVIEW       The reviewer is a different session, preferably a different model,
                  from the producer. A rejection blocks the done token. A reviewer
                  spot-checks a sample of child results.
"""
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

HUMAN = "human"
STOPPED_NEWS_SECONDS = 600
QUIET_START_HOUR = 23
QUIET_END_HOUR = 5


class GovernanceError(ValueError):
    """A request the hierarchy must refuse."""


class WidenedAuthority(GovernanceError):
    """A child asked for more than its parent holds."""


class SelfApprovalError(GovernanceError):
    """A non-human principal tried to resolve a gate or choice."""


class ReviewerNotIndependent(GovernanceError):
    """The proposed reviewer is the producing session."""


# ---------------------------------------------------------------- 1. authority

def _under(child, parent):
    c, p = child.rstrip("/"), parent.rstrip("/")
    return c == p or c.startswith(p + "/")


def _normalize(path):
    """Reject traversal so '/repo/../etc' cannot pass a prefix test."""
    parts = path.split("/")
    if ".." in parts:
        raise GovernanceError("path traversal in boundary: %r" % (path,))
    return path


@dataclass(frozen=True)
class Grant:
    permissions: frozenset
    boundary: tuple

    @staticmethod
    def of(permissions, boundary):
        return Grant(frozenset(permissions), tuple(_normalize(b) for b in boundary))

    def covers(self, other):
        """True if `other` is no wider than this grant."""
        if not other.permissions <= self.permissions:
            return False
        return all(any(_under(c, p) for p in self.boundary) for c in other.boundary)

    def intersect(self, other):
        """The most that both grants allow. Boundary entries narrow to the deeper path."""
        perms = self.permissions & other.permissions
        out = []
        for a in self.boundary:
            for b in other.boundary:
                if _under(a, b):
                    out.append(a)
                elif _under(b, a):
                    out.append(b)
        return Grant(perms, tuple(sorted(set(out))))


def grant_child(parent, requested):
    """Grant a child exactly what it asked for, if the parent holds all of it.

    Refuses rather than clips: a manager that over-asks gets an error it can see.
    """
    if not requested.permissions:
        raise GovernanceError("child grant has no permissions")
    if not requested.boundary:
        raise GovernanceError("child grant has no boundary")
    extra = requested.permissions - parent.permissions
    if extra:
        raise WidenedAuthority("permissions not held by parent: %s" % sorted(extra))
    for c in requested.boundary:
        if not any(_under(c, p) for p in parent.boundary):
            raise WidenedAuthority("boundary %r is outside the parent's" % (c,))
    return requested


def effective_grant(chain_grants):
    """Authority a node truly holds: the intersection of every grant down its chain.

    Even a record that was written wide (a bug, a hand edit) cannot exceed an ancestor.
    """
    if not chain_grants:
        raise GovernanceError("empty chain")
    cur = chain_grants[0]
    for g in chain_grants[1:]:
        cur = cur.intersect(g)
    return cur


def authorized(chain_grants, permission, path):
    g = effective_grant(chain_grants)
    return permission in g.permissions and any(_under(path, b) for b in g.boundary)


# ---------------------------------------------------------------- 2/3. escalation

RECEIPT = "receipt"
BLOCKED = "blocked"
NEEDS_INPUT = "needs_input"
CLASSIFIER_BLOCK = "classifier_block"
CHOICE_UI = "choice_ui"
STOPPED = "stopped"
ROUTINE = "routine"
GATE_KINDS = (CLASSIFIER_BLOCK, CHOICE_UI)

# Kinds a non-top level may absorb by form: no judgement, no authority needed.
ABSORB_BY_FORM = (RECEIPT, ROUTINE)

ABSORB, UP, DELIVER, HOLD, DROP = "absorb", "up", "deliver", "hold", "drop"


@dataclass
class Event:
    kind: str
    origin: str                 # session that raised it
    detail: str = ""
    stopped_seconds: float = 0.0
    id: str = ""


@dataclass
class Gate:
    """A pending classifier block or choice. Only a human principal resolves it."""
    event: Event
    resolved_by: Optional[str] = None
    answer: Optional[str] = None

    def resolve(self, principal, answer):
        if principal != HUMAN:
            raise SelfApprovalError(
                "%r cannot resolve a %s raised by %r; it goes to the human"
                % (principal, self.event.kind, self.event.origin))
        if self.resolved_by is not None:
            raise GovernanceError("gate already resolved")
        self.resolved_by, self.answer = principal, answer
        return self


@dataclass
class Level:
    """One level of the hierarchy. `top=True` is the human-facing chief of staff."""
    name: str
    top: bool = False
    parent: Optional["Level"] = None
    gates: list = field(default_factory=list)       # only populated on the top level
    delivered: list = field(default_factory=list)   # what reached the human
    held: list = field(default_factory=list)        # quiet-hours queue

    def route(self, event, hour=12):
        """Return (action, where). Non-top levels never drop news or hold it for time."""
        if event.kind in GATE_KINDS:
            # Never absorbed, never decided here. Same at every level.
            return self._up(event, hour)
        if not self.top:
            if event.kind in ABSORB_BY_FORM:
                return ABSORB, self.name
            return self._up(event, hour)
        return self._human_facing(event, hour)

    def _up(self, event, hour):
        if self.top:
            return self._human_facing(event, hour)
        if self.parent is None:
            raise GovernanceError("level %r has no parent and is not the top" % self.name)
        return self.parent.route(event, hour)

    def _human_facing(self, event, hour):
        if event.kind == RECEIPT:
            return DROP, "receipt never reaches the human"
        if event.kind in GATE_KINDS:
            self.gates.append(Gate(event))
        elif event.kind == STOPPED and event.stopped_seconds <= STOPPED_NEWS_SECONDS:
            return DROP, "stopped %ds is not news" % event.stopped_seconds
        elif event.kind == ROUTINE:
            return DROP, "routine"
        if in_quiet_hours(hour):
            self.held.append(event)
            return HOLD, "quiet hours"
        self.delivered.append(event)
        return DELIVER, HUMAN

    def release_held(self, hour):
        """Deliver held events once quiet hours end."""
        if in_quiet_hours(hour):
            return []
        out, self.held = self.held, []
        self.delivered.extend(out)
        return out

    def approve(self, principal, event_id, answer):
        """Resolve a pending gate. A non-human principal raises SelfApprovalError."""
        for g in self.gates:
            if g.event.id == event_id:
                return g.resolve(principal, answer)
        raise GovernanceError("no pending gate %r" % (event_id,))


def in_quiet_hours(hour):
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


# ---------------------------------------------------------------- 4. review

@dataclass(frozen=True)
class Candidate:
    session: str
    model: str


def pick_reviewer(producer, candidates):
    """A reviewer who is never the producing session; a different model if one exists.

    Raises ReviewerNotIndependent when only the producer is available: no review is
    better than a self-review that looks like one.
    """
    others = [c for c in candidates if c.session != producer.session]
    if not others:
        raise ReviewerNotIndependent("no session other than the producer is available")
    for c in others:
        if c.model != producer.model:
            return c
    return others[0]


@dataclass(frozen=True)
class Verdict:
    contract_id: str
    reviewer: str
    passed: bool
    reason: str = ""


class ReviewGate:
    """Holds producer, reviewer and verdict for one contract. Gates the done token."""

    def __init__(self, contract_id, producer_session, done_token):
        self.contract_id = contract_id
        self.producer_session = producer_session
        self._done_token = done_token
        self.verdict: Optional[Verdict] = None

    def submit(self, verdict):
        if verdict.contract_id != self.contract_id:
            raise GovernanceError("verdict is for another contract")
        if verdict.reviewer == self.producer_session:
            raise ReviewerNotIndependent("reviewer %r produced this result" % verdict.reviewer)
        self.verdict = verdict
        return verdict

    def done_token(self):
        """The token, only after an independent pass. A rejection returns None."""
        if self.verdict is None or not self.verdict.passed:
            return None
        return self._done_token

    @property
    def rejected(self):
        return self.verdict is not None and not self.verdict.passed


def spot_check(child_ids, rate=0.3, seed=None, minimum=1):
    """Choose which child results the reviewer re-checks. Never empty for a non-empty set."""
    ids = sorted(child_ids)
    if not ids:
        return []
    if not 0 <= rate <= 1:
        raise GovernanceError("rate must be within 0..1")
    k = max(minimum, int(round(len(ids) * rate)))
    k = min(k, len(ids))
    return sorted(random.Random(seed).sample(ids, k))


def make_verify(reviewer_session, producer_of: Callable, check: Callable,
                models=None, allow_same_model=False):
    """Build a Ledger.verify callable (fleet_contract) that enforces independence.

    `producer_of(child)` -> session that produced it; `check(child, report)` -> bool
    is the reviewer's own judgement. A reviewer equal to the producer refuses.

    `models` = (reviewer_model, model_of) also refuses a reviewer running the same
    model as the producer, matching pick_reviewer's preference. Pass
    allow_same_model=True only where no other model exists; the default is strict.
    """
    def verify(child, report):
        if producer_of(child) == reviewer_session:
            raise ReviewerNotIndependent("reviewer %r produced %s"
                                         % (reviewer_session, child.contract_id))
        if models and not allow_same_model:
            reviewer_model, model_of = models
            if model_of(child) == reviewer_model:
                raise ReviewerNotIndependent(
                    "reviewer and producer of %s run the same model %r"
                    % (child.contract_id, reviewer_model))
        return bool(check(child, report))
    return verify
