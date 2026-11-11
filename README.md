# ACA Premium Explorer

A Text2SQL agent for the question "what does an ACA marketplace health plan actually cost?" You ask in plain English. It writes BigQuery SQL against CMS public use files for plan years 2025 to 2027, runs it, and shows the table and the SQL it used.

**Who it is for:** people comparing marketplace plans, and analysts, journalists and students who want the real numbers behind premium headlines. It covers premiums by age, tobacco use and rating area, plan details, what each plan covers and charges per visit, which counties each plan serves, and what happened to last year's plans.

**Live demo:** TODO (Cloud Run URL)

https://aca-premium-explorer-ai-agent-git-707559739793.us-central1.run.app

## Background: the Affordable Care Act

### What it is

The Affordable Care Act (ACA, also called Obamacare) is the 2010 federal health insurance law. It created online marketplaces where people who do not get coverage from an employer or the government can compare and buy private health plans, and it gave many of them financial help to pay for it. It also set rules that every plan sold on the individual market must follow.

### How it is structured

- **Marketplaces.** Each state is served by either the federal marketplace (HealthCare.gov) or its own state run marketplace. Insurers file their plans, and shoppers compare them during open enrollment, which in most states runs from November 1 to January 15. Outside that window you need a special enrollment period, for example after losing other coverage, having a baby or moving. The CMS files used here cover states on the federal platform, which is why states such as Massachusetts and New York are missing.
- **Metal levels.** Plans are grouped as Bronze, Silver, Gold and Platinum by how much of the average person's costs the plan pays, roughly 60%, 70%, 80% and 90%. Bronze has the lowest premium and highest deductible. Platinum is the reverse. A fifth type, Catastrophic, is limited to certain people.
- **Essential health benefits.** Every plan must cover ten categories of care, including doctor visits, hospital stays, prescriptions and maternity care. Insurers cannot turn people down or charge them more because of a pre-existing condition.
- **How premiums are set.** Insurers can set a price per rating area (a group of counties within a state), plan, age and tobacco status. Price may vary by age only up to a 3 to 1 ratio between the oldest and youngest adults, and tobacco use can raise the price by at most 1.5 times. Gender and health history cannot be used. The age bands and rating areas in the `rates` table come straight from these rules.
- **Financial help.** Premium tax credits lower the monthly premium for people with incomes from 100% to 400% of the federal poverty level. The credit is the cost of the benchmark plan (the second lowest cost Silver plan in your area) minus an expected share of your income. Silver plans also come in cost sharing reduction versions for lower income households, with lower deductibles and copays. These are the extra plan variants you see in `plans` and in the plan IDs.
- **What changed recently.** The enhanced credits that had removed the 400% income cap expired at the end of 2025, and enrollment and net premiums have moved sharply since ([KFF](https://www.kff.org/affordable-care-act/fact-sheet/midterm-election-update-the-affordable-care-act-marketplaces/)). The 2027 plan year in this dataset is the first priced under those rules for most shoppers.

### Who it is for

People who need to buy their own insurance: the self employed, early retirees, workers whose employer offers no coverage or only unaffordable coverage, and anyone between jobs. To use the marketplace you must be a U.S. citizen or a lawfully present immigrant, and you cannot enroll in a marketplace plan if you only qualify through Medicare, Medicaid, CHIP or military coverage. Credits are open to people in the 100% to 400% income range. For 2027 that is roughly $15,960 to $63,840 for one person and $33,000 to $132,000 for a family of four, and employer coverage counts as affordable when it costs under 10.22% of income ([News4JAX](https://www.news4jax.com/news/politics/2026/11/01/what-to-know-about-the-affordable-care-acts-open-enrollment-period-now-underway/)). Children can stay on a parent's plan until age 26.

### Why this matters for the agent

The CMS public files list prices before any tax credit, because the credit depends on a person's income and household. So the agent answers questions about the listed premium, plan details, benefits and service areas, and says so when a question needs your personal subsidy.

## Data

The data comes from the CMS Center for Consumer Information and Insurance Oversight, [Health Insurance Exchange Public Use Files](https://www.cms.gov/marketplace/resources/data/public-use-files). Five files per plan year, loaded into BigQuery as five tables:

| CMS file | Table | What it holds |
|---|---|---|
| Rate PUF | `rates` | Monthly premium by plan, rating area, age band and tobacco status (about 2.2M rows a year) |
| Plan Attributes PUF | `plans` | Issuer, metal level, plan type, deductible, out of pocket maximum |
| Benefits and Cost Sharing PUF | `benefits` | Which benefits each plan covers, with in-network copay and coinsurance |
| Service Area PUF | `service_area_counties` | Which counties each issuer service area covers |
| Plan ID Crosswalk PUF | `crosswalk` | Where each of last year's plans went |

The files cover states that use the federal enrollment platform. States that run their own platform, such as New York, Massachusetts and California, are not in them, so the agent says so when asked about those states. The front end lists the states that are in the data.

**To load the data yourself:** download the five files for each plan year from the CMS page above into `data/raw/<year>/`, run `gcloud auth application-default login`, then `uv run build_mappings.py`. The CMS data dictionaries are in `docs/`.

## How it works

**The offline build (`build_mappings.py`)** runs once, before the app, and does the work the model should not have to do at question time.

- It cleans the CSVs. Dollar strings like `"$450 "` become numbers. `Not Applicable` and CMS placeholders become NULL. The rate files use 0 and 9999 for "no price", and those become NULL so the cheapest plan is never $0.
- It reshapes the data for SQL. Age bands like `0-14` and `64 and over` become `age_min` and `age_max`, so any age matches a band. Statewide service areas are expanded to every county. Copays are split into an amount, a unit (service, day or stay) and whether the deductible applies first. Crosswalk reason codes become readable outcomes such as "Discontinued, no replacement".
- It keeps one standard plan variant per plan, so each plan is one row. Silver cost-sharing-reduction variants are left out.
- It describes every column from BigQuery itself: type, valid values for small columns, and min and max for numbers.
- It writes a one sentence description for each column, grounded in the real CMS definition (`data/cms_definitions.json`, extracted from the CMS data dictionaries) rather than the model's guess.
- It saves everything to `data/aca_mappings.json`, which the agent reads at question time.

**The agent (`app.py`)** has four tools: `read_mappings`, `get_enum_values`, `find_values` (looks up exact issuer, plan, county and benefit names) and `run_sql`. Its instructions carry the rules this data needs: default year, individual market medical plans only, how age and tobacco rates work, how to read copays, and what the data cannot answer. Six example queries show the join patterns.

**Guardrails on `run_sql`:** `select_only` rejects anything that is not a SELECT. `needs_plan_year` rejects queries that ignore the year, since every table holds three years. `no_placeholder_strings` rejects comparisons against `'Not Applicable'`, because missing values are NULL.

**Safety:** the prompt is not the security boundary. The service account holds only BigQuery Data Viewer on the dataset plus Job User. Every query is dry run first and rejected unless BigQuery says it is a SELECT. `maximum_bytes_billed` caps how much a query can scan, and `max_turns` caps the agent loop.

## Sample queries

<!-- SAMPLES:START -->
Run these in the live demo. Each expected answer comes from the ground-truth SQL under it, run directly in BigQuery.

### 1. What was the average listed premium for a 40 year old on a Silver plan in Texas in 2026?

**Comes from:** `rates` joined to `plans` (Rate PUF and Plan Attributes PUF)

**Expected answer:** `avg_premium=786.33, plans=303`

| Same model, no tools, no system prompt | This agent |
|---|---|
| I do not have access to specific health insurance premium data for the year 2026, as my knowledge cutoff is in January 2025, and official rate filings for 2026 are typically not finalized or published until later in 2025. | The average listed premium for a 40-year-old on an individual Silver plan in Texas in 2026 was $786.33 (across 303 plans). <br><br>This figure is based on 2026 individual market health plans with dental_only_plan set to 'No'. |

<details><summary>Ground-truth SQL</summary>

```sql
SELECT ROUND(AVG(r.individual_rate), 2) AS avg_premium, COUNT(DISTINCT r.plan_id) AS plans FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id WHERE r.plan_year = 2026 AND r.state_code = 'TX' AND r.age_min <= 40 AND 40 <= r.age_max AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
```
</details>

<details><summary>The agent's tool calls</summary>

```
run_sql: SELECT ROUND(AVG(r.individual_rate), 2) AS avg_premium, COUNT(DISTINCT r.plan_id) AS plans
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2026 AND r.state_code = 'TX' AND r.age_min <= 40 AND 40 <= r.age_max
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No';
-> {"columns": ["avg_premium", "plans"], "rows": [[786.33, 303]], "truncated": false}
```
</details>

### 2. In 2027, how many medical plans from 2026 were discontinued with no replacement?

**Comes from:** `crosswalk` (Plan ID Crosswalk PUF)

**Expected answer:** `plans_discontinued=59`

| Same model, no tools, no system prompt | This agent |
|---|---|
| It looks like you forgot to mention which specific company, state, employer, or health insurance exchange (like Healthcare.gov) you are asking about! <br><br>Could you please provide a little more context or specify the insurance provider and region? Once you share those details, I'd be happy to help you find the information. | In 2027, there were 59 medical plans from 2026 that were discontinued with no replacement. <br><br>This analysis is based on the 2027 crosswalk table for individual market medical plans (`dental_plan = 'N'`). |

<details><summary>Ground-truth SQL</summary>

```sql
SELECT COUNT(DISTINCT prior_plan_id) AS plans_discontinued FROM crosswalk WHERE plan_year = 2027 AND dental_plan = 'N' AND crosswalk_outcome = 'Discontinued, no replacement'
```
</details>

<details><summary>The agent's tool calls</summary>

```
run_sql: SELECT crosswalk_outcome, COUNT(DISTINCT prior_plan_id) AS plans
FROM crosswalk
WHERE plan_year = 2027 AND dental_plan = 'N'
GROUP BY crosswalk_outcome
-> {"columns": ["crosswalk_outcome", "plans"], "rows": [["Renewed, same plan ID", 2815], ["Renewed, new plan ID", 116], ["Moved to a different plan in the same product", 476], ["Moved to a different product", 206], ["Moved to a different issuer (set by CMS)", 606], ["Discontinued, no replacement", 59]], "truncated": false}
```
</details>

### 3. Among Silver plans in Texas in 2027, what is the lowest flat copay for a primary care visit, and how many plans charge it?

**Comes from:** `benefits` joined to `plans` (Benefits and Cost Sharing PUF and Plan Attributes PUF)

**Expected answer:** `lowest_copay=0.0, plans_at_lowest=65`

| Same model, no tools, no system prompt | This agent |
|---|---|
| I do not have access to specific data for 2027 health insurance plans in Texas. Plan availability, metal tiers, and cost-sharing details such as copays for future years like 2027 are generally not published or finalized until late 2026 when the open enrollment period approaches. Therefore, I cannot provide the lowest flat copay or the number of plans charging it for that year. | Among individual Silver plans in Texas for 2027, the lowest flat copay for a primary care visit (per service) is **$0** (no charge), which is offered by **65 plans**. <br><br>*(Note: Filters applied were plan year 2027, Texas, Individual market, and medical plans only.)* |

<details><summary>Ground-truth SQL</summary>

```sql
WITH s AS (SELECT b.copay_in_network AS copay FROM benefits b JOIN plans p ON b.plan_year = p.plan_year AND b.standard_component_id = p.standard_component_id WHERE b.plan_year = 2027 AND p.state_code = 'TX' AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No' AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness' AND b.is_covered = 'Covered' AND b.copay_in_network IS NOT NULL AND b.copay_unit = 'Service') SELECT MIN(copay) AS lowest_copay, COUNTIF(copay = (SELECT MIN(copay) FROM s)) AS plans_at_lowest FROM s
```
</details>

<details><summary>The agent's tool calls</summary>

```
read_mappings: {}
-> {"joins": ["benefits.standard_component_id = plans.standard_component_id AND benefits.plan_year = plans.plan_year", "rates.plan_id = plans.standard_component_id AND rates.plan_year = plans.plan_year", "plans.service_area_id = service_area_counties.service_area_id AND plans.issuer_id = service_area_counties.issuer_id AND plans.plan_year = service_area_counties.plan_year", "crosswalk.prior_plan_id =…

find_values: {"column": "benefit_name", "table": "benefits", "search": "primary care"}
-> {"matches": ["Primary Care Visit to Treat an Injury or Illness", "Telehealth - Primary Care", "Telehealth - Primary Care Visit"]}

run_sql: 
SELECT 
  b.copay_in_network,
  COUNT(DISTINCT p.standard_component_id) AS plan_count
FROM benefits b
JOIN plans p 
  ON b.standard_component_id = p.standard_component_id 
  AND b.plan_year = p.plan_year
WHERE b.plan_year = 2027
  AND p.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND p.market_coverage = 'Individual'
  AND p.dental_only_plan = 'No'
  AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
  AND b.is_covered = 'Covered'
  AND b.copay_in_network IS NOT NULL
  AND b.copay_unit = 'Service'
GROUP BY b.copay_in_network
ORDER BY b.copay_in_network ASC
LIMIT 5;

-> {"columns": ["copay_in_network", "plan_count"], "rows": [[0.0, 65], [5.0, 2], [10.0, 7], [15.0, 5], [20.0, 18]], "truncated": false}
```
</details>

<!-- SAMPLES:END -->

## Where a plain run_sql agent fails and this one does not

<!-- COMPARE:START -->
Both agents use the same model and the same BigQuery tables. The plain agent has only `run_sql`. This agent adds the offline build (mappings, enum values, value lookup), the domain rules and examples, and the guardrails.

### 1. What was the average listed premium for a 40 year old on a Silver plan in Texas in 2026?

**Expected:** `avg_premium=786.33, plans=303`

| Plain run_sql agent | This agent |
|---|---|
| Based on the data for 2026, the overall average listed monthly premium for a 40-year-old on a Silver plan in Texas is approximately **$786.33** (across all rating areas and tobacco status categories). <br><br>If restricted strictly to non-tobacco/no-preference rates, the average is approximately **$763.29**, while tobacco-rated plan entries average about **$789.57**. | The average listed premium for a 40-year-old on an individual Silver plan in Texas in 2026 was $786.33 (across 303 plans). <br><br>This figure is based on 2026 individual market health plans with dental_only_plan set to 'No'. |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT 
    TABLE_NAME, 
    COLUMN_NAME 
FROM 
    `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`

-> {"rows": [{"TABLE_NAME": "crosswalk", "COLUMN_NAME": "plan_year"}, {"TABLE_NAME": "crosswalk", "COLUMN_NAME": "state_code"}, {"TABLE_NAME": "crosswalk", "COLUMN_NAME": "dental_plan"}, {"TABLE_NAME": "crosswalk", "COLUMN_NAME": "prior_plan_id"}, {"TABLE_NAME": "crosswalk", "COLUMN_NAME": "prior_issuer_id"}, {"TABLE_NAME": "crosswalk", "COLUMN_NAME": "prior_metal_level"}, {"TABLE_NAME": "crosswalk",…

run_sql: 
SELECT DISTINCT plan_year FROM `ieor4570-f26-zb2362.aca.rates`

-> {"rows": [{"plan_year": 2025}, {"plan_year": 2026}, {"plan_year": 2027}]}

run_sql: 
SELECT 
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'

-> {"rows": [{"avg_premium": 786.3268704156476}]}

run_sql: 
SELECT DISTINCT age FROM `ieor4570-f26-zb2362.aca.rates` WHERE state_code = 'TX' AND plan_year = 2026

-> {"rows": [{"age": "20"}, {"age": "22"}, {"age": "25"}, {"age": "37"}, {"age": "38"}, {"age": "39"}, {"age": "40"}, {"age": "42"}, {"age": "45"}, {"age": "48"}, {"age": "50"}, {"age": "54"}, {"age": "60"}, {"age": "61"}, {"age": "62"}, {"age": "0-14"}, {"age": "18"}, {"age": "21"}, {"age": "23"}, {"age": "27"}, {"age": "41"}, {"age": "43"}, {"age": "47"}, {"age": "51"}, {"age": "55"}, {"age": "56"}…

run_sql: 
SELECT 
    COUNT(DISTINCT r.plan_id) AS num_plans,
    COUNT(r.individual_rate) AS num_rates,
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'

-> {"rows": [{"num_plans": 303, "num_rates": 818, "avg_premium": 786.3268704156476}]}

run_sql: 
SELECT 
    COUNT(DISTINCT r.plan_id) AS num_plans,
    COUNT(r.individual_rate) AS num_rates,
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'
    AND (r.tobacco = 'No Preference' OR r.tobacco = 'Non-Tobacco User' OR r.tobacco IS NULL)

-> {"rows": [{"num_plans": 14, "num_rates": 101, "avg_premium": 763.2920792079208}]}

run_sql: 
SELECT DISTINCT tobacco FROM `ieor4570-f26-zb2362.aca.rates` WHERE plan_year = 2026 AND state_code = 'TX'

-> {"rows": [{"tobacco": "Tobacco User/Non-Tobacco User"}, {"tobacco": "No Preference"}]}

run_sql: 
SELECT 
    COUNT(DISTINCT r.plan_id) AS num_plans,
    COUNT(r.individual_rate) AS num_rates,
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'
    AND r.tobacco = 'No Preference'

-> {"rows": [{"num_plans": 14, "num_rates": 101, "avg_premium": 763.2920792079208}]}

run_sql: 
SELECT 
    COUNT(DISTINCT r.plan_id) AS num_plans,
    COUNT(r.individual_rate) AS num_rates,
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'
    AND r.tobacco = 'Tobacco User/Non-Tobacco User'

-> {"rows": [{"num_plans": 289, "num_rates": 717, "avg_premium": 789.5716596931657}]}

run_sql: 
SELECT 
    AVG(r.individual_rate) AS avg_premium
FROM 
    `ieor4570-f26-zb2362.aca.rates` r
JOIN 
    `ieor4570-f26-zb2362.aca.plans` p 
    ON r.plan_year = p.plan_year 
    AND r.state_code = p.state_code 
    AND r.issuer_id = p.issuer_id 
    AND r.plan_id = p.standard_component_id
WHERE 
    r.plan_year = 2026
    AND r.state_code = 'TX'
    AND r.age = '40'
    AND p.metal_level = 'Silver'

-> {"rows": [{"avg_premium": 786.3268704156476}]}
```
</details>

<details><summary>This agent's trace</summary>

```
run_sql: SELECT ROUND(AVG(r.individual_rate), 2) AS avg_premium, COUNT(DISTINCT r.plan_id) AS plans
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2026 AND r.state_code = 'TX' AND r.age_min <= 40 AND 40 <= r.age_max
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No';
-> {"columns": ["avg_premium", "plans"], "rows": [[786.33, 303]], "truncated": false}
```
</details>

### 2. In 2027, how many medical plans from 2026 were discontinued with no replacement?

**Expected:** `plans_discontinued=59`

| Plain run_sql agent | This agent |
|---|---|
| To find the number of medical plans from 2026 that were discontinued with no replacement in 2027, we can query the `crosswalk` table filtering for `plan_year = 2027`, non-dental plans (`dental_plan = 'N'`), and the crosswalk outcome `"Discontinued, no replacement"`.<br><br>Counting the unique prior plan IDs (`prior_plan_id`) that meet these criteria:<br><br>```sql<br>SELECT COUNT(DISTINCT prior_plan_id) AS discontinued_count<br>FROM `ieor4570-f26-zb2362.aca.crosswalk`<br>WHERE plan_year = 2027 <br>  AND dental_plan = 'N' <br>  AND crosswalk_outcome = 'Discontinued, no replacement';<br>```<br><br>**Answer:**<br>There are **59** medical plans from 2026 that were discontinued with no replacement in 2027. | In 2027, there were 59 medical plans from 2026 that were discontinued with no replacement. <br><br>This analysis is based on the 2027 crosswalk table for individual market medical plans (`dental_plan = 'N'`). |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT column_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS` 
WHERE table_name = 'crosswalk'

-> {"rows": [{"column_name": "plan_year"}, {"column_name": "state_code"}, {"column_name": "dental_plan"}, {"column_name": "prior_plan_id"}, {"column_name": "prior_issuer_id"}, {"column_name": "prior_metal_level"}, {"column_name": "current_plan_id"}, {"column_name": "current_issuer_id"}, {"column_name": "current_metal_level"}, {"column_name": "county_fips"}, {"column_name": "zip_code"}, {"column_name"…

run_sql: 
SELECT column_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS` 
WHERE table_name = 'plans'

-> {"rows": [{"column_name": "plan_year"}, {"column_name": "state_code"}, {"column_name": "issuer_id"}, {"column_name": "issuer_name"}, {"column_name": "market_coverage"}, {"column_name": "dental_only_plan"}, {"column_name": "standard_component_id"}, {"column_name": "plan_name"}, {"column_name": "product_id"}, {"column_name": "network_id"}, {"column_name": "service_area_id"}, {"column_name": "is_new_…

run_sql: 
SELECT DISTINCT dental_plan, crosswalk_outcome, reason_code 
FROM `ieor4570-f26-zb2362.aca.crosswalk` 
WHERE plan_year = 2027

-> {"rows": [{"dental_plan": "N", "crosswalk_outcome": "Renewed, same plan ID", "reason_code": 0}, {"dental_plan": "N", "crosswalk_outcome": "Renewed, new plan ID", "reason_code": 1}, {"dental_plan": "N", "crosswalk_outcome": "Moved to a different plan in the same product", "reason_code": 2}, {"dental_plan": "N", "crosswalk_outcome": "Moved to a different product", "reason_code": 7}, {"dental_plan": …

run_sql: 
SELECT COUNT(DISTINCT prior_plan_id) AS discontinued_count
FROM `ieor4570-f26-zb2362.aca.crosswalk`
WHERE plan_year = 2027 
  AND dental_plan = 'N' 
  AND crosswalk_outcome = 'Discontinued, no replacement'

-> {"rows": [{"discontinued_count": 59}]}

run_sql: 
SELECT COUNT(*) AS total_rows, COUNT(DISTINCT prior_plan_id) AS distinct_plans
FROM `ieor4570-f26-zb2362.aca.crosswalk`
WHERE plan_year = 2027 
  AND dental_plan = 'N' 
  AND crosswalk_outcome = 'Discontinued, no replacement'

-> {"rows": [{"total_rows": 1544, "distinct_plans": 59}]}

run_sql: 
SELECT reason_code, COUNT(*) AS cnt, COUNT(DISTINCT prior_plan_id) AS distinct_plans
FROM `ieor4570-f26-zb2362.aca.crosswalk`
WHERE plan_year = 2027 
  AND dental_plan = 'N' 
  AND crosswalk_outcome = 'Discontinued, no replacement'
GROUP BY reason_code

-> {"rows": [{"reason_code": 10, "cnt": 1544, "distinct_plans": 59}]}

run_sql: 
SELECT crosswalk_outcome, COUNT(DISTINCT prior_plan_id) AS distinct_plans
FROM `ieor4570-f26-zb2362.aca.crosswalk`
WHERE plan_year = 2027 AND dental_plan = 'N'
GROUP BY crosswalk_outcome

-> {"rows": [{"crosswalk_outcome": "Renewed, same plan ID", "distinct_plans": 2815}, {"crosswalk_outcome": "Renewed, new plan ID", "distinct_plans": 116}, {"crosswalk_outcome": "Moved to a different plan in the same product", "distinct_plans": 476}, {"crosswalk_outcome": "Moved to a different product", "distinct_plans": 206}, {"crosswalk_outcome": "Moved to a different issuer (set by CMS)", "distinct…
```
</details>

<details><summary>This agent's trace</summary>

```
run_sql: SELECT crosswalk_outcome, COUNT(DISTINCT prior_plan_id) AS plans
FROM crosswalk
WHERE plan_year = 2027 AND dental_plan = 'N'
GROUP BY crosswalk_outcome
-> {"columns": ["crosswalk_outcome", "plans"], "rows": [["Renewed, same plan ID", 2815], ["Renewed, new plan ID", 116], ["Moved to a different plan in the same product", 476], ["Moved to a different product", 206], ["Moved to a different issuer (set by CMS)", 606], ["Discontinued, no replacement", 59]], "truncated": false}
```
</details>

### 3. Among Silver plans in Texas in 2027, what is the lowest flat copay for a primary care visit, and how many plans charge it?

**Expected:** `lowest_copay=0.0, plans_at_lowest=65`

| Plain run_sql agent | This agent |
|---|---|
| Among Silver plans in Texas for the 2027 plan year, the lowest flat copay for a primary care visit (specifically the benefit *"Primary Care Visit to Treat an Injury or Illness"*) is **$0.00** (or $0). <br><br>There are **65** plans that charge this $0 flat copay. | Among individual Silver plans in Texas for 2027, the lowest flat copay for a primary care visit (per service) is **$0** (no charge), which is offered by **65 plans**. <br><br>*(Note: Filters applied were plan year 2027, Texas, Individual market, and medical plans only.)* |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT 
    copay_amount, 
    COUNT(*) as plan_count
FROM (
    SELECT 
        benefit_name,
        state,
        plan_marketing_name,
        metal_level,
        CAST(
            REGEXP_EXTRACT(copay_ov_structured, r'\$(\d+(?:\.\d+)?)') AS FLOAT64
        ) AS copay_amount
    FROM `ieor4570-f26-zb2362.aca.benefits_cost_sharing`
    WHERE state = 'TX' 
      AND metal_level = 'Silver'
      AND (
          LOWER(benefit_name) LIKE '%primary care%' 
          OR LOWER(benefit_name) LIKE '%primary care visit%'
          OR LOWER(benefit_name) LIKE '%pcp%'
      )
      AND copay_ov_structured IS NOT NULL
      AND copay_ov_structured LIKE '%$%'
      AND LOWER(copay_ov_structured) NOT LIKE '%coinsurance%'
      AND LOWER(copay_ov_structured) NOT LIKE '%deductible%'
)
WHERE copay_amount IS NOT NULL
GROUP BY copay_amount
ORDER BY copay_amount ASC
LIMIT 5;

-> {"error": "404 POST https://bigquery.googleapis.com/bigquery/v2/projects/ieor4570-f26-zb2362/jobs?prettyPrint=false: Not found: Table ieor4570-f26-zb2362:aca.benefits_cost_sharing was not found in location US\n\nLocation: US\nJob ID: 42790eb5-308d-4c8d-b64a-16d40719659f\n"}

run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT column_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'benefits'

-> {"rows": [{"column_name": "plan_year"}, {"column_name": "state_code"}, {"column_name": "issuer_id"}, {"column_name": "standard_component_id"}, {"column_name": "benefit_name"}, {"column_name": "is_covered"}, {"column_name": "is_ehb"}, {"column_name": "copay_in_network"}, {"column_name": "copay_in_network_text"}, {"column_name": "copay_unit"}, {"column_name": "coinsurance_in_network"}, {"column_name…

run_sql: 
SELECT column_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'plans'

-> {"rows": [{"column_name": "plan_year"}, {"column_name": "state_code"}, {"column_name": "issuer_id"}, {"column_name": "issuer_name"}, {"column_name": "market_coverage"}, {"column_name": "dental_only_plan"}, {"column_name": "standard_component_id"}, {"column_name": "plan_name"}, {"column_name": "product_id"}, {"column_name": "network_id"}, {"column_name": "service_area_id"}, {"column_name": "is_new_…

run_sql: 
SELECT DISTINCT plan_year FROM `ieor4570-f26-zb2362.aca.plans`

-> {"rows": [{"plan_year": 2025}, {"plan_year": 2026}, {"plan_year": 2027}]}

run_sql: 
SELECT DISTINCT benefit_name
FROM `ieor4570-f26-zb2362.aca.benefits`
WHERE plan_year = 2027 AND state_code = 'TX'
  AND (LOWER(benefit_name) LIKE '%primary care%' OR LOWER(benefit_name) LIKE '%pcp%')

-> {"rows": [{"benefit_name": "Primary Care Visit to Treat an Injury or Illness"}]}

run_sql: 
SELECT 
    b.copay_in_network,
    b.copay_unit,
    b.copay_in_network_text,
    COUNT(DISTINCT p.standard_component_id) as plan_count
FROM `ieor4570-f26-zb2362.aca.benefits` b
JOIN `ieor4570-f26-zb2362.aca.plans` p 
  ON b.plan_year = p.plan_year 
  AND b.state_code = p.state_code 
  AND b.issuer_id = p.issuer_id 
  AND b.standard_component_id = p.standard_component_id
WHERE b.plan_year = 2027 
  AND b.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
GROUP BY b.copay_in_network, b.copay_unit, b.copay_in_network_text
ORDER BY b.copay_in_network ASC
LIMIT 15;

-> {"rows": [{"copay_in_network": null, "copay_unit": null, "copay_in_network_text": null, "plan_count": 2}, {"copay_in_network": 0.0, "copay_unit": "Service", "copay_in_network_text": "$0.00", "plan_count": 65}, {"copay_in_network": 5.0, "copay_unit": "Service", "copay_in_network_text": "$5.00", "plan_count": 2}, {"copay_in_network": 10.0, "copay_unit": "Service", "copay_in_network_text": "$10.00", …

run_sql: 
SELECT 
    b.copay_in_network,
    b.coinsurance_in_network,
    b.after_deductible,
    b.copay_in_network_text,
    COUNT(DISTINCT p.standard_component_id) as plan_count
FROM `ieor4570-f26-zb2362.aca.benefits` b
JOIN `ieor4570-f26-zb2362.aca.plans` p 
  ON b.plan_year = p.plan_year 
  AND b.state_code = p.state_code 
  AND b.issuer_id = p.issuer_id 
  AND b.standard_component_id = p.standard_component_id
WHERE b.plan_year = 2027 
  AND b.state_code = 'TX'
  AND p.metal_level = 'Silver'
  And b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
GROUP BY b.copay_in_network, b.coinsurance_in_network, b.after_deductible, b.copay_in_network_text
ORDER BY b.copay_in_network ASC;

-> {"rows": [{"copay_in_network": null, "coinsurance_in_network": 0.0, "after_deductible": "Yes", "copay_in_network_text": null, "plan_count": 2}, {"copay_in_network": 0.0, "coinsurance_in_network": null, "after_deductible": "No", "copay_in_network_text": "$0.00", "plan_count": 65}, {"copay_in_network": 5.0, "coinsurance_in_network": null, "after_deductible": "No", "copay_in_network_text": "$5.00", "…

run_sql: 
SELECT 
    b.copay_in_network,
    COUNT(DISTINCT p.standard_component_id) as plan_count
FROM `ieor4570-f26-zb2362.aca.benefits` b
JOIN `ieor4570-f26-zb2362.aca.plans` p 
  ON b.plan_year = p.plan_year 
  AND b.state_code = p.state_code 
  AND b.issuer_id = p.issuer_id 
  AND b.standard_component_id = p.standard_component_id
WHERE b.plan_year = 2027 
  AND b.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
GROUP BY b.copay_in_network
ORDER BY b.copay_in_network ASC;

-> {"rows": [{"copay_in_network": null, "plan_count": 2}, {"copay_in_network": 0.0, "plan_count": 65}, {"copay_in_network": 5.0, "plan_count": 2}, {"copay_in_network": 10.0, "plan_count": 7}, {"copay_in_network": 15.0, "plan_count": 5}, {"copay_in_network": 20.0, "plan_count": 18}, {"copay_in_network": 25.0, "plan_count": 6}, {"copay_in_network": 30.0, "plan_count": 2}, {"copay_in_network": 35.0, "pl…
```
</details>

<details><summary>This agent's trace</summary>

```
read_mappings: {}
-> {"joins": ["benefits.standard_component_id = plans.standard_component_id AND benefits.plan_year = plans.plan_year", "rates.plan_id = plans.standard_component_id AND rates.plan_year = plans.plan_year", "plans.service_area_id = service_area_counties.service_area_id AND plans.issuer_id = service_area_counties.issuer_id AND plans.plan_year = service_area_counties.plan_year", "crosswalk.prior_plan_id =…

find_values: {"column": "benefit_name", "table": "benefits", "search": "primary care"}
-> {"matches": ["Primary Care Visit to Treat an Injury or Illness", "Telehealth - Primary Care", "Telehealth - Primary Care Visit"]}

run_sql: 
SELECT 
  b.copay_in_network,
  COUNT(DISTINCT p.standard_component_id) AS plan_count
FROM benefits b
JOIN plans p 
  ON b.standard_component_id = p.standard_component_id 
  AND b.plan_year = p.plan_year
WHERE b.plan_year = 2027
  AND p.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND p.market_coverage = 'Individual'
  AND p.dental_only_plan = 'No'
  AND b.benefit_name = 'Primary Care Visit to Treat an Injury or Illness'
  AND b.is_covered = 'Covered'
  AND b.copay_in_network IS NOT NULL
  AND b.copay_unit = 'Service'
GROUP BY b.copay_in_network
ORDER BY b.copay_in_network ASC
LIMIT 5;

-> {"columns": ["copay_in_network", "plan_count"], "rows": [[0.0, 65], [5.0, 2], [10.0, 7], [15.0, 5], [20.0, 18]], "truncated": false}
```
</details>

### 4. How many individual market medical plans are offered in each metal level in Ohio in 2026?

**Expected:** `metal_level=Bronze, plans=5; metal_level=Expanded Bronze, plans=60; metal_level=Gold, plans=55; metal_level=Silver, plans=69`

| Plain run_sql agent | This agent |
|---|---|
| In 2026, the number of individual market medical (non-dental) plans offered in Ohio across each metal level is as follows:<br><br>* **Silver:** 69 plans<br>* **Expanded Bronze:** 60 plans<br>* **Gold:** 55 plans<br>* **Bronze:** 5 plans | In 2026, there are 189 individual market medical plans offered across different metal levels in Ohio: 69 Silver plans, 60 Expanded Bronze plans, 55 Gold plans, and 5 Bronze plans (filtered for Individual market, non-dental-only plans). |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT
  metal_level,
  COUNT(DISTINCT plan_id) AS num_plans
FROM
  `ieor4570-f26-zb2362.aca.pff_ohio_2026`
WHERE
  market_coverage = 'Individual'
GROUP BY
  metal_level
ORDER BY
  num_plans DESC

-> {"error": "404 POST https://bigquery.googleapis.com/bigquery/v2/projects/ieor4570-f26-zb2362/jobs?prettyPrint=false: Not found: Table ieor4570-f26-zb2362:aca.pff_ohio_2026 was not found in location US\n\nLocation: US\nJob ID: b2ccb872-2591-42ef-ba3f-a7edbca592b3\n"}

run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT 
  column_name, data_type 
FROM 
  `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS` 
WHERE 
  table_name = 'plans'

-> {"rows": [{"column_name": "plan_year", "data_type": "INT64"}, {"column_name": "state_code", "data_type": "STRING"}, {"column_name": "issuer_id", "data_type": "STRING"}, {"column_name": "issuer_name", "data_type": "STRING"}, {"column_name": "market_coverage", "data_type": "STRING"}, {"column_name": "dental_only_plan", "data_type": "STRING"}, {"column_name": "standard_component_id", "data_type": "ST…

run_sql: 
SELECT
  metal_level,
  COUNT(DISTINCT standard_component_id) AS num_plans
FROM
  `ieor4570-f26-zb2362.aca.plans`
WHERE
  plan_year = 2026
  AND state_code = 'OH'
  AND market_coverage = 'Individual'
  AND (dental_only_plan = 'No' OR dental_only_plan IS NULL)
GROUP BY
  metal_level
ORDER BY
  num_plans DESC

-> {"rows": [{"metal_level": "Silver", "num_plans": 69}, {"metal_level": "Expanded Bronze", "num_plans": 60}, {"metal_level": "Gold", "num_plans": 55}, {"metal_level": "Bronze", "num_plans": 5}]}

run_sql: 
SELECT DISTINCT dental_only_plan
FROM `ieor4570-f26-zb2362.aca.plans`
WHERE plan_year = 2026 AND state_code = 'OH'

-> {"rows": [{"dental_only_plan": "Yes"}, {"dental_only_plan": "No"}]}

run_sql: 
SELECT
  metal_level,
  COUNT(DISTINCT standard_component_id) AS num_plans
FROM
  `ieor4570-f26-zb2362.aca.plans`
WHERE
  plan_year = 2026
  AND state_code = 'OH'
  AND market_coverage = 'Individual'
  AND dental_only_plan = 'No'
GROUP BY
  metal_level
ORDER BY
  num_plans DESC

-> {"rows": [{"metal_level": "Silver", "num_plans": 69}, {"metal_level": "Expanded Bronze", "num_plans": 60}, {"metal_level": "Gold", "num_plans": 55}, {"metal_level": "Bronze", "num_plans": 5}]}
```
</details>

<details><summary>This agent's trace</summary>

```
run_sql: SELECT p.metal_level, COUNT(DISTINCT p.standard_component_id) AS plan_count
FROM plans p
WHERE p.plan_year = 2026 AND p.state_code = 'OH'
  AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
GROUP BY p.metal_level
ORDER BY plan_count DESC;
-> {"columns": ["metal_level", "plan_count"], "rows": [["Silver", 69], ["Expanded Bronze", 60], ["Gold", 55], ["Bronze", 5]], "truncated": false}
```
</details>

### 5. What is the cheapest Silver plan premium for a 70 year old in Texas in 2027?

**Expected:** `cheapest_silver_age_70=1404.33`

| Plain run_sql agent | This agent |
|---|---|
| FAILED: MaxTurnsExceeded: Max turns (12) exceeded | The cheapest Silver plan premium for a 70-year-old in Texas in 2027 is $1,404.33 per month. <br><br>This is based on individual market, non-dental Silver plans for 2027, assuming non-tobacco rates and age 70 falling within the plan's applicable age band. |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT 
    plan_rate.rate
FROM 
    `ieor4570-f26-zb2362.aca.plan_attributes` AS plan_attr
JOIN 
    `ieor4570-f26-zb2362.aca.plan_rates` AS plan_rate 
    ON plan_attr.plan_id_standardized = plan_rate.plan_id_standardized
WHERE 
    plan_attr.state_code = 'TX'
    AND plan_attr.metal_level = 'Silver'
    AND plan_rate.age = '70'
    AND plan_rate.rate_year = 2027
ORDER BY 
    plan_rate.rate ASC
LIMIT 1

-> {"error": "404 POST https://bigquery.googleapis.com/bigquery/v2/projects/ieor4570-f26-zb2362/jobs?prettyPrint=false: Not found: Table ieor4570-f26-zb2362:aca.plan_attributes was not found in location US\n\nLocation: US\nJob ID: e5f15914-fa36-4dfa-bf89-f27874beb792\n"}

run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT column_name, table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('plans', 'rates')

-> {"rows": [{"column_name": "plan_year", "table_name": "plans"}, {"column_name": "state_code", "table_name": "plans"}, {"column_name": "issuer_id", "table_name": "plans"}, {"column_name": "issuer_name", "table_name": "plans"}, {"column_name": "market_coverage", "table_name": "plans"}, {"column_name": "dental_only_plan", "table_name": "plans"}, {"column_name": "standard_component_id", "table_name": "…

run_sql: 
SELECT 
    r.individual_rate
FROM 
    `ieor4570-f26-zb2362.aca.plans` AS p
JOIN 
    `ieor4570-f26-zb2362.aca.rates` AS r 
    ON p.plan_year = r.plan_year
    AND p.state_code = r.state_code
    AND p.issuer_id = r.issuer_id
    AND p.standard_component_id = r.plan_id
WHERE 
    p.state_code = 'TX'
    AND p.plan_year = 2027
    AND p.metal_level = 'Silver'
    AND r.age = '70'
ORDER BY 
    r.individual_rate ASC
LIMIT 1

-> {"rows": []}

run_sql: 
SELECT plan_year, state_code, metal_level, count(*) 
FROM `ieor4570-f26-zb2362.aca.plans` 
GROUP BY 1, 2, 3
ORDER BY plan_year DESC
LIMIT 10

-> {"rows": [{"plan_year": 2027, "state_code": "AL", "metal_level": "Gold", "f0_": 18}, {"plan_year": 2027, "state_code": "AK", "metal_level": "Low", "f0_": 4}, {"plan_year": 2027, "state_code": "AL", "metal_level": "Expanded Bronze", "f0_": 20}, {"plan_year": 2027, "state_code": "AL", "metal_level": "Silver", "f0_": 26}, {"plan_year": 2027, "state_code": "AK", "metal_level": "Gold", "f0_": 4}, {"pla…

run_sql: 
SELECT plan_year, state_code, count(*) 
FROM `ieor4570-f26-zb2362.aca.plans` 
WHERE state_code = 'TX'
GROUP BY 1, 2
ORDER BY plan_year DESC

-> {"rows": [{"plan_year": 2027, "state_code": "TX", "f0_": 972}, {"plan_year": 2026, "state_code": "TX", "f0_": 896}, {"plan_year": 2025, "state_code": "TX", "f0_": 989}]}

run_sql: 
SELECT plan_year, state_code, count(*) 
FROM `ieor4570-f26-zb2362.aca.rates` 
WHERE state_code = 'TX'
GROUP BY 1, 2
ORDER BY plan_year DESC

-> {"rows": [{"plan_year": 2027, "state_code": "TX", "f0_": 223533}, {"plan_year": 2026, "state_code": "TX", "f0_": 203592}, {"plan_year": 2025, "state_code": "TX", "f0_": 191454}]}

run_sql: 
SELECT DISTINCT age FROM `ieor4570-f26-zb2362.aca.rates` WHERE state_code = 'TX' AND plan_year = 2027

-> {"rows": [{"age": "18"}, {"age": "19"}, {"age": "23"}, {"age": "31"}, {"age": "34"}, {"age": "38"}, {"age": "42"}, {"age": "44"}, {"age": "57"}, {"age": "60"}, {"age": "0-14"}, {"age": "16"}, {"age": "20"}, {"age": "25"}, {"age": "27"}, {"age": "37"}, {"age": "45"}, {"age": "49"}, {"age": "53"}, {"age": "55"}, {"age": "59"}, {"age": "61"}, {"age": "62"}, {"age": "64 and over"}, {"age": "21"}, {"ag…

run_sql: 
SELECT DISTINCT age FROM `ieor4570-f26-zb2362.aca.rates` WHERE state_code = 'TX' AND plan_year = 2027 AND (CAST(age AS INT64) >= 64 OR age LIKE '%over%')

-> {"error": "400 Bad int64 value: 0-14; while executing the filter on column 'age'; reason: invalidQuery, location: query, message: Bad int64 value: 0-14; while executing the filter on column 'age'\n\nLocation: US\nJob ID: fa1f3eea-29ac-4493-aac3-7cadb4a8ec77\n"}

run_sql: 
SELECT DISTINCT age FROM `ieor4570-f26-zb2362.aca.rates` WHERE state_code = 'TX' AND plan_year = 2027 AND (age = '64 and over' OR SAFE_CAST(age AS INT64) >= 64)

-> {"rows": [{"age": "64 and over"}]}

run_sql: 
SELECT 
    MIN(r.individual_rate) AS min_rate
FROM 
    `ieor4570-f26-zb2362.aca.plans` AS p
JOIN 
    `ieor4570-f26-zb2362.aca.rates` AS r 
    ON p.plan_year = r.plan_year
    AND p.state_code = r.state_code
    AND p.issuer_id = r.issuer_id
    AND p.standard_component_id = r.plan_id
WHERE 
    p.state_code = 'TX'
    AND p.plan_year = 2027
    AND p.metal_level = 'Silver'
    AND r.age = '64 and over'

-> {"rows": [{"min_rate": 1404.33}]}

run_sql: 
SELECT age_min, age_max, COUNT(*) 
FROM `ieor4570-f26-zb2362.aca.rates` 
WHERE state_code = 'TX' AND plan_year = 2027 AND age = '64 and over'
GROUP BY 1, 2

-> {"rows": [{"age_min": 64, "age_max": 120, "f0_": 4383}]}
```
</details>

<details><summary>This agent's trace</summary>

```
run_sql: SELECT MIN(r.individual_rate) AS min_premium
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2027 AND r.state_code = 'TX' AND r.age_min <= 70 AND 70 <= r.age_max
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
-> {"columns": ["min_premium"], "rows": [[1404.33]], "truncated": false}
```
</details>

<!-- COMPARE:END -->

## Run locally

```
gcloud auth application-default login
uv sync
uv run build_mappings.py     # once: loads BigQuery and writes the mappings
uv run app.py                # http://localhost:8000
```

## Limits

- Premiums are listed prices for one person before subsidies. Averages are simple averages across plans, not weighted by how many people enrolled.
- Premiums vary by rating area. No file maps counties to rating areas, so the agent answers county questions about which plans are available, not what a county pays.
- Benefits cost sharing is for the standard plan variant, in network. The discounted Silver variants are not included.
- `deductible_individual` and `moop_individual` use the standard variant, in network tier 1, on a medical or combined medical and drug basis.
- Some Florida Blue plans have very high listed premiums at older ages (above $5,000 a month). They are in the CMS files as filed, so the agent reports them as listed.
