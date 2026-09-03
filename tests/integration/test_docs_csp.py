"""Swagger UI and ReDoc must actually render in a browser.

The service-wide policy is ``default-src 'none'``, which is correct for JSON
responses and fatal for the two HTML pages: the browser fetches the document,
silently blocks the CDN bundle, and paints a blank page with a 200 status. That
failure is invisible to any test that only asserts on status codes, so these
tests assert on the policy the browser will actually enforce.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

CDN = "https://cdn.jsdelivr.net"


@pytest.mark.parametrize("path", ["/api/docs", "/api/redoc"])
async def test_docs_pages_allow_the_cdn_bundle(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200

    csp = response.headers["content-security-policy"]
    assert f"script-src 'self' {CDN}" in csp
    assert f"style-src 'self' {CDN}" in csp
    # The blanket rule must not survive for scripts, or nothing renders.
    assert csp.startswith("default-src 'none'")


async def test_swagger_page_references_its_bundle(client: AsyncClient) -> None:
    """A 200 proves nothing on its own — the HTML must load the bundle."""
    body = (await client.get("/api/docs")).text
    assert "swagger-ui-bundle" in body


@pytest.mark.parametrize("path", ["/openapi.json", "/api/v1/health/live"])
async def test_non_docs_responses_keep_the_strict_policy(client: AsyncClient, path: str) -> None:
    """Widening the policy for two HTML pages must not leak to the API."""
    csp = (await client.get(path)).headers["content-security-policy"]
    assert csp == "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:"
    assert CDN not in csp


async def test_docs_pages_keep_the_other_security_headers(client: AsyncClient) -> None:
    """Only the CSP is relaxed; the rest of the hardening stays put."""
    headers = (await client.get("/api/docs")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
