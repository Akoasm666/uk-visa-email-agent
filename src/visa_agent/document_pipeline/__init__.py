"""Document pipeline — isolate originals + human annotations (no OCR confirm)."""

from visa_agent.document_pipeline.service import (
    EvidenceAnnotation,
    EvidenceRecord,
    attach_evidence,
    get_evidence,
    list_annotations,
    list_evidence,
    record_annotation,
    set_evidence_quarantine,
)

__all__ = [
    "EvidenceAnnotation",
    "EvidenceRecord",
    "attach_evidence",
    "get_evidence",
    "list_annotations",
    "list_evidence",
    "record_annotation",
    "set_evidence_quarantine",
]
