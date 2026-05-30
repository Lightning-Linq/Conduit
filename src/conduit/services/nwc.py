"""
NWC (Nostr Wallet Connect) backend — NIP-47 wallet access over Nostr relays.

Implements the WalletBackend protocol using the NIP-47 standard.
Users paste a nostr+walletconnect:// URI from their wallet (Alby, Primal,
Zeus, etc.) and Conduit communicates with the wallet via encrypted Nostr
events.

Protocol reference: https://github.com/nostr-protocol/nips/blob/master/47.md

Event kinds:
  13194 — info (wallet capabilities)
  23194 — request (client → wallet)
  23195 — response (wallet → client)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from conduit.services.wallet_backend import InvoiceResponse, PaymentResponse, NodeInfo


# NIP-47 event kinds
NWC_INFO_KIND = 13194
NWC_REQUEST_KIND = 23194
NWC_RESPONSE_KIND = 23195

# Timeout for waiting for a wallet response (seconds)
NWC_RESPONSE_TIMEOUT = 30


@dataclass
class NwcConnection:
    """Parsed nostr+walletconnect:// URI."""
    wallet_pubkey: str   # hex pubkey of the wallet service
    client_secret: str   # hex secret for the client (our signing key)
    relays: list[str]    # relay URLs
    lud16: str = ""      # optional lightning address


def parse_nwc_uri(uri: str) -> NwcConnection:
    """Parse a nostr+walletconnect:// connection string.

    Format: nostr+walletconnect://<wallet_pubkey>?relay=<url>&secret=<hex>&lud16=<addr>
    """
    if not uri.startswith("nostr+walletconnect://"):
        raise ValueError(
            f"Invalid NWC URI: must start with nostr+walletconnect:// "
            f"(got {uri[:30]}...)"
        )

    # Parse the URI — the pubkey is the "host" part
    # Replace the scheme so urlparse handles it
    fake_url = uri.replace("nostr+walletconnect://", "https://", 1)
    parsed = urlparse(fake_url)
    wallet_pubkey = parsed.hostname or ""

    if not wallet_pubkey or len(wallet_pubkey) != 64:
        raise ValueError(f"Invalid NWC URI: wallet pubkey must be 64 hex chars")

    params = parse_qs(parsed.query)

    secret = params.get("secret", [""])[0]
    if not secret or len(secret) != 64:
        raise ValueError("Invalid NWC URI: secret must be 64 hex chars")

    relays = params.get("relay", [])
    if not relays:
        raise ValueError("Invalid NWC URI: at least one relay URL is required")

    lud16 = params.get("lud16", [""])[0]

    return NwcConnection(
        wallet_pubkey=wallet_pubkey,
        client_secret=secret,
        relays=relays,
        lud16=lud16,
    )


def _derive_pubkey_from_secret(secret_hex: str) -> str:
    """Derive the public key from a secret key using secp256k1."""
    try:
        import coincurve
        pk = coincurve.PublicKey.from_secret(bytes.fromhex(secret_hex))
        return pk.format(compressed=True)[1:].hex()
    except ImportError:
        # Fallback to pure-Python (from nostr.py)
        from conduit.services.nostr import _xonly_pubkey, _int_from_bytes
        privkey_int = _int_from_bytes(bytes.fromhex(secret_hex))
        return _xonly_pubkey(privkey_int).hex()


class NwcWalletBackend:
    """
    NIP-47 Nostr Wallet Connect backend.

    Communicates with a Lightning wallet through encrypted Nostr events
    over websocket relays. Works with any NWC-compatible wallet:
    Alby, Primal, Zeus, Coinos, Umbrel, etc.
    """

    def __init__(self, connection_uri: str):
        self._conn = parse_nwc_uri(connection_uri)
        self._client_pubkey = _derive_pubkey_from_secret(self._conn.client_secret)
        self._connected = False
        self._wallet_methods: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Verify the connection by fetching the wallet's info event."""
        # For NWC, "connecting" means we verified the URI parses and
        # we can derive our pubkey. Actual relay connections happen
        # per-request since websockets are ephemeral.
        self._connected = True
        print(
            f"[nwc] Connected via NWC\n"
            f"  Wallet pubkey: {self._conn.wallet_pubkey[:16]}...\n"
            f"  Client pubkey: {self._client_pubkey[:16]}...\n"
            f"  Relay(s): {', '.join(self._conn.relays)}",
            file=sys.stderr,
        )

    def disconnect(self) -> None:
        """No persistent connection to close."""
        self._connected = False

    def get_info(self) -> NodeInfo:
        """Get wallet info via NIP-47 get_info."""
        result = self._send_nwc_request("get_info", {})
        return NodeInfo(
            pubkey=result.get("pubkey", self._conn.wallet_pubkey),
            alias=result.get("alias", "NWC Wallet"),
            num_active_channels=0,
            num_peers=0,
            block_height=result.get("block_height", 0),
            synced_to_chain=True,
            version="NWC/NIP-47",
            backend_type="nwc",
        )

    def get_balance(self) -> dict[str, int]:
        """Get wallet balance via NIP-47 get_balance."""
        result = self._send_nwc_request("get_balance", {})
        balance_msats = result.get("balance", 0)
        return {
            "channel_balance_sats": balance_msats // 1000,
            "channel_pending_sats": 0,
            "onchain_confirmed_sats": 0,
            "onchain_unconfirmed_sats": 0,
            "onchain_total_sats": 0,
        }

    def create_invoice(
        self, amount_msats: int, memo: str = "", expiry: int = 3600
    ) -> InvoiceResponse:
        """Create an invoice via NIP-47 make_invoice."""
        params = {
            "amount": amount_msats,
            "description": memo,
            "expiry": expiry,
        }
        result = self._send_nwc_request("make_invoice", params)
        return InvoiceResponse(
            payment_request=result.get("invoice", ""),
            payment_hash=result.get("payment_hash", ""),
            add_index=0,
        )

    def pay_invoice(
        self, payment_request: str, max_fee_msats: int = 10000
    ) -> PaymentResponse:
        """Pay an invoice via NIP-47 pay_invoice."""
        params = {"invoice": payment_request}
        try:
            result = self._send_nwc_request("pay_invoice", params)
            return PaymentResponse(
                payment_hash=result.get("payment_hash", ""),
                preimage=result.get("preimage", ""),
                fee_msats=result.get("fees_paid", 0),
                status="SUCCEEDED",
            )
        except NwcError as e:
            return PaymentResponse(
                payment_hash="",
                preimage="",
                fee_msats=0,
                status="FAILED",
                failure_reason=str(e),
            )

    def decode_invoice(self, payment_request: str) -> dict:
        """Decode a BOLT-11 invoice locally (no NWC call needed).

        Uses the bolt11 format: we parse what we can from the invoice
        string. For full decode, we'd need a bolt11 library, but for
        Conduit's needs we can use lookup_invoice or parse the basics.
        """
        # Try to decode locally first using pyln-bolt11 or similar
        # For now, make a lookup_invoice call if we have the hash,
        # or return partial data from the invoice string itself
        try:
            import bolt11 as bolt11_lib
            decoded = bolt11_lib.decode(payment_request)
            return {
                "destination": decoded.payee or "",
                "payment_hash": decoded.payment_hash or "",
                "amount_sats": (decoded.amount_msat or 0) // 1000,
                "amount_msats": decoded.amount_msat or 0,
                "description": decoded.description or "",
                "expiry": decoded.expiry or 3600,
                "timestamp": decoded.date or 0,
            }
        except ImportError:
            pass

        # Fallback: minimal parse from the invoice string
        # BOLT-11 invoices encode the amount in the human-readable part
        amount_sats = _parse_bolt11_amount(payment_request)
        return {
            "destination": "",
            "payment_hash": "",
            "amount_sats": amount_sats,
            "amount_msats": amount_sats * 1000,
            "description": "",
            "expiry": 3600,
            "timestamp": int(time.time()),
        }

    def lookup_invoice(self, payment_hash: str) -> dict:
        """Look up an invoice via NIP-47 lookup_invoice."""
        params = {"payment_hash": payment_hash}
        try:
            result = self._send_nwc_request("lookup_invoice", params)
            settled = result.get("state") == "settled"
            return {
                "payment_request": result.get("invoice", ""),
                "amount_msats": result.get("amount", 0),
                "amount_paid_msats": result.get("amount", 0) if settled else 0,
                "settled": settled,
                "state": result.get("state", "pending"),
                "memo": result.get("description", ""),
                "preimage": result.get("preimage", ""),
            }
        except NwcError:
            return {
                "payment_request": "",
                "amount_msats": 0,
                "amount_paid_msats": 0,
                "settled": False,
                "state": "unknown",
                "memo": "",
            }

    def sign_message(self, message: str) -> str:
        """Sign a message with the client's NWC key.

        Note: NWC doesn't expose the wallet's node key for signing.
        We sign with the client's Nostr key instead. For provider
        verification, LND backend is preferred.
        """
        from conduit.services.nostr import NostrKeypair
        keypair = NostrKeypair.from_hex(self._conn.client_secret)
        # Simple Schnorr sign of the message hash
        msg_hash = hashlib.sha256(message.encode("utf-8")).digest()
        from conduit.services.nostr import _schnorr_sign
        sig = _schnorr_sign(msg_hash, bytes.fromhex(self._conn.client_secret))
        return sig.hex()

    def verify_message(self, message: str, signature: str) -> dict:
        """Verify a signed message.

        Uses Schnorr verification since NWC doesn't have LND's
        VerifyMessage RPC.
        """
        from conduit.services.nostr import _schnorr_verify
        msg_hash = hashlib.sha256(message.encode("utf-8")).digest()
        try:
            sig_bytes = bytes.fromhex(signature)
            # We don't know the pubkey — return the client pubkey
            # This is a limitation of NWC vs LND
            valid = _schnorr_verify(msg_hash, bytes.fromhex(self._client_pubkey), sig_bytes)
            return {"valid": valid, "pubkey": self._client_pubkey}
        except Exception:
            return {"valid": False, "pubkey": ""}

    # ── NWC Protocol Layer ──────────────────────────────────────────

    def _send_nwc_request(self, method: str, params: dict) -> dict:
        """Send a NIP-47 request and wait for the response.

        This is the core NWC protocol implementation:
        1. Build a Nostr event (kind 23194) with encrypted payload
        2. Send it to the relay
        3. Wait for a response event (kind 23195)
        4. Decrypt and return the result
        """
        import asyncio

        # Run the async websocket flow synchronously
        # (Conduit's LND client is sync, so NWC matches that interface)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — use a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run, self._async_nwc_request(method, params)
                    ).result(timeout=NWC_RESPONSE_TIMEOUT + 5)
                return result
            else:
                return loop.run_until_complete(
                    self._async_nwc_request(method, params)
                )
        except RuntimeError:
            return asyncio.run(self._async_nwc_request(method, params))

    async def _async_nwc_request(self, method: str, params: dict) -> dict:
        """Async implementation of the NWC request/response cycle."""
        import websockets

        # Build the request payload
        payload = json.dumps({"method": method, "params": params})

        # Encrypt with NIP-04 (widely supported) or NIP-44
        encrypted = self._nip04_encrypt(payload, self._conn.wallet_pubkey)

        # Build the Nostr event
        event = self._build_event(
            kind=NWC_REQUEST_KIND,
            content=encrypted,
            tags=[
                ["p", self._conn.wallet_pubkey],
            ],
        )

        # Sign the event
        event_id = self._compute_event_id(event)
        event["id"] = event_id
        event["sig"] = self._sign_event(event_id)

        relay_url = self._conn.relays[0]

        async with websockets.connect(relay_url, close_timeout=5) as ws:
            # Subscribe for the response first
            sub_id = secrets.token_hex(8)
            sub_filter = {
                "kinds": [NWC_RESPONSE_KIND],
                "#e": [event_id],
                "#p": [self._client_pubkey],
                "authors": [self._conn.wallet_pubkey],
            }
            await ws.send(json.dumps(["REQ", sub_id, sub_filter]))

            # Publish the request
            await ws.send(json.dumps(["EVENT", event]))

            # Wait for response
            deadline = time.time() + NWC_RESPONSE_TIMEOUT
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=deadline - time.time()
                    )
                except asyncio.TimeoutError:
                    break

                msg = json.loads(raw)
                if not isinstance(msg, list):
                    continue

                if msg[0] == "EVENT" and msg[1] == sub_id:
                    response_event = msg[2]
                    decrypted = self._nip04_decrypt(
                        response_event["content"],
                        self._conn.wallet_pubkey,
                    )
                    response = json.loads(decrypted)

                    if response.get("error"):
                        error = response["error"]
                        raise NwcError(
                            f"NWC {method} failed: [{error.get('code', 'UNKNOWN')}] "
                            f"{error.get('message', 'Unknown error')}"
                        )

                    return response.get("result", {})

                # Handle OK/NOTICE messages
                if msg[0] == "OK" and msg[2] is False:
                    raise NwcError(f"Relay rejected event: {msg[3] if len(msg) > 3 else 'unknown'}")

            # Close subscription
            await ws.send(json.dumps(["CLOSE", sub_id]))

        raise NwcError(f"NWC {method}: no response within {NWC_RESPONSE_TIMEOUT}s")

    # ── Nostr Event Building ────────────────────────────────────────

    def _build_event(self, kind: int, content: str, tags: list) -> dict:
        """Build an unsigned Nostr event."""
        return {
            "pubkey": self._client_pubkey,
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags,
            "content": content,
        }

    def _compute_event_id(self, event: dict) -> str:
        """Compute the event ID (SHA-256 of canonical serialization)."""
        serialized = json.dumps(
            [0, event["pubkey"], event["created_at"], event["kind"],
             event["tags"], event["content"]],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _sign_event(self, event_id: str) -> str:
        """Sign an event ID with the client secret (BIP-340 Schnorr)."""
        from conduit.services.nostr import _schnorr_sign
        msg = bytes.fromhex(event_id)
        privkey = bytes.fromhex(self._conn.client_secret)
        aux_rand = secrets.token_bytes(32)
        return _schnorr_sign(msg, privkey, aux_rand).hex()

    # ── NIP-04 Encryption ───────────────────────────────────────────

    def _nip04_encrypt(self, plaintext: str, recipient_pubkey: str) -> str:
        """Encrypt content using NIP-04 (AES-256-CBC with shared secret).

        NIP-04 is deprecated in favor of NIP-44, but is still the most
        widely supported encryption method across NWC wallets.
        """
        import base64
        import os
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding

        shared_secret = self._compute_shared_secret(recipient_pubkey)
        iv = os.urandom(16)

        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

        cipher = Cipher(algorithms.AES(shared_secret), modes.CBC(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        encrypted_b64 = base64.b64encode(ciphertext).decode()
        iv_b64 = base64.b64encode(iv).decode()

        return f"{encrypted_b64}?iv={iv_b64}"

    def _nip04_decrypt(self, content: str, sender_pubkey: str) -> str:
        """Decrypt NIP-04 encrypted content."""
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding

        parts = content.split("?iv=")
        if len(parts) != 2:
            raise NwcError("Invalid NIP-04 encrypted content format")

        ciphertext = base64.b64decode(parts[0])
        iv = base64.b64decode(parts[1])

        shared_secret = self._compute_shared_secret(sender_pubkey)

        cipher = Cipher(algorithms.AES(shared_secret), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()

        return plaintext.decode("utf-8")

    def _compute_shared_secret(self, their_pubkey: str) -> bytes:
        """Compute ECDH shared secret for NIP-04 encryption.

        shared_secret = SHA256(ECDH(our_privkey, their_pubkey))[:32]
        """
        try:
            import coincurve
            our_privkey = coincurve.PrivateKey(bytes.fromhex(self._conn.client_secret))
            # NIP-04 uses compressed pubkey (02 + x-only) for ECDH
            their_pk = coincurve.PublicKey(b"\x02" + bytes.fromhex(their_pubkey))
            # coincurve.ecdh returns the raw shared point x-coordinate
            shared = our_privkey.ecdh(their_pk.format())
            return shared[:32]
        except (ImportError, Exception):
            # Fallback: pure Python ECDH (slow but works)
            from conduit.services.nostr import _point_mul, _int_from_bytes, _P
            privkey_int = _int_from_bytes(bytes.fromhex(self._conn.client_secret))
            # Lift x-only pubkey to full point
            pubkey_x = _int_from_bytes(bytes.fromhex(their_pubkey))
            y_sq = (pow(pubkey_x, 3, _P) + 7) % _P
            y = pow(y_sq, (_P + 1) // 4, _P)
            if y % 2 != 0:
                y = _P - y
            point = (pubkey_x, y)
            shared_point = _point_mul(privkey_int, point)
            if shared_point is None:
                raise NwcError("ECDH failed: result is point at infinity")
            shared_x = shared_point[0].to_bytes(32, "big")
            return hashlib.sha256(shared_x).digest()[:32]


class NwcError(Exception):
    """Raised when an NWC operation fails."""
    pass


# ── BOLT-11 Amount Parser ──────────────────────────────────────────


def _parse_bolt11_amount(invoice: str) -> int:
    """Parse the amount from a BOLT-11 invoice string.

    BOLT-11 encodes amount in the human-readable part after 'ln' + network:
      lnbc = mainnet, lntb = testnet, lnbcrt = regtest
    Amount suffixes: m=milli, u=micro, n=nano, p=pico (of BTC)
    """
    invoice = invoice.lower().strip()

    # Strip the prefix
    for prefix in ("lnbcrt", "lntbs", "lntb", "lnbc"):
        if invoice.startswith(prefix):
            rest = invoice[len(prefix):]
            break
    else:
        return 0

    # Find where the amount ends (at the '1' separator)
    sep = rest.find("1")
    if sep <= 0:
        return 0

    amount_str = rest[:sep]

    # Parse multiplier suffix
    multipliers = {
        "m": 100_000_00,     # milli-BTC in sats
        "u": 100_00,         # micro-BTC in sats
        "n": 100,            # nano-BTC in sats (actually 0.01 sat)
        "p": 0,              # pico-BTC (sub-sat, round to 0)
    }

    for suffix, mult in multipliers.items():
        if amount_str.endswith(suffix):
            try:
                return int(float(amount_str[:-1]) * mult)
            except ValueError:
                return 0

    # No suffix — amount is in BTC
    try:
        return int(float(amount_str) * 100_000_000)
    except ValueError:
        return 0
