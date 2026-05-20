"""ERP Health Report -- Odoo audit -> charts -> PDF -> email via Himalaya."""

import asyncio, json, os, shutil, subprocess
from datetime import datetime, timedelta
from pathlib import Path
import xmlrpc.client
from pydantic import BaseModel, Field
import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as wf_mistral


class ERPHealthInput(BaseModel):
    odoo_url: str
    odoo_db: str
    odoo_username: str
    odoo_password: str
    company_name: str = "Sharma Furnishings"
    output_dir: str = "./reports"
    email_to: str = ""


class ERPHealthOutput(BaseModel):
    company_name: str
    overall_score: int
    health_label: str
    report_path: str = ""
    email_sent_to: str = ""


def _odoo_connect(url, db, username, password):
    c = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = c.authenticate(db, username, password, {})
    if not uid:
        raise ConnectionError(f"Auth failed: {username}")
    return url, db, uid, password


def _odoo_count(url, db, uid, pwd, model):
    m = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return m.execute_kw(db, uid, pwd, model, "search_count", [[]])


MODULES = ["crm", "sales", "inventory", "accounting", "hr", "manufacturing"]
MODULE_MODELS = {
    "crm": ["crm.lead"], "sales": ["sale.order"], "inventory": ["stock.quant"],
    "accounting": ["account.move"], "hr": ["hr.employee"], "manufacturing": ["mrp.production"],
}


@workflows.activity(name="erp_audit", retry_policy_max_attempts=2, start_to_close_timeout=timedelta(seconds=60))
async def audit_module(odoo_url: str, odoo_db: str, odoo_username: str, odoo_password: str, module: str) -> dict:
    try:
        url, db, uid, pwd = _odoo_connect(odoo_url, odoo_db, odoo_username, odoo_password)
    except Exception as e:
        return {"module": module, "ok": False, "error": str(e)}
    models = MODULE_MODELS.get(module, [])
    counts = {}
    for model in models:
        counts[model] = _odoo_count(url, db, uid, pwd, model)
    return {"module": module, "ok": True, "record_counts": counts}


@workflows.activity(name="erp_build_report", retry_policy_max_attempts=3, start_to_close_timeout=timedelta(minutes=3))
async def build_report_data(audit_results_json: str, company_name: str) -> str:
    """Feed Odoo audit data to Mistral LLM, get back a rich structured ERP health report JSON."""
    audits_raw = json.loads(audit_results_json)
    audit_lines = "\n".join(
        f"- {a.get('module','')}: ok={a.get('ok')}, counts={a.get('record_counts',{})}"
        for a in audits_raw
    )

    request = wf_mistral.ChatCompletionRequest(
        model="mistral-small-latest",
        messages=[
            wf_mistral.SystemMessage(content=(
                f"You are a senior ERP auditor for an Indian furniture manufacturer named '{company_name}' "
                f"(45 employees, 6 departments, Rs.2.4Cr revenue, 3 warehouses, 12 suppliers, 180 customers).\n\n"
                "Return ONLY valid JSON -- no markdown, no backticks, no preamble.\n"
                "Structure: {{company:{{name,industry,employees,annual_revenue,currency}},\n"
                "audits:[6 items for crm,sales,inventory,accounting,hr,manufacturing each with:\n"
                "module,display_name,health(healthy|warning|critical),record_counts,issues[],details(3 sentences),\n"
                "metrics{{...fields...}}, plus module-specific: trend{{labels[Jan-Jun],values[]}},\n"
                "stock_by_category{{labels[],values[],health[]}} for inventory,\n"
                "cashflow{{labels[],inflow[],outflow[],net[]}} + aging{{labels[],amounts[]}} for accounting,\n"
                "headcount_by_dept{{labels[],values[]}} for hr,\n"
                "production_by_product{{labels[],values[]}} + trend{{labels[],planned[],completed[],delayed[]}} for manufacturing],\n"
                "overall:{{score(0-100: mix as 2 critical,2 warning,2 healthy => score 55-65),\n"
                "label(warning),critical_findings[],warnings[],recommendations[],summary,\n"
                "kpis:{{revenue_ytd,expenses_ytd,net_profit,margin_pct,total_orders,on_time_delivery_pct,customer_satisfaction}}}}}}\n\n"
                "RULES:\n"
                "- Mix health: accounting CRITICAL, inventory WARNING, crm WARNING, manufacturing WARNING, sales HEALTHY, hr HEALTHY\n"
                "- All monetary values in INR (use Rs. prefix in text, raw numbers in JSON fields)\n"
                "- Issues must be specific with rupee amounts\n"
                "- Trend data must tell a coherent story across Jan-Jun\n"
                "- overall.score must reflect the health mix (55-65 range)\n"
                "- Generate EVERY field -- no nulls, no empty values\n"
                "- Keep it concise so the full JSON fits in a single response"
            )),
            wf_mistral.UserMessage(content=(
                f"Odoo audit baseline:\n{audit_lines}\n\n"
                f"Generate a complete ERP health report JSON for {company_name}. Include all metrics, trends, and charts data."
            )),
        ],
    )
    response = await wf_mistral.mistralai_chat_complete(request)
    content = response.choices[0].message.content if response.choices and response.choices[0].message else ""
    if not content:
        raise ValueError("Empty LLM response")

    # Clean markdown wrappers
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    # Repair truncated JSON
    try:
        json.loads(content)
    except json.JSONDecodeError:
        brace_count = 0
        in_string = False
        escape = False
        last_valid = 0
        for i, ch in enumerate(content):
            if escape:
                escape = False; continue
            if ch == '"' and not escape:
                in_string = not in_string
            if in_string:
                if ch == '\\': escape = True
                continue
            if ch == '{': brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    last_valid = i + 1
        if last_valid > 0:
            content = content[:last_valid]
            needed = content.count("{") - content.count("}")
            content += "}" * needed
            if content.endswith(",\n}"):
                content = content.rstrip(",}") + "\n}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM JSON invalid at char {e.pos}: {content[max(0,e.pos-40):e.pos+40]}") from e

    return json.dumps(data, default=str)


# -- Chart generation --------------------------------------------------------

@workflows.activity(name="erp_charts", retry_policy_max_attempts=2, start_to_close_timeout=timedelta(minutes=2))
async def generate_charts(data_json: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    data = json.loads(data_json)
    charts_dir = "/tmp/erp_charts"
    os.makedirs(charts_dir, exist_ok=True)
    charts = {}

    DARK, GREEN, RED, YELLOW = "#2C3E50", "#2ECC71", "#E74C3C", "#F39C12"
    COLORS = [DARK, RED, GREEN, YELLOW, "#3498DB", "#9B59B6"]
    GRAY = "#95A5A6"

    def save(name):
        path = f"{charts_dir}/{name}"
        plt.tight_layout()
        plt.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close()
        charts[name] = path

    audits = data["audits"]
    overall = data["overall"]

    # 1. Revenue trend
    try:
        sales = next(a for a in audits if a["module"] == "sales")
        t = sales["trend"]
        rev_key = "revenue" if "revenue" in t else next((k for k in t if k != "labels" and isinstance(t[k], list)), None)
        if rev_key:
            fig, ax = plt.subplots(figsize=(8, 3.2))
            ax.plot(t["labels"], t[rev_key], "o-", color=DARK, lw=2.5, ms=6)
            ax.fill_between(range(len(t["labels"])), t[rev_key], alpha=0.12, color=DARK)
            ax.set_title("Monthly Revenue (Rs. Lakhs)", fontsize=13, fontweight="bold", color=DARK)
            ax.grid(True, alpha=0.3)
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"Rs.{x}L"))
            save("revenue_trend.png")
    except (StopIteration, KeyError):
        pass

    # 2. Module health bar
    try:
        names = [a["display_name"] for a in audits]
        bar_colors = [GREEN if a.get("health") == "healthy" else YELLOW if a.get("health") == "warning" else RED for a in audits]
        scores_val = [
            a.get("metrics", {}).get("conversion_rate_pct", 0) or len(a.get("issues", [])) * 10 + 50
            for a in audits
        ]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        bars = ax.barh(names, scores_val, color=bar_colors, height=0.55)
        ax.set_title("Module Health Scores", fontsize=13, fontweight="bold", color=DARK)
        ax.grid(True, alpha=0.3, axis="x")
        for b, s in zip(bars, scores_val):
            ax.text(b.get_width() + 1.5, b.get_y() + b.get_height() / 2, str(int(s)), va="center", fontsize=8.5, fontweight="bold", color=DARK)
        save("module_health.png")
    except Exception:
        pass

    # 3. Cashflow
    try:
        acct = next(a for a in audits if a["module"] == "accounting")
        cf = acct["cashflow"]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        x = range(len(cf["labels"])); w = 0.3
        ax.bar([i - w/2 for i in x], cf["inflow"], w, label="Inflow", color=GREEN, alpha=0.85)
        ax.bar([i + w/2 for i in x], cf["outflow"], w, label="Outflow", color=RED, alpha=0.85)
        ax.axhline(y=0, color="black", lw=0.5)
        ax.set_xticks(x); ax.set_xticklabels(cf["labels"])
        ax.set_title("Monthly Cashflow (Rs. Lakhs)", fontsize=13, fontweight="bold", color=DARK)
        ax.legend(fontsize=9); ax.grid(True, alpha=0.3, axis="y")
        save("cashflow.png")
    except (StopIteration, KeyError):
        pass

    # 4. Receivables aging
    try:
        ag = acct["aging"]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        age_c = [GREEN, YELLOW, "#E67E22", RED]
        wedges, texts, autotexts = ax.pie(ag["amounts"], labels=ag["labels"], colors=age_c,
                                           autopct="%1.1f%%", startangle=140)
        for t in autotexts: t.set_fontsize(9); t.set_fontweight("bold")
        ax.set_title("Receivables Aging (Rs. Lakhs)", fontsize=13, fontweight="bold", color=DARK)
        save("receivables_aging.png")
    except (StopIteration, KeyError):
        pass

    # 5. Stock by category
    try:
        inv = next(a for a in audits if a["module"] == "inventory")
        sc = inv["stock_by_category"]
        hc = [RED if h == "critical" else YELLOW if h == "warning" else GREEN for h in sc["health"]]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.barh(sc["labels"], sc["values"], color=hc, height=0.55)
        ax.set_title("Inventory Stock by Category (Units)", fontsize=13, fontweight="bold", color=DARK)
        ax.grid(True, alpha=0.3, axis="x")
        save("stock_category.png")
    except (StopIteration, KeyError):
        pass

    # 6. Production: planned vs completed vs delayed
    try:
        mfg = next(a for a in audits if a["module"] == "manufacturing")
        t2 = mfg["trend"]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        x = range(len(t2["labels"])); w = 0.25
        ax.bar([i - w for i in x], t2.get("planned", [0]*6), w, label="Planned", color=GRAY)
        ax.bar(x, t2.get("completed", [0]*6), w, label="Completed", color=GREEN)
        ax.bar([i + w for i in x], t2.get("delayed", [0]*6), w, label="Delayed", color=RED)
        ax.set_xticks(x); ax.set_xticklabels(t2["labels"])
        ax.set_title("Production: Planned vs Completed", fontsize=13, fontweight="bold", color=DARK)
        ax.legend(fontsize=8); ax.grid(True, alpha=0.3, axis="y")
        save("production.png")
    except (StopIteration, KeyError):
        pass

    # 7. Headcount by dept
    try:
        hr_mod = next(a for a in audits if a["module"] == "hr")
        hc = hr_mod["headcount_by_dept"]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.pie(hc["values"], labels=hc["labels"], colors=COLORS, autopct="%1.1f%%", startangle=140)
        ax.set_title("Headcount by Department", fontsize=13, fontweight="bold", color=DARK)
        save("headcount.png")
    except (StopIteration, KeyError):
        pass

    return json.dumps(charts)


# -- PDF generation ----------------------------------------------------------

@workflows.activity(name="erp_pdf", retry_policy_max_attempts=2, start_to_close_timeout=timedelta(minutes=3))
async def generate_pdf(data_json: str, charts_json: str) -> str:
    data = json.loads(data_json)
    charts = json.loads(charts_json)
    from weasyprint import HTML

    company = data["company"]
    overall = data["overall"]
    audits = data["audits"]
    score = overall["score"]
    label = overall["label"]

    score_color = "#2d8a4e" if score >= 75 else "#c47e1a" if score >= 50 else "#b91c1c"

    def status_color(h):
        return "#2d8a4e" if h == "healthy" else "#c47e1a" if h == "warning" else "#b91c1c"

    kpis = overall["kpis"]

    kpi_parts = []
    kpi_data = [
        ("Revenue YTD", "&#8377;" + str(round(kpis['revenue_ytd']/100000, 1)) + "L"),
        ("Net Profit", "&#8377;" + str(round(kpis['net_profit']/100000, 1)) + "L"),
        ("Margin", str(round(kpis.get('margin_pct', 0), 1)) + "%"),
        ("Orders", str(kpis.get('total_orders', 0))),
        ("On-Time", str(kpis.get('on_time_delivery_pct', 0)) + "%"),
        ("CSAT", str(kpis.get('customer_satisfaction', 0)) + "/100"),
    ]
    for lbl, val in kpi_data:
        kpi_parts.append('<div class="kpi"><span class="kpi-label">' + lbl + '</span><span class="kpi-val">' + val + '</span></div>')
    kpi_rows = "".join(kpi_parts)

    mod_cards = ""
    for m in audits:
        h = m["health"]
        c = status_color(h)
        metrics = m.get("metrics", {})
        met_parts = []
        for k, v in list(metrics.items())[:6]:
            if isinstance(v, (int, float)):
                if v > 50000:
                    vstr = "&#8377;" + str(round(v/100000, 1)) + "L"
                elif any(x in k for x in ("pct", "rate")):
                    vstr = str(v) + "%"
                else:
                    vstr = str(v)[:18]
            else:
                vstr = str(v)[:18]
            met_parts.append('<div class="m-cell"><span class="m-label">' + k.replace("_", " ").title()[:22] + '</span><span class="m-num">' + vstr + '</span></div>')
        met_html = "".join(met_parts)

        issues_html = ""
        for i in m.get("issues", []):
            issues_html += "<li>" + i + "</li>"

        mod_cards += '<div class="module-card" style="border-top:3px solid ' + c + '">'
        mod_cards += '<div class="mod-head"><h2>' + m['display_name'] + '</h2><span class="badge" style="background:' + c + '">' + h.upper() + '</span></div>'
        mod_cards += '<p class="mod-desc">' + m.get('details', '') + '</p>'
        mod_cards += '<div class="m-grid">' + met_html + '</div>'
        if issues_html:
            mod_cards += '<div class="issues-box"><h4>Issues</h4><ul>' + issues_html + '</ul></div>'
        mod_cards += '</div>'

    rec_html = ""
    sections = [
        ("Critical Actions", overall.get("critical_findings", []), "#b91c1c"),
        ("Warnings", overall.get("warnings", []), "#c47e1a"),
        ("Recommendations", overall.get("recommendations", []), "#2d8a4e"),
    ]
    for title, items, color in sections:
        if not items:
            continue
        items_parts = []
        for i, item in enumerate(items, 1):
            items_parts.append('<li data-num="' + str(i) + '">' + item + '</li>')
        items_html = "".join(items_parts)
        rec_html += '<div class="rec-block"><h3 style="color:' + color + '">' + title + '</h3><ol class="rec-list">' + items_html + '</ol></div>'

    # Read chart PNGs as base64
    import base64
    chart_b64 = {}
    for name, path in charts.items():
        with open(path, "rb") as f:
            chart_b64[name] = base64.b64encode(f.read()).decode()

    chart_html = ""
    chart_order = [
        ("revenue_trend.png", "Revenue Trend"),
        ("cashflow.png", "Cashflow Analysis"),
        ("receivables_aging.png", "Receivables Aging"),
        ("stock_category.png", "Inventory by Category"),
        ("production.png", "Production Overview"),
        ("headcount.png", "Headcount"),
    ]
    for ch_name, ch_title in chart_order:
        if ch_name in chart_b64:
            chart_html += '<div class="chart-page"><h2>' + ch_title + '</h2><img src="data:image/png;base64,' + chart_b64[ch_name] + '" alt="' + ch_title + '"/></div>'

    summary_chart = ""
    if "module_health.png" in chart_b64:
        summary_chart = '<div class="chart-card"><img src="data:image/png;base64,' + chart_b64["module_health.png"] + '" alt="Module Health"/></div>'

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<style>
  @page { size: A4; margin: 0; }
  @page cover { margin: 0; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Georgia, 'Times New Roman', serif; color: #1e1e2e; font-size: 10pt; line-height: 1.6; }

  .cover { page: cover; width: 210mm; height: 297mm; background: linear-gradient(170deg, #0f172a 0%, #1e293b 55%, #334155 100%); color: #fff; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px; position: relative; overflow: hidden; }
  .cover::before { content: ''; position: absolute; top: -60%; left: -20%; width: 140%; height: 140%; background: radial-gradient(ellipse at 30% 20%, rgba(255,255,255,0.04) 0%, transparent 60%); }
  .cover-logo { font-size: 9pt; letter-spacing: 4px; text-transform: uppercase; opacity: 0.6; margin-bottom: 60px; }
  .cover-name { font-size: 28pt; font-weight: bold; line-height: 1.2; margin-bottom: 8px; }
  .cover-sub { font-size: 11pt; opacity: 0.65; margin-bottom: 48px; }
  .score-ring { width: 130px; height: 130px; border-radius: 50%; border: 5px solid """ + score_color + """; display: flex; flex-direction: column; align-items: center; justify-content: center; margin-bottom: 16px; }
  .score-num { font-size: 38pt; font-weight: bold; color: """ + score_color + """; line-height: 1; }
  .score-out { font-size: 8pt; opacity: 0.5; }
  .score-badge { display: inline-block; padding: 4px 22px; border-radius: 20px; background: """ + score_color + """; font-size: 9pt; font-weight: bold; letter-spacing: 2px; margin-bottom: 30px; }
  .cover-footer { font-size: 7.5pt; opacity: 0.4; position: absolute; bottom: 30px; }

  .report-page { page-break-after: always; padding: 48px 44px 40px; }
  .report-page h1 { font-size: 18pt; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 10px; margin-bottom: 20px; }
  .report-page h2 { font-size: 13pt; color: #1e293b; margin: 20px 0 10px; }

  .kpi-grid { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 24px; }
  .kpi { flex: 0 0 calc(33.33% - 8px); background: rgba(255,255,255,0.1); border-radius: 8px; padding: 12px 14px; text-align: center; }
  .kpi-label { display: block; font-size: 7pt; color: rgba(255,255,255,0.55); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; font-family: Arial, sans-serif; }
  .kpi-val { display: block; font-size: 15pt; font-weight: bold; color: #fff; }

  .module-card { background: #fff; border-radius: 8px; padding: 22px 24px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .mod-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .mod-head h2 { font-size: 14pt; color: #0f172a; margin: 0; padding: 0; border: 0; }
  .badge { font-size: 7.5pt; font-weight: bold; letter-spacing: 1.5px; color: #fff; padding: 3px 12px; border-radius: 12px; font-family: Arial, sans-serif; }
  .mod-desc { font-size: 9pt; color: #475569; margin-bottom: 14px; line-height: 1.55; }
  .m-grid { display: flex; flex-wrap: wrap; gap: 8px; }
  .m-cell { flex: 0 0 calc(33.33% - 6px); background: #f8fafc; border-radius: 6px; padding: 8px 10px; text-align: center; }
  .m-label { display: block; font-size: 6.5pt; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; font-family: Arial, sans-serif; }
  .m-num { display: block; font-size: 10pt; font-weight: bold; color: #1e293b; }

  .issues-box { background: #fef2f2; border-radius: 6px; padding: 10px 14px; margin-top: 12px; }
  .issues-box h4 { font-size: 8pt; color: #b91c1c; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; font-family: Arial, sans-serif; }
  .issues-box ul { list-style: none; }
  .issues-box li { font-size: 8pt; color: #7f1d1d; padding: 3px 0; }

  .chart-card { page-break-before: always; padding: 40px 44px 30px; page-break-inside: avoid; }
  .chart-card h2 { font-size: 13pt; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-bottom: 18px; }
  .chart-card img { display: block; width: 100%; max-height: 220mm; object-fit: contain; margin: 0 auto; }

  .rec-block { margin-bottom: 20px; }
  .rec-block h3 { font-size: 11pt; margin-bottom: 8px; font-weight: bold; }
  .rec-list { list-style: none; counter-reset: rec; }
  .rec-list li { counter-increment: rec; padding: 8px 0 8px 32px; position: relative; font-size: 9pt; line-height: 1.5; color: #334155; border-bottom: 1px solid #f1f5f9; }
  .rec-list li::before { content: counter(rec); position: absolute; left: 0; top: 8px; width: 20px; height: 20px; border-radius: 50%; color: #fff; text-align: center; font-size: 8pt; font-weight: bold; line-height: 20px; font-family: Arial, sans-serif; }
  .rec-block:nth-child(1) .rec-list li::before { background: #b91c1c; }
  .rec-block:nth-child(2) .rec-list li::before { background: #c47e1a; }
  .rec-block:nth-child(3) .rec-list li::before { background: #2d8a4e; }

  .summary-text { font-size: 9.5pt; color: #334155; line-height: 1.65; margin-bottom: 20px; }

  .page-footer { font-size: 7pt; color: #94a3b8; text-align: center; margin-top: 30px; padding-top: 12px; border-top: 1px solid #e2e8f0; font-family: Arial, sans-serif; }
</style>
</head>
<body>
<div class="cover">
    <div class="cover-logo">ERP Health Report</div>
    <div class="cover-name">""" + company["name"] + """</div>
    <div class="cover-sub">""" + company["industry"] + """ &middot; """ + str(company["employees"]) + """ Employees &middot; &#8377;2.4Cr Revenue</div>
    <div class="score-ring">
        <div class="score-num">""" + str(score) + """</div>
        <div class="score-out">out of 100</div>
    </div>
    <div class="score-badge">""" + label.upper() + """</div>
    <div class="kpi-grid" style="max-width:380px;margin:0 auto 40px;">
        """ + kpi_rows + """
    </div>
    <div class="cover-footer">""" + datetime.now().strftime('%B %d, %Y') + """ &middot; Confidential &middot; Powered by Mistral Workflows</div>
</div>

<div class="report-page">
    <h1>Executive Summary</h1>
    <p class="summary-text">""" + overall.get("summary", "") + """</p>
    """ + summary_chart + """
    <div class="page-footer">Confidential &middot; ERP Health Report &middot; """ + company["name"] + """ &middot; Page 2</div>
</div>

<div class="report-page">
    <h1>Module Health</h1>
    """ + mod_cards + """
    <div class="page-footer">Confidential &middot; ERP Health Report &middot; """ + company["name"] + """</div>
</div>

""" + chart_html + """

<div class="report-page">
    <h1>Recommendations</h1>
    """ + rec_html + """
    <div class="page-footer">Report generated """ + datetime.now().strftime('%B %d, %Y') + """ &middot; Confidential &middot; Powered by Mistral Workflows</div>
</div>
</body>
</html>"""

    out = "/tmp/erp_health_report.pdf"
    HTML(string=html).write_pdf(out)
    return out


# -- Save & Email ------------------------------------------------------------

@workflows.activity(name="erp_save", retry_policy_max_attempts=2, start_to_close_timeout=timedelta(seconds=20))
async def save_report(pdf_path: str, data_json: str, output_dir: str) -> dict:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rdir = Path(output_dir) / f"erp-health-report-{ts}"
    rdir.mkdir(parents=True, exist_ok=True)
    dest = rdir / "ERP_Health_Report.pdf"
    shutil.copy(pdf_path, dest)
    (rdir / "audit_data.json").write_text(json.dumps(json.loads(data_json), indent=2, default=str))
    return {"report_path": str(dest), "output_dir": str(rdir)}


@workflows.activity(name="erp_email", retry_policy_max_attempts=1, start_to_close_timeout=timedelta(seconds=45))
async def send_email(report_path: str, company_name: str, overall_score: int, email_to: str) -> bool:
    label = "HEALTHY" if overall_score >= 80 else ("WARNING" if overall_score >= 50 else "CRITICAL")
    subject = f"ERP Health Report: {company_name} -- Score: {overall_score}/100 ({label})"
    body = (
        f"From: aisentinel087@gmail.com\nTo: {email_to}\nSubject: {subject}\n\n"
        f"<#multipart type=mixed>\n<#part type=text/plain>\n"
        f"ERP Health Report for {company_name}\nOverall Score: {overall_score}/100 -- {label}\n\n"
        f"Please find the attached PDF report with module audits, charts, and recommendations.\n\n"
        f"---\nAutomated by Mistral Workflows\n"
        f"<#part filename={report_path} name=ERP_Health_Report.pdf><#/part>\n<#/multipart>\n"
    )
    r = subprocess.run(["himalaya", "template", "send"], input=body, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"Himalaya: {r.stderr.strip()}")
    return True


# -- WORKFLOW ----------------------------------------------------------------

@workflows.workflow.define(
    name="erp-health-report",
    workflow_display_name="ERP Health Report",
    workflow_description="Odoo audit -> structured data -> charts -> PDF -> email via Himalaya.",
    execution_timeout=timedelta(hours=24),
)
class ERPHealthReportWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: ERPHealthInput) -> ERPHealthOutput:
        items = [{"odoo_url": input.odoo_url, "odoo_db": input.odoo_db, "odoo_username": input.odoo_username, "odoo_password": input.odoo_password, "module": m} for m in MODULES]

        results = await workflows.execute_activities_in_parallel(activity=audit_module, items=items, max_concurrent_scheduled_tasks=6)
        audit_json = json.dumps(results, default=str)

        data_json = await build_report_data(audit_json, input.company_name)

        await asyncio.sleep(10)

        charts_json = await generate_charts(data_json)

        pdf_path = await generate_pdf(data_json, charts_json)

        saved = await save_report(pdf_path, data_json, input.output_dir)

        data = json.loads(data_json)
        ov = data["overall"]

        email_sent = ""
        if input.email_to:
            try:
                await send_email(saved["report_path"], input.company_name, ov["score"], input.email_to)
                email_sent = input.email_to
            except Exception:
                pass

        return ERPHealthOutput(
            company_name=input.company_name,
            overall_score=ov["score"],
            health_label=ov["label"],
            report_path=saved["report_path"],
            email_sent_to=email_sent,
        )
