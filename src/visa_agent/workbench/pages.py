"""Workbench HTML — presentation only; forms keep existing endpoints."""

from __future__ import annotations

import uuid
from html import escape
from urllib.parse import urlencode

from visa_agent.case_service.advisor_actions import CustomerMail, takeover_reply_subject
from visa_agent.case_service.remediation import AdvisorAlert, UnassignedMail
from visa_agent.case_service.service import FINISHED_CASE_STATUSES, CaseRecord, ReviewBoardSnapshot
from visa_agent.document_pipeline.classify import requirement_for_classification
from visa_agent.domain.enums import CaseStatus, RouteCode
from visa_agent.email_adapter.allowlist import normalize_email
from visa_agent.rules.seed import (
    GRADUATE_REQUIREMENTS,
    SKILLED_WORKER_REQUIREMENTS,
    STUDENT_REQUIREMENTS,
    VISITOR_REQUIREMENTS,
)
from visa_agent.rules.service import CHECKLIST_READ_ONLY_STATUSES, can_judge_applicability

# List page route labels (homepage polish deferred).
_ROUTE_OPTIONS: tuple[tuple[str, str], ...] = (
    (RouteCode.STANDARD_VISITOR.value, "访问签证"),
    (RouteCode.STUDENT.value, "学生签证"),
    (RouteCode.SKILLED_WORKER.value, "技术工签"),
    (RouteCode.GRADUATE.value, "毕业生签证"),
)

_ROUTE_ZH: dict[str, str] = dict(_ROUTE_OPTIONS)

# Plain-language case status for operators (not internal enum names).
_STATUS_ZH: dict[str, str] = {
    "lead": "刚开案",
    "intake_authorization": "待接案",
    "route_scope_confirmed": "已定签证类型",
    "collect_and_verify": "收材料中",
    "issue_resolution": "有问题待处理",
    "draft_preparation": "准备材料确认",
    "customer_confirmation": "材料确认中",
    "final_review": "待终审",
    "ready_to_deliver": "交付中",
    "sent_awaiting_ack": "已发出，等回执",
    "delivered": "已办完",
    "closed": "案件已结束",
}

_CASE_STAGE_FILTERS = {
    "lead": _STATUS_ZH["lead"],
    "collect_and_verify": _STATUS_ZH["collect_and_verify"],
    "customer_confirmation": _STATUS_ZH["customer_confirmation"],
    "final_review": _STATUS_ZH["final_review"],
    "ready_to_deliver": _STATUS_ZH["ready_to_deliver"],
    "closed": _STATUS_ZH["closed"],
    "terminated": "案件已终止",
}

_REQ_TITLE_ZH: dict[str, str] = {
    "passport_valid": "护照",
    "visit_purpose_allowed": "访问目的",
    "intention_to_leave": "离境意向 / 行程",
    "funds_for_stay": "资金证明",
    "home_ties": "国内约束力证明",
    "sponsor_if_any": "担保人材料（如有）",
    "translation_if_needed": "翻译件（如需要）",
    "advisor_unclassified": "材料分类（需顾问）",
    "advisor_customer_question": "客户提问（需顾问）",
    "cas_confirmation": "CAS（录取确认）",
    "funds_maintenance": "学费 / 生活费资金",
    "english_language": "英语能力证明",
    "course_offer": "课程录取材料",
    "tuberculosis_test_if_required": "TB 体检（如适用）",
    "certificate_of_sponsorship": "担保证书 CoS",
    "job_skill_level": "职位 / 技能等级",
    "salary_threshold": "薪资达标证明",
    "uk_study_completed": "英国学业已完成",
    "qualification_confirmed": "学历 / 学位确认",
    "current_permission_basis": "当前在英身份依据",
}

_REQ_STATUS_ZH: dict[str, tuple[str, str]] = {
    # status -> (label, badge class)
    "applicable_missing": ("还缺", "badge-bad"),
    "received_pending_review": ("待核对", "badge-warn"),
    "evidence_accepted": ("已核对", "badge-ok"),
    "needs_correction": ("需处理", "badge-bad"),
    "not_applicable": ("不适用", "badge-info"),
    "applicability_unknown": ("待判断", "badge-warn"),
    "exempted_with_basis": ("已豁免", "badge-ok"),
}

_SEED_BY_ROUTE: dict[str, tuple] = {
    RouteCode.STANDARD_VISITOR.value: VISITOR_REQUIREMENTS,
    RouteCode.STUDENT.value: STUDENT_REQUIREMENTS,
    RouteCode.SKILLED_WORKER.value: SKILLED_WORKER_REQUIREMENTS,
    RouteCode.GRADUATE.value: GRADUATE_REQUIREMENTS,
}

_CLASS_TO_REQ: dict[str, str] = {
    "passport": "passport_valid",
    "bank": "funds_for_stay",
    "funds": "funds_for_stay",
    "sponsor": "sponsor_if_any",
    "translation": "translation_if_needed",
    "translation_draft": "translation_if_needed",
    "itinerary": "intention_to_leave",
    "employment": "home_ties",
    "purpose": "visit_purpose_allowed",
    "cas": "cas_confirmation",
    "maintenance": "funds_maintenance",
    "course": "course_offer",
    "offer": "course_offer",
    "cos": "certificate_of_sponsorship",
    "sponsorship": "certificate_of_sponsorship",
    "salary": "salary_threshold",
    "skill": "job_skill_level",
    "english": "english_language",
    "tb": "tuberculosis_test_if_required",
    "tuberculosis": "tuberculosis_test_if_required",
    "degree": "qualification_confirmed",
    "qualification": "qualification_confirmed",
    "graduation": "uk_study_completed",
    "uk_study": "uk_study_completed",
    "permission": "current_permission_basis",
}

_REQ_TO_CLASS: dict[str, str] = {
    "passport_valid": "passport",
    "visit_purpose_allowed": "purpose",
    "intention_to_leave": "itinerary",
    "funds_for_stay": "bank",
    "home_ties": "employment",
    "sponsor_if_any": "sponsor",
    "translation_if_needed": "translation",
    "cas_confirmation": "cas",
    "funds_maintenance": "maintenance",
    "english_language": "english",
    "course_offer": "course",
    "tuberculosis_test_if_required": "tb",
    "certificate_of_sponsorship": "cos",
    "job_skill_level": "skill",
    "salary_threshold": "salary",
    "uk_study_completed": "graduation",
    "qualification_confirmed": "degree",
    "current_permission_basis": "permission",
}

_FILENAME_HINTS: tuple[tuple[str, str], ...] = (
    ("passport", "passport_valid"),
    ("bank", "funds_for_stay"),
    ("statement", "funds_for_stay"),
    ("funds", "funds_for_stay"),
    ("sponsor", "sponsor_if_any"),
    ("cas", "cas_confirmation"),
    ("cos", "certificate_of_sponsorship"),
    ("english", "english_language"),
    ("ielts", "english_language"),
    ("tb", "tuberculosis_test_if_required"),
    ("tuberculosis", "tuberculosis_test_if_required"),
    ("degree", "qualification_confirmed"),
    ("graduation", "uk_study_completed"),
)

def _flags(case: CaseRecord) -> list[str]:
    bits: list[str] = []
    if case.human_takeover:
        bits.append("人工接管中")
    if case.waiting_customer:
        bits.append("等客户")
    if case.waiting_third_party:
        bits.append("等第三方")
    if case.system_blocked:
        bits.append("被卡住了")
    if case.paused:
        bits.append("已暂停")
    if case.cancelled:
        bits.append("已取消")
    return bits


def _status_zh(value: str) -> str:
    return _STATUS_ZH.get(value, value)


def _case_status_text(case: CaseRecord, *, terminated: bool = False) -> str:
    if case.status == CaseStatus.CLOSED and terminated:
        return "案件已终止"
    if case.status == CaseStatus.SENT_AWAITING_ACK and not case.current_delivery_id:
        return "待核对发送记录"
    return _status_zh(case.status.value)


def _route_zh(case: CaseRecord) -> str:
    if not case.route:
        return "还没选签证类型"
    return _ROUTE_ZH.get(case.route.value, case.route.value)


def _req_title(key: str, titles: dict[str, str] | None = None) -> str:
    if titles and key in titles:
        return titles[key]
    return _REQ_TITLE_ZH.get(key, key)


def _version_field(case: CaseRecord) -> str:
    return (
        f'<input type="hidden" name="expected_case_version" '
        f'value="{case.case_version}">'
    )


def _shell_css() -> str:
    return """
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
:root{
  --color-primary:#0D9488;
  --color-primary-hover:#0F766E;
  --color-on-primary:#FFFFFF;
  --color-secondary:#2DD4BF;
  --color-accent:#D97706;
  --color-on-accent:#FFFFFF;
  --color-background:#F0FDFA;
  --color-foreground:#134E4A;
  --color-card:#FFFFFF;
  --color-muted:#E8F1F4;
  --color-muted-fg:#475569;
  --color-border:#99F6E4;
  --color-border-strong:#5EEAD4;
  --color-destructive:#DC2626;
  --color-success:#059669;
  --color-warning:#B45309;
  --table-font-size:.9rem;
  --radius:12px;
  --shadow:0 1px 2px rgba(19,78,74,.06),0 8px 24px rgba(13,148,136,.06);
  --font: 'Plus Jakarta Sans', system-ui, sans-serif;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;min-height:100vh;
  font-family:var(--font);color:var(--color-foreground);
  background:
    radial-gradient(1200px 480px at 10% -10%, rgba(45,212,191,.22), transparent 55%),
    radial-gradient(900px 420px at 100% 0%, rgba(217,119,6,.10), transparent 50%),
    var(--color-background);
  line-height:1.5;
}
a{color:var(--color-primary);text-decoration:none;cursor:pointer}
a:hover{color:var(--color-primary-hover);text-decoration:underline}
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{
  outline:2px solid var(--color-primary);outline-offset:2px
}
.wrap{max-width:820px;margin:0 auto;padding:1.25rem 1.25rem 3rem}
.topbar{
  display:flex;align-items:center;justify-content:space-between;gap:1rem;
  margin-bottom:1.25rem;flex-wrap:wrap
}
.brand{display:flex;flex-direction:column;gap:.15rem}
.brand-mark{
  font-weight:800;font-size:1.35rem;letter-spacing:-.02em;color:var(--color-foreground)
}
.brand-sub{font-size:.95rem;color:var(--color-muted-fg)}
.top-links{display:flex;gap:.75rem;align-items:center;font-size:.9rem;font-weight:600}
.inbox-link{display:inline-flex;align-items:center;gap:.2rem}
.inbox-count{
  display:inline-flex;align-items:center;justify-content:center;align-self:flex-start;
  min-width:1.1rem;height:1.1rem;padding:0 .3rem;border-radius:999px;
  background:var(--color-destructive);color:#fff;font-size:.68rem;font-weight:700;
  font-variant-numeric:tabular-nums;transform:translateY(-.35rem)
}
.panel{
  background:var(--color-card);border:1px solid var(--color-border);
  border-radius:var(--radius);box-shadow:var(--shadow);padding:1.15rem 1.25rem;
  margin-bottom:1rem
}
.panel h2,.panel h3{
  margin:0 0 .65rem;font-size:1.05rem;font-weight:700;letter-spacing:-.01em
}
.panel-head{
  display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;
  margin-bottom:.85rem;flex-wrap:wrap
}
.case-list-heading{
  display:flex;align-items:center;justify-content:space-between;gap:1rem;
  margin-bottom:.85rem
}
.case-list-heading h2{margin:0}
.case-pagination{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:.5rem;
  padding:.6rem 1.25rem;font-size:calc(var(--table-font-size) * 2 / 3);
}
.case-page-jump{display:flex;align-items:center;gap:.4rem}
.case-pagination .btn{
  min-height:1.5rem;padding:.2rem .55rem;border-radius:6px;font-size:inherit
}
.case-search{
  display:grid;grid-template-columns:minmax(0,1fr) auto;gap:.55rem;margin-bottom:.85rem
}
.case-search-field{position:relative}
.case-search-field input{
  width:100%;height:100%;font:inherit;font-size:.9rem;color:var(--color-foreground);
  padding:.55rem 2.5rem .55rem .7rem;background:var(--color-card);
  border:1px solid var(--color-border-strong);border-radius:10px
}
.case-search-submit{
  position:absolute;inset:1px 1px 1px auto;width:2.35rem;
  display:grid;place-items:center;padding:0;border:0;border-radius:0 9px 9px 0;
  background:none;color:var(--color-muted-fg);cursor:pointer
}
.case-search-submit:hover{background:var(--color-muted);color:var(--color-primary-hover)}
.case-search-icon{
  position:relative;width:.75rem;height:.75rem;border:1.5px solid currentColor;
  border-radius:50%;margin:0 .15rem .15rem 0
}
.case-search-icon::after{
  content:'';position:absolute;top:100%;left:85%;width:.35rem;height:1.5px;
  border-radius:1px;background:currentColor;transform:rotate(45deg);transform-origin:left
}
.text-action{
  border:0;background:none;padding:.25rem 0;font:inherit;font-size:.9rem;
  font-weight:600;color:var(--color-primary-hover);cursor:pointer
}
.text-action:hover{text-decoration:underline}
.inbox-row-action{white-space:nowrap}
.case-dialog{
  width:min(28rem,calc(100% - 2rem));max-height:calc(100% - 2rem);
  padding:clamp(1.25rem,5vw,1.75rem);border:1px solid var(--color-border);
  border-radius:var(--radius);background:var(--color-card);color:var(--color-foreground);
  box-shadow:0 16px 48px rgba(19,78,74,.18)
}
.case-dialog::backdrop{background:rgba(15,23,42,.38)}
.case-dialog h2{margin:0 0 1rem;font-size:1.2rem}
.case-dialog form{display:grid;gap:.9rem}
.case-dialog-actions{
  display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:1rem;
  margin-top:.35rem
}
.case-dialog-actions .clear-filters{margin-right:auto}
.flash{
  background:#ECFDF5;border:1px solid #A7F3D0;color:#065F46;
  border-radius:var(--radius);padding:.75rem 1rem;margin-bottom:1rem;font-weight:600
}
.flash-error{background:#FEF2F2;border-color:#FECACA;color:#991B1B}
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:.35rem;
  font:inherit;font-weight:600;font-size:.9rem;border-radius:10px;
  border:1px solid transparent;padding:.55rem .95rem;cursor:pointer;
  transition:background .15s ease,border-color .15s ease,color .15s ease,opacity .15s ease
}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--color-primary);color:var(--color-on-primary)}
.btn-primary:hover{background:var(--color-primary-hover)}
#create-case-submit:disabled{
  background:var(--color-muted);color:var(--color-muted-fg);opacity:1
}
.create-case-spinner{
  display:none;width:1em;height:1em;flex:none;
  border:2px solid currentColor;border-right-color:transparent;border-radius:50%
}
#create-case-dialog form[aria-busy="true"] .create-case-spinner{
  display:inline-block;animation:create-case-spin .7s linear infinite
}
@keyframes create-case-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){
  #create-case-dialog form[aria-busy="true"] .create-case-spinner{animation:none}
}
.btn-accent{background:var(--color-accent);color:var(--color-on-accent)}
.btn-accent:hover{background:#B45309}
.btn-ghost{
  background:transparent;border-color:var(--color-border-strong);color:var(--color-foreground)
}
.btn-ghost:hover{background:var(--color-muted)}
.btn-danger{
  background:transparent;border-color:#FECACA;color:var(--color-destructive)
}
.btn-danger:hover{background:#FEF2F2}
.btn-sm{padding:.4rem .7rem;font-size:.82rem}
.btn-lg{padding:.7rem 1.15rem;font-size:1rem}
.field{
  display:flex;flex-direction:column;gap:.3rem;min-width:min(100%,14rem);flex:1
}
.field label{font-size:.78rem;font-weight:600;color:var(--color-muted-fg)}
.field input,.field select,textarea,select,input[type=text],input[type=email],#case-page-number{
  font:inherit;padding:.55rem .7rem;border:1px solid var(--color-border-strong);
  border-radius:10px;background:#fff;color:var(--color-foreground);width:100%
}
#case-page-number{width:3.5rem;min-height:1.5rem;padding:.2rem .35rem;border-radius:6px}
.row{display:flex;gap:.65rem;flex-wrap:wrap;align-items:flex-end}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:var(--table-font-size)}
th,td{padding:.7rem .8rem;text-align:left;border-bottom:1px solid var(--color-muted)}
th{
  font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
  color:var(--color-muted-fg);font-weight:700;background:var(--color-muted)
}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:rgba(13,148,136,.04)}
.badge{
  display:inline-flex;align-items:center;gap:.25rem;font-size:.75rem;font-weight:700;
  padding:.2rem .55rem;border-radius:999px;background:var(--color-muted);color:var(--color-foreground)
}
.badge-ok{background:#D1FAE5;color:#065F46}
.badge-warn{background:#FEF3C7;color:#92400E}
.badge-bad{background:#FEE2E2;color:#991B1B}
.badge-info{background:#CCFBF1;color:#115E59}
.muted{color:var(--color-muted-fg)}
.hero{
  background:linear-gradient(135deg,#CCFBF1 0%,#FFFFFF 60%);
  border:1px solid var(--color-border-strong);border-radius:var(--radius);
  padding:1.25rem 1.35rem;margin-bottom:1rem
}
.hero h1{margin:0 0 .35rem;font-size:1.45rem;font-weight:800;letter-spacing:-.02em}
.hero-meta{display:flex;flex-wrap:wrap;gap:.5rem .85rem;margin-top:.75rem;font-size:.95rem}
.hero-meta strong{font-weight:700}
.todo{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.55rem}
.todo li{
  display:flex;align-items:flex-start;justify-content:space-between;gap:.75rem;
  padding:.75rem .85rem;border-radius:10px;background:var(--color-muted);
  border:1px solid transparent
}
.todo li.need{background:#FEF2F2;border-color:#FECACA}
.todo li.wait{background:#FEF3C7;border-color:#FDE68A}
.todo li.ok{background:#ECFDF5;border-color:#A7F3D0}
.todo .todo-main{display:flex;flex-direction:column;gap:.15rem;min-width:0}
.todo .todo-title{font-weight:700}
.todo .todo-note{font-size:.85rem;color:var(--color-muted-fg)}
.file-list{list-style:none;margin:.45rem 0 0;padding:0;display:flex;flex-direction:column;gap:.35rem}
.file-list li{
  display:flex;justify-content:space-between;gap:.75rem;align-items:center;
  padding:.45rem .65rem;border-radius:8px;background:#F5FAF9;
  flex-wrap:wrap;font-size:.85rem
}
.file-list .name{font-weight:600;flex:1;min-width:min(11rem,100%);overflow-wrap:anywhere}
.file-list a{font-weight:600;color:var(--color-primary-hover)}
.file-list .flag-list{align-items:center;gap:.6rem}
.file-delete{color:var(--color-destructive);font-size:inherit}
.file-delete-dialog p{margin:0;color:var(--color-muted-fg);overflow-wrap:anywhere}
.btn.file-delete-confirm{background:var(--color-destructive);color:#fff}
.btn.file-delete-confirm:hover{background:#B91C1C}
.materials-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--color-muted)}
.material-item{padding:.7rem 0;border-bottom:1px solid var(--color-muted)}
.material-item:last-child{border-bottom:0;padding-bottom:.15rem}
.material-heading{
  display:grid;grid-template-columns:minmax(0,1fr) max-content 2.75rem;
  align-items:center;column-gap:.65rem
}
.material-title{font-weight:600;font-size:.95rem;overflow-wrap:anywhere}
.material-status{justify-self:end}
.material-actions{display:flex;flex-direction:column;align-items:flex-end}
.material-review-files{margin:0;padding-left:1.25rem;overflow-wrap:anywhere}
.material-note{margin:.25rem 0 0;font-size:.82rem;color:var(--color-muted-fg);overflow-wrap:anywhere}
.check-row{display:flex;flex-direction:column;gap:.35rem;width:100%}
.check-top{display:flex;justify-content:space-between;gap:.75rem;align-items:flex-start;flex-wrap:wrap}
.reference{font-size:.82rem}
.reference>summary{cursor:pointer;color:var(--color-muted-fg)}
.case-email{display:block;overflow-wrap:anywhere}
.preview-frame{
  width:100%;min-height:70vh;border:1px solid var(--color-border);
  border-radius:10px;background:#fff
}
.preview-img{max-width:100%;height:auto;border-radius:10px;border:1px solid var(--color-border)}
.case-heading h1{margin:0;overflow-wrap:anywhere}
.case-heading form{flex-shrink:0}
.customer-reply{height:clamp(25rem,72vh,36rem);display:flex;flex-direction:column;gap:.75rem}
.customer-reply .case-list-heading{flex:none;flex-wrap:wrap;gap:.2rem .85rem;margin:0}
.customer-reply .case-email{font-size:.85rem}
.customer-reply form{display:grid;gap:.55rem;flex:none}
.customer-reply .flash{margin:0}
.customer-compose-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:.55rem}
.customer-compose-row textarea{min-height:0;resize:none}
.customer-history-list{
  display:flex;flex:1;flex-direction:column;gap:.75rem;min-height:0;overflow:auto;
  padding:.75rem .35rem .75rem 0;border-block:1px solid var(--color-muted)
}
.customer-message{
  flex:none;align-self:flex-start;max-width:88%;min-width:0;padding:.65rem .85rem;
  border-radius:12px 12px 12px 4px;background:#F1F5F9;overflow-wrap:anywhere
}
.customer-message.outbound{
  align-self:flex-end;border-radius:12px 12px 4px 12px;background:#CCFBF1
}
.customer-message-meta{
  display:flex;justify-content:space-between;flex-wrap:wrap;gap:.2rem .9rem;
  color:var(--color-muted-fg);font-size:.78rem
}
.customer-message-subject{margin:.35rem 0 0;font-size:.85rem;font-weight:600}
.customer-message-body{margin:.3rem 0 0;font-size:.9rem;white-space:pre-wrap}
.action-row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:.75rem}
.pre{
  white-space:pre-wrap;overflow-wrap:anywhere;background:var(--color-muted);border-radius:10px;
  padding:.85rem;border:1px solid var(--color-border);font-size:.9rem;margin:.5rem 0 0
}
.empty{color:var(--color-muted-fg);font-size:.92rem;margin:.25rem 0}
.case-link{font-weight:700}
.flag-list{display:flex;flex-wrap:wrap;gap:.3rem}
@media (max-width:640px){
  .wrap{padding:1rem 1rem 2.5rem}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_shell_css()}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
<script>
  const flashUrl = new URL(window.location.href);
  document.querySelectorAll('.flash[data-flash-param]:not(.flash-error)').forEach(message => {{
    const param = message.dataset.flashParam;
    if (flashUrl.searchParams.has(param)) flashUrl.searchParams.delete(param);
    window.setTimeout(() => message.remove(), 3000);
  }});
  if (flashUrl.href !== window.location.href) {{
    window.history.replaceState(window.history.state, '', flashUrl);
  }}
  document.querySelectorAll('dialog').forEach(dialog => {{
    const isBackdrop = event => {{
      if (!dialog.open || event.target !== dialog) return false;
      const {{left, right, top, bottom}} = dialog.getBoundingClientRect();
      return event.clientX < left || event.clientX >= right || event.clientY < top || event.clientY >= bottom;
    }};
    let startedOnBackdrop = false;
    dialog.addEventListener('pointerdown', event => {{
      startedOnBackdrop = event.isPrimary && event.button === 0 && isBackdrop(event);
    }});
    dialog.addEventListener('click', event => {{
      if (startedOnBackdrop && isBackdrop(event)) dialog.close();
      startedOnBackdrop = false;
    }});
  }});
</script>
</body>
</html>"""


def _alert_kind_zh(kind: str, reason_code: str) -> str:
    if kind == "human_takeover" or reason_code == "human_takeover":
        return "人工接管中"
    if reason_code == "customer_question_needs_advisor":
        return "客户提问需顾问"
    if reason_code in {"unclear_classification", "advisor_unclassified"}:
        return "材料需人工分类"
    if kind == "escalate_question":
        return "需人工处理"
    return kind or "提醒"


def _nav(*, alert_count: int = 0, active: str = "home") -> str:
    count_badge = ""
    inbox_label = ""
    if alert_count > 0:
        count_badge = f'<span class="inbox-count" aria-hidden="true">{alert_count}</span>'
        inbox_label = f' aria-label="收件箱，{alert_count} 项待处理"'
    home_cls = " aria-current='page'" if active == "home" else ""
    inbox_cls = " aria-current='page'" if active == "inbox" else ""
    return f"""
<header class="topbar">
  <div class="brand-mark">英国签证助手工作台</div>
  <nav class="top-links">
    <a href="/workbench"{home_cls}>案件列表</a>
    <a class="inbox-link" href="/workbench/inbox"{inbox_cls}{inbox_label}>收件箱{count_badge}</a>
  </nav>
</header>
"""


def render_inbox(
    alerts: list[AdvisorAlert] | None = None,
    *,
    message: str | None = None,
    unassigned_mail: list[UnassignedMail] | None = None,
    pending_count: int = 0,
) -> str:
    """Standalone advisor reminder page — not mixed into the case list."""
    alerts = list(alerts or [])
    rows: list[str] = []
    for a in alerts:
        case_id = escape(a.case_id)
        title = escape(a.case_title or "未命名案件")
        detail = escape(a.reason_detail)
        kind = _alert_kind_zh(a.alert_kind, a.reason_code)
        email = escape(a.customer_email or "—")
        target = "advisor-work" if a.question_id else "customer-reply"
        rows.append(
            "<tr>"
            f"<td><span class='badge badge-bad'>{escape(kind)}</span></td>"
            f"<td><strong>{title}</strong>"
            f"<small class='muted case-email'>{email}</small>"
            f"<div class='muted' style='font-size:.85rem;margin-top:.25rem'>{detail}</div></td>"
            f"<td class='inbox-row-action'><a class='case-link' href='/workbench/cases/{case_id}#{target}'>处理</a></td>"
            "</tr>"
        )
    body = "\n".join(rows) or (
        "<tr><td colspan='3'><p class='empty'>暂无顾问待办</p></td></tr>"
    )
    unassigned = ""
    if unassigned_mail:
        mail_rows: list[str] = []
        for mail in unassigned_mail:
            mail_rows.append(
                f"<tr><td>{escape(mail.sender or '—')}</td>"
                f"<td>{escape(mail.subject or '（无主题）')}"
                "<details class='reference'><summary>排查编号</summary>"
                f"<code>{escape(mail.id)}</code></details></td>"
                f"<td class='inbox-row-action'><a class='case-link' href='/workbench/inbox/mail/{escape(mail.id)}'>处理</a></td></tr>"
            )
        unassigned = f"""
<section class="panel">
  <h2>待核对归属的来信</h2>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>发件人</th><th>主题</th><th>操作</th></tr></thead>
      <tbody>{''.join(mail_rows)}</tbody>
    </table>
  </div>
</section>
"""
    flash = _flash(message) if message else ""
    content = f"""
{_nav(alert_count=pending_count, active="inbox")}
{flash}
<section class="panel" style="padding:0;overflow:hidden;border-color:#FECACA">
  <div class="case-list-heading" style="padding:1rem 1.25rem 0">
    <h2>顾问收件箱</h2>
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr><th>待办</th><th>案件与原因</th><th>操作</th></tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
  </div>
</section>
{unassigned}
"""
    return _page("工作台 · 收件箱", content)


def render_unassigned_mail(
    mail: UnassignedMail, cases: list[CaseRecord], *, q: str, pending_count: int,
    message: str | None = None,
) -> str:
    path = f"/workbench/inbox/mail/{escape(mail.id)}"
    assign = '<p class="empty">没有符合条件的办理中案件</p>'
    if cases:
        choices = "".join(
            f'<option value="{escape(case.id)}">{escape(case.title or "未命名案件")}'
            f'{escape(" · " + case.customer_email) if case.customer_email else ""}</option>'
            for case in cases
        )
        assign = f"""
<form class="row action-row" method="post" action="{path}">
  <div class="field">
    <label for="mail-case">归到案件</label>
    <select id="mail-case" name="case_id" required>
      <option value="">选择案件</option>{choices}
    </select>
  </div>
  <button class="btn btn-primary" type="submit">确认归属</button>
</form>"""
    content = f"""
{_nav(alert_count=pending_count, active="inbox")}
{_flash(message)}
<section class="panel">
  <h2>{escape(mail.subject or '（无主题）')}</h2>
  <p class="muted case-email">发件人 {escape(mail.sender or '—')}</p>
  <pre class="pre">{escape(mail.body_text or '（无正文）')}</pre>
</section>
<section class="panel">
  <form class="row" method="get" action="{path}">
    <div class="field">
      <label for="mail-case-search">查找案件</label>
      <input id="mail-case-search" name="q" value="{escape(q)}" placeholder="案件名 / 邮箱 / 案件号">
    </div>
    <button class="btn btn-ghost" type="submit">查找</button>
  </form>
  {assign}
</section>"""
    return _page("工作台 · 处理来信", content)


def render_list(
    cases: list[CaseRecord],
    *,
    alert_count: int = 0,
    filters: dict[str, str] | None = None,
    message: str | None = None,
    terminated_case_ids: set[str] | None = None,
    page: int = 1,
    total_pages: int = 0,
) -> str:
    """Workbench home: create case + searchable case list only."""
    filters = filters or {}
    q = filters.get("q") or ""
    status_f = (filters.get("status") or "").strip()
    route_f = filters.get("route") or ""
    waiting_f = filters.get("waiting") or ""
    filter_count = sum(bool(value) for value in (status_f, route_f, waiting_f))
    filter_label = f"筛选 · {filter_count}" if filter_count else "筛选"

    rows: list[str] = []
    terminated_case_ids = terminated_case_ids or set()
    for c in cases:
        st = c.status.value
        badge_cls = "badge-info"
        if st in {"delivered", "closed"}:
            badge_cls = "badge-ok"
        elif st in {"issue_resolution", "final_review"}:
            badge_cls = "badge-warn"
        rows.append(
            "<tr>"
            f"<td><a class='case-link' href='/workbench/cases/{escape(c.id)}'>"
            f"{escape(c.title or '未命名案件')}</a>"
            f"<small class='muted case-email'>{escape(c.customer_email or '')}</small></td>"
            f"<td>{escape(_route_zh(c))}</td>"
            f"<td><span class='badge {badge_cls}'>{escape(_case_status_text(c, terminated=c.id in terminated_case_ids))}</span></td>"
            "</tr>"
        )
    empty = "没有符合条件的案件" if any((q, status_f, route_f, waiting_f)) else "还没有案件"
    body = "\n".join(rows) or f"<tr><td colspan='3'><p class='empty'>{empty}</p></td></tr>"
    route_opts = "\n".join(
        f'<option value="{escape(code)}"'
        f'{" selected" if code == RouteCode.STANDARD_VISITOR.value else ""}>'
        f"{escape(label)}</option>"
        for code, label in _ROUTE_OPTIONS
    )
    filter_route_opts = (
        '<option value="">全部签证</option>\n'
        + "\n".join(
            f'<option value="{escape(code)}"'
            f'{" selected" if code == route_f else ""}>'
            f"{escape(label)}</option>"
            for code, label in _ROUTE_OPTIONS
        )
    )
    stage_filters = _CASE_STAGE_FILTERS
    if status_f in _STATUS_ZH and status_f not in stage_filters:
        stage_filters = {**stage_filters, status_f: _STATUS_ZH[status_f]}
    status_opts = (
        '<option value="">全部办理阶段</option>\n'
        + "\n".join(
            f'<option value="{escape(code)}"'
            f'{" selected" if code == status_f else ""}>'
            f"{escape(label)}</option>"
            for code, label in stage_filters.items()
        )
    )
    waiting_opts = "\n".join(
        f'<option value="{escape(val)}"'
        f'{" selected" if val == waiting_f else ""}>{escape(lab)}</option>'
        for val, lab in (
            ("", "全部提醒"),
            ("any_flag", "有任何提醒"),
            ("takeover", "人工接管"),
            ("customer", "等客户"),
            ("third_party", "等第三方"),
        )
    )
    flash = _flash(message) if message else ""
    pagination = ""
    if total_pages:
        query = {key: filters[key] for key in ("q", "status", "route", "waiting") if filters.get(key)}
        previous = escape(urlencode({**query, "page": page - 1}))
        following = escape(urlencode({**query, "page": page + 1}))
        previous_link = f'<a class="btn btn-ghost" href="/workbench?{previous}" rel="prev">上一页</a>' if page > 1 else ""
        next_link = f'<a class="btn btn-ghost" href="/workbench?{following}" rel="next">下一页</a>' if page < total_pages else ""
        jump = ""
        if total_pages > 1:
            hidden_fields = "".join(
                f'<input type="hidden" name="{key}" value="{escape(value)}">' for key, value in query.items()
            )
            jump = f"""
<form class="case-page-jump" method="get" action="/workbench">
  {hidden_fields}
  <label for="case-page-number">跳转到</label>
  <input id="case-page-number" type="number" name="page" min="1" max="{total_pages}" required>
  <button class="btn btn-ghost" type="submit">跳转</button>
</form>"""
        pagination = (
            f'<nav class="case-pagination" aria-label="案件分页">'
            f'{previous_link}<span>第 {page} 页 / 共 {total_pages} 页</span>{next_link}{jump}</nav>'
        )
    content = f"""
{_nav(alert_count=alert_count, active="home")}
<section class="panel" style="padding:0;overflow:hidden">
  <div style="padding:1rem 1.25rem 0">
    <div class="case-list-heading">
      <h2>全部案件</h2>
      <button class="text-action" type="button" id="open-create-case"
              aria-haspopup="dialog" aria-controls="create-case-dialog">新建案件</button>
    </div>
    <form method="get" action="/workbench" class="case-search" role="search">
      <div class="case-search-field">
        <input id="q" type="search" name="q" value="{escape(q)}" aria-label="搜索案件"
               placeholder="案件名 / 邮箱 / 案件号">
        <button class="case-search-submit" type="submit" aria-label="搜索">
          <span class="case-search-icon" aria-hidden="true"></span>
        </button>
      </div>
      <input type="hidden" name="status" value="{escape(status_f)}">
      <input type="hidden" name="route" value="{escape(route_f)}">
      <input type="hidden" name="waiting" value="{escape(waiting_f)}">
      <button class="btn btn-ghost" type="button" id="open-case-filters"
              aria-haspopup="dialog" aria-controls="case-filter-dialog">{filter_label}</button>
    </form>
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr>
        <th>客户 / 案件</th><th>签证</th><th>办理阶段</th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
  </div>
  {pagination}
</section>
<dialog class="case-dialog" id="case-filter-dialog" aria-labelledby="case-filter-title">
  <h2 id="case-filter-title">筛选案件</h2>
  <form id="case-filter-form" method="get" action="/workbench">
    <input type="hidden" name="q" value="{escape(q)}">
    <div class="field">
      <label for="status">办理阶段</label>
      <select id="status" name="status" autofocus>{status_opts}</select>
    </div>
    <div class="field">
      <label for="route_filter">签证</label>
      <select id="route_filter" name="route">{filter_route_opts}</select>
    </div>
    <div class="field">
      <label for="waiting">提醒</label>
      <select id="waiting" name="waiting">{waiting_opts}</select>
    </div>
    <div class="case-dialog-actions">
      <button class="text-action clear-filters" type="button" id="clear-case-filters">清空条件</button>
      <button class="text-action" type="button" id="cancel-case-filters">取消</button>
      <button class="btn btn-primary" type="submit">应用筛选</button>
    </div>
  </form>
</dialog>
<dialog class="case-dialog" id="create-case-dialog" aria-labelledby="create-case-title">
  <h2 id="create-case-title">新建案件</h2>
  {flash}
  <form method="post" action="/workbench/cases">
    <div class="field">
      <label for="title">客户 / 案件名</label>
      <input id="title" name="title" placeholder="例如：张三 · 访英旅游" required autofocus>
    </div>
    <div class="field">
      <label for="customer_email">客户邮箱</label>
      <input id="customer_email" name="customer_email" type="email"
             placeholder="client@example.com" required>
    </div>
    <div class="field">
      <label for="route">签证类型</label>
      <select id="route" name="route" required>{route_opts}</select>
    </div>
    <div class="case-dialog-actions">
      <button class="text-action" type="button" id="cancel-create-case">取消</button>
      <button class="btn btn-primary" type="submit" id="create-case-submit">
        <span class="create-case-spinner" aria-hidden="true"></span>新建并发送首封邮件
      </button>
    </div>
  </form>
</dialog>
<script>
  const caseFilterDialog = document.getElementById('case-filter-dialog');
  const caseFilterForm = document.getElementById('case-filter-form');
  document.getElementById('open-case-filters').addEventListener('click', () => {{
    caseFilterForm.reset();
    caseFilterDialog.showModal();
  }});
  document.getElementById('cancel-case-filters').addEventListener('click', () => caseFilterDialog.close());
  document.getElementById('clear-case-filters').addEventListener('click', () => {{
    for (const select of caseFilterForm.querySelectorAll('select')) select.value = '';
  }});
  caseFilterForm.addEventListener('submit', () => {{
    caseFilterForm.elements.q.value = document.getElementById('q').value;
  }});

  const createCaseDialog = document.getElementById('create-case-dialog');
  const createCaseForm = createCaseDialog.querySelector('form');
  const createCaseSubmit = document.getElementById('create-case-submit');
  document.getElementById('open-create-case').addEventListener('click', () => createCaseDialog.showModal());
  document.getElementById('cancel-create-case').addEventListener('click', () => createCaseDialog.close());
  createCaseForm.addEventListener('submit', event => {{
    if (createCaseSubmit.disabled) {{
      event.preventDefault();
      return;
    }}
    createCaseSubmit.disabled = true;
    createCaseForm.setAttribute('aria-busy', 'true');
  }});
  window.addEventListener('pageshow', event => {{
    if (event.persisted) {{
      createCaseSubmit.disabled = false;
      createCaseForm.removeAttribute('aria-busy');
    }}
  }});
  {'createCaseDialog.showModal();' if message else ''}
</script>
"""
    return _page("工作台 · 案件列表", content)


def _flash(message: str | None, *, param: str = "msg") -> str:
    if not message:
        return ""
    # Soften common backend jargon for operators.
    soft = {
        "customer confirmed": "已记下：客户确认了",
        "review:approve": "终审已通过",
        "review:reject": "终审已驳回",
        "receipt recorded": "已记下：客户收到了",
        "takeover updated": "人工接管状态已更新",
        "version bumped": "材料有变，确认需重做",
        "draft ready": "客户说明已生成",
    }
    if message.startswith("已上传：") or message.startswith("案件已创建"):
        shown = message
        is_warn = "未发出" in message
        return (
            f'<div class="flash{" flash-error" if is_warn else ""}" role="status" '
            f'data-flash-param="{escape(param)}">'
            f"{escape(shown)}</div>"
        )
    shown = message
    low = message.lower()
    for k, v in soft.items():
        if k in low:
            shown = v
            break
    if "migrated to" in low:
        target = message.split()[-1]
        shown = f"进度已更新为：{_status_zh(target)}"
    if low.startswith("route set"):
        shown = "签证类型已更新"
    if "evidence isolated" in low:
        shown = "材料已收到"
    if low.startswith("annotated"):
        shown = "人工标注已保存"
    if "ocr" in low and "ready" in low:
        shown = "AI 已读完这份材料"
    is_err = any(
        x in low
        for x in (
            "error", "fail", "mismatch", "required", "conflict", "not_found", "blocked",
            "失败", "拒绝", "缺失", "被拦", "待确认", "未发出", "发送受限",
        )
    )
    return (
        f'<div class="flash{" flash-error" if is_err else ""}" role="status" '
        f'data-flash-param="{escape(param)}">'
        f"{escape(shown)}</div>"
    )


def _build_todos(
    case: CaseRecord,
    *,
    board: object | None,
    requirement_states: list[object],
    advisor_items: list[object] | None = None,
    itinerary: object | None = None,
    confirmation_sent: bool = False,
) -> list[tuple[str, str, str]]:
    """Summarize waits and material checks alongside the actionable forms."""
    todos: list[tuple[str, str, str]] = []
    st = case.status.value
    questions = advisor_items or []
    waiting = [
        item
        for item in questions
        if getattr(item, "status", "") in {"waiting_customer", "open"}
        and getattr(item, "severity", "") == "simple_auto"
    ]
    advisor_questions = [
        item for item in questions if getattr(item, "status", "") == "escalated_human"
    ]
    shown_elsewhere = {
        getattr(item, "requirement_key", "") for item in waiting + advisor_questions
    }

    if not case.route:
        todos.append(("need", "选择签证类型", ""))
    elif case.customer_email and st in {"lead", "intake_authorization", "route_scope_confirmed"}:
        todos.append(("wait", "等待客户邮件往来开始", ""))
    if st == "sent_awaiting_ack":
        if case.current_delivery_id:
            todos.append(("wait", "等待客户确认收件", "客户直接回复交付邮件「确认收件」后，案件自动标为已办完。"))
        else:
            todos.append(("need", "待核对发送记录", "还没有可关联的材料包发送记录。"))
    if st == "customer_confirmation" and confirmation_sent:
        todos.append(("wait", "等客户确认材料", "请客户直接回复确认邮件「确认材料」，案件会自动进入待终审。"))
    elif case.waiting_customer and not waiting and st != "sent_awaiting_ack" and not (
        st in {"collect_and_verify", "draft_preparation", "customer_confirmation"}
    ):
        todos.append(("wait", "等客户回复", ""))
    for flag in _flags(case):
        if flag != "等客户":
            todos.append(("wait", flag, ""))

    shown_waits: set[str] = set()
    for item in waiting:
        key = getattr(item, "requirement_key", "") or ""
        if key in shown_waits:
            continue
        shown_waits.add(key)
        detail = (getattr(item, "reason_detail", "") or "")[:160]
        title = f"等客户补交{_req_title(key)}" if key else "等客户补件"
        todos.append(("wait", title, detail))

    remaining = [
        s for s in requirement_states if getattr(s, "requirement_key", "") not in shown_elsewhere
    ]
    if st == "collect_and_verify":
        itinerary_status = getattr(itinerary, "status", "")
        if itinerary_status in {"awaiting_confirmation", "draft_ready"} and getattr(itinerary, "evidence_id", None):
            todos.append(("need", "行程单待核对", "可在材料清单直接核对通过。客户回复确认当前版也可以。"))
        elif itinerary_status == "awaiting_details":
            todos.append(("wait", "等客户补充行程信息", "收到旅行日期和停留天数后，Agent 会准备行程单。"))
    prepared_by_agent = {"translation_if_needed", "intention_to_leave"} if case.route == RouteCode.STANDARD_VISITOR else set()
    missing = sum(
        1
        for s in remaining
        if getattr(s, "status", "") == "applicable_missing"
        and getattr(s, "severity", "") != "transfer_human"
        and getattr(s, "requirement_key", "") not in prepared_by_agent
    )
    if missing:
        detail = "Agent 通过邮件跟进补件" if not case.human_takeover else "见下方材料清单"
        todos.append(("wait", f"还有 {missing} 项材料待补", detail))
    elif (st == "draft_preparation" or st == "customer_confirmation" and not confirmation_sent) and not advisor_questions:
        todos.append(("wait", "Agent 正在准备材料确认邮件", ""))

    if any(
        getattr(s, "status", "") in {"received_pending_review", "needs_correction"}
        and not (getattr(s, "requirement_key", "") == "visit_purpose_allowed"
                 and getattr(s, "reason", "").endswith("（来自客户邮件）"))
        for s in remaining
    ):
        todos.append(("need", "有材料待核对", "见下方材料清单"))
    if st == "final_review":
        if getattr(board, "current_final_decision", None) == "reject":
            todos.append(("need", "终审已驳回，需修改", ""))
        elif getattr(board, "customer_confirmation_effective", False):
            todos.append(("need", "核对客户已确认的材料", "点击「终审通过」后，案件进入交付中。"))
    return todos


def _guess_req_key(ev: object) -> str | None:
    cls = (getattr(ev, "classification", "") or "").strip().lower()
    if cls in _CLASS_TO_REQ:
        return _CLASS_TO_REQ[cls]
    if getattr(ev, "source_email_event_id", None):
        # Keep unresolved inbound materials visible for manual classification.
        return None
    name = (getattr(ev, "original_filename", "") or "").lower()
    for needle, key in _FILENAME_HINTS:
        if needle in name:
            return key
    return None


def _evidence_reviewed(ev: object) -> bool:
    version = getattr(ev, "advisor_reviewed_version", None)
    return version is not None and version == getattr(ev, "case_version", None)


def _evidence_status_short(ev: object, ocr: object | None) -> tuple[str, str]:
    q = getattr(ev, "quarantine_status", "") or ""
    if q == "rejected":
        return "已退回", "badge-bad"
    if _evidence_reviewed(ev):
        return "已核对", "badge-ok"
    if getattr(ev, "viewed_at", None):
        return "已查看", "badge-info"
    if q == "accepted_for_review":
        return "已收件", "badge-info"
    ocr_status = (getattr(ocr, "status", "") or "").lower() if ocr else ""
    if ocr_status == "completed":
        return "待顾问核对", "badge-warn"
    if ocr_status in {"failed", "unsupported"}:
        return "待人看", "badge-warn"
    return "待看", "badge-warn"


def _classify_options_html(*, selected: str = "") -> str:
    from visa_agent.case_service.advisor_actions import CLASSIFY_OPTIONS

    bits = ['<option value="">选择材料类型…</option>']
    for value, label in CLASSIFY_OPTIONS:
        sel = " selected" if value == selected else ""
        bits.append(
            f'<option value="{escape(value)}"{sel}>{escape(label)}</option>'
        )
    return "\n".join(bits)


def _file_links(
    case: CaseRecord,
    ev: object,
    ocr: object | None,
    *,
    allow_classify: bool = False,
) -> str:
    eid = escape(getattr(ev, "id", "") or "")
    name = escape(getattr(ev, "original_filename", "") or "文件")
    label, badge = _evidence_status_short(ev, ocr)
    base = f"/workbench/cases/{escape(case.id)}/evidence/{eid}"
    viewed_badge = (
        f" data-evidence-badge='{eid}' aria-live='polite'"
        if getattr(ev, "quarantine_status", "") != "rejected" and not _evidence_reviewed(ev) else ""
    )
    sign_action = ""
    sign_dialog = ""
    if (
        getattr(ev, "classification", "") == "translation_draft"
        and getattr(ev, "translation_source_id", None)
        and getattr(ev, "quarantine_status", "") != "rejected"
        and case.status not in CHECKLIST_READ_ONLY_STATUSES
    ):
        sign_action = (
            f'<button class="text-action" type="button" aria-haspopup="dialog" '
        f'aria-controls="signed-translation-{eid}" data-open-material-dialog>替换译文</button>'
        )
        sign_dialog = f"""
<dialog class="case-dialog" id="signed-translation-{eid}" aria-labelledby="signed-title-{eid}">
  <h2 id="signed-title-{eid}">替换译文</h2>
  <form method="post" action="/workbench/cases/{escape(case.id)}/evidence" enctype="multipart/form-data">
    {_version_field(case)}
    <input type="hidden" name="classification" value="translation">
    <input type="hidden" name="translation_draft_id" value="{eid}">
    <div class="field">
      <label for="signed-file-{eid}">译文 PDF</label>
      <input id="signed-file-{eid}" name="file" type="file" accept=".pdf,application/pdf" required>
    </div>
    <div class="case-dialog-actions">
      <button class="text-action" type="button" data-close-material-dialog>取消</button>
      <button class="btn btn-primary" type="submit">上传译文</button>
    </div>
  </form>
</dialog>
"""
    classify = ""
    if allow_classify:
        classify = f"""
<form method="post" action="/workbench/cases/{escape(case.id)}/evidence/{eid}/classify"
      class="row" style="margin-top:.5rem;align-items:flex-end;gap:.5rem">
  {_version_field(case)}
  <div class="field" style="flex:1;min-width:10rem;margin:0">
    <label for="classify-{eid}">材料类型</label>
    <select id="classify-{eid}" name="classification" required>{_classify_options_html()}</select>
  </div>
  <button class="btn btn-primary btn-sm" type="submit">确认归类</button>
</form>
"""
    delete = ""
    if (
        eid
        and not getattr(ev, "archived_at", None)
        and case.status not in CHECKLIST_READ_ONLY_STATUSES
    ):
        delete = f"""
<button class="text-action file-delete" type="button" aria-label="删除 {name}"
        aria-haspopup="dialog" aria-controls="delete-file-{eid}" data-open-material-dialog>删除</button>
<dialog class="case-dialog file-delete-dialog" id="delete-file-{eid}"
        aria-labelledby="delete-title-{eid}" aria-describedby="delete-description-{eid}">
  <h2 id="delete-title-{eid}">确认删除</h2>
  <p id="delete-description-{eid}">将从此案件移除「{name}」。</p>
  <form method="post" action="{base}/delete">
    {_version_field(case)}
    <div class="case-dialog-actions">
      <button class="text-action" type="button" data-close-material-dialog autofocus>取消</button>
      <button class="btn file-delete-confirm" type="submit">删除</button>
    </div>
  </form>
</dialog>
"""
    return (
        "<li>"
        f"<div class='name'>{name}</div>"
        f"<div class='flag-list'>"
        f"<span class='badge {badge}'{viewed_badge}>{escape(label)}</span>"
        f"<a href='{base}/view' target='_blank' rel='noopener'>查看</a>"
        f"<a href='{base}/file?download=1'>下载</a>"
        f"{sign_action}</div>{sign_dialog}{classify}{delete}</li>"
    )


def _manual_upload(case: CaseRecord, options: dict[str, str]) -> str:
    choices = "".join(
        f'<option value="{escape(value)}">{escape(title)}</option>'
        for value, title in options.items()
    )
    return f"""
<dialog class="case-dialog" id="material-upload" aria-labelledby="material-upload-title">
<h2 id="material-upload-title">手动添加材料</h2>
<form method="post" action="/workbench/cases/{escape(case.id)}/evidence"
      enctype="multipart/form-data">
  {_version_field(case)}
  <div class="field">
    <label for="upload-type">材料类型</label>
    <select id="upload-type" name="classification" required>
      <option value="">选择材料类型…</option>
      {choices}
      <option value="unclassified">其他或待归类</option>
    </select>
  </div>
  <div class="field">
    <label for="upload-file">选择文件</label>
    <input id="upload-file" type="file" name="file" required>
  </div>
  <div class="case-dialog-actions">
    <button class="text-action" type="button" data-close-material-dialog>取消</button>
    <button class="btn btn-primary" type="submit">上传</button>
  </div>
</form>
</dialog>
"""


def _applicability_dialog(
    case: CaseRecord, key: str, title: str, severity: str, decision: str, note: str,
) -> str:
    ref = escape(key)
    choices = (
        ("", "待判断"),
        ("applies", "需转人工" if severity == "transfer_human" else "适用"),
        ("not_applicable", "无需转人工" if severity == "transfer_human" else "不适用"),
    )
    options = "".join(
        f'<option value="{value}"{" selected" if value == decision else ""}>{label}</option>'
        for value, label in choices
    )
    return f"""
<dialog class="case-dialog" id="judge-{ref}" aria-labelledby="judge-title-{ref}">
  <h2 id="judge-title-{ref}">{escape(title)}</h2>
  <form method="post" action="/workbench/cases/{escape(case.id)}/requirements/{ref}/applicability">
    {_version_field(case)}
    <div class="field">
      <label for="decision-{ref}">判断结果</label>
      <select id="decision-{ref}" name="decision" autofocus>{options}</select>
    </div>
    <div class="field">
      <label for="note-{ref}">判断依据（选填）</label>
      <textarea id="note-{ref}" name="note" rows="2">{escape(note)}</textarea>
    </div>
    <div class="case-dialog-actions">
      <button class="text-action" type="button" data-close-material-dialog>取消</button>
      <button class="btn btn-primary" type="submit">保存判断</button>
    </div>
  </form>
</dialog>
"""


def _material_review_dialog(case: CaseRecord, key: str, title: str, files: list[object]) -> str:
    ref = escape(key)
    case_ref = escape(case.id)
    links = []
    for file in files:
        file_ref = escape(getattr(file, "id", ""))
        name = escape(getattr(file, "original_filename", "") or "文件")
        links.append(
            f'<li><a href="/workbench/cases/{case_ref}/evidence/{file_ref}/view" '
            f'target="_blank" rel="noopener">{name}</a></li>'
        )
    return f"""
<dialog class="case-dialog" id="review-{ref}" aria-labelledby="review-title-{ref}">
  <h2 id="review-title-{ref}">核对{escape(title)}</h2>
  <ul class="material-review-files">{''.join(links)}</ul>
  <form method="post" action="/workbench/cases/{case_ref}/requirements/{ref}/review">
    {_version_field(case)}
    <div class="case-dialog-actions">
      <button class="text-action" type="button" data-close-material-dialog autofocus>取消</button>
      <button class="btn btn-primary" type="submit">核对通过</button>
    </div>
  </form>
</dialog>"""


def _checklist_html(
    case: CaseRecord,
    *,
    requirement_states: list[object],
    requirement_titles: dict[str, str] | None,
    evidence_items: list[object],
    ocr_by_evidence: dict[str, object] | None,
    message: str | None = None,
    itinerary: object | None = None,
) -> str:
    ocr_map = ocr_by_evidence or {}
    reasons = {getattr(s, "requirement_key", ""): getattr(s, "reason", "") for s in requirement_states}
    can_edit = case.status not in FINISHED_CASE_STATUSES
    seeds = _SEED_BY_ROUTE.get(case.route.value, ()) if case.route else ()
    items: list[tuple[str, str, str, str, str, str]] = []
    if requirement_states:
        for s in requirement_states:
            key = getattr(s, "requirement_key", "") or ""
            items.append((
                key, _REQ_TITLE_ZH.get(key) or _req_title(key, requirement_titles),
                getattr(s, "status", ""), getattr(s, "severity", ""),
                getattr(s, "applicability_decision", ""), getattr(s, "applicability_note", ""),
            ))
        order = {req.key: index for index, req in enumerate(seeds)}
        items.sort(key=lambda item: order.get(item[0], len(order)))
    else:
        for req in seeds:
            status = "applicability_unknown" if can_judge_applicability(req.key, req.severity) else "applicable_missing"
            items.append((req.key, _req_title(req.key), status, req.severity, "", ""))
    items = [item for item in items if item[0] != "transfer_human_triggers"]

    by_req: dict[str, list[object]] = {key: [] for key, *_ in items}
    unmatched: list[object] = []
    for ev in evidence_items:
        key = _guess_req_key(ev)
        if key and key in by_req:
            by_req[key].append(ev)
        elif key:
            # Known theme not in this route pack — still show under unmatched.
            unmatched.append(ev)
        else:
            unmatched.append(ev)

    upload = ""
    dialogs: list[str] = []
    if can_edit:
        upload_types: dict[str, str] = {}
        for key, title, *_ in items:
            classification = _REQ_TO_CLASS.get(key)
            if classification:
                upload_types.setdefault(classification, title)
        upload = """<button class="text-action" type="button" aria-haspopup="dialog"
          aria-controls="material-upload" data-open-material-dialog>手动添加材料</button>"""
        dialogs.append(_manual_upload(case, upload_types))
    heading = f'<div class="case-list-heading"><h2>材料清单</h2>{upload}</div>'

    lis: list[str] = []
    active_evidence_ids = {
        getattr(file, "id", None) for file in evidence_items
        if getattr(file, "quarantine_status", "") != "rejected" and not getattr(file, "archived_at", None)
    } - {None}
    for key, title, status, severity, decision, note in items:
        itinerary_row = key == "intention_to_leave" and case.route == RouteCode.STANDARD_VISITOR
        section_id = ""
        if itinerary_row:
            title = "行程单"
            if itinerary is not None:
                section_id = ' id="itinerary"'
        elif key == "translation_if_needed":
            section_id = ' id="translation-materials"'
        label, badge = _REQ_STATUS_ZH.get(status, (status or "未知", "badge"))
        if severity == "transfer_human":
            if status == "needs_correction":
                label = "需转人工"
            elif status == "not_applicable":
                label = "无需转人工"
        files = by_req.get(key) or []
        review_files = [
            file for file in files
            if getattr(file, "quarantine_status", "") != "rejected"
            and (
                requirement_for_classification(getattr(file, "classification", ""), route=case.route) == key
                or (key == "translation_if_needed" and getattr(file, "classification", "") == "translation_draft"
                    and getattr(file, "translation_source_id", None) in active_evidence_ids)
            )
        ]
        legacy_unclassified = [
            file for file in files
            if getattr(file, "quarantine_status", "") != "rejected"
            and not requirement_for_classification(getattr(file, "classification", ""), route=case.route)
            and getattr(file, "classification", "") != "translation_draft"
        ]
        if status == "applicable_missing":
            if review_files:
                label, badge = "待核对", "badge-warn"
            elif legacy_unclassified:
                label, badge = "待归类", "badge-warn"
        if itinerary_row and status == "applicable_missing" and not files:
            label, badge = "Agent 准备", "badge-info"
        actions = []
        if can_judge_applicability(key, severity) and case.status not in CHECKLIST_READ_ONLY_STATUSES:
            actions.append(
                '<button class="text-action" type="button" aria-haspopup="dialog" '
                f'aria-controls="judge-{escape(key)}" aria-label="判断 {escape(title)}" '
                f'data-open-material-dialog>{"修改" if decision else "判断"}</button>'
            )
            dialogs.append(_applicability_dialog(case, key, title, severity, decision, note))
        note_html = f'<p class="material-note">{escape(note)}</p>' if note else ""
        if key == "visit_purpose_allowed":
            reason = reasons.get(key, "")
            purpose = reason.removesuffix("（来自客户邮件）")
            if purpose != reason:
                label, badge = "已记录", "badge-info"
                note_html = f'<p class="material-note">{escape(purpose)}</p>'
        if key == "translation_if_needed" and case.route == RouteCode.STANDARD_VISITOR and decision != "not_applicable":
            if status == "applicable_missing" and any(not _evidence_reviewed(file) for file in review_files):
                label, badge = "待核对", "badge-warn"
            elif status == "applicable_missing":
                label, badge = "Agent 待起草", "badge-info"
            elif status == "applicability_unknown":
                label, badge = "随原件判断", "badge-info"
        pending_translation_draft = key == "translation_if_needed" and status == "evidence_accepted" and any(
            getattr(file, "classification", "") == "translation_draft" for file in review_files
        )
        if (label == "待核对" or pending_translation_draft) and review_files and case.status not in CHECKLIST_READ_ONLY_STATUSES:
            actions.append(
                '<button class="text-action" type="button" aria-haspopup="dialog" '
                f'aria-controls="review-{escape(key)}" aria-label="核对 {escape(title)}" '
                'data-open-material-dialog>核对</button>'
            )
            dialogs.append(_material_review_dialog(case, key, title, review_files))
        action_html = f'<div class="material-actions">{"".join(actions)}</div>' if actions else ""
        file_html = ""
        if files:
            file_html = (
                '<ul class="file-list">'
                + "".join(
                    _file_links(
                        case, ev, ocr_map.get(getattr(ev, "id", "") or ""),
                        allow_classify=can_edit and ev in legacy_unclassified,
                    )
                    for ev in files
                )
                + "</ul>"
            )
        lis.append(
            f'<li class="material-item"{section_id}><div class="material-heading">'
            f'<span class="material-title">{escape(title)}</span>'
            f'<span class="badge {badge} material-status">{escape(label)}</span>{action_html}</div>'
            f'{_itinerary_status_html(itinerary) if itinerary_row else ""}'
            f"{note_html}{file_html}</li>"
        )

    if unmatched:
        file_html = (
            '<ul class="file-list">'
            + "".join(
                _file_links(
                    case,
                    ev,
                    ocr_map.get(getattr(ev, "id", "") or ""),
                    allow_classify=can_edit,
                )
                for ev in unmatched
            )
            + "</ul>"
        )
        lis.append(
            '<li class="material-item" id="unclassified-materials"><div class="material-heading">'
            '<span class="material-title">未归类材料</span>'
            '<span class="badge badge-warn material-status">待归类</span></div>'
            f"{file_html}</li>"
        )
    listing = (
        f'<ul class="materials-list">{"".join(lis)}</ul>' if lis
        else '<p class="empty">选好签证类型后会出现材料清单。</p>'
    )
    return f"""{heading}{_flash(message, param="materials_msg")}{listing}{''.join(dialogs)}
<script>
  if ('BroadcastChannel' in window) {{
    const previews = new BroadcastChannel('workbench-material-viewed');
    previews.addEventListener('message', (event) => {{
      document.querySelectorAll('[data-evidence-badge]').forEach(badge => {{
        if (badge.dataset.evidenceBadge === event.data) {{
          badge.className = 'badge badge-info';
          badge.textContent = '已查看';
        }}
      }});
    }});
  }}
  document.querySelectorAll('[data-open-material-dialog]').forEach(button => {{
    const dialog = document.getElementById(button.getAttribute('aria-controls'));
    button.addEventListener('click', () => {{
      dialog.querySelector('form').reset();
      dialog.showModal();
    }});
  }});
  document.querySelectorAll('[data-close-material-dialog]').forEach(button => {{
    button.addEventListener('click', () => button.closest('dialog').close());
  }});
</script>"""


def render_evidence_view(
    case: CaseRecord,
    *,
    evidence: object,
    text_preview: str | None,
    can_inline_frame: bool,
    viewed: bool = False,
) -> str:
    """HTML shell for opening a material without forcing download."""
    eid = escape(getattr(evidence, "id", "") or "")
    name = escape(getattr(evidence, "original_filename", "") or "材料")
    file_url = f"/workbench/cases/{escape(case.id)}/evidence/{eid}/file"
    body_inner: str
    if text_preview is not None:
        body_inner = f"<pre class='pre'>{escape(text_preview)}</pre>"
    elif can_inline_frame:
        lower = (getattr(evidence, "content_type", "") or "").lower()
        name_l = (getattr(evidence, "original_filename", "") or "").lower()
        if lower.startswith("image/") or name_l.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            body_inner = (
                f"<img class='preview-img' src='{file_url}' alt='{name}'>"
            )
        else:
            body_inner = (
                f"<iframe class='preview-frame' src='{file_url}' title='{name}'></iframe>"
            )
    else:
        body_inner = "<p class='empty'>请下载文件查看</p>"
    notify = f"""
<script data-evidence-id="{eid}">
  if ('BroadcastChannel' in window) {{
    const previews = new BroadcastChannel('workbench-material-viewed');
    previews.postMessage(document.currentScript.dataset.evidenceId);
    previews.close();
  }}
</script>""" if viewed else ""
    content = f"""
<header class="topbar">
  <div class="brand">
    <div class="brand-mark">{name}</div>
    <div class="brand-sub">{escape(case.title or '')}</div>
  </div>
  <nav class="top-links">
    <a href="/workbench/cases/{escape(case.id)}">← 回案件</a>
    <a href="{file_url}?download=1">下载</a>
  </nav>
</header>
<section class="panel">{body_inner}</section>
{notify}
"""
    return _page(f"查看 · {getattr(evidence, 'original_filename', '材料')}", content)


def _advisor_work_panel(
    case: CaseRecord,
    *,
    advisor_items: list[object] | None,
    evidence_items: list[object] | None,
) -> str:
    """Advisor actions for the current case."""
    items = [
        q
        for q in (advisor_items or [])
        if (getattr(q, "status", "") or "") == "escalated_human"
    ]
    if not items:
        return ""
    has_unclassified = any(
        _guess_req_key(item) is None for item in evidence_items or []
    )
    blocks: list[str] = []
    for q in items:
        key = getattr(q, "requirement_key", "") or ""
        qid = escape(getattr(q, "id", "") or "")
        detail = escape(getattr(q, "reason_detail", "") or "")
        title = escape(_req_title(key))
        if key == "advisor_unclassified":
            classify_link = (
                '<a href="#unclassified-materials">查看待归类材料</a>'
                if has_unclassified else ""
            )
            blocks.append(
                f'<div class="check-row" style="margin-bottom:.75rem">'
                f'<div class="todo-title">{title}</div>'
                f'<div class="todo-note">{detail or "材料需要人工归类"}</div>'
                f'{classify_link}'
                f'<details class="reference"><summary>无需归类</summary>'
                f"<form method='post' action='/workbench/cases/{escape(case.id)}/advisor-ack' "
                f"style='margin-top:.5rem'>"
                f"<input type='hidden' name='question_id' value='{qid}'>"
                f"<input type='hidden' name='next' value='case'>"
                f"<button class='btn btn-ghost btn-sm' type='submit'>确认关闭这条待办</button>"
                f"</form></details></div>"
            )
        elif key == "advisor_customer_question" and not case.human_takeover:
            blocks.append(
                f'<div class="check-row" style="margin-bottom:.75rem">'
                f'<div class="todo-title">{title}</div>'
                f'<div class="todo-note">{detail or "客户提问需顾问作答"}</div>'
                f"<p class='muted'>收件人 {escape(case.customer_email or '未填写')}</p>"
                f"<form method='post' action='/workbench/cases/{escape(case.id)}/advisor-reply'>"
                f"<input type='hidden' name='question_id' value='{qid}'>"
                f"<div class='field'><label>写给客户的答复</label>"
                f"<textarea name='body' rows='5' required "
                f"placeholder='用客户能看懂的话写清楚…'></textarea></div>"
                f"<button class='btn btn-primary' type='submit'>发送答复</button>"
                f"</form></div>"
            )
        else:
            blocks.append(
                f'<div class="check-row" style="margin-bottom:.75rem">'
                f'<div class="todo-title">{title}</div>'
                f'<div class="todo-note">{detail}</div>'
                f"<form method='post' action='/workbench/cases/{escape(case.id)}/advisor-ack' "
                f"style='margin-top:.5rem'>"
                f"<input type='hidden' name='question_id' value='{qid}'>"
                f"<input type='hidden' name='next' value='case'>"
                f"<button class='btn btn-primary btn-sm' type='submit'>标为已处理</button>"
                f"</form></div>"
            )
    return "".join(blocks)


def _customer_history(mail: list[CustomerMail]) -> str:
    messages = []
    previous_subject = ""
    for item in reversed(mail):
        outbound = item.direction == "outbound"
        sender = "发给客户" if outbound else "客户来信"
        direction_class = " outbound" if outbound else ""
        timestamp = escape(item.created_at.isoformat(timespec="minutes"))
        subject_key = takeover_reply_subject(item)
        subject = ""
        if item.subject and subject_key != previous_subject:
            subject = f'<p class="customer-message-subject">{escape(item.subject)}</p>'
        previous_subject = subject_key
        messages.append(
            f'<article class="customer-message{direction_class}">'
            f'<div class="customer-message-meta"><strong>{sender}</strong><time datetime="{timestamp}">{timestamp}</time></div>'
            f'{subject}<p class="customer-message-body">{escape(item.body or "（无正文）")}</p></article>'
        )
    history = "".join(messages) or '<p class="empty">暂无往来邮件</p>'
    return f'<div class="customer-history-list" role="region" aria-label="往来邮件" tabindex="0">{history}</div>'


def _takeover_reply_panel(
    case: CaseRecord, *, mail: list[CustomerMail], form: dict[str, str], message: str | None,
    reply_allowed: bool,
) -> str:
    selected = next((item for item in mail if item.id == form.get("reply_event_id")), None)
    if selected is None and mail:
        selected = mail[0]
    case_ref = escape(case.id)
    request_id = escape(form.get("request_id") or uuid.uuid4().hex)
    email = normalize_email(case.customer_email)
    can_send = case.human_takeover and bool(email) and reply_allowed
    recipient = f'<span class="muted case-email">{escape(email)}</span>' if email else ""
    email_field = ""
    if case.human_takeover and not email:
        email_field = f"""
    <div class="row">
      <div class="field">
        <label for="takeover-customer-email">收件人邮箱</label>
        <input id="takeover-customer-email" name="customer_email" type="email"
               value="{escape(form.get('customer_email', ''))}">
      </div>
      <button class="btn btn-ghost" type="submit" formnovalidate
              formaction="/workbench/cases/{case_ref}/customer-email">保存邮箱</button>
    </div>"""
    notice = ""
    if not case.human_takeover:
        notice = "请先接管案件"
    elif not email:
        notice = "先填写并保存收件人邮箱"
    elif not reply_allowed:
        notice = "客户邮箱不在允许发信的名单中"
    notice_html = f'<small class="muted">{notice}</small>' if notice else ""
    return f"""
<section class="panel customer-reply" id="customer-reply">
  <div class="case-list-heading">
    <h2>客户对话</h2>
    {recipient}
  </div>
  {_customer_history(mail)}
  <form method="post" action="/workbench/cases/{case_ref}/takeover-reply">
    {_version_field(case)}
    <input type="hidden" name="request_id" value="{request_id}">
    <input type="hidden" name="reply_event_id" value="{escape(selected.id if selected else '')}">
    {_flash(message, param="reply_msg")}
    {email_field}
    <div class="customer-compose-row">
      <textarea id="takeover-reply-body" name="body" rows="2" required aria-label="邮件正文"
                placeholder="输入消息">{escape(form.get('body', ''))}</textarea>
      <button class="btn btn-primary" type="submit"{"" if can_send else " disabled"}>发送邮件</button>
    </div>
    {notice_html}
  </form>
</section>
<script>
  const customerHistory = document.querySelector('#customer-reply .customer-history-list');
  const historyTimeFormat = new Intl.DateTimeFormat('zh-CN', {{dateStyle: 'short', timeStyle: 'short'}});
  customerHistory.querySelectorAll('time').forEach(time => {{
    time.textContent = historyTimeFormat.format(new Date(time.dateTime));
  }});
  window.requestAnimationFrame(() => {{
    customerHistory.scrollTop = customerHistory.scrollHeight;
  }});
</script>
"""


def _review_actions_html(case: CaseRecord, board: ReviewBoardSnapshot | None) -> str:
    if case.status != CaseStatus.FINAL_REVIEW or not board or not board.customer_confirmation_effective:
        return ""
    version = _version_field(case)
    action = f"/workbench/cases/{escape(case.id)}/reviews"
    return f"""
<div class="action-row">
  <form method="post" action="{action}">
    {version}
    <button class="btn btn-primary btn-lg" name="decision" value="approve">终审通过</button>
    <button class="btn btn-danger btn-lg" name="decision" value="reject">终审驳回</button>
  </form>
</div>
"""


def _itinerary_status_html(itinerary: object | None) -> str:
    if itinerary is None:
        return ""
    start = getattr(itinerary, "start", None)
    window = getattr(itinerary, "departure_window", "")
    days = getattr(itinerary, "days", None)
    details = []
    if start or window:
        details.append(f"出发 {start.isoformat() if start else window}")
    if days:
        approximate = "约 " if getattr(itinerary, "duration_approximate", False) else ""
        details.append(f"停留 {approximate}{days} 天")
    dates = f'<p class="material-note">{escape(" · ".join(details))}</p>' if details else ""
    draft = (getattr(itinerary, "draft_text", "") or "").strip()
    if not draft:
        if getattr(itinerary, "awaiting_details", True):
            return dates + '<p class="material-note">等客户补充旅行日期或停留天数</p>'
        return dates
    labels = {
        "awaiting_details": "等客户补充行程信息",
        "draft_ready": "草案待发送",
    }
    status = labels.get(getattr(itinerary, "status", ""), "")
    if not getattr(itinerary, "evidence_id", None):
        return dates + '<p class="material-note">PDF 附件待生成</p>'
    return dates + (f'<p class="material-note">{status}</p>' if status else "")


def _case_closure_html(
    case: CaseRecord, status: str | None, *, completion_ready: bool, termination_reason: str | None,
) -> str:
    heading = '<h2>案件状态变更</h2>'
    endpoint = f"/workbench/cases/{escape(case.id)}/close"
    version = _version_field(case)
    if case.status == CaseStatus.CLOSED:
        if termination_reason is not None:
            reason = f'<p class="material-note">终止理由：{escape(termination_reason)}</p>' if termination_reason else ""
            return f'<section class="panel" id="case-closure">{heading}<p><strong>案件已终止</strong></p>{reason}</section>'
        notes = {
            "provider_accepted": "服务结束通知已发送。",
            "failed": "服务结束通知发送失败。",
            "blocked": "服务结束通知被发信设置拦住。",
            "pending": "服务结束通知等待发送。",
            "processing": "服务结束通知正在发送。",
        }
        note = notes.get(status, "服务结束通知的发送结果待确认。") if status else ""
        form = ""
        if status in {"pending", "blocked", "failed"}:
            form = f"""
<form method="post" action="{endpoint}">
  {version}
  <button class="btn btn-ghost" type="submit">重试发送通知</button>
</form>"""
        note_html = f'<p class="desc">{note}</p>' if note else ""
        return f'<section class="panel" id="case-closure">{heading}<p><strong>案件已结束</strong></p>{note_html}{form}</section>'

    disabled = "" if completion_ready else " disabled"
    return f"""
<section class="panel" id="case-closure">
  {heading}
  <div class="action-row">
    <button class="btn btn-danger" type="button" aria-haspopup="dialog" aria-controls="terminate-case-dialog" data-case-dialog>终止案件</button>
    <button class="btn btn-primary" type="button" aria-haspopup="dialog" aria-controls="complete-case-dialog" data-case-dialog{disabled}>结束案件</button>
  </div>
  <dialog class="case-dialog" id="terminate-case-dialog" aria-labelledby="terminate-case-title">
    <h2 id="terminate-case-title">确认终止案件</h2>
    <form method="post" action="{endpoint}">
      {version}
      <input type="hidden" name="operation" value="terminate">
      <div class="field">
        <label for="termination-reason">终止理由（选填）</label>
        <textarea id="termination-reason" name="reason" rows="3" maxlength="1000"></textarea>
      </div>
      <div class="case-dialog-actions">
        <button class="text-action" type="button" data-case-dialog-cancel autofocus>取消</button>
        <button class="btn btn-danger" type="submit">确认终止</button>
      </div>
    </form>
  </dialog>
  <dialog class="case-dialog" id="complete-case-dialog" aria-labelledby="complete-case-title">
    <h2 id="complete-case-title">确认结束案件</h2>
    <p>系统会给客户发邮件告知本次服务结束。</p>
    <form method="post" action="{endpoint}">
      {version}
      <div class="case-dialog-actions">
        <button class="text-action" type="button" data-case-dialog-cancel autofocus>取消</button>
        <button class="btn btn-primary" type="submit"{disabled}>确认结束</button>
      </div>
    </form>
  </dialog>
</section>
<script>
  document.querySelectorAll('#case-closure [data-case-dialog]').forEach(button => {{
    button.addEventListener('click', () => document.getElementById(button.getAttribute('aria-controls')).showModal());
  }});
  document.querySelectorAll('#case-closure [data-case-dialog-cancel]').forEach(button => {{
    button.addEventListener('click', () => button.closest('dialog').close());
  }});
</script>
"""


def render_detail(
    case: CaseRecord,
    *,
    message: str | None = None,
    drafts: list[object] | None = None,
    evidence_items: list[object] | None = None,
    review_board: ReviewBoardSnapshot | None = None,
    ocr_by_evidence: dict[str, object] | None = None,
    requirement_states: list[object] | None = None,
    requirement_titles: dict[str, str] | None = None,
    advisor_items: list[object] | None = None,
    itinerary: object | None = None,
    confirmation_sent: bool = False,
    materials_message: str | None = None,
    closure_status: str | None = None,
    completion_ready: bool = False,
    termination_reason: str | None = None,
    customer_mail: list[CustomerMail] | None = None,
    reply_allowed: bool = True,
    reply_form: dict[str, str] | None = None,
    reply_message: str | None = None,
) -> str:
    evidence = list(evidence_items or [])
    reqs = list(requirement_states or [])
    can_edit = case.status not in FINISHED_CASE_STATUSES
    show_next_steps = can_edit and case.status != CaseStatus.READY_TO_DELIVER
    todos = []
    if show_next_steps:
        todos = _build_todos(
            case,
            board=review_board,
            requirement_states=reqs,
            advisor_items=advisor_items,
            itinerary=itinerary,
            confirmation_sent=confirmation_sent,
        )
    todo_lis = []
    for kind, title, note in todos:
        note_html = (
            f'<div class="todo-note">{escape(note)}</div>' if note else ""
        )
        todo_lis.append(
            f'<li class="{escape(kind)}"><div class="todo-main">'
            f'<div class="todo-title">{escape(title)}</div>{note_html}</div></li>'
        )

    can_set_route = case.status.value in {
        "lead",
        "intake_authorization",
        "route_scope_confirmed",
    }
    route_block = ""
    if can_set_route:
        opts = "\n".join(
            f'<option value="{escape(code)}"'
            f'{" selected" if case.route and case.route.value == code else ""}>'
            f"{escape(label)}</option>"
            for code, label in _ROUTE_OPTIONS
        )
        route_block = f"""
<form method="post" action="/workbench/cases/{escape(case.id)}/route" class="row" style="margin-top:.75rem">
  {_version_field(case)}
  <div class="field">
    <label for="route">签证类型</label>
    <select id="route" name="route" required>{opts}</select>
  </div>
  <button class="btn btn-ghost" type="submit">保存签证类型</button>
</form>
"""

    draft_html = ""
    draft_rows = drafts or []
    current = [
        d
        for d in draft_rows
        if int(getattr(d, "case_version", 0) or 0) == case.case_version
    ]
    if current and case.route != RouteCode.STANDARD_VISITOR:
        draft = current[0]
        facts = getattr(draft, "candidate_facts", ()) or ()
        text = getattr(draft, "draft_text", "") or ""
        # Prefer customer-facing rewrite from facts (covers old jargon drafts).
        if facts:
            from visa_agent.ai_capabilities.gateway import (
                format_customer_draft_from_facts,
            )

            text, _actions = format_customer_draft_from_facts(facts)
        draft_html = f"<pre class='pre'>{escape(text)}</pre>"
    advisor_actions = ""
    primary_action = ""
    takeover = ""
    customer_reply = ""
    if show_next_steps:
        advisor_actions = _advisor_work_panel(
            case, advisor_items=advisor_items, evidence_items=evidence,
        )
        primary_action = _review_actions_html(case, review_board)
    if can_edit:
        takeover = f"""
<form method="post" action="/workbench/cases/{escape(case.id)}/takeover">
  {_version_field(case)}
  <input type="hidden" name="active" value="{'false' if case.human_takeover else 'true'}">
  <button class="text-action" type="submit">{'交还系统' if case.human_takeover else '人工接管'}</button>
</form>
"""
        if case.human_takeover or reply_form is not None:
            customer_reply = _takeover_reply_panel(
                case, mail=customer_mail or [], form=reply_form or {}, message=reply_message,
                reply_allowed=reply_allowed,
            )
    next_steps = ""
    if todo_lis or advisor_actions or primary_action:
        todo_html = f'<ul class="todo">{"".join(todo_lis)}</ul>' if todo_lis else ""
        next_steps = f"""
<section class="panel" id="advisor-work">
  <h2>下一步</h2>
  {todo_html}
  {advisor_actions}
  {primary_action}
</section>
"""

    content = f"""
<header class="topbar">
  <nav class="top-links">
    <a href="/workbench">← 案件列表</a>
    <a href="/workbench/inbox">收件箱</a>
  </nav>
</header>

{_flash(message)}

<section class="hero">
  <div class="case-list-heading case-heading" id="case-heading">
    <h1>{escape(case.title or '未命名客户')}</h1>
    {takeover}
  </div>
  <div class="hero-meta">
    <span>客户：<strong>{escape(case.customer_email or '（没填邮箱）')}</strong></span>
    <span>签证：<strong>{escape(_route_zh(case))}</strong></span>
    <span>进度：<strong>{escape(_case_status_text(case, terminated=termination_reason is not None))}</strong></span>
  </div>
  {route_block}
</section>

{customer_reply}
{next_steps}

<section class="panel" id="materials">
  {_checklist_html(
      case,
      requirement_states=reqs,
      requirement_titles=requirement_titles,
      evidence_items=evidence,
      ocr_by_evidence=ocr_by_evidence,
      message=materials_message,
      itinerary=itinerary,
  )}
</section>

{f'<section class="panel"><h2>给客户的邮件草稿</h2>{draft_html}</section>' if draft_html else ''}

{_case_closure_html(case, closure_status, completion_ready=completion_ready, termination_reason=termination_reason)}

"""
    return _page(f"{case.title or '案件'}", content)
