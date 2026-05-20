# ERP Health Report - Mistral Workflows

> Parallel Odoo ERP audit -> LLM-enriched analysis -> charts -> professional PDF -> email via Himalaya.

Built with [Mistral Workflows](https://docs.mistral.ai/studio-api/workflows/overview) (public preview). Audits 6 Odoo ERP modules in parallel, feeds results to Mistral LLM for structured analysis, generates matplotlib charts, and produces a boardroom-ready PDF - end to end.

**[Download demo report ->](demo-report.pdf)**

## Demo

| Cover | Executive Summary |
|-------|-------------------|
| Dark navy gradient, score ring, KPI cards | LLM analysis + module health chart |

| Module Cards | Charts |
|-------------|--------|
| Color-coded per-module metrics with issues | Revenue, cashflow, receivables, inventory, production, headcount |

| Recommendations |
|-----------------|
| Prioritized action items with severity-colored numbering |

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [Mistral API key](https://console.mistral.ai/home?profile_dialog=api-keys)
- Odoo instance (local or remote, XML-RPC enabled)
- [Himalaya CLI](https://github.com/pimalaya/himalaya) (optional, for email)

### Install

```bash
cd my-workflow
uv sync
```

### Configure

Copy and edit `.env`:

```env
MISTRAL_API_KEY=your-api-key
SERVER_URL=https://api.mistral.ai
DEPLOYMENT_NAME=default
ODOO_URL=http://localhost:8069
ODOO_DB=odoo
ODOO_USERNAME=admin
ODOO_PASSWORD=admin
COMPANY_NAME=Sharma Furnishings
OUTPUT_DIR=./reports
EMAIL_TO=you@example.com
```

### Run

**Terminal 1 - Start worker:**

```bash
make start-worker
```

**Terminal 2 - Trigger:**

```bash
make execute-erp-health
```

Or with custom input:

```bash
make execute-erp-health input='{"odoo_url":"http://localhost:8069","odoo_db":"odoo","odoo_username":"admin","odoo_password":"admin","company_name":"Acme Corp","email_to":"ceo@acme.com"}'
```

### Output

```bash
ls reports/erp-health-report-*/
# ERP_Health_Report.pdf
# audit_data.json
```

## Project Structure

```
my-workflow/
├── src/
│   ├── workflows/
│   │   ├── erp_health.py      # ERP Health Report workflow
│   │   └── hello.py           # Hello-world example
│   ├── examples/              # Cookbook examples
│   └── entrypoints/
│       ├── worker.py          # Worker runner
│       └── start.py           # CLI trigger
├── demo-report.pdf            # Sample output
├── Makefile                   # Commands
├── pyproject.toml
├── .env                       # Credentials (gitignored)
└── README.md
```

## Workflow Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `odoo_url` | string | yes | - | Odoo instance URL |
| `odoo_db` | string | yes | - | Database name |
| `odoo_username` | string | yes | - | Odoo login |
| `odoo_password` | string | yes | - | Odoo password |
| `company_name` | string | no | `Sharma Furnishings` | Company name on report |
| `output_dir` | string | no | `./reports` | Report output directory |
| `email_to` | string | no | `""` | Email recipient (blank = skip) |

## Workflow Output

| Field | Type | Description |
|-------|------|-------------|
| `company_name` | string | Company name |
| `overall_score` | int | ERP health score (0-100) |
| `health_label` | string | `healthy`, `warning`, `critical` |
| `report_path` | string | Path to generated PDF |
| `email_sent_to` | string | Recipient if emailed |

## Activities

| # | Activity | Type | Description |
|---|----------|------|-------------|
| 1 | `erp_audit` | Parallel (6x) | Queries Odoo XML-RPC for record counts |
| 2 | `erp_build_report` | Sequential | Mistral LLM enriches audit with metrics, trends, KPIs |
| 3 | `erp_charts` | Sequential | Generates 7 matplotlib chart PNGs |
| 4 | `erp_pdf` | Sequential | HTML -> WeasyPrint -> professional A4 PDF |
| 5 | `erp_save` | Sequential | Copies PDF + JSON to output directory |
| 6 | `erp_email` | Sequential | Sends PDF via Himalaya CLI |

## Modules Audited

| Module | Odoo Model | Checks |
|--------|-----------|--------|
| CRM | `crm.lead` | Pipeline value, conversion rate, lead volume |
| Sales | `sale.order` | Revenue YTD, orders, avg order value |
| Inventory | `stock.quant` | Stock levels, low stock, overstock |
| Accounting | `account.move` | Receivables, payables, DSO, cashflow |
| HR | `hr.employee` | Headcount, attendance, payroll |
| Manufacturing | `mrp.production` | Orders, capacity, defects |

## PDF Sections

1. **Cover** - Company name, score ring, health badge, 6 KPI cards
2. **Executive Summary** - LLM analysis + module health chart
3. **Module Cards** - 6 per-module pages with metrics, issues, status
4. **Charts** - Revenue trend, cashflow, receivables aging, inventory, production, headcount
5. **Recommendations** - Prioritized actions with severity-colored numbering

## Email

Uses [Himalaya](https://github.com/pimalaya/himalaya) CLI for sending. Configure once:

```bash
# ~/.config/himalaya/config.toml
[accounts.gmail]
email = "you@gmail.com"
...
```

The workflow sends from `aisentinel087@gmail.com`. Edit the `send_report_email` activity to change the sender.

## Dependencies

```
mistralai-workflows>=3.4.0   # Workflow SDK + Mistral plugin
matplotlib>=3.10             # Chart generation
weasyprint>=68               # HTML -> PDF rendering
fpdf2>=2.8                   # Included, not used by default
```

## Makefile Commands

```bash
make start-worker         # Start Temporal worker
make execute-erp-health   # Run ERP health report
make execute              # Run hello-world example
make start-examples       # Worker for cookbook examples
```

## Architecture

```
[entrypoints/start.py] --> [Mistral SDK client] --> [Temporal Scheduler (SaaS)]
                                                          |
[entrypoints/worker.py] <-- [Mistral SDK worker] <-------+
        |
        +--> Odoo XML-RPC (audit queries)
        +--> Mistral API (LLM enrichment)
        +--> WeasyPrint (PDF rendering)
        +--> Himalaya CLI (email)
```

The worker runs locally and connects to Mistral's hosted Temporal scheduler at `wf-scheduler.mistral.ai:443`. Activities execute in the worker's Python environment.

## Contest

Built for the [Mistral Workflows Public Preview contest](https://mistr.al/4wqQEAE).

**SDK features used:**
- `execute_activities_in_parallel` - 6 Odoo audits concurrently
- `ChatCompletionRequest` - LLM-structured analysis
- `mistralai_chat_complete` - Direct API plugin integration
- External orchestration - Odoo XML-RPC, Matplotlib, WeasyPrint, Himalaya
