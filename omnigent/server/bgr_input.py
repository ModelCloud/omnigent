"""Shared durable-membership fence after normal session authorization."""

import asyncio

from omnigent.stores.bgr_store import BgrStore


async def check_bgr_session_input(
    app_state: object, session_id: str, *, restart: bool = False
) -> None:
    store: BgrStore | None = getattr(app_state, "bgr_store", None)
    if store is not None:
        await asyncio.to_thread(store.check_session_runtime_input, session_id, restart=restart)
