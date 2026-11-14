"""Tiny HTML workbench — no Jinja dependency."""

from __future__ import annotations

from html import escape

from visa_agent.case_service.service import CaseRecord
from visa_agent.domain.enums import CaseStatus


def _flags(case: CaseRecord) -> str:
    bits = []
    if case.human_takeover:
        bits.append("takeover")
    if case.waiting_customer:
        bits.append("waiting_customer")
    if case.system_blocked:
        bits.append("blocked")
    if case.paused:
        bits.append("paused")
    if case.cancelled:
        bits.append("cancelled")
    return ", ".join(bits) if bits else "—"


def _version_field(case: CaseRecord) -> str:
    return (
        f'<input type="hidden" name="expected_case_version" '
        f'value="{case.case_version}">'
    )


def render_list(cases: list[CaseRecord]) -> str:
    rows = []
    for c in cases:
        rows.append(
            "<tr>"
            f"<td><a href='/workbench/cases/{escape(c.id)}'>{escape(c.id[:8])}…</a></td>"
            f"<td>{escape(c.title or '(untitled)')}</td>"
            f"<td>{escape(c.status.value)}</td>"
            f"<td>v{c.case_version}</td>"
            f"<td>{escape(c.customer_email or '—')}</td>"
            f"<td>{escape(_flags(c))}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan='6'>暂无案件</td></tr>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>工作台 · 案件列表</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem;background:#f6f4ef;color:#1a1a1a}}
a{{color:#0b5fff}} table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #ddd;padding:.5rem .75rem;text-align:left;font-size:.95rem}}
th{{background:#efeae2}} .bar{{margin-bottom:1rem;display:flex;gap:1rem;align-items:center}}
button,input,select{{font:inherit;padding:.35rem .6rem}}
</style></head><body>
<div class="bar"><strong>签证邮件助手 · 工作台</strong>
<a href="/workbench">刷新</a> <a href="/health">health</a></div>
<form method="post" action="/workbench/cases" style="margin-bottom:1.25rem;display:flex;gap:.5rem;flex-wrap:wrap">
  <input name="title" placeholder="案件标题" required>
  <input name="customer_email" placeholder="客户邮箱" type="email">
  <button type="submit">新建案件</button>
</form>
<table><thead><tr>
<th>ID</th><th>标题</th><th>状态</th><th>版本</th><th>客户</th><th>控制</th>
</tr></thead><tbody>{body}</tbody></table>
</body></html>"""


def _render_draft_block(
    draft: object, *, case_version: int, heading: str
) -> str:
    draft_text = escape(getattr(draft, "draft_text", "") or "")
    call_id = escape(getattr(draft, "model_call_id", "") or "")
    bound = int(getattr(draft, "case_version", 0) or 0)
    label = (
        f"当前草稿（绑定 v{bound}）"
        if bound == case_version
        else f"历史版本（绑定 v{bound}，当前案件 v{case_version}）"
    )
    return (
        f"<div style='margin-bottom:1rem'>"
        f"<p><strong>{escape(heading)}</strong> · {escape(label)}</p>"
        f"<p><code>model_call_id={call_id}</code> · "
        f"<code>draft_case_version={bound}</code></p>"
        f"<pre style='white-space:pre-wrap;background:#faf8f4;"
        f"padding:.75rem;border:1px solid #eee'>{draft_text}</pre>"
        f"</div>"
    )


def _render_evidence_section(
    case: CaseRecord, evidence_items: list[object]
) -> str:
    if not evidence_items:
        body = "<p>（尚无证据原件）</p>"
    else:
        rows = []
        for ev in evidence_items:
            eid = escape(getattr(ev, "id", "") or "")
            name = escape(getattr(ev, "original_filename", "") or "")
            status = escape(getattr(ev, "quarantine_status", "") or "")
            reason = escape(getattr(ev, "rejection_reason", "") or "")
            sha = escape((getattr(ev, "content_sha256", "") or "")[:12])
            rows.append(
                "<tr>"
                f"<td><code>{eid[:8]}…</code></td>"
                f"<td>{name}</td>"
                f"<td>{status}</td>"
                f"<td><code>{sha}</code></td>"
                f"<td>{reason or '—'}</td>"
                "</tr>"
                f"<tr><td colspan='5'>"
                f"<form method='post' action='/workbench/cases/{escape(case.id)}"
                f"/evidence/{eid}/annotations' style='margin:.35rem 0'>"
                f"{_version_field(case)}"
                f"<input name='field_key' placeholder='字段' required>"
                f"<input name='field_value' placeholder='人工标注值' required>"
                f"<button type='submit'>标注（升版）</button>"
                f"</form></td></tr>"
            )
        body = (
            "<table><thead><tr>"
            "<th>ID</th><th>文件</th><th>隔离</th><th>sha</th><th>原因</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return f"""<section>
<strong>证据原件（隔离存储 · 不改字节 · 无 OCR 自动确认）</strong>
{body}
<form method="post" action="/workbench/cases/{escape(case.id)}/evidence"
      style="margin-top:.75rem">
  {_version_field(case)}
  <input name="filename" placeholder="文件名.pdf" required>
  <input name="content_text" placeholder="合成演示正文（将按原文落盘）" required>
  <input name="classification" placeholder="分类（可选）" value="unclassified">
  <button type="submit">上传隔离原件</button>
</form>
</section>"""


def _badge(
    effective: bool,
    *,
    approve: bool | None = None,
    superseded: bool = False,
) -> str:
    if superseded:
        return (
            '<span style="color:#666;font-weight:600">'
            "已被同版本后续决定取代</span>"
        )
    if not effective:
        return (
            '<span style="color:#a33;font-weight:600">'
            "已失效（非当前版本）</span>"
        )
    if approve is False:
        return (
            '<span style="color:#8a5a00;font-weight:600">'
            "当前版本 · 驳回</span>"
        )
    return (
        '<span style="color:#0a7a3e;font-weight:600">'
        "当前有效</span>"
    )


def _render_review_board(
    case: CaseRecord, board: object | None
) -> str:
    if board is None:
        return ""
    # Always use board snapshot version — same source as gates/history.
    snap_ver = int(getattr(board, "case_version", case.case_version))
    conf_gate = (
        "已满足"
        if getattr(board, "customer_confirmation_effective", False)
        else "缺失 / 已失效"
    )
    if getattr(board, "final_approval_effective", False):
        appr_gate = "已满足"
    elif getattr(board, "current_final_decision", None) == "reject":
        appr_gate = "已驳回"
    else:
        appr_gate = "缺失 / 已失效"
    conf_rows = []
    for c in getattr(board, "confirmations", ()) or ():
        conf_rows.append(
            "<tr>"
            f"<td>v{int(getattr(c, 'case_version', 0))}</td>"
            f"<td>{_badge(bool(getattr(c, 'effective', False)))}</td>"
            f"<td>{escape(getattr(c, 'note', '') or '—')}</td>"
            f"<td><code>{escape((getattr(c, 'created_at', '') or '')[:19])}</code></td>"
            "</tr>"
        )
    review_rows = []
    for r in getattr(board, "reviews", ()) or ():
        decision = getattr(r, "decision", "") or ""
        effective = bool(getattr(r, "effective", False))
        superseded = bool(getattr(r, "superseded", False))
        badge = _badge(
            effective,
            approve=(decision == "approve") if effective else None,
            superseded=superseded,
        )
        review_rows.append(
            "<tr>"
            f"<td>v{int(getattr(r, 'case_version', 0))}</td>"
            f"<td>{escape(decision)}</td>"
            f"<td>{badge}</td>"
            f"<td>{escape(getattr(r, 'note', '') or '—')}</td>"
            f"<td><code>{escape((getattr(r, 'created_at', '') or '')[:19])}</code></td>"
            "</tr>"
        )
    conf_body = (
        "".join(conf_rows)
        if conf_rows
        else "<tr><td colspan='4'>（尚无客户确认）</td></tr>"
    )
    review_body = (
        "".join(review_rows)
        if review_rows
        else "<tr><td colspan='5'>（尚无最终审批）</td></tr>"
    )
    return f"""<section>
<strong>审核台 · 确认与审批（旧版失效可见）</strong>
<p style="margin:.5rem 0">当前案件 <code>v{snap_ver}</code> 门控：
客户确认 <strong>{escape(conf_gate)}</strong> ·
最终审批 <strong>{escape(appr_gate)}</strong>
</p>
<p style="color:#666;font-size:.9rem;margin:.25rem 0 0.75rem">
仅绑定当前 <code>case_version</code> 的最新决定有效；版本推进后旧确认/审批自动失效且仍可在此追溯。
</p>
<p style="margin:.5rem 0 .25rem"><em>客户确认历史</em></p>
<table><thead><tr>
<th>绑定版本</th><th>状态</th><th>备注</th><th>时间</th>
</tr></thead><tbody>{conf_body}</tbody></table>
<p style="margin:1rem 0 .25rem"><em>最终审批历史</em></p>
<table><thead><tr>
<th>绑定版本</th><th>决定</th><th>状态</th><th>备注</th><th>时间</th>
</tr></thead><tbody>{review_body}</tbody></table>
</section>"""


def render_detail(
    case: CaseRecord,
    *,
    allowed: list[CaseStatus],
    message: str | None = None,
    drafts: list[object] | None = None,
    evidence_items: list[object] | None = None,
    review_board: object | None = None,
) -> str:
    opts = "\n".join(
        f'<option value="{escape(s.value)}">{escape(s.value)}</option>'
        for s in allowed
    )
    flash = f'<p style="color:#0a7a3e">{escape(message)}</p>' if message else ""
    resume = case.resume_status.value if case.resume_status else "—"
    delivery = case.current_delivery_id or "—"
    ver = _version_field(case)
    draft_rows = drafts or []
    current = [
        d
        for d in draft_rows
        if int(getattr(d, "case_version", 0) or 0) == case.case_version
    ]
    historical = [
        d
        for d in draft_rows
        if int(getattr(d, "case_version", 0) or 0) != case.case_version
    ]
    if current:
        draft_html = _render_draft_block(
            current[0], case_version=case.case_version, heading="当前"
        )
    else:
        draft_html = (
            f"<p>（当前 v{case.case_version} 尚无模型草稿"
            + ("；下方为历史版本" if historical else "")
            + "）</p>"
        )
    if historical:
        draft_html += "".join(
            _render_draft_block(
                d, case_version=case.case_version, heading="历史"
            )
            for d in historical[:3]
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>案件 {escape(case.id[:8])}</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem;background:#f6f4ef;color:#1a1a1a;max-width:52rem}}
a{{color:#0b5fff}} section{{background:#fff;border:1px solid #e2ddd4;padding:1rem 1.25rem;margin-bottom:1rem}}
h1{{font-size:1.25rem;margin:0 0 .5rem}} dl{{display:grid;grid-template-columns:8rem 1fr;gap:.35rem 1rem;margin:0}}
dt{{color:#666}} form{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.75rem}}
button,input,select{{font:inherit;padding:.35rem .6rem}}
</style></head><body>
<p><a href="/workbench">← 案件列表</a></p>
{flash}
<section>
<h1>{escape(case.title or '(untitled)')}</h1>
<dl>
  <dt>case_id</dt><dd><code>{escape(case.id)}</code></dd>
  <dt>status</dt><dd>{escape(case.status.value)}</dd>
  <dt>case_version</dt><dd>v{case.case_version}</dd>
  <dt>route</dt><dd>{escape(case.route.value if case.route else '—')}</dd>
  <dt>customer</dt><dd>{escape(case.customer_email or '—')}</dd>
  <dt>resume_status</dt><dd>{escape(resume)}</dd>
  <dt>delivery_id</dt><dd><code>{escape(delivery)}</code></dd>
  <dt>closure_seq</dt><dd>{case.closure_seq}</dd>
  <dt>flags</dt><dd>{escape(_flags(case))}</dd>
</dl>
</section>
<section>
<strong>模型草稿（合成上下文 · 不发信）</strong>
{draft_html}
<form method="post" action="/workbench/cases/{escape(case.id)}/draft">
  <button type="submit">生成草稿</button>
</form>
</section>
{_render_evidence_section(case, evidence_items or [])}
{_render_review_board(case, review_board)}
<section>
<strong>状态迁移</strong>
<form method="post" action="/workbench/cases/{escape(case.id)}/transition">
  {ver}
  <select name="target" required>{opts or '<option value="">(无可用目标)</option>'}</select>
  <button type="submit">执行迁移</button>
</form>
</section>
<section>
<strong>人工控制</strong>
<form method="post" action="/workbench/cases/{escape(case.id)}/takeover">
  {ver}
  <input type="hidden" name="active" value="{'false' if case.human_takeover else 'true'}">
  <button type="submit">{'解除接管' if case.human_takeover else '人工接管'}</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/confirm">
  {ver}
  <button type="submit">记录客户确认（绑定 v{case.case_version}）</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/reviews">
  {ver}
  <input type="hidden" name="decision" value="approve">
  <button type="submit">最终审批通过（绑定 v{case.case_version}）</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/reviews">
  {ver}
  <input type="hidden" name="decision" value="reject">
  <button type="submit">最终审批驳回（绑定 v{case.case_version}）</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/reopen-grant">
  {ver}
  <input type="hidden" name="expected_closure_seq" value="{case.closure_seq}">
  <button type="submit">授权重开（绑定关闭轮次 #{case.closure_seq} / v{case.case_version}）</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/bump-version">
  {ver}
  <button type="submit">版本 +1（使旧确认/审批/交付失效）</button>
</form>
<form method="post" action="/workbench/cases/{escape(case.id)}/receipt">
  {ver}
  <input type="hidden" name="expected_delivery_id" value="{escape(case.current_delivery_id or '')}">
  <button type="submit">记录收件确认（绑定当前交付）</button>
</form>
</section>
</body></html>"""
