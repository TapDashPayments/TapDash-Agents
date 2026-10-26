# Acceptance: the "it ran" message survives restarts and crashes (GitHub #66)

**Result: PASSED, 2026-10-26, on the test VM `proto-agents` (QuickBooks sandbox).** Code: PR #67 (merge `d1076e7`).

In plain words: after an approved action runs, you are now always told, even if the agent restarts or crashes
before it can write its own reply.

| # | Test | Result | What happened |
|---|---|---|---|
| 1 | Normal run | PASS | P-031A05A8 ($5, payment 159) ran at 15:58:31Z. durable-park's own line `[durable-park] P-031A05A8 ran: ...` reached the Teams chat at 15:58:32Z, and the ledger marked it told. The agent's own reply came too. Nothing was sent twice. |
| 2 | Crash right after the run | PASS | With `DURABLE_PARK_CRASH_AFTER_EXECUTE=1` (a user-unit drop-in), P-E0AAB76E ($1) ran at 16:06:01Z and the gateway exited (status 70) the same second, before telling the chat. systemd restarted it at 16:06:11Z; the row was `executed` and untold. The sweep, run by hand after the 2-minute gap (16:08:19Z), woke it; the wake handler gave the agent the outcome line and marked it told; the agent said "Done — the $1 payment ... was recorded successfully" in the same chat at 16:08:33Z. Told once. The drop-in was removed and the gateway restarted clean (no crash variable, nothing untold). |
| 3 | Restart in the middle of a reply | Not run separately | `agent.restart_drain_timeout` is `45` live (it was `0`). The restarts above happened with no reply in progress. |

Before the fix (10:50 ET the same day), a restart cut off the agent's reply for P-BBD5A244 and nobody was told.

Known limits (accepted in review; see the runbook "How you hear about an approval"): a crash at exactly the wrong
instant can send the line once more; a second crash before the agent relays the sweep's re-send can lose that one
re-send. The run itself is always in the ledger.
