"""Offline tests for the application-wide authentication boundary."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.dependencies.auth import get_current_user, require_csrf
from app.config import get_settings

app = main_module.app


class SecurityRouteBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.api_prefix = get_settings().api_prefix.rstrip("/")

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()

    def _effective_routes(self):
        """Flatten direct and lazily included FastAPI routes."""

        for route in app.routes:
            if isinstance(route, APIRoute):
                yield route
                continue
            route_contexts = getattr(route, "effective_route_contexts", None)
            if route_contexts is not None:
                yield from route_contexts()

    def test_every_business_operation_uses_the_ordered_security_boundary(
        self,
    ) -> None:
        business_routes = [
            route
            for route in self._effective_routes()
            if route.path.startswith(f"{self.api_prefix}/")
            and not route.path.startswith(f"{self.api_prefix}/auth/")
        ]

        self.assertTrue(business_routes)
        for route in business_routes:
            with self.subTest(path=route.path, methods=route.methods):
                dependency_calls = [
                    dependency.call
                    for dependency in route.dependant.dependencies
                ]
                self.assertGreaterEqual(len(dependency_calls), 2)
                self.assertIs(dependency_calls[0], get_current_user)
                self.assertIs(dependency_calls[1], require_csrf)

    def test_anonymous_requests_cannot_reach_business_routes(self) -> None:
        for path in (
            f"{self.api_prefix}/leads",
            f"{self.api_prefix}/documents",
            f"{self.api_prefix}/emails",
            f"{self.api_prefix}/agent-runs",
            f"{self.api_prefix}/chat/sessions",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

        mutation = self.client.post(f"{self.api_prefix}/leads/sync")
        self.assertEqual(mutation.status_code, 401)
        self.assertEqual(mutation.headers["cache-control"], "no-store")
        self.assertEqual(mutation.headers["pragma"], "no-cache")

    def test_health_and_cors_preflight_remain_public(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        origin = get_settings().cors_allowed_origins[0]
        preflight = self.client.options(
            f"{self.api_prefix}/leads/sync",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "Content-Type, X-CSRF-Token, Idempotency-Key"
                ),
            },
        )

        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["access-control-allow-origin"], origin)
        self.assertEqual(
            preflight.headers["access-control-allow-credentials"],
            "true",
        )
        allowed_headers = preflight.headers[
            "access-control-allow-headers"
        ].lower()
        self.assertIn("x-csrf-token", allowed_headers)
        self.assertIn("idempotency-key", allowed_headers)

    def test_security_settings_are_validated_before_database_startup(self) -> None:
        calls: list[str] = []

        async def enter_lifespan() -> None:
            async with main_module.lifespan(app):
                pass

        with (
            patch.object(
                main_module,
                "validate_web_auth_settings",
                side_effect=lambda _: calls.append("security"),
            ),
            patch.object(
                main_module,
                "check_database_schema",
                side_effect=lambda: calls.append("database"),
            ),
        ):
            asyncio.run(enter_lifespan())

        self.assertEqual(calls, ["security", "database"])


if __name__ == "__main__":
    unittest.main()
