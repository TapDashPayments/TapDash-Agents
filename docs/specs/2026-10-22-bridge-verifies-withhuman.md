# Bridge verifies approvals in withHuman (small Phase 2, slimmed)

Date: 2026-10-22. Approved by Yaakov 2026-10-22. Replaces the "dpledger ledger service" design
(docs/specs/2026-10-21-all-tools-withhuman-design.md §4.5) and backlog item 7 for the bridge.

## Problem

The bridge (`bridge/app.py`, withHuman webhook block on the gateway's pipeline) says `approve` when the
local ledger has an `executing` park with the same tool and args. The ledger is a file the bot's Linux
user (`hermes`) can write, so anything running as `hermes` could forge a row and get a gateway call
approved without a person.

## Change

The bridge stops reading and writing the ledger. It asks withHuman directly, with the existing
organization key `/etc/durable-park/audit_key` (permissions: `audit.read`, `request.read`,
`request.read.unrouted`; it cannot approve, deny or change anything). The bridge keeps no state.

Documented facts this relies on (withHuman docs / source, read 2026-10-22):

* `GET https://app.withhuman.ai/api/v1/requests?agent_slug=&q=&limit=` lists one agent's requests,
  newest first, including automatic decisions; `q` is a case-insensitive substring of tool or instance
  name; `limit` max 200. Response `{"requests": [...]}`; each item has `id, agent_slug, tool, server,
  arguments, context, status, decision{status, decided_at, expires_at, note}, consumption{consumed_at,
  outcome}, created_at, deadline_at` (verified live 2026-10-22).
* Consume: "only one consume succeeds"; the executor consumes the park approval, then dispatches the
  call through the gateway (so the gateway request is created seconds after `consumed_at`).
* Webhook block payload (withHuman source `internal/webhookdelivery/payload.go`): `request.id` is the
  gateway request's id and is always set on real deliveries (omitted only in previews/tests).
* Webhook block timeout is 5 s (deploy/withhuman/entry-pipeline.json), up to 3 attempts.

## Decision rule (pure function, `bridge/verify.py`)

Given the verified gateway call G = (request_id, server, tool, arguments, agent_slug):

1. `park_tool = hermes_tool_name(G.server, G.tool)`; `park_server = "park:" + gateway_slug(park_tool)`;
   `digest = args_digest(G.arguments)` (ledger.args_digest: normalized, canonical JSON).
2. A park candidate P (from listing `q=park_tool`) MATCHES only if ALL hold:
   `P.agent_slug == G.agent_slug`, `P.tool == park_tool`, `P.server == park_server`,
   `P.status == "approved"`, `P.decision.status == "approved"`, `P.consumption.consumed_at` parses
   (timezone-aware ISO 8601), `consumed_at <= now + SKEW_SECONDS` and
   `now - consumed_at <= WINDOW_SECONDS`, `P.decision.expires_at` parses and `consumed_at <= expires_at`,
   `P.context` is a dict without a truthy `args_truncated`, `P.arguments` is a dict and
   `args_digest(P.arguments) == digest`.
3. Zero matches -> human (`no-approved-park`). More than one -> human (`ambiguous-park`).
4. Single use: from listing `q=G.tool`, any OTHER request O (`O.id != G.request_id`) with
   `O.agent_slug == G.agent_slug`, `O.server == G.server`, `O.tool == G.tool`, whose `created_at` is
   missing/unparseable or `>= P.consumed_at`, and whose arguments are not a dict or have the same digest
   -> human (`already-used`). Any status counts (pending, approved, denied, expired, cancelled).
5. Otherwise approve, naming P.id.

Anything unexpected (bad shapes, list failure, timeout, missing key) -> human. Never raise to the caller.
A retried delivery of the same G gets the same answer (G excludes itself).

## Interfaces (all new code; tests first)

`bridge/verify.py`
```python
WINDOW_SECONDS = 120
SKEW_SECONDS = 5
@dataclass(frozen=True)
class GatewayCall: request_id: str; server: str; tool: str; arguments: dict; agent_slug: str
@dataclass(frozen=True)
class Verdict: approve: bool; why: str; park_request_id: str | None = None
def expected_park(server: str, tool: str) -> tuple[str, str]   # (park_tool, park_server)
def parse_time(value) -> datetime | None                       # aware datetimes only, else None
def decide(call: GatewayCall, parks: list, neighbours: list, now: datetime) -> Verdict
```
`hermes_tool_name` moves from app.py into verify.py (app.py re-exports it); `gateway_slug` is imported
from `durable_park.gatecall`, `args_digest` from `durable_park.ledger` (both pure).

`bridge/whapi.py`
```python
DEFAULT_API_BASE = "https://app.withhuman.ai/api/v1"
DEFAULT_KEY_FILE = "/etc/durable-park/audit_key"
class ListError(Exception): code: str     # short safe code: no-key, deadline, http-403, shape, transport
class RequestLister:
    def __init__(self, base_url: str, key_file: str, *, per_call_timeout: float = 3.0,
                 transport: httpx.BaseTransport | None = None, clock=time.monotonic)
    def list(self, *, agent_slug: str, q: str, limit: int = 50, deadline: float | None = None) -> list
```
Reads the key file on every call (rotation), never logs or repr()s it; https only; no redirects;
timeout = min(per_call_timeout, deadline - clock()); non-200 -> ListError; body must be an object with a
`requests` list.

`bridge/app.py`
* `create_app(*, secret, lister, expected_agent=DEFAULT_EXPECTED_AGENT, clock=time.time)`.
* `Bridge(expected_agent, lister, clock)`; keeps all current payload checks, plus `request.id` must be a
  non-empty string; overall budget 3.5 s for both list calls; ListError/any exception -> human.
* Removed: ledger access, `_DeliveryStore`, `bridge_deliveries.db`, `LedgerMissing`, `--ledger`.
* CLI: `--api-base` (env `DURABLE_PARK_WH_API_BASE`), `--key-file` (env `DURABLE_PARK_WH_KEY_FILE`),
  plus existing `--secret-file`, `--expected-agent`, `--host`, `--port`.
* Responses unchanged in shape: `{"outcome":"approve","reason":"Matches approved park request <id>"}` or
  `{"outcome":"human","reason": NO_PARK_REASON}`.

## Deploy (bridge only; the bot is not touched)

* Unit: drop `DURABLE_PARK_LEDGER` and `ReadWritePaths`; `ProtectHome=yes`; ExecStart adds
  `--key-file /etc/durable-park/audit_key`. User stays `hermes` (reads `audit_key` root:hermes 0640 and
  `bridge_secret` via group dpwake).
* Before restarting the bridge: no ledger row `parked`, `approved` or `executing`.
* Rollback: copy `/opt/durable-park/bridge` and the unit file aside first; restore and restart.
* Live tests: one real payment passes once; replaying the same call through the gateway afterwards gets
  a human; the bridge works with the ledger unreadable to it.

## Not in scope

Removing the ledger from park/wake/sweep/executor (backlog). Non-money QuickBooks double checks.
