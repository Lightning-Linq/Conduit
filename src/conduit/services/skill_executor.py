"""
Skill execution engine — webhook-based.

After a consumer pays for a skill, this service:
1. Verifies the payment settled (via payment preimage)
2. POSTs the input data + payment proof to the provider's endpoint_url
3. Returns the provider's response as the skill output
4. Updates the execution record with results, timing, and status
"""

import time
from datetime import datetime, timezone

import httpx

from conduit.models.execution import ExecutionStatus


class SkillExecutionError(Exception):
    """Raised when skill execution fails."""

    def __init__(self, reason: str, status: ExecutionStatus = ExecutionStatus.FAILED):
        self.reason = reason
        self.status = status
        super().__init__(reason)


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
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                endpoint_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Conduit-MCP/0.1.0",
                    "X-Conduit-Execution-ID": str(execution_id),
                },
            )

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        if response.status_code >= 500:
            raise SkillExecutionError(
                f"Provider server error (HTTP {response.status_code}): {response.text[:200]}"
            )

        if response.status_code >= 400:
            raise SkillExecutionError(
                f"Provider rejected request (HTTP {response.status_code}): {response.text[:200]}"
            )

        try:
            result = response.json()
        except Exception:
            raise SkillExecutionError(
                f"Provider returned invalid JSON: {response.text[:200]}"
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
            f"Could not connect to provider at {endpoint_url}: {e}"
        )

    except SkillExecutionError:
        raise  # Re-raise our own errors

    except Exception as e:
        raise SkillExecutionError(
            f"Unexpected error calling provider webhook: {e}"
        )
