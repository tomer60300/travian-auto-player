"""WebSocket connection manager with per-user channels and JWT authentication."""

import asyncio
import logging
from typing import Optional

import jwt
from fastapi import WebSocket, status

from travian_api.web.auth import decode_access_token
from travian_api.web.sessions import session_manager

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections keyed by user_id.

    Each user can have multiple concurrent WebSocket connections
    (e.g., one for farm loop, one for build queue).  Connections are
    stored under a unique ``ws_key`` (channel + object id) so that
    multiple browser tabs on the same channel don't evict each other.
    """

    def __init__(self):
        # {user_id: {channel: WebSocket}}
        self._connections: dict[int, dict[str, WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def authenticate(
        self,
        websocket: WebSocket,
        *,
        require_travian_session: bool = True,
    ) -> Optional[int]:
        """Authenticate a WebSocket connection via query parameter token.

        Expected URL: ws://host/ws/path?token=<JWT>
        Returns user_id on success, None on failure.
        Must accept() before close() per ASGI spec.

        Args:
            websocket: The WebSocket connection.
            require_travian_session: If True (default), reject the connection
                when the user has no active Travian session.  Set to False for
                endpoints like /ws/logs and /ws/sessions that only need JWT auth.
        """
        token = websocket.query_params.get("token")
        if not token:
            await websocket.accept()
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
            return None

        try:
            payload = decode_access_token(token)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            await websocket.accept()
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
            return None

        user_id = payload["user_id"]

        # Verify user has active Travian session (when required)
        if require_travian_session and session_manager.get(user_id) is None:
            await websocket.accept()
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION, reason="No active Travian session"
            )
            return None

        return user_id

    async def connect(self, websocket: WebSocket, user_id: int, channel: str):
        """Accept and register a WebSocket connection.

        Each connection gets a unique key ``channel_<id(ws)>`` so that
        multiple tabs on the same logical channel coexist without evicting
        each other.  The key is stored on ``websocket._ws_channel_key`` for
        later retrieval by :meth:`disconnect`.
        """
        await websocket.accept()
        # Use unique key per connection so different tabs don't replace each other
        ws_key = f"{channel}_{id(websocket)}"
        async with self._lock:
            if user_id not in self._connections:
                self._connections[user_id] = {}
            self._connections[user_id][ws_key] = websocket
        # Store the key on the websocket so disconnect() can find it
        websocket._ws_channel_key = ws_key  # type: ignore[attr-defined]
        logger.info(f"WebSocket connected: user={user_id} channel={ws_key}")

    async def disconnect(self, user_id: int, channel: str, websocket: WebSocket = None):
        """Remove a WebSocket connection.

        Args:
            user_id: Owner of the connection.
            channel: Logical channel name (fallback key).
            websocket: If provided, the unique ``_ws_channel_key`` set by
                :meth:`connect` is used instead of *channel*.
        """
        ws_key = channel
        if websocket and hasattr(websocket, "_ws_channel_key"):
            ws_key = websocket._ws_channel_key
        async with self._lock:
            if user_id in self._connections:
                self._connections[user_id].pop(ws_key, None)
                if not self._connections[user_id]:
                    del self._connections[user_id]
        logger.info(f"WebSocket disconnected: user={user_id} channel={ws_key}")

    async def send_json(self, user_id: int, channel: str, data: dict):
        """Send a JSON message to a specific user's channel."""
        async with self._lock:
            ws = self._connections.get(user_id, {}).get(channel)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                await self.disconnect(user_id, channel)

    async def send_text(self, user_id: int, channel: str, text: str):
        """Send a text message to a specific user's channel."""
        async with self._lock:
            ws = self._connections.get(user_id, {}).get(channel)
        if ws:
            try:
                await ws.send_text(text)
            except Exception:
                await self.disconnect(user_id, channel)

    async def broadcast_to_user(self, user_id: int, data: dict):
        """Send a JSON message to all of a user's active connections."""
        async with self._lock:
            channels = dict(self._connections.get(user_id, {}))
        for channel, ws in channels.items():
            try:
                await ws.send_json(data)
            except Exception:
                await self.disconnect(user_id, channel)

    def is_connected(self, user_id: int, channel: str) -> bool:
        """Check if a user has an active connection matching the given key."""
        return channel in self._connections.get(user_id, {})


# Global singleton
ws_manager = ConnectionManager()
