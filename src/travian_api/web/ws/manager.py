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
        Returns user_id on success, None on failure (closes the socket).
        """
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
            return None

        try:
            payload = decode_access_token(token)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
            return None

        user_id = payload["user_id"]

        # Verify user has active Travian session
        if session_manager.get(user_id) is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="No active Travian session")
            return None

        return user_id

    async def connect(self, websocket: WebSocket, user_id: int, channel: str):
        """Accept and register a WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            if user_id not in self._connections:
                self._connections[user_id] = {}
            # Close existing connection on same channel
            existing = self._connections[user_id].get(channel)
            if existing:
                try:
                    await existing.close(code=status.WS_1000_NORMAL_CLOSURE, reason="Replaced by new connection")
                except Exception:
                    logger.debug("Failed to close existing WS connection: user=%s channel=%s", user_id, channel, exc_info=True)
            self._connections[user_id][channel] = websocket
        logger.info(f"WebSocket connected: user={user_id} channel={channel}")

    async def disconnect(self, user_id: int, channel: str):
        """Remove a WebSocket connection."""
        async with self._lock:
            if user_id in self._connections:
                self._connections[user_id].pop(channel, None)
                if not self._connections[user_id]:
                    del self._connections[user_id]
        logger.info(f"WebSocket disconnected: user={user_id} channel={channel}")

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
