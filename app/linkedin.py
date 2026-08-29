import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class LinkedInError(Exception):
    code = "upstream_error"
    status_code = 502


class CredentialsError(LinkedInError):
    code = "linkedin_session_unavailable"
    status_code = 503


class AuthenticationError(LinkedInError):
    code = "linkedin_session_expired"
    status_code = 503


class ProfileNotFound(LinkedInError):
    code = "profile_not_found"
    status_code = 404


class RateLimited(LinkedInError):
    code = "linkedin_rate_limited"
    status_code = 429


@dataclass(slots=True)
class VoyagerDocument:
    payload: dict[str, Any]
    endpoint: str


class LinkedInClient:
    base_url = "https://www.linkedin.com/voyager/api"
    decorations = (
        "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101",
        "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-91",
    )

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        li_at: str | None = None,
        jsessionid: str | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        location: str | None = None,
        account: str = "default",
    ) -> None:
        self.li_at = (li_at or os.getenv("LINKEDIN_LI_AT", "")).strip()
        self.jsessionid = (
            (jsessionid or os.getenv("LINKEDIN_JSESSIONID", "")).strip().strip('"')
        )
        self.user_agent = (user_agent or "").strip()
        self.timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
        self.transport = transport
        self.proxy = proxy
        self.location = location
        self.account = account
        self.healthy = True

    @property
    def configured(self) -> bool:
        return bool(self.li_at and self.jsessionid and self.user_agent)

    async def fetch(self, identifier: str) -> VoyagerDocument:
        if not self.configured:
            raise CredentialsError("LinkedIn session cookies are not configured")

        candidates = [
            (
                "/identity/dash/profiles",
                {
                    "q": "memberIdentity",
                    "memberIdentity": identifier,
                    "decorationId": decoration,
                },
                f"dash:{decoration.rsplit('-', 1)[-1]}",
            )
            for decoration in self.decorations
        ]
        candidates.append(
            (f"/identity/profiles/{identifier}/profileView", None, "profileView")
        )

        last_error: LinkedInError | None = None
        async with self._client() as client:
            for path, params, label in candidates:
                try:
                    payload = await self._get(client, path, params)
                except (AuthenticationError, RateLimited):
                    raise
                except LinkedInError as exc:
                    last_error = exc
                    continue
                if self._contains_profile(payload, identifier):
                    return VoyagerDocument(payload, label)

        if isinstance(last_error, ProfileNotFound):
            raise last_error
        raise last_error or LinkedInError("LinkedIn returned no recognizable profile")

    async def check(self) -> None:
        if not self.configured:
            raise CredentialsError("LinkedIn session cookies are not configured")
        async with self._client() as client:
            await self._get(client, "/me", None)

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "csrf-token": self.jsessionid,
            "cookie": f'li_at={self.li_at}; JSESSIONID="{self.jsessionid}"',
            "user-agent": self.user_agent,
            "x-li-lang": "en_US",
            "x-restli-protocol-version": "2.0.0",
        }
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=False,
            transport=self.transport,
            proxy=self.proxy,
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str] | None,
    ) -> dict[str, Any]:
        try:
            response = await client.get(path, params=params)
        except httpx.TransportError as exc:
            raise LinkedInError("LinkedIn could not be reached") from exc

        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            raise AuthenticationError(
                "LinkedIn session is invalid, expired, or checkpointed"
            )
        if response.status_code == 429:
            raise RateLimited("LinkedIn rate-limited this session")
        if response.status_code in {404, 410}:
            raise ProfileNotFound(
                "Profile was not found or is not visible to this session"
            )
        if response.status_code >= 400:
            raise LinkedInError(f"LinkedIn returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LinkedInError("LinkedIn returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise LinkedInError("LinkedIn returned an unexpected response")
        return payload

    @staticmethod
    def _contains_profile(payload: dict[str, Any], identifier: str) -> bool:
        if isinstance(payload.get("profile"), dict):
            return True
        return any(
            isinstance(item, dict)
            and item.get("publicIdentifier") == identifier
            and "profile" in str(item.get("$type", "")).lower()
            for item in payload.get("included", [])
        )


class LinkedInPool:
    """A fair queue keeps each LinkedIn session on its own proxy."""

    def __init__(self, clients: list[LinkedInClient]) -> None:
        self.clients = clients
        self.available = deque(clients)
        self.condition = asyncio.Condition()
        self.health_lock = asyncio.Lock()
        self.checking_health = False

    @classmethod
    def from_env(cls) -> "LinkedInPool":
        raw = os.getenv("LINKEDIN_ACCOUNTS_JSON", "").strip()
        if not raw:
            client = LinkedInClient()
            return cls([client] if client.configured else [])

        try:
            accounts = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LINKEDIN_ACCOUNTS_JSON must be valid JSON") from exc
        if not isinstance(accounts, list):
            raise TypeError("LINKEDIN_ACCOUNTS_JSON must be a JSON array")

        clients = []
        required = (
            "proxy_address",
            "proxy_port",
            "proxy_username",
            "proxy_password",
            "location",
            "li_at",
            "jsessionid",
            "user_agent",
        )
        for index, account in enumerate(accounts, start=1):
            if not isinstance(account, dict):
                raise RuntimeError(f"LinkedIn account {index} is incomplete")
            missing = [key for key in required if account.get(key) in (None, "")]
            if missing:
                raise RuntimeError(
                    f"LinkedIn account {index} is missing: {', '.join(missing)}"
                )
            try:
                port = int(account["proxy_port"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"LinkedIn account {index} has an invalid proxy port"
                ) from exc
            if not 1 <= port <= 65535:
                raise RuntimeError(
                    f"LinkedIn account {index} has an invalid proxy port"
                )

            username = quote(str(account["proxy_username"]), safe="")
            password = quote(str(account["proxy_password"]), safe="")
            proxy = f"http://{username}:{password}@{account['proxy_address']}:{port}"
            clients.append(
                LinkedInClient(
                    li_at=str(account["li_at"]),
                    jsessionid=str(account["jsessionid"]),
                    user_agent=str(account["user_agent"]),
                    proxy=proxy,
                    location=str(account["location"]),
                    account=str(account.get("account") or f"account-{index}"),
                )
            )
        return cls(clients)

    @property
    def configured_count(self) -> int:
        return len(self.clients)

    @property
    def healthy_count(self) -> int:
        return sum(client.healthy for client in self.clients)

    async def _acquire(self) -> LinkedInClient:
        async with self.condition:
            await self.condition.wait_for(
                lambda: (
                    (not self.checking_health and bool(self.available))
                    or self.healthy_count == 0
                )
            )
            if not self.available:
                raise CredentialsError("No healthy LinkedIn sessions are available")
            return self.available.popleft()

    async def _release(self, client: LinkedInClient) -> None:
        async with self.condition:
            self.available.append(client)
            self.condition.notify()

    async def _quarantine(self, client: LinkedInClient) -> None:
        async with self.condition:
            client.healthy = False
            self.condition.notify_all()
        logger.warning(
            "LinkedIn account %s (%s) requires login",
            client.account,
            client.location or "unknown location",
        )

    async def fetch(self, identifier: str) -> VoyagerDocument:
        attempts = self.healthy_count
        for _ in range(attempts):
            client = await self._acquire()
            try:
                document = await client.fetch(identifier)
            except AuthenticationError:
                await self._quarantine(client)
                continue
            except BaseException:
                await self._release(client)
                raise
            await self._release(client)
            return document
        raise CredentialsError("No healthy LinkedIn sessions are available")

    async def check_health(self) -> dict[str, Any]:
        async with self.health_lock:
            clients = []
            try:
                async with self.condition:
                    self.checking_health = True
                    await self.condition.wait_for(
                        lambda: len(self.available) == self.healthy_count
                    )
                    clients = list(self.available)
                    self.available.clear()

                for client in clients:
                    try:
                        await client.check()
                    except AuthenticationError:
                        await self._quarantine(client)
                    except LinkedInError:
                        pass
            finally:
                async with self.condition:
                    self.available.extend(
                        client for client in clients if client.healthy
                    )
                    self.checking_health = False
                    self.condition.notify_all()

        healthy = [client for client in self.clients if client.healthy]
        unhealthy = [client for client in self.clients if not client.healthy]
        return {
            "total_accounts": self.configured_count,
            "healthy_accounts": [
                {"account": client.account, "location": client.location}
                for client in healthy
            ],
            "unhealthy_accounts": [
                {
                    "account": client.account,
                    "location": client.location,
                    "jsessionid_hint": f"…{client.jsessionid[-4:]}",
                }
                for client in unhealthy
            ],
        }
