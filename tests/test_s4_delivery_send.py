"""S4: delivery package assemble (W09) + case-bound send control (W10)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import psycopg

os.environ["VISA_AGENT_SKIP_APP_BOOT"] = "1"

from fastapi.testclient import TestClient

from tests.db_isolation import TEST_DB, TEST_PUBLISH_TOKEN, reset_test_database
from visa_agent.case_service import (
    create_case,
    get_case,
    mark_inbound_processed,
    record_customer_confirmation,
    record_final_review,
)
from visa_agent.config import Settings
from visa_agent.delivery import assemble_package, get_package
from visa_agent.document_pipeline import attach_evidence, set_evidence_quarantine
from visa_agent.domain.enums import CaseStatus, DeliveryKind, ErrorCode, OutboundMessageKind, RouteCode
from visa_agent.domain.errors import DomainError
from visa_agent.email_adapter.errors import SendPartialAcceptedError, SendStatusUnknownError
from visa_agent.email_adapter.outbox import build_send_operation_id
from visa_agent.rules.service import ensure_seed_route_packs, record_requirement_applicability, record_requirement_review
from visa_agent.send_control import PackageSendRequest, send_case_package
from visa_agent.storage.files import LocalFileStore
from visa_agent.storage.migrate import apply_migrations
from visa_agent.workflow.transitions import apply_transition

TENANT = "demo"


def _settings(**overrides: object) -> Settings:
    base = dict(
        app_host="127.0.0.1",
        app_port=8000,
        database_url=TEST_DB,
        file_storage_dir=Path(tempfile.mkdtemp()) / "files",
        tenant_id=TENANT,
        siliconflow_api_key="",
        siliconflow_base_url="https://api.siliconflow.cn/v1",
        siliconflow_model="zai-org/GLM-5.3-Flash",
        email_smtp_host="",
        email_smtp_port=587,
        email_smtp_user="",
        email_smtp_password="",
        email_from="",
        email_imap_host="",
        email_imap_port=993,
        email_imap_user="",
        email_imap_password="",
        outbound_allowlist=frozenset({"allow@example.com"}),
        rule_publish_token=TEST_PUBLISH_TOKEN,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _store(settings: Settings) -> LocalFileStore:
    return LocalFileStore(settings.file_storage_dir)


def _accepted_evidence(
    case_id: str,
    *,
    store: LocalFileStore,
    blob: bytes = b"%PDF-s4",
    classification: str = "passport",
) -> str:
    case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
    ev = attach_evidence(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        file_store=store,
        data=blob,
        original_filename="doc.pdf",
        classification=classification,
        expected_case_version=case.case_version,
    )
    case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
    set_evidence_quarantine(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        evidence_id=ev.id,
        quarantine_status="accepted_for_review",
        expected_case_version=case.case_version,
    )
    return ev.id


def _ver(case_id: str) -> int:
    return get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id).case_version


def _walk_to_ready(case_id: str) -> None:
    for target in [
        CaseStatus.INTAKE_AUTHORIZATION,
        CaseStatus.ROUTE_SCOPE_CONFIRMED,
        CaseStatus.COLLECT_AND_VERIFY,
        CaseStatus.DRAFT_PREPARATION,
        CaseStatus.CUSTOMER_CONFIRMATION,
    ]:
        apply_transition(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case_id, target=target
        )
    record_customer_confirmation(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        expected_case_version=_ver(case_id),
    )
    apply_transition(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        target=CaseStatus.FINAL_REVIEW,
    )
    record_final_review(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        decision="approve",
        expected_case_version=_ver(case_id),
    )
    apply_transition(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        target=CaseStatus.READY_TO_DELIVER,
    )


def _approve_package(
    case_id: str,
    package_id: str,
    *,
    recipients: list[str] | None = None,
) -> None:
    record_final_review(
        database_url=TEST_DB,
        tenant_id=TENANT,
        case_id=case_id,
        decision="approve",
        expected_case_version=_ver(case_id),
        package_id=package_id,
        recipients=recipients or ["allow@example.com"],
        reviewer_id="reviewer-1",
    )


class DeliveryAssembleTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        apply_migrations(TEST_DB)
        self.settings = _settings()
        self.store = _store(self.settings)

    def test_rejects_isolated_evidence(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-iso")
        ev = attach_evidence(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            file_store=self.store,
            data=b"%PDF-iso",
            original_filename="x.pdf",
            classification="passport",
            expected_case_version=1,
        )
        with self.assertRaises(DomainError) as err:
            assemble_package(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                package_kind=DeliveryKind.FINAL_PACKAGE.value,
                file_store=self.store,
                evidence_ids=[ev.id],
                expected_case_version=2,
            )
        self.assertEqual(err.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_assemble_freezes_bytes(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-ok")
        eid = _accepted_evidence(case.id, store=self.store)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        self.assertEqual(pkg.package_kind, DeliveryKind.FINAL_PACKAGE.value)
        self.assertEqual(len(pkg.items), 1)
        self.assertEqual(pkg.items[0].evidence_id, eid)
        self.assertTrue(pkg.items[0].frozen_rel)
        frozen = self.store.root / pkg.items[0].frozen_rel
        self.assertTrue(frozen.is_file())
        loaded = get_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_id=pkg.package_id,
        )
        self.assertEqual(loaded.manifest_digest, pkg.manifest_digest)


class SendControlTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        apply_migrations(TEST_DB)
        self.settings = _settings()
        self.store = _store(self.settings)

    def _ready_final_package(self, title: str = "s4-send"):
        ensure_seed_route_packs(database_url=TEST_DB)
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title=title, route=RouteCode.STANDARD_VISITOR)
        keys = dict(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        evidence = []
        for label, requirement in (
            ("passport", "passport_valid"), ("purpose", "visit_purpose_allowed"),
            ("itinerary", "intention_to_leave"), ("bank", "funds_for_stay"), ("employment", "home_ties"),
        ):
            evidence.append(_accepted_evidence(
                case.id, store=self.store, blob=f"%PDF-s4-{label}".encode(), classification=label,
            ))
            record_requirement_review(**keys, requirement_key=requirement, expected_case_version=_ver(case.id))
        for requirement in ("sponsor_if_any", "translation_if_needed", "transfer_human_triggers"):
            record_requirement_applicability(**keys, requirement_key=requirement, decision="not_applicable",
                                             note="synthetic test case", expected_case_version=_ver(case.id))
        _walk_to_ready(case.id)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=evidence,
            expected_case_version=case.case_version,
        )
        return case, pkg

    def test_final_package_requires_bound_approval(self) -> None:
        case, pkg = self._ready_final_package()
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="final",
                body_text="pkg",
                send_now=False,
            ),
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("REVIEW_APPROVAL_REQUIRED", blocked.block_reasons)

        _approve_package(case.id, pkg.package_id)
        ok = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="final-ok",
                body_text="pkg",
                send_now=True,
                send_operation_id="s4-final-ok-1",
            ),
        )
        self.assertEqual(ok.status, "simulated")
        self.assertEqual(ok.block_reasons, ())
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        self.assertTrue(case.current_delivery_id)
        with psycopg.connect(TEST_DB) as conn:
            row = conn.execute(
                """
                SELECT package_id, outbox_id, simulated_send
                FROM case_deliveries WHERE id = %s
                """,
                (case.current_delivery_id,),
            ).fetchone()
        self.assertEqual(row[0], pkg.package_id)
        self.assertEqual(row[1], ok.outbox_id)
        self.assertTrue(row[2])

    def test_omitted_required_attachment_blocks_an_approved_package(self) -> None:
        case, complete = self._ready_final_package()
        passport = next(item.evidence_id for item in complete.items if item.classification == "passport")
        partial = assemble_package(
            database_url=TEST_DB, tenant_id=TENANT, case_id=case.id,
            file_store=self.store, package_kind=DeliveryKind.FINAL_PACKAGE.value,
            evidence_ids=[passport], expected_case_version=case.case_version,
        )
        _approve_package(case.id, partial.package_id)
        result = send_case_package(
            settings=self.settings, case_id=case.id, file_store=self.store,
            req=PackageSendRequest(partial.package_id, ("allow@example.com",), "final", "package", send_now=False),
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("FINAL_PACKAGE_REQUIREMENT_BLOCKED:funds_for_stay", result.block_reasons)

    def test_reject_supersedes_prior_approve(self) -> None:
        case, pkg = self._ready_final_package("s4-rej")
        _approve_package(case.id, pkg.package_id)
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="reject",
            expected_case_version=_ver(case.id),
            package_id=pkg.package_id,
            reviewer_id="reviewer-1",
        )
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="x",
                body_text="y",
                send_now=False,
            ),
        )
        self.assertIn("REVIEW_APPROVAL_REQUIRED", blocked.block_reasons)

    def test_wrong_package_approval_mismatch(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-mis")
        eid = _accepted_evidence(case.id, store=self.store)
        _walk_to_ready(case.id)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg_a = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        pkg_b = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        _approve_package(case.id, pkg_b.package_id)
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg_a.package_id,
                recipients=("allow@example.com",),
                subject="x",
                body_text="y",
                send_now=False,
            ),
        )
        self.assertIn("FINAL_PACKAGE_APPROVAL_MISMATCH", blocked.block_reasons)

    def test_unknown_package_review_rejected(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-nopkg")
        with self.assertRaises(DomainError) as err:
            record_final_review(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                decision="approve",
                expected_case_version=case.case_version,
                package_id="does-not-exist",
                recipients=["allow@example.com"],
                reviewer_id="reviewer-1",
            )
        self.assertEqual(err.exception.code, ErrorCode.NOT_FOUND)

    def test_frozen_byte_tamper_blocks_send(self) -> None:
        settings = self.settings
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-hash")
        eid = _accepted_evidence(case.id, store=self.store, blob=b"%PDF-aaa")
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        frozen = self.store.root / pkg.items[0].frozen_rel
        frozen.write_bytes(b"%PDF-TAMPERED")
        with self.assertRaises(DomainError) as err:
            send_case_package(
                settings=settings,
                case_id=case.id,
                file_store=self.store,
                req=PackageSendRequest(
                    package_id=pkg.package_id,
                    recipients=("allow@example.com",),
                    subject="x",
                    body_text="y",
                ),
            )
        self.assertEqual(err.exception.code, ErrorCode.DELIVERY_GATE_FAILED)

    def test_idempotent_op_id_binds_recipients(self) -> None:
        case, pkg = self._ready_final_package("s4-idem")
        _approve_package(case.id, pkg.package_id)
        calls: list[tuple] = []

        def fake_send(s, recipients, subject, body, attachments):
            calls.append((tuple(recipients), subject, body, tuple(
                (n, hashlib.sha256(d).hexdigest()) for n, d in attachments
            )))
            return "smtp-mock-1"

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
        )
        first = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=fake_send,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="idem",
                body_text="body",
                send_operation_id="op-bound-1",
            ),
        )
        self.assertEqual(first.status, "provider_accepted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ("allow@example.com",))
        self.assertTrue(calls[0][3])  # attachments present

        # Same op id + same content → no second SMTP.
        second = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=fake_send,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="idem",
                body_text="body",
                send_operation_id="op-bound-1",
            ),
        )
        self.assertEqual(second.status, "provider_accepted")
        self.assertTrue(second.resent_blocked)
        self.assertEqual(len(calls), 1)

        # Same op id + different recipients → rejected, no SMTP.
        with self.assertRaises(DomainError) as err:
            send_case_package(
                settings=smtp_settings,
                case_id=case.id,
                file_store=self.store,
                send_fn=fake_send,
                req=PackageSendRequest(
                    package_id=pkg.package_id,
                    recipients=("allow@example.com",),
                    subject="idem-other",
                    body_text="body",
                    send_operation_id="op-bound-1",
                ),
            )
        self.assertEqual(err.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertEqual(len(calls), 1)

    def test_concurrent_claim_sends_once(self) -> None:
        case, pkg = self._ready_final_package("s4-conc")
        _approve_package(case.id, pkg.package_id)
        calls: list[str] = []

        def fake_send(s, recipients, subject, body, attachments):
            calls.append("sent")
            return "smtp-conc"

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
        )

        def _once() -> str:
            r = send_case_package(
                settings=smtp_settings,
                case_id=case.id,
                file_store=self.store,
                send_fn=fake_send,
                req=PackageSendRequest(
                    package_id=pkg.package_id,
                    recipients=("allow@example.com",),
                    subject="conc",
                    body_text="body",
                    send_operation_id="op-conc-1",
                ),
            )
            return r.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: _once(), range(2)))
        self.assertEqual(len(calls), 1, statuses)
        # Loser may observe processing/provider_accepted while winner holds the claim.
        self.assertTrue(
            set(statuses) <= {"provider_accepted", "processing"},
            statuses,
        )
        self.assertIn("provider_accepted", statuses)

    def test_status_unknown_no_auto_resend(self) -> None:
        case, pkg = self._ready_final_package("s4-unk")
        _approve_package(case.id, pkg.package_id)
        calls: list[int] = []

        def boom(s, recipients, subject, body, attachments):
            calls.append(1)
            raise SendStatusUnknownError("smtp unclear")

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
        )
        first = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=boom,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="unk",
                body_text="body",
                send_operation_id="op-unk-1",
            ),
        )
        self.assertEqual(first.status, "status_unknown")
        second = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=boom,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="unk",
                body_text="body",
                send_operation_id="op-unk-1",
            ),
        )
        self.assertEqual(second.status, "status_unknown")
        self.assertTrue(second.resent_blocked)
        self.assertEqual(len(calls), 1)

    def test_version_bump_blocks_send(self) -> None:
        case, pkg = self._ready_final_package("s4-ver")
        _approve_package(case.id, pkg.package_id)
        from visa_agent.case_service.service import bump_case_version

        bump_case_version(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=_ver(case.id),
        )
        with self.assertRaises(DomainError) as err:
            send_case_package(
                settings=self.settings,
                case_id=case.id,
                file_store=self.store,
                req=PackageSendRequest(
                    package_id=pkg.package_id,
                    recipients=("allow@example.com",),
                    subject="x",
                    body_text="y",
                ),
            )
        self.assertEqual(err.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_unprocessed_inbound_blocks_send(self) -> None:
        case, pkg = self._ready_final_package("s4-inb")
        _approve_package(case.id, pkg.package_id)
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO email_events (
                    id, tenant_id, direction, dedupe_key, subject, body_text,
                    case_id, attribution_status, processing_status, created_at
                ) VALUES (
                    %s, %s, 'inbound', %s, 'update', 'new info',
                    %s, 'attributed', 'unprocessed', NOW()
                )
                """,
                (uuid.uuid4().hex, TENANT, uuid.uuid4().hex, case.id),
            )
            conn.commit()
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="x",
                body_text="y",
                send_now=False,
            ),
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("DELIVERY_GATE_FAILED", blocked.block_reasons)

    def test_assemble_does_not_clear_unprocessed_inbound(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-pre-inb")
        eid = _accepted_evidence(case.id, store=self.store)
        inbound_id = uuid.uuid4().hex
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO email_events (
                    id, tenant_id, direction, dedupe_key, subject, body_text,
                    case_id, attribution_status, processing_status
                ) VALUES (
                    %s, %s, 'inbound', %s, 'stop', '请停止交付',
                    %s, 'attributed', 'unprocessed'
                )
                """,
                (inbound_id, TENANT, uuid.uuid4().hex, case.id),
            )
            conn.commit()
        _walk_to_ready(case.id)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        # Progress assemble must NOT clear the stop-delivery inbound.
        assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        with psycopg.connect(TEST_DB) as conn:
            row = conn.execute(
                """
                SELECT processing_status FROM email_events WHERE id = %s
                """,
                (inbound_id,),
            ).fetchone()
        self.assertEqual(row[0], "unprocessed")
        final = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        _approve_package(case.id, final.package_id)
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=final.package_id,
                recipients=("allow@example.com",),
                subject="ok",
                body_text="body",
                send_now=False,
            ),
        )
        self.assertIn("DELIVERY_GATE_FAILED", blocked.block_reasons)
        # Stop-delivery conclusion clears the gate but invalidates prior approvals.
        before_ver = _ver(case.id)
        mark_inbound_processed(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            email_event_id=inbound_id,
            conclusion="stop_delivery",
            processed_by="ops-1",
            expected_case_version=before_ver,
        )
        self.assertGreater(_ver(case.id), before_ver)
        with self.assertRaises(DomainError) as err:
            send_case_package(
                settings=self.settings,
                case_id=case.id,
                file_store=self.store,
                req=PackageSendRequest(
                    package_id=final.package_id,
                    recipients=("allow@example.com",),
                    subject="ok2",
                    body_text="body",
                ),
            )
        self.assertEqual(err.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_case_level_reject_blocks_package_send(self) -> None:
        case, pkg = self._ready_final_package("s4-case-rej")
        _approve_package(case.id, pkg.package_id)
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="reject",
            expected_case_version=_ver(case.id),
        )
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="x",
                body_text="y",
                send_now=False,
            ),
        )
        self.assertIn("REVIEW_APPROVAL_REQUIRED", blocked.block_reasons)

    def test_acknowledged_inbound_allows_send_without_version_bump(self) -> None:
        case, pkg = self._ready_final_package("s4-ack-inb")
        _approve_package(case.id, pkg.package_id)
        inbound_id = uuid.uuid4().hex
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO email_events (
                    id, tenant_id, direction, dedupe_key, subject, body_text,
                    case_id, attribution_status, processing_status
                ) VALUES (
                    %s, %s, 'inbound', %s, 'fyi', 'noted',
                    %s, 'attributed', 'unprocessed'
                )
                """,
                (inbound_id, TENANT, uuid.uuid4().hex, case.id),
            )
            conn.commit()
        ver = _ver(case.id)
        mark_inbound_processed(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            email_event_id=inbound_id,
            conclusion="acknowledged",
            processed_by="ops-1",
            expected_case_version=ver,
        )
        self.assertEqual(_ver(case.id), ver)
        ok = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="after-ack",
                body_text="body",
            ),
        )
        self.assertEqual(ok.status, "simulated")

    def test_mark_processed_requires_actor(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-mp-auth")
        inbound_id = uuid.uuid4().hex
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                INSERT INTO email_events (
                    id, tenant_id, direction, dedupe_key, subject, body_text,
                    case_id, attribution_status, processing_status
                ) VALUES (
                    %s, %s, 'inbound', %s, 'x', 'y',
                    %s, 'attributed', 'unprocessed'
                )
                """,
                (inbound_id, TENANT, uuid.uuid4().hex, case.id),
            )
            conn.commit()
        with self.assertRaises(DomainError) as err:
            mark_inbound_processed(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                email_event_id=inbound_id,
                conclusion="acknowledged",
                processed_by="",
                expected_case_version=case.case_version,
            )
        self.assertEqual(err.exception.code, ErrorCode.UNAUTHORIZED)

    def test_partial_accepted_blocks_retry(self) -> None:
        case, pkg = self._ready_final_package("s4-partial")
        _approve_package(case.id, pkg.package_id)
        calls: list[tuple[str, ...]] = []

        def partial_send(s, recipients, subject, body, attachments):
            calls.append(tuple(recipients))
            raise SendPartialAcceptedError(
                accepted=["allow@example.com"],
                refused={"other@example.com": (550, b"user unknown")},
                provider_response="partial;A ok;B refused",
            )

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
            outbound_allowlist=frozenset(
                {"allow@example.com", "other@example.com"}
            ),
        )
        # Approve for both recipients
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=_ver(case.id),
            package_id=pkg.package_id,
            recipients=["allow@example.com", "other@example.com"],
            reviewer_id="reviewer-1",
        )
        first = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=partial_send,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="partial",
                body_text="body",
                send_operation_id="op-partial-1",
            ),
        )
        self.assertEqual(first.status, "partial_accepted")
        second = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=partial_send,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="partial",
                body_text="body",
                send_operation_id="op-partial-1",
            ),
        )
        self.assertEqual(second.status, "partial_accepted")
        self.assertTrue(second.resent_blocked)
        self.assertEqual(len(calls), 1)

    def test_rcpt_stage_refusal_is_failed_and_retryable(self) -> None:
        """A=250 B=421 at RCPT (before DATA) must not become partial_accepted."""
        from unittest.mock import MagicMock
        from smtplib import SMTPRecipientsRefused

        from visa_agent.email_adapter.smtp_client import send_smtp

        case, pkg = self._ready_final_package("s4-rcpt-partial")
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=_ver(case.id),
            package_id=pkg.package_id,
            recipients=["allow@example.com", "other@example.com"],
            reviewer_id="reviewer-1",
        )
        calls: list[int] = []

        def send_via_client(s, recipients, subject, body, attachments):
            calls.append(1)
            with patch(
                "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
            ) as mock_smtp_cls:
                session = MagicMock()
                mock_smtp_cls.return_value.__enter__.return_value = session
                # Body never sent — refuse at RCPT (even if A looked accepted).
                session.send_message.side_effect = SMTPRecipientsRefused(
                    {
                        "other@example.com": (421, b"try again later"),
                    }
                )
                return send_smtp(
                    s,
                    recipients=list(recipients),
                    subject=subject,
                    body_text=body,
                    attachments=attachments,
                )

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
            outbound_allowlist=frozenset(
                {"allow@example.com", "other@example.com"}
            ),
        )
        first = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=send_via_client,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="rcpt-stage",
                body_text="body",
                send_operation_id="op-rcpt-stage-1",
            ),
        )
        self.assertEqual(first.status, "failed")
        second = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=send_via_client,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="rcpt-stage",
                body_text="body",
                send_operation_id="op-rcpt-stage-1",
            ),
        )
        self.assertEqual(second.status, "failed")
        self.assertEqual(len(calls), 2)

    def test_partial_then_quit_421_keeps_partial_no_resend(self) -> None:
        from unittest.mock import MagicMock
        from smtplib import SMTPResponseException

        from visa_agent.email_adapter.smtp_client import send_smtp

        case, pkg = self._ready_final_package("s4-partial-quit")
        record_final_review(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            decision="approve",
            expected_case_version=_ver(case.id),
            package_id=pkg.package_id,
            recipients=["allow@example.com", "other@example.com"],
            reviewer_id="reviewer-1",
        )
        calls: list[int] = []

        def send_via_client(s, recipients, subject, body, attachments):
            calls.append(1)
            with patch(
                "visa_agent.email_adapter.smtp_client.smtplib.SMTP"
            ) as mock_smtp_cls:
                session = MagicMock()
                mock_smtp_cls.return_value.__enter__.return_value = session
                session.send_message.return_value = {
                    "other@example.com": (550, b"user unknown")
                }
                mock_smtp_cls.return_value.__exit__.side_effect = (
                    SMTPResponseException(421, b"Service not available")
                )
                return send_smtp(
                    s,
                    recipients=list(recipients),
                    subject=subject,
                    body_text=body,
                    attachments=attachments,
                )

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
            outbound_allowlist=frozenset(
                {"allow@example.com", "other@example.com"}
            ),
        )
        first = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=send_via_client,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="partial-quit",
                body_text="body",
                send_operation_id="op-partial-quit-1",
            ),
        )
        self.assertEqual(first.status, "partial_accepted")
        self.assertIn("quit:421:", first.provider_response or "")
        second = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=send_via_client,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com", "other@example.com"),
                subject="partial-quit",
                body_text="body",
                send_operation_id="op-partial-quit-1",
            ),
        )
        self.assertEqual(second.status, "partial_accepted")
        self.assertTrue(second.resent_blocked)
        self.assertEqual(len(calls), 1)

    def test_ordinary_reply_op_id_unchanged_without_package(self) -> None:
        a = build_send_operation_id(
            tenant_id=TENANT,
            kind=OutboundMessageKind.ORDINARY_REPLY.value,
            recipients=("allow@example.com",),
            subject="hi",
            body_text="body",
        )
        b = build_send_operation_id(
            tenant_id=TENANT,
            kind=OutboundMessageKind.ORDINARY_REPLY.value,
            recipients=("allow@example.com",),
            subject="hi",
            body_text="body",
            case_id="ignored-without-package",
            case_version=9,
        )
        self.assertEqual(a, b)
        pkg = build_send_operation_id(
            tenant_id=TENANT,
            kind=OutboundMessageKind.FINAL_PACKAGE.value,
            recipients=("allow@example.com",),
            subject="hi",
            body_text="body",
            case_id="c1",
            case_version=1,
            package_id="p1",
        )
        self.assertNotEqual(a, pkg)

    def test_default_op_id_distinct_per_package(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-opid")
        eid = _accepted_evidence(case.id, store=self.store)
        _walk_to_ready(case.id)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg_a = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        pkg_b = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        a = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg_a.package_id,
                recipients=("allow@example.com",),
                subject="same",
                body_text="same",
            ),
        )
        b = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg_b.package_id,
                recipients=("allow@example.com",),
                subject="same",
                body_text="same",
            ),
        )
        self.assertEqual(a.status, "simulated")
        self.assertEqual(b.status, "simulated")
        self.assertNotEqual(a.send_operation_id, b.send_operation_id)

    def test_stale_package_review_rejected(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-oldpkg")
        eid = _accepted_evidence(case.id, store=self.store)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        from visa_agent.case_service.service import bump_case_version

        bump_case_version(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            expected_case_version=_ver(case.id),
        )
        with self.assertRaises(DomainError) as err:
            record_final_review(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                decision="approve",
                expected_case_version=_ver(case.id),
                package_id=pkg.package_id,
                recipients=["allow@example.com"],
                reviewer_id="r1",
            )
        self.assertEqual(err.exception.code, ErrorCode.CONFLICT_VERSION)

    def test_classification_in_manifest_digest(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-cls")
        eid = _accepted_evidence(case.id, store=self.store)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        with psycopg.connect(TEST_DB) as conn:
            conn.execute(
                """
                UPDATE delivery_packages
                SET manifest_json = (
                    SELECT jsonb_agg(
                        CASE
                          WHEN elem->>'evidence_id' = %s
                          THEN elem || '{"classification":"tampered"}'::jsonb
                          ELSE elem
                        END
                    )
                    FROM jsonb_array_elements(manifest_json) elem
                )
                WHERE id = %s
                """,
                (eid, pkg.package_id),
            )
            conn.commit()
        from visa_agent.delivery import get_package, verify_package_bytes

        loaded = get_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_id=pkg.package_id,
        )
        with self.assertRaises(DomainError) as err:
            verify_package_bytes(file_store=self.store, package=loaded)
        self.assertEqual(err.exception.code, ErrorCode.DELIVERY_GATE_FAILED)

    def test_version_bump_during_send_keeps_history_only(self) -> None:
        case, pkg = self._ready_final_package("s4-bump-send")
        _approve_package(case.id, pkg.package_id)
        claim_version = case.case_version

        def bump_then_accept(s, recipients, subject, body, attachments):
            from visa_agent.case_service.service import bump_case_version

            bump_case_version(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                expected_case_version=claim_version,
            )
            return "smtp-after-bump"

        smtp_settings = _settings(
            file_storage_dir=self.settings.file_storage_dir,
            email_smtp_host="smtp.example.com",
            email_smtp_user="u",
            email_smtp_password="p",
            email_from="from@example.com",
        )
        result = send_case_package(
            settings=smtp_settings,
            case_id=case.id,
            file_store=self.store,
            send_fn=bump_then_accept,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="bump",
                body_text="body",
                send_operation_id="op-bump-1",
            ),
        )
        self.assertEqual(result.status, "provider_accepted")
        after = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        self.assertGreater(after.case_version, claim_version)
        # Newer version must not adopt the vN send as current delivery.
        self.assertIsNone(after.current_delivery_id)
        with psycopg.connect(TEST_DB) as conn:
            hist = conn.execute(
                """
                SELECT case_version, package_id FROM case_deliveries
                WHERE case_id = %s AND outbox_id = %s
                """,
                (case.id, result.outbox_id),
            ).fetchone()
        self.assertEqual(hist[0], claim_version)
        self.assertEqual(hist[1], pkg.package_id)

    def test_progress_send_does_not_replace_final_current(self) -> None:
        case, final_pkg = self._ready_final_package("s4-prog-ovr")
        _approve_package(case.id, final_pkg.package_id)
        final = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=final_pkg.package_id,
                recipients=("allow@example.com",),
                subject="final",
                body_text="f",
                send_operation_id="op-final-keep",
            ),
        )
        self.assertEqual(final.status, "simulated")
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        final_delivery = case.current_delivery_id
        prog = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.PROGRESS_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[final_pkg.items[0].evidence_id],
            expected_case_version=case.case_version,
        )
        send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=prog.package_id,
                recipients=("allow@example.com",),
                subject="prog",
                body_text="p",
                send_operation_id="op-prog-keep",
            ),
        )
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        self.assertEqual(case.current_delivery_id, final_delivery)

    def test_unready_case_blocks_final_send(self) -> None:
        case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-early")
        eid = _accepted_evidence(case.id, store=self.store)
        case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
        pkg = assemble_package(
            database_url=TEST_DB,
            tenant_id=TENANT,
            case_id=case.id,
            package_kind=DeliveryKind.FINAL_PACKAGE.value,
            file_store=self.store,
            evidence_ids=[eid],
            expected_case_version=case.case_version,
        )
        _approve_package(case.id, pkg.package_id)
        blocked = send_case_package(
            settings=self.settings,
            case_id=case.id,
            file_store=self.store,
            req=PackageSendRequest(
                package_id=pkg.package_id,
                recipients=("allow@example.com",),
                subject="x",
                body_text="y",
                send_now=False,
            ),
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("DELIVERY_GATE_FAILED", blocked.block_reasons)
        self.assertIn("CUSTOMER_CONFIRMATION_REQUIRED", blocked.block_reasons)


class S4ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_database()
        apply_migrations(TEST_DB)
        self.settings = _settings()
        self.store = _store(self.settings)

    def test_stage_and_assemble_api(self) -> None:
        with patch("visa_agent.fastapi_app.get_settings", return_value=self.settings):
            from visa_agent import fastapi_app as fa

            client = TestClient(fa.app)
            self.assertEqual(client.get("/").json()["data"]["stage"], "S4")
            created = client.post("/cases", json={"title": "s4-api"}).json()
            case_id = created["data"]["case_id"]
            eid = _accepted_evidence(case_id, store=self.store)
            case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case_id)
            resp = client.post(
                f"/cases/{case_id}/packages/assemble",
                json={
                    "package_kind": "progress_package",
                    "expected_case_version": case.case_version,
                    "evidence_ids": [eid],
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["data"]["package_id"])

    def test_package_review_requires_token(self) -> None:
        with patch("visa_agent.fastapi_app.get_settings", return_value=self.settings):
            from visa_agent import fastapi_app as fa

            client = TestClient(fa.app)
            case = create_case(database_url=TEST_DB, tenant_id=TENANT, title="s4-tok")
            eid = _accepted_evidence(case.id, store=self.store)
            case = get_case(database_url=TEST_DB, tenant_id=TENANT, case_id=case.id)
            pkg = assemble_package(
                database_url=TEST_DB,
                tenant_id=TENANT,
                case_id=case.id,
                package_kind=DeliveryKind.FINAL_PACKAGE.value,
                file_store=self.store,
                evidence_ids=[eid],
                expected_case_version=case.case_version,
            )
            denied = client.post(
                f"/cases/{case.id}/reviews",
                json={
                    "decision": "approve",
                    "expected_case_version": case.case_version,
                    "package_id": pkg.package_id,
                    "recipients": ["allow@example.com"],
                    "reviewer_id": "r1",
                },
            )
            self.assertEqual(denied.status_code, 403)
            ok = client.post(
                f"/cases/{case.id}/reviews",
                headers={"X-Rule-Publish-Token": TEST_PUBLISH_TOKEN},
                json={
                    "decision": "approve",
                    "expected_case_version": case.case_version,
                    "package_id": pkg.package_id,
                    "recipients": ["allow@example.com"],
                    "reviewer_id": "r1",
                },
            )
            self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
