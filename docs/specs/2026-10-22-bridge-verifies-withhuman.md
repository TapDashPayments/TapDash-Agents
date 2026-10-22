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

Given the verified gateway call G = (request_id, server, tool, arguments, agent_slug). Every refusal is a
human verdict; the `why` code is what the bridge's log says.

0. **Unsafe names** (`unsafe-name`, checked before anything else). G.server must fullmatch
   `[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*` (no `__`, no leading or trailing `-`/`_`), G.tool must fullmatch
   `[A-Za-z0-9_-]+`, both must be strings, and the unhashed Hermes name `mcp__wh__<server>__<tool>` (after
   `-` becomes `_`) must be at most 64 characters, so Hermes never adds a hash suffix. Then Hermes' name
   sanitisation only folds `-` into `_`, and the bridge can build the park's tool name exactly.
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
4. On the single match P, in this order:
   * **Redaction** (`redacted-args`): P.arguments contain `gatecall.REDACTED` anywhere (dict values, list items,
     nested), or `redact_arguments(G.arguments)[0] != G.arguments` (a secret-looking key, or arguments too large
     to be shown whole). The approver could not read those values, so a person must see the call again.
   * **Outcome** (`already-reported`): `P.consumption.outcome` must be absent or null. The executor reports an
     outcome after the call ran; a park that has one was used.
5. **The gateway request itself must be listed.** The neighbour listing is every recent request of the agent
   (`q` omitted, `limit=200`, newest first). Find the item with `id == G.request_id`:
   not listed -> human (`gateway-request-not-listed`); its `created_at` must parse and be
   `>= P.consumed_at - SKEW_SECONDS`, else `gateway-request-before-release`; its `agent_slug`, `server` and `tool`
   must equal G's and `args_digest(its arguments) == digest` (arguments must be a dict), else
   `gateway-request-mismatch`.
6. **Truncated listing** (`neighbours-truncated`): when the listing has at least `neighbours_limit` items and its
   oldest `created_at` (any item with a missing or unparseable one counts as not old enough) is
   `>= P.consumed_at`, requests after the consumption may have been cut off: human.
7. **Collision-aware single use** (`already-used`): any other request O (`O.id != G.request_id`) with
   `O.agent_slug == G.agent_slug`, `O.server` not starting with `park:`, string `server` and `tool`, and
   `expected_park(O.server, O.tool) == expected_park(G.server, G.tool)` (Hermes folds `-` into `_`, so
   `create-bill` and `create_bill` share one park), whose `created_at` is missing/unparseable or
   `>= P.consumed_at`, and whose arguments are not a dict or have the same digest -> human. Any status counts
   (pending, approved, denied, expired, cancelled). An O whose names cannot be mapped is skipped.
8. Otherwise approve, naming P.id.

Anything unexpected (bad shapes, list failure, timeout, missing key) -> human. Never raise to the caller.
A retried delivery of the same G gets the same answer (G excludes itself).

Limit: if a gateway server ever offers two tools that differ only by `-` versus `_` (for example `create-bill`
and `create_bill`), the bridge cannot tell them apart (they share one park tool name and one use). Check the tool
catalogue when adding a server; today quickbooks has `create-bill` but no `create_bill`.

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
def decide(call: GatewayCall, parks: list, neighbours: list, now: datetime, *,
           neighbours_limit: int | None = None) -> Verdict
```
`hermes_tool_name` moves from app.py into verify.py (app.py re-exports it); `gateway_slug` is imported
from `durable_park.gatecall`, `args_digest` from `durable_park.ledger` (both pure).

`bridge/whapi.py`
```python
DEFAULT_API_BASE = "https://app.withhuman.ai/api/v1"
DEFAULT_KEY_FILE = "/etc/durable-park/audit_key"
class ListError(Exception): code: str     # short safe code: no-key, deadline, http-<status>, shape, transport, too-large, bad-base-url
class RequestLister:
    def __init__(self, base_url: str, key_file: str, *, per_call_timeout: float = 3.0,
                 transport: httpx.BaseTransport | None = None, clock=time.monotonic)
    def list(self, *, agent_slug: str, q: str | None, limit: int = 50, deadline: float | None = None) -> list
```
Reads the key file on every call (rotation), never logs or repr()s it; https only; no redirects;
timeout = min(per_call_timeout, deadline - clock()); the body is streamed, capped at 4 MiB (`too-large`) and the
wall clock is checked after each chunk (`deadline`); `trust_env=False`; non-200 -> ListError; body must be an object with a
`requests` list.

`bridge/app.py`
* `create_app(*, secret, lister, expected_agent=DEFAULT_EXPECTED_AGENT, clock=time.time)`.
* `Bridge(expected_agent, lister, clock)`; keeps all current payload checks, plus `request.id` must be a
  non-empty string; overall budget 3.5 s for both list calls; ListError/any exception -> human.
  The park listing is `lister.list(agent_slug=slug, q=park_tool, limit=50, ...)`; the neighbour listing is
  `lister.list(agent_slug=slug, q=None, limit=200, ...)` (`q=None` omits the `q` parameter, so every recent
  request of the agent comes back, whatever its tool) and `decide(..., neighbours_limit=200)`.
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
