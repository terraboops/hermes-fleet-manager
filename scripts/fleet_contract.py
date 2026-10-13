#!/usr/bin/env python3
"""Nested contracts and chain-namespaced tokens for the fleet hierarchy.

Spec: docs/agent-hierarchy.md sections 3, 4.2 and 5. This module is additive. The flat
path (fleet_ack, fleet_dispatch) does not import it and is unchanged; a flat token such
as DONE-fw-ow-1790025010 may parse here, but it matches no registered contract, so a
ledger never accepts it.

Rules enforced in code, each with a test in tests/test_fleet_contract.py:

  1. TOKEN SHAPE   <KIND>-<goal>-<chain>-<seq>, chain = node ids joined by '-',
                   e.g. DONE-g41-m2-i3-1. Tokens are derived from the contract, never typed.
  2. NAMESPACING   A ledger accepts a token only for the exact contract that owns it, so a
                   sibling's token, a parent's token, or a lookalike is rejected.
  3. EMISSION      A token counts only as an ASSISTANT line, alone on the line, in the
                   transcript of the session the contract was given to. The same string
                   inside the directive (a user turn) is the instruction, not the result.
  4. DELIVERY      A marker is delivered only when it is a real user turn. A paste
                   visible on screen, a queued record or an assistant echo is not.
  5. NO SAY-SO     A manager's done token exists only after every child contract has an
                   emitted token AND a passing verification from a reviewer callable.
  6. POINTERS      A report upward is a pointer plus a short verdict, never a body.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable

DONE = "DONE"
BLOCKED = "BLOCKED"
KINDS = (DONE, BLOCKED)

NODE_RE = re.compile(r"^[a-z][a-z0-9]*$")
GOAL_RE = re.compile(r"^[a-z][a-z0-9]*$")
TOKEN_RE = re.compile(r"^(DONE|BLOCKED)-([a-z][a-z0-9]*)-((?:[a-z][a-z0-9]*-)+)(\d+)$")

MAX_DEPTH = 2
MAX_VERDICT_CHARS = 120
VERDICTS = ("pass", "fail", "blocked")
POINTER_RE = re.compile(r"^(transcript|artifact):\S+$")


class ContractError(ValueError):
    """A contract or report that must be refused."""


# ---------------------------------------------------------------- tokens

def make_token(kind, goal, chain, seq=1):
    """Build a token. `chain` is a list of node ids from the goal down to the owner."""
    if kind not in KINDS:
        raise ContractError("unknown token kind: %r" % (kind,))
    if not GOAL_RE.match(goal or ""):
        raise ContractError("bad goal short id: %r" % (goal,))
    if not chain or not all(NODE_RE.match(n or "") for n in chain):
        raise ContractError("bad chain: %r" % (chain,))
    if not isinstance(seq, int) or seq < 1:
        raise ContractError("bad seq: %r" % (seq,))
    return "%s-%s-%s-%d" % (kind, goal, "-".join(chain), seq)


def parse_token(text):
    """(kind, goal, chain tuple, seq) for a well-formed token, else None."""
    m = TOKEN_RE.match((text or "").strip())
    if not m:
        return None
    kind, goal, chain_s, seq = m.groups()
    return kind, goal, tuple(chain_s.rstrip("-").split("-")), int(seq)


def chain_prefix(chain_a, chain_b):
    """True if chain_a is chain_b or an ancestor of it."""
    return tuple(chain_b[:len(chain_a)]) == tuple(chain_a)


# ---------------------------------------------------------------- contract

@dataclass(frozen=True)
class Contract:
    contract_id: str
    goal: str
    boundary: tuple
    acceptance: tuple          # each: {"check": str, "positive": [..], "negative": [..]}
    done_token: str
    blocked_token: str
    deadline: float            # absolute epoch seconds
    parent_contract_id: object  # str | None
    chain: tuple               # chain namespace, goal-relative node ids
    goal_short: str = ""
    owner_session: str = ""    # the session this contract was given to

    @property
    def depth(self):
        return len(self.chain)


def _subset(child_paths, parent_paths):
    """Every child boundary entry lies under some parent boundary entry."""
    def under(c, p):
        c, p = c.rstrip("/"), p.rstrip("/")
        return c == p or c.startswith(p + "/")
    return all(any(under(c, p) for p in parent_paths) for c in child_paths)


def new_contract(goal_short, chain, goal, boundary, acceptance, deadline,
                 owner_session="", parent=None, seq=1, max_depth=MAX_DEPTH):
    """Create a contract. Raises ContractError for anything the spec forbids.

    The contract id IS the done token's stem: DONE/BLOCKED tokens are derived from the
    same goal and chain, so they cannot be edited apart from it.
    """
    chain = tuple(chain)
    if len(chain) > max_depth:
        raise ContractError("depth %d exceeds cap %d" % (len(chain), max_depth))
    if not (goal or "").strip():
        raise ContractError("goal is empty")
    boundary = tuple(boundary or ())
    if not boundary:
        raise ContractError("boundary is empty")
    acc = []
    for a in acceptance or ():
        if not (a.get("check") or "").strip():
            raise ContractError("acceptance entry has no check")
        if not a.get("positive"):
            raise ContractError("acceptance %r has no positive case" % a["check"])
        if not a.get("negative"):
            raise ContractError("acceptance %r has no negative case" % a["check"])
        acc.append({"check": a["check"], "positive": list(a["positive"]),
                    "negative": list(a["negative"])})
    if not acc:
        raise ContractError("no acceptance criteria")
    if parent is not None:
        if not chain_prefix(parent.chain, chain) or len(chain) != len(parent.chain) + 1:
            raise ContractError("chain %r is not a direct child of %r"
                                % (chain, parent.chain))
        if parent.goal_short != goal_short:
            raise ContractError("child goal differs from parent goal")
        if not _subset(boundary, parent.boundary):
            raise ContractError("boundary is not a subset of the parent's")
    elif len(chain) != 1:
        raise ContractError("a contract with no parent must have a one-node chain")
    done = make_token(DONE, goal_short, chain, seq)
    blocked = make_token(BLOCKED, goal_short, chain, seq)
    return Contract(
        contract_id="c-%s-%s-%d" % (goal_short, "-".join(chain), seq),
        goal=goal.strip(), boundary=boundary, acceptance=tuple(
            _freeze(a) for a in acc),
        done_token=done, blocked_token=blocked, deadline=float(deadline),
        parent_contract_id=parent.contract_id if parent else None,
        chain=chain, goal_short=goal_short, owner_session=owner_session)


def _freeze(a):
    return {"check": a["check"], "positive": tuple(a["positive"]),
            "negative": tuple(a["negative"])}


# ---------------------------------------------------------------- transcript proofs

def _records(path):
    with open(path, errors="replace") as fh:
        for ln in fh:
            try:
                yield json.loads(ln)
            except ValueError:
                continue


def _blocks(obj):
    """(role, [text strings]) for a transcript record. tool_result blocks are skipped."""
    msg = obj.get("message") or {}
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, str):
        return role, [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
    return role, out


def delivered_as_user_turn(path, marker):
    """True only if `marker` is inside a real user turn of the transcript at `path`.

    Not counted: queue-operation or attachment records (queued, not a turn), assistant
    text quoting the marker, tool results, or a path that cannot be read. Unreadable is
    False here, not unknown: this is a proof, and an absent proof is a refusal. The flat
    path's lenient fleet_ack.delivered is left alone for flat sessions.
    """
    if not marker or not path or not os.path.exists(path):
        return False
    for obj in _records(path):
        if obj.get("type") == "queue-operation":
            continue
        role, texts = _blocks(obj)
        if role == "user" and any(marker in t for t in texts):
            return True
    return False


def emitted_by_assistant(path, token):
    """Number of assistant turns that carry `token` ALONE on a line."""
    if not token or not path or not os.path.exists(path):
        return 0
    n = 0
    for obj in _records(path):
        role, texts = _blocks(obj)
        if role != "assistant":
            continue
        if any(token == ln.strip() for t in texts for ln in t.splitlines()):
            n += 1
    return n


# ---------------------------------------------------------------- reports

@dataclass(frozen=True)
class Report:
    contract_id: str
    pointer: str
    verdict: str
    note: str = ""

    def __post_init__(self):
        if not POINTER_RE.match(self.pointer or ""):
            raise ContractError("pointer must be transcript:<path> or artifact:<ref>")
        if self.verdict not in VERDICTS:
            raise ContractError("verdict must be one of %s" % (VERDICTS,))
        if len(self.note) > MAX_VERDICT_CHARS or "\n" in self.note:
            raise ContractError("note is a short label, not a body (max %d chars, one line)"
                                % MAX_VERDICT_CHARS)


# ---------------------------------------------------------------- ledger

@dataclass
class Ledger:
    """A manager's view of its own contract and its children's.

    `transcript_of(session)` -> path or None; injected so tests use fixture files and
    production passes fleet_transcript.resolve. `verify(child, report)` -> bool is the
    independent reviewer; a child's own verdict is recorded but never trusted.
    """
    own: Contract
    transcript_of: Callable
    verify: Callable
    children: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)

    def add_child(self, c):
        if c.contract_id in self.children:
            raise ContractError("duplicate contract id %s" % c.contract_id)
        if c.parent_contract_id != self.own.contract_id:
            raise ContractError("not a child of this manager")
        if any(x.done_token == c.done_token or x.blocked_token == c.blocked_token
               for x in self.children.values()):
            raise ContractError("token collision")
        if not c.owner_session:
            raise ContractError("child contract has no owner session")
        self.children[c.contract_id] = c
        self.state[c.contract_id] = {"delivered": False, "token": None, "verified": None,
                                     "report": None, "why": "not delivered"}

    # -- hop 1: down. The directive must be a real user turn in the child.
    def confirm_delivery(self, contract_id, marker):
        c = self._child(contract_id)
        path = self.transcript_of(c.owner_session)
        ok = delivered_as_user_turn(path, marker)
        st = self.state[contract_id]
        st["delivered"] = ok
        st["why"] = "delivered" if ok else "marker is not a user turn in the child"
        return ok

    # -- hop 2: up. A token is read from the child's own transcript, nowhere else.
    def receive(self, contract_id, token, report):
        """Offer a token and report for a child. Returns (accepted, reason)."""
        c = self._child(contract_id)
        st = self.state[contract_id]
        if st["token"] is not None:
            return False, "duplicate: contract already settled"
        if not st["delivered"]:
            return False, "directive not proven delivered"
        if report.contract_id != contract_id:
            return False, "report is for another contract"
        parsed = parse_token(token)
        if parsed is None:
            return False, "malformed token"
        if token not in (c.done_token, c.blocked_token):
            if parsed[1] == self.own.goal_short:
                if chain_prefix(self.own.chain, parsed[2]):
                    if tuple(parsed[2]) == tuple(self.own.chain):
                        return False, "manager's own token"
                    # same manager, another child's contract
                    return False, "sibling chain"
                return False, "foreign chain"
            return False, "token does not belong to this contract"
        path = self.transcript_of(c.owner_session)
        if emitted_by_assistant(path, token) < 1:
            return False, "token not emitted by the child session"
        if token == c.blocked_token:
            st.update(token=token, verified=False, report=report, why="child blocked")
            return True, "blocked"
        if report.verdict != "pass":
            st.update(token=token, verified=False, report=report,
                      why="child verdict %s contradicts done token" % report.verdict)
            return True, "contradicted"
        ok = bool(self.verify(c, report))
        st.update(token=token, verified=ok, report=report,
                  why="verified" if ok else "failed acceptance")
        return True, st["why"]

    def missing(self):
        """Child ids that do not yet have a verified done token."""
        return [cid for cid, st in self.state.items() if st["verified"] is not True]

    def blocked_children(self):
        return [cid for cid, st in self.state.items()
                if st["token"] == self.children[cid].blocked_token]

    def done_token(self):
        """The manager's own done token, or None. No children means no basis: None."""
        if not self.children or self.missing():
            return None
        return self.own.done_token

    def blocked_token(self):
        """The manager's blocked token once any child is blocked or failed; else None."""
        bad = [cid for cid, st in self.state.items()
               if st["token"] is not None and st["verified"] is False]
        return self.own.blocked_token if bad else None

    def upward_report(self):
        """What goes to the parent: a pointer plus a short verdict. Never a body."""
        tok = self.done_token()
        if tok:
            return tok, Report(self.own.contract_id, "artifact:ledger/%s" % self.own.contract_id,
                               "pass", "%d children verified" % len(self.children))
        btok = self.blocked_token()
        if btok:
            return btok, Report(self.own.contract_id, "artifact:ledger/%s" % self.own.contract_id,
                                "blocked", "children blocked: %s" % ",".join(self.blocked_children())[:80])
        return None, None

    def upward_proven(self, own_transcript_path):
        """upward_report plus the same emission proof one level up.

        The manager's token counts only if the manager's own transcript carries it as an
        assistant turn. Returns (token, report, why); token and report are None when the
        ledger has nothing to report or the manager has not emitted the token.
        """
        tok, rep = self.upward_report()
        if tok is None:
            return None, None, "no manager token derived"
        if emitted_by_assistant(own_transcript_path, tok) < 1:
            return None, None, "manager token not emitted by the manager session"
        return tok, rep, "emitted"

    def _child(self, contract_id):
        try:
            return self.children[contract_id]
        except KeyError:
            raise ContractError("unknown child contract %s" % contract_id)
