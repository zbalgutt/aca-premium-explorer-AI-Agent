# ACA Premium Explorer

A Text2SQL agent for the question "what does an ACA marketplace health plan actually cost?" You ask in plain English. It writes BigQuery SQL against CMS public use files for plan years 2025 to 2027, runs it, and shows the table and the SQL it used.

**Who it is for:** people comparing marketplace plans, and analysts, journalists and students who want the real numbers behind premium headlines. It covers premiums by age, tobacco use and rating area, plan details, what each plan covers and charges per visit, which counties each plan serves, and what happened to last year's plans.

**Live demo:** https://aca-premium-explorer-ai-agent-git-707559739793.us-central1.run.app

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

**The agent (`app.py`)** has four tools: `read_mappings`, `get_enum_values`, `find_values` (looks up exact issuer, plan, county and benefit names) and `run_sql`. Its instructions carry the rules this data needs: default year, individual market medical plans only, how age and tobacco rates work, how to read copays, and what the data cannot answer. Seven example queries show the join patterns.

**Guardrails on `run_sql`:** `select_only` rejects anything that is not a SELECT. `needs_plan_year` rejects queries that ignore the year, since every table holds three years. `no_placeholder_strings` rejects comparisons against `'Not Applicable'`, because missing values are NULL. `no_county_premium` rejects joining premiums to counties, because no file maps counties to rating areas.

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

### 1. What was the average listed premium for a 40 year old tobacco user on a Silver plan in Texas in 2026?

**Expected:** `avg_tobacco_premium=952.74, plans=289`

| Plain run_sql agent | This agent |
|---|---|
| To answer the question, we can look at two possible interpretations of "premium for a tobacco user": the specific tobacco-user rate (`individual_tobacco_rate`) or the standard rate plus any tobacco surcharge. <br><br>Based on the 2026 data for Texas Silver plans for a 40-year-old:<br>* **Average Tobacco-Specific Rate (`individual_tobacco_rate`)**: **$952.74**<br>* **Average Base/Non-Tobacco Rate (`individual_rate`)**: **$789.57**<br><br>*(Note: These figures are averaged across all rating areas and plans in Texas matching these criteria for plan year 2026).* | The average listed premium for a 40-year-old tobacco user on a Silver plan in Texas in 2026 was $952.74 across 289 plans, filtering for individual market, non-dental plans, and tobacco user rates. |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT column_name, data_type 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS` 
WHERE table_name = 'rates'

-> {"rows": [{"column_name": "plan_year", "data_type": "INT64"}, {"column_name": "state_code", "data_type": "STRING"}, {"column_name": "issuer_id", "data_type": "STRING"}, {"column_name": "plan_id", "data_type": "STRING"}, {"column_name": "rating_area_id", "data_type": "STRING"}, {"column_name": "rating_area_num", "data_type": "INT64"}, {"column_name": "tobacco", "data_type": "STRING"}, {"column_name…

run_sql: 
SELECT column_name, data_type 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS` 
WHERE table_name = 'plans'

-> {"rows": [{"column_name": "plan_year", "data_type": "INT64"}, {"column_name": "state_code", "data_type": "STRING"}, {"column_name": "issuer_id", "data_type": "STRING"}, {"column_name": "issuer_name", "data_type": "STRING"}, {"column_name": "market_coverage", "data_type": "STRING"}, {"column_name": "dental_only_plan", "data_type": "STRING"}, {"column_name": "standard_component_id", "data_type": "ST…

run_sql: 
SELECT DISTINCT plan_year FROM `ieor4570-f26-zb2362.aca.rates`

-> {"rows": [{"plan_year": 2025}, {"plan_year": 2026}, {"plan_year": 2027}]}

run_sql: 
SELECT DISTINCT metal_level FROM `ieor4570-f26-zb2362.aca.plans`

-> {"rows": [{"metal_level": "Low"}, {"metal_level": "High"}, {"metal_level": "Gold"}, {"metal_level": "Silver"}, {"metal_level": "Expanded Bronze"}, {"metal_level": "Platinum"}, {"metal_level": "Catastrophic"}, {"metal_level": "Bronze"}]}

run_sql: 
SELECT DISTINCT age FROM `ieor4570-f26-zb2362.aca.rates` WHERE plan_year = 2026 AND state_code = 'TX' LIMIT 20

-> {"rows": [{"age": "0-14"}, {"age": "17"}, {"age": "22"}, {"age": "25"}, {"age": "39"}, {"age": "42"}, {"age": "63"}, {"age": "16"}, {"age": "24"}, {"age": "29"}, {"age": "30"}, {"age": "32"}, {"age": "34"}, {"age": "35"}, {"age": "36"}, {"age": "44"}, {"age": "49"}, {"age": "53"}, {"age": "58"}, {"age": "61"}]}

run_sql: 
SELECT DISTINCT tobacco FROM `ieor4570-f26-zb2362.aca.rates` WHERE plan_year = 2026 AND state_code = 'TX'

-> {"rows": [{"tobacco": "Tobacco User/Non-Tobacco User"}, {"tobacco": "No Preference"}]}

run_sql: 
SELECT 
    AVG(r.individual_rate) as avg_individual_rate,
    AVG(r.individual_tobacco_rate) as avg_individual_tobacco_rate,
    COUNT(*) as rate_count
FROM `ieor4570-f26-zb2362.aca.rates` r
JOIN `ieor4570-f26-zb2362.aca.plans` p
  ON r.plan_id = p.standard_component_id 
  AND r.plan_year = p.plan_year 
  AND r.state_code = p.state_code
WHERE r.plan_year = 2026
  AND r.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND (r.age = '40' OR (r.age_min <= 40 AND r.age_max >= 40))
  AND r.tobacco = 'Tobacco User/Non-Tobacco User'

-> {"rows": [{"avg_individual_rate": 789.5716596931657, "avg_individual_tobacco_rate": 952.7438772663879, "rate_count": 717}]}

run_sql: 
SELECT age, age_min, age_max, COUNT(*) as cnt
FROM `ieor4570-f26-zb2362.aca.rates`
WHERE plan_year = 2026 AND state_code = 'TX' AND (age = '40' OR (age_min <= 40 AND age_max >= 40))
GROUP BY age, age_min, age_max

-> {"rows": [{"age": "40", "age_min": 40, "age_max": 40, "cnt": 3992}]}

run_sql: 
SELECT 
    AVG(r.individual_rate) as avg_individual_rate,
    AVG(r.individual_tobacco_rate) as avg_individual_tobacco_rate,
    COUNT(*) as rate_count
FROM `ieor4570-f26-zb2362.aca.rates` r
JOIN `ieor4570-f26-zb2362.aca.plans` p
  ON r.plan_id = p.standard_component_id 
  AND r.plan_year = p.plan_year 
  AND r.state_code = p.state_code
WHERE r.plan_year = 2026
  AND r.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND r.age = '40'
  AND r.tobacco = 'Tobacco User/Non-Tobacco User'

-> {"rows": [{"avg_individual_rate": 789.5716596931657, "avg_individual_tobacco_rate": 952.7438772663879, "rate_count": 717}]}

run_sql: 
SELECT 
    AVG(r.individual_tobacco_rate) as avg_tobacco_rate,
    AVG(r.individual_rate) as avg_base_rate,
    COUNT(*) as num_rates
FROM `ieor4570-f26-zb2362.aca.rates` r
JOIN `ieor4570-f26-zb2362.aca.plans` p
  ON r.plan_id = p.standard_component_id 
  AND r.plan_year = p.plan_year
  AND r.state_code = p.state_code
WHERE r.plan_year = 2026
  AND r.state_code = 'TX'
  AND p.metal_level = 'Silver'
  AND r.age = '40'
  AND r.tobacco = 'Tobacco User/Non-Tobacco User'

-> {"rows": [{"avg_tobacco_rate": 952.7438772663879, "avg_base_rate": 789.5716596931657, "num_rates": 717}]}
```
</details>

<details><summary>This agent's trace</summary>

```
run_sql: SELECT ROUND(AVG(r.individual_tobacco_rate), 2) AS avg_tobacco_premium, COUNT(DISTINCT r.plan_id) AS plans
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2026 AND r.state_code = 'TX' AND r.age_min <= 40 AND 40 <= r.age_max
  AND r.tobacco = 'Tobacco User/Non-Tobacco User'
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'

-> {"columns": ["avg_tobacco_premium", "plans"], "rows": [[952.74, 289]], "truncated": false}
```
</details>

### 2. What is the average listed premium for a Silver plan in Florida?

**Expected:** `avg_premium=978.1, plans=133`

| Plain run_sql agent | This agent |
|---|---|
| Based on the available ACA dataset, the overall average listed individual premium for a Silver plan in Florida (across all years in the dataset: 2025–2027) is **$999.16**.<br><br>If broken down by plan year, the average listed premiums are:<br>* **2025:** $777.54<br>* **2026:** $1,032.17<br>* **2027:** $1,175.32<br><br>*(Note: These figures represent the average across all listed individual age rates and rating areas in Florida for Silver plans.)* | The average listed premium for a 40-year-old enrolled in an Individual Silver health plan in Florida is **$978.10** per month.<br><br>*For plan year 2027, filtering for individual-market medical plans (`market_coverage = 'Individual'` and `dental_only_plan = 'No'`) using age band 40 across all rating areas.* |

<details><summary>Plain agent trace</summary>

```
run_sql: 
SELECT
  AVG(rate) AS avg_listed_premium
FROM
  `ieor4570-f26-zb2362.aca.pff_rate` AS rate
JOIN
  `ieor4570-f26-zb2362.aca.pff_plan` AS plan
ON
  rate.plan_id = plan.plan_id
WHERE
  plan.state = 'FL'
  AND plan.metal_level = 'Silver'

-> {"error": "404 POST https://bigquery.googleapis.com/bigquery/v2/projects/ieor4570-f26-zb2362/jobs?prettyPrint=false: Not found: Table ieor4570-f26-zb2362:aca.pff_plan was not found in location US\n\nLocation: US\nJob ID: 0029e381-9551-4f07-9dfc-0d1db3e6a985\n"}

run_sql: 
SELECT table_name 
FROM `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.TABLES`

-> {"rows": [{"table_name": "crosswalk"}, {"table_name": "benefits"}, {"table_name": "plans"}, {"table_name": "service_area_counties"}, {"table_name": "rates"}]}

run_sql: 
SELECT
  column_name, table_name
FROM
  `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`
WHERE
  table_name IN ('plans', 'rates')

-> {"rows": [{"column_name": "plan_year", "table_name": "plans"}, {"column_name": "state_code", "table_name": "plans"}, {"column_name": "issuer_id", "table_name": "plans"}, {"column_name": "issuer_name", "table_name": "plans"}, {"column_name": "market_coverage", "table_name": "plans"}, {"column_name": "dental_only_plan", "table_name": "plans"}, {"column_name": "standard_component_id", "table_name": "…

run_sql: 
SELECT
  column_name, table_name
FROM
  `ieor4570-f26-zb2362.aca.INFORMATION_SCHEMA.COLUMNS`
WHERE
  table_name = 'plans'
  AND (LOWER(column_name) LIKE '%id%' OR LOWER(column_name) LIKE '%plan%')

-> {"rows": [{"column_name": "plan_year", "table_name": "plans"}, {"column_name": "issuer_id", "table_name": "plans"}, {"column_name": "dental_only_plan", "table_name": "plans"}, {"column_name": "standard_component_id", "table_name": "plans"}, {"column_name": "plan_name", "table_name": "plans"}, {"column_name": "product_id", "table_name": "plans"}, {"column_name": "network_id", "table_name": "plans"}…

run_sql: 
SELECT * 
FROM `ieor4570-f26-zb2362.aca.plans` 
LIMIT 5

-> {"rows": [{"plan_year": 2025, "state_code": "AK", "issuer_id": "21989", "issuer_name": "Delta Dental of Alaska", "market_coverage": "Individual", "dental_only_plan": "Yes", "standard_component_id": "21989AK0030001", "plan_name": "Delta Dental Premier Plan", "product_id": "21989AK003", "network_id": "AKN003", "service_area_id": "AKS003", "is_new_plan": "Existing", "plan_type": "Indemnity", "metal_l…

run_sql: 
SELECT * 
FROM `ieor4570-f26-zb2362.aca.rates` 
LIMIT 5

-> {"rows": [{"plan_year": 2027, "state_code": "WY", "issuer_id": "83964", "plan_id": "83964WY0040001", "rating_area_id": "Rating Area 1", "rating_area_num": 1, "tobacco": "No Preference", "age": "0-14", "age_min": 0, "age_max": 14, "individual_rate": 40.89, "individual_tobacco_rate": null, "couple": null, "primary_subscriber_and_one_dependent": null, "primary_subscriber_and_two_dependents": null, "p…

run_sql: 
SELECT
  AVG(r.individual_rate) AS avg_listed_premium
FROM
  `ieor4570-f26-zb2362.aca.rates` AS r
JOIN
  `ieor4570-f26-zb2362.aca.plans` AS p
ON
  r.plan_id = p.standard_component_id
  AND r.plan_year = p.plan_year
  AND r.state_code = p.state_code
WHERE
  p.state_code = 'FL'
  AND p.metal_level = 'Silver'

-> {"rows": [{"avg_listed_premium": 999.162533730095}]}

run_sql: 
SELECT
  p.plan_year,
  AVG(r.individual_rate) AS avg_listed_premium,
  COUNT(DISTINCT r.plan_id) AS num_plans
FROM
  `ieor4570-f26-zb2362.aca.rates` AS r
JOIN
  `ieor4570-f26-zb2362.aca.plans` AS p
ON
  r.plan_id = p.standard_component_id
  AND r.plan_year = p.plan_year
  AND r.state_code = p.state_code
WHERE
  p.state_code = 'FL'
  AND p.metal_level = 'Silver'
GROUP BY
  p.plan_year
ORDER BY
  p.plan_year

-> {"rows": [{"plan_year": 2025, "avg_listed_premium": 777.5361130113871, "num_plans": 134}, {"plan_year": 2026, "avg_listed_premium": 1032.1665062585973, "num_plans": 161}, {"plan_year": 2027, "avg_listed_premium": 1175.3188437647807, "num_plans": 133}]}

run_sql: 
SELECT
  p.plan_year,
  COUNT(DISTINCT p.standard_component_id) AS total_plans_in_plans,
  COUNT(DISTINCT r.plan_id) AS total_plans_in_rates
FROM
  `ieor4570-f26-zb2362.aca.plans` AS p
LEFT JOIN
  `ieor4570-f26-zb2362.aca.rates` AS r
ON
  r.plan_id = p.standard_component_id
  AND r.plan_year = p.plan_year
  AND r.state_code = p.state_code
WHERE
  p.state_code = 'FL'
  AND p.metal_level = 'Silver'
GROUP BY
  p.plan_year

-> {"rows": [{"plan_year": 2025, "total_plans_in_plans": 134, "total_plans_in_rates": 134}, {"plan_year": 2026, "total_plans_in_plans": 161, "total_plans_in_rates": 161}, {"plan_year": 2027, "total_plans_in_plans": 133, "total_plans_in_rates": 133}]}

run_sql: 
SELECT DISTINCT plan_year FROM `ieor4570-f26-zb2362.aca.plans` WHERE state_code = 'FL'

-> {"rows": [{"plan_year": 2025}, {"plan_year": 2026}, {"plan_year": 2027}]}
```
</details>

<details><summary>This agent's trace</summary>

```
read_mappings: {}
-> {"joins": ["benefits.standard_component_id = plans.standard_component_id AND benefits.plan_year = plans.plan_year", "rates.plan_id = plans.standard_component_id AND rates.plan_year = plans.plan_year", "plans.service_area_id = service_area_counties.service_area_id AND plans.issuer_id = service_area_counties.issuer_id AND plans.plan_year = service_area_counties.plan_year", "crosswalk.prior_plan_id =…

run_sql: SELECT ROUND(AVG(r.individual_rate), 2) AS avg_premium, COUNT(DISTINCT r.plan_id) AS plan_count
FROM rates r JOIN plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
WHERE r.plan_year = 2027 AND r.state_code = 'FL' AND r.age_min <= 40 AND 40 <= r.age_max
  AND p.metal_level = 'Silver' AND p.market_coverage = 'Individual' AND p.dental_only_plan = 'No'
-> {"columns": ["avg_premium", "plan_count"], "rows": [[978.1, 133]], "truncated": false}
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
- A few Florida Blue Platinum plans show listed premiums above $5,000 a month in 2026. We kept them as filed and have not verified them.
- Asked for the premium in a county such as Harris County, Texas, the agent says the files cannot tie a county to a rating area, and offers the state average or a rating area premium instead.
