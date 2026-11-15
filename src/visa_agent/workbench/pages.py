"""Workbench HTML — presentation only; forms keep existing endpoints."""

from __future__ import annotations

from html import escape

from visa_agent.case_service.service import CaseRecord
from visa_agent.domain.enums import CaseStatus, RouteCode
from visa_agent.rules.seed import (
    GRADUATE_REQUIREMENTS,
    SKILLED_WORKER_REQUIREMENTS,
    STUDENT_REQUIREMENTS,
    VISITOR_REQUIREMENTS,
)

# List page route labels (homepage polish deferred).
_ROUTE_OPTIONS: tuple[tuple[str, str], ...] = (
    (RouteCode.STANDARD_VISITOR.value, "访问签证（Visitor）"),
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
    "draft_preparation": "写客户说明中",
    "customer_confirmation": "等客户确认",
    "final_review": "等人终审",
    "ready_to_deliver": "可以发了",
    "sent_awaiting_ack": "已发出，等回执",
    "delivered": "已办完",
    "closed": "已关闭",
}

_REQ_TITLE_ZH: dict[str, str] = {
    "passport_valid": "护照",
    "visit_purpose_allowed": "访问目的说明",
    "intention_to_leave": "离境意向 / 行程",
    "funds_for_stay": "资金证明",
    "home_ties": "国内约束力证明",
    "sponsor_if_any": "担保人材料（如有）",
    "translation_if_needed": "翻译件（如需要）",
    "transfer_human_triggers": "是否需转人工（敏感情形）",
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
    "evidence_accepted": ("已通过", "badge-ok"),
    "needs_correction": ("需处理", "badge-bad"),
    "not_applicable": ("不适用", "badge-info"),
    "applicability_unknown": ("待确认", "badge-warn"),
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

# Preferred next step: (target_status, button_label, tip)
_PRIMARY_NEXT: dict[str, tuple[str, str, str]] = {
    "lead": ("intake_authorization", "开始接案", "确认要接这个客户后点这里。"),
    "intake_authorization": (
        "route_scope_confirmed",
        "确认签证类型",
        "签证类型对了再往下收材料。",
    ),
    "route_scope_confirmed": (
        "collect_and_verify",
        "开始收材料",
        "进入收件阶段，对照下面清单补材料。",
    ),
    "collect_and_verify": (
        "draft_preparation",
        "材料差不多了，去写说明",
        "缺的补齐、该人看的看完后再点。",
    ),
    "draft_preparation": (
        "customer_confirmation",
        "说明写好了，发给客户确认",
        "先点「生成客户说明」，满意后再推进。",
    ),
    "customer_confirmation": (
        "final_review",
        "客户已同意，送终审",
        "先点「客户已确认」，再推进。",
    ),
    "final_review": (
        "ready_to_deliver",
        "终审通过，可以准备发送",
        "先点「终审通过」，再推进。",
    ),
    "ready_to_deliver": (
        "sent_awaiting_ack",
        "标记为已发出",
        "真正发信目前走后台；发完后点这里。",
    ),
    "sent_awaiting_ack": (
        "delivered",
        "客户已收到，办完",
        "先点「客户已回执」，再点办完。",
    ),
}


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
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
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
  --radius:12px;
  --shadow:0 1px 2px rgba(19,78,74,.06),0 8px 24px rgba(13,148,136,.06);
  --font: 'Plus Jakarta Sans', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
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
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible{
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
.eyebrow{
  display:inline-flex;align-items:center;gap:.35rem;
  font-size:.72rem;font-weight:700;
  letter-spacing:.04em;color:var(--color-muted-fg);
  background:var(--color-muted);border-radius:999px;padding:.2rem .55rem;
  margin-bottom:.45rem
}
.lead{margin:0;color:var(--color-muted-fg);font-size:.92rem;max-width:42rem}
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
.field input,.field select,textarea,select,input[type=text],input[type=email]{
  font:inherit;padding:.55rem .7rem;border:1px solid var(--color-border-strong);
  border-radius:10px;background:#fff;color:var(--color-foreground);width:100%
}
.row{display:flex;gap:.65rem;flex-wrap:wrap;align-items:flex-end}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:.9rem}
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
.mono{font-family:var(--mono);font-size:.82rem}
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
  padding:.45rem .6rem;border-radius:8px;border:1px solid var(--color-border);
  background:#fff;flex-wrap:wrap;font-size:.88rem
}
.file-list .name{font-weight:600}
.file-list a{font-weight:600}
.check-row{display:flex;flex-direction:column;gap:.35rem;width:100%}
.check-top{display:flex;justify-content:space-between;gap:.75rem;align-items:flex-start;flex-wrap:wrap}
.upload-mini{margin-top:.35rem}
.upload-mini .row{gap:.4rem}
.preview-frame{
  width:100%;min-height:70vh;border:1px solid var(--color-border);
  border-radius:10px;background:#fff
}
.preview-img{max-width:100%;height:auto;border-radius:10px;border:1px solid var(--color-border)}
details.adv{
  border:1px dashed var(--color-border-strong);border-radius:var(--radius);
  padding:.85rem 1rem;margin-bottom:1rem;background:rgba(255,255,255,.65)
}
details.adv>summary{
  cursor:pointer;font-weight:700;list-style:none;display:flex;align-items:center;
  justify-content:space-between;gap:1rem
}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::after{content:'展开';font-size:.78rem;color:var(--color-muted-fg);font-weight:600}
details.adv[open]>summary::after{content:'收起'}
.action-row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:.75rem}
.pre{
  white-space:pre-wrap;background:var(--color-muted);border-radius:10px;
  padding:.85rem;border:1px solid var(--color-border);font-size:.9rem;margin:.5rem 0 0
}
.empty{color:var(--color-muted-fg);font-size:.92rem;margin:.25rem 0}
.case-link{font-weight:700}
.flag-list{display:flex;flex-wrap:wrap;gap:.3rem}
.primary-block{margin-top:1rem}
@media (max-width:640px){
  .wrap{padding:1rem 1rem 2.5rem}
  th:nth-child(4),td:nth-child(4),th:nth-child(6),td:nth-child(6){display:none}
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
    inbox_label = "顾问提醒"
    if alert_count:
        inbox_label = f"顾问提醒 · {alert_count}"
    home_cls = " aria-current='page'" if active == "home" else ""
    inbox_cls = " aria-current='page'" if active == "inbox" else ""
    return f"""
<header class="topbar">
  <div class="brand">
    <div class="brand-mark">签证邮件助手</div>
    <div class="brand-sub">工作台</div>
  </div>
  <nav class="top-links">
    <a href="/workbench"{home_cls}>案件</a>
    <a href="/workbench/inbox"{inbox_cls}>{escape(inbox_label)}</a>
  </nav>
</header>
"""


def render_inbox(
    alerts: list[object] | None = None,
    *,
    message: str | None = None,
) -> str:
    """Standalone advisor reminder page — not mixed into the case list."""
    alerts = list(alerts or [])
    rows: list[str] = []
    for a in alerts:
        case_id = escape(getattr(a, "case_id", "") or "")
        title = escape(getattr(a, "case_title", "") or "未命名案件")
        detail = escape(getattr(a, "reason_detail", "") or "")
        kind = _alert_kind_zh(
            getattr(a, "alert_kind", "") or "",
            getattr(a, "reason_code", "") or "",
        )
        email = escape(getattr(a, "customer_email", "") or "—")
        qid = getattr(a, "question_id", "") or ""
        ack = ""
        if qid:
            ack = (
                f"<form method='post' action='/workbench/cases/{case_id}/advisor-ack' "
                f"style='display:inline'>"
                f"<input type='hidden' name='question_id' value='{escape(qid)}'>"
                f"<button class='btn btn-ghost btn-sm' type='submit'>标为已处理</button>"
                f"</form>"
            )
        rows.append(
            "<tr>"
            f"<td><span class='badge badge-bad'>{escape(kind)}</span></td>"
            f"<td><a class='case-link' href='/workbench/cases/{case_id}'>{title}</a>"
            f"<div class='muted' style='font-size:.85rem;margin-top:.25rem'>{detail}</div></td>"
            f"<td>{email}</td>"
            f"<td><a class='btn btn-primary btn-sm' "
            f"href='/workbench/cases/{case_id}#advisor-work'>"
            f"去案件处理</a> {ack}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or (
        "<tr><td colspan='4'><p class='empty' style='margin:0'>"
        "暂无需要顾问立刻处理的事项。Agent 无法回答客户问题、材料分不清类、"
        "或案件被人工接管时会出现在这里。"
        "</p></td></tr>"
    )
    flash = _flash(message) if message else ""
    content = f"""
{_nav(alert_count=len(alerts), active="inbox")}
{flash}
<section class="panel" style="padding:0;overflow:hidden;border-color:#FECACA">
  <div style="padding:1rem 1.25rem 0">
    <span class="eyebrow">顾问收件箱</span>
    <h2>需要你处理的事</h2>
    <p class="lead" style="margin-bottom:.75rem">
      这里是待办列表，具体操作在案件页的「顾问处理区」：
      归类材料、写答复（经 Agent 发给客户）。共 {len(alerts)} 条。
    </p>
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr><th>类型</th><th>案件 / 原因</th><th>客户邮箱</th><th></th></tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
  </div>
</section>
"""
    return _page("工作台 · 顾问提醒", content)


def render_list(
    cases: list[CaseRecord],
    *,
    alert_count: int = 0,
    filters: dict[str, str] | None = None,
    message: str | None = None,
) -> str:
    """Workbench home: create case + searchable case list only."""
    filters = filters or {}
    q = filters.get("q") or ""
    status_f = filters.get("status") or ""
    route_f = filters.get("route") or ""
    waiting_f = filters.get("waiting") or ""

    rows: list[str] = []
    for c in cases:
        flags = _flags(c)
        flag_html = (
            '<div class="flag-list">'
            + "".join(f'<span class="badge badge-warn">{escape(f)}</span>' for f in flags)
            + "</div>"
            if flags
            else '<span class="muted">—</span>'
        )
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
            f"<div class='mono muted'>{escape(c.id[:10])}…</div></td>"
            f"<td><span class='badge {badge_cls}'>{escape(_status_zh(st))}</span></td>"
            f"<td>{escape(_route_zh(c))}</td>"
            f"<td>{escape(c.customer_email or '—')}</td>"
            f"<td>{flag_html}</td>"
            f"<td><a class='btn btn-ghost btn-sm' href='/workbench/cases/{escape(c.id)}'>"
            f"打开</a></td>"
            "</tr>"
        )
    body = "\n".join(rows) or (
        "<tr><td colspan='6'><p class='empty'>"
        "没有匹配的案件。试试清空筛选，或新建一个。"
        "</p></td></tr>"
    )
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
    status_opts = (
        '<option value="">全部进度</option>\n'
        + "\n".join(
            f'<option value="{escape(code)}"'
            f'{" selected" if code == status_f else ""}>'
            f"{escape(label)}</option>"
            for code, label in _STATUS_ZH.items()
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
    content = f"""
{_nav(alert_count=alert_count, active="home")}
{flash}
<section class="panel">
  <h2>新建案件</h2>
  <form method="post" action="/workbench/cases" class="row">
    <div class="field">
      <label for="title">客户 / 案件名</label>
      <input id="title" name="title" placeholder="例如：张三 · 访英旅游" required>
    </div>
    <div class="field">
      <label for="customer_email">客户邮箱（必填，开案后系统先发信）</label>
      <input id="customer_email" name="customer_email" type="email"
             placeholder="client@example.com" required>
    </div>
    <div class="field">
      <label for="route">签证类型</label>
      <select id="route" name="route" required>
        {route_opts}
      </select>
    </div>
    <button class="btn btn-primary" type="submit">新建</button>
  </form>
</section>

<section class="panel" style="padding:0;overflow:hidden">
  <div style="padding:1rem 1.25rem 0">
    <h2>全部案件</h2>
    <p class="lead" style="margin-bottom:.75rem">共 {len(cases)} 条 · 可搜索 / 按进度与签证筛选</p>
    <form method="get" action="/workbench" class="row" style="margin-bottom:.85rem">
      <div class="field" style="flex:1.4">
        <label for="q">搜索</label>
        <input id="q" name="q" value="{escape(q)}"
               placeholder="案件名 / 邮箱 / 案件号">
      </div>
      <div class="field">
        <label for="status">进度</label>
        <select id="status" name="status">{status_opts}</select>
      </div>
      <div class="field">
        <label for="route_filter">签证</label>
        <select id="route_filter" name="route">{filter_route_opts}</select>
      </div>
      <div class="field">
        <label for="waiting">提醒</label>
        <select id="waiting" name="waiting">{waiting_opts}</select>
      </div>
      <button class="btn btn-primary" type="submit">筛选</button>
      <a class="btn btn-ghost" href="/workbench">清空</a>
    </form>
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr>
        <th>客户 / 案件</th><th>目前进度</th><th>签证</th><th>邮箱</th><th>提醒</th><th></th>
      </tr>
    </thead>
    <tbody>{body}</tbody>
  </table>
  </div>
</section>
"""
    return _page("工作台 · 案件列表", content)


def _flash(message: str | None) -> str:
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
        "reopen authorized": "已允许重开",
        "draft ready": "客户说明已生成",
    }
    if message.startswith("已上传：") or message.startswith("案件已创建"):
        shown = message
        is_warn = "未发出" in message
        return (
            f'<div class="flash{" flash-error" if is_warn else ""}" role="status">'
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
        for x in ("error", "fail", "拒绝", "缺失", "mismatch", "required", "conflict")
    )
    return (
        f'<div class="flash{" flash-error" if is_err else ""}" role="status">'
        f"{escape(shown)}</div>"
    )


def _build_todos(
    case: CaseRecord,
    *,
    board: object | None,
    requirement_states: list[object],
    evidence_items: list[object],
    allowed: list[CaseStatus],
    advisor_items: list[object] | None = None,
) -> list[tuple[str, str, str]]:
    """Return (kind, title, note) where kind in need|wait|ok|info."""
    todos: list[tuple[str, str, str]] = []
    st = case.status.value
    conf_ok = bool(getattr(board, "customer_confirmation_effective", False)) if board else False
    appr_ok = bool(getattr(board, "final_approval_effective", False)) if board else False
    rejected = (
        getattr(board, "current_final_decision", None) == "reject" if board else False
    )

    if not case.route:
        todos.append(("need", "选好签证类型", ""))
    if case.human_takeover:
        todos.append(("wait", "人工接管中", ""))
    if case.waiting_customer:
        todos.append(("wait", "在等客户", ""))

    for item in advisor_items or []:
        status = getattr(item, "status", "") or ""
        if status not in {"escalated_human", "waiting_customer", "open"}:
            continue
        key = getattr(item, "requirement_key", "") or ""
        detail = (getattr(item, "reason_detail", "") or "")[:160]
        if status == "escalated_human":
            hint = "请到下方「顾问处理区」操作"
            if key == "advisor_unclassified":
                hint = "请到下方「顾问处理区」或「未归类材料」里归类"
            elif key == "advisor_customer_question":
                hint = "请到下方「顾问处理区」写回复，经 Agent 发给客户"
            todos.append(
                (
                    "need",
                    f"顾问待办：{_req_title(key)}",
                    (detail + " · " if detail else "") + hint,
                )
            )
        elif getattr(item, "severity", "") == "simple_auto":
            todos.append(("wait", f"等客户补件：{_req_title(key)}", detail))

    missing = [
        s
        for s in requirement_states
        if getattr(s, "status", "") == "applicable_missing"
        and getattr(s, "severity", "") != "transfer_human"
    ]
    human_review = [
        s
        for s in requirement_states
        if getattr(s, "status", "")
        in {"received_pending_review", "needs_correction"}
    ]
    for s in missing[:6]:
        key = getattr(s, "requirement_key", "")
        todos.append(("need", f"还缺：{_req_title(key)}", ""))
    for s in human_review[:6]:
        key = getattr(s, "requirement_key", "")
        status = getattr(s, "status", "")
        if status == "needs_correction":
            todos.append(("need", f"要处理：{_req_title(key)}", ""))
        else:
            todos.append(("wait", f"待核对：{_req_title(key)}", ""))

    isolated = [
        e
        for e in evidence_items
        if (getattr(e, "quarantine_status", "") or "") == "isolated"
    ]
    if isolated:
        todos.append(("wait", f"{len(isolated)} 份材料待验收", ""))

    if st == "draft_preparation":
        todos.append(("info", "生成给客户的邮件草稿", ""))
    if st == "customer_confirmation" and not conf_ok:
        todos.append(("need", "记下客户确认", ""))
    if st == "final_review":
        if rejected:
            todos.append(("need", "终审已驳回，需修改", ""))
        elif not appr_ok:
            todos.append(("need", "做终审", ""))
    if st == "sent_awaiting_ack":
        todos.append(("wait", "等客户回执", ""))

    if not todos:
        if st in {"delivered", "closed"}:
            todos.append(("ok", "暂无待办", ""))
        else:
            todos.append(("info", "继续下一步", ""))
    return todos


def _guess_req_key(ev: object) -> str | None:
    cls = (getattr(ev, "classification", "") or "").strip().lower()
    if cls in _CLASS_TO_REQ:
        return _CLASS_TO_REQ[cls]
    name = (getattr(ev, "original_filename", "") or "").lower()
    for needle, key in _FILENAME_HINTS:
        if needle in name:
            return key
    return None


def _evidence_status_short(ev: object, ocr: object | None) -> tuple[str, str]:
    q = getattr(ev, "quarantine_status", "") or ""
    if q == "rejected":
        return "已退回", "badge-bad"
    if q == "accepted_for_review":
        return "已验收", "badge-ok"
    ocr_status = (getattr(ocr, "status", "") or "").lower() if ocr else ""
    if ocr_status == "completed":
        return "AI已读", "badge-warn"
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
    classify = ""
    if allow_classify:
        classify = f"""
<form method="post" action="/workbench/cases/{escape(case.id)}/evidence/{eid}/classify"
      class="row" style="margin-top:.5rem;align-items:flex-end;gap:.5rem">
  {_version_field(case)}
  <div class="field" style="flex:1;min-width:10rem;margin:0">
    <label>归类为</label>
    <select name="classification" required>{_classify_options_html()}</select>
  </div>
  <button class="btn btn-primary btn-sm" type="submit">确认归类</button>
</form>
"""
    return (
        "<li>"
        f"<div class='name'>{name}</div>"
        f"<div class='flag-list'>"
        f"<span class='badge {badge}'>{escape(label)}</span>"
        f"<a href='{base}/view' target='_blank' rel='noopener'>查看</a>"
        f"<a href='{base}/file?download=1'>下载</a>"
        f"</div>{classify}</li>"
    )


def _row_upload(case: CaseRecord, *, req_key: str, title: str) -> str:
    if req_key == "transfer_human_triggers":
        return ""
    cls = _REQ_TO_CLASS.get(req_key, "unclassified")
    return f"""
<form method="post" action="/workbench/cases/{escape(case.id)}/evidence"
      enctype="multipart/form-data" class="upload-mini">
  {_version_field(case)}
  <input type="hidden" name="classification" value="{escape(cls)}">
  <div class="row">
    <div class="field" style="flex:2;min-width:10rem">
      <label>从本地选择文件</label>
      <input type="file" name="file" required>
    </div>
    <button class="btn btn-ghost btn-sm" type="submit">上传</button>
  </div>
</form>
"""


def _checklist_html(
    case: CaseRecord,
    *,
    requirement_states: list[object],
    requirement_titles: dict[str, str] | None,
    evidence_items: list[object],
    ocr_by_evidence: dict[str, object] | None,
) -> str:
    ocr_map = ocr_by_evidence or {}
    items: list[tuple[str, str, str, str]] = []
    if requirement_states:
        for s in requirement_states:
            key = getattr(s, "requirement_key", "") or ""
            status = getattr(s, "status", "") or ""
            label, badge = _REQ_STATUS_ZH.get(status, (status or "未知", "badge"))
            items.append((key, _req_title(key, requirement_titles), label, badge))
    else:
        route = case.route.value if case.route else None
        seeds = _SEED_BY_ROUTE.get(route or "", ())
        for req in seeds:
            items.append((req.key, _req_title(req.key), "还缺", "badge-bad"))

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

    if not items and not evidence_items:
        return "<p class='empty'>选好签证类型后会出现材料清单。</p>"

    lis: list[str] = []
    for key, title, label, badge in items:
        kind = "ok"
        if label in {"还缺"}:
            kind = "need"
        elif label in {"待核对", "待确认", "还没核对", "需处理"}:
            kind = "need" if label == "需处理" else "wait"
        files = by_req.get(key) or []
        if files and kind == "need" and label == "还缺":
            label, badge, kind = "待核对", "badge-warn", "wait"
        file_html = ""
        if files:
            file_html = (
                '<ul class="file-list">'
                + "".join(
                    _file_links(case, ev, ocr_map.get(getattr(ev, "id", "") or ""))
                    for ev in files
                )
                + "</ul>"
            )
        upload = _row_upload(case, req_key=key, title=title)
        lis.append(
            f'<li class="{kind}"><div class="check-row">'
            f'<div class="check-top"><div class="todo-main">'
            f'<div class="todo-title">{escape(title)}</div></div>'
            f'<span class="badge {badge}">{escape(label)}</span></div>'
            f"{file_html}{upload}</div></li>"
        )

    if unmatched:
        file_html = (
            '<ul class="file-list">'
            + "".join(
                _file_links(
                    case,
                    ev,
                    ocr_map.get(getattr(ev, "id", "") or ""),
                    allow_classify=True,
                )
                for ev in unmatched
            )
            + "</ul>"
        )
        lis.append(
            '<li class="wait" id="unclassified-materials"><div class="check-row">'
            '<div class="check-top"><div class="todo-main">'
            '<div class="todo-title">未归类材料</div>'
            '<div class="todo-note">选类型后点「确认归类」，文件会进入对应清单项</div>'
            "</div>"
            '<span class="badge badge-warn">待归类</span></div>'
            f"{file_html}</div></li>"
        )
    return f'<ul class="todo">{"".join(lis)}</ul>'


def render_evidence_view(
    case: CaseRecord,
    *,
    evidence: object,
    text_preview: str | None,
    can_inline_frame: bool,
) -> str:
    """HTML shell for opening a material without forcing download."""
    eid = escape(getattr(evidence, "id", "") or "")
    name = escape(getattr(evidence, "original_filename", "") or "材料")
    ctype = escape(getattr(evidence, "content_type", "") or "")
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
        body_inner = (
            f"<p class='empty'>这类文件浏览器里不好直接打开，请下载查看。</p>"
            f"<p><a class='btn btn-primary' href='{file_url}?download=1'>下载 {name}</a></p>"
        )
    content = f"""
<header class="topbar">
  <div class="brand">
    <div class="brand-mark">{name}</div>
    <div class="brand-sub">{escape(case.title or '')} · {ctype}</div>
  </div>
  <nav class="top-links">
    <a href="/workbench/cases/{escape(case.id)}">← 回案件</a>
    <a href="{file_url}?download=1">下载</a>
  </nav>
</header>
<section class="panel">{body_inner}</section>
"""
    return _page(f"查看 · {getattr(evidence, 'original_filename', '材料')}", content)


def _advisor_work_panel(
    case: CaseRecord,
    *,
    advisor_items: list[object] | None,
    evidence_items: list[object] | None,
    ocr_by_evidence: dict[str, object] | None,
) -> str:
    """Actionable advisor queue on the case page (classify + reply via agent)."""
    items = [
        q
        for q in (advisor_items or [])
        if (getattr(q, "status", "") or "") == "escalated_human"
    ]
    if not items:
        return ""
    ocr_map = ocr_by_evidence or {}
    unmatched = [
        e
        for e in (evidence_items or [])
        if (getattr(e, "classification", "") or "").lower()
        in {"", "unclassified", "fuzzy", "unclear", "unknown"}
        or _guess_req_key(e) is None
    ]
    blocks: list[str] = []
    for q in items:
        key = getattr(q, "requirement_key", "") or ""
        qid = escape(getattr(q, "id", "") or "")
        detail = escape(getattr(q, "reason_detail", "") or "")
        title = escape(_req_title(key))
        if key == "advisor_unclassified":
            files = ""
            if unmatched:
                files = (
                    '<ul class="file-list">'
                    + "".join(
                        _file_links(
                            case,
                            ev,
                            ocr_map.get(getattr(ev, "id", "") or ""),
                            allow_classify=True,
                        )
                        for ev in unmatched
                    )
                    + "</ul>"
                )
            else:
                files = (
                    "<p class='empty' style='margin:.5rem 0 0'>"
                    "没有待归类文件（可能已归类）。可标为已处理。</p>"
                )
            blocks.append(
                f'<div class="check-row" style="margin-bottom:.75rem">'
                f'<div class="todo-title">{title}</div>'
                f'<div class="todo-note">{detail or "Agent 无法自动分类"}</div>'
                f"{files}"
                f"<form method='post' action='/workbench/cases/{escape(case.id)}/advisor-ack' "
                f"style='margin-top:.5rem'>"
                f"<input type='hidden' name='question_id' value='{qid}'>"
                f"<input type='hidden' name='next' value='case'>"
                f"<button class='btn btn-ghost btn-sm' type='submit'>无需归类，标为已处理</button>"
                f"</form></div>"
            )
        elif key == "advisor_customer_question":
            blocks.append(
                f'<div class="check-row" style="margin-bottom:.75rem">'
                f'<div class="todo-title">{title}</div>'
                f'<div class="todo-note">{detail or "客户提问需顾问作答"}</div>'
                f"<p class='muted' style='font-size:.85rem;margin:.5rem 0'>"
                f"回复经 <strong>Agent 出箱</strong> 发给客户（白名单 + 留痕），"
                f"不要另开邮箱直发。</p>"
                f"<form method='post' action='/workbench/cases/{escape(case.id)}/advisor-reply'>"
                f"<input type='hidden' name='question_id' value='{qid}'>"
                f"<div class='field'><label>写给客户的答复</label>"
                f"<textarea name='body' rows='5' required "
                f"placeholder='用客户能看懂的话写清楚…'></textarea></div>"
                f"<button class='btn btn-primary' type='submit'>经 Agent 发给客户</button>"
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
    return f"""
<section class="panel" id="advisor-work" style="border-color:#FECACA">
  <span class="eyebrow">顾问处理区</span>
  <h2>在这里处理待办</h2>
  <p class="lead" style="margin-bottom:.75rem">
    归类材料、回答客户问题都在本页完成；答疑通过 Agent 转发，不另开邮箱。
  </p>
  {"".join(blocks)}
</section>
"""


def _primary_action_html(case: CaseRecord, allowed: list[CaseStatus]) -> str:
    st = case.status.value
    ver = _version_field(case)
    allowed_vals = {a.value for a in allowed}
    bits: list[str] = []

    # Contextual one-click human actions before transition.
    if st == "customer_confirmation":
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/confirm'>"
            f"{ver}<button class='btn btn-primary btn-lg' type='submit'>客户已确认</button></form>"
        )
    if st == "final_review":
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/reviews'>"
            f"{ver}<input type='hidden' name='decision' value='approve'>"
            f"<button class='btn btn-primary btn-lg' type='submit'>终审通过</button></form>"
        )
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/reviews'>"
            f"{ver}<input type='hidden' name='decision' value='reject'>"
            f"<button class='btn btn-danger btn-lg' type='submit'>终审驳回</button></form>"
        )
    if st == "draft_preparation":
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/draft'>"
            f"<button class='btn btn-primary btn-lg' type='submit'>生成客户说明</button></form>"
        )
    if st == "sent_awaiting_ack":
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/receipt'>"
            f"{ver}<input type='hidden' name='expected_delivery_id' "
            f"value='{escape(case.current_delivery_id or '')}'>"
            f"<button class='btn btn-primary btn-lg' type='submit'>客户已回执</button></form>"
        )

    tip = _PRIMARY_NEXT.get(st)
    if tip and tip[0] in allowed_vals:
        target, label, note = tip
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/transition'>"
            f"{ver}<input type='hidden' name='target' value='{escape(target)}'>"
            f"<button class='btn btn-accent btn-lg' type='submit'>{escape(label)}</button></form>"
        )
    elif not bits and allowed:
        # Fallback: only friendly Chinese options, no raw enums.
        opts = "\n".join(
            f'<option value="{escape(s.value)}">{escape(_status_zh(s.value))}</option>'
            for s in allowed
        )
        bits.append(
            f"<form method='post' action='/workbench/cases/{escape(case.id)}/transition' class='row'>"
            f"{ver}<div class='field'><label for='target'>下一步</label>"
            f"<select id='target' name='target' required>{opts}</select></div>"
            f"<button class='btn btn-accent' type='submit'>继续</button></form>"
        )
    if not bits:
        return "<p class='empty'>这一步暂时没有可点的按钮。</p>"
    return f"<div class='action-row'>{''.join(bits)}</div>"


def render_detail(
    case: CaseRecord,
    *,
    allowed: list[CaseStatus],
    message: str | None = None,
    drafts: list[object] | None = None,
    evidence_items: list[object] | None = None,
    review_board: object | None = None,
    ocr_by_evidence: dict[str, object] | None = None,
    requirement_states: list[object] | None = None,
    requirement_titles: dict[str, str] | None = None,
    advisor_items: list[object] | None = None,
) -> str:
    evidence = list(evidence_items or [])
    reqs = list(requirement_states or [])
    todos = _build_todos(
        case,
        board=review_board,
        requirement_states=reqs,
        evidence_items=evidence,
        allowed=allowed,
        advisor_items=advisor_items,
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
    if current:
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
    elif case.status.value == "draft_preparation":
        draft_html = "<p class='empty'>还没有草稿。</p>"

    content = f"""
<header class="topbar">
  <div class="brand">
    <div class="brand-mark">{escape(case.title or '未命名客户')}</div>
    <div class="brand-sub">案件详情</div>
  </div>
  <nav class="top-links">
    <a href="/workbench">← 案件列表</a>
    <a href="/workbench/inbox">顾问提醒</a>
  </nav>
</header>

{_flash(message)}

<section class="hero">
  <h1>{escape(case.title or '未命名客户')}</h1>
  <div class="hero-meta">
    <span>客户：<strong>{escape(case.customer_email or '（没填邮箱）')}</strong></span>
    <span>签证：<strong>{escape(_route_zh(case))}</strong></span>
    <span>进度：<strong>{escape(_status_zh(case.status.value))}</strong></span>
  </div>
  {route_block}
</section>

<section class="panel">
  <h2>今天要做什么</h2>
  <ul class="todo">{''.join(todo_lis)}</ul>
  <div class="primary-block">
    {_primary_action_html(case, allowed)}
  </div>
</section>

{_advisor_work_panel(
    case,
    advisor_items=advisor_items,
    evidence_items=evidence,
    ocr_by_evidence=ocr_by_evidence,
)}

<section class="panel">
  <h2>材料清单</h2>
  {_checklist_html(
      case,
      requirement_states=reqs,
      requirement_titles=requirement_titles,
      evidence_items=evidence,
      ocr_by_evidence=ocr_by_evidence,
  )}
</section>

{f'<section class="panel"><h2>给客户的邮件草稿</h2>{draft_html}</section>' if draft_html else ''}

<details class="adv">
  <summary>很少用的操作</summary>
  <div class="action-row">
    <form method="post" action="/workbench/cases/{escape(case.id)}/takeover">
      {_version_field(case)}
      <input type="hidden" name="active" value="{'false' if case.human_takeover else 'true'}">
      <button class="btn btn-ghost" type="submit">
        {'取消人工接管' if case.human_takeover else '改成人工接管'}
      </button>
    </form>
    <form method="post" action="/workbench/cases/{escape(case.id)}/bump-version">
      {_version_field(case)}
      <button class="btn btn-danger" type="submit">材料大改，旧确认作废</button>
    </form>
    <form method="post" action="/workbench/cases/{escape(case.id)}/reopen-grant">
      {_version_field(case)}
      <input type="hidden" name="expected_closure_seq" value="{case.closure_seq}">
      <button class="btn btn-ghost" type="submit">允许重开已关闭案件</button>
    </form>
  </div>
</details>
"""
    return _page(f"{case.title or '案件'}", content)
