"""Build the BigQuery tables and the schema mappings. Run once, offline, before app.py.

    uv run build_mappings.py              # load CSVs -> BigQuery, then write mappings
    uv run build_mappings.py --skip-load  # tables already loaded: only rebuild the mappings

1. Clean the CMS Exchange PUF CSVs (placeholders become NULL, "$450 " becomes 450.0, age bands
   become age_min/age_max, 14-character plan IDs line up across files) and load them into
   BigQuery as four tables: rates, plans, service_area_counties, crosswalk.
2. Describe every column from BigQuery itself: type, row counts, valid values (enums), ranges.
3. Ask the model for a one-sentence description of each column, grounded in the real CMS
   definition (data/cms_definitions.json) instead of its own guesses.
4. Write it all to data/aca_mappings.json, the context app.py retrieves.
"""

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

import litellm
import numpy as np
import pandas as pd
from google.cloud import bigquery

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = DATA / "raw"
MAPPINGS_PATH = DATA / "aca_mappings.json"
COUNTY_NAMES_PATH = DATA / "county_names.txt"
COUNTY_NAMES_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"

YEARS = [2025, 2026, 2027]
DATASET = os.getenv("BQ_DATASET", "aca")
LOCATION = os.getenv("BQ_LOCATION", "US")
MODEL = os.getenv("MODEL", "vertex_ai/gemini-3.5-flash-lite")
MAX_ENUM_VALUES = 60
CHUNK_ROWS = 500_000  # the rate files have ~2M rows each: stream them

# --- What goes into each table ---
# (column, BigQuery type, source column in the CSV or None if derived, description or None).
# A description given here is used as-is. Otherwise the model writes one from the CMS definition.

PLAN_YEAR = ("plan_year", "INT64", "BusinessYear", "Plan year (2025, 2026 or 2027). Every table has it. Always filter on it.")

TABLES = {
    "rates": {
        "kind": "rate",
        "description": "Monthly premium for each plan, rating area, age band and tobacco status. The main table.",
        "grain": "One row per plan_year x plan_id x rating_area_id x age x tobacco. Join to plans for metal level, issuer, deductible.",
        "columns": [
            PLAN_YEAR,
            ("state_code", "STRING", "StateCode", None),
            ("issuer_id", "STRING", "IssuerId", None),
            ("plan_id", "STRING", "PlanId", "14-character HIOS plan ID. Joins to plans.standard_component_id (plus plan_year)."),
            ("rating_area_id", "STRING", "RatingAreaId", "Rating area within the state, like 'Rating Area 3'. Numbers restart in every state. Premiums vary by rating area, not county."),
            ("rating_area_num", "INT64", None, "The number in rating_area_id, as an integer."),
            ("tobacco", "STRING", "Tobacco", "'No Preference' means one rate for everyone. 'Tobacco User/Non-Tobacco User' means the plan has separate smoker and non-smoker rates. NULL for family-tier rows."),
            ("age", "STRING", "Age", "Age band exactly as CMS reports it: '0-14', '15' ... '63', '64 and over', or 'Family Option'. To match a person's age use age_min and age_max instead."),
            ("age_min", "INT64", None, "Lowest age in the band. '0-14' is 0, '64 and over' is 64. NULL for 'Family Option'."),
            ("age_max", "INT64", None, "Highest age in the band. '0-14' is 14, '64 and over' is 120. NULL for 'Family Option'. Match an age with age_min <= X AND X <= age_max."),
            ("individual_rate", "FLOAT64", "IndividualRate", "Monthly premium in dollars for one person, before subsidies. NULL when the plan has no price for that row (CMS placeholders 0 and 9999 were removed). It is the non-tobacco rate when the plan prices tobacco separately, and the rate for everyone when tobacco is 'No Preference'."),
            ("individual_tobacco_rate", "FLOAT64", "IndividualTobaccoRate", "Monthly premium in dollars for a tobacco user. Only filled when tobacco = 'Tobacco User/Non-Tobacco User', otherwise NULL."),
            ("couple", "FLOAT64", "Couple", None),
            ("primary_subscriber_and_one_dependent", "FLOAT64", "PrimarySubscriberAndOneDependent", None),
            ("primary_subscriber_and_two_dependents", "FLOAT64", "PrimarySubscriberAndTwoDependents", None),
            ("primary_subscriber_and_three_or_more_dependents", "FLOAT64", "PrimarySubscriberAndThreeOrMoreDependents", None),
            ("couple_and_one_dependent", "FLOAT64", "CoupleAndOneDependent", None),
            ("couple_and_two_dependents", "FLOAT64", "CoupleAndTwoDependents", None),
            ("couple_and_three_or_more_dependents", "FLOAT64", "CoupleAndThreeOrMoreDependents", None),
            ("rate_effective_date", "DATE", "RateEffectiveDate", None),
            ("rate_expiration_date", "DATE", "RateExpirationDate", None),
        ],
    },
    "plans": {
        "kind": "plan_attributes",
        "description": "One row per plan: issuer, metal level, plan type, deductible, out-of-pocket max. Standard (non cost-sharing-reduction) variant only.",
        "grain": "One row per plan_year x standard_component_id.",
        "columns": [
            PLAN_YEAR,
            ("state_code", "STRING", "StateCode", None),
            ("issuer_id", "STRING", "IssuerId", None),
            ("issuer_name", "STRING", "IssuerMarketPlaceMarketingName", None),
            ("market_coverage", "STRING", "MarketCoverage", "'Individual' (the ACA marketplace) or 'SHOP (Small Group)' (small employers)."),
            ("dental_only_plan", "STRING", "DentalOnlyPlan", "'Yes' for stand-alone dental plans, 'No' for medical plans. Filter to 'No' for health insurance questions."),
            ("standard_component_id", "STRING", "StandardComponentId", "14-character HIOS plan ID. Joins to rates.plan_id and crosswalk prior/current plan ids (plus plan_year)."),
            ("plan_name", "STRING", "PlanMarketingName", None),
            ("product_id", "STRING", "HIOSProductId", None),
            ("network_id", "STRING", "NetworkId", None),
            ("service_area_id", "STRING", "ServiceAreaId", "Joins to service_area_counties (with issuer_id and plan_year) to find the counties the plan covers."),
            ("is_new_plan", "STRING", "IsNewPlan", None),
            ("plan_type", "STRING", "PlanType", None),
            ("metal_level", "STRING", "MetalLevel", "Bronze, Silver, Gold, Platinum or Catastrophic for medical plans. 'High' and 'Low' are dental plans only."),
            ("exchange_type", "STRING", "QHPNonQHPTypeId", "'On Exchange', 'Off Exchange' or 'Both'."),
            ("child_only_offering", "STRING", "ChildOnlyOffering", None),
            ("is_hsa_eligible", "STRING", "IsHSAEligible", None),
            ("deductible_individual", "FLOAT64", None, "Individual in-network (tier 1) deductible in dollars for the standard plan variant. Medical deductible, or the combined medical and drug deductible when the plan combines them. NULL when not applicable."),
            ("moop_individual", "FLOAT64", None, "Individual in-network (tier 1) maximum out-of-pocket in dollars for the standard plan variant, same medical or combined basis as deductible_individual. NULL when not applicable."),
        ],
    },
    "service_area_counties": {
        "kind": "service_area",
        "description": "Which counties each issuer service area covers. Statewide service areas are expanded to every county in the state.",
        "grain": "One row per plan_year x issuer_id x service_area_id x county_fips.",
        "columns": [
            PLAN_YEAR,
            ("state_code", "STRING", "StateCode", None),
            ("issuer_id", "STRING", "IssuerId", None),
            ("service_area_id", "STRING", "ServiceAreaId", None),
            ("service_area_name", "STRING", "ServiceAreaName", None),
            ("county_fips", "STRING", None, "Five-digit county FIPS code, like '48201' for Harris County, TX."),
            ("county_name", "STRING", None, "County name from the Census Bureau FIPS list, like 'Harris County'. Can be NULL if the FIPS code is not in that list."),
            ("covers_entire_state", "STRING", "CoverEntireState", "'Yes' when the issuer lists the whole state (counties were expanded by the build), 'No' when it listed counties."),
            ("partial_county", "STRING", "PartialCounty", "'Yes' when only some zip codes of the county are covered. NULL for statewide service areas."),
            ("market_coverage", "STRING", "MarketCoverage", None),
            ("dental_only_plan", "STRING", "DentalOnlyPlan", None),
        ],
    },
    "crosswalk": {
        "kind": "crosswalk",
        "description": "Where enrollees in last year's plans were moved this year. Use it for year-over-year plan changes, discontinued plans and replacements.",
        "grain": "One row per prior plan x county (x zip code for partial counties). plan_year is the year enrollees move INTO, so plan_year 2027 maps 2026 plans to 2027 plans.",
        "columns": [
            ("plan_year", "INT64", None, "The year enrollees move into. plan_year 2027 means a 2026 plan crosswalked to a 2027 plan."),
            ("state_code", "STRING", "State", None),
            ("dental_plan", "STRING", "DentalPlan", "'Y' for stand-alone dental plans, 'N' for medical plans."),
            ("prior_plan_id", "STRING", None, "14-character plan ID in plan_year - 1. Joins to plans.standard_component_id with plan_year - 1."),
            ("prior_issuer_id", "STRING", None, None),
            ("prior_metal_level", "STRING", None, None),
            ("current_plan_id", "STRING", None, "14-character plan ID in plan_year. NULL when the plan was discontinued with no replacement."),
            ("current_issuer_id", "STRING", None, "NULL when there is no replacement."),
            ("current_metal_level", "STRING", None, "NULL when there is no replacement."),
            ("county_fips", "STRING", "FIPSCode", "Five-digit county FIPS code the crosswalk applies to."),
            ("zip_code", "STRING", "ZipCode", "'00000' means the whole county."),
            ("crosswalk_level", "INT64", "CrosswalkLevel", "0 same plan ID, 1 plan ID level, 2 plan ID and county level, 3 zip-code level."),
            ("reason_code", "INT64", "ReasonForCrosswalk", "CMS reason code, 0 to 10. See crosswalk_outcome for a readable version."),
            ("crosswalk_outcome", "STRING", None, "Readable outcome from reason_code. Count plans with COUNT(DISTINCT prior_plan_id) because one plan has many county rows."),
        ],
    },
    "benefits": {
        "kind": "benefits",
        "description": "Which benefits each plan covers and what the member pays in network (copay or coinsurance), for the standard plan variant.",
        "grain": "One row per plan_year x standard_component_id x benefit_name. Join to plans on standard_component_id and plan_year.",
        "columns": [
            PLAN_YEAR,
            ("state_code", "STRING", "StateCode", "Two-letter state abbreviation."),
            ("issuer_id", "STRING", "IssuerId", "Five-digit HIOS issuer ID."),
            ("standard_component_id", "STRING", "StandardComponentId", "14-character HIOS plan ID. Joins to plans.standard_component_id (plus plan_year)."),
            ("benefit_name", "STRING", "BenefitName", "The benefit exactly as filed, like 'Primary Care Visit to Treat an Injury or Illness', 'Specialist Visit' or 'Preferred Brand Drugs'. There are too many names to list: call find_values to get the exact spelling."),
            ("is_covered", "STRING", "IsCovered", "'Covered' or 'Not Covered'. The cost sharing columns are NULL when the benefit is not covered."),
            ("is_ehb", "STRING", "IsEHB", "'Yes' when the benefit is an essential health benefit."),
            ("copay_in_network", "FLOAT64", None, "Fixed in-network (tier 1) copay in dollars. 0 for 'No Charge'. NULL when the benefit uses coinsurance instead, or is not covered."),
            ("copay_in_network_text", "STRING", None, "The in-network copay exactly as filed, like '$50.00 Copay after deductible' or 'No Charge'. NULL when not applicable."),
            ("copay_unit", "STRING", None, "What the copay is charged per: 'Day' or 'Stay' for hospital-type benefits, otherwise 'Service' (a visit or an item). Only compare copays that share a unit. A per-Day copay is charged for every day. NULL when there is no copay."),
            ("coinsurance_in_network", "FLOAT64", None, "In-network (tier 1) coinsurance: the percent of the cost the member pays, so 50 means 50%. 0 means no charge. NULL when the benefit uses a copay instead, or is not covered."),
            ("coinsurance_in_network_text", "STRING", None, "The in-network coinsurance exactly as filed, like '50.00% Coinsurance after deductible'. NULL when not applicable."),
            ("after_deductible", "STRING", None, "'Yes' when the in-network cost sharing applies only after the deductible is met, 'No' when it applies from the first visit. NULL when the benefit has no cost sharing."),
            ("quantity_limit", "STRING", "QuantLimitOnSvc", "'Yes' when the plan limits how much of the benefit it covers."),
            ("limit_qty", "FLOAT64", "LimitQty", "Size of the limit, like 35 for 35 visits."),
            ("limit_unit", "STRING", "LimitUnit", "What limit_qty counts, like 'Visit(s) per Benefit Period' or 'Days per Benefit Period'."),
        ],
    },
}

JOINS = [
    "benefits.standard_component_id = plans.standard_component_id AND benefits.plan_year = plans.plan_year",
    "rates.plan_id = plans.standard_component_id AND rates.plan_year = plans.plan_year",
    "plans.service_area_id = service_area_counties.service_area_id AND plans.issuer_id = service_area_counties.issuer_id AND plans.plan_year = service_area_counties.plan_year",
    "crosswalk.prior_plan_id = plans.standard_component_id AND crosswalk.plan_year - 1 = plans.plan_year (the old plan)",
    "crosswalk.current_plan_id = plans.standard_component_id AND crosswalk.plan_year = plans.plan_year (the new plan)",
]

# Crosswalk reason codes, from the CMS data dictionary (2027 definitions).
OUTCOMES = {
    0: "Renewed, same plan ID",
    1: "Renewed, new plan ID",
    2: "Moved to a different plan in the same product",
    3: "Moved to a different product",
    6: "Moved to a different product",
    7: "Moved to a different product",
    8: "Moved to a different issuer (set by CMS)",
    4: "Discontinued, no replacement",
    5: "Discontinued, no replacement",
    10: "Discontinued, no replacement",
}
NO_PLAN = "00000XX0000000"  # CMS placeholder for "no plan"

DEDUCTIBLE_COLS = ["TEHBDedInnTier1Individual", "MEHBDedInnTier1Individual", "TEHBDedCombInnOonIndividual", "MEHBDedCombInnOonIndividual"]
MOOP_COLS = ["TEHBInnTier1IndividualMOOP", "MEHBInnTier1IndividualMOOP", "TEHBCombInnOonIndividualMOOP", "MEHBCombInnOonIndividualMOOP"]

# --- Cleaning ---


def money(s: pd.Series) -> pd.Series:
    """'$450 ' -> 450.0, '65.0' -> 65.0, 'Not Applicable' / 'per person not applicable' -> NaN."""
    digits = s.astype("string").str.replace(",", "", regex=False).str.extract(r"(\d+(?:\.\d+)?)")[0]
    return pd.to_numeric(digits, errors="coerce")


def convert(s: pd.Series, bq_type: str) -> pd.Series:
    if bq_type == "STRING":
        return s.astype("string").str.strip().replace("", pd.NA)
    if bq_type == "INT64":
        return pd.to_numeric(s, errors="coerce").astype("Int64")
    if bq_type == "FLOAT64":
        return money(s)
    if bq_type == "DATE":
        d = pd.to_datetime(s, format="mixed", errors="coerce").dt.date  # the files mix 2025-01-01 and 1/1/2026
        return d.where(d.notna(), None)
    raise ValueError(bq_type)


def finish(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Convert every column to its BigQuery type, in schema order. Missing source columns become NULL."""
    out = pd.DataFrame(index=df.index)
    for name, bq_type, source, _ in TABLES[table]["columns"]:
        if name in df.columns:
            raw = df[name]
        elif source and source in df.columns:
            raw = df[source]
        else:
            raw = pd.Series(pd.NA, index=df.index, dtype="string")
        out[name] = convert(raw, bq_type)
    return out


def age_bounds(age: pd.Series) -> tuple[pd.Series, pd.Series]:
    a = age.astype("string").str.strip()
    lo = pd.to_numeric(a.str.extract(r"^(\d+)")[0], errors="coerce")
    band_hi = pd.to_numeric(a.str.extract(r"^\d+-(\d+)$")[0], errors="coerce")
    hi = lo.where(band_hi.isna(), band_hi)
    hi = hi.where(~a.str.contains("over", case=False, na=False), 120)
    return lo.astype("Int64"), hi.astype("Int64")


def first_money(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    out = pd.Series(np.nan, index=df.index)
    for c in columns:
        if c in df.columns:
            out = out.fillna(money(df[c]))
    return out


def clean_rates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["age_min"], df["age_max"] = age_bounds(df["Age"])
    df["rating_area_num"] = pd.to_numeric(df["RatingAreaId"].str.extract(r"(\d+)")[0], errors="coerce")
    out = finish(df, "rates")
    # CMS files use 0 and 9999 as "no rate" placeholders. Left in, they make the cheapest plan $0.
    for col in RATE_COLUMNS:
        out[col] = out[col].mask((out[col] <= 0) | (out[col] >= 9999))
    return out


def standard_variant(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Keep one standard plan variant per key. PlanId is the 14-character plan plus a variant like '-01'.
    '-00' is the standard off-exchange variant and '-01' the standard on-exchange one. The rest are
    cost-sharing-reduction variants of Silver and similar. Prefer '-00', and fall back to '-01' so plans
    sold only on the exchange are not lost."""
    plan = df["PlanId"].fillna("").str.strip()
    df = df[plan.str.endswith(("-00", "-01"))].copy()
    df["_pref"] = plan[df.index].str.endswith("-01").astype(int)
    return df.sort_values("_pref", kind="stable").drop_duplicates(keys).drop(columns="_pref")


def clean_plans(df: pd.DataFrame) -> pd.DataFrame:
    df = standard_variant(df, ["BusinessYear", "StandardComponentId"])
    df["deductible_individual"] = first_money(df, DEDUCTIBLE_COLS)
    df["moop_individual"] = first_money(df, MOOP_COLS)
    return finish(df, "plans")


def clean_benefits(df: pd.DataFrame) -> pd.DataFrame:
    df = standard_variant(df, ["BusinessYear", "StandardComponentId", "BenefitName"])

    def text(col):  # 'Not Applicable' is not a value: it becomes NULL
        t = df[col].astype("string").str.strip()
        return t.mask(t.str.lower().eq("not applicable").fillna(False).astype(bool))

    def no_charge(t):
        return t.str.contains("no charge", case=False, na=False).astype(bool)

    copay, coins = text("CopayInnTier1"), text("CoinsInnTier1")
    df["copay_in_network_text"], df["coinsurance_in_network_text"] = copay, coins
    df["copay_in_network"] = money(copay).mask(no_charge(copay), 0.0)
    # '$3000.00 Copay per Day', '$950.00 Copay per Stay after deductible': the dollar amount means different things.
    unit = copay.str.extract(r"(?i)\bper\s+(\w+)")[0].str.title()
    df["copay_unit"] = unit.fillna("Service").astype("string").mask(copay.isna())
    df["coinsurance_in_network"] = pd.to_numeric(coins.str.extract(r"(\d+(?:\.\d+)?)\s*%")[0], errors="coerce").mask(no_charge(coins), 0.0)
    both = copay.fillna("") + " " + coins.fillna("")
    after = both.str.contains(r"(?:after|with)\s+(?:the\s+)?deductible", case=False).astype(bool)
    df["after_deductible"] = after.map({True: "Yes", False: "No"}).astype("string").mask(copay.isna() & coins.isna())
    return finish(df, "benefits")


def clean_crosswalk(df: pd.DataFrame, year: int) -> pd.DataFrame:
    df = df.copy()
    prior, current = year - 1, year
    for new, old in {
        "prior_plan_id": f"PlanID_{prior}",
        "prior_issuer_id": f"IssuerID_{prior}",
        "prior_metal_level": f"MetalLevel_{prior}",
        "current_plan_id": f"PlanID_{current}",
        "current_issuer_id": f"IssuerID_{current}",
        "current_metal_level": f"MetalLevel_{current}",
    }.items():
        df[new] = df[old]
    df["plan_year"] = year
    reasons = pd.to_numeric(df["ReasonForCrosswalk"], errors="coerce")
    df["crosswalk_outcome"] = reasons.map(OUTCOMES).fillna("Other")
    no_plan = df["current_plan_id"] == NO_PLAN
    df.loc[no_plan, ["current_plan_id", "current_issuer_id", "current_metal_level"]] = None
    df["zip_code"] = df["ZipCode"].astype("string").str.strip().str.zfill(5)
    df["FIPSCode"] = fips(df["FIPSCode"])
    return finish(df, "crosswalk")


def fips(s: pd.Series) -> pd.Series:
    """Zero-pad numeric county codes to five digits ('1001' -> '01001'). Leave anything else alone."""
    s = s.astype("string").str.strip()
    digits = s.str.fullmatch(r"\d+").fillna(False).astype(bool)
    return s.where(~digits, s.str.zfill(5))


def county_names() -> dict[str, str]:
    """FIPS -> county name from the Census Bureau list. Cached in data/. Empty if unreachable."""
    try:
        if not COUNTY_NAMES_PATH.exists():
            with urllib.request.urlopen(COUNTY_NAMES_URL, timeout=30) as r:
                COUNTY_NAMES_PATH.write_bytes(r.read())
        c = pd.read_csv(COUNTY_NAMES_PATH, sep="|", dtype=str, encoding="latin-1")
        return dict(zip(c["STATEFP"] + c["COUNTYFP"], c["COUNTYNAME"]))
    except Exception as e:
        print(f"  warning: no county names ({type(e).__name__}: {e}). county_name will be NULL.")
        print(f"  To fix it, save {COUNTY_NAMES_URL} as {COUNTY_NAMES_PATH} and rerun.")
        return {}


def clean_service_areas(raw: pd.DataFrame, counties_seen: pd.DataFrame) -> pd.DataFrame:
    """Expand 'covers the entire state' rows into one row per county, so county questions work."""
    df = raw.copy()
    df["County"] = fips(df["County"])
    statewide = df[df["CoverEntireState"].str.strip().str.lower() == "yes"].drop(columns="County")
    statewide = statewide.merge(counties_seen, on="StateCode", how="left")
    listed = df[df["CoverEntireState"].str.strip().str.lower() != "yes"].rename(columns={"County": "county_fips"})
    expanded = pd.concat([statewide, listed], ignore_index=True)
    expanded["county_name"] = expanded["county_fips"].map(county_names())
    expanded = expanded[expanded["county_fips"].notna()]
    out = finish(expanded, "service_area_counties")
    return out.drop_duplicates()


# --- Reading the CSVs ---


def find(kind: str, year: int) -> Path:
    hits = [p for p in sorted(RAW.rglob(f"{kind}-puf-{year}.csv")) if "urr" not in p.parts]
    if not hits:
        raise SystemExit(f"Missing {kind}-puf-{year}.csv under {RAW}")
    return hits[0]


def read_csv(path: Path, **kwargs):
    # keep_default_na=False: only blanks are missing. 'NA' and 'N/A' are text, not NaN.
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding_errors="replace", **kwargs)


# --- Loading into BigQuery ---


def schema_of(table: str) -> list[bigquery.SchemaField]:
    return [bigquery.SchemaField(name, bq_type) for name, bq_type, _, _ in TABLES[table]["columns"]]


def load(client: bigquery.Client, df: pd.DataFrame, table: str, first: bool):
    config = bigquery.LoadJobConfig(
        schema=schema_of(table),
        write_disposition="WRITE_TRUNCATE" if first else "WRITE_APPEND",
    )
    if first and table == "rates":
        # Every question filters on plan_year and most on state. Partitioning and clustering
        # keep the bytes scanned (and so the bill) small.
        config.range_partitioning = bigquery.RangePartitioning(
            field="plan_year", range_=bigquery.PartitionRange(start=2014, end=2040, interval=1)
        )
        config.clustering_fields = ["state_code", "plan_id"]
    client.load_table_from_dataframe(df, f"{client.project}.{DATASET}.{table}", job_config=config).result()


ALL_TABLES = ["plans", "crosswalk", "service_area_counties", "benefits", "rates"]


def build_tables(client: bigquery.Client, only: list[str] | None = None):
    want = set(only or ALL_TABLES)
    client.create_dataset(bigquery.Dataset(f"{client.project}.{DATASET}"), exists_ok=True)

    if "plans" in want:
        print("plans")
        plans = pd.concat([clean_plans(read_csv(find("plan-attributes", y), usecols=lambda c: c in PLAN_SOURCES)) for y in YEARS])
        load(client, plans, "plans", first=True)
        print(f"  {len(plans):,} rows")

    if want & {"crosswalk", "service_area_counties"}:
        crosswalk_raw = {y: read_csv(find("plan-id-crosswalk", y)) for y in YEARS}
    if "crosswalk" in want:
        print("crosswalk")
        crosswalk = pd.concat([clean_crosswalk(df, y) for y, df in crosswalk_raw.items()])
        load(client, crosswalk, "crosswalk", first=True)
        print(f"  {len(crosswalk):,} rows")

    if "service_area_counties" in want:
        print("service_area_counties")
        areas = pd.concat([read_csv(find("service-area", y)) for y in YEARS])
        listed = areas.loc[areas["County"].notna(), ["StateCode", "County"]].rename(columns={"County": "county_fips"})
        from_crosswalk = pd.concat(crosswalk_raw.values())[["State", "FIPSCode"]].rename(columns={"State": "StateCode", "FIPSCode": "county_fips"})
        counties_seen = pd.concat([listed, from_crosswalk]).dropna()
        counties_seen["county_fips"] = fips(counties_seen["county_fips"])
        counties_seen = counties_seen.drop_duplicates()
        service_areas = clean_service_areas(areas, counties_seen)
        load(client, service_areas, "service_area_counties", first=True)
        print(f"  {len(service_areas):,} rows")
        odd = service_areas["county_fips"].str.fullmatch(r"\d{5}").eq(False).sum()
        if odd:
            print(f"  warning: {odd} county_fips values are not five digits. Look at the County column in the raw file.")

    if "benefits" in want:
        print("benefits (streamed, standard variants only)")
        parts = []
        for year in YEARS:
            kept = []
            for chunk in read_csv(find("benefits-and-cost-sharing", year), usecols=lambda c: c in BENEFIT_SOURCES, chunksize=CHUNK_ROWS):
                plan = chunk["PlanId"].fillna("").str.strip()
                kept.append(chunk[plan.str.endswith(("-00", "-01"))])
            parts.append(clean_benefits(pd.concat(kept)))
            print(f"  {year}: {len(parts[-1]):,} rows", end="\r")
        benefits = pd.concat(parts)
        load(client, benefits, "benefits", first=True)
        print(f"\n  {len(benefits):,} rows")

    if "rates" in want:
        print("rates (the big one, streamed in chunks)")
        first, total = True, 0
        for year in YEARS:
            for chunk in read_csv(find("rate", year), usecols=lambda c: c in RATE_SOURCES, chunksize=CHUNK_ROWS):
                df = clean_rates(chunk)
                load(client, df, "rates", first=first)
                first, total = False, total + len(df)
                print(f"  {year}: {total:,} rows loaded", end="\r")
        print(f"\n  {total:,} rows")
    sanity_checks(client)


def sanity_checks(client: bigquery.Client):
    print("sanity checks")
    q = lambda sql: client.query(sql.replace("DS", f"`{client.project}.{DATASET}`")).to_dataframe()
    print(q("SELECT plan_year, COUNT(*) AS rate_rows, COUNT(DISTINCT state_code) AS states FROM DS.rates GROUP BY 1 ORDER BY 1").to_string(index=False))
    orphans = q("""SELECT r.plan_year, COUNT(*) AS rate_rows_without_a_plan
                   FROM DS.rates r LEFT JOIN DS.plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
                   WHERE p.standard_component_id IS NULL GROUP BY 1 ORDER BY 1""")
    print("rate rows with no matching plan:", "none" if orphans.empty else "\n" + orphans.to_string(index=False))
    gaps = q("""SELECT r.plan_year, COUNT(DISTINCT r.plan_id) AS plans_in_rates,
                       COUNT(DISTINCT IF(p.standard_component_id IS NULL, r.plan_id, NULL)) AS plans_missing_from_plans
                FROM DS.rates r LEFT JOIN DS.plans p ON r.plan_year = p.plan_year AND r.plan_id = p.standard_component_id
                GROUP BY 1 ORDER BY 1""")
    print("plans priced in rates but missing from plans (should be 0, or tiny):\n" + gaps.to_string(index=False))
    try:
        lost = q("""SELECT b.plan_year, COUNT(DISTINCT b.standard_component_id) AS plans_missing_from_plans
                    FROM DS.benefits b LEFT JOIN DS.plans p ON b.plan_year = p.plan_year AND b.standard_component_id = p.standard_component_id
                    WHERE p.standard_component_id IS NULL GROUP BY 1 ORDER BY 1""")
        print("benefit plans missing from plans:", "none" if lost.empty else "\n" + lost.to_string(index=False))
    except Exception:
        pass
    odd = q("SELECT COUNT(*) AS n, MIN(individual_rate) AS lo, MAX(individual_rate) AS hi FROM DS.rates WHERE individual_rate <= 0 OR individual_rate > 5000")
    print("rates <= 0 or > $5,000 (look at these):", odd.to_dict("records")[0])


# --- Describing the tables ---


def describe(client: bigquery.Client) -> list[dict]:
    """Per column: type, valid values for enums, min/max for numbers. Straight from BigQuery."""
    tables = []
    ds = f"`{client.project}.{DATASET}`"
    for table, spec in TABLES.items():
        cols = [(name, bq_type) for name, bq_type, *_ in spec["columns"] if bq_type != "DATE"]
        counts_sql = ", ".join(f"COUNT(DISTINCT `{n}`) AS `{n}`" for n, _ in cols)
        counts = next(iter(client.query(f"SELECT COUNT(*) AS _rows, {counts_sql} FROM {ds}.{table}").result()))
        columns = []
        for name, bq_type, source, custom in spec["columns"]:
            column = {"name": name, "type": bq_type}
            distinct = counts[name] if bq_type != "DATE" else None
            if bq_type in ("STRING", "INT64") and distinct <= MAX_ENUM_VALUES:
                rows = client.query(f"SELECT DISTINCT `{name}` AS v FROM {ds}.{table} WHERE `{name}` IS NOT NULL ORDER BY v").result()
                column["enum_values"] = [r.v for r in rows]
            elif bq_type in ("INT64", "FLOAT64"):
                r = next(iter(client.query(f"SELECT MIN(`{name}`) AS lo, MAX(`{name}`) AS hi FROM {ds}.{table}").result()))
                column["range"] = [r.lo, r.hi]
            elif bq_type == "STRING":
                rows = client.query(f"SELECT DISTINCT `{name}` AS v FROM {ds}.{table} WHERE `{name}` IS NOT NULL LIMIT 5").result()
                column["samples"] = [r.v for r in rows]
            columns.append(column)
        tables.append({"name": table, "description": spec["description"], "grain": spec["grain"], "rows": counts["_rows"], "columns": columns})
        print(f"  {table}: {counts['_rows']:,} rows, {len(columns)} columns")
    return tables


def cms_definition(kind: str, source: str | None) -> dict:
    return json.loads((DATA / "cms_definitions.json").read_text()).get(kind, {}).get(source or "", {})


def write_descriptions(tables: list[dict]):
    for table in tables:
        spec = TABLES[table["name"]]
        known = {name: (source, custom) for name, _, source, custom in spec["columns"]}
        items = []
        for column in table["columns"]:
            source, custom = known[column["name"]]
            if custom:
                continue
            d = cms_definition(spec["kind"], source)
            items.append({
                "column": column["name"],
                "type": column["type"],
                "cms_definition": d.get("definition", ""),
                "cms_comments": d.get("comments", "")[:400],
                "valid_values": column.get("enum_values") or column.get("samples"),
                "range": column.get("range"),
            })
        descriptions = {}
        if items:
            prompt = (
                f"These are columns of the table `{table['name']}` ({spec['description']}), built from the CMS "
                "Health Insurance Exchange public use files. Write a concise one-sentence description of each column "
                "for an analyst who writes SQL. Ground it in the CMS definition. Do not invent facts, and do not just "
                "list the values. Return a JSON object mapping each column name to its description.\n\n"
                + json.dumps(items, default=str)
            )
            try:
                response = litellm.completion(
                    model=MODEL,
                    vertex_location="global",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                descriptions = json.loads(response.choices[0].message.content)
            except Exception as e:
                print(f"  warning: model call failed for {table['name']} ({type(e).__name__}). Using the CMS definitions as-is.")
        for column in table["columns"]:
            source, custom = known[column["name"]]
            fallback = cms_definition(spec["kind"], source).get("definition", "")
            column["description"] = custom or descriptions.get(column["name"]) or fallback
            column.pop("samples", None)  # samples helped the model describe the column; the agent doesn't need them


def label_bigquery_columns(client: bigquery.Client, tables: list[dict]):
    """Put the descriptions on the BigQuery columns too, so they show in the console."""
    for table in tables:
        ref = client.get_table(f"{client.project}.{DATASET}.{table['name']}")
        said = {c["name"]: c["description"] for c in table["columns"]}
        ref.schema = [bigquery.SchemaField(f.name, f.field_type, f.mode, description=said.get(f.name, "")) for f in ref.schema]
        client.update_table(ref, ["schema"])


RATE_COLUMNS = [name for name, bq_type, *_ in TABLES["rates"]["columns"] if bq_type == "FLOAT64"]
BENEFIT_SOURCES = {s for _, _, s, _ in TABLES["benefits"]["columns"] if s} | {"PlanId", "CopayInnTier1", "CoinsInnTier1"}
RATE_SOURCES = {s for _, _, s, _ in TABLES["rates"]["columns"] if s}
PLAN_SOURCES = {s for _, _, s, _ in TABLES["plans"]["columns"] if s} | {"PlanId"} | set(DEDUCTIBLE_COLS) | set(MOOP_COLS)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-load", action="store_true", help="tables are already in BigQuery: only rebuild the mappings")
    parser.add_argument("--only", nargs="+", choices=ALL_TABLES, help="load just these tables, e.g. --only plans benefits")
    args = parser.parse_args()

    client = bigquery.Client(location=LOCATION)
    print(f"Project {client.project}, dataset {DATASET}, location {LOCATION}")
    if not args.skip_load:
        build_tables(client, args.only)
    print("describing")
    tables = describe(client)
    write_descriptions(tables)
    MAPPINGS_PATH.write_text(json.dumps({"dataset": DATASET, "joins": JOINS, "tables": tables}, indent=2, default=str))
    label_bigquery_columns(client, tables)
    print(f"Wrote {MAPPINGS_PATH.name}: {len(tables)} tables, {sum(len(t['columns']) for t in tables)} columns")
