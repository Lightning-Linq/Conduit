"""LND gRPC client — connects to your Lightning node via Tor tunnel.

Requires:
    - socat tunnel running: socat TCP-LISTEN:10009,fork,reuseaddr \
          SOCKS4A:127.0.0.1:<onion>:10009,socksport=9050
    - Tor service running: brew services start tor
    - credentials/full-chain.pem and credentials/admin.macaroon in project root
"""

import codecs
import sys
from dataclasses import dataclass
from pathlib import Path

import grpc

from conduit.core.config import settings

# Add proto_generated to path so imports resolve
_proto_path = Path(__file__).parent / "proto_generated"
if str(_proto_path) not in sys.path:
    sys.path.insert(0, str(_proto_path))

import lightning_pb2 as ln  # noqa: E402
import lightning_pb2_grpc as lnrpc  # noqa: E402


@dataclass
class InvoiceResponse:
    """Result of creating a Lightning invoice."""

    payment_request: str
    payment_hash: str
    add_index: int


@dataclass
class PaymentResponse:
    """Result of sending a Lightning payment."""

    payment_hash: str
    preimage: str
    fee_msats: int
    status: str
    failure_reason: str | None = None


@dataclass
class NodeInfo:
    """Basic info about the connected LND node."""

    pubkey: str
    alias: str
    num_active_channels: int
    num_peers: int
    block_height: int
    synced_to_chain: bool
    version: str


class LndClient:
    """
    gRPC wrapper for LND operations.

    Connects to LND via a local socat tunnel (localhost:10009 → Tor → .onion:10009).
    All calls are synchronous gRPC — async wrappers can be added via grpc.aio if needed.

    Usage:
        client = LndClient()
        client.connect()
        info = client.get_info()
        invoice = client.create_invoice(amount_msats=10000, memo="test payment")
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = settings.lnd_grpc_port,
        tls_cert_path: Path = settings.lnd_tls_cert_path,
        macaroon_path: Path = settings.lnd_macaroon_path,
    ):
        self.host = host
        self.port = port
        self.tls_cert_path = Path(tls_cert_path).expanduser()
        self.macaroon_path = Path(macaroon_path).expanduser()
        self._channel: grpc.Channel | None = None
        self._stub: lnrpc.LightningStub | None = None
        self._macaroon: str = ""
        self._connected: bool = False

    def connect(self) -> None:
        """Establish gRPC connection to LND via local tunnel."""
        # Read TLS cert chain
        tls_cert = self.tls_cert_path.read_bytes()
        ssl_creds = grpc.ssl_channel_credentials(tls_cert)

        # Read macaroon for auth
        macaroon_bytes = self.macaroon_path.read_bytes()
        self._macaroon = codecs.encode(macaroon_bytes, "hex").decode()

        # Connect via local socat tunnel to Tor
        self._channel = grpc.secure_channel(
            f"{self.host}:{self.port}",
            ssl_creds,
            options=[("grpc.ssl_target_name_override", "lnd.embassy")],
        )

        # Verify connection (30s timeout — Tor tunnels need extra time)
        grpc.channel_ready_future(self._channel).result(timeout=30)

        self._stub = lnrpc.LightningStub(self._channel)
        self._connected = True

    def disconnect(self) -> None:
        """Close the gRPC connection."""
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _metadata(self) -> list[tuple[str, str]]:
        """Auth metadata for gRPC calls."""
        return [("macaroon", self._macaroon)]

    def get_info(self) -> NodeInfo:
        """Get basic node info (alias, pubkey, sync status)."""
        assert self._stub, "Not connected — call connect() first"
        response = self._stub.GetInfo(ln.GetInfoRequest(), metadata=self._metadata())
        return NodeInfo(
            pubkey=response.identity_pubkey,
            alias=response.alias,
            num_active_channels=int(response.num_active_channels),
            num_peers=int(response.num_peers),
            block_height=int(response.block_height),
            synced_to_chain=response.synced_to_chain,
            version=response.version,
        )

    def create_invoice(
        self, amount_msats: int, memo: str = "", expiry: int = 3600
    ) -> InvoiceResponse:
        """
        Create a new Lightning invoice.

        Args:
            amount_msats: Amount in millisatoshis (0 for any-amount invoice)
            memo: Human-readable description
            expiry: Seconds until invoice expires

        Returns:
            InvoiceResponse with payment_request and payment_hash
        """
        assert self._stub, "Not connected — call connect() first"
        request = ln.Invoice(
            value_msat=amount_msats,
            memo=memo,
            expiry=expiry,
        )
        response = self._stub.AddInvoice(request, metadata=self._metadata())
        return InvoiceResponse(
            payment_request=response.payment_request,
            payment_hash=response.r_hash.hex(),
            add_index=response.add_index,
        )

    def pay_invoice(
        self, payment_request: str, max_fee_msats: int = 10000
    ) -> PaymentResponse:
        """
        Pay a Lightning invoice.

        Args:
            payment_request: BOLT-11 encoded invoice
            max_fee_msats: Maximum routing fee willing to pay

        Returns:
            PaymentResponse with preimage on success
        """
        assert self._stub, "Not connected — call connect() first"
        request = ln.SendRequest(
            payment_request=payment_request,
            fee_limit=ln.FeeLimit(fixed_msat=max_fee_msats),
        )
        response = self._stub.SendPaymentSync(request, metadata=self._metadata())

        # Check for success first: if we got a preimage, the payment worked
        # regardless of what payment_error says (some LND versions set both)
        preimage_hex = response.payment_preimage.hex() if response.payment_preimage else ""
        payment_hash_hex = response.payment_hash.hex() if response.payment_hash else ""

        if preimage_hex and preimage_hex != "0" * 64:
            # Got a real preimage — payment succeeded
            return PaymentResponse(
                payment_hash=payment_hash_hex,
                preimage=preimage_hex,
                fee_msats=int(response.payment_route.total_fees_msat) if response.payment_route else 0,
                status="SUCCEEDED",
            )

        if response.payment_error:
            return PaymentResponse(
                payment_hash=payment_hash_hex,
                preimage="",
                fee_msats=0,
                status="FAILED",
                failure_reason=response.payment_error,
            )

        # No preimage and no error — shouldn't happen but treat as failure
        return PaymentResponse(
            payment_hash=payment_hash_hex,
            preimage="",
            fee_msats=0,
            status="FAILED",
            failure_reason="No preimage returned and no error reported",
        )

    def get_balance(self) -> dict[str, int]:
        """Get channel and on-chain balances in satoshis."""
        assert self._stub, "Not connected — call connect() first"

        # Channel balance
        chan_resp = self._stub.ChannelBalance(
            ln.ChannelBalanceRequest(), metadata=self._metadata()
        )

        # On-chain balance
        chain_resp = self._stub.WalletBalance(
            ln.WalletBalanceRequest(), metadata=self._metadata()
        )

        return {
            "channel_balance_sats": int(chan_resp.local_balance.sat) if chan_resp.local_balance else 0,
            "channel_pending_sats": int(chan_resp.pending_open_local_balance.sat)
            if chan_resp.pending_open_local_balance
            else 0,
            "onchain_confirmed_sats": int(chain_resp.confirmed_balance),
            "onchain_unconfirmed_sats": int(chain_resp.unconfirmed_balance),
            "onchain_total_sats": int(chain_resp.total_balance),
        }

    def decode_invoice(self, payment_request: str) -> dict:
        """Decode a BOLT-11 invoice without paying it."""
        assert self._stub, "Not connected — call connect() first"
        request = ln.PayReqString(pay_req=payment_request)
        response = self._stub.DecodePayReq(request, metadata=self._metadata())
        return {
            "destination": response.destination,
            "payment_hash": response.payment_hash,
            "amount_sats": int(response.num_satoshis),
            "amount_msats": int(response.num_msat),
            "description": response.description,
            "expiry": int(response.expiry),
            "timestamp": int(response.timestamp),
        }

    def lookup_invoice(self, payment_hash: str) -> dict:
        """Look up an invoice by payment hash."""
        assert self._stub, "Not connected — call connect() first"
        r_hash = bytes.fromhex(payment_hash)
        request = ln.PaymentHash(r_hash=r_hash)
        response = self._stub.LookupInvoice(request, metadata=self._metadata())
        return {
            "payment_request": response.payment_request,
            "amount_msats": int(response.value_msat),
            "amount_paid_msats": int(response.amt_paid_msat),
            "settled": response.settled,
            "state": response.state,
            "memo": response.memo,
        }

    def sign_message(self, message: str) -> str:
        """
        Sign a message with this node's private key.
        Returns the base64-encoded signature.
        """
        assert self._stub, "Not connected — call connect() first"
        request = ln.SignMessageRequest(msg=message.encode("utf-8"))
        response = self._stub.SignMessage(request, metadata=self._metadata())
        return response.signature

    def verify_message(self, message: str, signature: str) -> dict:
        """
        Verify a signed message. Returns the signer's pubkey if valid.
        """
        assert self._stub, "Not connected — call connect() first"
        request = ln.VerifyMessageRequest(
            msg=message.encode("utf-8"),
            signature=signature,
        )
        response = self._stub.VerifyMessage(request, metadata=self._metadata())
        return {
            "valid": response.valid,
            "pubkey": response.pubkey,
        }


# Singleton instance
lnd_client = LndClient()
