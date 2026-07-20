"""Immutable, versioned Accoya product and application catalog.

This module is the only product-selection vocabulary used by the isolated agent.
It intentionally contains no marketing claims: catalog entries establish product
identity, material form, application membership, aliases, and deterministic
routing vocabulary only.
"""

from __future__ import annotations

import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


CATALOG_VERSION = "1.0.0"


class CatalogModel(BaseModel):
    """Frozen base class for catalog records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogApplication(CatalogModel):
    """One canonical application within exactly one product family."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    application_group: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    routing_terms: tuple[str, ...] = ()


class CatalogFamily(CatalogModel):
    """One canonical product family and its immutable applications."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    routing_terms: tuple[str, ...] = ()
    material_form: str = Field(min_length=1)
    is_solid_lumber: bool
    selection_rule: str = Field(min_length=1)
    applications: tuple[CatalogApplication, ...]


class AccoyaCatalog(CatalogModel):
    """Versioned root object injected into the generation prompt."""

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    families: tuple[CatalogFamily, ...]
    invariants: tuple[str, ...]

    @model_validator(mode="after")
    def validate_unique_identifiers(self) -> AccoyaCatalog:
        family_ids = [family.id for family in self.families]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("catalog family IDs must be unique")

        application_ids = [
            application.id
            for family in self.families
            for application in family.applications
        ]
        if len(application_ids) != len(set(application_ids)):
            raise ValueError("catalog application IDs must be globally unique")

        tricoya = next(
            (family for family in self.families if family.id == "tricoya_panels"),
            None,
        )
        if tricoya is None or tricoya.is_solid_lumber:
            raise ValueError("Tricoya must exist and must not be solid lumber")
        return self


def _application(
    application_id: str,
    display_name: str,
    group: str,
    *,
    aliases: tuple[str, ...] = (),
    routing_terms: tuple[str, ...] = (),
) -> CatalogApplication:
    return CatalogApplication(
        id=application_id,
        display_name=display_name,
        application_group=group,
        aliases=aliases,
        routing_terms=routing_terms,
    )


ACCOYA_WOOD = CatalogFamily(
    id="accoya_wood",
    display_name="Accoya Wood",
    aliases=("Accoya", "Accoya solid wood", "Accoya timber", "Accoya lumber"),
    routing_terms=("solid wood", "solid lumber", "timber", "wood"),
    material_form="solid acetylated wood",
    is_solid_lumber=True,
    selection_rule=(
        "Use for solid-wood decking, siding and cladding, windows, doors, "
        "landscape, and custom exterior applications."
    ),
    applications=(
        _application(
            "standard_decking",
            "Standard decking",
            "decking",
            aliases=("decking", "deck", "porch", "porch decking"),
            routing_terms=(
                "thermory",
                "thermally modified decking",
                "trex",
                "timbertech",
                "composite decking",
                "ipe decking",
                "porch",
            ),
        ),
        _application(
            "pool_decking",
            "Pool decking",
            "decking",
            aliases=("pool deck",),
            routing_terms=("pool deck", "pool decking"),
        ),
        _application(
            "rooftop_decking",
            "Rooftop decking",
            "decking",
            aliases=("rooftop deck", "roof deck"),
            routing_terms=("rooftop deck", "rooftop decking", "roof deck"),
        ),
        _application(
            "deck_stairs",
            "Deck stairs",
            "decking",
            aliases=("deck stair", "deck steps"),
            routing_terms=("deck stairs", "deck stair", "deck steps"),
        ),
        _application(
            "elevated_walkways",
            "Elevated walkways",
            "decking",
            aliases=("elevated walkway", "walkway"),
            routing_terms=("elevated walkway", "elevated walkways", "walkway"),
        ),
        _application(
            "boardwalks_pedestrian_decking",
            "Boardwalks and pedestrian decking",
            "decking",
            aliases=("pedestrian decking", "decking boardwalk"),
            routing_terms=("boardwalk", "boardwalks", "pedestrian decking"),
        ),
        _application(
            "standard_siding",
            "Standard siding",
            "siding_cladding",
            aliases=("siding", "cladding", "facade", "façade"),
            routing_terms=("siding", "cladding", "facade", "façade"),
        ),
        _application(
            "charred_shou_sugi_ban_siding",
            "Charred or Shou Sugi Ban siding",
            "siding_cladding",
            aliases=("charred siding", "shou sugi ban siding", "shou sugi ban"),
            routing_terms=("charred", "shou sugi ban", "yakisugi"),
        ),
        _application(
            "rainscreen_siding",
            "Rainscreen siding",
            "siding_cladding",
            aliases=("rainscreen", "rainscreen cladding"),
            routing_terms=("rainscreen", "rainscreen siding", "rainscreen facade"),
        ),
        _application(
            "brise_soleil",
            "Brise Soleil",
            "siding_cladding",
            aliases=("brise-soleil", "solar shading"),
            routing_terms=("brise soleil", "brise-soleil"),
        ),
        _application(
            "louvres_shade_screens",
            "Louvres and shade screens",
            "siding_cladding",
            aliases=("louvers", "louvres", "shade screens", "shade screen"),
            routing_terms=("louvre", "louver", "shade screen", "shade screens"),
        ),
        _application(
            "general_wooden_windows",
            "General wooden windows",
            "windows",
            aliases=("wood windows", "wooden windows", "window"),
            routing_terms=("window", "windows", "wood window", "wooden window"),
        ),
        _application(
            "bay_windows",
            "Bay windows",
            "windows",
            aliases=("bay window",),
            routing_terms=("bay window", "bay windows"),
        ),
        _application(
            "sash_windows",
            "Sash windows",
            "windows",
            aliases=("sash window",),
            routing_terms=("sash", "sash window", "sash windows"),
        ),
        _application(
            "casement_windows",
            "Casement windows",
            "windows",
            aliases=("casement window",),
            routing_terms=("casement", "casement window", "casement windows"),
        ),
        _application(
            "window_shutters",
            "Window shutters",
            "windows",
            aliases=("shutters", "window shutter"),
            routing_terms=("window shutter", "window shutters", "shutters"),
        ),
        _application(
            "exterior_wooden_doors",
            "Exterior wooden doors",
            "doors",
            aliases=("exterior door", "wood exterior door", "wooden exterior door"),
            routing_terms=("exterior door", "exterior doors", "wooden door"),
        ),
        _application(
            "front_entrance_doors",
            "Front and entrance doors",
            "doors",
            aliases=("front door", "entrance door", "entry door"),
            routing_terms=("front door", "entrance door", "entry door"),
        ),
        _application(
            "garage_doors",
            "Garage doors",
            "doors",
            aliases=("garage door",),
            routing_terms=("garage door", "garage doors"),
        ),
        _application(
            "bifold_doors",
            "Bifold doors",
            "doors",
            aliases=("bifold door", "bi-fold door", "bi-fold doors"),
            routing_terms=("bifold", "bi-fold", "bifold door"),
        ),
        _application(
            "french_doors",
            "French doors",
            "doors",
            aliases=("French door",),
            routing_terms=("french door", "french doors"),
        ),
        _application(
            "bridges",
            "Bridges",
            "landscape_custom",
            aliases=("bridge",),
            routing_terms=("bridge", "bridges"),
        ),
        _application(
            "landscape_boardwalks",
            "Boardwalks",
            "landscape_custom",
            aliases=("landscape boardwalk",),
            routing_terms=("landscape boardwalk",),
        ),
        _application(
            "garden_rooms",
            "Garden rooms",
            "landscape_custom",
            aliases=("garden room",),
            routing_terms=("garden room", "garden rooms"),
        ),
        _application(
            "conservatories",
            "Conservatories",
            "landscape_custom",
            aliases=("conservatory",),
            routing_terms=("conservatory", "conservatories"),
        ),
        _application(
            "gates",
            "Gates",
            "landscape_custom",
            aliases=("gate",),
            routing_terms=("gate", "gates"),
        ),
        _application(
            "outdoor_furniture",
            "Outdoor furniture",
            "landscape_custom",
            aliases=("exterior furniture",),
            routing_terms=("outdoor furniture", "exterior furniture"),
        ),
        _application(
            "planters",
            "Planters",
            "landscape_custom",
            aliases=("planter",),
            routing_terms=("planter", "planters"),
        ),
        _application(
            "fencing_railings",
            "Fencing and railings",
            "landscape_custom",
            aliases=("fencing", "railing", "railings"),
            routing_terms=("fence", "fencing", "railing", "railings"),
        ),
        _application(
            "structures_sculptures",
            "Structures and sculptures",
            "landscape_custom",
            aliases=("structure", "sculpture", "sculptures"),
            routing_terms=("exterior structure", "outdoor sculpture"),
        ),
        _application(
            "freshwater_specialist_exterior",
            "Freshwater and specialist exterior applications",
            "landscape_custom",
            aliases=("freshwater application", "specialist exterior application"),
            routing_terms=("freshwater", "specialist exterior"),
        ),
    ),
)


ACCOYA_COLOR_GREY = CatalogFamily(
    id="accoya_color_grey",
    display_name="Accoya Color Grey",
    aliases=("Accoya Color", "Accoya Grey", "Accoya Gray", "Color Grey"),
    routing_terms=(
        "grey through the board",
        "gray through the board",
        "grey-through-the-board",
        "gray-through-the-board",
        "pre-greyed",
        "pre-grayed",
        "grey decking",
        "gray decking",
        "grey siding",
        "gray siding",
    ),
    material_form="grey-through-the-board solid acetylated wood",
    is_solid_lumber=True,
    selection_rule=(
        "Use only when the lead makes a grey-through-the-board appearance or "
        "pre-greyed finish relevant; do not infer this preference from weathering alone."
    ),
    applications=(
        _application(
            "color_grey_decking",
            "Color Grey decking",
            "color_grey",
            aliases=("grey decking", "gray decking", "pre-greyed decking"),
            routing_terms=(
                "grey deck",
                "gray deck",
                "grey decking",
                "gray decking",
                "pre-greyed deck",
            ),
        ),
        _application(
            "grey_pool_decking",
            "Grey pool decking",
            "color_grey",
            aliases=("gray pool decking", "grey pool deck", "gray pool deck"),
            routing_terms=("grey pool deck", "gray pool deck"),
        ),
        _application(
            "color_grey_siding_cladding",
            "Color Grey siding and cladding",
            "color_grey",
            aliases=(
                "grey siding",
                "gray siding",
                "grey cladding",
                "gray cladding",
            ),
            routing_terms=(
                "grey siding",
                "gray siding",
                "grey cladding",
                "gray cladding",
                "pre-greyed siding",
            ),
        ),
    ),
)


TRICOYA_PANELS = CatalogFamily(
    id="tricoya_panels",
    display_name="Tricoya Panels",
    aliases=("Tricoya", "Tricoya panel", "Tricoya MDF"),
    routing_terms=("exterior MDF", "acetylated MDF", "exterior panel"),
    material_form="acetylated wood-fibre panel product",
    is_solid_lumber=False,
    selection_rule=(
        "Use for exterior panel and MDF-component applications. Tricoya is a "
        "panel product and must never be represented as solid lumber."
    ),
    applications=(
        _application(
            "exterior_mdf_panels",
            "Exterior MDF panels",
            "panels",
            aliases=("exterior MDF", "exterior MDF panel", "exterior MDF panels"),
            routing_terms=("exterior mdf", "exterior mdf panel", "acetylated mdf"),
        ),
        _application(
            "exterior_doors_door_components",
            "Exterior doors and door components",
            "panels",
            aliases=("exterior door components", "door components"),
            routing_terms=("exterior door component", "door component panel"),
        ),
        _application(
            "siding_facades",
            "Siding and façades",
            "panels",
            aliases=("panel siding", "panel facade", "panel façade"),
            routing_terms=("panel siding", "panel facade", "panel façade"),
        ),
        _application(
            "exterior_paneling",
            "Exterior paneling",
            "panels",
            aliases=("exterior panelling", "exterior panels"),
            routing_terms=("exterior paneling", "exterior panelling", "exterior panels"),
        ),
        _application(
            "mouldings_trim",
            "Mouldings and trim",
            "panels",
            aliases=("moldings and trim", "mouldings", "exterior trim"),
            routing_terms=("moulding", "molding", "exterior trim"),
        ),
        _application(
            "fascias_soffits",
            "Fascias and soffits",
            "panels",
            aliases=("fascia", "fascias", "soffit", "soffits"),
            routing_terms=("fascia", "fascias", "soffit", "soffits"),
        ),
        _application(
            "signage",
            "Signage",
            "panels",
            aliases=("exterior signage", "signs"),
            routing_terms=("exterior signage", "outdoor signage"),
        ),
        _application(
            "routed_cnc_exterior_components",
            "Routed or CNC-cut exterior components",
            "panels",
            aliases=("routed panel", "CNC-cut panel", "CNC exterior component"),
            routing_terms=("routed panel", "routed exterior", "cnc-cut", "cnc cut"),
        ),
        _application(
            "painted_large_format_exterior_panels",
            "Painted large-format exterior panels",
            "panels",
            aliases=("painted exterior panel", "large-format exterior panel"),
            routing_terms=("painted large-format", "painted exterior panel"),
        ),
    ),
)


ACCOYA_CATALOG = AccoyaCatalog(
    version=CATALOG_VERSION,
    families=(ACCOYA_WOOD, ACCOYA_COLOR_GREY, TRICOYA_PANELS),
    invariants=(
        "Select exactly one primary family and one application from that family.",
        "Accoya Color Grey is relevant only for a grey-through-the-board or pre-greyed finish.",
        "Tricoya is a panel product, not solid lumber.",
        "Catalog membership is not evidence for a technical or project-specific claim.",
    ),
)


FAMILY_BY_ID: Mapping[str, CatalogFamily] = MappingProxyType(
    {family.id: family for family in ACCOYA_CATALOG.families}
)
APPLICATION_BY_ID: Mapping[str, CatalogApplication] = MappingProxyType(
    {
        application.id: application
        for family in ACCOYA_CATALOG.families
        for application in family.applications
    }
)
APPLICATION_FAMILY_BY_ID: Mapping[str, str] = MappingProxyType(
    {
        application.id: family.id
        for family in ACCOYA_CATALOG.families
        for application in family.applications
    }
)
PRODUCT_FAMILY_IDS = tuple(FAMILY_BY_ID)
APPLICATION_IDS = tuple(APPLICATION_BY_ID)


def _normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _build_family_aliases() -> Mapping[str, CatalogFamily]:
    aliases: dict[str, CatalogFamily] = {}
    for family in ACCOYA_CATALOG.families:
        for value in (family.id, family.display_name, *family.aliases):
            aliases[_normalize_lookup(value)] = family
    return MappingProxyType(aliases)


def _build_application_aliases() -> Mapping[str, tuple[CatalogApplication, ...]]:
    aliases: dict[str, list[CatalogApplication]] = {}
    for family in ACCOYA_CATALOG.families:
        for application in family.applications:
            for value in (application.id, application.display_name, *application.aliases):
                key = _normalize_lookup(value)
                if application not in aliases.setdefault(key, []):
                    aliases[key].append(application)
    return MappingProxyType({key: tuple(values) for key, values in aliases.items()})


_FAMILY_ALIASES = _build_family_aliases()
_APPLICATION_ALIASES = _build_application_aliases()


def get_family(family_id_or_alias: str | None) -> CatalogFamily | None:
    """Resolve a canonical family ID, display name, or documented alias."""

    if not family_id_or_alias:
        return None
    return _FAMILY_ALIASES.get(_normalize_lookup(str(family_id_or_alias)))


def get_application(
    application_id_or_alias: str | None,
    family_id: str | None = None,
) -> CatalogApplication | None:
    """Resolve an application, optionally disambiguating it by product family."""

    if not application_id_or_alias:
        return None
    candidates = _APPLICATION_ALIASES.get(
        _normalize_lookup(str(application_id_or_alias)), ()
    )
    if family_id:
        family = get_family(family_id)
        if family is None:
            return None
        candidates = tuple(
            application
            for application in candidates
            if APPLICATION_FAMILY_BY_ID[application.id] == family.id
        )
    return candidates[0] if len(candidates) == 1 else None


def application_belongs_to_family(
    family_id_or_alias: str | None,
    application_id_or_alias: str | None,
) -> bool:
    """Return whether a resolvable application belongs to a resolvable family."""

    family = get_family(family_id_or_alias)
    if family is None:
        return False
    application = get_application(application_id_or_alias, family.id)
    return application is not None


def catalog_payload() -> dict[str, Any]:
    """Return a fresh JSON-compatible copy suitable for prompts and telemetry."""

    return ACCOYA_CATALOG.model_dump(mode="json")


def render_catalog_for_prompt() -> str:
    """Render the complete versioned catalog deterministically for the prompt."""

    return json.dumps(catalog_payload(), indent=2, ensure_ascii=False, sort_keys=True)


__all__ = [
    "AccoyaCatalog",
    "ACCOYA_CATALOG",
    "ACCOYA_COLOR_GREY",
    "ACCOYA_WOOD",
    "APPLICATION_BY_ID",
    "APPLICATION_FAMILY_BY_ID",
    "APPLICATION_IDS",
    "application_belongs_to_family",
    "catalog_payload",
    "CatalogApplication",
    "CatalogFamily",
    "CATALOG_VERSION",
    "FAMILY_BY_ID",
    "get_application",
    "get_family",
    "PRODUCT_FAMILY_IDS",
    "render_catalog_for_prompt",
    "TRICOYA_PANELS",
]
