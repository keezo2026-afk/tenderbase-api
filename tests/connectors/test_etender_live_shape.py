"""The eTender connector against the *live* publisher's actual JSON shape.

The existing fixture follows the OCDS specification. This one is a trimmed copy
of a genuine response from ``ocds-api.etenders.gov.za/api/OCDSReleases``
(retrieved 2026-09-03), which differs from the spec in four ways that each
caused a real mapping bug:

* contacts live on ``tender.contactPerson`` with ``telephoneNumber``, not on
  ``procuringEntity.contactPoint`` with ``telephone``;
* ``procurementMethod`` is "open" for nearly everything, so RFQs are only
  distinguishable via the free-text ``procurementMethodDetails``;
* attachment URLs are ``/home/Download?blobName=<uuid>``, so the URL path is
  literally "Download" for every document;
* ``briefingSession`` is a publisher extension whose "no session" sentinel is
  the .NET zero date ``0001-01-01``.

Spec-shaped input must keep working too — that is covered in
``test_source_connectors.py``; these tests pin the live shape down so a refactor
cannot quietly regress to "correct per the standard, wrong for this publisher".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.connectors.base import FetchResult, SourceContext
from app.connectors.custom.etender import ETenderOCDSConnector
from app.enums import ConnectorType

pytestmark = pytest.mark.connectors

FIXTURE = Path(__file__).parent.parent / "fixtures" / "etender_ocds_live_shape.json"
LISTING = "https://ocds-api.etenders.gov.za/api/OCDSReleases?PageNumber=1&PageSize=50"


def _source() -> SourceContext:
    return SourceContext(
        id="etender",
        name="National Treasury eTender (OCDS)",
        organization="National Treasury",
        base_url="https://ocds-api.etenders.gov.za",
        connector_type=ConnectorType.CUSTOM,
        connector_key="custom.etender_ocds",
        config={"listing_paths": ["/api/OCDSReleases?PageNumber=1&PageSize=50"]},
    )


async def _parse():
    connector = ETenderOCDSConnector()
    source = _source()
    target = (await connector.discover(source))[0]
    response = FetchResult(
        url=target.url,
        status_code=200,
        content=FIXTURE.read_bytes(),
        headers={"content-type": "application/json"},
        target=target,
    )
    return await connector.parse(source, response)


async def test_discover_uses_the_configured_endpoint() -> None:
    """No endpoint is hardcoded; it must come from source configuration."""
    targets = await ETenderOCDSConnector().discover(_source())
    assert [t.url for t in targets] == [LISTING]


async def test_parses_every_release() -> None:
    assert len(await _parse()) == 2


async def test_rfq_is_not_flattened_into_tender() -> None:
    """procurementMethod is "open" here; only the details field says RFQ."""
    rfq = (await _parse())[0]
    assert rfq.fields["procurement_type"] == "RFQ"


async def test_open_tender_still_maps_to_tender() -> None:
    tender = (await _parse())[1]
    assert tender.fields["procurement_type"] == "TENDER"


async def test_reads_the_publisher_specific_contact_block() -> None:
    """tender.contactPerson + telephoneNumber, not contactPoint + telephone."""
    fields = (await _parse())[0].fields
    assert fields["contact_name"] == "Pinky Moloi"
    assert fields["contact_email"] == "Pmoloi@dffe.gov.za"
    assert fields["contact_phone"] == "066-471-1335"


async def test_document_filename_survives_the_download_endpoint() -> None:
    """Every attachment URL path is "Download"; the real name is elsewhere."""
    documents = (await _parse())[0].documents
    assert len(documents) == 1
    assert documents[0].filename == "PFHM-329-009-2026-2027.pdf"
    assert str(documents[0].document_format) == "PDF"


async def test_zero_date_is_not_read_as_a_briefing() -> None:
    """0001-01-01 is a .NET sentinel, not a briefing in year 1."""
    fields = (await _parse())[0].fields
    assert "briefing_date" not in fields
    assert "briefing_location" not in fields


async def test_real_briefing_session_is_captured() -> None:
    fields = (await _parse())[1].fields
    assert fields["briefing_date"] == "2026-09-12T10:00:00Z"
    assert "KE Masinga Road" in fields["briefing_location"]
    assert fields["briefing_required"] is True


async def test_core_fields_are_mapped() -> None:
    fields = (await _parse())[0].fields
    assert fields["title"] == "RFQ0001253"
    assert fields["reference_number"] == "168797"
    assert fields["external_id"] == "ocds-9t57fa-168797"
    assert fields["organization"] == "Marine Living Resources Fund"
    assert fields["closing_at"] == "2026-09-14T11:00:00Z"
    assert fields["currency"] == "ZAR"


async def test_location_extensions_are_preserved_not_invented() -> None:
    """province/deliveryLocation are non-standard: keep them, map nothing."""
    payload = (await _parse())[0].raw_payload
    assert payload["province"] == "Western Cape"
    assert "ARNISTON" in payload["deliveryLocation"]


async def test_awards_and_contracts_are_retained_for_later() -> None:
    payload = (await _parse())[0].raw_payload
    assert payload["awards"] == []
    assert payload["contracts"] == []


async def test_release_without_documents_yields_none() -> None:
    """A works tender with no attachments must not fabricate one."""
    assert (await _parse())[1].documents == []


def test_fixture_matches_the_published_envelope() -> None:
    """Guards the fixture itself against drifting away from the real feed."""
    payload = json.loads(FIXTURE.read_text())
    assert payload["version"] == "1.1"
    assert payload["publisher"]["name"] == "National Treasury (South Africa)"
    assert "releases" in payload
    assert payload["links"]["next"].startswith("https://ocds-api.etenders.gov.za/")


async def test_date_window_placeholders_are_substituted() -> None:
    """The API rejects requests without dateFrom/dateTo, so a stored source
    must resolve a fresh window on every run rather than a stale literal."""
    from datetime import timedelta

    from app.utils.dates import utcnow

    source = _source()
    source.config = {
        "listing_paths": [
            "/api/OCDSReleases?PageNumber=1&PageSize=100&dateFrom={date_from}&dateTo={date_to}"
        ],
        "lookback_days": 7,
    }
    today = utcnow().date()
    url = (await ETenderOCDSConnector().discover(source))[0].url
    assert f"dateTo={today.isoformat()}" in url
    assert f"dateFrom={(today - timedelta(days=7)).isoformat()}" in url


async def test_lookback_defaults_and_is_clamped() -> None:
    source = _source()
    source.config = {"listing_paths": ["/api/OCDSReleases?dateFrom={date_from}"]}
    connector = ETenderOCDSConnector()
    assert connector._lookback_days(source) == 30

    source.config["lookback_days"] = 9999
    assert connector._lookback_days(source) == 365
    source.config["lookback_days"] = "not a number"
    assert connector._lookback_days(source) == 30


async def test_unknown_placeholder_is_rejected_clearly() -> None:
    from app.errors import ParseError

    source = _source()
    source.config = {"listing_paths": ["/api/OCDSReleases?x={nope}"]}
    with pytest.raises(ParseError, match="placeholder"):
        await ETenderOCDSConnector().discover(source)


async def test_paths_without_placeholders_are_left_alone() -> None:
    source = _source()
    source.config = {"listing_paths": ["/api/OCDSReleases?dateFrom=2026-01-01&dateTo=2026-02-01"]}
    url = (await ETenderOCDSConnector().discover(source))[0].url
    assert url.endswith("/api/OCDSReleases?dateFrom=2026-01-01&dateTo=2026-02-01")


class _StubFetcher:
    """Serves canned pages so the pagination walk can be tested offline."""

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url, *, source=None, target=None, headers=None):
        self.calls.append(url)
        from app.connectors.base import DiscoveryTarget

        return FetchResult(
            url=url,
            status_code=200,
            content=json.dumps(self.pages[url]).encode(),
            headers={"content-type": "application/json"},
            target=target or DiscoveryTarget(url=url, kind="listing"),
        )


def _release(ocid: str, title: str) -> dict:
    return {
        "ocid": ocid,
        "id": f"{ocid}-2026-09-03",
        "date": "2026-09-03T00:00:00Z",
        "tender": {
            "id": ocid.split("-")[-1],
            "title": title,
            "status": "active",
            "tenderPeriod": {"endDate": "2026-09-30T11:00:00Z"},
            "procurementMethodDetails": "Open Tender",
        },
        "buyer": {"id": "1", "name": "Test Buyer"},
    }


def _page(releases: list[dict], next_url: str | None) -> dict:
    payload: dict = {"version": "1.1", "releases": releases}
    if next_url:
        payload["links"] = {"next": next_url}
    return payload


async def _run_with(pages: dict[str, dict], config: dict) -> tuple[list, _StubFetcher]:
    source = _source()
    source.config = config
    connector = ETenderOCDSConnector()
    fetcher = _StubFetcher(pages)
    connector.fetcher = fetcher  # type: ignore[assignment]
    items = [item async for item in connector.run(source)]
    return items, fetcher


P1 = "https://ocds-api.etenders.gov.za/api/OCDSReleases?PageNumber=1"
P2 = "https://ocds-api.etenders.gov.za/api/OCDSReleases?PageNumber=2"
P3 = "https://ocds-api.etenders.gov.za/api/OCDSReleases?PageNumber=3"


async def test_pagination_follows_links_next_to_the_end() -> None:
    """A single date window spans many pages; page 1 alone silently loses data."""
    pages = {
        P1: _page([_release("ocds-a-1", "Page one tender")], P2),
        P2: _page([_release("ocds-b-2", "Page two tender")], P3),
        P3: _page([_release("ocds-c-3", "Page three tender")], None),
    }
    items, fetcher = await _run_with(pages, {"listing_paths": ["/api/OCDSReleases?PageNumber=1"]})
    assert [i.fields["title"] for i in items] == [
        "Page one tender",
        "Page two tender",
        "Page three tender",
    ]
    assert fetcher.calls == [P1, P2, P3]


async def test_pagination_stops_at_max_pages() -> None:
    pages = {
        P1: _page([_release("ocds-a-1", "One")], P2),
        P2: _page([_release("ocds-b-2", "Two")], P3),
        P3: _page([_release("ocds-c-3", "Three")], None),
    }
    items, fetcher = await _run_with(
        pages, {"listing_paths": ["/api/OCDSReleases?PageNumber=1"], "max_pages": 2}
    )
    assert len(items) == 2
    assert fetcher.calls == [P1, P2]


async def test_self_referential_cursor_does_not_loop_forever() -> None:
    """A cursor pointing back at a fetched page must terminate the walk."""
    pages = {P1: _page([_release("ocds-a-1", "Only")], P1)}
    items, fetcher = await _run_with(pages, {"listing_paths": ["/api/OCDSReleases?PageNumber=1"]})
    assert len(items) == 1
    assert fetcher.calls == [P1]


async def test_missing_next_link_ends_cleanly() -> None:
    pages = {P1: _page([_release("ocds-a-1", "Only")], None)}
    items, fetcher = await _run_with(pages, {"listing_paths": ["/api/OCDSReleases?PageNumber=1"]})
    assert len(items) == 1
    assert fetcher.calls == [P1]


async def test_max_pages_is_clamped_and_defaulted() -> None:
    source = _source()
    connector = ETenderOCDSConnector()
    source.config = {}
    assert connector._max_pages(source) == 5
    source.config = {"max_pages": 0}
    assert connector._max_pages(source) == 1
    source.config = {"max_pages": "junk"}
    assert connector._max_pages(source) == 5


async def test_releases_path_is_honoured() -> None:
    """The documented releases_path option was previously ignored."""
    source = _source()
    source.config = {
        "listing_paths": ["/api/OCDSReleases"],
        "releases_path": "data.records",
    }
    connector = ETenderOCDSConnector()
    target = (await connector.discover(source))[0]
    payload = {"data": {"records": [_release("ocds-x-9", "Nested tender")]}}
    response = FetchResult(
        url=target.url,
        status_code=200,
        content=json.dumps(payload).encode(),
        headers={},
        target=target,
    )
    items = await connector.parse(source, response)
    assert [i.fields["title"] for i in items] == ["Nested tender"]
