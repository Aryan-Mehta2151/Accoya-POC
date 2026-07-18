"""Versioned claim and CTA policy shared by prompting and validation."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .models import CTAType, ProjectStage

PROMPT_VERSION = "accoya-email-v1.0.0"
POLICY_VERSION = "1.0.0"

PROHIBITED_PHRASES = (
    "60-year warranty",
    "Class 1 certified",
    "structural performance",
    "ADA compliant",
    "makes the project ADA compliant",
    "crack-free",
    "splinter-free",
    "maintenance-free",
    "zero maintenance",
    "flawless",
    "will never warp",
    "will never fail",
    "never warps",
    "cannot fail",
    "requires no maintenance",
    "ADA-compliant",
    "free of splinters",
    "unmatched sustainability",
)

SAFE_CLAIM_FORMS = (
    "60-year service life",
    "Class 1 durability",
    "high dimensional stability",
    "resistant to rot and fungal decay",
    "resists swelling, warping or splintering",
    "low maintenance",
    "can help support a consistent walking surface",
    "subject to the applicable warranty terms",
)

CLAIM_POLICY = MappingProxyType(
    {
        "version": POLICY_VERSION,
        "source_precedence": (
            "hardcoded_policy",
            "explicit_lead_facts",
            "static_catalog",
            "retrieved_approved_strategy",
        ),
        "general_model_knowledge_for_product_claims": False,
        "maximum_primary_products": 1,
        "maximum_benefits": 3,
        "competitor_requires_exact_lead_evidence": True,
        "strategy_cannot_create_project_facts": True,
        "strategy_is_not_automatic_technical_claim_support": True,
        "warranty_requires_matching_approved_source": (
            "product_family",
            "application",
            "geography",
        ),
        "catalog_fallback_allows_technical_claims": False,
        "prohibited_equivalent_categories": (
            "absolute performance or compliance guarantees",
            "environmental superlatives",
            "competitor superiority or outlasting claims",
            "claims that Accoya made or supplied a finished system",
        ),
        "every_strategy_benefit_requires_claim_in_exact_approved_quote": True,
        "strategy_evidence_cannot_ground_project_numbers_or_named_entities": True,
    }
)

_CTA_OPTIONS = {
    ProjectStage.PLANNING: {
        CTAType.TECHNICAL_INFORMATION: "Would it be helpful if I sent the relevant technical information?",
        CTAType.SAMPLE: "Would it be useful if I arranged an Accoya sample for your review?",
        CTAType.SPECIFICATION_DISCUSSION: "Would a short specification discussion be useful?",
    },
    ProjectStage.SPECIFICATION: {
        CTAType.CSI_LANGUAGE: "Would it help if I shared relevant CSI language?",
        CTAType.TECHNICAL_REVIEW: "Would a short technical review be useful for the project?",
        CTAType.MANUFACTURER_COORDINATION: "Would manufacturer coordination be useful at this stage?",
    },
    ProjectStage.PROCUREMENT: {
        CTAType.SUPPLIER_ASSISTANCE: "Would supplier or approved-manufacturer assistance be useful?",
        CTAType.AVAILABILITY_DISCUSSION: "Would a short availability discussion be useful?",
        CTAType.SAMPLE: "Would it be useful if I arranged an Accoya sample for your review?",
    },
    ProjectStage.UNKNOWN: {
        CTAType.CLARIFY_NEEDS: "Could you share where the project is in its material evaluation?",
    },
}

CTA_OPTIONS_BY_STAGE: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        stage.value: MappingProxyType(
            {cta_type.value: text for cta_type, text in options.items()}
        )
        for stage, options in _CTA_OPTIONS.items()
    }
)


def is_cta_allowed(stage: ProjectStage | str, cta_type: CTAType | str) -> bool:
    """Return whether the CTA category is valid for the normalized stage."""
    try:
        typed_stage = stage if isinstance(stage, ProjectStage) else ProjectStage(stage)
        typed_cta = cta_type if isinstance(cta_type, CTAType) else CTAType(cta_type)
    except ValueError:
        return False
    return typed_cta in _CTA_OPTIONS.get(typed_stage, {})


def is_cta_text_allowed(
    stage: ProjectStage | str, cta_type: CTAType | str, text: str
) -> bool:
    """Return whether text matches the fixed template for its CTA."""
    try:
        typed_stage = stage if isinstance(stage, ProjectStage) else ProjectStage(stage)
        typed_cta = cta_type if isinstance(cta_type, CTAType) else CTAType(cta_type)
    except ValueError:
        return False
    allowed = _CTA_OPTIONS.get(typed_stage, {}).get(typed_cta)
    return bool(allowed and allowed.casefold() == text.strip().casefold())


def cta_text(stage: ProjectStage | str, cta_type: CTAType | str) -> str | None:
    """Return the fixed CTA text for an allowed stage/category pair."""
    try:
        typed_stage = stage if isinstance(stage, ProjectStage) else ProjectStage(stage)
        typed_cta = cta_type if isinstance(cta_type, CTAType) else CTAType(cta_type)
    except ValueError:
        return None
    return _CTA_OPTIONS.get(typed_stage, {}).get(typed_cta)


__all__ = [
    "CLAIM_POLICY", "CTA_OPTIONS_BY_STAGE", "POLICY_VERSION", "PROMPT_VERSION",
    "PROHIBITED_PHRASES", "SAFE_CLAIM_FORMS", "cta_text", "is_cta_allowed",
    "is_cta_text_allowed",
]
