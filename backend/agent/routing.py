"""Catalog-backed deterministic product-routing hints.

Hints narrow the structured model's choices; they are not product claims and
they do not by themselves prove that a lead has selected an Accoya product.
Every hint retains the exact matched lead text and its normalized source field.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from .catalog import application_belongs_to_family
from .models import NormalizedLead, RoutingHint


ACCOYA_WOOD = "accoya_wood"
ACCOYA_COLOR_GREY = "accoya_color_grey"
TRICOYA_PANELS = "tricoya_panels"

STANDARD_DECKING = "standard_decking"
COLOR_GREY_DECKING = "color_grey_decking"
COLOR_GREY_SIDING = "color_grey_siding_cladding"
EXTERIOR_MDF_PANELS = "exterior_mdf_panels"
STANDARD_SIDING = "standard_siding"
GENERAL_WINDOWS = "general_wooden_windows"
EXTERIOR_DOORS = "exterior_wooden_doors"

_TEXT_FIELDS = (
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

_GREY_DECK_RE = re.compile(
    r"\b(?:gr[ae]y(?:[- ]through[- ]the[- ]board)?|pre[- ]gr[ae]yed)"
    r"(?:\s+\w+){0,2}\s+deck(?:ing)?\b|"
    r"\bdeck(?:ing)?(?:\s+\w+){0,2}\s+(?:gr[ae]y|pre[- ]gr[ae]yed)\b",
    re.IGNORECASE,
)
_GREY_SIDING_RE = re.compile(
    r"\b(?:gr[ae]y(?:[- ]through[- ]the[- ]board)?|pre[- ]gr[ae]yed)"
    r"(?:\s+\w+){0,2}\s+(?:siding|cladding)\b|"
    r"\b(?:siding|cladding)(?:\s+\w+){0,2}\s+(?:gr[ae]y|pre[- ]gr[ae]yed)\b",
    re.IGNORECASE,
)
_TRICOYA_RE = re.compile(
    r"\b(?:exterior(?:[- ]grade)?\s+MDF|MDF\s+(?:fascia|soffit|panel(?:s)?)|"
    r"routed\s+(?:exterior\s+)?panel(?:s)?|CNC[- ]cut\s+exterior\s+component(?:s)?|"
    r"fascias?|soffits?)\b",
    re.IGNORECASE,
)
_DECK_MATERIAL_RE = re.compile(
    r"\b(?:Thermory|Trex|TimberTech|Ip[eé]|thermally\s+modified(?:\s+(?:wood|timber))?|"
    r"composite(?:\s+wood)?)\b",
    re.IGNORECASE,
)
_DECK_CONTEXT_RE = re.compile(
    r"\b(?:deck(?:ing|s|\s+stairs?)?|walkways?|boardwalks?|porch(?:es)?|"
    r"rooftop\s+deck(?:ing)?|pool\s+deck(?:ing)?|pedestrian\s+decking)\b",
    re.IGNORECASE,
)
_SIDING_RE = re.compile(
    r"\b(?:cladding|fa[cç]ades?|rainscreen(?:\s+siding)?|siding)\b",
    re.IGNORECASE,
)
_WINDOW_RE = re.compile(
    r"\b(?:(?:exterior|building|bay|sash|casement|timber|wood(?:en)?)\s+"
    r"windows?|windows?\s+(?:application|frame|project|system)|"
    r"window\s+shutters?|shutters?)\b",
    re.IGNORECASE,
)
_DOOR_RE = re.compile(
    r"\b(?:(?:front|entrance|exterior|garage|bifold|bi-fold|French)\s+doors?|"
    r"exterior\s+wooden\s+doors?)\b",
    re.IGNORECASE,
)

_GENERIC_APPLICATION_PATTERNS = {
    "bridge": re.compile(
        r"\b(?:(?:exterior|outdoor|pedestrian|foot|road|trail|timber|"
        r"wood(?:en)?)\s+){1,3}bridges?\b|"
        r"\bbridges?\s+(?:application|deck|project|structure)\b",
        re.IGNORECASE,
    ),
    "gate": re.compile(
        r"\b(?:(?:exterior|outdoor|entrance|garden|driveway|pedestrian|"
        r"timber|wood(?:en)?)\s+){1,3}gates?\b|"
        r"\bgates?\s+(?:application|project)\b",
        re.IGNORECASE,
    ),
    "sign": re.compile(
        r"\b(?:(?:exterior|outdoor|wayfinding|timber|wood(?:en)?)\s+)"
        r"signs?\b|\bsigns?\s+(?:application|project|system)\b",
        re.IGNORECASE,
    ),
    "structure": re.compile(
        r"\b(?:(?:exterior|outdoor|timber|wood(?:en)?)\s+)"
        r"structures?\b|\bstructures?\s+(?:application|project)\b",
        re.IGNORECASE,
    ),
    "window": re.compile(
        r"\b(?:(?:exterior|building|bay|sash|casement|timber|wood(?:en)?)\s+)"
        r"windows?\b|\bwindows?\s+(?:application|frame|project|system)\b",
        re.IGNORECASE,
    ),
}
_GENERIC_APPLICATION_FORMS = {
    "bridge": "bridge",
    "bridges": "bridge",
    "gate": "gate",
    "gates": "gate",
    "sign": "sign",
    "signs": "sign",
    "structure": "structure",
    "structures": "structure",
    "window": "window",
    "windows": "window",
}


@dataclass(frozen=True, slots=True)
class _Occurrence:
    source_field: str
    source_trigger: str


def _field_texts(lead: NormalizedLead) -> Iterator[tuple[str, str]]:
    for field in _TEXT_FIELDS:
        value = getattr(lead, field, None)
        if isinstance(value, str) and value.strip():
            yield field, value
        elif isinstance(value, (list, tuple, set)):
            text = ", ".join(str(item) for item in value if str(item).strip())
            if text:
                yield field, text


def _first_occurrence(
    fields: tuple[tuple[str, str], ...], pattern: re.Pattern[str]
) -> _Occurrence | None:
    for source_field, text in fields:
        match = pattern.search(text)
        if match:
            return _Occurrence(source_field, match.group(0).strip())
    return None


def routing_term_supported(term: str, text: str) -> bool:
    """Match a catalog term without treating generic nouns as applications.

    Words such as ``structure`` and ``gate`` occur in ordinary construction
    prose. They count as application evidence only with literal exterior or
    wood context; all other catalog terms use boundary-aware phrase matching.
    """

    cleaned = re.sub(r"\s+", " ", term.strip())
    if not cleaned:
        return False
    generic = _GENERIC_APPLICATION_FORMS.get(cleaned.casefold())
    if generic is not None:
        return bool(_GENERIC_APPLICATION_PATTERNS[generic].search(text))
    return bool(
        re.search(
            rf"(?<!\w){re.escape(cleaned)}(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


def get_routing_hints(lead: NormalizedLead) -> list[RoutingHint]:
    """Return supported catalog hints ordered by descending specificity.

    An unrelated lead returns an empty list.  In particular, the function does
    not use a generic exterior-project fallback, because that would force a
    product where the source record provides no application evidence.
    """

    fields = tuple(_field_texts(lead))
    candidates: list[RoutingHint] = []

    def add(
        family: str,
        application: str,
        occurrence: _Occurrence | None,
        reason: str,
        priority: int,
    ) -> None:
        if occurrence is None:
            return
        if not application_belongs_to_family(family, application):
            return
        candidates.append(
            RoutingHint(
                product_family=family,
                application=application,
                source_field=occurrence.source_field,
                source_trigger=occurrence.source_trigger,
                reason=reason,
                priority=priority,
            )
        )

    # Appearance-specific Color Grey evidence must outrank a generic decking
    # or siding signal.  Merely mentioning the color grey elsewhere is not
    # sufficient; it must be adjacent to the application.
    add(
        ACCOYA_COLOR_GREY,
        COLOR_GREY_DECKING,
        _first_occurrence(fields, _GREY_DECK_RE),
        "The lead explicitly connects a grey or pre-greyed appearance to decking.",
        100,
    )
    add(
        ACCOYA_COLOR_GREY,
        COLOR_GREY_SIDING,
        _first_occurrence(fields, _GREY_SIDING_RE),
        "The lead explicitly connects a grey or pre-greyed appearance to siding or cladding.",
        100,
    )

    add(
        TRICOYA_PANELS,
        EXTERIOR_MDF_PANELS,
        _first_occurrence(fields, _TRICOYA_RE),
        "Exterior MDF, routed-panel, fascia, or soffit language indicates a panel application; Tricoya is not solid lumber.",
        95,
    )

    material = _first_occurrence(fields, _DECK_MATERIAL_RE)
    deck_context = _first_occurrence(fields, _DECK_CONTEXT_RE)
    if material is not None and deck_context is not None:
        add(
            ACCOYA_WOOD,
            STANDARD_DECKING,
            material,
            "A named decking material or competitor appears with a decking application in the lead.",
            90,
        )
    add(
        ACCOYA_WOOD,
        STANDARD_DECKING,
        deck_context,
        "A walkway, boardwalk, porch, rooftop deck, pool deck, or other decking application appears in the lead.",
        80,
    )

    add(
        ACCOYA_WOOD,
        STANDARD_SIDING,
        _first_occurrence(fields, _SIDING_RE),
        "Cladding, facade, rainscreen, or siding language indicates an Accoya Wood siding application.",
        75,
    )
    add(
        ACCOYA_WOOD,
        GENERAL_WINDOWS,
        _first_occurrence(fields, _WINDOW_RE),
        "Window, sash, casement, or shutter language indicates an Accoya Wood window application.",
        75,
    )
    add(
        ACCOYA_WOOD,
        EXTERIOR_DOORS,
        _first_occurrence(fields, _DOOR_RE),
        "Front, exterior, garage, bifold, or French door language indicates an Accoya Wood door application.",
        75,
    )

    # Keep the strongest exact trigger for each family/application pair.  The
    # remaining order is deterministic even when multiple fields match.
    candidates.sort(
        key=lambda item: (
            -item.priority,
            item.product_family,
            item.application,
            item.source_field,
            item.source_trigger.casefold(),
        )
    )
    hints: list[RoutingHint] = []
    seen: set[tuple[str, str]] = set()
    for hint in candidates:
        key = (hint.product_family, hint.application)
        if key not in seen:
            seen.add(key)
            hints.append(hint)
    return hints


__all__ = ["get_routing_hints", "routing_term_supported"]
