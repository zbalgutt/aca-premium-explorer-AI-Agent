import json
import os
import re
import uuid
from pathlib import Path

import uvicorn
from agents import (
    Agent,
    ModelSettings,
    Runner,
    RunHooks,
    SQLiteSession,
    ToolGuardrailFunctionOutput,
    function_tool,
    set_tracing_disabled,
    tool_input_guardrail,
)
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from pydantic import BaseModel

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# --- The Warehouse ---

# build_mappings.py loads BigQuery and writes the mappings once, offline. The app only reads.
DATA = Path(__file__).parent / "data"
MAX_ROWS = 100
MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", 5 * 10**9))  # a query that would scan more than this fails, unbilled
MODEL = os.getenv("MODEL", "vertex_ai/gemini-3.5-flash-lite")

try:
    MAPPINGS = json.loads((DATA / "aca_mappings.json").read_text())
except FileNotFoundError:
    raise SystemExit("No mappings found. Run `uv run build_mappings.py` first.")

try:
    bq = bigquery.Client(location=os.getenv("BQ_LOCATION", "US"))
except Exception as e:
    raise SystemExit(f"No Google Cloud credentials ({type(e).__name__}). Run `gcloud auth application-default login`.")

# Unqualified table names in the agent's SQL (`FROM rates`) resolve to this dataset.
DATASET = f"{bq.project}.{MAPPINGS['dataset']}"
TABLES = {t["name"]: t for t in MAPPINGS["tables"]}
YEARS = TABLES["plans"]["columns"][0]["enum_values"]  # the first column of every table is plan_year
LATEST = max(YEARS)

# The security boundary is not the prompt. It is, in order: (1) the service account, which should hold
# only BigQuery Data Viewer on this dataset plus Job User, so it cannot write anything; (2) the dry run
# below, which asks BigQuery itself whether the statement is a SELECT; (3) the bytes-billed cap.
QUERY_DEFAULTS = {"default_dataset": DATASET}

# --- Tools ---


@function_tool
def read_mappings() -> str:
    """Read the tables: what each holds, its grain, how to join them, and each column's name, type, description.

    Call this first. Call get_enum_values for an enum column's valid values.
    """
    return json.dumps({
        "joins": MAPPINGS["joins"],
        "tables": [
            {
                "name": t["name"],
                "description": t["description"],
                "grain": t["grain"],
                "rows": t["rows"],
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["type"],
                        "description": c["description"],
                        "is_enum": "enum_values" in c,
                        **({"range": c["range"]} if "range" in c else {}),
                    }
                    for c in t["columns"]
                ],
            }
            for t in MAPPINGS["tables"]
        ],
    })


def _column(table: str, column: str):
    t = TABLES.get(table)
    if t is None:
        return None, f"No table '{table}'. Tables: {', '.join(TABLES)}."
    match = next((c for c in t["columns"] if c["name"] == column), None)
    if match is None:
        return None, f"No column '{column}' in {table}. Call read_mappings for the columns."
    return match, None


@function_tool
def get_enum_values(table: str, column: str) -> str:
    """Get the exact valid values of an enum column. Match them exactly: equality is case-sensitive.

    Args:
        table: The table name, e.g. 'plans'.
        column: The column name, e.g. 'metal_level'.
    """
    match, error = _column(table, column)
    if error:
        return json.dumps({"error": error})
    if "enum_values" not in match:
        return json.dumps({"error": f"'{table}.{column}' is not an enum column. Use find_values to search it."})
    return json.dumps({"table": table, "column": column, "enum_values": match["enum_values"]}, default=str)


@function_tool
def find_values(table: str, column: str, search: str) -> str:
    """Look up the exact spelling of a name. Use it for issuers, plans and counties, which have too many
    values to list. It returns up to 25 distinct values containing the search text, ignoring case.

    Args:
        table: The table name, e.g. 'plans'.
        column: A STRING column, e.g. 'issuer_name' or 'county_name'.
        search: Text to look for, e.g. 'blue cross' or 'harris'.
    """
    match, error = _column(table, column)
    if error:
        return json.dumps({"error": error})
    if match["type"] != "STRING":
        return json.dumps({"error": f"'{column}' is not a STRING column."})
    # table and column were checked against the mappings above, so they are safe to put in the SQL.
    sql = f"SELECT DISTINCT `{column}` AS value FROM `{DATASET}.{table}` WHERE LOWER(`{column}`) LIKE @pattern ORDER BY value LIMIT 25"
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("pattern", "STRING", f"%{search.lower()}%")],
        maximum_bytes_billed=MAX_BYTES_BILLED,
    )
    try:
        values = [row.value for row in bq.query(sql, job_config=config).result()]
    except GoogleAPICallError as e:
        return json.dumps({"error": str(e)[:300]})
    return json.dumps({"matches": values})


@tool_input_guardrail
def select_only(data) -> ToolGuardrailFunctionOutput:
    """Reject anything that is not a SELECT. A friendly first line of defense: the dry run in run_sql is the real one."""
    query = json.loads(data.context.tool_arguments or "{}").get("query", "")
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", query, flags=re.DOTALL).strip()
    if not re.match(r"(?i)(select|with)\b", body) or re.search(r"(?i)\b(insert|update|delete|merge|drop|create|alter|truncate|grant|call|export|load)\b", body):
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: this data is read-only. Only SELECT queries are allowed. Tell the user you cannot change the data."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_input_guardrail
def needs_plan_year(data) -> ToolGuardrailFunctionOutput:
    """Reject queries that never mention plan_year. Every table holds three years, and a query that ignores
    the year silently averages them together."""
    query = json.loads(data.context.tool_arguments or "{}").get("query", "")
    if "plan_year" not in query.lower():
        return ToolGuardrailFunctionOutput.reject_content(
            f"Rejected: every table holds plan years {YEARS[0]} to {LATEST}. Filter on plan_year (or GROUP BY it), "
            f"and tell the user which year you used. If they gave none, use {LATEST}."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_input_guardrail
def no_placeholder_strings(data) -> ToolGuardrailFunctionOutput:
    """Reject SQL that compares against CMS placeholder text. The build turned those into NULLs."""
    query = json.loads(data.context.tool_arguments or "{}").get("query", "")
    if re.search(r"['\"](not applicable|n/?a|00000XX0000000)['\"]", query, re.IGNORECASE):
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: missing values are NULL, not 'Not Applicable' or 'N/A'. Use IS NULL / IS NOT NULL."
        )
    return ToolGuardrailFunctionOutput.allow()


@function_tool(tool_input_guardrails=[select_only, needs_plan_year, no_placeholder_strings])
def run_sql(query: str) -> str:
    """Run a BigQuery GoogleSQL query against the ACA tables and return the results.

    Tables are in the default dataset: write `FROM rates`, not a full path.

    Args:
        query: A GoogleSQL SELECT query.
    """
    try:
        # A dry run costs nothing and tells us what the statement is and how much it would scan.
        dry = bq.query(query, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, **QUERY_DEFAULTS))
        if dry.statement_type != "SELECT":
            return json.dumps({"error": f"Rejected: only SELECT is allowed, this is a {dry.statement_type}."})
        job = bq.query(query, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED, **QUERY_DEFAULTS))
        rows = job.result(max_results=MAX_ROWS + 1)
        columns = [field.name for field in rows.schema]
        rows = [list(row.values()) for row in rows]
    except GoogleAPICallError as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": str(e)[:600]})
    return json.dumps({"columns": columns, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}, default=str)


# --- The Agent ---

agent = Agent(
    name="ACA Premium Analyst",
    instructions=f"""\
You are a health insurance data analyst. Answer questions about ACA marketplace health plans and
their premiums, using CMS Exchange public use files for plan years {YEARS[0]} to {LATEST}.
The data is in BigQuery (GoogleSQL), five tables: rates, plans, benefits, service_area_counties, crosswalk.

How to work:
- Call read_mappings first. It lists the tables, how to join them, and every column.
- Before filtering on an enum column, call get_enum_values. For issuer, plan and county names, call
  find_values to get the exact spelling. Never guess values.
- Only some states are in the data: states that run their own enrollment platform are not. If a state
  has no rows, say so. Check state_code with get_enum_values if unsure.
- If the data cannot answer the question, say so plainly and say what is missing. Offer the closest
  question it can answer. Do not make up a number.

Rules for this data:
1. Year. Every table holds all years. If the user gives no year, use {LATEST} and say so. Only compare
   years when asked.
2. Scope. Unless the user asks about dental or small-group plans, filter plans to
   market_coverage = 'Individual' AND dental_only_plan = 'No'. Mention that you did.
3. Joins. rates joins plans on plan_id = standard_component_id AND plan_year. Join before filtering on
   metal level, issuer or plan type, because rates does not have them.
4. Age. Match a person's age with age_min <= X AND X <= age_max. Ages 0 to 14 share one band and 64 and
   over share one band. Without an age, use 40 and say so.
5. Tobacco. individual_rate is the rate for non-tobacco users, and for everyone when tobacco = 'No Preference'.
   individual_tobacco_rate is the smoker rate. It is NULL unless tobacco = 'Tobacco User/Non-Tobacco User'.
   To compare smokers with non-smokers, filter to that tobacco value.
6. Premiums are monthly dollars for one person, before subsidies. An average over plans is a simple
   average of listed prices, not weighted by enrollment: call it the "average listed premium".
7. Geography. Premiums vary by rating_area_id, and no file maps counties to rating areas. So you can
   answer county questions about which plans or issuers are available (service_area_counties), but not
   the premium in a county. Say so and offer the state or rating area premium instead.
8. Plan changes use crosswalk. plan_year is the year enrollees move into. Count plans with
   COUNT(DISTINCT prior_plan_id), since each plan has one row per county. A plan can appear under several
   outcomes when different counties were handled differently.
9. Benefits. benefits has one row per plan and benefit, for the standard plan variant, joined to plans on
   standard_component_id AND plan_year. Benefit names are long and exact, so call find_values on
   benefits.benefit_name first (for example search 'primary care' or 'specialist'). A benefit has either a
   copay (copay_in_network, dollars) or coinsurance (coinsurance_in_network, percent), rarely both, and the
   other is NULL. Only compare plans on the same kind. copay_unit is 'Service' (per visit or item), 'Day' or
   'Stay': a per-Day copay is charged every day, so never rank it against a per-Service copay. after_deductible says whether the member pays the
   full price until the deductible is met. Cost sharing for the Silver plans with income-based discounts
   is not in this data.
10. Missing values are NULL. Exclude them from filters, groups and aggregates.
- Answer in plain English with the numbers. Add one sentence with the year, filters and any caveat.
  Don't show the SQL unless asked.

Example queries:

-- Average listed premium: 40-year-old, Silver, one state, one year
SELECT ROUND(AVG(r.individual_rate), 2) AS avg_premium, COUNT(DISTINCT r.plan_id) AS plans
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2026 AND r.state_code = 'TX' AND r.age_min <= 40 AND 40 <= r.age_max
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No';

-- Same plans, year over year: compare the same plan in the same rating area, not different mixes of plans
SELECT a.state_code, ROUND(AVG(b.individual_rate / a.individual_rate - 1) * 100, 1) AS pct_change
FROM rates a JOIN rates b
  ON a.plan_id = b.plan_id AND a.rating_area_id = b.rating_area_id AND a.age = b.age AND a.tobacco IS NOT DISTINCT FROM b.tobacco
JOIN plans p ON b.plan_year = p.plan_year AND b.plan_id = p.standard_component_id
WHERE a.plan_year = 2026 AND b.plan_year = 2027 AND a.age = '40' AND a.individual_rate > 0
  AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
GROUP BY 1 ORDER BY pct_change DESC;

-- Benchmark: second-lowest Silver premium in each rating area
SELECT rating_area_id, rate AS benchmark_premium FROM (
  SELECT r.rating_area_id, r.individual_rate AS rate,
         ROW_NUMBER() OVER (PARTITION BY r.rating_area_id ORDER BY r.individual_rate) AS rank
  FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
  WHERE r.plan_year = 2027 AND r.state_code = 'TX' AND r.age = '40' AND p.metal_level = 'Silver'
    AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No')
WHERE rank = 2 ORDER BY rating_area_id;

-- Plan availability by county: how many counties have only one issuer
SELECT COUNT(*) AS counties FROM (
  SELECT s.county_fips
  FROM service_area_counties s JOIN plans p
    ON s.plan_year = p.plan_year AND s.issuer_id = p.issuer_id AND s.service_area_id = p.service_area_id
  WHERE s.plan_year = 2027 AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
  GROUP BY s.county_fips HAVING COUNT(DISTINCT p.issuer_id) = 1);

-- What happened to last year's plans
SELECT crosswalk_outcome, COUNT(DISTINCT prior_plan_id) AS plans
FROM crosswalk WHERE plan_year = 2027 AND dental_plan = 'N'
GROUP BY 1 ORDER BY plans DESC;

-- Cheapest primary care copay among Silver plans (benefit name from find_values)
SELECT p.issuer_name, p.plan_name, b.copay_in_network AS copay, b.after_deductible
FROM benefits b JOIN plans p ON b.plan_year = p.plan_year AND b.standard_component_id = p.standard_component_id
WHERE b.plan_year = 2027 AND p.state_code = 'TX' AND p.metal_level = 'Silver'
  AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
  AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
  AND b.is_covered = 'Covered' AND b.copay_in_network IS NOT NULL
ORDER BY copay LIMIT 10;

-- Average deductible by plan type
SELECT plan_type, ROUND(AVG(deductible_individual)) AS avg_deductible, COUNT(*) AS plans
FROM plans WHERE plan_year = 2026 AND state_code = 'TX' AND metal_level = 'Gold'
  AND market_coverage = 'Individual' AND dental_only_plan = 'No' AND deductible_individual IS NOT NULL
GROUP BY 1 ORDER BY avg_deductible;
""",
    # LiteLLM routes the call to Gemini, the same way agents-sdk-web does.
    # api_key is unused: Vertex AI authenticates with your gcloud credentials.
    model=LitellmModel(model=MODEL, api_key="unused"),
    model_settings=ModelSettings(extra_args={"vertex_location": "global"}),
    tools=[read_mappings, get_enum_values, find_values, run_sql],
)


class RecordToolCalls(RunHooks):
    """Watches the run and records each tool call, for the page to show.

    Make a new one per request: it holds this run's calls.
    """

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # For a function tool, context is a ToolContext: it carries the call's arguments.
        self.tool_calls += [{"agent": agent.name, "name": tool.name, "args": json.loads(context.tool_arguments), "result": str(result)}]


# --- Session Store ---

SESSIONS_DB = "conversations.db"

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/meta")
def meta():
    """What the page needs to describe itself: the years covered and the states in the data."""
    states = next(c for c in TABLES["rates"]["columns"] if c["name"] == "state_code").get("enum_values", [])
    return {"years": YEARS, "states": states}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Get or create the session. A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    hooks = RecordToolCalls()

    try:
        result = await Runner.run(agent, request.message, session=session, hooks=hooks, max_turns=12)
        response, tool_calls = result.final_output, hooks.tool_calls
    except Exception as e:
        # Auth, billing, a model that is not running, too many turns: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", hooks.tool_calls

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run sets PORT and needs 0.0.0.0. Locally, stay on 127.0.0.1.
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 8000)))
