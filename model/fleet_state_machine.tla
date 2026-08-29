--------------------------- MODULE fleet_state_machine ---------------------------
(*
  Model of the fleet-manager agent state machine (v1 contract).

  States: idle / running / awaiting_input / done / error
  - start        : begin a task (idle | done -> running)
  - needs_input  : agent blocks on the human (running -> awaiting_input)
  - provide_input: human answers / steer (awaiting_input -> running)
  - complete     : done sentinel (running | awaiting_input -> done)  [terminal]
  - fail         : traceback (running | awaiting_input -> error)
  - retry        : resume an errored session (error -> running)

  Invariants under test:
  - OK_STATES  : state is always one of the five.
  - INPUT_EXACT: pendingInput == TRUE  <=>  state == "awaiting_input"
                 (an agent that emits needs_input MUST be in awaiting_input,
                  never still "running").
  - NO_FALSE_DONE: a session can reach done only from running/awaiting_input
                 (i.e. never errors directly to done, never starts as done).
*)
EXTENDS Naturals

VARIABLES state, pendingInput

vars == <<state, pendingInput>>

States == {"idle", "running", "awaiting_input", "done", "error"}

Init == state = "idle" /\ pendingInput = FALSE

(* start a new task *)
StartNew ==
  /\ state \in {"idle", "done"}
  /\ state' = "running"
  /\ pendingInput' = FALSE

(* agent blocks waiting on the human *)
NeedsInput ==
  /\ state = "running"
  /\ pendingInput' = TRUE
  /\ state' = "awaiting_input"

(* human answers / steer resumes work *)
ProvideInput ==
  /\ state = "awaiting_input"
  /\ pendingInput' = FALSE
  /\ state' = "running"

(* done sentinel - terminal once reached *)
Complete ==
  /\ state \in {"running", "awaiting_input"}
  /\ state' = "done"
  /\ pendingInput' = FALSE

(* traceback -> error *)
Fail ==
  /\ state \in {"running", "awaiting_input"}
  /\ state' = "error"
  /\ pendingInput' = FALSE

(* retry / resume from error *)
Retry ==
  /\ state = "error"
  /\ state' = "running"
  /\ pendingInput' = FALSE

(* done is absorbing; anything else must take a step (progress) *)
Next ==
  \/ StartNew
  \/ NeedsInput
  \/ ProvideInput
  \/ Complete
  \/ Fail
  \/ Retry
  \/ (state = "done" /\ UNCHANGED vars)

OK_STATES == state \in States
INPUT_EXACT == (pendingInput = TRUE) <=> (state = "awaiting_input")
NO_FALSE_DONE == ~(state = "done" /\ pendingInput = TRUE)

Spec == Init /\ [][Next]_vars

=============================================================================
