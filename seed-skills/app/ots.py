"""OpenTimestamps stamping for the opentimestamps skill.

Submits a digest to FIXED, well-known calendar servers (never user-supplied) and
returns a serialized .ots detached timestamp. The proof is PENDING: it commits the
digest to the calendars now; Bitcoin confirmation (and a complete proof) follows
hours later via `ots upgrade`. ``_submit_to_calendars`` is the single point the
tests monkeypatch, keeping the suite offline.

Needs the ``timestamps`` extra (opentimestamps).
"""

from __future__ import annotations

from opentimestamps.calendar import RemoteCalendar
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.serialize import BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

from app.registry import SkillError

# Fixed public calendar servers, tried in turn. Never user-supplied.
_CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://alice.btc.calendar.opentimestamps.org",
)


def _submit_to_calendars(digest: bytes) -> Timestamp:
    """Submit the digest to the fixed calendars; merge their pending attestations."""
    timestamp = Timestamp(digest)
    submitted = 0
    for url in _CALENDARS:
        try:
            calendar_timestamp = RemoteCalendar(url).submit(digest)
            timestamp.merge(calendar_timestamp)
            submitted += 1
        except Exception:
            continue  # try the next calendar
    if submitted == 0:
        raise SkillError("no OpenTimestamps calendar reachable")
    return timestamp


def stamp_digest(digest: bytes) -> bytes:
    """Serialized .ots detached-timestamp bytes for a 32-byte sha256 digest.

    Blocking (calendar HTTP + serialize) — call it off the event loop.
    """
    timestamp = _submit_to_calendars(digest)
    detached = DetachedTimestampFile(OpSHA256(), timestamp)
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes()
