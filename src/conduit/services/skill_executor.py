"""
Skill execution engine — webhook-based.

After a consumer pays for a skill, this service:
1. Verifies the payment settled (via payment preimage)
2. POSTs the input data + payment proof to the provider's endpoint_url
3. Returns the provider's response as the skill output
4. Updates the execution record with results, timing, and status
"""

import re
import time
from datetime import datetime, timezone

import httpx

from conduit.models.execution import ExecutionStatus
from conduit.services.url_safety import UnsafeURLError, resolve_and_validate


class SkillExecutionError(Exception):
    """Raised when skill execution fails."""

    def __init__(self, reason: str, status: ExecutionStatus = ExecutionStatus.FAILED):
        self.reason = reason
        self.status = status
        super().__init__(reason)


# Strip ANSI / C0 control bytes from provider-returned strings before we
# interpolate them into logs or MCP tool output. The MCP stdio transport
# is line-framed and ANSI escapes corrupt terminal logs; hostile providers
# could otherwise poison both.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]|\x1b\[[0-?]*[ -/]*[@-~]")


def _safe_excerpt(text: str, limit: int = 200) -> str:
    """Return a short, control-character-free excerpt of provider text."""
    if not text:
        return ""
    return _CONTROL_CHARS.sub("", text)[:limit]


async def execute_skill_webhook(
    endpoint_url: str,
    input_data: dict,
    payment_hash: str,
    payment_preimage: str,
    skill_name: str,
    execution_id: str,
    timeout_seconds: int = 30,
) -> dict:
    """
    Call the provider's webhook with the skill input and payment proof.

    The webhook receives a POST with JSON body:
    {
        "execution_id": "...",
        "skill_name": "...",
        "input_data": { ... },
        "payment_proof": {
            "payment_hash": "...",
            "payment_preimage": "..."
        }
    }

    Expected response: JSON with at minimum an "output" key.
    {
        "output": { ... },
        "metadata": { ... }  // optional
    }

    Returns the parsed response dict.
    Raises SkillExecutionError on failure.
    """
    # Refuse to talk to internal services. The payload below contains the
    # payment preimage — if we POST it to an attacker-chosen URL, we leak
    # bearer proof of payment AND turn Conduit into a generic SSRF proxy.
    #
    # H3: Resolve DNS once and connect to the validated IP to prevent
    # DNS rebinding attacks (where a hostile provider switches the DNS
    # record between our validation call and the actual connect).
    try:
        validated_url, hostname, resolved_ips = resolve_and_validate(endpoint_url)
    except UnsafeURLError as e:
        raise SkillExecutionError(
            f"Refusing to call provider endpoint: {e}",
            status=ExecutionStatus.FAILED,
        )

    if not resolved_ips:
        raise SkillExecutionError(
            f"Provider endpoint did not resolve to any IP: {endpoint_url}",
            status=ExecutionStatus.FAILED,
        )

    # Rewrite the URL to connect directly to the validated IP, passing
    # the original hostname via the Host header so TLS SNI works.
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(validated_url)
    pinned_ip = resolved_ips[0]
    # For IPv6, wrap in brackets
    ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned_url = urlunparse((
        parsed.scheme,
        f"{ip_host}:{port}",
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    payload = {
        "execution_id": str(execution_id),
        "skill_name": skill_name,
        "input_data": input_data,
        "payment_proof": {
            "payment_hash": payment_hash,
            "payment_preimage": payment_preimage,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    start_time = time.monotonic()

    try:
        # follow_redirects=False is httpx's default and we keep it explicit:
        # a 30x to an internal URL would otherwise bypass our SSRF check.
        # Connect to the pinned IP with the original hostname in Host/SNI.
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            verify=True,
        ) as client:
            response = await client.post(
                pinned_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Conduit-MCP/0.1.0",
                    "X-Conduit-Execution-ID": str(execution_id),
                    "Host": hostname,
                },
            )

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        if 300 <= response.status_code < 400:
            raise SkillExecutionError(
                f"Provider returned redirect (HTTP {response.status_code}); "
                f"redirects are not followed."
            )

        if response.status_code >= 500:
            raise SkillExecutionError(
                f"Provider server error (HTTP {response.status_code}): "
                f"{_safe_excerpt(response.text)}"
            )

        if response.status_code >= 400:
            raise SkillExecutionError(
                f"Provider rejected request (HTTP {response.status_code}): "
                f"{_safe_excerpt(response.text)}"
            )

        try:
            result = response.json()
        except Exception:
            raise SkillExecutionError(
                f"Provider returned invalid JSON: {_safe_excerpt(response.text)}"
            )

        # Normalize response — ensure "output" key exists
        if "output" not in result:
            # If the response is a flat dict, wrap it as the output
            result = {"output": result}

        result["execution_time_ms"] = elapsed_ms
        return result

    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        raise SkillExecutionError(
            f"Provider webhook timed out after {timeout_seconds}s"
        )

    except httpx.ConnectError as e:
        raise SkillExecutionError(
            f"Could not connect to provider: {_safe_excerpt(str(e))}"
        )

    except SkillExecutionError:
        raise  # Re-raise our own errors

    except Exception as e:
        raise SkillExecutionError(
            f"Unexpected error calling provider webhook: {_safe_excerpt(str(e))}"
        )
