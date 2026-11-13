# Codex startup update protocol

The owner has authorized always applying recognized official Codex CLI updates. This is a standing authorization for the updater alone. Hook review, sign-in, unknown menus, and unapproved trust decisions are not updater authorization. Personal profiles remain excluded from work-fleet automation.

`fleet_boot.codex_update_auto_allowed` recognizes the official npm menu by its complete three rows, selected label, increasing semantic version, official release-notes URL, and exact install command annotation. `auto_resolve_codex_update` re-reads it before acting; moves Up at most twice with verification after each move; sends one Enter only on Update now. It never executes pane text as a shell command.

Before confirmation the session needs an authoritative native UUID and a resolvable transcript. Wait up to 120 seconds for the updater process/pane to exit, then verify the configured executable using `--version` through the same login-shell PATH. Require an installed version at least as new as the offered target. Resume that exact entry/UUID with the harness. A successful updater return means relaunched, not READY. Boot polling requires two usable-TUI observations with no intervening choice screen. Live reconcile persists its attempt fingerprint before sending confirmation to avoid repeating an uncertain update on a later pass. Failed installation, version checks, ambiguous selection, unknown screens and resume timeout are reported, never converted to READY.

The provider override is a launch-protocol boundary: a Codex entry under a legacy Claude work profile must not inherit Claude's executable, `--remote-control`, environment, home, or resume flag.

A newly spawned handoff without an authoritative native UUID is deliberately not relaunched as a fresh conversation after an update. Preserve the pending handoff/context for identity resolution; the boot updater handles existing registered native sessions. Source routing remains unchanged until live recovery, rollback and monitor gates pass.

Tests: `python3 -m unittest discover -s tests -p test_codex_update.py -v`.
