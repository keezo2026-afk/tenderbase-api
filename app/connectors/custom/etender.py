"""National Treasury eTender (South Africa) connector — OCDS release parser.

Status: **verified against the live service on 2026-09-03.** The endpoint
contract below was read from the publisher's own swagger document and a real
response was parsed end to end.

The live contract
-----------------
``GET https://ocds-api.etenders.gov.za/api/OCDSReleases``

===============  ==========================================================
``PageNumber``   1-based page index.
``PageSize``     Max 1000 in a browser; the publisher notes larger values
                 (~20000) work for non-browser clients.
``dateFrom``     ISO date, inclusive.
``dateTo``       ISO date, inclusive.
===============  ==========================================================

Responses are OCDS 1.1 release packages with a ``releases`` array and a
``links.next`` cursor. ``GET /api/OCDSReleases/release/{ocid}`` returns a single
release. Licence: PDDL 1.0 (open data).

Background
----------
The South African National Treasury / Office of the Chief Procurement Officer
publishes eTender data using the Open Contracting Data Standard (OCDS). The
transparency portal documents bulk downloads and a paginated "Release API"
(see https://data.etenders.gov.za/Home/LearnMore, and the OCP data registry
entry for "South Africa: National Treasury" which lists
``https://ocds-api.etenders.gov.za/swagger/index.html`` as the retrieval
endpoint). Documentation reviewed: 2026-09-02.

What this connector does
------------------------
It parses **OCDS release packages** — a published open standard, not a guessed
private format — and maps ``tender`` objects onto TenderBase raw items. The
endpoint path, query parameters and pagination style are supplied through
source configuration (``base_url`` + ``listing_paths``), so no speculative URL
is compiled into the code.

Publisher deviations from the OCDS spec
---------------------------------------
Handled explicitly, because following the standard alone mis-parses this feed:

* contacts are on ``tender.contactPerson`` (with ``telephoneNumber``) rather
  than ``procuringEntity.contactPoint`` (with ``telephone``);
* ``procurementMethod`` is "open" for nearly every release, so RFQs are only
  identifiable from the free-text ``procurementMethodDetails``;
* attachments are served from ``/home/Download?blobName=<uuid>``, making the URL
  path "Download" for every document — the real filename comes from the OCDS
  ``title`` or the ``downloadedFileName`` query parameter;
* ``tender.briefingSession`` is a publisher extension whose "no session"
  sentinel is the .NET zero date ``0001-01-01``;
* ``tender.province`` and ``tender.deliveryLocation`` are extensions; they are
  preserved in the raw payload rather than mapped onto columns.

Known limitations
-----------------
* Rate limits are undocumented; the source's ``rate_limit_per_minute`` should be
  set conservatively until observed behaviour justifies otherwise.
* Only the ``tender`` stage is mapped; ``awards`` and ``contracts`` are
  preserved in the raw payload for a later awards feature.
* Buyer names are mapped to ``organization``; municipality resolution happens
  in the normalizer via the municipality-name matcher, and stays ``NULL`` when
  no confident match exists.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.http import guess_format
from app.connectors.registry import register_connector
from app.enums import ConnectorType, ProcurementType
from app.errors import ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.text import clean_text
from app.utils.urls import filename_from_url, is_http_url, normalize_url

#: OCDS ``tender.mainProcurementCategory`` / ``procurementMethod`` hints mapped
#: onto TenderBase procurement types. Unknown values fall back to TENDER.
_OCDS_TYPE_HINTS: dict[str, ProcurementType] = {
    "open": ProcurementType.TENDER,
    "selective": ProcurementType.TENDER,
    "limited": ProcurementType.RFQ,
    "direct": ProcurementType.RFQ,
}

#: ``procurementMethodDetails`` is free text and is the only field that tells an
#: RFQ apart from an open tender in the SA feed (``procurementMethod`` is almost
#: always "open"). Longest/most specific phrases first.
_METHOD_DETAIL_HINTS: dict[str, tuple[str, ...]] = {
    "RFQ": ("REQUEST FOR QUOTATION", "QUOTATION", "RFQ"),
    "RFP": ("REQUEST FOR PROPOSAL", "PROPOSAL", "RFP"),
    "RFI": ("REQUEST FOR INFORMATION", "RFI"),
    "EOI": ("EXPRESSION OF INTEREST", "EOI"),
    "RFB": ("REQUEST FOR BID", "RFB"),
}

_OCDS_STATUS_MAP = {
    "planning": "UNKNOWN",
    "planned": "UNKNOWN",
    "active": "OPEN",
    "cancelled": "CANCELLED",
    "unsuccessful": "CLOSED",
    "complete": "AWARDED",
    "withdrawn": "CANCELLED",
}


@register_connector()
class ETenderOCDSConnector(ProcurementConnector):
    """Parses OCDS release packages from the National Treasury eTender API."""

    key = "custom.etender_ocds"
    name = "National Treasury eTender (OCDS)"
    connector_type = ConnectorType.CUSTOM
    #: Verified against the live service on 2026-09-03: the swagger document at
    #: ocds-api.etenders.gov.za/swagger/v1/swagger.json declares
    #: ``GET /api/OCDSReleases`` with PageNumber/PageSize/dateFrom/dateTo, and a
    #: real response was parsed end to end. ``listing_paths`` is still required
    #: configuration — the date window is an operator decision, not a default.
    production_ready = True
    status_note = (
        "Verified against the live OCDS API on 2026-09-03 "
        "(GET /api/OCDSReleases, PageNumber/PageSize/dateFrom/dateTo, "
        "links.next pagination). Run `python -m scripts.verify_source <id>` "
        "after configuring a source to confirm reachability from your network."
    )
    version = "0.2.0"
    description = """
    Parses Open Contracting Data Standard (OCDS) release packages published by
    the South African National Treasury eTender / transparency portal. The
    endpoint and paging parameters are configuration-driven; the parser itself
    follows the OCDS 1.1 release schema. Not yet verified against live traffic.
    """
    config_schema = {
        "listing_paths": (
            "list[str] — OCDS release endpoints. Supports the placeholders "
            "{date_from} and {date_to}, which are substituted at discovery time "
            "with a rolling window ending today (the API requires both)."
        ),
        "lookback_days": "int — width of the rolling {date_from}..{date_to} window (default 30)",
        "releases_path": "str — dotted path to the releases array (default 'releases')",
        "next_link_path": "str — dotted path to the next-page link (default 'links.next')",
        "max_pages": "int — pagination safety limit (default 5)",
    }

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        paths = source.get("listing_paths")
        if not paths:
            raise ParseError(
                "custom.etender_ocds requires 'listing_paths' in the source configuration; "
                "no endpoint is hard-coded because the live API contract must be verified "
                "by an operator.",
                details={"source": source.name},
            )

        # The API rejects requests without dateFrom/dateTo, so a literal window
        # baked into config would silently go stale. Substituting a rolling
        # window keeps a stored source correct on every future run.
        today = utcnow().date()
        window = {
            "date_to": today.isoformat(),
            "date_from": (today - timedelta(days=self._lookback_days(source))).isoformat(),
        }

        targets = []
        for path in paths:
            try:
                resolved = str(path).format(**window)
            except (KeyError, IndexError, ValueError) as exc:
                raise ParseError(
                    "Unsupported placeholder in listing_paths; only {date_from} and "
                    "{date_to} are available.",
                    details={"source": source.name, "path": path, "error": str(exc)},
                ) from exc
            targets.append(
                DiscoveryTarget(url=normalize_url(resolved, base=source.base_url), kind="listing")
            )
        return targets

    def _lookback_days(self, source: SourceContext) -> int:
        try:
            days = int(source.get("lookback_days", 30))
        except (TypeError, ValueError):
            return 30
        return min(max(days, 1), 365)

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        if self.fetcher is None:  # pragma: no cover
            raise ParseError("No fetcher configured for connector")
        return await self.fetcher.fetch(
            target.url, source=source, target=target, headers={"Accept": "application/json"}
        )

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(
                "eTender response was not valid JSON", details={"url": response.url}
            ) from exc

        releases = payload.get("releases") if isinstance(payload, dict) else payload
        if isinstance(releases, dict):
            releases = [releases]
        if not isinstance(releases, list):
            raise ParseError(
                "OCDS payload did not contain a 'releases' array", details={"url": response.url}
            )

        items: list[RawItem] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            item = self._map_release(source, response, release)
            if item is not None:
                items.append(item)
        return items

    def _map_release(
        self, source: SourceContext, response: FetchResult, release: dict[str, Any]
    ) -> RawItem | None:
        tender = release.get("tender")
        if not isinstance(tender, dict):
            return None

        title = clean_text(tender.get("title")) or clean_text(release.get("title"))
        if not title:
            return None

        buyer = release.get("buyer") or {}
        parties = release.get("parties") or []
        buyer_name = clean_text(buyer.get("name")) or _first_party_name(parties, "buyer")

        tender_period = tender.get("tenderPeriod") or {}
        enquiry_period = tender.get("enquiryPeriod") or {}
        value = tender.get("value") or {}

        fields: dict[str, Any] = {
            "title": title,
            "description": clean_text(tender.get("description")),
            "reference_number": clean_text(tender.get("id")) or clean_text(release.get("ocid")),
            "external_id": clean_text(release.get("ocid")) or clean_text(release.get("id")),
            "organization": buyer_name,
            "published_at": release.get("date") or tender_period.get("startDate"),
            "closing_at": tender_period.get("endDate"),
            "procurement_type": self._procurement_type(tender),
            "status": _OCDS_STATUS_MAP.get(str(tender.get("status") or "").lower(), "UNKNOWN"),
            "estimated_value": value.get("amount"),
            "currency": value.get("currency"),
            "submission_method": _join(tender.get("submissionMethod")),
            "submission_url": _first_http(tender.get("submissionMethodDetails")),
            "enquiry_deadline": enquiry_period.get("endDate"),
        }

        # The SA feed carries a non-standard ``tender.briefingSession`` object.
        # Its "no session" sentinel is the .NET zero date, which must not be
        # mistaken for a real briefing on 1 January year 1.
        briefing = tender.get("briefingSession")
        if isinstance(briefing, dict) and briefing.get("isSession"):
            date = briefing.get("date")
            if isinstance(date, str) and not date.startswith("0001-01-01"):
                fields["briefing_date"] = date
            venue = clean_text(briefing.get("venue"))
            if venue and venue.upper() != "N/A":
                fields["briefing_location"] = venue
            fields["briefing_required"] = bool(briefing.get("compulsory"))

        # Briefing / site-meeting information is carried in OCDS milestones.
        for milestone in tender.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            label = f"{milestone.get('title', '')} {milestone.get('description', '')}".lower()
            if any(word in label for word in ("briefing", "site meeting", "site inspection")):
                fields["briefing_date"] = milestone.get("dueDate") or milestone.get("dateMet")
                fields["briefing_location"] = clean_text(milestone.get("description"))
                fields["briefing_required"] = True
                break

        # Contacts: the standard puts these on ``procuringEntity.contactPoint``
        # with ``telephone``. The live SA feed instead publishes a top-level
        # ``tender.contactPerson`` using ``telephoneNumber``. Verified against
        # ocds-api.etenders.gov.za on 2026-09-03; both shapes are read so the
        # connector keeps working if the publisher moves to the standard one.
        contact = tender.get("procuringEntity") or _party(parties, "procuringEntity") or {}
        contact_point = (contact.get("contactPoint") if isinstance(contact, dict) else None) or {}
        if not contact_point and isinstance(tender.get("contactPerson"), dict):
            contact_point = tender["contactPerson"]
        if contact_point:
            fields["contact_name"] = clean_text(contact_point.get("name"))
            fields["contact_email"] = clean_text(contact_point.get("email"))
            fields["contact_phone"] = clean_text(
                contact_point.get("telephone") or contact_point.get("telephoneNumber")
            )

        detail_url = _first_http(tender.get("documents"), key="url") or response.url

        return RawItem(
            source_url=detail_url,
            fields={k: v for k, v in fields.items() if v not in (None, "")},
            documents=self._documents(tender, base_url=response.url),
            raw_payload={
                "ocid": release.get("ocid"),
                "id": release.get("id"),
                "date": release.get("date"),
                "tag": release.get("tag"),
                "initiationType": release.get("initiationType"),
                "buyer": buyer,
                # Publisher extensions to OCDS. Not mapped onto columns — the
                # normalizer resolves geography from organization names — but
                # kept because they are the only location signal in the feed.
                "province": release.get("tender", {}).get("province"),
                "deliveryLocation": release.get("tender", {}).get("deliveryLocation"),
                # Preserved for a future awards/contracts feature.
                "awards": release.get("awards"),
                "contracts": release.get("contracts"),
            },
            parser_metadata={
                "connector": self.key,
                "connector_version": self.version,
                "standard": "OCDS",
                "listing_url": response.url,
            },
            observed_at=utcnow(),
        )

    def _procurement_type(self, tender: dict[str, Any]) -> str:
        """Prefer the specific label over the coarse method.

        ``procurementMethod`` is a small OCDS codelist: nearly every SA release
        says ``open``, which would flatten genuine RFQs into TENDER. The free
        text in ``procurementMethodDetails`` ("Request for Quotation") is what
        actually distinguishes them, so it is consulted first.
        """
        details = str(tender.get("procurementMethodDetails") or "").upper()
        for candidate, phrases in _METHOD_DETAIL_HINTS.items():
            if any(phrase in details for phrase in phrases):
                return candidate

        method = str(tender.get("procurementMethod") or "").lower()
        if mapped := _OCDS_TYPE_HINTS.get(method):
            return str(mapped)
        return str(ProcurementType.TENDER)

    def _documents(self, tender: dict[str, Any], *, base_url: str) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        seen: set[str] = set()
        for document in tender.get("documents") or []:
            if not isinstance(document, dict):
                continue
            url = document.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            try:
                absolute = normalize_url(url, base=base_url)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            # eTender serves attachments from /home/Download?blobName=<uuid>&
            # downloadedFileName=<real name>, so the URL path is literally
            # "Download" for every document. The OCDS ``title`` carries the real
            # filename; fall back to the query string, then the path.
            filename = (
                _filename_from_title(document.get("title"))
                or _filename_from_query(absolute)
                or filename_from_url(absolute)
            )
            candidates.append(
                DocumentCandidate(
                    source_url=absolute,
                    filename=filename,
                    title=clean_text(document.get("title")),
                    mime_type=document.get("format"),
                    document_format=guess_format(filename),
                )
            )
        return candidates


#: Extensions worth trusting from a title/query string. Anything else is treated
#: as prose, not a filename.
_FILENAME_SUFFIXES = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
    ".csv",
    ".rtf",
    ".txt",
    ".ppt",
    ".pptx",
)


def _looks_like_filename(value: str) -> bool:
    return value.lower().endswith(_FILENAME_SUFFIXES)


def _filename_from_title(title: Any) -> str | None:
    """The OCDS ``title`` is the original filename in this publisher's feed."""
    cleaned = clean_text(title)
    if cleaned and _looks_like_filename(cleaned):
        return cleaned.strip().replace("/", "_").replace("\\", "_")
    return None


def _filename_from_query(url: str) -> str | None:
    """Recover ``?downloadedFileName=...`` from an eTender download link."""
    from urllib.parse import parse_qs, unquote, urlparse

    query = parse_qs(urlparse(url).query)
    for key in ("downloadedFileName", "fileName", "filename"):
        for candidate in query.get(key, []):
            value = unquote(candidate).strip()
            if value and _looks_like_filename(value):
                return value.replace("/", "_").replace("\\", "_")
    return None


def _party(parties: list[Any], role: str) -> dict[str, Any] | None:
    for party in parties:
        if isinstance(party, dict) and role in (party.get("roles") or []):
            return party
    return None


def _first_party_name(parties: list[Any], role: str) -> str | None:
    party = _party(parties, role)
    return clean_text(party.get("name")) if party else None


def _join(value: Any) -> str | None:
    if isinstance(value, list):
        joined = ", ".join(str(v) for v in value if v)
        return joined or None
    return clean_text(value) if value else None


def _first_http(value: Any, key: str = "url") -> str | None:
    """Return the first http(s) URL found in a string, list or list of dicts."""
    if isinstance(value, str):
        return value if is_http_url(value) else None
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and is_http_url(entry.get(key, "")):
                return str(entry[key])
            if isinstance(entry, str) and is_http_url(entry):
                return entry
    return None
