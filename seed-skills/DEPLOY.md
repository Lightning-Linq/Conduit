# Deploying the seed skills

This runs the seed-skills webhook at `https://skills.lightninglinq.ai` so the skills
listed on Nostr become executable, not just discoverable. It uses Docker Compose with
Caddy for automatic HTTPS. Any small VPS works (1 vCPU / 1 GB is plenty).

## 1. Get a host and point DNS
- Provision a VPS (Hetzner, DigitalOcean, etc.) running Ubuntu or Debian.
- Add a DNS A record: `skills.lightninglinq.ai` to the VPS public IP.
- Open ports 80 and 443 in the firewall.

## 2. Install Docker
On the VPS:
```bash
curl -fsSL https://get.docker.com | sh
```

## 3. Get the code onto the VPS
```bash
git clone https://github.com/Lightning-Linq/Conduit.git
cd Conduit/seed-skills
```

## 4. Launch
```bash
docker compose up -d --build
```
Caddy automatically obtains a Let's Encrypt certificate for `skills.lightninglinq.ai`.
This needs the DNS record from step 1 to be live and ports 80/443 reachable.

## 5. Verify
```bash
curl https://skills.lightninglinq.ai/         # {"status":"ok","skills":15}
curl https://skills.lightninglinq.ai/skills   # the catalog
```
The Nostr listings already point at `https://skills.lightninglinq.ai/skills/<name>`,
so once this is up the marketplace skills are executable through Conduit.

## Configuration
- `REQUIRE_PAYMENT_PROOF` (default `true`): require a valid payment preimage on every
  call. Set to `false` only for local testing.

## Updating
```bash
git pull && docker compose up -d --build
```

## A note on abuse
The payment-proof check (`SHA256(preimage) == payment_hash`) is a basic guard: it
proves a preimage matches a hash, not that you issued the invoice, so a direct caller
could still run these keyless skills for free. They are cheap and keyless, but for a
public endpoint consider putting Cloudflare (free tier) in front for rate limiting and
DDoS protection, or add a Caddy rate-limit. The stronger fix, verifying each
`payment_hash` against an invoice your node actually issued, is the documented
extension point in `app/payment.py`.
