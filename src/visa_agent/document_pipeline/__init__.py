"""Document pipeline — isolate originals + human annotations + derived OCR."""

from visa_agent.document_pipeline.ocr import (
    OcrExtraction,
    OcrProvider,
    SyntheticOcrProvider,
    default_ocr_provider,
)
from visa_agent.document_pipeline.classify import classify_attachment
from visa_agent.document_pipeline.service import (
    EvidenceAnnotation,
    EvidenceOcrResult,
    EvidenceRecord,
    attach_evidence,
    get_evidence,
    get_ocr_result,
    latest_ocr_by_evidence,
    list_annotations,
    list_evidence,
    list_ocr_results,
    record_annotation,
    run_ocr_on_evidence,
    set_evidence_classification,
    set_evidence_quarantine,
)

__all__ = [
    "EvidenceAnnotation",
    "EvidenceOcrResult",
    "EvidenceRecord",
    "OcrExtraction",
    "OcrProvider",
    "SyntheticOcrProvider",
    "attach_evidence",
    "classify_attachment",
    "default_ocr_provider",
    "get_evidence",
    "get_ocr_result",
    "latest_ocr_by_evidence",
    "list_annotations",
    "list_evidence",
    "list_ocr_results",
    "record_annotation",
    "run_ocr_on_evidence",
    "set_evidence_classification",
    "set_evidence_quarantine",
]
