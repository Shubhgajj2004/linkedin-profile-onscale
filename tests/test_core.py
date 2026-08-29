import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import main
from app.linkedin import AuthenticationError, LinkedInClient, LinkedInPool
from app.parser import parse_profile
from app.urls import parse_profile_url


class UrlTests(unittest.TestCase):
    def test_canonicalizes_profile_url(self):
        self.assertEqual(
            parse_profile_url("https://in.linkedin.com/in/ada-lovelace/?trk=test"),
            ("ada-lovelace", "https://www.linkedin.com/in/ada-lovelace/"),
        )

    def test_rejects_non_profile_and_lookalike_hosts(self):
        for url in (
            "https://evil.example/in/ada",
            "https://linkedin.com.evil.example/in/ada",
            "https://www.linkedin.com/company/openai",
            "http://www.linkedin.com/in/ada",
            "https://www.linkedin.com/in/ada/details/experience",
            "https://www.linkedin.com:bad/in/ada",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_profile_url(url)


class ParserTests(unittest.TestCase):
    def test_parses_modern_normalized_payload(self):
        payload = {
            "included": [
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "entityUrn": "urn:li:fsd_profile:1",
                    "publicIdentifier": "ada-lovelace",
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "headline": "Programmer",
                    "summary": "Analytical Engine notes",
                    "geoLocationName": "London",
                    "profilePicture": {
                        "displayImageReference": {
                            "vectorImage": {
                                "rootUrl": "https://media.example/",
                                "artifacts": [
                                    {
                                        "width": 100,
                                        "height": 100,
                                        "fileIdentifyingUrlPathSegment": "small.jpg",
                                    },
                                    {
                                        "width": 400,
                                        "height": 400,
                                        "fileIdentifyingUrlPathSegment": "large.jpg",
                                    },
                                ],
                            }
                        }
                    },
                },
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                    "title": "Mathematician",
                    "companyName": "Self-employed",
                    "dateRange": {"start": {"year": 1842}},
                },
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.PositionGroup",
                    "companyName": "Must not become another job",
                },
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Skill",
                    "name": "Mathematics",
                },
            ]
        }
        profile = parse_profile(
            payload,
            "ada-lovelace",
            "https://www.linkedin.com/in/ada-lovelace/",
        )
        self.assertEqual(profile.name, "Ada Lovelace")
        self.assertEqual(profile.profile_image_url, "https://media.example/large.jpg")
        self.assertEqual(profile.experience[0].title, "Mathematician")
        self.assertEqual(profile.skills, ["Mathematics"])

    def test_parses_legacy_profile_view(self):
        payload = {
            "profile": {
                "firstName": "Grace",
                "lastName": "Hopper",
                "locationName": "New York",
            },
            "educationView": {
                "elements": [
                    {
                        "schoolName": "Yale University",
                        "degreeName": "PhD",
                        "timePeriod": {"startDate": {"year": 1930}},
                    }
                ]
            },
            "languageView": {
                "elements": [{"name": "English", "proficiency": "NATIVE_OR_BILINGUAL"}]
            },
        }
        profile = parse_profile(
            payload,
            "grace-hopper",
            "https://www.linkedin.com/in/grace-hopper/",
        )
        self.assertEqual(profile.education[0].school, "Yale University")
        self.assertEqual(profile.languages[0].name, "English")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def test_openapi_documents_success_and_errors(self):
        schema = main.app.openapi()
        profile = schema["paths"]["/v1/profile"]["post"]
        health = schema["paths"]["/health"]["get"]

        self.assertEqual(
            set(profile["responses"]),
            {"200", "404", "422", "429", "502", "503"},
        )
        self.assertEqual(set(health["responses"]), {"200", "401", "422"})
        self.assertIn(
            "examples", schema["components"]["schemas"]["ProfileRequest"]["properties"]["url"]
        )
        documented = str(schema)
        for message in (
            "Profile fetched.",
            "invalid or missing X-API-Key",
            "Profile was not found or is not visible to this session",
            "body must contain a valid url",
            "invalid LinkedIn profile URL",
            "use an HTTPS linkedin.com profile URL",
            "URL must match https://www.linkedin.com/in/<identifier>",
            "LinkedIn rate-limited this session",
            "LinkedIn could not be reached",
            "LinkedIn returned HTTP 500",
            "LinkedIn returned a non-JSON response",
            "LinkedIn returned an unexpected response",
            "LinkedIn returned no recognizable profile",
            "LinkedIn returned an unsupported profile shape",
            "LinkedIn session cookies are not configured",
            "LinkedIn session is invalid, expired, or checkpointed",
            "No healthy LinkedIn sessions are available",
        ):
            self.assertIn(message, documented)

    async def test_health_requires_its_api_key(self):
        result = {
            "total_accounts": 2,
            "healthy_accounts": [{"account": "one", "location": "London, UK"}],
            "unhealthy_accounts": [],
        }
        check_health = AsyncMock(return_value=result)
        transport = httpx.ASGITransport(app=main.app)
        with (
            patch.object(main, "health_api_key", "secret"),
            patch.object(main.pool, "check_health", check_health),
        ):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                self.assertEqual((await client.get("/health")).status_code, 401)
                response = await client.get("/health", headers={"X-API-Key": "secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        check_health.assert_awaited_once()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def test_loads_account_proxy_pair_from_json(self):
        config = """[{"account":"london-one","proxy_address":"proxy.example","proxy_port":6754,"proxy_username":"user@x","proxy_password":"pass:word","location":"London, UK","li_at":"session","jsessionid":"ajax:1","user_agent":"test-agent"}]"""
        with patch.dict(os.environ, {"LINKEDIN_ACCOUNTS_JSON": config}, clear=True):
            pool = LinkedInPool.from_env()

        self.assertEqual(pool.configured_count, 1)
        self.assertEqual(pool.clients[0].account, "london-one")
        self.assertEqual(pool.clients[0].location, "London, UK")
        self.assertEqual(pool.clients[0].user_agent, "test-agent")
        self.assertEqual(
            pool.clients[0].proxy,
            "http://user%40x:pass%3Aword@proxy.example:6754",
        )

    def test_rejects_account_without_user_agent(self):
        config = """[{"account":"london-one","proxy_address":"proxy.example","proxy_port":6754,"proxy_username":"user","proxy_password":"pass","location":"London, UK","li_at":"session","jsessionid":"ajax:1"}]"""
        with (
            patch.dict(os.environ, {"LINKEDIN_ACCOUNTS_JSON": config}, clear=True),
            self.assertRaisesRegex(RuntimeError, "missing: user_agent"),
        ):
            LinkedInPool.from_env()

    async def test_pool_uses_each_account_in_turn(self):
        seen = []

        def transport(name):
            def handler(_: httpx.Request):
                seen.append(name)
                return httpx.Response(
                    200,
                    json={
                        "included": [
                            {
                                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                                "publicIdentifier": "ada-lovelace",
                            }
                        ]
                    },
                )

            return httpx.MockTransport(handler)

        pool = LinkedInPool(
            [
                LinkedInClient(
                    transport("one"),
                    li_at="one",
                    jsessionid="ajax:1",
                    user_agent="test-agent",
                ),
                LinkedInClient(
                    transport("two"),
                    li_at="two",
                    jsessionid="ajax:2",
                    user_agent="test-agent",
                ),
            ]
        )
        for _ in range(3):
            await pool.fetch("ada-lovelace")

        self.assertEqual(seen, ["one", "two", "one"])

    async def test_expired_account_is_quarantined_and_next_account_is_used(self):
        def expired(_: httpx.Request):
            return httpx.Response(401)

        def healthy(_: httpx.Request):
            return httpx.Response(
                200,
                json={
                    "included": [
                        {
                            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                            "publicIdentifier": "ada-lovelace",
                        }
                    ]
                },
            )

        expired_client = LinkedInClient(
            httpx.MockTransport(expired),
            li_at="expired",
            jsessionid="ajax:expired",
            user_agent="test-agent",
            account="expired-account",
        )
        healthy_client = LinkedInClient(
            httpx.MockTransport(healthy),
            li_at="healthy",
            jsessionid="ajax:healthy",
            user_agent="test-agent",
            account="healthy-account",
        )
        pool = LinkedInPool([expired_client, healthy_client])

        document = await pool.fetch("ada-lovelace")

        self.assertEqual(document.endpoint, "dash:101")
        self.assertFalse(expired_client.healthy)
        self.assertEqual(pool.healthy_count, 1)

    async def test_health_check_does_not_retry_quarantined_accounts(self):
        calls = {"expired": 0, "healthy": 0}

        def expired(_: httpx.Request):
            calls["expired"] += 1
            return httpx.Response(403)

        def healthy(_: httpx.Request):
            calls["healthy"] += 1
            return httpx.Response(200, json={"plainId": "1"})

        pool = LinkedInPool(
            [
                LinkedInClient(
                    httpx.MockTransport(expired),
                    li_at="expired",
                    jsessionid="ajax:expired",
                    user_agent="test-agent",
                    account="expired-account",
                ),
                LinkedInClient(
                    httpx.MockTransport(healthy),
                    li_at="healthy",
                    jsessionid="ajax:healthy",
                    user_agent="test-agent",
                    account="healthy-account",
                ),
            ]
        )

        first = await pool.check_health()
        second = await pool.check_health()

        self.assertEqual(calls, {"expired": 1, "healthy": 2})
        self.assertEqual(first["total_accounts"], 2)
        self.assertEqual(
            first["healthy_accounts"],
            [{"account": "healthy-account", "location": None}],
        )
        self.assertEqual(first["unhealthy_accounts"][0]["account"], "expired-account")
        self.assertNotIn("ajax:expired", str(first))
        self.assertEqual(len(second["healthy_accounts"]), 1)

    async def test_cancelled_health_wait_reopens_pool(self):
        def healthy(_: httpx.Request):
            return httpx.Response(
                200,
                json={
                    "included": [
                        {
                            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                            "publicIdentifier": "ada-lovelace",
                        }
                    ]
                },
            )

        pool = LinkedInPool(
            [
                LinkedInClient(
                    httpx.MockTransport(healthy),
                    li_at="healthy",
                    jsessionid="ajax:healthy",
                    user_agent="test-agent",
                )
            ]
        )
        held = await pool._acquire()
        check = asyncio.create_task(pool.check_health())
        await asyncio.sleep(0)

        check.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await check
        await pool._release(held)

        document = await asyncio.wait_for(pool.fetch("ada-lovelace"), 0.1)
        self.assertEqual(document.endpoint, "dash:101")

    async def test_falls_back_from_stale_decoration(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(str(request.url))
            if len(seen) < 3:
                return httpx.Response(400, json={"message": "stale decoration"})
            return httpx.Response(
                200,
                json={"profile": {"firstName": "Ada", "lastName": "Lovelace"}},
            )

        with patch.dict(
            os.environ,
            {"LINKEDIN_LI_AT": "session", "LINKEDIN_JSESSIONID": "ajax:1"},
        ):
            document = await LinkedInClient(
                httpx.MockTransport(handler), user_agent="test-agent"
            ).fetch("ada-lovelace")

        self.assertEqual(document.endpoint, "profileView")
        self.assertIsInstance(document.payload, dict)
        self.assertEqual(len(seen), 3)

    async def test_modern_response_avoids_extra_upstream_calls(self):
        modern = {
            "included": [
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "publicIdentifier": "ada-lovelace",
                    "firstName": "Ada",
                }
            ]
        }
        calls = 0

        def handler(_: httpx.Request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=modern)

        with patch.dict(
            os.environ,
            {"LINKEDIN_LI_AT": "session", "LINKEDIN_JSESSIONID": "ajax:1"},
        ):
            document = await LinkedInClient(
                httpx.MockTransport(handler), user_agent="test-agent"
            ).fetch("ada-lovelace")

        self.assertEqual(document.endpoint, "dash:101")
        self.assertEqual(calls, 1)

    async def test_does_not_retry_an_expired_session(self):
        calls = 0

        def handler(_: httpx.Request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                302, headers={"location": "https://www.linkedin.com/login"}
            )

        with (
            patch.dict(
                os.environ,
                {"LINKEDIN_LI_AT": "session", "LINKEDIN_JSESSIONID": "ajax:1"},
            ),
            self.assertRaises(AuthenticationError),
        ):
            await LinkedInClient(
                httpx.MockTransport(handler), user_agent="test-agent"
            ).fetch("ada-lovelace")

        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
