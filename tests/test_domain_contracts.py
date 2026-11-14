from __future__ import annotations

import unittest

from visa_agent.domain.enums import (
    CaseStatus,
    DeliveryKind,
    ErrorCode,
    FactStatus,
    OutboundMessageKind,
    RequirementStatus,
    RouteCode,
)
from visa_agent.domain.errors import DomainError
from visa_agent.domain.models import (
    MODULE_BOUNDARIES,
    CaseRef,
    FinalPackageApproval,
    OutboundGateInput,
    evaluate_outbound_gate,
)
from visa_agent.domain.transitions import (
    CONTROL_FLAGS,
    TransitionContext,
    _requirements_for,
    allowed_targets,
    can_transition,
    validate_transition,
)


def _ordinary_gate(**overrides: object) -> OutboundGateInput:
    base: dict[str, object] = {
        "kind": OutboundMessageKind.ORDINARY_REPLY.value,
        "recipient_emails": frozenset({"friend@example.com"}),
        "allowlist": frozenset({"friend@example.com"}),
        "human_takeover": False,
        "human_send_approved": False,
        "case_version": 2,
        "outbox_case_version": 2,
        "has_unprocessed_inbound": False,
        "system_blocked": False,
        "paused": False,
        "cancelled": False,
    }
    base.update(overrides)
    return OutboundGateInput(**base)  # type: ignore[arg-type]


def _final_gate(**overrides: object) -> OutboundGateInput:
    approval = FinalPackageApproval(
        case_version=2,
        package_id="pkg-1",
        recipient_emails=frozenset({"friend@example.com"}),
    )
    base: dict[str, object] = {
        "kind": OutboundMessageKind.FINAL_PACKAGE.value,
        "recipient_emails": frozenset({"friend@example.com"}),
        "allowlist": frozenset({"friend@example.com"}),
        "human_takeover": False,
        "human_send_approved": True,
        "case_version": 2,
        "outbox_case_version": 2,
        "has_unprocessed_inbound": False,
        "system_blocked": False,
        "paused": False,
        "cancelled": False,
        "package_id": "pkg-1",
        "package_kind": DeliveryKind.FINAL_PACKAGE.value,
        "final_package_approval": approval,
    }
    base.update(overrides)
    return OutboundGateInput(**base)  # type: ignore[arg-type]


def _progress_gate(**overrides: object) -> OutboundGateInput:
    base: dict[str, object] = {
        "kind": OutboundMessageKind.PROGRESS_PACKAGE.value,
        "recipient_emails": frozenset({"friend@example.com"}),
        "allowlist": frozenset({"friend@example.com"}),
        "human_takeover": False,
        "human_send_approved": False,
        "case_version": 2,
        "outbox_case_version": 2,
        "has_unprocessed_inbound": False,
        "system_blocked": False,
        "paused": False,
        "cancelled": False,
        "package_id": "pkg-progress",
        "package_kind": DeliveryKind.PROGRESS_PACKAGE.value,
        "final_package_approval": None,
    }
    base.update(overrides)
    return OutboundGateInput(**base)  # type: ignore[arg-type]


class DomainContractTests(unittest.TestCase):
    def test_case_status_values_match_docs(self) -> None:
        expected = {
            "lead",
            "intake_authorization",
            "route_scope_confirmed",
            "collect_and_verify",
            "issue_resolution",
            "draft_preparation",
            "customer_confirmation",
            "final_review",
            "ready_to_deliver",
            "sent_awaiting_ack",
            "delivered",
            "closed",
        }
        self.assertEqual({s.value for s in CaseStatus}, expected)

    def test_fact_and_requirement_enums_stable(self) -> None:
        self.assertEqual(FactStatus.UNKNOWN.value, "unknown")
        self.assertEqual(
            RequirementStatus.EXEMPTED_WITH_BASIS.value, "exempted_with_basis"
        )
        self.assertEqual(RouteCode.STANDARD_VISITOR.value, "standard_visitor")
        self.assertNotEqual(
            DeliveryKind.FINAL_PACKAGE, DeliveryKind.PROGRESS_PACKAGE
        )

    def test_unused_outbound_alias_codes_removed(self) -> None:
        values = {c.value for c in ErrorCode}
        self.assertNotIn("UNKNOWN_OUTBOUND_KIND", values)
        self.assertNotIn("ORDINARY_REPLY_WITH_PACKAGE", values)
        self.assertIn("DELIVERY_RECEIPT_REQUIRED", values)

    def test_happy_path_transitions_with_preconditions(self) -> None:
        path = [
            CaseStatus.LEAD,
            CaseStatus.INTAKE_AUTHORIZATION,
            CaseStatus.ROUTE_SCOPE_CONFIRMED,
            CaseStatus.COLLECT_AND_VERIFY,
            CaseStatus.DRAFT_PREPARATION,
            CaseStatus.CUSTOMER_CONFIRMATION,
            CaseStatus.FINAL_REVIEW,
            CaseStatus.READY_TO_DELIVER,
            CaseStatus.SENT_AWAITING_ACK,
            CaseStatus.DELIVERED,
            CaseStatus.CLOSED,
        ]
        ctx = TransitionContext(
            case_version=1,
            customer_confirmation_case_version=1,
            final_review_approval_case_version=1,
            delivery_id="del-1",
            receipt_for_delivery_id="del-1",
        )
        for current, target in zip(path[:-1], path[1:], strict=True):
            validate_transition(current, target, ctx)

    def test_illegal_transition_raises(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(CaseStatus.LEAD, CaseStatus.READY_TO_DELIVER)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_version_bump_path_from_ready_to_collect(self) -> None:
        self.assertTrue(
            can_transition(
                CaseStatus.READY_TO_DELIVER, CaseStatus.COLLECT_AND_VERIFY
            )
        )
        self.assertIn(
            CaseStatus.COLLECT_AND_VERIFY,
            allowed_targets(CaseStatus.SENT_AWAITING_ACK),
        )

    def test_control_flags_are_orthogonal(self) -> None:
        self.assertIn("human_takeover", CONTROL_FLAGS)
        self.assertNotIn("lead", CONTROL_FLAGS)

    def test_case_ref_rejects_unknown_kwargs(self) -> None:
        with self.assertRaises(TypeError):
            CaseRef(
                tenant_id="t1",
                case_id="c1",
                case_version=1,
                status=CaseStatus.LEAD,
                unexpected="x",  # type: ignore[call-arg]
            )

    def test_case_ref_rejects_invalid_version(self) -> None:
        with self.assertRaises(ValueError):
            CaseRef(
                tenant_id="t1",
                case_id="c1",
                case_version=0,
                status=CaseStatus.LEAD,
            )

    def test_ai_module_cannot_send_or_approve(self) -> None:
        ai = next(m for m in MODULE_BOUNDARIES if m.name == "ai_capabilities")
        self.assertFalse(ai.may_send_email)
        self.assertFalse(ai.may_approve)
        self.assertFalse(ai.may_mutate_case)

    def test_only_send_control_may_send(self) -> None:
        senders = [m.name for m in MODULE_BOUNDARIES if m.may_send_email]
        self.assertEqual(senders, ["send_control"])

    def test_case_service_owns_transition_context_output(self) -> None:
        case_svc = next(m for m in MODULE_BOUNDARIES if m.name == "case_service")
        self.assertIn("transition_context", case_svc.outputs)


class TransitionPreconditionTests(unittest.TestCase):
    def test_requirements_helper_is_single_source_for_ready(self) -> None:
        req = _requirements_for(
            CaseStatus.FINAL_REVIEW, CaseStatus.READY_TO_DELIVER
        )
        self.assertEqual(
            req, frozenset({"customer_confirmation", "final_approval"})
        )

    def test_requirements_helper_for_final_review_any_source(self) -> None:
        for source in (
            CaseStatus.CUSTOMER_CONFIRMATION,
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.READY_TO_DELIVER,
            CaseStatus.SENT_AWAITING_ACK,
        ):
            req = _requirements_for(source, CaseStatus.FINAL_REVIEW)
            self.assertIn("customer_confirmation", req)

    def test_rejects_final_review_without_customer_confirmation(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.CUSTOMER_CONFIRMATION,
                CaseStatus.FINAL_REVIEW,
                TransitionContext(case_version=3),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED
        )

    def test_rejects_ready_to_deliver_rollback_to_final_review_without_confirmation(
        self,
    ) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.READY_TO_DELIVER,
                CaseStatus.FINAL_REVIEW,
                TransitionContext(case_version=4),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED
        )

    def test_rejects_sent_awaiting_ack_to_final_review_without_confirmation(
        self,
    ) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.SENT_AWAITING_ACK,
                CaseStatus.FINAL_REVIEW,
                TransitionContext(
                    case_version=4,
                    final_review_approval_case_version=4,
                ),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED
        )

    def test_rejects_ready_when_confirmation_stale_even_if_approval_current(
        self,
    ) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.FINAL_REVIEW,
                CaseStatus.READY_TO_DELIVER,
                TransitionContext(
                    case_version=5,
                    customer_confirmation_case_version=4,
                    final_review_approval_case_version=5,
                ),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED
        )

    def test_rejects_ready_when_approval_stale_even_if_confirmation_current(
        self,
    ) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.SENT_AWAITING_ACK,
                CaseStatus.READY_TO_DELIVER,
                TransitionContext(
                    case_version=5,
                    customer_confirmation_case_version=5,
                    final_review_approval_case_version=4,
                ),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.REVIEW_APPROVAL_REQUIRED)

    def test_rejects_ready_to_deliver_without_final_approval(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.FINAL_REVIEW,
                CaseStatus.READY_TO_DELIVER,
                TransitionContext(
                    case_version=3,
                    customer_confirmation_case_version=3,
                ),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.REVIEW_APPROVAL_REQUIRED)

    def test_rejects_delivered_without_receipt(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.SENT_AWAITING_ACK,
                CaseStatus.DELIVERED,
                TransitionContext(
                    case_version=1,
                    delivery_id="del-9",
                    receipt_for_delivery_id=None,
                ),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.DELIVERY_RECEIPT_REQUIRED
        )

    def test_rejects_delivered_when_receipt_for_old_package(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.SENT_AWAITING_ACK,
                CaseStatus.DELIVERED,
                TransitionContext(
                    case_version=1,
                    delivery_id="del-new",
                    receipt_for_delivery_id="del-old",
                ),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.DELIVERY_RECEIPT_REQUIRED
        )

    def test_allows_delivered_with_matching_receipt(self) -> None:
        validate_transition(
            CaseStatus.SENT_AWAITING_ACK,
            CaseStatus.DELIVERED,
            TransitionContext(
                case_version=1,
                delivery_id="del-9",
                receipt_for_delivery_id="del-9",
            ),
        )

    def test_rejects_closed_reopen_without_authorization(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.CLOSED,
                CaseStatus.LEAD,
                TransitionContext(case_version=1, reopen_authorized=False),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.REOPEN_UNAUTHORIZED)

    def test_rejects_gated_transition_without_context(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.CUSTOMER_CONFIRMATION,
                CaseStatus.FINAL_REVIEW,
                None,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_allows_reopen_when_authorized(self) -> None:
        validate_transition(
            CaseStatus.CLOSED,
            CaseStatus.INTAKE_AUTHORIZATION,
            TransitionContext(case_version=1, reopen_authorized=True),
        )


class IssueResolutionResumeTests(unittest.TestCase):
    def test_lead_cannot_skip_to_draft_via_issue_resolution(self) -> None:
        """Full counterexample: new lead finds issue, must not jump to draft."""
        validate_transition(
            CaseStatus.LEAD,
            CaseStatus.ISSUE_RESOLUTION,
            TransitionContext(case_version=1),
        )
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.ISSUE_RESOLUTION,
                CaseStatus.DRAFT_PREPARATION,
                TransitionContext(
                    case_version=1,
                    resume_status=CaseStatus.LEAD,
                ),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_lead_issue_recovers_via_resume_then_forward_path(self) -> None:
        ctx = TransitionContext(case_version=1, resume_status=CaseStatus.LEAD)
        validate_transition(
            CaseStatus.ISSUE_RESOLUTION, CaseStatus.LEAD, ctx
        )
        validate_transition(
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.INTAKE_AUTHORIZATION,
            ctx,
        )
        # Continue the normal pipeline after recovery.
        validate_transition(
            CaseStatus.LEAD,
            CaseStatus.INTAKE_AUTHORIZATION,
            TransitionContext(case_version=1),
        )

    def test_collect_resume_may_continue_to_draft(self) -> None:
        validate_transition(
            CaseStatus.ISSUE_RESOLUTION,
            CaseStatus.DRAFT_PREPARATION,
            TransitionContext(
                case_version=2,
                resume_status=CaseStatus.COLLECT_AND_VERIFY,
            ),
        )

    def test_issue_to_final_review_still_needs_confirmation(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            validate_transition(
                CaseStatus.ISSUE_RESOLUTION,
                CaseStatus.FINAL_REVIEW,
                TransitionContext(
                    case_version=3,
                    resume_status=CaseStatus.CUSTOMER_CONFIRMATION,
                ),
            )
        self.assertEqual(
            ctx.exception.code, ErrorCode.CUSTOMER_CONFIRMATION_REQUIRED
        )


class OutboundGateTests(unittest.TestCase):
    def test_ordinary_reply_allows_clean_whitelist_send(self) -> None:
        self.assertEqual(evaluate_outbound_gate(_ordinary_gate()), [])

    def test_ordinary_reply_blocks_non_allowlisted(self) -> None:
        reasons = evaluate_outbound_gate(
            _ordinary_gate(
                recipient_emails=frozenset({"evil@example.com"}),
            )
        )
        self.assertIn("OUTBOUND_NOT_ALLOWLISTED", reasons)

    def test_ordinary_reply_takeover_requires_approval(self) -> None:
        reasons = evaluate_outbound_gate(
            _ordinary_gate(human_takeover=True, human_send_approved=False)
        )
        self.assertIn("TAKEOVER_ACTIVE", reasons)

    def test_rejects_unknown_outbound_kind_as_validation_error(self) -> None:
        reasons = evaluate_outbound_gate(_ordinary_gate(kind="newsletter"))
        self.assertEqual(reasons, ["VALIDATION_ERROR"])

    def test_rejects_ordinary_reply_carrying_package_as_validation_error(
        self,
    ) -> None:
        reasons = evaluate_outbound_gate(
            _ordinary_gate(package_id="pkg-sneak")
        )
        self.assertIn("VALIDATION_ERROR", reasons)

    def test_progress_package_allows_without_final_approval(self) -> None:
        self.assertEqual(evaluate_outbound_gate(_progress_gate()), [])

    def test_final_package_allows_when_approval_fully_bound(self) -> None:
        self.assertEqual(evaluate_outbound_gate(_final_gate()), [])

    def test_final_package_rejects_missing_approval(self) -> None:
        reasons = evaluate_outbound_gate(
            _final_gate(final_package_approval=None)
        )
        self.assertIn("REVIEW_APPROVAL_REQUIRED", reasons)

    def test_rejects_progress_label_when_actual_package_is_final(self) -> None:
        reasons = evaluate_outbound_gate(
            _progress_gate(
                package_id="pkg-1",
                package_kind=DeliveryKind.FINAL_PACKAGE.value,
                final_package_approval=None,
            )
        )
        self.assertIn("REVIEW_APPROVAL_REQUIRED", reasons)


if __name__ == "__main__":
    unittest.main()
