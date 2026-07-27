# Federation

A single Conduit node is a marketplace of one. Federation is how nodes stop being
islands: they share reputation, share catalogs, and (opt-in) let each other's agents
buy skills across node boundaries.

This is the operator's guide. It covers what each layer does, what it costs you, what
it exposes, and how to turn it on or off. Everything here was checked against the code
in `src/conduit/services/federation*.py` and `src/conduit/api/routers/federation.py`.

## The one idea worth reading first

**A peer is untrusted infrastructure.** Not a partner, not an authority. Every layer
below assumes a peer may lie, withhold, flood, or disappear, and is built so that none
of those break you:

- Anything a peer sends that claims to be signed is re-verified locally on arrival.
  A peer cannot forge a rating or inflate a listing; at worst it serves junk (rejected)
  or withholds (mitigated by having several sources).
- A peer's *claims about itself* are never trusted. Its "verified provider" badges are
  stripped, and its reputation numbers are recomputed from signed attestations here.
- Every peer response is size-capped while streaming and refused if compressed, so a
  hostile peer cannot exhaust memory with an endless or bomb-encoded body.
- Every outbound peer URL is SSRF-validated before a socket opens, and redirects are
  disabled so a peer cannot bounce a request to an address the check already cleared.

Federation is opt-out for discovery and reputation, and opt-in for execution.

## The four layers

### Federation #1: shared reputation

The problem: a new node has no ratings, and ratings that live only in one node's
database are worth nothing to anyone else.

When a paid execution is confirmed, the hosting node mints a **payer binding**: a
signature over (skill, payment hash, payer pubkey) proving that this specific payer
really did pay for this specific skill. The consumer can then publish a **kind-9070
rating attestation** to Nostr carrying that binding. The rating is therefore bound to
a real Lightning payment, not to an anonymous form submission.

Other nodes fetch those attestations, re-verify every signature, and cache them in
Postgres (`federated_attestations`). Ratings become portable across the network.

Sybil resistance is deliberately described as raising cost, not eliminating abuse. A
provider who is willing to pay themselves can always rate themselves. What the
aggregation does (`aggregate_reputation`):

- only attestations for this exact skill and provider key count,
- one rating per payment hash, and on a collision (the same payment hash bound to two
  different raters or scores, which only a provider can mint) the **lowest** score wins
  and a `duplicate_payment_binding` flag is raised, so a provider cannot quietly bury
  an honest bad rating,
- direct self-ratings (rater equals provider) are excluded and flagged,
- per-rater diminishing weight (1/n), so one loud account cannot dominate,
- `created_at` is attacker-controlled and is never used as a security tiebreak.

### Federation #1.5: reputation peering

Relays are the broadcast channel, but they are lossy and not everyone runs one. This
layer lets nodes pull cached attestations directly from each other over HTTP:

- `GET /api/v1/federation/attestations?provider_pubkey=...` serves this node's cached
  attestation events. Public and read-only, because the data is already public on
  Nostr; serving it exposes nothing new.
- A node pulls from every peer in `FEDERATION_PEERS`, merges with what relays gave it,
  and re-verifies everything on ingest.

A peer that is down, slow, or hostile is skipped. Its absence costs you coverage, never
correctness.

### Federation #2: shared skill catalog

Reputation without a catalog still leaves each node selling only its own skills. Here,
each node publishes its active listings as signed **kind-38383** events, and pulls
other nodes' listings into a local cache (`cached_skills`).

- `GET /api/v1/federation/skills` serves this node's active listings as signed events.
- Listings arrive from two transports: Nostr relays and peer HTTP endpoints.
- Every event's signature is re-verified on ingest, and this node's own listings are
  excluded so a node never re-imports itself.
- NIP-33 replaceable semantics: one row per (provider pubkey, skill id), newest wins.

Discovery then merges local and cached skills. Three things happen to remote entries:

- they are **origin-tagged** (`relay` or `peer`) so a caller can tell where a skill
  came from,
- their **verification badges are neutralized** to `unverified`, because a peer
  asserting "this provider is verified" is just a peer talking about itself,
- the **federated reputation overlay** is applied from layer #1, which is the trust
  signal that actually crosses node boundaries.

Local skills always win a coordinate clash. That is a security property, not a
preference: without it, a remote node could publish a listing carrying a local skill's
(public) UUID and shadow it.

### Federation #3: cross-node execution

Discovery federated the *catalog*, but a skill you can see and cannot buy is a
brochure. This layer lets an agent on node A buy a skill hosted by node B.

**A brokers; it never takes custody.** The flow:

1. A's agent asks to execute a skill that A has cached but does not host.
2. A resolves which peer hosts it, and asks that peer to open an execution.
3. B mints its invoices exactly as it would for a local buyer and returns them.
4. A verifies the quote, records a routing row, and hands the invoice to the consumer.
5. The consumer pays **B directly over Lightning**. A never touches the funds.
6. The consumer confirms with A; A forwards the proof to B.
7. B verifies settlement against its own wallet, runs the provider webhook, and
   returns the output. A stores and relays it.

The guards that matter:

- **Quotes are checked against the signed listing.** Three numbers must agree: the
  price the catalog advertised, the peer's claim, and the amount actually encoded in
  the bolt11 invoice. Only the last one binds the consumer's wallet, and only the first
  is what the agent decided to buy against. A peer may quote *less* than it listed (a
  price cut is legitimate); it may never quote more. The fee split is verified too, so
  an honest-looking provider invoice cannot hide an inflated platform-fee invoice.
  This matters more than in a human marketplace because the payer is an agent that will
  likely pay the invoice without a human reading the amount.
- **Peers must be allowlisted.** A target host is only ever a URL you put in
  `FEDERATION_PEERS`. The provenance recorded on a cached listing is peer-supplied and
  is never treated as an address on its own.
- **No onward brokering.** A node sells only what it hosts. It will not broker a
  purchase to a third node, which prevents A to B to C chaining, request amplification
  across the network, and cycles between two nodes that each cache the other.
- **A paid purchase stays retryable.** If the hosting node fails during confirm, the
  local record returns to pending rather than failed. The consumer's money has already
  left, so the purchase must remain confirmable; the hosting node is idempotent on its
  own execution id, so a retry re-delivers instead of double-charging.

Ratings still work across nodes: the binding signature is minted by the hosting node's
key (which the broker structurally cannot mint), and the broker relays it back so the
consumer can publish the attestation themselves.

Current limits, stated plainly:

- Only skills discovered from a **peer** can be bought cross-node. A relay-discovered
  listing advertises no node address, so there is nobody to call.
- There is no cross-node refund beyond what the hosting node already does.
- Rating submission goes to Nostr directly, not through the broker.

## Configuration

| Setting | Default | What it does |
| --- | --- | --- |
| `FEDERATION_ENABLED` | `true` | Master switch for reputation + catalog sharing. Off means local-only: no attestations published, no remote listings, and the serve endpoints return 404. |
| `FEDERATION_PEERS` | empty | Comma-separated peer base URLs (https only). Empty means relay-only discovery and no cross-node buying. This list is also the allowlist for cross-node execution. |
| `FEDERATION_REFRESH_INTERVAL_MINUTES` | `30` | How often the background loop pulls relays and peers into the caches. Minimum enforced interval is 60 seconds. |
| `FEDERATION_EXECUTION_ENABLED` | `false` | Cross-node execution (#3). Off by default and **not** derived from `FEDERATION_ENABLED`, so upgrading never silently starts serving executions. |

### What turning on cross-node execution actually exposes

`FEDERATION_EXECUTION_ENABLED=true` does two things at once, and you should want both
before setting it:

- **Outbound:** your agents may buy skills from nodes in `FEDERATION_PEERS`.
- **Inbound:** your node serves `POST /api/v1/federation/executions` and
  `POST /api/v1/federation/executions/{id}/confirm` **without a credential**, so any
  node can buy the skills you host.

The unauthenticated surface is intentional; that is what makes an open marketplace
open, and what is handed back is a Lightning invoice, which is public by nature. A
caller gains nothing without paying, and confirm still requires a preimage that hashes
to the invoice's payment hash plus real settlement on your wallet.

The exposure to weigh is invoice-minting spam against your wallet. Both endpoints are
rate-limited (request more tightly than confirm, since minting costs your node real
work). Be aware that anonymous callers currently share one global rate-limit counter,
so the limit caps total abuse but does not isolate one noisy peer from another.

## Running it

Peers are configured, not discovered. To federate with a node, put its base URL in
`FEDERATION_PEERS` and have its operator do the same for you.

```
FEDERATION_ENABLED=true
FEDERATION_PEERS=https://peer-a.example,https://peer-b.example
FEDERATION_EXECUTION_ENABLED=false
```

The REST API runs a background refresh loop on the interval above, but only when
`FEDERATION_ENABLED` is true. **MCP-only nodes do not run that loop**, because it lives
in the API's lifespan. If you run MCP without the REST API, refresh on demand:

```bash
curl -X POST https://your-node.example/api/v1/federation/refresh -H "X-API-Key: $CONDUIT_API_KEY"
```

That endpoint, unlike the serve endpoints, requires your API key: pulling is an
operator action, serving is public.

### Turning it off

Set `FEDERATION_ENABLED=false`. Your node keeps working as a standalone marketplace:
local skills, local ratings, no publishing, no remote listings, and the federation
endpoints return 404. Clearing `FEDERATION_PEERS` is the narrower move, leaving relay
discovery on but ending direct peering.

## Endpoint summary

| Endpoint | Auth | Layer |
| --- | --- | --- |
| `GET /api/v1/federation/attestations` | public | #1.5 |
| `GET /api/v1/federation/skills` | public | #2 |
| `POST /api/v1/federation/refresh` | API key | #1.5 / #2 |
| `POST /api/v1/federation/executions` | public, opt-in | #3 |
| `POST /api/v1/federation/executions/{id}/confirm` | public, opt-in | #3 |
