"""Deterministic claim, format, catalog, CTA, and evidence validation tests."""

from __future__ import annotations

import unittest

from agent.models import (
    BenefitClaim,
    CTAType,
    EmailDraft,
    EvidenceReference,
    ProductSelection,
    StrategyChunk,
)
from agent.normalization import normalize_lead
from agent.policy import PROHIBITED_PHRASES, cta_text
from agent.validation import validate_email


class EmailValidationTests(unittest.TestCase):
    def setUp(self):
        self.lead = normalize_lead(
            {
                "id": "validation-1",
                "Project": "Riverside Walkway",
                "Location": "Sacramento, CA",
                "Signal": "Thermory walkway is under planning review.",
                "Timing": "Early planning",
                "Contacts": "Taylor Smith - Project Architect, taylor@example.com",
            }
        )
        self.trigger = EvidenceReference(
            source_type="lead",
            source_id=self.lead.lead_id,
            source_field="signal",
            quote="Thermory walkway",
        )
        self.project_evidence = EvidenceReference(
            source_type="lead",
            source_id=self.lead.lead_id,
            source_field="project",
            quote="Riverside Walkway",
        )
        self.stage_evidence = EvidenceReference(
            source_type="lead",
            source_id=self.lead.lead_id,
            source_field="signal",
            quote="planning review",
        )
        self.selection = ProductSelection(
            audience=self.lead.audience,
            project_name=self.lead.project,
            project_stage=self.lead.project_stage,
            project_application="Standard decking",
            material_signal="Thermory",
            named_competitor="Thermory",
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            selection_reason="Exact decking signal",
            exact_source_trigger=self.trigger,
            cta_type=CTAType.SAMPLE,
            benefit_topics=[],
            retrieval_query="approved Accoya strategy positioning",
            confidence=0.9,
        )
        self.sample_cta = cta_text(self.lead.project_stage, CTAType.SAMPLE)
        self.assertIsNotNone(self.sample_cta)
        self.body = (
            "Thermory walkway appears in the planning review for Riverside Walkway, creating "
            "a focused opportunity to evaluate Accoya Wood for standard decking. "
            "This draft stays with that single catalog application and does not "
            "assume a final decision. It remains centered on the supplied signal "
            "without adding unrelated project details, contacts, or technical claims.\n\n"
            "This note keeps the discussion centered on the supplied project signal "
            "and a practical next step for the planning stage. The purpose is simply "
            "to support evaluation of the relevant decking option while the project "
            f"team considers its needs. {self.sample_cta}"
        )
        self.draft = EmailDraft(
            subject="Accoya Wood for the Riverside walkway",
            body=self.body,
            selected_product_family="accoya_wood",
            selected_application="standard_decking",
            lead_evidence_used=[
                self.trigger,
                self.project_evidence,
                self.stage_evidence,
            ],
            strategy_source_ids=[],
            benefits=[],
            cta_type=CTAType.SAMPLE,
            cta_text=self.sample_cta,
            competitor_mentions=["Thermory"],
        )

    def validate(self, draft: EmailDraft, chunks=()) -> set[str]:
        return {
            str(getattr(item.code, "value", item.code))
            for item in validate_email(
                draft,
                lead=self.lead,
                selection=self.selection,
                strategy_chunks=chunks,
            )
        }

    def test_grounded_claim_light_draft_is_valid(self):
        self.assertEqual(self.validate(self.draft), set())

    def test_every_prohibited_phrase_is_detected_case_insensitively(self):
        for phrase in PROHIBITED_PHRASES:
            for rendered in (phrase, phrase.swapcase()):
                with self.subTest(phrase=phrase, rendered=rendered):
                    draft = self.draft.model_copy(
                        update={"body": self.body.replace("focused opportunity", rendered)}
                    )
                    self.assertIn("prohibited_phrase", self.validate(draft))

    def test_environmental_superlative_and_competitor_superiority_are_prohibited(self):
        for claim in (
            "the greenest option",
            "the most eco-friendly choice",
            "better than Thermory",
            "far more durable than Thermory",
            "outlasts Thermory",
            "better than Arbor Wood",
            "better than composite decking",
            "never warps",
            "cannot fail",
            "requires no maintenance",
            "ADA-compliant",
            "free of splinters",
            "unmatched sustainability",
            "virtually maintenance free",
            "zero upkeep",
            "without splinters",
            "warp-proof",
            "weatherproof",
        ):
            with self.subTest(claim=claim):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("focused opportunity", claim)}
                )
                self.assertIn("prohibited_phrase", self.validate(draft))

    def test_subject_body_and_paragraph_formatting(self):
        long_subject = self.draft.model_copy(update={"subject": "S" * 61})
        self.assertIn("subject_too_long", self.validate(long_subject))

        blank_subject = self.draft.model_copy(update={"subject": ""})
        self.assertIn("subject_too_long", self.validate(blank_subject))

        short_body = self.draft.model_copy(update={"body": "Thermory walkway. " + self.sample_cta})
        codes = self.validate(short_body)
        self.assertIn("body_word_count", codes)
        self.assertIn("paragraph_count", codes)

    def test_catalog_selection_and_single_product_are_enforced(self):
        unknown_family = self.draft.model_copy(
            update={"selected_product_family": "invented_product"}
        )
        self.assertIn("unknown_product", self.validate(unknown_family))

        unknown_application = self.draft.model_copy(
            update={"selected_application": "invented_application"}
        )
        self.assertIn("unknown_application", self.validate(unknown_application))

        wrong_family = self.draft.model_copy(
            update={"selected_product_family": "tricoya_panels"}
        )
        self.assertIn("application_family_mismatch", self.validate(wrong_family))

        two_products = self.draft.model_copy(
            update={"body": self.body.replace("technical claims", "Tricoya Panels")}
        )
        self.assertIn("product_count", self.validate(two_products))

        for alternative in (
            "Color Grey is another option",
            "siding is another application",
        ):
            with self.subTest(alternative=alternative):
                draft = self.draft.model_copy(
                    update={
                        "body": self.body.replace("technical claims", alternative)
                    }
                )
                self.assertIn("product_count", self.validate(draft))

    def test_exactly_one_stage_valid_fixed_cta_is_required(self):
        duplicate = self.draft.model_copy(
            update={"body": self.body + " " + self.sample_cta}
        )
        self.assertIn("cta_count", self.validate(duplicate))

        second_offer = self.draft.model_copy(
            update={"body": self.body + " I can also send technical information."}
        )
        self.assertIn("cta_count", self.validate(second_offer))

        for extra_cta in (
            "I am happy to send technical information.",
            "I can get a second sample to you.",
            "Please reply if interested.",
            "Email me to discuss.",
            "Feel free to call.",
            "Send me your thoughts.",
            "I invite you to reply.",
            "Happy to share more.",
            "Let's connect.",
            "Drop me a line.",
            "Get in touch.",
            "Reach me directly.",
            "You can reply anytime.",
            "We welcome your reply.",
            "Book a call.",
            "Reply anytime.",
            "A brochure is available if useful.",
        ):
            with self.subTest(extra_cta=extra_cta):
                draft = self.draft.model_copy(
                    update={"body": self.body + " " + extra_cta}
                )
                self.assertIn("cta_count", self.validate(draft))

        procurement_cta = cta_text("procurement", CTAType.AVAILABILITY_DISCUSSION)
        wrong_stage = self.draft.model_copy(
            update={
                "body": self.body.replace(self.sample_cta, procurement_cta),
                "cta_type": CTAType.AVAILABILITY_DISCUSSION,
                "cta_text": procurement_cta,
            }
        )
        self.assertIn("cta_stage_mismatch", self.validate(wrong_stage))

        improvised_text = "Would a brochure be useful?"
        improvised = self.draft.model_copy(
            update={
                "body": self.body.replace(self.sample_cta, improvised_text),
                "cta_text": improvised_text,
            }
        )
        self.assertIn("cta_stage_mismatch", self.validate(improvised))

    def test_opening_and_evidence_ledger_must_quote_the_lead(self):
        missing_opening = self.draft.model_copy(
            update={"body": self.body.replace("Thermory walkway", "The project", 1)}
        )
        self.assertIn("opening_ungrounded", self.validate(missing_opening))

        invented = EvidenceReference(
            source_type="lead",
            source_id=self.lead.lead_id,
            source_field="signal",
            quote="invented specification",
        )
        invalid_ledger = self.draft.model_copy(
            update={"lead_evidence_used": [invented]}
        )
        self.assertIn("evidence_invalid", self.validate(invalid_ledger))

    def test_recipient_competitor_and_numeric_facts_must_be_grounded(self):
        recipient = self.draft.model_copy(
            update={"body": "Hello Unknown Person, " + self.body}
        )
        self.assertIn("recipient_ungrounded", self.validate(recipient))

        competitor = self.draft.model_copy(update={"competitor_mentions": ["Trex"]})
        self.assertIn("competitor_ungrounded", self.validate(competitor))

        numeric = self.draft.model_copy(
            update={"body": self.body.replace("focused opportunity", "42-unit opportunity")}
        )
        self.assertIn("project_fact_ungrounded", self.validate(numeric))

    def test_strategy_claims_require_exact_approved_evidence(self):
        claim = "high dimensional stability"
        no_evidence = BenefitClaim(topic="stability", claim=claim, evidence=[])
        draft = self.draft.model_copy(
            update={
                "body": self.body.replace("technical claims", claim),
                "benefits": [no_evidence],
            }
        )
        self.assertIn("evidence_invalid", self.validate(draft))

        evidence = EvidenceReference(
            source_type="strategy",
            source_id="strategy-1",
            quote="high dimensional stability",
        )
        approved = StrategyChunk(
            document_id="strategy-1",
            text="Approved positioning notes high dimensional stability.",
            metadata={"status": "approved"},
        )
        grounded = draft.model_copy(
            update={
                "strategy_source_ids": ["strategy-1"],
                "benefits": [
                    BenefitClaim(topic="stability", claim=claim, evidence=[evidence])
                ],
            }
        )
        self.assertNotIn("evidence_invalid", self.validate(grounded, [approved]))

        unapproved = approved.model_copy(update={"metadata": {"status": "draft"}})
        self.assertIn(
            "strategy_source_unapproved", self.validate(grounded, [unapproved])
        )

    def test_warranty_requires_matching_approved_product_application_and_geography(self):
        warranty_text = "subject to the applicable warranty terms"
        draft = self.draft.model_copy(
            update={"body": self.body.replace("technical claims", warranty_text)}
        )
        self.assertIn("warranty_unsupported", self.validate(draft))

        supporting = StrategyChunk(
            document_id="warranty-1",
            text="Warranty details are subject to the applicable warranty terms.",
            metadata={
                "status": "approved",
                "product_family": "accoya_wood",
                "application": "standard_decking",
                "region": "CA",
            },
        )
        warranty_benefit = BenefitClaim(
            topic="warranty terms",
            claim=warranty_text,
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id="warranty-1",
                    quote="Warranty details are subject to the applicable warranty terms.",
                )
            ],
        )
        supported = draft.model_copy(
            update={
                "strategy_source_ids": ["warranty-1"],
                "benefits": [warranty_benefit],
            }
        )
        self.assertNotIn(
            "warranty_unsupported", self.validate(supported, [supporting])
        )

        wrong_region = supporting.model_copy(
            update={"metadata": {**supporting.metadata, "region": "WA"}}
        )
        self.assertIn(
            "warranty_unsupported", self.validate(supported, [wrong_region])
        )

        two_warranties = supported.model_copy(
            update={
                "body": supported.body.replace(
                    warranty_text,
                    warranty_text + " and an unlimited lifetime warranty",
                )
            }
        )
        self.assertIn(
            "warranty_unsupported", self.validate(two_warranties, [supporting])
        )

        guarantee = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "technical claims", "a lifetime guarantee"
                )
            }
        )
        self.assertIn("warranty_unsupported", self.validate(guarantee))

        numbered_claim = "25-year warranty"
        numbered_quote = "Approved terms include a 25-year warranty."
        numbered_chunk = StrategyChunk(
            document_id="warranty-25",
            text=numbered_quote,
            metadata={
                "status": "approved",
                "product_family": "accoya_wood",
                "application": "standard_decking",
                "region": "CA",
            },
        )
        numbered = self.draft.model_copy(
            update={
                "body": self.body.replace("technical claims", numbered_claim),
                "strategy_source_ids": [numbered_chunk.document_id],
                "benefits": [
                    BenefitClaim(
                        topic="warranty",
                        claim=numbered_claim,
                        evidence=[
                            EvidenceReference(
                                source_type="strategy",
                                source_id=numbered_chunk.document_id,
                                quote=numbered_quote,
                            )
                        ],
                    )
                ],
            }
        )
        numbered_codes = self.validate(numbered, [numbered_chunk])
        self.assertNotIn("project_fact_ungrounded", numbered_codes)
        self.assertNotIn("warranty_unsupported", numbered_codes)

    def test_planning_finality_manufactured_system_and_benefit_limit(self):
        final = self.draft.model_copy(
            update={"body": self.body.replace("does not assume a final decision", "material is final")}
        )
        self.assertIn("planning_finality", self.validate(final))

        manufactured = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "focused opportunity", "Accoya manufactured the finished deck"
                )
            }
        )
        self.assertIn("manufactured_system_claim", self.validate(manufactured))

        for wording in (
            "Accoya has been selected",
            "team selected Accoya Wood",
            "the team chose Accoya Wood",
        ):
            with self.subTest(wording=wording):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("does not assume a final decision", wording)}
                )
                self.assertIn("planning_finality", self.validate(draft))

        for wording in (
            "Accoya produced the finished deck",
            "Accoya produces the finished deck",
            "the finished deck was manufactured by Accoya",
        ):
            with self.subTest(wording=wording):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("focused opportunity", wording)}
                )
                self.assertIn("manufactured_system_claim", self.validate(draft))

        benefit = BenefitClaim(topic="topic", claim="claim", evidence=[])
        too_many = EmailDraft.model_validate(
            {**self.draft.model_dump(), "benefits": [benefit] * 4}
        )
        self.assertIn("too_many_benefits", self.validate(too_many))

    def test_undeclared_technical_claim_is_rejected_in_catalog_fallback(self):
        for claim in (
            "high dimensional stability",
            "exceptional fire resistance",
            "excellent thermal performance",
            "weather resistant",
            "resists moisture",
            "dimensionally stable",
            "minimal upkeep",
            "designed to last for decades",
            "lower carbon footprint",
            "sustainably sourced",
            "a renewable low-carbon material",
            "durable outdoors",
            "exceptionally durable",
            "long-lasting",
            "built for harsh conditions",
            "resists decay",
            "water resistant",
            "hard-wearing",
            "high performance",
            "less prone to movement",
            "reduces swelling",
            "responsibly sourced",
            "carbon efficient",
            "termite resistance",
            "high load-bearing strength",
            "exceptional exterior durability",
        ):
            with self.subTest(claim=claim):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("technical claims", claim)}
                )
                self.assertIn("evidence_invalid", self.validate(draft))

    def test_competitor_must_be_present_in_lead_and_declared_ledger(self):
        draft = self.draft.model_copy(
            update={"body": self.body.replace("focused opportunity", "Trex")}
        )
        self.assertIn("competitor_ungrounded", self.validate(draft))

    def test_lead_evidence_source_id_must_match_current_lead(self):
        wrong = self.trigger.model_copy(update={"source_id": "different-lead"})
        draft = self.draft.model_copy(update={"lead_evidence_used": [wrong]})
        self.assertIn("evidence_invalid", self.validate(draft))

    def test_vacuous_single_word_trigger_is_not_valid_evidence(self):
        for quote in ("a", "the", "planning"):
            with self.subTest(quote=quote):
                trigger = EvidenceReference(
                    source_type="lead",
                    source_id=self.lead.lead_id,
                    source_field="signal",
                    quote=quote,
                )
                selection = self.selection.model_copy(
                    update={"exact_source_trigger": trigger}
                )
                draft = self.draft.model_copy(
                    update={
                        "body": quote + " " + self.body,
                        "lead_evidence_used": [
                            trigger,
                            self.project_evidence,
                            self.stage_evidence,
                        ],
                    }
                )
                codes = {
                    str(getattr(item.code, "value", item.code))
                    for item in validate_email(
                        draft,
                        lead=self.lead,
                        selection=selection,
                        strategy_chunks=[],
                    )
                }
                self.assertIn("evidence_invalid", codes)

    def test_copy_must_name_product_application_and_analyzed_cta(self):
        no_product = self.draft.model_copy(
            update={
                "subject": "A material option for the Riverside walkway",
                "body": self.body.replace("Accoya Wood", "a material").replace(
                    "standard decking", "the exterior application"
                ),
            }
        )
        self.assertIn("product_count", self.validate(no_product))

        other_cta = cta_text("planning", CTAType.SPECIFICATION_DISCUSSION)
        wrong_cta = self.draft.model_copy(
            update={
                "body": self.body.replace(self.sample_cta, other_cta),
                "cta_type": CTAType.SPECIFICATION_DISCUSSION,
                "cta_text": other_cta,
            }
        )
        self.assertIn("selection_mismatch", self.validate(wrong_cta))

    def test_invented_named_project_entity_is_rejected(self):
        draft = self.draft.model_copy(
            update={"subject": "Imaginary Palace decking review"}
        )
        self.assertIn("project_fact_ungrounded", self.validate(draft))

        for invented_fact in (
            "Gensler approved the budget",
            "Taylor Wood approved the budget",
            "the architect approved the budget and deadline",
            "an imaginary plaza",
            "a new hospital",
            "the downtown library",
            "architect prefers Accoya",
            "owner wants Accoya",
            "team plans to use Accoya",
            "client requested Accoya",
            "contractor intends to specify Accoya",
            "developer expects Accoya",
            "owner favors Accoya",
            "construction begins next spring",
            "funding is fully secured",
            "materials are ready to order",
            "Taylor is the project owner",
            "a new university",
            "a park pavilion",
            "the marina",
        ):
            with self.subTest(invented_fact=invented_fact):
                invented = self.draft.model_copy(
                    update={
                        "body": self.body.replace(
                            "focused opportunity", invented_fact
                        )
                    }
                )
                self.assertIn(
                    "project_fact_ungrounded", self.validate(invented)
                )

    def test_greeting_requires_contact_evidence_and_allows_first_name_rendering(self):
        greeted = self.draft.model_copy(update={"body": "Hi Taylor,\n" + self.body})
        self.assertIn("evidence_invalid", self.validate(greeted))

        contact_evidence = EvidenceReference(
            source_type="lead",
            source_id=self.lead.lead_id,
            source_field="contacts",
            quote="Taylor",
        )
        grounded = greeted.model_copy(
            update={
                "lead_evidence_used": [
                    *self.draft.lead_evidence_used,
                    contact_evidence,
                ]
            }
        )
        self.assertEqual(self.validate(grounded), set())

    def test_stage_fact_requires_a_stage_specific_ledger_entry(self):
        without_stage_evidence = self.draft.model_copy(
            update={
                "lead_evidence_used": [self.trigger, self.project_evidence]
            }
        )
        self.assertIn("evidence_invalid", self.validate(without_stage_evidence))

    def test_lowercase_competitor_requires_exact_lead_evidence(self):
        lead = normalize_lead(
            {
                "id": "validation-1",
                "Project": "Riverside Walkway",
                "Location": "Sacramento, CA",
                "Signal": (
                    "Trex walkway and composite decking option are under "
                    "planning review."
                ),
                "Timing": "Early planning",
            }
        )
        trex = EvidenceReference(
            source_type="lead",
            source_id=lead.lead_id,
            source_field="signal",
            quote="Trex",
        )
        stage = EvidenceReference(
            source_type="lead",
            source_id=lead.lead_id,
            source_field="signal",
            quote="planning review",
        )
        selection = self.selection.model_copy(
            update={
                "material_signal": "Trex",
                "named_competitor": "Trex",
                "exact_source_trigger": trex,
            }
        )
        body = self.body.replace("Thermory", "Trex").replace(
            "technical claims", "composite decking context"
        )
        draft = self.draft.model_copy(
            update={
                "body": body,
                "lead_evidence_used": [trex, self.project_evidence, stage],
                "competitor_mentions": ["Trex", "composite decking"],
            }
        )
        codes = {
            str(getattr(item.code, "value", item.code))
            for item in validate_email(
                draft,
                lead=lead,
                selection=selection,
                strategy_chunks=[],
            )
        }
        self.assertIn("evidence_invalid", codes)

    def test_safe_sentence_starters_application_idioms_and_negations_pass(self):
        starters = (
            "With",
            "Because",
            "From",
            "Looking",
            "Happy",
            "Should",
            "Perhaps",
            "Instead",
            "Also",
            "Alternatively",
            "Specifically",
            "Ultimately",
            "Importantly",
            "Exploring",
            "Evaluating",
            "Maintaining",
            "Addressing",
            "Understanding",
            "Aligning",
        )
        for starter in starters:
            with self.subTest(starter=starter):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("This note", starter)}
                )
                self.assertNotIn("project_fact_ungrounded", self.validate(draft))

        safe_phrases = (
            "project structure",
            "decision window",
            "quality signs",
            "project gate",
            "can bridge the discussion",
            "without assuming a budget or schedule",
            "no deadline assumption",
            "not making climate assumptions",
            "without asserting procurement status",
            "We decided to keep the note focused",
            "We chose to keep the note focused",
        )
        for phrase in safe_phrases:
            with self.subTest(phrase=phrase):
                draft = self.draft.model_copy(
                    update={"body": self.body.replace("technical claims", phrase)}
                )
                self.assertEqual(self.validate(draft), set())

    def test_strategy_evidence_cannot_create_project_facts(self):
        quote = (
            "A Seattle case study notes a 500-unit project with attractive appearance."
        )
        chunk = StrategyChunk(
            document_id="case-study-1",
            text=quote,
            metadata={"status": "approved"},
        )
        benefit = BenefitClaim(
            topic="appearance",
            claim="attractive appearance",
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id=chunk.document_id,
                    quote=quote,
                )
            ],
        )
        draft = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "technical claims",
                    "the Seattle site is a 500-unit project with attractive appearance",
                ),
                "strategy_source_ids": [chunk.document_id],
                "benefits": [benefit],
            }
        )
        self.assertIn(
            "project_fact_ungrounded", self.validate(draft, [chunk])
        )

        smuggled_claim = "60-year service life for this 500-unit project"
        smuggled_quote = "Approved notes state " + smuggled_claim + "."
        smuggled_chunk = StrategyChunk(
            document_id="smuggled-number",
            text=smuggled_quote,
            metadata={"status": "approved"},
        )
        smuggled = self.draft.model_copy(
            update={
                "body": self.body.replace("technical claims", smuggled_claim),
                "strategy_source_ids": [smuggled_chunk.document_id],
                "benefits": [
                    BenefitClaim(
                        topic="service life",
                        claim=smuggled_claim,
                        evidence=[
                            EvidenceReference(
                                source_type="strategy",
                                source_id=smuggled_chunk.document_id,
                                quote=smuggled_quote,
                            )
                        ],
                    )
                ],
            }
        )
        self.assertIn(
            "project_fact_ungrounded",
            self.validate(smuggled, [smuggled_chunk]),
        )

    def test_strategy_quote_must_support_its_declared_benefit(self):
        chunk = StrategyChunk(
            document_id="unrelated-1",
            text="Beautiful appearance options are available.",
            metadata={"status": "approved"},
        )
        benefit = BenefitClaim(
            topic="fire",
            claim="exceptional fire resistance",
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id=chunk.document_id,
                    quote=chunk.text,
                )
            ],
        )
        draft = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "technical claims", benefit.claim
                ),
                "strategy_source_ids": [chunk.document_id],
                "benefits": [benefit],
            }
        )
        self.assertIn("evidence_invalid", self.validate(draft, [chunk]))

    def test_unused_strategy_source_is_not_returnable_provenance(self):
        chunk = StrategyChunk(
            document_id="unused",
            text="Approved but unused positioning.",
            metadata={"status": "approved"},
        )
        draft = self.draft.model_copy(
            update={"strategy_source_ids": [chunk.document_id]}
        )
        self.assertIn("evidence_invalid", self.validate(draft, [chunk]))

    def test_exact_lead_sustainability_goal_is_not_a_product_claim(self):
        lead = normalize_lead(
            {
                "id": "validation-1",
                "Project": "Riverside Walkway",
                "Location": "Sacramento, CA",
                "Signal": (
                    "Thermory walkway is under planning review with a "
                    "sustainable design goal."
                ),
                "Timing": "Early planning",
                "Contacts": (
                    "Taylor Smith - Project Architect, taylor@example.com"
                ),
            }
        )
        goal = EvidenceReference(
            source_type="lead",
            source_id=lead.lead_id,
            source_field="signal",
            quote="sustainable design goal",
        )
        draft = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "technical claims", "sustainable design goal"
                ),
                "lead_evidence_used": [
                    self.trigger,
                    self.project_evidence,
                    self.stage_evidence,
                    goal,
                ],
            }
        )
        codes = {
            str(getattr(item.code, "value", item.code))
            for item in validate_email(
                draft,
                lead=lead,
                selection=self.selection,
                strategy_chunks=[],
            )
        }
        self.assertNotIn("evidence_invalid", codes)

    def test_approved_numeric_strategy_claim_is_not_treated_as_project_fact(self):
        claim = "60-year service life"
        quote = "Approved positioning supports a 60-year service life."
        chunk = StrategyChunk(
            document_id="service-life-1",
            text=quote,
            metadata={"status": "approved"},
        )
        benefit = BenefitClaim(
            topic="service life",
            claim=claim,
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id=chunk.document_id,
                    quote=quote,
                )
            ],
        )
        draft = self.draft.model_copy(
            update={
                "body": self.body.replace("technical claims", claim),
                "strategy_source_ids": [chunk.document_id],
                "benefits": [benefit],
            }
        )
        codes = self.validate(draft, [chunk])
        self.assertNotIn("project_fact_ungrounded", codes)
        self.assertNotIn("evidence_invalid", codes)

    def test_benefit_source_must_be_declared_and_warranty_quote_must_match(self):
        technical_quote = "Approved high dimensional stability positioning."
        chunk = StrategyChunk(
            document_id="strategy-undeclared",
            text=technical_quote,
            metadata={"status": "approved"},
        )
        technical = BenefitClaim(
            topic="stability",
            claim="high dimensional stability",
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id=chunk.document_id,
                    quote=technical_quote,
                )
            ],
        )
        undeclared = self.draft.model_copy(
            update={
                "body": self.body.replace(
                    "technical claims", "high dimensional stability"
                ),
                "benefits": [technical],
                "strategy_source_ids": [],
            }
        )
        self.assertIn("evidence_invalid", self.validate(undeclared, [chunk]))

        warranty_claim = "unlimited lifetime warranty"
        warranty_chunk = StrategyChunk(
            document_id="warranty-vague",
            text="General warranty details are available.",
            metadata={
                "status": "approved",
                "product_family": "accoya_wood",
                "application": "standard_decking",
                "region": "CA",
            },
        )
        vague_warranty = BenefitClaim(
            topic="warranty",
            claim=warranty_claim,
            evidence=[
                EvidenceReference(
                    source_type="strategy",
                    source_id=warranty_chunk.document_id,
                    quote=warranty_chunk.text,
                )
            ],
        )
        warranty_draft = self.draft.model_copy(
            update={
                "body": self.body.replace("technical claims", warranty_claim),
                "benefits": [vague_warranty],
                "strategy_source_ids": [warranty_chunk.document_id],
            }
        )
        self.assertIn(
            "warranty_unsupported", self.validate(warranty_draft, [warranty_chunk])
        )


if __name__ == "__main__":
    unittest.main()
