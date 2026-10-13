| case | verdict | checks | note |
|---|---|---|---|
| S1 (a) worker killed mid-run: crash detected, card requeued, re-run, finishes. | PASS | 6/6 | - |
| S1b (a') two kills trip the circuit breaker (failure_limit=2): card parked blocked, not lost. | PASS | 4/4 | - |
| S2 (b) gateway/dispatcher SIGKILLed mid-run; worker survives; new dispatcher must not duplicate. | PASS | 5/5 | - |
| S2b (b') dispatcher dies after the claim commit, before any worker exists. Lease must expire, then reclaim. | PASS | 5/5 | - |
| S2c (b'') pid-record window, worker does NOT self-register (what a bare worker would do). | FAIL | 2/3 | I2 no concurrent duplicate execution |
| S2d (b''') pid-record window, worker self-registers via adopt_worker_pid (the shipped mitigation). | PASS | 3/3 | - |
| S3 (c) dispatcher AND worker SIGKILLed together mid-run; DB files left as-is; recovery on restart. | PASS | 6/6 | - |
| S3b (c') writer SIGKILLed mid-transaction, repeatedly: DB opens clean, no half-written cards. | PASS | 2/2 | - |
| S4a (d) hung worker, PID alive, never heartbeated, lease expired: spec wants reclaim. | FAIL | 2/3 | stalled card reclaimed after lease expiry |
| S4b (d') worker heartbeats once then wedges: reclaimed once heartbeat age > HB max-stale (patched 4s). | PASS | 4/4 | - |
| S4c (d'') max_runtime_seconds bounds a hung worker with no heartbeat at all. | PASS | 3/3 | - |
| S5 Three dispatchers hammer one board: each card executes exactly once. | PASS | 3/3 | - |
