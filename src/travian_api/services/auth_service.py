"""Authentication service for Travian API."""

from __future__ import annotations

import json
import os
import time
from base64 import b64decode
from pathlib import Path
from typing import Optional

from ..clients.http_client import HttpClient
from ..config import Settings
from ..exceptions import AuthError, TravianError
from ..models.auth import LoginRequest, LoginResponse, AuthStatus, AuthState, Village


class AuthService:
    """Service for handling authentication with Travian server."""
    
    def __init__(self, http_client: HttpClient, settings: Settings):
        self.http_client = http_client
        self.settings = settings
        self._auth_state: Optional[AuthState] = None
        self._jwt_cache_path = Path(settings.jwt_cache_path) if settings.jwt_cache_path else None
        
        # Set up re-auth callback
        self.http_client.set_auth_callback(self._handle_reauth)
        
    async def login(self) -> AuthState:
        """
        Perform 2-step authentication flow.
        
        Returns:
            AuthState with JWT and player info
            
        Raises:
            AuthError: If login fails
        """
        try:
            # Clear any existing cookies
            self.http_client.clear_cookies()
            
            # Step 1: POST login credentials
            login_payload = {
                "name": self.settings.username,
                "password": self.settings.password,
                "w": "1920:1080",
                "mobileOptimizations": False
            }
            
            response = await self.http_client.post_json("/api/v1/auth/login", login_payload, skip_reauth=True)
            
            if "redirectTo" not in response or "code" not in response:
                raise AuthError("Invalid login response - missing redirectTo or code")
                
            redirect_url = response["redirectTo"]
            
            # Step 2: GET redirect URL to exchange code for JWT cookie (don't follow redirects)
            await self.http_client.get_html(redirect_url, follow_redirects=False, skip_reauth=True)
            
            # JWT should now be set in cookies
            jwt = await self.get_jwt()
            if not jwt:
                raise AuthError("JWT cookie not set after authentication")
                
            # Decode JWT to get expiry and village ID
            jwt_payload = self._decode_jwt_payload(jwt)
            properties = jwt_payload.get("properties", {})
            
            self._auth_state = AuthState(
                jwt=jwt,
                expires_at=jwt_payload.get("exp", int(time.time()) + 7200),
                village_id=properties.get("did", 0),
            )
            
            # Enrich with player data from GraphQL
            try:
                player_data = await self.http_client.post_json("/api/v1/graphql", {
                    "query": "{ ownPlayer { name tribeId villages { id name x y isMainVillage } } }"
                })
                own_player = player_data.get("data", {}).get("ownPlayer", {})
                if own_player:
                    self._auth_state.player_name = own_player.get("name", "Unknown")
                    self._auth_state.tribe_id = own_player.get("tribeId", 0)
                    villages_data = own_player.get("villages", [])
                    if villages_data:
                        self._auth_state.villages = [
                            Village(
                                id=v["id"],
                                name=v.get("name", ""),
                                x=v.get("x", 0),
                                y=v.get("y", 0),
                                is_main_village=v.get("isMainVillage", False),
                            )
                            for v in villages_data if v.get("id")
                        ]
                        # Set village_id to main village if not already set from JWT
                        if not self._auth_state.village_id:
                            main = next((v for v in self._auth_state.villages if v.is_main_village), None)
                            if main:
                                self._auth_state.village_id = main.id
                            elif self._auth_state.villages:
                                self._auth_state.village_id = self._auth_state.villages[0].id
            except Exception:
                pass  # GraphQL enrichment is best-effort
            
            # Cache JWT if configured
            if self._jwt_cache_path:
                await self._cache_jwt(self._auth_state)

            # Stealth: post-login warm-up sequence (loads pages a human would visit)
            if self.settings.stealth:
                try:
                    village_id = self._auth_state.village_id or None
                    await self.http_client.navigator.warm_up(village_id=village_id)
                except Exception:
                    pass  # Warm-up failure shouldn't block login

            # Resolve dynamic X-Version now that we're authenticated
            try:
                await self.http_client.try_resolve_x_version()
            except Exception:
                pass  # Fallback to config value is fine

            return self._auth_state
            
        except Exception as e:
            if isinstance(e, (AuthError, TravianError)):
                raise
            raise AuthError(f"Login failed: {e}") from e
    
    async def get_jwt(self) -> Optional[str]:
        """
        Get current JWT token from cookies.
        
        Returns:
            JWT string if available, None otherwise
        """
        cookies = self.http_client.get_cookies()
        
        # Look for JWT in various possible cookie names
        jwt_cookie_names = ["jwt", "JWT", "token", "authToken", "session", "travian_session"]
        
        for cookie_name in jwt_cookie_names:
            if cookie_name in cookies:
                return cookies[cookie_name]
                
        # Check all cookies for JWT-like patterns (contains dots and base64)
        for name, value in cookies.items():
            if isinstance(value, str) and "." in value and len(value) > 50:
                try:
                    # Try to decode as JWT
                    parts = value.split(".")
                    if len(parts) == 3:
                        # Verify it's actually a JWT by trying to decode payload
                        self._decode_jwt_payload(value)
                        return value
                except:
                    continue
                    
        return None
    
    async def ensure_authenticated(self) -> None:
        """
        Ensure we have a valid JWT token, re-authenticating if needed.
        
        Raises:
            AuthError: If authentication fails
        """
        # Try to load cached JWT first
        if self._jwt_cache_path and not self._auth_state:
            await self._load_cached_jwt()
            
        # Check if current JWT is valid
        if self._auth_state and self._is_jwt_valid(self._auth_state.jwt):
            return
            
        # Re-authenticate
        await self.login()
    
    async def _handle_reauth(self) -> None:
        """Handle re-authentication callback from HTTP client."""
        await self.login()
    
    def _decode_jwt_payload(self, jwt: str) -> dict:
        """
        Decode JWT payload without verification.
        
        Args:
            jwt: JWT token string
            
        Returns:
            Decoded payload dictionary
        """
        try:
            parts = jwt.split(".")
            if len(parts) != 3:
                return {}
                
            # Decode payload (add padding if needed)
            payload = parts[1]
            padding = len(payload) % 4
            if padding:
                payload += "=" * (4 - padding)
                
            decoded = b64decode(payload)
            return json.loads(decoded.decode("utf-8"))
            
        except Exception:
            return {}
    
    def _is_jwt_valid(self, jwt: str) -> bool:
        """
        Check if JWT is still valid (not expired).
        
        Args:
            jwt: JWT token string
            
        Returns:
            True if valid, False otherwise
        """
        payload = self._decode_jwt_payload(jwt)
        exp = payload.get("exp")
        
        if not exp:
            # No expiry, assume valid for now
            return True
            
        # Add 60 second buffer
        return int(time.time()) + 60 < exp
    
    async def _cache_jwt(self, auth_state: AuthState) -> None:
        """Cache JWT to file for reuse."""
        if not self._jwt_cache_path:
            return
            
        try:
            cache_data = {
                "jwt": auth_state.jwt,
                "expires_at": auth_state.expires_at,
                "player_name": auth_state.player_name,
                "tribe_id": auth_state.tribe_id,
                "village_id": auth_state.village_id,
                "villages": [v.model_dump() for v in auth_state.villages],
                "cached_at": int(time.time())
            }
            
            self._jwt_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._jwt_cache_path.write_text(json.dumps(cache_data, indent=2))
            try:
                os.chmod(self._jwt_cache_path, 0o600)
            except OSError:
                pass  # Windows ACL may not support chmod
            
        except Exception:
            # Don't fail on cache errors
            pass
    
    async def _load_cached_jwt(self) -> None:
        """Load JWT from cache file if available and valid."""
        if not self._jwt_cache_path or not self._jwt_cache_path.exists():
            return
            
        try:
            cache_data = json.loads(self._jwt_cache_path.read_text())
            jwt = cache_data.get("jwt", "")
            
            if jwt and self._is_jwt_valid(jwt):
                villages = [
                    Village(**v) for v in cache_data.get("villages", [])
                ]
                self._auth_state = AuthState(
                    jwt=jwt,
                    expires_at=cache_data.get("expires_at", 0),
                    player_name=cache_data.get("player_name", "Unknown"),
                    tribe_id=cache_data.get("tribe_id", 0),
                    village_id=cache_data.get("village_id", 0),
                    villages=villages,
                )
                
                # Set cookie in HTTP client
                self.http_client.set_cookie("jwt", jwt)
                
        except Exception:
            # Don't fail on cache errors, just ignore
            pass
