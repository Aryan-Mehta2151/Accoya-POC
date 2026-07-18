"""Deterministic validation for generated Accoya email drafts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .catalog import (
    ACCOYA_CATALOG,
    application_belongs_to_family,
    get_application,
    get_family,
)
from .models import (
    EmailDraft,
    EvidenceReference,
    NormalizedLead,
    ProductSelection,
    StrategyChunk,
    ValidationCode,
    ValidationViolation,
)
from .normalization import normalize_state_code
from .policy import (
    PROHIBITED_PHRASES,
    SAFE_CLAIM_FORMS,
    is_cta_allowed,
    is_cta_text_allowed,
)
from .routing import routing_term_supported


_WORD_RE = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*%|[-–]\w+)?\b")
_GREETING_RE = re.compile(r"^\s*(?:hi|hello|dear)\s+([^,\n]+)", re.IGNORECASE)
_EXTRA_CTA_RE = re.compile(
    r"\b(?:could|can|may|would)\s+(?:i|we|you)\b|"
    r"\b(?:i|we)\s+(?:(?:can|could|will|would)\s+(?:also\s+)?|"
    r"(?:am|are)\s+(?:happy|glad|available)\s+to\s+)"
    r"(?:send|share|provide|arrange|schedule|discuss|review|get|deliver|"
    r"mail|forward)\b|"
    r"\bplease\s+(?:reply|respond|call|email|contact|send|share|"
    r"let\s+(?:me|us)\s+know)\b|"
    r"\b(?:reply|respond)\s+if\b|"
    r"\b(?:reply|respond|call|email|contact)\s+(?:me|us)\b|"
    r"\b(?:send|share)\s+(?:me|us)\s+(?:your|the|a)\b|"
    r"\bfeel\s+free\s+to\s+(?:reply|respond|call|email|contact|reach\s+out)\b|"
    r"\bi\s+invite\s+you\s+to\s+(?:reply|respond|call|email|contact)\b|"
    r"\b(?:let\s+(?:me|us)\s+know|are you available|do you have time|"
    r"shall we|reach out)\b|"
    r"\bhappy\s+to\s+(?:share|send|provide|discuss)\b|"
    r"\blet['’]?s\s+(?:connect|talk|discuss)\b|"
    r"\b(?:drop\s+me\s+a\s+line|get\s+in\s+touch|reach\s+me\s+directly|"
    r"you\s+can\s+reply\s+anytime|we\s+welcome\s+your\s+reply|"
    r"book\s+a\s+call|reply\s+anytime)\b|"
    r"\b(?:a\s+)?(?:brochure|sample|details?|information)\s+"
    r"(?:is|are)\s+available\s+if\s+(?:useful|helpful|needed)\b|"
    r"\btechnical\s+information\s+is\s+available\s+upon\s+request\b|"
    r"\bwe\s+have\s+samples?\s+ready\b|"
    r"\bsamples?\s+can\s+be\s+provided\b|"
    r"\b(?:i['’]?d|i\s+would)\s+(?:be\s+)?glad\s+to\s+(?:connect|talk|help)\b|"
    r"\b(?:call|write\s+back|reply|message\s+me)\s+anytime\b|"
    r"\b(?:just\s+reply|we\s+should\s+connect|i\s+can\s+follow\s+up|"
    r"i\s+am\s+available\s+for\s+a\s+call|we\s+can\s+explore\s+next\s+steps|"
    r"(?:i['’]?d|i\s+would)\s+welcome\s+a\s+reply|talk\s+soon)\b",
    re.IGNORECASE,
)
_PLANNING_FINALITY_RE = re.compile(
    r"\b(?:material\s+(?:is|was)\s+final|finali[sz]ed|has\s+(?:been\s+)?chosen|"
    r"will\s+use|is\s+using|(?:has|have)\s+(?:been\s+)?selected|"
    r"(?:is|was)\s+selected|(?:team|client|owner|architect)\s+"
    r"(?:has\s+)?(?:chosen|chose|selected|specified)|"
    r"(?:team|client|owner|architect)\s+(?:plans?\s+to\s+use|"
    r"intends?\s+to\s+(?:specify|use)|expects?\s+to\s+use|"
    r"agreed\s+to\s+use)|"
    r"material\s+choice\s+is\s+locked\s+in|"
    r"(?:team|client|owner|architect)\s+(?:settled\s+on|committed\s+to)|"
    r"Accoya\s+is\s+the\s+(?:chosen\s+material|selected\s+product)|"
    r"the\s+chosen\s+option\s+is\s+Accoya|"
    r"(?:we|the\s+team)\s+are\s+going\s+with\s+Accoya|"
    r"team\s+has\s+committed\s+to\s+Accoya|"
    r"owner\s+settled\s+on\s+Accoya|moving\s+forward\s+with\s+Accoya|"
    r"Accoya\s+will\s+be\s+specified|we\s+chose\s+Accoya|"
    r"the\s+specification\s+calls\s+for\s+Accoya|"
    r"Accoya\s+is\s+set\s+for\s+the\s+project|"
    r"the\s+project\s+is\s+proceeding\s+with\s+Accoya|"
    r"Accoya\s+is\s+confirmed|"
    r"is\s+specified|was\s+awarded)\b",
    re.IGNORECASE,
)
_MANUFACTURED_SYSTEM_RE = re.compile(
    r"\bAccoya\s+(?:manufacture[sd]?|make[sd]?|made|builds?|built|"
    r"fabricate[sd]?|suppl(?:y|ies|ied)|produce[sd]?|create[sd]?|"
    r"deliver(?:s|ed)?|install(?:s|ed)?|construct(?:s|ed)?|develop(?:s|ed)?)\s+"
    r"(?:(?:the|this|your|our|their|a)\s+)?(?:finished\s+)?"
    r"(?:window|door|deck|siding|"
    r"cladding|fa[cç]ade|system)\b|\b(?:finished\s+)?(?:window|door|deck|"
    r"siding|cladding|fa[cç]ade|system)\s+(?:is|was)\s+"
    r"(?:manufacture[sd]?|made|built|fabricated|supplied|produced|created|"
    r"provided|furnished)\s+"
    r"by\s+Accoya\b|"
    r"\bAccoya\s+was\s+responsible\s+for\s+(?:the\s+)?(?:finished\s+)?"
    r"(?:window|door|deck|siding|cladding|fa[cç]ade|system)\b|"
    r"\b(?:the\s+)?(?:window|door|deck|siding|cladding|fa[cç]ade|system)\s+"
    r"came\s+from\s+Accoya\b|"
    r"\bAccoya\s+(?:provide[sd]?|furnish(?:es|ed)?)\s+"
    r"(?:(?:the|this|your|our|their|a)\s+)?(?:finished\s+)?"
    r"(?:window|door|deck|siding|cladding|fa[cç]ade|system)\b|"
    r"\bAccoya\s+handled\s+(?:the\s+)?(?:window|door|deck|siding|"
    r"cladding|fa[cç]ade|system)\s+installation\b",
    re.IGNORECASE,
)
_ENVIRONMENTAL_SUPERLATIVE_RE = re.compile(
    r"\b(?:most\s+(?:sustainable|eco[- ]friendly|environmentally\s+friendly|green)|"
    r"greenest|eco[- ]friendliest|best\s+for\s+the\s+environment|"
    r"environmentally\s+superior|ultimate\s+eco[- ]friendly|zero[- ]carbon|"
    r"carbon[- ]negative)\b",
    re.IGNORECASE,
)
_ABSOLUTE_CLAIM_RE = re.compile(
    r"\b(?:never\s+(?:warp|warps|fail|fails|crack|cracks|splinter|splinters|rot)|"
    r"(?:cannot|can't)\s+(?:warp|fail|crack|splinter|rot)|"
    r"(?:won't|will\s+not)\s+(?:warp|fail|crack|splinter|rot)|"
    r"requires?\s+no\s+maintenance|no\s+maintenance\s+(?:is\s+)?required|"
    r"(?:virtually\s+)?maintenance[- ]free|maintenance\s+is\s+unnecessary|"
    r"(?:zero|no)\s+upkeep|without\s+splinters?|"
    r"never\s+needs?\s+upkeep|maintenance\s+will\s+never\s+be\s+needed|"
    r"needs?\s+virtually\s+no\s+maintenance|will\s+not\s+need\s+upkeep|"
    r"eliminates?\s+the\s+need\s+for\s+upkeep|"
    r"no\s+ongoing\s+care\s+is\s+needed|"
    r"splinters?\s+are\s+impossible|incapable\s+of\s+cracking|"
    r"does\s+not\s+splinter|eliminates?\s+maintenance|permanently\s+stable|"
    r"(?:crack|splinter|warp|weather|water|fire)[- ]?proof|"
    r"(?:immune|impervious)\s+to\s+(?:rot|decay)|lasts?\s+forever|"
    r"ADA[- ]compliant|free\s+of\s+(?:splinters?|cracks?|maintenance)|"
    r"(?:unmatched|unparalleled|unsurpassed)\s+"
    r"(?:sustainability|performance|durability|quality))\b",
    re.IGNORECASE,
)
_WARRANTY_TERM_RE = re.compile(
    r"\b(?:warrant(?:y|ies|ied)|guarantee(?:d|s)?)\b", re.IGNORECASE
)
_TRICOYA_SOLID_RE = re.compile(
    r"\b(?:solid[- ](?:lumber|wood|timber|boards?)|"
    r"(?:sawn|dimensional)\s+(?:lumber|wood|timber))\b",
    re.IGNORECASE,
)
_KNOWN_COMPETITOR_RE = re.compile(
    r"\b(?:Thermory|Trex|TimberTech|Kebony|AZEK|Ipe|Ipé|Arbor\s+Wood|"
    r"composite(?:\s+wood)?\s+decking)\b",
    re.IGNORECASE,
)
_PROPER_TOKEN_RE = re.compile(r"\b(?:[A-Z][a-zÀ-ÖØ-öø-ÿ'-]{2,}|[A-Z]{2,})\b")
_COMMON_CAPITALIZED_WORDS = {
    "a", "about", "according", "after", "also", "although", "an", "and", "any",
    "as", "at", "based", "because", "before", "best", "both", "by", "catalog",
    "additionally", "beyond", "considering", "could", "dear", "during",
    "each", "evaluation", "even", "every",
    "exploring", "exterior", "finally", "first", "following", "for", "from", "further",
    "given", "hello", "hi", "however", "i", "if", "in", "instead", "it",
    "centering", "focusing", "happy", "here", "how", "importantly", "keeping",
    "last", "later", "lead",
    "looking", "material", "moreover",
    "meanwhile", "next", "no", "not",
    "now", "one", "only", "our", "overall", "planning", "please", "project",
    "perhaps", "recognizing", "regarding", "reviewing", "second", "should",
    "similarly", "since", "so", "staying",
    "offering", "otherwise", "sharing", "some", "specifically", "still",
    "such", "supporting", "technical", "thanks", "that", "the",
    "then", "there", "therefore",
    "these", "this", "those", "through", "to", "today", "together", "under",
    "ultimately", "using", "we", "what", "when", "where", "whether", "while",
    "why", "with", "without",
    "would", "yet", "your", "alternatively",
}
_FACT_ASSERTION_RE = re.compile(
    r"\b(?:the\s+)?(?!(?:we|i)\b)(?:architect|contractor|client|owner|"
    r"developer|team|(?-i:[A-Z][A-Za-z'-]+"
    r"(?:\s+[A-Z][A-Za-z'-]+){0,2}))\s+"
    r"(?:has\s+|have\s+)?(?:approved|confirmed|chose|chosen|selected|"
    r"specified|awarded|decided|set|scheduled|prefers?|wants?|requested?|"
    r"expects?|favou?rs?|recommends?|requires?|likes?|needs?|seeks?|"
    r"asked\s+for|is\s+(?:considering|evaluating|interested\s+in)|"
    r"hopes?\s+to\s+use|agreed\s+to\s+use|calls?\s+for|"
    r"plans?\s+to\s+use|intends?\s+to\s+(?:specify|use))\b[^.!?\n]{0,80}",
    re.IGNORECASE,
)
_PROJECT_STATE_RE = re.compile(
    r"\b(?:construction\s+(?:begins?|starts?|commences?)[^.!?\n]{0,50}|"
    r"funding\s+(?:is|has\s+been)\s+(?:fully\s+)?secured|"
    r"(?:the\s+)?project\s+is\s+fully\s+funded|"
    r"(?:the\s+)?permit\s+has\s+been\s+issued|"
    r"materials?\s+(?:(?:is|are)\s+ready\s+to\s+order|"
    r"(?:is|are)\s+being\s+purchased|have\s+(?:already\s+)?been\s+ordered)|"
    r"(?:site\s+work|the\s+bid|construction)\s+is\s+underway|"
    r"installation\s+starts?\s+soon|funding\s+is\s+available|"
    r"the\s+contractor\s+is\s+onboard|"
    r"(?:the\s+)?design\s+is\s+approved|permitting\s+is\s+complete|"
    r"(?:the\s+)?project\s+is\s+shovel[- ]ready|"
    r"(?:the\s+)?work\s+starts?\s+soon|"
    r"(?:the\s+)?project\s+has\s+entered\s+construction|"
    r"(?:the\s+)?site\s+is\s+ready|"
    r"(?:the\s+)?team\s+is\s+ready\s+to\s+order|"
    r"(?:the\s+)?project\s+is\s+on\s+track|"
    r"targeting\s+a\s+spring\s+start|"
    r"(?:the\s+)?site\s+has\s+harsh\s+winters|"
    r"(?:the\s+)?order\s+window\s+closes?\s+soon|"
    r"bids?\s+close\s+soon|"
    r"(?!(?:we|i)\b)(?:[A-Z][A-Za-z'-]+|architect|contractor|client|"
    r"owner|developer)\s+is\s+the\s+project\s+"
    r"(?:owner|architect|contractor|developer))\b",
    re.IGNORECASE,
)
_PROJECT_ROLE_RE = re.compile(
    r"\b(?:for|with|to)\s+(?:the\s+)?project\s+"
    r"(?:engineer|architect|owner|contractor|developer|manager)\b",
    re.IGNORECASE,
)
_PROJECT_DETAIL_RE = re.compile(
    r"\b(?:budget|deadline|schedule|timeline|completion\s+date|climate|"
    r"procurement|humid\s+(?:coastal\s+)?conditions?|coastal\s+conditions?)\b",
    re.IGNORECASE,
)
_PROJECT_TYPE_RE = re.compile(
    r"\b(?:hospital|library|plaza|school|hotel|resort|office|campus|"
    r"residence|cabin|station|airport|arena|stadium|warehouse|factory|"
    r"facility|museum|theat(?:er|re)|center|centre|tower|complex|"
    r"development|building|house|home|apartments?|condominium|restaurant|"
    r"retail|store|clinic|laborator(?:y|ies)|courthouse|church|temple|"
    r"monument|universit(?:y|ies)|park|pavilion|marina)\b",
    re.IGNORECASE,
)
_NUMBERED_WARRANTY_RE = re.compile(
    r"\b\d+(?:[-– ]year)?\s+(?:limited\s+)?warrant(?:y|ies)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_APPLICATION_TERMS = {
    "bridge",
    "gate",
    "signs",
    "structure",
    "window",
}
_STAGE_EVIDENCE_PATTERNS = {
    "planning": re.compile(
        r"\b(?:planning|planned|proposed|concept(?:ual)?|schematic(?:\s+design)?|"
        r"pre[- ]design|feasibility|early[- ]stage|design\s+intent)\b",
        re.IGNORECASE,
    ),
    "specification": re.compile(
        r"\b(?:specification(?:s)?|specif(?:y|ying|ied)|spec\s+"
        r"(?:review|language|package)|design(?:\s+development)?|"
        r"construction\s+documents?|material\s+selection|technical\s+review|"
        r"submittals?|CSI)\b",
        re.IGNORECASE,
    ),
    "procurement": re.compile(
        r"\b(?:procurement|purchas(?:e|ed|ing)|bid(?:ding|s)?|tender(?:ing)?|"
        r"RFQ|request\s+for\s+quote|quot(?:e|ed|ing)|award(?:ed|ing)?|"
        r"supplier\s+selection|order(?:ed|ing)?|"
        r"construction\s+(?:underway|started|phase)|installation)\b",
        re.IGNORECASE,
    ),
}
_EXPLICIT_STAGE_FIELDS = {"projectstage", "stage"}
_CONTACT_FIELD_TOKENS = {"contact", "contacts", "contactemail", "email"}
_TECHNICAL_CLAIM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b\d+(?:[-– ]year)?\s+service\s+life\b",
        r"\bclass\s+1\s+durability\b",
        r"\b(?:high\s+)?dimensional(?:ly)?\s+stability\b",
        r"\bresistan(?:t|ce)\s+to\s+rot(?:\s+and\s+fungal\s+decay)?\b",
        r"\b(?:decay|rot|fungal\s+decay)[- ]resistan(?:t|ce)\b",
        r"\bresists?\s+rot(?:\s+and\s+fungal\s+decay)?\b",
        r"\brot\s+or\s+fungal\s+decay\b",
        r"\bresists?\s+(?:swelling|warping|splintering)\b",
        r"\b(?:exceptional|excellent|enhanced|superior|high)\s+"
        r"(?:fire\s+resistance|thermal\s+performance)\b",
        r"\b(?:fire|weather|moisture)[- ]resistan(?:t|ce)\b",
        r"\bresists?\s+(?:weather|moisture)\b",
        r"\bresists?\s+(?:decay|termites?)\b",
        r"\bstrong\s+resistance\s+to\s+insects?\b",
        r"\b(?:water|termite|mold|impact)[- ]resistan(?:t|ce)\b",
        r"\bdimensionally\s+stable\b",
        r"\bstable\s+outdoors\b",
        r"\blow\s+maintenance\b",
        r"\bminimal\s+upkeep\b",
        r"\blow\s+upkeep\b",
        r"\bconsistent\s+walking\s+surface\b",
        r"\bservice\s+life\b",
        r"\b(?:designed|built|engineered)\s+to\s+last\s+(?:for\s+)?decades\b",
        r"\bbuilt\s+to\s+endure\b",
        r"\bdecades\s+of\s+use\b",
        r"\b(?:lower|reduced)\s+carbon\s+footprint\b",
        r"\bsustainably\s+sourced\b",
        r"\brenewable(?:,?\s+(?:and\s+)?)?low[- ]carbon\s+material\b",
        r"\b(?:beautiful|attractive|natural|consistent|uniform|distinctive)\s+"
        r"(?:appearance|finish|look|aesthetic)\b",
        r"\b(?:exceptional|excellent|enhanced|superior|improved|high|greater)\s+"
        r"(?:[\w-]+\s+){0,2}(?:durability|performance|stability|strength|"
        r"resistance|insulation|workability|versatility)\b",
        r"\b(?:exceptionally\s+)?(?:durable|long[- ]lasting|hard[- ]wearing|"
        r"resilient|robust|versatile|easy\s+to\s+"
        r"(?:machine|coat|finish|maintain))\b",
        r"\bdurable\s+outdoors\b",
        r"\bbuilt\s+for\s+harsh\s+conditions\b",
        r"\b(?:provides?|offers?)\s+thermal\s+insulation\b",
        r"\bhandles?\s+heavy\s+foot\s+traffic\b",
        r"\bhelps?\s+reduce\s+upkeep\b",
        r"\bstays?\s+stable\s+in\s+changing\s+weather\b",
        r"\bsupports?\s+long[- ]term\s+durability\b",
        r"\bbuilt\s+for\s+high[- ]traffic\s+areas?\b",
        r"\bless\s+prone\s+to\s+movement\b",
        r"\b(?:enhances?\s+stability|minimi[sz]es?\s+movement|"
        r"limits?\s+swelling|reduces?\s+(?:swelling|movement|shrinkage))\b",
        r"\bweather\s+durability\b",
        r"\bneeds?\s+less\s+maintenance\b",
        r"\bresponsibly\s+sourced\b",
        r"\bcarbon[- ]efficient\b",
        r"\b(?:improved|reduced|lower)\s+"
        r"(?:maintenance|movement|swelling|shrinkage)\b",
        r"\b(?:eco[- ]friendly|environmentally\s+friendly)\b",
        r"\b(?:sustainable|renewable|low[- ]carbon|non[- ]toxic)\s+"
        r"(?:material|product|option|choice|wood)\b",
    )
)


def validate_email(
    draft: EmailDraft,
    *,
    lead: NormalizedLead,
    selection: ProductSelection,
    strategy_chunks: Iterable[StrategyChunk],
) -> list[ValidationViolation]:
    """Return every deterministic violation found in ``draft``.

    Only a draft producing an empty list is safe to expose to a caller.
    """

    chunks = list(strategy_chunks)
    chunk_by_id = {chunk.document_id: chunk for chunk in chunks}
    violations: list[ValidationViolation] = []
    combined = f"{draft.subject}\n{draft.body}"

    if not draft.subject.strip() or len(draft.subject.strip()) > 60:
        violations.append(
            _v(
                "subject_too_long",
                "Subject must contain 1-60 characters.",
                "subject",
                draft.subject,
            )
        )

    word_count = len(_WORD_RE.findall(draft.body))
    if not 90 <= word_count <= 150:
        violations.append(
            _v(
                "body_word_count",
                f"Body must contain 90-150 words; found {word_count}.",
                "body",
                str(word_count),
            )
        )
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", draft.body) if part.strip()]
    if not 2 <= len(paragraphs) <= 3:
        violations.append(
            _v(
                "paragraph_count",
                f"Body must contain two or three paragraphs; found {len(paragraphs)}.",
                "body",
                str(len(paragraphs)),
            )
        )

    _validate_product(draft, lead, selection, combined, violations)
    _validate_cta(draft, lead, selection, violations)
    _validate_claim_language(draft, violations)
    _validate_lead_grounding(
        draft, lead, selection, chunks, paragraphs, combined, violations
    )
    _validate_strategy_grounding(draft, lead, selection, chunks, chunk_by_id, violations)

    if len(draft.benefits) > 3:
        violations.append(
            _v(
                "too_many_benefits",
                "No more than three main benefits may be used.",
                "benefits",
                str(len(draft.benefits)),
            )
        )

    if _enum_value(lead.project_stage) == "planning" and _PLANNING_FINALITY_RE.search(
        draft.body
    ):
        violations.append(
            _v(
                "planning_finality",
                "The draft implies a final decision although the lead is in planning.",
                "body",
                _PLANNING_FINALITY_RE.search(draft.body).group(0),
            )
        )
    manufactured = _MANUFACTURED_SYSTEM_RE.search(draft.body)
    if manufactured:
        violations.append(
            _v(
                "manufactured_system_claim",
                "The draft claims Accoya manufactured or supplied a finished system.",
                "body",
                manufactured.group(0),
            )
        )
    tricoya_solid = (
        _TRICOYA_SOLID_RE.search(combined)
        if "tricoya" in combined.casefold()
        else None
    )
    if tricoya_solid:
        violations.append(
            _v(
                "unknown_product",
                "Tricoya Panels must not be described as solid lumber.",
                "body",
                tricoya_solid.group(0),
            )
        )

    return _deduplicate(violations)


def _validate_product(
    draft: EmailDraft,
    lead: NormalizedLead,
    selection: ProductSelection,
    combined: str,
    violations: list[ValidationViolation],
) -> None:
    family = draft.selected_product_family
    application = draft.selected_application
    family_entry = get_family(family)
    application_entry = get_application(application, family_id=family)
    if family_entry is None:
        violations.append(
            _v("unknown_product", "Selected product family is not in the catalog.", "selected_product_family", family)
        )
    if application_entry is None:
        violations.append(
            _v("unknown_application", "Selected application is not in the catalog.", "selected_application", application)
        )
    belongs = application_belongs_to_family(family, application)
    if not belongs:
        violations.append(
            _v(
                "application_family_mismatch",
                "Selected application does not belong to the selected product family.",
                "selected_application",
                application,
            )
        )
    if (
        family != selection.selected_product_family
        or application != selection.selected_application
    ):
        violations.append(
            _v(
                "selection_mismatch",
                "Draft product/application must exactly match analyzed selection.",
                "selected_product_family",
                f"{family}/{application}",
            )
        )

    present_families = _mentioned_family_ids(combined)
    if len(present_families) > 1 or (
        present_families and family not in present_families
    ):
        violations.append(
            _v(
                "product_count",
                "Email must discuss exactly one primary Accoya product family.",
                "body",
                ", ".join(sorted(present_families)),
            )
        )
    present_applications = _mentioned_application_ids(
        combined,
        selected_application=application,
        lead=lead,
        evidence=draft.lead_evidence_used,
    )
    other_applications = present_applications - {application}
    if other_applications:
        violations.append(
            _v(
                "product_count",
                "Email must discuss exactly one primary catalog application.",
                "body",
                ", ".join(sorted(other_applications)),
            )
        )
    family_terms = (family_entry.display_name,) if family_entry is not None else ()
    application_terms = (
        (application_entry.display_name, *application_entry.aliases)
        if application_entry is not None
        else ()
    )
    family_mentioned = any(
        term.casefold() in combined.casefold() for term in family_terms if term
    )
    application_mentioned = any(
        term.casefold() in combined.casefold() for term in application_terms if term
    )
    if not family_mentioned or not application_mentioned:
        violations.append(
            _v(
                "product_count",
                "Email copy must name the selected product family and application.",
                "body",
                f"family={family_mentioned}, application={application_mentioned}",
            )
        )


def _validate_cta(
    draft: EmailDraft,
    lead: NormalizedLead,
    selection: ProductSelection,
    violations: list[ValidationViolation],
) -> None:
    cta = draft.cta_text.strip()
    occurrence_count = draft.body.casefold().count(cta.casefold()) if cta else 0
    body_without_cta = _remove_once(draft.body, cta)
    extra_requests = list(_EXTRA_CTA_RE.finditer(body_without_cta))
    if not cta or occurrence_count != 1 or extra_requests or body_without_cta.count("?"):
        detail = f"declared CTA occurrences={occurrence_count}, additional requests={len(extra_requests)}"
        violations.append(
            _v(
                "cta_count",
                "Body must contain exactly one clear declared CTA and no second request.",
                "cta_text",
                detail,
            )
        )
    if not is_cta_allowed(lead.project_stage, draft.cta_type):
        violations.append(
            _v(
                "cta_stage_mismatch",
                "CTA type is not allowed for the normalized project stage.",
                "cta_type",
                _enum_value(draft.cta_type),
            )
        )
    elif not is_cta_text_allowed(lead.project_stage, draft.cta_type, cta):
        violations.append(
            _v(
                'cta_stage_mismatch',
                'CTA text must exactly match the fixed template for its stage and category.',
                'cta_text',
                cta,
            )
        )
    if draft.cta_type != selection.cta_type:
        violations.append(
            _v(
                "selection_mismatch",
                "Draft CTA type must match the analyzed CTA selection.",
                "cta_type",
                _enum_value(draft.cta_type),
            )
        )


def _validate_claim_language(
    draft: EmailDraft, violations: list[ValidationViolation]
) -> None:
    combined = f"{draft.subject}\n{draft.body}"
    lowered = combined.casefold()
    for phrase in PROHIBITED_PHRASES:
        if str(phrase).casefold() in lowered:
            violations.append(
                _v(
                    "prohibited_phrase",
                    f"Prohibited claim or phrase detected: {phrase}",
                    "body",
                    str(phrase),
                )
            )
    for pattern, label in (
        (_ENVIRONMENTAL_SUPERLATIVE_RE, "unsupported environmental superlative"),
        (_ABSOLUTE_CLAIM_RE, "unsupported absolute performance claim"),
    ):
        match = pattern.search(combined)
        if match:
            violations.append(
                _v("prohibited_phrase", f"Detected {label}.", "body", match.group(0))
            )
    superiority = _competitor_superiority_match(draft, combined)
    if superiority:
        violations.append(
            _v(
                "prohibited_phrase",
                "Detected unsupported competitor superiority claim.",
                "body",
                superiority,
            )
        )


def _competitor_superiority_match(
    draft: EmailDraft, combined: str
) -> str | None:
    targets = {
        competitor.strip()
        for competitor in draft.competitor_mentions
        if competitor.strip()
    }
    targets.update(match.group(0) for match in _KNOWN_COMPETITOR_RE.finditer(combined))
    for target in sorted(targets, key=lambda value: (-len(value), value.casefold())):
        escaped = re.escape(target)
        patterns = (
            rf"\b(?:better|superior|best|preferable|a\s+stronger\s+choice|"
            rf"(?:far\s+)?more\s+[\w-]+|less\s+[\w-]+|lower\s+[\w-]+|"
            rf"greater\s+[\w-]+)\s+(?:than|to|versus|compared\s+(?:with|to))\s+"
            rf"{escaped}\b",
            rf"\b(?:an?\s+upgrade|advantages?)\s+over\s+{escaped}\b",
            rf"\b(?:outperform(?:s|ed|ing)?|outlast(?:s|ed|ing)?|beats?|"
            rf"exceeds?|surpass(?:es|ed|ing)?)\s+{escaped}\b",
        )
        for pattern in patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return match.group(0)
    return None


def _validate_lead_grounding(
    draft: EmailDraft,
    lead: NormalizedLead,
    selection: ProductSelection,
    strategy_chunks: list[StrategyChunk],
    paragraphs: list[str],
    combined: str,
    violations: list[ValidationViolation],
) -> None:
    trigger = (
        selection.exact_source_trigger.quote.strip()
        if selection.exact_source_trigger is not None
        else ""
    )
    opening = paragraphs[0] if paragraphs else ""
    trigger_supported = _trigger_supports_selection(trigger, lead, selection)
    if not trigger or not _contains_phrase(opening, trigger) or not trigger_supported:
        violations.append(
            _v(
                "opening_ungrounded",
                "Opening paragraph must include the exact source-backed sales trigger.",
                "body",
                trigger,
            )
        )
    if trigger and not trigger_supported:
        violations.append(
            _v(
                "evidence_invalid",
                "The exact source trigger must support the selected catalog pair.",
                "lead_evidence_used",
                trigger,
            )
        )

    valid_evidence_quotes: set[str] = set()
    valid_lead_evidence: list[EvidenceReference] = []
    for raw_evidence in draft.lead_evidence_used:
        try:
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, EvidenceReference)
                else EvidenceReference.model_validate(raw_evidence)
            )
        except Exception:
            violations.append(
                _v(
                    "evidence_invalid",
                    "Lead evidence entry does not match the evidence schema.",
                    "lead_evidence_used",
                    str(raw_evidence),
                )
            )
            continue
        if _enum_value(evidence.source_type) != "lead" or not _lead_evidence_valid(
            evidence, lead
        ):
            violations.append(
                _v(
                    "evidence_invalid",
                    "Lead evidence must quote the declared lead field exactly.",
                    "lead_evidence_used",
                    evidence.quote,
                )
            )
            continue
        valid_evidence_quotes.add(evidence.quote.casefold())
        valid_lead_evidence.append(evidence)
        if not _contains_phrase(combined, evidence.quote):
            violations.append(
                _v(
                    "project_fact_ungrounded",
                    "Declared lead evidence is not referenced in the draft.",
                    "lead_evidence_used",
                    evidence.quote,
                )
            )
    if trigger and trigger.casefold() not in valid_evidence_quotes:
        violations.append(
            _v(
                "evidence_invalid",
                "The exact source trigger must appear in the lead evidence ledger.",
                "lead_evidence_used",
                trigger,
            )
        )

    if (
        lead.project
        and _contains_phrase(combined, lead.project)
        and not _ledger_covers_phrase(lead.project, valid_lead_evidence)
    ):
        violations.append(
            _v(
                "evidence_invalid",
                "A project name used in the draft must have declared lead evidence.",
                "lead_evidence_used",
                lead.project,
            )
        )

    if _enum_value(lead.project_stage) != "unknown" and not any(
        _evidence_supports_stage(evidence, lead.project_stage)
        for evidence in valid_lead_evidence
    ):
        violations.append(
            _v(
                "evidence_invalid",
                "The routed project stage must have declared exact lead evidence.",
                "lead_evidence_used",
                _enum_value(lead.project_stage),
            )
        )

    lead_text = _all_lead_text(lead).casefold()
    declared_competitors = {
        competitor.strip().casefold()
        for competitor in draft.competitor_mentions
        if competitor.strip()
    }
    for competitor in draft.competitor_mentions:
        if competitor.casefold() not in lead_text:
            violations.append(
                _v(
                    "competitor_ungrounded",
                    "Named competitor is absent from the supplied lead.",
                    "competitor_mentions",
                    competitor,
                )
            )
        if competitor.casefold() not in combined.casefold():
            violations.append(
                _v(
                    "evidence_invalid",
                    "Declared competitor is not referenced in the email copy.",
                    "competitor_mentions",
                    competitor,
                )
            )
        if not _ledger_covers_phrase(competitor, valid_lead_evidence):
            violations.append(
                _v(
                    "evidence_invalid",
                    "A competitor used in the draft must have declared lead evidence.",
                    "lead_evidence_used",
                    competitor,
                )
            )

    lead_signals = {
        signal.strip()
        for signal in (selection.material_signal, selection.named_competitor)
        if signal and signal.strip() and _contains_phrase(combined, signal)
    }
    lead_signals.update(
        evidence.quote
        for evidence in (*lead.material_mentions, *lead.competitor_mentions)
        if _contains_phrase(combined, evidence.quote)
    )
    for signal in sorted(lead_signals, key=str.casefold):
        if not _ledger_covers_phrase(signal, valid_lead_evidence):
            violations.append(
                _v(
                    "evidence_invalid",
                    "A material or competitor fact must have declared lead evidence.",
                    "lead_evidence_used",
                    signal,
                )
            )

    for match in _KNOWN_COMPETITOR_RE.finditer(combined):
        competitor = match.group(0)
        if (
            competitor.casefold() not in lead_text
            or competitor.casefold() not in declared_competitors
        ):
            violations.append(
                _v(
                    "competitor_ungrounded",
                    "Every competitor in the copy must appear in the lead and competitor ledger.",
                    "body",
                    competitor,
                )
            )

    evidence_corpus = " ".join(sorted(valid_evidence_quotes))
    chunk_by_id = {
        chunk.document_id: chunk for chunk in strategy_chunks if chunk.is_approved
    }
    declared_source_ids = set(draft.strategy_source_ids)
    for number_match in _NUMBER_RE.finditer(combined):
        number = number_match.group(0)
        if (
            number.casefold() not in evidence_corpus
            and not _number_has_strategy_support(
                number_match,
                combined,
                draft,
                chunk_by_id,
                declared_source_ids,
            )
        ):
            violations.append(
                _v(
                    "project_fact_ungrounded",
                    "A number must come from lead evidence or an approved technical claim.",
                    "body",
                    number,
                )
            )

    contact_names = [
        str(getattr(contact, "name", "")).strip()
        for contact in lead.contacts
        if getattr(contact, "name", None)
    ]
    entity_corpus = " ".join(
        (
            evidence_corpus,
            _catalog_text().casefold(),
            draft.cta_text.casefold(),
        )
    )
    for match in _PROPER_TOKEN_RE.finditer(combined):
        token = match.group(0)
        normalized = token.casefold()
        if normalized in _COMMON_CAPITALIZED_WORDS:
            continue
        if _is_sentence_initial_gerund(match, combined):
            continue
        if re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", entity_corpus):
            continue
        violations.append(
            _v(
                "project_fact_ungrounded",
                "Named entity in the draft is absent from grounded sources.",
                "body",
                token,
            )
        )

    for pattern, message in (
        (
            _FACT_ASSERTION_RE,
            "A project decision or approval assertion is absent from exact lead evidence.",
        ),
        (
            _PROJECT_DETAIL_RE,
            "A project-specific detail is absent from exact lead evidence.",
        ),
        (
            _PROJECT_STATE_RE,
            "A project status, timing, or stakeholder-role assertion is absent "
            "from exact lead evidence.",
        ),
        (
            _PROJECT_ROLE_RE,
            "A project recipient role is absent from exact lead evidence.",
        ),
        (
            _PROJECT_TYPE_RE,
            "A project type is absent from exact lead evidence.",
        ),
    ):
        for match in pattern.finditer(combined):
            if pattern is _PROJECT_DETAIL_RE and _project_detail_is_negated(
                combined, match
            ):
                continue
            value = match.group(0).strip()
            if not any(
                _contains_phrase(quote, value)
                if pattern in {
                    _FACT_ASSERTION_RE,
                    _PROJECT_STATE_RE,
                    _PROJECT_ROLE_RE,
                }
                else _contains_phrase(quote, match.group(0))
                for quote in valid_evidence_quotes
            ):
                violations.append(
                    _v(
                        "project_fact_ungrounded",
                        message,
                        "body",
                        value,
                    )
                )

    greeting = _GREETING_RE.search(draft.body)
    if greeting:
        recipient = greeting.group(1).strip()
        known_names = {name.casefold() for name in contact_names}
        known_first_names = {
            name.split()[0].casefold() for name in contact_names if name.split()
        }
        if (
            recipient.casefold() not in known_names
            and recipient.casefold() not in known_first_names
        ):
            violations.append(
                _v(
                    "recipient_ungrounded",
                    "Greeting recipient is not a parsed lead contact.",
                    "body",
                    recipient,
                )
            )
        elif not any(
            _key_token(evidence.source_field or "") in _CONTACT_FIELD_TOKENS
            and _contains_phrase(evidence.quote, recipient)
            for evidence in valid_lead_evidence
        ):
            violations.append(
                _v(
                    "evidence_invalid",
                    "A greeting recipient must have declared exact contact evidence.",
                    "lead_evidence_used",
                    recipient,
                )
            )


def _validate_strategy_grounding(
    draft: EmailDraft,
    lead: NormalizedLead,
    selection: ProductSelection,
    chunks: list[StrategyChunk],
    chunk_by_id: Mapping[str, StrategyChunk],
    violations: list[ValidationViolation],
) -> None:
    declared_source_ids = set(draft.strategy_source_ids)
    used_strategy_ids: set[str] = set()
    for chunk in chunks:
        if str(chunk.metadata.get("status", "")).strip().casefold() != "approved":
            violations.append(
                _v(
                    "strategy_source_unapproved",
                    "Every strategy source must have status=approved metadata.",
                    "strategy_source_ids",
                    chunk.document_id,
                )
            )

    for document_id in draft.strategy_source_ids:
        if document_id not in chunk_by_id:
            violations.append(
                _v(
                    "evidence_invalid",
                    "Draft cites a strategy document that was not retrieved.",
                    "strategy_source_ids",
                    document_id,
                )
            )

    for benefit in draft.benefits:
        if benefit.claim.casefold() not in draft.body.casefold():
            violations.append(
                _v(
                    "evidence_invalid",
                    "Each declared benefit claim must appear in the email body.",
                    "benefits",
                    benefit.claim,
                )
            )
        if not benefit.evidence:
            violations.append(
                _v(
                    "evidence_invalid",
                    "Every benefit claim requires approved strategy evidence.",
                    "benefits",
                    benefit.claim,
                )
            )
        for raw_evidence in benefit.evidence:
            try:
                evidence = (
                    raw_evidence
                    if isinstance(raw_evidence, EvidenceReference)
                    else EvidenceReference.model_validate(raw_evidence)
                )
            except Exception:
                violations.append(
                    _v(
                        "evidence_invalid",
                        "Benefit evidence entry does not match the evidence schema.",
                        "benefits",
                        str(raw_evidence),
                    )
                )
                continue
            if _enum_value(evidence.source_type) == "strategy":
                used_strategy_ids.add(evidence.source_id)
            chunk = chunk_by_id.get(evidence.source_id)
            if (
                _enum_value(evidence.source_type) != "strategy"
                or chunk is None
                or evidence.source_id not in declared_source_ids
                or evidence.quote.casefold() not in chunk.text.casefold()
                or benefit.claim.casefold() not in evidence.quote.casefold()
                or str(chunk.metadata.get("status", "")).strip().casefold()
                != "approved"
            ):
                violations.append(
                    _v(
                        "evidence_invalid",
                        "Benefit evidence must exactly quote a retrieved approved chunk.",
                        "benefits",
                        evidence.quote,
                    )
                )

    for document_id in sorted(declared_source_ids - used_strategy_ids):
        violations.append(
            _v(
                "evidence_invalid",
                "Every declared strategy source must support a declared benefit.",
                "strategy_source_ids",
                document_id,
            )
        )

    claim_copy = f"{draft.subject}\n{draft.body}"
    technical_matches = {
        match.group(0)
        for pattern in _TECHNICAL_CLAIM_PATTERNS
        for match in pattern.finditer(claim_copy)
    }
    technical_matches.update(
        phrase
        for phrase in SAFE_CLAIM_FORMS
        if "warranty" not in phrase.casefold()
        and phrase.casefold() in claim_copy.casefold()
    )
    detected_benefits = _distinct_technical_claims(claim_copy)
    if len(detected_benefits) > 3:
        violations.append(
            _v(
                "too_many_benefits",
                "Email copy contains more than three distinct benefit claims.",
                "body",
                str(len(detected_benefits)),
            )
        )
    for claim_text in sorted(technical_matches, key=str.casefold):
        supported = any(
            claim_text.casefold() in benefit.claim.casefold()
            and _benefit_has_valid_evidence(
                benefit, chunk_by_id, declared_source_ids
            )
            for benefit in draft.benefits
        )
        if not supported:
            violations.append(
                _v(
                    "evidence_invalid",
                    "Technical claims must be declared as benefits with exact approved evidence.",
                    "body",
                    claim_text,
                )
            )

    if not chunks and draft.benefits:
        violations.append(
            _v(
                "strategy_source_unapproved",
                "Catalog-only fallback cannot contain strategy benefit claims.",
                "benefits",
                str(len(draft.benefits)),
            )
        )

    warranty_copy = f"{draft.subject} {draft.body}"
    for mention in _WARRANTY_TERM_RE.finditer(warranty_copy):
        if _warranty_mention_supported(
            mention,
            warranty_copy,
            draft,
            lead,
            selection,
            chunk_by_id,
        ):
            continue
        violations.append(
            _v(
                "warranty_unsupported",
                "Each warranty or guarantee phrase requires matching approved "
                "product/application/geography support.",
                "body",
                mention.group(0),
            )
        )


def _warranty_mention_supported(
    mention: re.Match[str],
    combined: str,
    draft: EmailDraft,
    lead: NormalizedLead,
    selection: ProductSelection,
    chunks: Mapping[str, StrategyChunk],
) -> bool:
    if not lead.state:
        return False
    declared_ids = set(draft.strategy_source_ids)
    for benefit in draft.benefits:
        claim = benefit.claim.strip()
        if not _WARRANTY_TERM_RE.search(claim):
            continue
        claim_spans = [
            match.span()
            for match in re.finditer(re.escape(claim), combined, re.IGNORECASE)
        ]
        if not any(start <= mention.start() < end for start, end in claim_spans):
            continue
        for raw_evidence in benefit.evidence:
            try:
                evidence = (
                    raw_evidence
                    if isinstance(raw_evidence, EvidenceReference)
                    else EvidenceReference.model_validate(raw_evidence)
                )
            except Exception:
                continue
            chunk = chunks.get(evidence.source_id)
            if (
                _enum_value(evidence.source_type) != "strategy"
                or evidence.source_id not in declared_ids
                or chunk is None
                or not chunk.is_approved
                or evidence.quote.casefold() not in chunk.text.casefold()
                or claim.casefold() not in evidence.quote.casefold()
            ):
                continue
            metadata = {
                str(key).casefold(): str(value).casefold()
                for key, value in chunk.metadata.items()
            }
            family = metadata.get("product_family", "")
            application = metadata.get("application", "")
            geography = metadata.get("region") or metadata.get("geography", "")
            if (
                _matches_metadata(family, selection.selected_product_family)
                and _matches_metadata(application, selection.selected_application)
                and _matches_geography(geography, lead.state)
            ):
                return True
    return False


def _benefit_has_valid_evidence(
    benefit: Any,
    chunks: Mapping[str, StrategyChunk],
    declared_ids: set[str],
) -> bool:
    for raw_evidence in benefit.evidence:
        try:
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, EvidenceReference)
                else EvidenceReference.model_validate(raw_evidence)
            )
        except Exception:
            continue
        chunk = chunks.get(evidence.source_id)
        if (
            _enum_value(evidence.source_type) == "strategy"
            and evidence.source_id in declared_ids
            and chunk is not None
            and chunk.is_approved
            and evidence.quote.casefold() in chunk.text.casefold()
            and benefit.claim.casefold() in evidence.quote.casefold()
        ):
            return True
    return False


def _number_has_strategy_support(
    number: re.Match[str],
    combined: str,
    draft: EmailDraft,
    chunks: Mapping[str, StrategyChunk],
    declared_ids: set[str],
) -> bool:
    """Allow strategy numbers only inside recognized, evidence-backed claims."""

    for benefit in draft.benefits:
        claim = benefit.claim
        if number.group(0).casefold() not in claim.casefold():
            continue
        support_spans = [
            match.span()
            for pattern in _TECHNICAL_CLAIM_PATTERNS
            for match in pattern.finditer(claim)
        ]
        support_spans.extend(
            match.span() for match in _NUMBERED_WARRANTY_RE.finditer(claim)
        )
        for phrase in SAFE_CLAIM_FORMS:
            if "warranty" in phrase.casefold():
                continue
            support_spans.extend(
                match.span()
                for match in re.finditer(
                    re.escape(phrase), claim, re.IGNORECASE
                )
            )
        if not support_spans or not _benefit_has_valid_evidence(
            benefit, chunks, declared_ids
        ):
            continue
        for claim_match in re.finditer(
            re.escape(claim), combined, re.IGNORECASE
        ):
            relative_start = number.start() - claim_match.start()
            relative_end = number.end() - claim_match.start()
            if any(
                start <= relative_start and relative_end <= end
                for start, end in support_spans
            ):
                return True
    return False


def _distinct_technical_claims(text: str) -> set[str]:
    spans = [
        match.span()
        for pattern in _TECHNICAL_CLAIM_PATTERNS
        for match in pattern.finditer(text)
    ]
    for phrase in SAFE_CLAIM_FORMS:
        if "warranty" in phrase.casefold():
            continue
        spans.extend(
            match.span()
            for match in re.finditer(re.escape(phrase), text, re.IGNORECASE)
        )
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return {
        re.sub(r"\s+", " ", text[start:end].casefold()).strip()
        for start, end in merged
        if text[start:end].strip()
    }


def _lead_evidence_valid(evidence: EvidenceReference, lead: NormalizedLead) -> bool:
    if (
        evidence.source_id != lead.lead_id
        or not evidence.quote.strip()
        or not evidence.source_field
    ):
        return False
    field = evidence.source_field
    value: Any = getattr(lead, field, None)
    if value is None:
        lookup = {_key_token(key): item for key, item in lead.source_values.items()}
        value = lookup.get(_key_token(field))
    return _specific_evidence_quote(evidence.quote) and _contains_phrase(
        _stringify(value), evidence.quote
    )


def _ledger_covers_phrase(
    phrase: str, evidence_entries: Iterable[EvidenceReference]
) -> bool:
    return any(
        _contains_phrase(evidence.quote, phrase)
        for evidence in evidence_entries
    )


def _evidence_supports_stage(
    evidence: EvidenceReference, project_stage: Any
) -> bool:
    stage = _enum_value(project_stage)
    field = _key_token(evidence.source_field or "")
    if field in _EXPLICIT_STAGE_FIELDS:
        return True
    if stage == "procurement" and field == "awardedto":
        return True
    pattern = _STAGE_EVIDENCE_PATTERNS.get(stage)
    return bool(pattern and pattern.search(evidence.quote))


def _all_lead_text(lead: NormalizedLead) -> str:
    return " ".join(
        part
        for part in (
            _stringify(lead.source_values),
            lead.project or "",
            lead.location or "",
            lead.state or "",
            lead.signal or "",
            lead.intelligence or "",
            lead.timing or "",
            lead.next_step or "",
            lead.awarded_to or "",
            lead.priority_reasons or "",
            lead.summary or "",
            " ".join(lead.tags),
        )
        if part
    )


def _catalog_text() -> str:
    values: list[str] = []
    for family in ACCOYA_CATALOG.families:
        values.extend((family.display_name, *family.aliases))
        for application in family.applications:
            values.extend((application.display_name, *application.aliases))
    return " ".join(values)


def _mentioned_family_ids(text: str) -> set[str]:
    present: set[str] = set()
    for family in ACCOYA_CATALOG.families:
        terms = (family.display_name, *family.aliases)
        if any(
            term.casefold() != "accoya" and _contains_phrase(text, term)
            for term in terms
            if term.strip()
        ):
            present.add(family.id)
    return present


def _trigger_supports_selection(
    trigger: str,
    lead: NormalizedLead,
    selection: ProductSelection,
) -> bool:
    if not _specific_evidence_quote(trigger):
        return False
    family = get_family(selection.selected_product_family)
    application = get_application(
        selection.selected_application,
        family_id=family.id if family else None,
    )
    if family is None or application is None:
        return False
    terms = [
        family.display_name,
        *(term for term in family.aliases if term.casefold() != "accoya"),
        application.display_name,
        *application.aliases,
        *application.routing_terms,
        selection.material_signal or "",
        selection.named_competitor or "",
    ]
    source_field = (
        selection.exact_source_trigger.source_field
        if selection.exact_source_trigger is not None
        else None
    )
    for evidence in (*lead.material_mentions, *lead.competitor_mentions):
        if (
            not source_field
            or not evidence.source_field
            or _key_token(source_field) == _key_token(evidence.source_field)
        ):
            terms.append(evidence.quote)
    return any(
        routing_term_supported(term, trigger)
        for term in terms
        if term.strip()
    )


def _mentioned_application_ids(
    text: str,
    *,
    selected_application: str,
    lead: NormalizedLead,
    evidence: Iterable[EvidenceReference],
) -> set[str]:
    """Find catalog applications after removing quoted project-source phrases."""

    scan_text = text
    for value in (lead.project, lead.location):
        if value:
            scan_text = _remove_all(scan_text, value)
    for item in evidence:
        try:
            parsed = (
                item
                if isinstance(item, EvidenceReference)
                else EvidenceReference.model_validate(item)
            )
        except Exception:
            continue
        if _enum_value(parsed.source_type) == "lead":
            scan_text = _remove_all(scan_text, parsed.quote)

    owners_by_term: dict[str, set[str]] = {}
    for family in ACCOYA_CATALOG.families:
        for application in family.applications:
            for term in (application.display_name, *application.aliases):
                normalized = term.strip().casefold()
                if normalized:
                    owners_by_term.setdefault(normalized, set()).add(application.id)

    candidates: list[tuple[int, int, set[str]]] = []
    for term, owners in owners_by_term.items():
        for match in re.finditer(
            rf"(?<!\w){re.escape(term)}(?!\w)", scan_text, re.IGNORECASE
        ):
            if (
                term in _AMBIGUOUS_APPLICATION_TERMS
                and selected_application not in owners
                and not _ambiguous_application_is_literal(term, scan_text, match)
            ):
                continue
            candidates.append((match.start(), match.end(), owners))

    occupied: list[tuple[int, int]] = []
    present: set[str] = set()
    for start, end, owners in sorted(
        candidates, key=lambda item: (-(item[1] - item[0]), item[0])
    ):
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        if selected_application in owners:
            present.add(selected_application)
        else:
            present.update(owners)
    return present


def _ambiguous_application_is_literal(
    term: str,
    text: str,
    match: re.Match[str],
) -> bool:
    context = text[max(0, match.start() - 48) : match.end() + 48]
    return routing_term_supported(term, context)


def _specific_evidence_quote(value: str) -> bool:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    if not words:
        return False
    if len(words) > 1:
        return len(value.strip()) >= 5
    return len(words[0]) >= 4 and words[0] not in {
        "material",
        "planning",
        "project",
        "review",
    }


def _is_sentence_initial_gerund(match: re.Match[str], text: str) -> bool:
    """Do not mistake ordinary sentence-opening gerunds for named entities."""

    token = match.group(0)
    if token.isupper() or not token.casefold().endswith("ing"):
        return False
    prefix = text[: match.start()].rstrip()
    return not prefix or prefix[-1] in ".!?\n"


def _project_detail_is_negated(text: str, match: re.Match[str]) -> bool:
    sentence_start = max(
        text.rfind(".", 0, match.start()),
        text.rfind("!", 0, match.start()),
        text.rfind("?", 0, match.start()),
        text.rfind("\n", 0, match.start()),
    )
    before = text[sentence_start + 1 : match.start()].casefold()
    after = text[match.end() : match.end() + 24].casefold()
    if re.search(
        r"\b(?:without|not)\s+(?:assuming|asserting|making|adding|"
        r"inventing|stating|claiming)\b",
        before,
    ):
        return True
    return bool(
        re.search(r"\bno\s*$", before)
        and re.match(r"\s+assumptions?\b", after)
    )


def _contains_phrase(text: str, phrase: str) -> bool:
    cleaned = phrase.strip()
    if not cleaned:
        return False
    return bool(
        re.search(
            rf"(?<!\w){re.escape(cleaned)}(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


def _remove_all(text: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return text
    return re.sub(re.escape(cleaned), " ", text, flags=re.IGNORECASE)


def _remove_once(text: str, value: str) -> str:
    if not value:
        return text
    index = text.casefold().find(value.casefold())
    if index < 0:
        return text
    return text[:index] + text[index + len(value) :]


def _matches_metadata(actual: str, expected: str | None) -> bool:
    if not actual or not expected:
        return False
    normalized_actual = re.sub(r"[^a-z0-9]+", "", actual.casefold())
    normalized_expected = re.sub(r"[^a-z0-9]+", "", expected.casefold())
    return normalized_actual == normalized_expected


def _matches_geography(actual: str, expected: str | None) -> bool:
    actual_state = normalize_state_code(actual)
    expected_state = normalize_state_code(expected)
    if actual_state and expected_state:
        return actual_state == expected_state
    return _matches_metadata(actual, expected)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _key_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_stringify(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    return str(value)


def _v(code: str, message: str, field: str, offending_value: str) -> ValidationViolation:
    try:
        typed_code: ValidationCode | str = ValidationCode(code)
    except ValueError:
        typed_code = code
    return ValidationViolation(
        code=typed_code,
        message=message,
        field=field,
        offending_value=offending_value,
    )


def _deduplicate(values: list[ValidationViolation]) -> list[ValidationViolation]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[ValidationViolation] = []
    for value in values:
        marker = (_enum_value(value.code), value.message, value.offending_value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


__all__ = ["validate_email"]
