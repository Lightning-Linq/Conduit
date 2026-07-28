# Federation

A single Conduit node is a marketplace of one. Federation is how nodes stop being
islands: they share reputation, share catalogs, and (opt-in) let each other's agents
buy skills across node boundaries.

This is the operator's guide. It covers what each layer does, what it costs you, what
it exposes, and how to turn it on or off. Everything here was checked against the code
in `src/conduit/services/federation*.py` and `src/conduit/api/routers/federation.py`.

## First, Nostr

Federation moves signed Nostr events around, so it is worth a page on what those are
before the layers make sense.

Nostr is a protocol for publishing signed messages through servers that are
deliberately dumb. A relay stores events and hands them to whoever asks. It cannot
forge one, because every event carries a signature from the key that wrote it, and it
cannot be an authority over anything, because anyone can run a different relay and the
same events will be there. Identity is a keypair rather than an account: there is
nobody to register with, and nobody who can revoke you.

Conduit uses it for four distinct jobs, and it helps to keep them separate:

- **Identity.** A provider is a public key, displayed as an `npub`. A kind-0 profile
  event can attach a display name and a Lightning address, and a NIP-05 record maps
  that key to a domain the provider already controls, which is the closest thing to a
  human-readable name Nostr offers.
- **Skill listings.** Each listing is a kind-38383 event carrying price, category, and
  endpoint. They are NIP-33 replaceable, keyed by (author, skill id), so editing a
  skill supersedes the previous event instead of leaving stale copies on relays.
- **Reputation.** Ratings are kind-9070 events signed by the payer and bound to a
  specific payment (see Federation #1 below). A provider cannot mint praise for a sale
  that never happened, and cannot delete a rating it dislikes.
- **Wallet connection.** NWC (NIP-47) talks to your wallet over encrypted Nostr events.
  Conduit encrypts with NIP-44 v2 and still accepts the deprecated NIP-04, which many
  wallets continue to send. This one is unlike the others: it is a private channel
  between your node and your wallet, not part of the public marketplace. It shares the
  transport, nothing else.

Relays are configured with `NOSTR_RELAYS` and Conduit publishes to several by default.
Relay URLs are SSRF-validated before any connection, and the federated catalog pull
additionally pins the resolved IP to defeat DNS rebinding. The real defense, though, is
that a relay is never believed in the first place: an event counts because its
signature checks out, not because of where it arrived from.

### How this relates to federation

Nostr defines what a listing or a rating **is**. Federation is how nodes make sure they
actually **receive** it.

Relays broadcast to anyone listening, which is exactly right for publishing, but they
are best-effort. A relay can be slow, drop an event, rate-limit you, or be unreachable
at the moment you need it, and no single relay is guaranteed to have seen everything.
So nodes also pull the same signed events directly from each other over HTTP, then
re-verify every signature exactly as they would from a relay. Two paths, one set of
events, identical trust rules. That is layers #1.5 and #2.

Cross-node execution (#3) is the one part that Nostr cannot carry. Buying something is
a conversation: quote, invoice, payment proof, result, all in sequence between two
specific parties. Broadcast events are one-way and asynchronous, so that exchange runs
over direct HTTP between the two nodes involved, while the listing that started it and
the rating that ends it stay on Nostr where everyone can see them.

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
- **`REQUIRE_VERIFIED_SKILLS` fails closed across the federation.** With that policy
  on, this node refuses to broker a peer-hosted skill at all: remote badges are
  neutralized on ingest (see above), so a cached listing carries no verification it
  can trust, and quietly brokering one would be the exact bypass the policy exists to
  prevent. Serving works the other way round and is enforced per skill: a peer buying
  one of *your* unverified skills is refused just like a local buyer. Both refusals
  land before any invoice is minted.
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
