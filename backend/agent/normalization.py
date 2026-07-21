"""Deterministic adapters for EarlyBid lead records.

This module deliberately performs no provider calls.  It accepts either the
snake_case representation used by the backend or the display headers present
in the EarlyBid CSV export and produces the agent's typed, normalized input.
The caller's mapping is never mutated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, TypeVar

from .models import (
    AudienceType,
    Contact,
    EvidenceReference,
    NormalizedLead,
    ProjectStage,
)


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "section": ("section", "Section"),
    "project": ("project", "Project"),
    "location": ("location", "Location"),
    "state": ("state", "State"),
    "signal": ("signal", "Signal"),
    "intelligence": ("intelligence", "Intelligence"),
    "score": ("score", "Score"),
    "timing": ("timing", "Timing"),
    "next_step": ("next_step", "Next Step", "next step"),
    "awarded_to": ("awarded_to", "Awarded To", "awarded to"),
    "priority_reasons": (
        "priority_reasons",
        "Priority Reasons",
        "priority reasons",
    ),
    "summary": ("summary", "Summary"),
    "contacts": ("contacts", "Contacts"),
    "contact_email": ("contact_email", "Contact Email", "contact email"),
    "meeting_date": ("meeting_date", "Meeting Date", "meeting date"),
    "tags": ("tags", "Tags"),
    "url": ("url", "URL", "Url"),
}

_BLANK_MARKERS = {"", "n/a", "na", "none", "null", "nan", "-", "--"}
_DISPLAY_IDENTIFIER_RE = re.compile(
    r"^lead\s*(?:#|no\.?\s*|number\s+)\d+$", re.IGNORECASE
)
EARLYBID_NATURAL_ID_PREFIX = "earlybid-natural-v1:"
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.()-]*)?(?:\(?\d{3}\)?[\s.-]*)"
    r"\d{3}[\s.-]*\d{4}(?:\s*(?:x|ext\.?)\s*\d+)?(?!\d)",
    re.IGNORECASE,
)

_STATE_CODES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_VALID_STATE_CODES = frozenset(_STATE_CODES.values())

_STAGE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "planning": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bplanning\b",
            r"\bplanned\b",
            r"\bproposed\b",
            r"\bconcept(?:ual)?\b",
            r"\bschematic(?:\s+design)?\b",
            r"\bpre[- ]design\b",
            r"\bfeasibility\b",
            r"\bearly[- ]stage\b",
            r"\bdesign\s+intent\b",
        )
    ),
    "specification": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bspecification(?:s)?\b",
            r"\bspecif(?:y|ying|ied)\b",
            r"\bspec\s+(?:review|language|package)\b",
            r"\bdesign\s+development\b",
            r"\bconstruction\s+documents?\b",
            r"\bmaterial\s+selection\b",
            r"\btechnical\s+review\b",
            r"\bsubmittals?\b",
            r"\bCSI\b",
        )
    ),
    "procurement": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bprocurement\b",
            r"\bpurchas(?:e|ed|ing)\b",
            r"\bbid(?:ding|s)?\b",
            r"\btender(?:ing)?\b",
            r"\bRFQ\b",
            r"\brequest\s+for\s+quote\b",
            r"\bquot(?:e|ed|ing)\b",
            r"\baward(?:ed|ing)?\b",
            r"\bsupplier\s+selection\b",
            r"\border(?:ed|ing)?\b",
            r"\bconstruction\s+(?:underway|started|phase)\b",
            r"\binstallation\b",
        )
    ),
}

_AUDIENCE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "architect_specifier": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\barchitect(?:s|ural|ure)?\b",
            r"\bspecifier\b",
            r"\bdesign(?:er|\s+firm)\b",
            r"\bAIA\b",
            r"\bCSI\b",
        )
    ),
    "contractor_builder": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bgeneral\s+contractor\b",
            r"\bcontractor\b",
            r"\bbuilder\b",
            r"\bconstruction\s+manager\b",
            r"\bcarpenter\b",
            r"\binstaller\b",
        )
    ),
    "distributor_supplier": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bdistributor\b",
            r"\bsupplier\b",
            r"\bdealer\b",
            r"\blumber\s*yard\b",
            r"\bmerchant\b",
            r"\bbuyer\b",
            r"\bpurchasing\s+(?:agent|manager)\b",
        )
    ),
    "owner_developer": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bowner\b",
            r"\bdeveloper\b",
            r"\bmunicipal(?:ity|ities)?\b",
            r"\bpublic\s+agency\b",
        )
    ),
    "manufacturer_fabricator": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bmanufacturer\b",
            r"\bfabricator\b",
            r"\bmillwork(?:er|\s+shop)?\b",
            r"\bjoiner(?:y)?\b",
            r"\bwindow\s+(?:and\s+door\s+)?fabricator\b",
        )
    ),
    "facilities_property": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bproperty\s+manager\b",
            r"\bfacilit(?:y|ies)\s+manager\b",
            r"\bfacilities\s+director\b",
            r"\bbuilding\s+manager\b",
        )
    ),
}

_MATERIAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bThermory(?:\s*,?\s*or\s+similar)?\b",
        r"\bthermally\s+modified(?:\s+(?:wood|timber|decking))?\b",
        r"\bTimberTech\b",
        r"\bTrex\b",
        r"\bcomposite(?:\s+wood)?\s+decking\b",
        r"\bIp[eé](?:\s+decking)?\b",
        r"\bexterior[- ]grade\s+MDF\b",
        r"\bexterior\s+MDF\b",
        r"\bMDF\s+(?:panel(?:s)?|fascia|soffit)\b",
        r"\bgrey[- ]through[- ]the[- ]board\b",
        r"\bgr[ae]y(?:\s+finish(?:ed)?)?\s+(?:decking|siding|cladding)\b",
        r"\bpre[- ]gr[ae]yed(?:\s+(?:decking|siding|cladding))?\b",
        r"\bPVC\s+decking\b",
        r"\bhardwood\s+decking\b",
    )
)

_COMPETITOR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bThermory\b",
        r"\bTimberTech\b",
        r"\bTrex\b",
        r"\bAZEK\b",
        r"\bKebony\b",
        r"\bArbor\s+Wood\b",
    )
)

_EVIDENCE_FIELDS = (
    "project",
    "section",
    "signal",
    "intelligence",
    "timing",
    "next_step",
    "awarded_to",
    "priority_reasons",
    "summary",
    "tags",
)

_EnumT = TypeVar("_EnumT")


def _key_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip().casefold() in _BLANK_MARKERS


def _clean_text(value: Any) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return str(value).strip() or None


def _record_lookup(record: Mapping[str, Any]) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    for key, value in record.items():
        lookup.setdefault(_key_token(key), value)
    return lookup


def _get_value(
    record: Mapping[str, Any], lookup: Mapping[str, Any], *aliases: str
) -> Any:
    """Return the first nonblank alias, with exact-key precedence."""

    fallback: Any = None
    found = False
    for alias in aliases:
        if alias in record:
            value = record[alias]
            if not found:
                fallback, found = value, True
            if not _is_blank(value):
                return value
    for alias in aliases:
        token = _key_token(alias)
        if token in lookup:
            value = lookup[token]
            if not found:
                fallback, found = value, True
            if not _is_blank(value):
                return value
    return fallback


def _resolve_lead_id(
    record: Mapping[str, Any],
    lookup: Mapping[str, Any],
    *,
    identity_scope: str | None = None,
    project: Any = None,
    location: Any = None,
    state: Any = None,
) -> str:
    for alias in ("lead_id", "id", "external_id"):
        value = _get_value(record, lookup, alias)
        identifier = _clean_text(value)
        if identifier is None:
            continue
        if _DISPLAY_IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(
                f"{alias} is a display rank, not a stable lead identifier: {identifier!r}"
            )
        return identifier
    if identity_scope is not None:
        return derive_earlybid_natural_id(
            identity_scope=identity_scope,
            project=project,
            location=location,
            state=state,
        )
    raise ValueError("A stable lead identifier is required (lead_id, id, or external_id)")


def _parse_score(value: Any) -> float | None:
    if _is_blank(value) or isinstance(value, bool):
        return None
    try:
        score = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _parse_date(value: Any) -> date | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _clean_text(value)
    if not text:
        return None
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        pass
    for date_format in (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
        "%m-%d-%Y",
        "%d-%b-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _parse_tags(value: Any) -> list[str]:
    if _is_blank(value):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in {"[", "{"}:
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if decoded is not None and decoded != value:
                return _parse_tags(decoded)
        values: Iterable[Any] = re.split(r"[,;|\n]+", text)
    elif isinstance(value, Mapping):
        values = value.keys()
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = (value,)

    tags: list[str] = []
    seen: set[str] = set()
    for item in values:
        tag = _clean_text(item)
        if not tag:
            continue
        tag = tag.lstrip("#").strip()
        marker = tag.casefold()
        if tag and marker not in seen:
            seen.add(marker)
            tags.append(tag)
    return tags


def _mapping_value(value: Mapping[str, Any], *keys: str) -> Any:
    lookup = _record_lookup(value)
    return _get_value(value, lookup, *keys)


def _contact_from_mapping(value: Mapping[str, Any]) -> Contact | None:
    name = _clean_text(
        _mapping_value(value, "name", "full_name", "contact_name", "contact")
    )
    email = _clean_text(_mapping_value(value, "email", "email_address"))
    phone = _clean_text(_mapping_value(value, "phone", "telephone", "mobile"))
    title = _clean_text(_mapping_value(value, "title", "role", "job_title"))
    organization = _clean_text(
        _mapping_value(value, "organization", "company", "firm", "employer")
    )
    try:
        raw = json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(value)
    if not any((name, email, phone, title, organization)):
        return None
    return Contact(
        name=name,
        email=email.casefold() if email else None,
        phone=phone,
        title=title,
        organization=organization,
        raw=raw,
    )


def _contact_from_text(value: str) -> Contact | None:
    raw = value.strip()
    if not raw:
        return None
    email_match = _EMAIL_RE.search(raw)
    phone_match = _PHONE_RE.search(raw)
    email = email_match.group(0).casefold() if email_match else None
    phone = phone_match.group(0).strip() if phone_match else None

    remainder = _EMAIL_RE.sub(" ", raw)
    remainder = _PHONE_RE.sub(" ", remainder)
    remainder = re.sub(r"\b(?:email|e-mail|phone|tel|telephone)\s*:\s*", " ", remainder, flags=re.I)
    parts = [
        part.strip(" ,:-()[]")
        for part in re.split(r"\s*(?:,|\s[-–—]\s)\s*", remainder)
        if part.strip(" ,:-()[]")
    ]

    title_pattern = re.compile(
        r"\b(?:architect|specifier|designer|project manager|construction manager|"
        r"general contractor|contractor|builder|buyer|purchasing manager|owner|"
        r"developer|facilities manager|sales manager)\b",
        re.IGNORECASE,
    )
    organization_pattern = re.compile(
        r"\b(?:inc\.?|llc|ltd\.?|corp\.?|company|co\.?|architects?|design|"
        r"construction|builders?|studio|associates|group|agency|municipality)\b",
        re.IGNORECASE,
    )
    title = next((part for part in parts if title_pattern.search(part)), None)
    organization = next(
        (part for part in parts if part != title and organization_pattern.search(part)),
        None,
    )
    name = next(
        (
            part
            for part in parts
            if part not in {title, organization}
            and re.search(r"[A-Za-z]", part)
            and len(part.split()) <= 6
        ),
        None,
    )
    if not any((name, email, phone, title, organization)):
        return Contact(raw=raw)
    return Contact(
        name=name,
        email=email,
        phone=phone,
        title=title,
        organization=organization,
        raw=raw,
    )


def _parse_contacts(value: Any, fallback_email: Any = None) -> list[Contact]:
    pending: list[Any]
    if _is_blank(value):
        pending = []
    elif isinstance(value, Mapping):
        pending = [value]
    elif isinstance(value, str):
        text = value.strip()
        decoded: Any = None
        if text[:1] in {"[", "{"}:
            try:
                decoded = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                decoded = None
        if isinstance(decoded, list):
            pending = decoded
        elif isinstance(decoded, Mapping):
            pending = [decoded]
        else:
            pending = [part for part in re.split(r"[;|\n]+", text) if part.strip()]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        pending = list(value)
    else:
        pending = [value]

    contacts: list[Contact] = []
    for item in pending:
        if isinstance(item, Mapping):
            contact = _contact_from_mapping(item)
        else:
            text = _clean_text(item)
            contact = _contact_from_text(text) if text else None
        if contact is not None:
            contacts.append(contact)

    email = _clean_text(fallback_email)
    if email and _EMAIL_RE.fullmatch(email):
        email = email.casefold()
        if not any(contact.email and contact.email.casefold() == email for contact in contacts):
            if contacts and contacts[0].email is None:
                contacts[0] = contacts[0].model_copy(update={"email": email})
            else:
                contacts.append(Contact(email=email, raw=email))

    deduplicated: list[Contact] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for contact in contacts:
        key = tuple(
            (item or "").strip().casefold()
            for item in (
                contact.name,
                contact.email,
                contact.phone,
                contact.title,
                contact.organization,
            )
        )
        if key not in seen:
            seen.add(key)
            deduplicated.append(contact)
    return deduplicated


def _normalize_state(value: Any) -> str | None:
    state = _clean_text(value)
    if not state:
        return None
    state_without_zip = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", state).strip()
    if state_without_zip.upper() in _VALID_STATE_CODES:
        return state_without_zip.upper()
    return _STATE_CODES.get(state_without_zip.casefold())


def normalize_state_code(value: Any) -> str | None:
    """Return a canonical US state code for metadata comparisons."""

    return _normalize_state(value)


def _parse_location(location_value: Any, state_value: Any) -> tuple[str | None, str | None]:
    location = _clean_text(location_value)
    state = _normalize_state(state_value)
    city: str | None = None
    if not location:
        return city, state

    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) >= 2:
        inferred_state = _normalize_state(parts[-1])
        if inferred_state:
            state = state or inferred_state
            city = parts[-2] if len(parts) > 2 else parts[0]
        else:
            city = parts[0]
    else:
        state_match = re.match(
            r"^(?P<city>.+?)\s+(?P<state>[A-Za-z]{2}|[A-Za-z ]+?)(?:\s+\d{5}(?:-\d{4})?)?$",
            location,
        )
        if state_match:
            inferred_state = _normalize_state(state_match.group("state"))
            if inferred_state:
                state = state or inferred_state
                city = state_match.group("city").strip()
        if city is None:
            city = location
    return city or None, state


def _canonical_identity_text(value: Any) -> str | None:
    """Return the locale-independent text used by natural-key hashing."""

    text = _clean_text(value)
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized or None


def derive_earlybid_natural_id(
    *,
    identity_scope: str,
    project: Any,
    location: Any,
    state: Any = None,
) -> str:
    """Derive a versioned EarlyBid ID when the source supplies no stable ID.

    The source scope is normally ``reseller/client``. Only the stable natural
    key participates; mutable feed attributes such as score, URL, contacts,
    timing, and next step are deliberately excluded.
    """

    canonical_scope = _canonical_identity_text(identity_scope)
    canonical_project = _canonical_identity_text(project)
    canonical_location = _canonical_identity_text(location)
    if canonical_scope is None:
        raise ValueError("identity_scope must not be blank")
    if canonical_project is None:
        raise ValueError("Project is required to derive an EarlyBid lead identity")
    if canonical_location is None:
        raise ValueError("Location is required to derive an EarlyBid lead identity")

    canonical_state_input = _canonical_identity_text(state)
    city, state_code = _parse_location(canonical_location, canonical_state_input)
    canonical_city = _canonical_identity_text(city) or canonical_location
    canonical_state = _canonical_identity_text(state_code) or ""
    identity_components = [
        canonical_scope,
        canonical_project,
        canonical_city,
        canonical_state,
    ]
    serialized = json.dumps(
        identity_components,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{EARLYBID_NATURAL_ID_PREFIX}{hashlib.sha256(serialized).hexdigest()}"


def _enum_from_candidates(enum_type: type[_EnumT], *candidates: str) -> _EnumT:
    for candidate in candidates:
        try:
            return enum_type(candidate)  # type: ignore[call-arg,return-value]
        except (TypeError, ValueError):
            normalized = _key_token(candidate)
            for member in enum_type:  # type: ignore[union-attr]
                if _key_token(getattr(member, "name", "")) == normalized:
                    return member
    raise ValueError(f"{enum_type.__name__} has none of the values {candidates!r}")


def _text_from_value(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, Contact):
        return " ".join(
            item
            for item in (
                value.name,
                value.title,
                value.organization,
                value.raw,
            )
            if item
        )
    if isinstance(value, Mapping):
        return " ".join(_text_from_value(item) for item in value.values())
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return " ".join(_text_from_value(item) for item in value)
    return _clean_text(value) or ""


def determine_stage(values: Mapping[str, Any] | NormalizedLead) -> ProjectStage:
    """Infer a conservative project stage from explicit lead language."""

    if isinstance(values, Mapping):
        get_value = values.get
    else:
        get_value = lambda key, default=None: getattr(values, key, default)

    explicit = _clean_text(
        get_value("project_stage") or get_value("stage") or get_value("Project Stage")
    )
    if explicit:
        token = _key_token(explicit)
        explicit_map = {
            "planning": "planning",
            "plan": "planning",
            "specification": "specification",
            "specifying": "specification",
            "design": "specification",
            "procurement": "procurement",
            "bidding": "procurement",
            "bid": "procurement",
        }
        if token in explicit_map:
            return _enum_from_candidates(ProjectStage, explicit_map[token])

    field_weights = {
        "section": 3,
        "signal": 2,
        "intelligence": 1,
        "timing": 3,
        "next_step": 3,
        "priority_reasons": 1,
        "summary": 1,
        "tags": 2,
    }
    scores = {"planning": 0, "specification": 0, "procurement": 0}
    for field, weight in field_weights.items():
        text = _text_from_value(get_value(field))
        for stage, patterns in _STAGE_PATTERNS.items():
            scores[stage] += weight * sum(1 for pattern in patterns if pattern.search(text))

    awarded_to = _clean_text(get_value("awarded_to"))
    if awarded_to and not re.search(
        r"\b(?:not\s+(?:yet\s+)?awarded|unawarded|tbd|unknown|none)\b",
        awarded_to,
        re.IGNORECASE,
    ):
        scores["procurement"] += 4

    top_score = max(scores.values())
    if top_score == 0:
        return _enum_from_candidates(ProjectStage, "unknown")
    # When signals tie, use the later stage so that CTA routing remains
    # conservative about what the project team may already need.
    for stage in ("procurement", "specification", "planning"):
        if scores[stage] == top_score:
            return _enum_from_candidates(ProjectStage, stage)
    return _enum_from_candidates(ProjectStage, "unknown")


def determine_audience(values: Mapping[str, Any] | NormalizedLead) -> AudienceType:
    """Infer recipient type from contact roles and explicit lead language."""

    if isinstance(values, Mapping):
        get_value = values.get
    else:
        get_value = lambda key, default=None: getattr(values, key, default)

    explicit = _clean_text(
        get_value("audience") or get_value("audience_type") or get_value("recipient_type")
    )
    if explicit:
        token = _key_token(explicit)
        aliases = {
            "architect": ("architect_specifier", "architect"),
            "architect_specifier": ("architect_specifier", "architect"),
            "specifier": ("architect_specifier", "architect"),
            "contractor": ("contractor_builder", "contractor"),
            "contractor_builder": ("contractor_builder", "contractor"),
            "builder": ("contractor_builder", "contractor"),
            "distributor": ("distributor_supplier", "distributor"),
            "supplier": ("distributor_supplier", "distributor"),
            "distributor_supplier": ("distributor_supplier", "distributor"),
            "owner": ("owner_developer", "owner"),
            "developer": ("owner_developer", "owner"),
            "owner_developer": ("owner_developer", "owner"),
            "manufacturer": ("manufacturer_fabricator", "manufacturer"),
            "fabricator": ("manufacturer_fabricator", "fabricator"),
            "manufacturer_fabricator": (
                "manufacturer_fabricator",
                "manufacturer",
            ),
            "facilities": ("facilities_property", "facilities"),
            "facilities_manager": ("facilities_property", "facilities"),
            "property_manager": ("facilities_property", "facilities"),
            "facilities_property": ("facilities_property", "facilities"),
        }
        if token in aliases:
            return _enum_from_candidates(AudienceType, *aliases[token])

    contacts_text = _text_from_value(get_value("contacts"))
    context_text = " ".join(
        _text_from_value(get_value(field))
        for field in ("section", "signal", "intelligence", "next_step", "summary")
    )
    scores: dict[str, int] = {audience: 0 for audience in _AUDIENCE_PATTERNS}
    for audience, patterns in _AUDIENCE_PATTERNS.items():
        scores[audience] += 3 * sum(
            1 for pattern in patterns if pattern.search(contacts_text)
        )
        scores[audience] += sum(1 for pattern in patterns if pattern.search(context_text))

    top_score = max(scores.values())
    if top_score == 0:
        return _enum_from_candidates(AudienceType, "unknown")
    for audience in (
        "architect_specifier",
        "contractor_builder",
        "distributor_supplier",
        "manufacturer_fabricator",
        "facilities_property",
        "owner_developer",
    ):
        if scores[audience] == top_score:
            short_name = audience.split("_")[0]
            return _enum_from_candidates(AudienceType, audience, short_name)
    return _enum_from_candidates(AudienceType, "unknown")


def _evidence_references(
    lead_id: str,
    values: Mapping[str, Any],
    patterns: tuple[re.Pattern[str], ...],
) -> list[EvidenceReference]:
    references: list[EvidenceReference] = []
    seen: set[tuple[str, str]] = set()
    for field in _EVIDENCE_FIELDS:
        text = _text_from_value(values.get(field))
        for pattern in patterns:
            for match in pattern.finditer(text):
                quote = match.group(0).strip()
                marker = (field, quote.casefold())
                if marker in seen:
                    continue
                seen.add(marker)
                references.append(
                    EvidenceReference(
                        source_type="lead",
                        source_id=lead_id,
                        source_field=field,
                        quote=quote,
                    )
                )
    return references


def normalize_lead(
    record: Mapping[str, Any], *, identity_scope: str | None = None
) -> NormalizedLead:
    """Return a typed lead without modifying ``record``.

    ``lead_id``, ``id`` and ``external_id`` are checked in that order. A value
    such as ``Lead #4`` is rejected because it is a dashboard rank, not an
    identity that remains stable across feed refreshes. When all explicit IDs
    are absent, callers may supply a stable EarlyBid ``identity_scope`` to
    derive an ID from the project and location natural key.
    """

    if not isinstance(record, Mapping):
        raise TypeError("Lead input must be a mapping")

    source_values = copy.deepcopy(dict(record))
    lookup = _record_lookup(record)

    raw_values: dict[str, Any] = {
        field: _get_value(record, lookup, *aliases)
        for field, aliases in _FIELD_ALIASES.items()
    }
    lead_id = _resolve_lead_id(
        record,
        lookup,
        identity_scope=identity_scope,
        project=raw_values["project"],
        location=raw_values["location"],
        state=raw_values["state"],
    )
    text_values = {
        field: _clean_text(raw_values[field])
        for field in (
            "section",
            "project",
            "location",
            "state",
            "signal",
            "intelligence",
            "timing",
            "next_step",
            "awarded_to",
            "priority_reasons",
            "summary",
            "url",
        )
    }
    tags = _parse_tags(raw_values["tags"])
    contacts = _parse_contacts(raw_values["contacts"], raw_values["contact_email"])
    city, state = _parse_location(text_values["location"], text_values["state"])
    meeting_date_raw = _clean_text(raw_values["meeting_date"])

    derived_values: dict[str, Any] = {
        **text_values,
        "tags": tags,
        "contacts": contacts,
    }
    # Explicit fields not part of the feed schema are accepted when a caller
    # already knows the audience or project stage.
    for field in ("project_stage", "stage", "audience", "audience_type", "recipient_type"):
        derived_values[field] = _get_value(record, lookup, field)

    audience = determine_audience(derived_values)
    project_stage = determine_stage(derived_values)
    material_mentions = _evidence_references(lead_id, derived_values, _MATERIAL_PATTERNS)
    competitor_mentions = _evidence_references(lead_id, derived_values, _COMPETITOR_PATTERNS)

    return NormalizedLead(
        lead_id=lead_id,
        section=text_values["section"],
        project=text_values["project"],
        location=text_values["location"],
        state=state,
        signal=text_values["signal"],
        intelligence=text_values["intelligence"],
        score=_parse_score(raw_values["score"]),
        timing=text_values["timing"],
        next_step=text_values["next_step"],
        awarded_to=text_values["awarded_to"],
        priority_reasons=text_values["priority_reasons"],
        summary=text_values["summary"],
        contacts_raw=_clean_text(raw_values["contacts"]),
        contacts=contacts,
        meeting_date_raw=meeting_date_raw,
        meeting_date=_parse_date(raw_values["meeting_date"]),
        tags=tags,
        tags_raw=_clean_text(raw_values["tags"]),
        url=text_values["url"],
        city=city,
        audience=audience,
        project_stage=project_stage,
        material_mentions=material_mentions,
        competitor_mentions=competitor_mentions,
        source_values=source_values,
    )


__all__ = [
    "EARLYBID_NATURAL_ID_PREFIX",
    "derive_earlybid_natural_id",
    "determine_audience",
    "determine_stage",
    "normalize_lead",
    "normalize_state_code",
]
