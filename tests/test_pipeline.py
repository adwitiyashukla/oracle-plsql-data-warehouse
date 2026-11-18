import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

import oracledb
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
YEARS = [2022, 2023, 2024]
ATTRS = ["Prscrbr_Last_Org_Name", "Prscrbr_First_Name", "Prscrbr_City", "Prscrbr_State_Abrvtn",
         "Prscrbr_State_FIPS", "Prscrbr_Type", "Prscrbr_Type_Src"]
NUMBERS = ["Tot_Clms", "Tot_30day_Fills", "Tot_Day_Suply", "Tot_Drug_Cst", "Tot_Benes", "GE65_Tot_Clms",
           "GE65_Tot_30day_Fills", "GE65_Tot_Drug_Cst", "GE65_Tot_Day_Suply", "GE65_Tot_Benes"]


def is_number(text):
    try:
        Decimal(text)
    except InvalidOperation:
        return False
    return True


def source(year):
    good, bad = [], []
    with open(FIXTURES / f"partd_{year}.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ok = (
                r["Prscrbr_NPI"].isdigit()
                and r["Brnd_Name"]
                and r["Gnrc_Name"]
                and r["Tot_Clms"]
                and all(r[c] == "" or is_number(r[c]) for c in NUMBERS)
            )
            (good if ok else bad).append(r)
    return good, bad


def npi_ok(npi):
    text = str(npi)
    if len(text) != 10 or not text.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + text)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def expected_versions():
    history = {}
    for year in YEARS:
        good, _ = source(year)
        attrs = {int(r["Prscrbr_NPI"]): tuple(r[a] for a in ATTRS) for r in good}
        for npi, values in attrs.items():
            versions = history.setdefault(npi, [])
            if versions and versions[-1][2] == values:
                continue
            if versions:
                versions[-1][1] = year - 1
            versions.append([year, 9999, values])
    return {npi: [(v[0], v[1]) for v in versions] for npi, versions in history.items()}


def latest_runs(cur):
    cur.execute(
        "select data_year, run_id, status, rows_staged, rows_rejected, fact_rows from load_run "
        "where run_id in (select max(run_id) from load_run group by data_year)"
    )
    return {int(r[0]): r[1:] for r in cur}


def snapshot(cur):
    cur.execute(
        "select npi, valid_from_year, valid_to_year, is_current, row_hash from dim_prescriber order by 1, 2"
    )
    dims = cur.fetchall()
    cur.execute(
        "select /*+ no_rewrite */ data_year, count(*), sum(tot_drug_cst) from fact_prescription "
        "group by data_year order by 1"
    )
    fact = cur.fetchall()
    cur.execute("select brand_name, generic_name, is_generic from dim_drug order by 1, 2")
    return dims, fact, cur.fetchall()


@pytest.fixture(scope="module")
def loaded(conn):
    cur = conn.cursor()
    cur.callproc("granary_load.reset")
    for year in YEARS:
        cur.callproc("granary_load.run_year", [year])
    return cur


def test_every_year_finishes(loaded):
    runs = latest_runs(loaded)
    assert [runs[y][1] for y in YEARS] == ["DONE"] * len(YEARS)


def test_staged_rejected_and_fact_counts(loaded):
    runs = latest_runs(loaded)
    for year in YEARS:
        good, bad = source(year)
        assert runs[year][2:] == (len(good), len(bad), len(good))


def test_rejected_rows_keep_the_oracle_error(loaded):
    loaded.execute(
        "select r.data_year, e.ora_err_number$ from err_stg_partd e "
        "join load_run r on to_char(r.run_id) = e.ora_err_tag$ order by 1, 2"
    )
    assert loaded.fetchall() == [(2023, 1722), (2024, 1400), (2024, 1400)]


def test_fact_totals_match_the_files(loaded):
    for year in YEARS:
        good, _ = source(year)
        loaded.execute(
            "select /*+ no_rewrite */ count(*), sum(tot_clms), sum(tot_drug_cst), sum(tot_benes), "
            "count(ge65_tot_clms) from fact_prescription where data_year = :y",
            y=year,
        )
        count, claims, cost, benes, age65_rows = loaded.fetchone()
        assert count == len(good)
        assert claims == sum(Decimal(r["Tot_Clms"]) for r in good)
        assert cost == sum(Decimal(r["Tot_Drug_Cst"]) for r in good)
        assert benes == sum(Decimal(r["Tot_Benes"]) for r in good if r["Tot_Benes"])
        assert age65_rows == sum(1 for r in good if r["GE65_Tot_Clms"])


def test_prescriber_history_follows_scd2(loaded):
    loaded.execute("select npi, valid_from_year, valid_to_year from dim_prescriber order by 1, 2")
    got = {}
    for npi, start, end in loaded:
        got.setdefault(int(npi), []).append((int(start), int(end)))
    assert got == expected_versions()


def test_one_current_version_per_prescriber(loaded):
    loaded.execute("select npi from dim_prescriber where is_current group by npi having count(*) != 1")
    assert loaded.fetchall() == []
    loaded.execute("select count(*) from dim_prescriber where is_current and valid_to_year != 9999")
    assert loaded.fetchone()[0] == 0


def test_fact_rows_point_at_the_version_of_their_year(loaded):
    loaded.execute(
        "select distinct f.data_year, p.npi, p.city, p.prescriber_type from fact_prescription f "
        "join dim_prescriber p on p.prescriber_key = f.prescriber_key"
    )
    got = {(int(y), int(n)): (c, t) for y, n, c, t in loaded}
    want = {}
    for year in YEARS:
        good, _ = source(year)
        for r in good:
            want[(year, int(r["Prscrbr_NPI"]))] = (r["Prscrbr_City"], r["Prscrbr_Type"])
    assert got == want


def test_generic_flag(loaded):
    loaded.execute("select brand_name, generic_name, is_generic from dim_drug")
    got = {(b, g): flag for b, g, flag in loaded}
    want = {}
    for year in YEARS:
        good, _ = source(year)
        for r in good:
            want[(r["Brnd_Name"], r["Gnrc_Name"])] = r["Brnd_Name"].lower() == r["Gnrc_Name"].lower()
    assert got == want


def test_marts_match_the_fact_table(loaded):
    loaded.execute("select data_year, sum(tot_clms), sum(tot_drug_cst) from mv_drug_year group by data_year order by 1")
    mart = loaded.fetchall()
    loaded.execute(
        "select /*+ no_rewrite */ data_year, sum(tot_clms), sum(tot_drug_cst) from fact_prescription "
        "group by data_year order by 1"
    )
    assert mart == loaded.fetchall()
    loaded.execute("select data_year, sum(tot_clms) from mv_specialty_year group by data_year order by 1")
    assert loaded.fetchall() == [(y, c) for y, c, _ in mart]


def test_mart_query_is_rewritten(loaded):
    loaded.execute("delete from plan_table where statement_id = 'rewrite'")
    loaded.execute(
        "explain plan set statement_id = 'rewrite' for "
        "select /*+ rewrite */ d.generic_name, sum(f.tot_drug_cst) from fact_prescription f "
        "join dim_drug d on d.drug_key = f.drug_key where f.data_year = 2024 group by d.generic_name"
    )
    loaded.execute("select operation || ' ' || options from plan_table where statement_id = 'rewrite'")
    assert any(op.startswith("MAT_VIEW REWRITE ACCESS") for (op,) in loaded)


def test_quality_checks(loaded):
    run_id = latest_runs(loaded)[2024][0]
    loaded.execute("select check_name, severity, failed_rows from dq_result where run_id = :r", r=run_id)
    checks = {name: (severity, int(n)) for name, severity, n in loaded}
    good, bad = source(2024)
    assert len(checks) == 9
    assert [name for name, (severity, n) in checks.items() if severity == "ERROR" and n] == []
    assert checks["npi_check_digit"] == ("WARN", len({r["Prscrbr_NPI"] for r in good if not npi_ok(r["Prscrbr_NPI"])}))
    assert checks["reject_rate_over_1_percent"] == ("WARN", len(bad))


def test_npi_check_digit_matches_python(conn):
    cur = conn.cursor()
    cur.execute(
        "select npi, granary_load.npi_is_valid(npi) from "
        "(select 1000000000 + level * 1987 npi from dual connect by level <= 2000) "
        "union all select 1234567893, granary_load.npi_is_valid(1234567893) from dual "
        "union all select 123456789, granary_load.npi_is_valid(123456789) from dual"
    )
    rows = cur.fetchall()
    assert len(rows) == 2002
    assert sum(1 for _, flag in rows if flag == "Y") > 100
    for npi, flag in rows:
        assert (flag == "Y") == npi_ok(int(npi)), npi


def test_interval_partitions_and_usable_indexes(loaded):
    loaded.execute("select count(*) from user_tab_partitions where table_name = 'FACT_PRESCRIPTION'")
    assert loaded.fetchone()[0] == 1 + len(YEARS)
    loaded.execute("select count(*) from user_ind_partitions where index_name like 'BX_FACT%' and status != 'USABLE'")
    assert loaded.fetchone()[0] == 0


def test_reloading_the_latest_year_changes_nothing(loaded):
    before = snapshot(loaded)
    loaded.callproc("granary_load.run_year", [YEARS[-1]])
    assert snapshot(loaded) == before
    assert latest_runs(loaded)[YEARS[-1]][1] == "DONE"


def test_older_year_is_refused(loaded):
    with pytest.raises(oracledb.DatabaseError, match="ORA-20001"):
        loaded.callproc("granary_load.run_year", [YEARS[0]])


def test_missing_file_marks_the_run_failed(loaded):
    with pytest.raises(oracledb.DatabaseError):
        loaded.callproc("granary_load.run_year", [YEARS[-1] + 1])
    loaded.execute(
        "select status, error_message from load_run where data_year = :y order by run_id desc fetch first 1 row only",
        y=YEARS[-1] + 1,
    )
    status, message = loaded.fetchone()
    assert status == "FAILED"
    assert f"partd_{YEARS[-1] + 1}.csv" in message
