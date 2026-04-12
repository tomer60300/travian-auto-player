"""WebSocket connection manager with per-user channels and JWT authentication."""

import asyncio
import json
import logging
from typing import Optional

import jwt
from fastapi import WebSocket, WebSocketDisconnect, status

from travian_api.web.auth import decode_access_token
from travian_api.web.sessions import session_manager

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections keyed by user_id.

    Each user can have multiple concurrent WebSocket connections
    (e.g., one for farm loop, one for build queue).
    Connections are tagged with a 'channel' name for routing.
    """

    def __init__(self):
        # {user_id: {channel: WebSocket}}
        self._connections: dict[int, dict[str, WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def authenticate(self, websocket: WebSocket) -> Optional[int]:
        """Authenticate a WebSocket connection via query parameter token.

        Expected URL: ws://host/ws/path?token=<JWT>
        Returns user_id on success, None on failure.
        Must accept() before close() per ASGI spec.
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

        # Verify user has active Travian session
        if session_manager.get(user_id) is None:
            await websocket.accept()
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="No active Travian session")
            return None

        return user_id

    async def connect(self, websocket: WebSocket, user_id: int, channel: str):
        """Accept and register a WebSocket connection.

        Each connection gets a unique key (channel + ws_id) so multiple tabs
        can coexist on the same logical channel without killing each other.
        The actual storage key is returned and must be used for disconnect().
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

        Uses the unique ws_key stored on the websocket object during connect().
        Falls back to the channel name for backward compatibility.
        """
        ws_key = channel
        if websocket and hasattr(websocket, '_ws_channel_key'):
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
        """Send a JSON message to all of a user's channels."""
        async with self._lock:
            channels = dict(self._connections.get(user_id, {}))
        for channel, ws in channels.items():
            try:
                await ws.send_json(data)
            except Exception:
                await self.disconnect(user_id, channel)

    def is_connected(self, user_id: int, channel: str) -> bool:
        """Check if a user has an active connection on a channel."""
        return channel in self._connections.get(user_id, {})


# Global singleton
ws_manager = ConnectionManager()
