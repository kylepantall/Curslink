from __future__ import annotations

import asyncio

from curslink.protocol import MAX_FRAME_BYTES, Message, ProtocolError, decode


async def read_message(reader: asyncio.StreamReader) -> Message | None:
    """Next frame, or ``None`` on a clean EOF."""
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise ProtocolError("connection closed mid-frame") from exc
        return None
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError("frame too large") from exc
    return decode(line)


STREAM_LIMIT = MAX_FRAME_BYTES
