"""W06-V Standard Visitor signal assessment — shared by scenarios and evaluate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

Polarity = Literal["affirmative", "negative", "unknown"]

# Individual triggers that must all be cleared (or overall conclusion) for negative.
TRANSFER_CHECK_KEYS = frozenset(
    {
        "transfer_minor",
        "transfer_complex_sponsor",
        "transfer_refusal_history",
        "transfer_in_country_switch",
        "transfer_unclear_activity",
        "transfer_medical_ppe",
    }
)

TRANSFER_TRIGGER_KEYS = TRANSFER_CHECK_KEYS | frozenset(
    {
        "transfer_human_triggers",
        "transfer_assessment",
    }
)

_AFFIRMATIVE = frozenset(
    {"yes", "y", "true", "1", "affirmative", "present", "triggered", "required"}
)
_NEGATIVE = frozenset(
    {"no", "n", "false", "0", "negative", "absent", "cleared", "not_applicable"}
)

_FORMULA_PHRASE = re.compile(
    r"student[_\s-]?fixed[_\s-]?funds"
    r"|cas[_\s-]?funds"
    r"|student[_\s-]?maintenance"
    r"|ukvi[_\s-]?student[_\s-]?funds(?:[_\s-]?formula)?"
    r"|student\s+funds\s+formula"
    r"|学生(?:固定)?资金公式"
    r"|Student\s*资金公式",
    re.I,
)
_BARE_CAS = re.compile(r"^cas$", re.I)
_FORMULA_NEGATION = re.compile(
    r"(未使用|没有使用|未采用|不使用|并未使用|"
    r"not\s+used|did\s+not\s+use|doesn't\s+use|does\s+not\s+use|"
    r"without\s+using|no\s+use\s+of|never\s+used)",
    re.I,
)
_FUNDS_KEY_HINTS = (
    "fund",
    "bank",
    "money",
    "maintenance",
    "cas",
    "student_fixed",
    "ukvi_student",
    "finance",
    "balance",
)

SCENARIO_TITLES = {
    "standard_adult_tourist": "成年境外个人旅游/探亲（标准场景）",
    "transfer_minor_or_complex": "未成年人/复杂赞助等默认转人工",
    "counter_student_funds": "反例：禁止套用 Student 资金公式",
}

SCENARIO_IDS = tuple(SCENARIO_TITLES.keys())


@dataclass(frozen=True, slots=True)
class AnnotationPair:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class VisitorSignalAssessment:
    """Shared outcome for scenario API and formal evaluate (pack v2+)."""

    transfer_polarity: Polarity
    transfer_triggers_affirmative: frozenset[str]
    transfer_triggers_negative: frozenset[str]
    transfer_triggers_unknown: frozenset[str]
    student_funds_hits: frozenset[str]

    @property
    def transfer_human_required(self) -> bool:
        return self.transfer_polarity == "affirmative"

    @property
    def student_funds_rejected(self) -> bool:
        return bool(self.student_funds_hits)


@dataclass(frozen=True, slots=True)
class VisitorScenarioResult:
    scenario_id: str
    title: str
    transfer_human_required: bool
    transfer_polarity: Polarity
    student_funds_rejected: bool
    student_funds_hits: tuple[str, ...]
    notes: tuple[str, ...]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def polarity_of(value: str) -> Polarity:
    v = _norm(value)
    if not v:
        return "unknown"
    if v in _AFFIRMATIVE:
        return "affirmative"
    if v in _NEGATIVE:
        return "negative"
    return "unknown"


def is_transfer_key(key: str) -> bool:
    k = _norm(key)
    return k in TRANSFER_TRIGGER_KEYS or k.startswith("transfer_")


def is_funds_related_key(key: str) -> bool:
    k = _norm(key)
    return any(h in k for h in _FUNDS_KEY_HINTS)


def normalize_annotations(
    *,
    annotations: Iterable[AnnotationPair] | None = None,
    annotation_keys: Sequence[str] | None = None,
    annotation_values: Sequence[str] | None = None,
) -> tuple[AnnotationPair, ...]:
    """Prefer key/value pairs; legacy parallel lists zip by absolute index.

    Empty keys are kept as placeholders so later values stay aligned
    (e.g. keys=['', 'transfer_minor'] values=['yes', 'no'] → transfer_minor=no).
    """
    pairs: list[AnnotationPair] = []
    if annotations:
        for a in annotations:
            pairs.append(
                AnnotationPair(key=_norm(a.key), value=(a.value or "").strip())
            )
        return tuple(pairs)

    raw_keys = list(annotation_keys or [])
    raw_values = list(annotation_values or [])
    n = max(len(raw_keys), len(raw_values))
    for i in range(n):
        raw_k = raw_keys[i] if i < len(raw_keys) else ""
        raw_v = raw_values[i] if i < len(raw_values) else ""
        key = _norm(raw_k) if raw_k is not None else ""
        value = (raw_v or "").strip()
        if not key and not value:
            continue
        if not key:
            pairs.append(AnnotationPair(key="funds_note", value=value))
        else:
            pairs.append(AnnotationPair(key=key, value=value))
    return tuple(pairs)


def _split_clauses(text: str) -> list[str]:
    # Keep English sentence boundaries (". ") without breaking decimals.
    parts = re.split(r"[。；;\n]+|(?<=\.)\s+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _key_is_formula_token(key: str) -> bool:
    k = _norm(key)
    if _FORMULA_PHRASE.search(k):
        return True
    return k in {
        "student_fixed_funds",
        "cas_funds",
        "student_maintenance",
        "ukvi_student_funds_formula",
        "cas",
    }


def _formula_hit_label(key: str, value: str) -> str:
    phrase = _FORMULA_PHRASE.search(value) or _FORMULA_PHRASE.search(key)
    if phrase:
        return _norm(phrase.group(0)).replace(" ", "_").replace("-", "_")
    if _key_is_formula_token(key):
        return _norm(key)
    if value.strip() and _BARE_CAS.match(value.strip()):
        return "cas"
    return "student_funds_formula"


def _clause_formula_stance(clause: str) -> Literal["adopt", "negate", "none"]:
    """Judge one clause: negation must target the formula, not 'another method'."""
    m = _FORMULA_PHRASE.search(clause)
    bare = _BARE_CAS.search(clause.strip()) if clause.strip() else None
    if not m and not bare:
        return "none"
    start = m.start() if m else int(bare.start())  # type: ignore[union-attr]
    window = clause[max(0, start - 96) : start]
    neg = _FORMULA_NEGATION.search(window)
    if neg:
        between = window[neg.end() :]
        # "did not use another/other estimation ... student_fixed_funds" → adopt.
        if re.search(r"\b(another|other|else|其他|别的|不同)\b", between, re.I):
            return "adopt"
        return "negate"
    return "adopt"


def _formula_stance(key: str, value: str) -> Literal["adopt", "negate", "irrelevant"]:
    """Per-field / per-clause: adopt vs negate vs irrelevant mention."""
    funds_ctx = is_funds_related_key(key) or _key_is_formula_token(key)

    # Field named after the formula: value polarity decides (cas=yes / =no).
    if _key_is_formula_token(key):
        pol = polarity_of(value)
        if pol == "negative":
            return "negate"
        if pol == "affirmative" or not value.strip():
            return "adopt"
        # Free-text value on a formula-named field: fall through to clause scan.
        funds_ctx = True

    if not funds_ctx:
        return "irrelevant"

    if value.strip() and _BARE_CAS.match(value.strip()):
        return "adopt"

    clauses = _split_clauses(value) or ([value.strip()] if value.strip() else [])
    saw_adopt = False
    saw_negate = False
    for clause in clauses:
        stance = _clause_formula_stance(clause)
        if stance == "adopt":
            saw_adopt = True
        elif stance == "negate":
            saw_negate = True
    if saw_adopt:
        return "adopt"
    if saw_negate:
        return "negate"
    return "irrelevant"


def assess_visitor_signals(
    *,
    classifications: Iterable[str] | None = None,
    annotations: Iterable[AnnotationPair] | None = None,
    annotation_keys: Sequence[str] | None = None,
    annotation_values: Sequence[str] | None = None,
) -> VisitorSignalAssessment:
    pairs = normalize_annotations(
        annotations=annotations,
        annotation_keys=annotation_keys,
        annotation_values=annotation_values,
    )
    aff: set[str] = set()
    neg: set[str] = set()
    unk: set[str] = set()
    overall_negative = False

    for pair in pairs:
        if not is_transfer_key(pair.key):
            continue
        pol = polarity_of(pair.value)
        if pair.key in {"transfer_human_triggers", "transfer_assessment"}:
            if pol == "affirmative":
                aff.add(pair.key)
            elif pol == "negative":
                overall_negative = True
                neg.add(pair.key)
            else:
                unk.add(pair.key)
            continue
        if pol == "affirmative":
            aff.add(pair.key)
        elif pol == "negative":
            neg.add(pair.key)
        else:
            unk.add(pair.key)

    # Unassessed required checks remain unknown unless overall conclusion clears.
    unassessed = TRANSFER_CHECK_KEYS - aff - neg

    if aff:
        transfer_polarity: Polarity = "affirmative"
    elif overall_negative or (
        TRANSFER_CHECK_KEYS and TRANSFER_CHECK_KEYS <= neg and not unassessed
    ):
        transfer_polarity = "negative"
    else:
        # Partial negatives (e.g. only transfer_minor=no) keep unknown.
        transfer_polarity = "unknown"
        unk |= unassessed

    hits: set[str] = set()
    for pair in pairs:
        stance = _formula_stance(pair.key, pair.value)
        if stance == "adopt":
            hits.add(_formula_hit_label(pair.key, pair.value))

    for c in classifications or []:
        c_n = _norm(c)
        # Classifications are funds-context only when the label itself is a formula token.
        if _FORMULA_PHRASE.search(c_n) or c_n in {
            "student_fixed_funds",
            "cas_funds",
            "student_maintenance",
            "ukvi_student_funds_formula",
        }:
            hits.add(c_n)

    return VisitorSignalAssessment(
        transfer_polarity=transfer_polarity,
        transfer_triggers_affirmative=frozenset(aff),
        transfer_triggers_negative=frozenset(neg),
        transfer_triggers_unknown=frozenset(unk),
        student_funds_hits=frozenset(hits),
    )


def run_visitor_scenario(
    *,
    scenario_id: str,
    classifications: Iterable[str] | None = None,
    annotation_keys: Sequence[str] | None = None,
    annotation_values: Sequence[str] | None = None,
    annotations: Iterable[AnnotationPair] | None = None,
) -> VisitorScenarioResult:
    """Scenario id is a label only — outcomes come from shared signal assessment."""
    assessment = assess_visitor_signals(
        classifications=classifications,
        annotations=annotations,
        annotation_keys=annotation_keys,
        annotation_values=annotation_values,
    )
    title = SCENARIO_TITLES.get(scenario_id, "unknown scenario")
    notes: list[str] = []
    if scenario_id not in SCENARIO_TITLES:
        notes.append("unknown scenario_id")
    if scenario_id == "standard_adult_tourist" and assessment.transfer_human_required:
        notes.append("inputs raised transfer triggers (not a clean standard path)")
    if scenario_id == "transfer_minor_or_complex" and not assessment.transfer_human_required:
        notes.append("expected affirmative transfer_* annotation for this demo label")
    if scenario_id == "counter_student_funds" and not assessment.student_funds_rejected:
        notes.append("expected Student funds formula adoption in funds-related fields")
    if assessment.transfer_polarity == "unknown" and assessment.transfer_triggers_negative:
        notes.append(
            "partial transfer negatives recorded; remaining checks still unknown"
        )
    return VisitorScenarioResult(
        scenario_id=scenario_id,
        title=title,
        transfer_human_required=assessment.transfer_human_required,
        transfer_polarity=assessment.transfer_polarity,
        student_funds_rejected=assessment.student_funds_rejected,
        student_funds_hits=tuple(sorted(assessment.student_funds_hits)),
        notes=tuple(notes),
    )


def detect_transfer_triggers(annotation_keys: set[str]) -> frozenset[str]:
    """Legacy: key presence only (v1). Prefer assess_visitor_signals."""
    hits = set()
    for key in annotation_keys:
        if is_transfer_key(key):
            hits.add(_norm(key))
    return frozenset(hits)


def detect_forbidden_student_funds(
    *,
    classifications: set[str],
    annotation_keys: set[str],
    annotation_values: set[str],
) -> frozenset[str]:
    """Legacy bag-of-tokens helper — prefer assess_visitor_signals."""
    return assess_visitor_signals(
        classifications=list(classifications),
        annotation_keys=list(annotation_keys),
        annotation_values=list(annotation_values),
    ).student_funds_hits
