"""
Wallet backend protocol — abstracts Lightning wallet operations.

Conduit supports multiple Lightning wallet backends:
  - LND (direct gRPC to your own node)
  - NWC (Nostr Wallet Connect — works with Alby, Primal, Zeus, etc.)
  - Hosted (future — Lightning Linq runs the node)

All marketplace, security, and MCP code calls these methods through
the protocol. Swapping backends requires zero changes outside this
module and the config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class InvoiceResponse:
    """Result of creating a Lightning invoice."""
    payment_request: str
    payment_hash: str
    add_index: int = 0


@dataclass
class PaymentResponse:
    """Result of sending a Lightning payment."""
    payment_hash: str
    preimage: str
    fee_msats: int
    status: str  # "SUCCEEDED" or "FAILED"
    failure_reason: str | None = None


@dataclass
class NodeInfo:
    """Basic info about the connected Lightning wallet/node."""
    pubkey: str
    alias: str
    num_active_channels: int = 0
    num_peers: int = 0
    block_height: int = 0
    synced_to_chain: bool = True
    version: str = ""
    backend_type: str = ""  # "lnd", "nwc", "hosted"


@runtime_checkable
class WalletBackend(Protocol):
    """Protocol that all Lightning wallet backends must implement.

    Methods map 1:1 to the operations Conduit needs:
    - Creating invoices (to receive payment for skills)
    - Paying invoices (to pay for skills)
    - Looking up invoice status (to verify settlement)
    - Decoding invoices (to extract amount/description before paying)
    - Node info and balance (for status display)
    - Message signing/verification (for provider verification)
    """

    @property
    def is_connected(self) -> bool:
        """Whether the backend is ready to handle requests."""
        ...

    def connect(self) -> None:
        """Establish connection to the wallet backend."""
        ...

    def disconnect(self) -> None:
        """Close the connection."""
        ...

    def get_info(self) -> NodeInfo:
        """Get basic info about the connected wallet/node."""
        ...

    def get_balance(self) -> dict[str, int]:
        """Get wallet balances in satoshis.

        Returns a dict with at minimum:
          - channel_balance_sats: available Lightning balance
          - onchain_total_sats: on-chain balance (0 if not applicable)
        """
        ...

    def create_invoice(
        self, amount_msats: int, memo: str = "", expiry: int = 3600
    ) -> InvoiceResponse:
        """Create a new Lightning invoice to receive payment.

        Args:
            amount_msats: Amount in millisatoshis
            memo: Human-readable description
            expiry: Seconds until invoice expires

        Returns:
            InvoiceResponse with payment_request and payment_hash
        """
        ...

    def pay_invoice(
        self, payment_request: str, max_fee_msats: int = 10000
    ) -> PaymentResponse:
        """Pay a Lightning invoice.

        Args:
            payment_request: BOLT-11 encoded invoice
            max_fee_msats: Maximum routing fee in millisatoshis

        Returns:
            PaymentResponse with preimage on success
        """
        ...

    def decode_invoice(self, payment_request: str) -> dict:
        """Decode a BOLT-11 invoice without paying it.

        Returns a dict with at minimum:
          - destination: recipient pubkey
          - payment_hash: hash for this payment
          - amount_sats: amount in satoshis
          - amount_msats: amount in millisatoshis
          - description: invoice memo
          - expiry: seconds until expiry
          - timestamp: creation timestamp
        """
        ...

    def lookup_invoice(self, payment_hash: str) -> dict:
        """Look up an invoice by payment hash.

        Returns a dict with at minimum:
          - settled: bool
          - amount_msats: int
          - preimage: hex string (if settled)
        """
        ...

    def sign_message(self, message: str) -> str:
        """Sign a message with the wallet's key. Returns signature string."""
        ...

    def verify_message(self, message: str, signature: str) -> dict:
        """Verify a signed message. Returns {"valid": bool, "pubkey": str}."""
        ...
