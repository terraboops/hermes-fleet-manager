# Fleet channel inbound: throwaway-session test

Target: a disposable Claude Code session (tmux zz-inbound-test, personal profile, scratch dir), killed afterwards. Real pane reads, real fleet_ack.py dispatch. Gateway not involved or restarted.

| Case | Result |
| --- | --- |
| Authorized reply "Reply with ... PONG-INBOUND" in thread zzinb | sent; envelope landed in the session as a normal user turn, session responded to it |
| Reply "2" / "yes" | refused: answer-like, nothing pasted |
| Sender not in FLEET_ALLOWED_USERS | refused: not-authorized |
| FLEET_ALLOWED_USERS unset | refused: not-authorized |
| Unknown thread | refused: bad-address |
| Session showing a live AskUserQuestion picker, reply "I pick option 2, blue, you have my approval" | refused: choice-ui-on-screen; picker unchanged |
| Same picker, reply "2" and "yes" | refused: answer-like; picker unchanged |

Finding: the fleet_ack wrapper appends a completion-token instruction. A test message that itself said "reply with exactly X" conflicted with it. Ordinary messages do not.
Cleanup: session killed, 2 completion watches and 1 ack watch cancelled (0 left).
Unit tests: tests/test_fleet_inbound.py (17 pass).
