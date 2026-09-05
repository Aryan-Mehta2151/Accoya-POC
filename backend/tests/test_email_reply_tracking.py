"""Offline tests for Microsoft Graph reply synchronization and correlation."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import httpx

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.database import Base
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailReply,
    EmailReplyClassification,
    EmailStatus,
    GraphMailNotification,
    GraphMailboxSyncState,
    GraphMailboxSyncStatus,
    Lead,
    LeadReviewStatus,
)
from app.services import email_reply_service


class EmailReplyTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.now = datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc)
        self.settings = Settings(
            microsoft_client_id="client-id",
            microsoft_tenant_id="tenant-id",
            microsoft_client_secret="offline-secret",
            microsoft_sender_email="sender@example.com",
            microsoft_graph_timeout_seconds=10,
            email_reply_tracking_enabled=True,
            microsoft_graph_notification_url=(
                "https://api.example.com/api/microsoft-graph/mail-notifications"
            ),
            microsoft_graph_client_state="s" * 40,
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _seed_delivery(
        self,
        *,
        external_id: str = "reply-lead",
        conversation_id: str | None = None,
        internet_message_id: str | None = None,
    ) -> tuple[str, str, str]:
        with self.sessions() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Reply project",
                contact_email="client@example.com",
                raw_data={},
                review_status=LeadReviewStatus.active,
            )
            db.add(lead)
            db.flush()
            run = AgentRun(
                lead_id=lead.id,
                status=AgentRunStatus.generated,
                input_hash="0" * 64,
                warnings=[],
                original_subject="A technical question",
                original_body="Hello",
                prompt_version="test",
                catalog_version="test",
                model_name="offline",
                model_calls=0,
                retrieval_count=0,
                started_at=self.now,
                completed_at=self.now,
            )
            db.add(run)
            db.flush()
            email = Email(
                agent_run_id=run.id,
                recipient_email="client@example.com",
                subject="A technical question",
                body="Hello",
                status=EmailStatus.sent,
            )
            db.add(email)
            db.flush()
            job = EmailDeliveryJob(
                email_id=email.id,
                status=EmailDeliveryJobStatus.succeeded,
                requested_by=str(uuid.uuid4()),
                idempotency_key=str(uuid.uuid4()),
                content_hash="a" * 64,
                message_id=f"<{uuid.uuid4()}@accoya-outreach.local>",
                sender_email="sender@example.com",
                recipient_email="client@example.com",
                subject="A technical question",
                body_snapshot="Hello",
                attempt_count=1,
                claimed_by="delivery-worker",
                claimed_at=self.now,
                heartbeat_at=self.now,
                send_started_at=self.now,
                accepted_at=self.now,
                completed_at=self.now,
                conversation_id=conversation_id,
                internet_message_id=internet_message_id,
            )
            db.add(job)
            db.commit()
            return str(lead.id), str(email.id), str(job.id)

    def _sent_payload(self, job: EmailDeliveryJob) -> dict[str, object]:
        return {
            "id": f"graph-sent-{job.id}",
            "internetMessageId": f"<internet-{job.id}@example.com>",
            "conversationId": f"conversation-{job.id}",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "receivedDateTime": self.now.isoformat(),
            "isRead": True,
            "internetMessageHeaders": [
                {"name": "x-accoya-message-id", "value": job.message_id},
            ],
        }

    def _reply_payload(
        self,
        *,
        graph_id: str,
        internet_message_id: str,
        conversation_id: str,
        reference_id: str | None,
        is_read: bool = False,
        extra_headers: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        headers = list(extra_headers or [])
        if reference_id:
            headers.append({"name": "In-Reply-To", "value": reference_id})
        return {
            "id": graph_id,
            "internetMessageId": internet_message_id,
            "conversationId": conversation_id,
            "from": {"emailAddress": {"address": "client@example.com"}},
            "receivedDateTime": (self.now + timedelta(minutes=5)).isoformat(),
            "isRead": is_read,
            "internetMessageHeaders": headers,
        }

    def _healthy_state(self) -> None:
        with self.sessions() as db:
            db.add(
                GraphMailboxSyncState(
                    mailbox_email="sender@example.com",
                    status=GraphMailboxSyncStatus.idle,
                    subscription_id="subscription-1",
                    subscription_expires_at=self.now + timedelta(days=5),
                    backfill_cutoff_at=self.now - timedelta(days=90),
                    initial_backfill_completed_at=self.now,
                    force_resync=False,
                    next_sync_at=self.now + timedelta(minutes=1),
                    last_succeeded_at=self.now,
                )
            )
            db.commit()

    def test_sent_identity_exact_reply_and_outlook_read_state(self) -> None:
        lead_id, _, job_id = self._seed_delivery()
        self._healthy_state()
        with self.sessions() as db:
            job = db.get(EmailDeliveryJob, job_id)
            sent = self._sent_payload(job)
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=sent,
                now=self.now,
            )
            db.commit()
            db.refresh(job)
            self.assertEqual(job.graph_message_id, sent["id"])
            with self.assertRaisesRegex(
                email_reply_service.GraphReplyError,
                "sent_message_graph_identity_conflict",
            ):
                email_reply_service.process_graph_message(
                    db,
                    mailbox_email="sender@example.com",
                    payload={**sent, "id": "different-immutable-id"},
                    now=self.now,
                )
            db.rollback()

            reply_payload = self._reply_payload(
                graph_id="graph-reply-1",
                internet_message_id="<reply-1@example.com>",
                conversation_id=str(sent["conversationId"]),
                reference_id=str(sent["internetMessageId"]),
            )
            reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=reply_payload,
                now=self.now,
            )
            db.commit()
            self.assertEqual(reply.lead_id, lead_id)
            self.assertEqual(reply.classification, EmailReplyClassification.human)
            self.assertEqual(reply.match_method.value, "references")

            summary = email_reply_service.reply_summary(
                db,
                settings=self.settings,
                now=self.now,
            )
            self.assertEqual(summary["unread_reply_count"], 1)
            self.assertEqual(summary["replied_opportunity_count"], 1)

            lead = db.get(Lead, lead_id)
            lead.review_status = LeadReviewStatus.deleted
            db.commit()
            summary = email_reply_service.reply_summary(
                db,
                settings=self.settings,
                now=self.now,
            )
            self.assertEqual(summary["unread_reply_count"], 0)
            self.assertEqual(summary["replied_opportunity_count"], 0)
            self.assertEqual(
                email_reply_service.unread_reply_summaries(db, [lead_id])[lead_id][0],
                1,
            )
            lead.review_status = LeadReviewStatus.active
            db.commit()

            reply_payload["isRead"] = True
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=reply_payload,
                now=self.now,
            )
            db.commit()
            summary = email_reply_service.reply_summary(
                db,
                settings=self.settings,
                now=self.now,
            )
            self.assertEqual(summary["unread_reply_count"], 0)
            self.assertEqual(summary["replied_opportunity_count"], 1)
            per_lead = email_reply_service.unread_reply_summaries(db, [lead_id])
            self.assertEqual(per_lead[lead_id][0], 0)
            self.assertEqual(
                email_reply_service._as_utc(per_lead[lead_id][1]),
                email_reply_service._as_utc(reply.received_at),
            )

            email_reply_service.mark_graph_message_removed(
                db,
                mailbox_email="sender@example.com",
                graph_message_id="graph-reply-1",
                now=self.now + timedelta(minutes=10),
            )
            db.commit()
            self.assertNotIn(
                lead_id,
                email_reply_service.unread_reply_summaries(db, [lead_id]),
            )

    def test_conversation_fallback_and_automatic_replies(self) -> None:
        lead_id, _, job_id = self._seed_delivery(
            conversation_id="tracked-conversation",
            internet_message_id="<sent@example.com>",
        )
        with self.sessions() as db:
            reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="conversation-reply",
                    internet_message_id="<conversation-reply@example.com>",
                    conversation_id="tracked-conversation",
                    reference_id=None,
                ),
            )
            automatic = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="automatic-reply",
                    internet_message_id="<automatic@example.com>",
                    conversation_id="tracked-conversation",
                    reference_id="<sent@example.com>",
                    extra_headers=[
                        {"name": "Auto-Submitted", "value": "auto-replied"},
                    ],
                ),
            )
            bounce = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="delivery-report",
                    internet_message_id="<delivery-report@example.com>",
                    conversation_id="tracked-conversation",
                    reference_id="<sent@example.com>",
                    extra_headers=[
                        {"name": "Content-Type", "value": "multipart/report"},
                    ],
                ),
            )
            db.commit()
            self.assertEqual(reply.lead_id, lead_id)
            self.assertEqual(reply.match_method.value, "conversation")
            self.assertEqual(automatic.delivery_job_id, job_id)
            self.assertEqual(
                automatic.classification,
                EmailReplyClassification.automatic,
            )
            self.assertEqual(bounce.classification, EmailReplyClassification.bounce)

            job = db.get(EmailDeliveryJob, job_id)
            job.conversation_id = None
            db.commit()
            later_reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="later-conversation-reply",
                    internet_message_id="<later-conversation-reply@example.com>",
                    conversation_id="tracked-conversation",
                    reference_id=None,
                ),
            )
            self.assertEqual(later_reply.lead_id, lead_id)
            self.assertEqual(later_reply.match_method.value, "conversation")

            self_sent = {
                **self._reply_payload(
                    graph_id="self-sent-message",
                    internet_message_id="<self-sent@example.com>",
                    conversation_id="tracked-conversation",
                    reference_id=None,
                ),
                "from": {"emailAddress": {"address": "sender@example.com"}},
            }
            self.assertIsNone(
                email_reply_service.process_graph_message(
                    db,
                    mailbox_email="sender@example.com",
                    payload=self_sent,
                )
            )

    def test_ambiguous_conversation_is_not_counted(self) -> None:
        self._seed_delivery(
            external_id="ambiguous-1",
            conversation_id="shared-conversation",
            internet_message_id="<first@example.com>",
        )
        self._seed_delivery(
            external_id="ambiguous-2",
            conversation_id="shared-conversation",
            internet_message_id="<second@example.com>",
        )
        with self.sessions() as db:
            reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="ambiguous-reply",
                    internet_message_id="<ambiguous@example.com>",
                    conversation_id="shared-conversation",
                    reference_id=None,
                ),
            )
            db.commit()
            self.assertIsNone(reply.lead_id)
            self.assertEqual(reply.classification, EmailReplyClassification.ambiguous)

    def test_notification_validation_coalescing_and_missed_lifecycle(self) -> None:
        self._healthy_state()
        normal = {
            "value": [
                {
                    "subscriptionId": "subscription-1",
                    "tenantId": "tenant-id",
                    "clientState": "s" * 40,
                    "changeType": "created",
                    "resourceData": {"id": "immutable-message-1"},
                }
            ]
        }
        with self.sessions() as db:
            self.assertEqual(
                email_reply_service.record_notifications(
                    db,
                    payload=normal,
                    settings=self.settings,
                    now=self.now,
                ),
                1,
            )
            later = self.now + timedelta(seconds=1)
            self.assertEqual(
                email_reply_service.record_notifications(
                    db,
                    payload=normal,
                    settings=self.settings,
                    now=later,
                ),
                1,
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(GraphMailNotification)),
                1,
            )

            invalid = {
                "value": [{**normal["value"][0], "clientState": "wrong"}]
            }
            self.assertEqual(
                email_reply_service.record_notifications(
                    db,
                    payload=invalid,
                    settings=self.settings,
                ),
                0,
            )
            for invalid_fields in (
                {"tenantId": None},
                {"tenantId": "another-tenant"},
                {"subscriptionId": "another-subscription"},
            ):
                invalid_identity = {
                    "value": [{**normal["value"][0], **invalid_fields}]
                }
                self.assertEqual(
                    email_reply_service.record_notifications(
                        db,
                        payload=invalid_identity,
                        settings=self.settings,
                    ),
                    0,
                )

            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.force_resync = False
            db.commit()
            lifecycle = {
                "value": [
                    {
                        "subscriptionId": "subscription-1",
                        "tenantId": "tenant-id",
                        "clientState": "s" * 40,
                        "lifecycleEvent": "missed",
                    }
                ]
            }
            email_reply_service.record_notifications(
                db,
                payload=lifecycle,
                settings=self.settings,
            )
            db.refresh(state)
            self.assertTrue(state.force_resync)

            state.force_resync = False
            state.subscription_expires_at = self.now + timedelta(days=5)
            db.commit()
            reauthorization_at = self.now + timedelta(seconds=2)
            lifecycle["value"][0]["lifecycleEvent"] = "reauthorizationRequired"
            email_reply_service.record_notifications(
                db,
                payload=lifecycle,
                settings=self.settings,
                now=reauthorization_at,
            )
            db.refresh(state)
            self.assertEqual(
                email_reply_service._as_utc(state.subscription_expires_at),
                reauthorization_at,
            )

            lifecycle["value"][0]["lifecycleEvent"] = "subscriptionRemoved"
            email_reply_service.record_notifications(
                db,
                payload=lifecycle,
                settings=self.settings,
                now=self.now + timedelta(seconds=3),
            )
            db.refresh(state)
            self.assertIsNone(state.subscription_id)
            self.assertIsNone(state.subscription_expires_at)
            self.assertTrue(state.force_resync)

    def test_duplicate_notifications_in_one_payload_are_durably_coalesced(self) -> None:
        self._healthy_state()
        notification = {
            "subscriptionId": "subscription-1",
            "tenantId": "tenant-id",
            "clientState": "s" * 40,
            "changeType": "updated",
            "resourceData": {"id": "duplicate-message"},
        }
        with self.sessions() as db:
            accepted = email_reply_service.record_notifications(
                db,
                payload={
                    "value": [
                        notification,
                        notification,
                        {
                            **notification,
                            "resourceData": {"id": "another-message"},
                        },
                    ]
                },
                settings=self.settings,
                now=self.now,
            )
            self.assertEqual(accepted, 3)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(GraphMailNotification)),
                2,
            )

    def test_initial_failure_is_reported_and_honors_retry_time(self) -> None:
        with self.sessions() as db:
            state = email_reply_service.ensure_mailbox_state(
                db,
                mailbox_email="sender@example.com",
                backfill_days=90,
                now=self.now,
            )
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )
        with self.sessions() as db:
            db.add(
                GraphMailNotification(
                    id=str(uuid.uuid4()),
                    mailbox_email="sender@example.com",
                    graph_message_id="claimed-message",
                    change_type="created",
                    subscription_id="subscription-1",
                    requested_at=self.now,
                    claimed_by=claim.worker_id,
                    claimed_at=self.now,
                    heartbeat_at=self.now,
                )
            )
            db.commit()
            email_reply_service.finalize_mailbox_sync(
                db,
                claim=claim,
                reconcile_seconds=60,
                error_code="microsoft_graph_authentication_failed",
                now=self.now,
            )
            notification = db.scalar(select(GraphMailNotification))
            self.assertIsNone(notification.claimed_by)
            self.assertEqual(
                email_reply_service.reply_summary(
                    db,
                    settings=self.settings,
                    now=self.now,
                )["sync_status"],
                "error",
            )
            self.assertIsNone(
                email_reply_service.claim_mailbox_sync(
                    db,
                    mailbox_email="sender@example.com",
                    worker_id="another-worker",
                    now=self.now + timedelta(seconds=1),
                )
            )

    def test_stale_notification_lease_recovers_after_mailbox_finalization(self) -> None:
        self._healthy_state()
        stale_at = self.now - timedelta(minutes=10)
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.status = GraphMailboxSyncStatus.error
            db.add(
                GraphMailNotification(
                    id=str(uuid.uuid4()),
                    mailbox_email=state.mailbox_email,
                    graph_message_id="stranded-message",
                    change_type="updated",
                    subscription_id="subscription-1",
                    requested_at=stale_at,
                    claimed_by="failed-worker",
                    claimed_at=stale_at,
                    heartbeat_at=stale_at,
                )
            )
            db.commit()
            self.assertTrue(
                email_reply_service.recover_stale_sync(
                    db,
                    mailbox_email=state.mailbox_email,
                    stale_after_seconds=300,
                    now=self.now,
                )
            )
            notification = db.scalar(select(GraphMailNotification))
            self.assertIsNone(notification.claimed_by)

    def test_auto_response_suppression_request_does_not_hide_human_reply(self) -> None:
        lead_id, _, _ = self._seed_delivery(
            conversation_id="human-conversation",
            internet_message_id="<human-sent@example.com>",
        )
        with self.sessions() as db:
            reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=self._reply_payload(
                    graph_id="human-with-suppression-request",
                    internet_message_id="<human-reply@example.com>",
                    conversation_id="human-conversation",
                    reference_id="<human-sent@example.com>",
                    extra_headers=[
                        {"name": "X-Auto-Response-Suppress", "value": "All"}
                    ],
                ),
            )
            db.commit()
            self.assertEqual(reply.lead_id, lead_id)
            self.assertEqual(reply.classification, EmailReplyClassification.human)

    def test_reply_arriving_before_sent_reconciliation_is_retried(self) -> None:
        lead_id, _, job_id = self._seed_delivery()
        reply_payload = self._reply_payload(
            graph_id="early-reply",
            internet_message_id="<early-reply@example.com>",
            conversation_id="eventual-conversation",
            reference_id="<eventual-sent@example.com>",
        )
        with self.sessions() as db:
            reply = email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=reply_payload,
            )
            db.commit()
            self.assertIsNone(reply.lead_id)

        with self.sessions() as db:
            persisted = db.scalar(
                select(EmailReply).where(EmailReply.graph_message_id == "early-reply")
            )
            self.assertEqual(
                persisted.reference_message_ids,
                ["<eventual-sent@example.com>"],
            )
            job = db.get(EmailDeliveryJob, job_id)
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload={
                    "id": "eventual-sent-id",
                    "internetMessageId": "<eventual-sent@example.com>",
                    "conversationId": "eventual-conversation",
                    "from": {"emailAddress": {"address": "sender@example.com"}},
                    "sentDateTime": self.now.isoformat(),
                    "isRead": True,
                    "internetMessageHeaders": [
                        {"name": "x-accoya-message-id", "value": job.message_id},
                    ],
                },
            )
            db.commit()
            db.refresh(persisted)
            self.assertEqual(persisted.lead_id, lead_id)
            self.assertEqual(persisted.match_method.value, "references")

    def test_repeated_messages_in_one_transaction_upsert_latest_read_state(self) -> None:
        lead_id, _, _ = self._seed_delivery(internet_message_id="<sent@example.com>")
        for copied_message in (False, True):
            with self.subTest(copied_message=copied_message), self.sessions() as db:
                graph_id = f"repeat-{copied_message}"
                payload = self._reply_payload(
                    graph_id=graph_id,
                    internet_message_id=f"<{graph_id}@example.com>",
                    conversation_id=graph_id,
                    reference_id="<sent@example.com>",
                )
                first = email_reply_service.process_graph_message(
                    db, mailbox_email="sender@example.com", payload=payload,
                )
                second = email_reply_service.process_graph_message(
                    db,
                    mailbox_email="sender@example.com",
                    payload={
                        **payload,
                        "id": f"{graph_id}-copy" if copied_message else graph_id,
                        "isRead": True,
                    },
                )
                db.commit()
                self.assertEqual(first.id, second.id)
                replies = db.scalars(select(EmailReply).where(
                    EmailReply.internet_message_id == payload["internetMessageId"],
                )).all()
                self.assertEqual(len(replies), 1)
                self.assertTrue(replies[0].is_read)
                self.assertEqual(replies[0].lead_id, lead_id)

    def test_late_sent_identity_rechecks_existing_conversation_matches(self) -> None:
        for is_read in (False, True):
            for exact_reference in (False, True):
                with self.subTest(is_read=is_read, exact_reference=exact_reference):
                    key = f"late-{is_read}-{exact_reference}"
                    first_lead, _, _ = self._seed_delivery(
                        external_id=f"{key}-first", conversation_id=key,
                    )
                    second_lead, second_email, second_job = self._seed_delivery(
                        external_id=f"{key}-second",
                    )
                    reference = f"<{key}@example.com>"
                    with self.sessions() as db:
                        reply = email_reply_service.process_graph_message(
                            db,
                            mailbox_email="sender@example.com",
                            payload=self._reply_payload(
                                graph_id=key,
                                internet_message_id=f"<reply-{key}@example.com>",
                                conversation_id=key,
                                reference_id=reference if exact_reference else None,
                                is_read=is_read,
                            ),
                        )
                        db.commit()
                        self.assertEqual(reply.lead_id, first_lead)
                        self.assertEqual(reply.match_method.value, "conversation")
                        payload = self._sent_payload(db.get(EmailDeliveryJob, second_job))
                        payload.update(internetMessageId=reference, conversationId=key)
                        email_reply_service.process_graph_message(
                            db, mailbox_email="sender@example.com", payload=payload,
                        )
                        db.commit()
                        # A later periodic pass must preserve the corrected result.
                        email_reply_service.reconcile_pending_replies(
                            db, mailbox_email="sender@example.com",
                        )
                        db.commit()
                        db.refresh(reply)
                        self.assertEqual(reply.is_read, is_read)
                        if exact_reference:
                            self.assertEqual(reply.lead_id, second_lead)
                            self.assertEqual(reply.email_id, second_email)
                            self.assertEqual(reply.delivery_job_id, second_job)
                            self.assertEqual(reply.match_method.value, "references")
                            self.assertEqual(reply.classification, EmailReplyClassification.human)
                        else:
                            self.assertIsNone(reply.lead_id)
                            self.assertIsNone(reply.email_id)
                            self.assertIsNone(reply.delivery_job_id)
                            self.assertEqual(reply.match_method.value, "none")
                            self.assertEqual(reply.classification, EmailReplyClassification.ambiguous)
                        summaries = email_reply_service.unread_reply_summaries(db, [first_lead])
                        self.assertNotIn(first_lead, summaries)

    def test_graph_client_retries_throttling_and_server_errors(self) -> None:
        sleeps: list[float] = []
        responses = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(503),
            httpx.Response(200, json={"value": []}),
        ]
        with (
            patch.object(email_reply_service.email_service, "graph_access_token", return_value="token"),
            patch.object(email_reply_service.httpx, "request", side_effect=responses) as request,
        ):
            client = email_reply_service.GraphMailClient(
                self.settings,
                sleeper=sleeps.append,
            )
            self.assertEqual(client.headers["Prefer"], 'IdType="ImmutableId"')
            self.assertEqual(client.request("GET", "/users/example/messages"), {"value": []})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleeps, [0.0, 2.0])

        with (
            patch.object(email_reply_service.email_service, "graph_access_token", return_value="token"),
            patch.object(
                email_reply_service.httpx,
                "request",
                return_value=httpx.Response(429, headers={"Retry-After": "0"}),
            ) as request,
        ):
            client = email_reply_service.GraphMailClient(
                self.settings,
                sleeper=Mock(),
            )
            with self.assertRaisesRegex(
                email_reply_service.GraphReplyError,
                "microsoft_graph_throttled",
            ):
                client.request("GET", "/users/example/messages")
        self.assertEqual(request.call_count, 3)

    def test_expired_delta_and_stale_lease_force_bounded_recovery(self) -> None:
        self._healthy_state()
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.inbox_delta_link = "https://graph.test/expired-delta"
            state.next_sync_at = self.now
            db.commit()
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )

        class ExpiredClient:
            def request(self, _method: str, _url: str):
                raise email_reply_service.GraphReplyError(
                    "microsoft_graph_delta_expired",
                    reset_delta=True,
                )

        with self.assertRaises(email_reply_service.GraphReplyError):
            email_reply_service._run_delta(
                self.sessions,
                client=ExpiredClient(),
                claim=claim,
                folder="inbox",
                state_field="inbox_delta_link",
            )
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            self.assertIsNone(state.inbox_delta_link)
            self.assertTrue(state.force_resync)
            state.heartbeat_at = self.now - timedelta(minutes=10)
            db.commit()
            self.assertTrue(
                email_reply_service.recover_stale_sync(
                    db,
                    mailbox_email=state.mailbox_email,
                    stale_after_seconds=300,
                    now=self.now,
                )
            )
            self.assertEqual(state.error_code, "reply_sync_lease_expired")

    def test_delta_removal_distinguishes_moves_from_deletions(self) -> None:
        self._seed_delivery(
            conversation_id="move-conversation",
            internet_message_id="<move-sent@example.com>",
        )
        moved_payload = self._reply_payload(
            graph_id="moved-message",
            internet_message_id="<moved@example.com>",
            conversation_id="move-conversation",
            reference_id="<move-sent@example.com>",
        )
        deleted_payload = self._reply_payload(
            graph_id="deleted-message",
            internet_message_id="<deleted@example.com>",
            conversation_id="move-conversation",
            reference_id="<move-sent@example.com>",
        )
        with self.sessions() as db:
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=moved_payload,
            )
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=deleted_payload,
            )
            db.commit()
        self._healthy_state()
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.next_sync_at = self.now
            db.commit()
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )

        class RemovalClient:
            def request(self, _method: str, _url: str):
                return {
                    "value": [
                        {"id": "moved-message", "@removed": {"reason": "changed"}},
                        {"id": "deleted-message", "@removed": {"reason": "deleted"}},
                    ],
                    "@odata.deltaLink": "https://graph.test/next-delta",
                }

            def get_message(self, _mailbox: str, message_id: str):
                if message_id == "deleted-message":
                    raise email_reply_service.GraphMessageMissing("missing")
                return {**moved_payload, "isRead": True}

        email_reply_service._run_delta(
            self.sessions,
            client=RemovalClient(),
            claim=claim,
            folder="inbox",
            state_field="inbox_delta_link",
        )
        with self.sessions() as db:
            moved = db.scalar(
                select(EmailReply).where(EmailReply.graph_message_id == "moved-message")
            )
            deleted = db.scalar(
                select(EmailReply).where(EmailReply.graph_message_id == "deleted-message")
            )
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            self.assertTrue(moved.is_read)
            self.assertIsNone(moved.removed_at)
            self.assertIsNotNone(deleted.removed_at)
            self.assertEqual(state.inbox_delta_link, "https://graph.test/next-delta")

    def test_subscription_is_renewed_with_immutable_id_preference(self) -> None:
        self._healthy_state()
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.subscription_expires_at = self.now + timedelta(hours=1)
            state.next_sync_at = self.now + timedelta(days=1)
            db.commit()
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )

        renewed_at = self.now + timedelta(days=6)

        class RenewalClient:
            def request(self, method: str, url: str, *, json=None):
                self.method = method
                self.url = url
                self.body = json
                return {
                    "id": "subscription-1",
                    "expirationDateTime": renewed_at.isoformat(),
                }

        client = RenewalClient()
        with patch.object(email_reply_service, "_utc_now", return_value=self.now):
            email_reply_service._ensure_subscription(
                self.sessions,
                client=client,
                claim=claim,
                settings=self.settings,
            )
        self.assertEqual(client.method, "PATCH")
        self.assertEqual(client.url, "/subscriptions/subscription-1")
        self.assertEqual(
            client.body["notificationUrl"],
            self.settings.microsoft_graph_notification_url,
        )
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            self.assertEqual(
                email_reply_service._as_utc(state.subscription_expires_at),
                renewed_at,
            )

    def test_synchronizer_renews_subscription_pages_and_backfills(self) -> None:
        lead_id, _, job_id = self._seed_delivery()
        with self.sessions() as db:
            state = email_reply_service.ensure_mailbox_state(
                db,
                mailbox_email="sender@example.com",
                backfill_days=90,
                now=self.now,
            )
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )
        self.assertIsNotNone(claim)
        with self.sessions() as db:
            job = db.get(EmailDeliveryJob, job_id)
            sent = self._sent_payload(job)
        reply = self._reply_payload(
            graph_id="synced-reply",
            internet_message_id="<synced-reply@example.com>",
            conversation_id=str(sent["conversationId"]),
            reference_id=str(sent["internetMessageId"]),
        )

        class FakeClient:
            def __init__(self, _settings: Settings):
                self.sent_page_calls = 0

            def request(self, method: str, url: str, *, json=None):
                if method == "POST" and url == "/subscriptions":
                    return {
                        "id": "new-subscription",
                        "expirationDateTime": (self_now + timedelta(days=6)).isoformat(),
                    }
                if "sentitems" in url or "sent-page-2" in url:
                    self.sent_page_calls += 1
                    if self.sent_page_calls == 1:
                        return {"value": [], "@odata.nextLink": "https://graph.test/sent-page-2"}
                    return {"value": [sent], "@odata.deltaLink": "https://graph.test/sent-delta"}
                if "mailFolders/inbox" in url:
                    return {"value": [reply], "@odata.deltaLink": "https://graph.test/inbox-delta"}
                if "/messages?" in url:
                    return {"value": [sent, reply]}
                raise AssertionError(f"Unexpected Graph request: {method} {url}")

            def get_message(self, _mailbox: str, _message_id: str):
                return reply

        self_now = self.now
        with patch.object(email_reply_service, "GraphMailClient", FakeClient):
            email_reply_service.synchronize_mailbox(
                self.sessions,
                claim=claim,
                settings=self.settings,
            )
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            tracked_reply = db.scalar(
                select(EmailReply).where(EmailReply.graph_message_id == "synced-reply")
            )
            self.assertEqual(state.subscription_id, "new-subscription")
            self.assertEqual(state.sent_delta_link, "https://graph.test/sent-delta")
            self.assertEqual(state.inbox_delta_link, "https://graph.test/inbox-delta")
            self.assertIsNotNone(state.initial_backfill_completed_at)
            self.assertEqual(tracked_reply.lead_id, lead_id)

    def test_subscription_conflict_adopts_the_existing_exact_subscription(self) -> None:
        with self.sessions() as db:
            state = email_reply_service.ensure_mailbox_state(
                db,
                mailbox_email="sender@example.com",
                backfill_days=90,
                now=self.now,
            )
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="reply-worker",
                now=self.now,
            )
        expires_at = self.now + timedelta(days=5)

        class ConflictClient:
            def request(self, method: str, url: str, *, json=None):
                if method == "POST":
                    raise email_reply_service.GraphReplyError(
                        "microsoft_graph_subscription_conflict"
                    )
                self.assertions.append((method, url))
                return {
                    "value": [
                        {
                            "id": "existing-subscription",
                            "resource": "users/sender%40example.com/messages",
                            "changeType": "deleted,created,updated",
                            "notificationUrl": self_url,
                            "expirationDateTime": expires_at.isoformat(),
                        }
                    ]
                }

            assertions: list[tuple[str, str]] = []

        self_url = self.settings.microsoft_graph_notification_url
        client = ConflictClient()
        with patch.object(email_reply_service, "_utc_now", return_value=self.now):
            email_reply_service._ensure_subscription(
                self.sessions,
                client=client,
                claim=claim,
                settings=self.settings,
            )
        self.assertEqual(client.assertions, [("GET", "/subscriptions")])
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            self.assertEqual(state.subscription_id, "existing-subscription")

    def test_unread_refresh_cannot_write_after_lease_loss(self) -> None:
        self._seed_delivery(
            conversation_id="lease-conversation",
            internet_message_id="<lease-sent@example.com>",
        )
        reply_payload = self._reply_payload(
            graph_id="lease-reply",
            internet_message_id="<lease-reply@example.com>",
            conversation_id="lease-conversation",
            reference_id="<lease-sent@example.com>",
        )
        with self.sessions() as db:
            email_reply_service.process_graph_message(
                db,
                mailbox_email="sender@example.com",
                payload=reply_payload,
            )
            db.commit()
        self._healthy_state()
        with self.sessions() as db:
            state = db.get(GraphMailboxSyncState, "sender@example.com")
            state.next_sync_at = self.now
            db.commit()
            claim = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email=state.mailbox_email,
                worker_id="original-worker",
                now=self.now,
            )

        class LeaseStealingClient:
            def get_message(inner_self, _mailbox: str, _message_id: str):
                with self.sessions() as db:
                    state = db.get(GraphMailboxSyncState, "sender@example.com")
                    state.claimed_by = "replacement-worker"
                    state.claimed_at = self.now + timedelta(seconds=1)
                    state.heartbeat_at = state.claimed_at
                    db.commit()
                return {**reply_payload, "isRead": True}

        with self.assertRaisesRegex(
            email_reply_service.GraphReplyError,
            "reply_sync_lease_lost",
        ):
            email_reply_service._refresh_unread_replies(
                self.sessions,
                client=LeaseStealingClient(),
                claim=claim,
            )
        with self.sessions() as db:
            reply = db.scalar(
                select(EmailReply).where(EmailReply.graph_message_id == "lease-reply")
            )
            self.assertFalse(reply.is_read)


if __name__ == "__main__":
    unittest.main()
