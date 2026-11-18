import argparse
import csv
import os
import statistics
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

import oracledb

ROOT = Path(__file__).resolve().parent

HEADER = [
    "Prscrbr_NPI", "Prscrbr_Last_Org_Name", "Prscrbr_First_Name", "Prscrbr_City",
    "Prscrbr_State_Abrvtn", "Prscrbr_State_FIPS", "Prscrbr_Type", "Prscrbr_Type_Src",
    "Brnd_Name", "Gnrc_Name", "Tot_Clms", "Tot_30day_Fills", "Tot_Day_Suply", "Tot_Drug_Cst",
    "Tot_Benes", "GE65_Sprsn_Flag", "GE65_Tot_Clms", "GE65_Tot_30day_Fills", "GE65_Tot_Drug_Cst",
    "GE65_Tot_Day_Suply", "GE65_Bene_Sprsn_Flag", "GE65_Tot_Benes",
]
STATE_COL = HEADER.index("Prscrbr_State_Abrvtn")
STEPS = ["STAGE", "DIMS", "FACT", "REFRESH", "QUALITY"]

MART_QUERIES = [
    (
        "top 10 generics by cost, latest year",
        "select {hint} d.generic_name, sum(f.tot_drug_cst) cost "
        "from fact_prescription f join dim_drug d on d.drug_key = f.drug_key "
        "where f.data_year = {year} group by d.generic_name "
        "order by cost desc fetch first 10 rows only",
    ),
    (
        "cost per claim by specialty and year",
        "select {hint} p.prescriber_type, f.data_year, "
        "round(sum(f.tot_drug_cst) / sum(f.tot_clms), 2) cost_per_claim "
        "from fact_prescription f join dim_prescriber p on p.prescriber_key = f.prescriber_key "
        "group by p.prescriber_type, f.data_year",
    ),
    (
        "generic share of claims by year",
        "select {hint} f.data_year, d.is_generic, sum(f.tot_clms) claims, "
        "round(100 * ratio_to_report(sum(f.tot_clms)) over (partition by f.data_year), 2) pct "
        "from fact_prescription f join dim_drug d on d.drug_key = f.drug_key "
        "group by f.data_year, d.is_generic",
    ),
]

LOOKUP_QUERY = (
    "select {hint} d.generic_name, f.tot_clms, f.tot_drug_cst "
    "from fact_prescription f "
    "join dim_drug d on d.drug_key = f.drug_key "
    "join dim_prescriber p on p.prescriber_key = f.prescriber_key "
    "where p.npi = {npi} and f.data_year = {year}"
)


def load_config(path=ROOT / "config.toml"):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    data = cfg.get("data", {})
    db = cfg.get("db", {})
    years = data.get("years")
    if not years or not all(isinstance(y, int) and 2013 <= y <= 2100 for y in years):
        raise ValueError("data.years must be a list of years between 2013 and 2100")
    if years != sorted(set(years)):
        raise ValueError("data.years must go up with no repeats")
    states = data.get("states")
    if not states or not all(isinstance(s, str) and len(s) == 2 and s.isalpha() and s.isupper() for s in states):
        raise ValueError("data.states must be two letter codes like OH")
    urls = {int(k): v for k, v in data.get("urls", {}).items()}
    missing = [y for y in years if y not in urls]
    if missing:
        raise ValueError(f"data.urls has no link for {missing}")
    for key in ("dsn", "user", "password_env"):
        if not isinstance(db.get(key), str) or not db[key]:
            raise ValueError(f"db.{key} must be set")
    repeats = cfg.get("bench", {}).get("repeats", 5)
    if not isinstance(repeats, int) or repeats < 1:
        raise ValueError("bench.repeats must be a positive whole number")
    data_dir = Path(os.environ.get("GRANARY_DATA_DIR") or data.get("dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return {
        "years": years,
        "states": states,
        "urls": urls,
        "data_dir": data_dir,
        "dsn": os.environ.get("GRANARY_DSN") or db["dsn"],
        "user": os.environ.get("GRANARY_USER") or db["user"],
        "password_env": db["password_env"],
        "repeats": repeats,
    }


def decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def filter_rows(lines, states, out):
    lines = iter(lines)
    header = decode(next(lines)).rstrip("\r\n")
    if header.split(",") != HEADER:
        raise ValueError(f"unexpected header: {header[:120]}")
    out.write(header + "\n")
    wanted = set(states)
    needles = [f",{s},".encode() for s in states] + [f',"{s}",'.encode() for s in states]
    seen = kept = bad = 0
    for raw in lines:
        seen += 1
        if not any(n in raw for n in needles):
            continue
        line = decode(raw).rstrip("\r\n")
        row = next(csv.reader([line]))
        if len(row) != len(HEADER):
            bad += 1
            continue
        if row[STATE_COL] in wanted:
            out.write(line + "\n")
            kept += 1
    return seen, kept, bad


def fetch(cfg, force):
    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    for year in cfg["years"]:
        target = cfg["data_dir"] / f"partd_{year}.csv"
        if target.exists() and not force:
            print(f"{year}: {target.name} already there, skipping", flush=True)
            continue
        part = target.with_suffix(".part")
        start = time.perf_counter()
        with urllib.request.urlopen(cfg["urls"][year], timeout=120) as resp:
            with open(part, "w", encoding="utf-8", newline="") as out:
                seen, kept, bad = filter_rows(resp, cfg["states"], out)
        os.replace(part, target)
        mb = target.stat().st_size / 1e6
        took = time.perf_counter() - start
        print(f"{year}: kept {kept:,} of {seen:,} rows ({bad} malformed), {mb:.1f} MB, {took:.0f} s", flush=True)


def connect(cfg):
    password = os.environ.get(cfg["password_env"])
    if not password:
        raise SystemExit(f"set {cfg['password_env']} to the password of {cfg['user']}")
    return oracledb.connect(user=cfg["user"], password=password, dsn=cfg["dsn"])


def table(headers, rows):
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        print("| " + " | ".join(str(v) for v in row) + " |")
    print()


def seconds(start, end):
    return (end - start).total_seconds() if start and end else 0.0


def step_times(cur, run_ids):
    if not run_ids:
        return {}
    binds = ",".join(f":{i}" for i in range(len(run_ids)))
    cur.execute(
        f"select run_id, step_name, started_at, finished_at from load_step where run_id in ({binds})",
        run_ids,
    )
    times = {}
    for run_id, step, start, end in cur:
        times.setdefault(run_id, {})[step] = seconds(start, end)
    return times


def show_runs(cur, limit=10):
    cur.execute(
        "select run_id, data_year, status, rows_staged, rows_rejected, fact_rows, "
        "prescribers_closed, prescribers_added, drugs_added, started_at, finished_at "
        "from load_run order by run_id desc fetch first :n rows only",
        n=limit,
    )
    rows = cur.fetchall()
    table(
        ["run", "year", "status", "staged", "rejected", "fact rows", "versions closed",
         "prescribers added", "drugs added", "seconds"],
        [list(r[:9]) + [f"{seconds(r[9], r[10]):.1f}"] for r in rows],
    )


def load(cfg, years):
    with connect(cfg) as conn:
        cur = conn.cursor()
        for year in years:
            start = time.perf_counter()
            cur.callproc("granary_load.run_year", [year])
            print(f"{year}: loaded in {time.perf_counter() - start:.1f} s", flush=True)
        show_runs(cur, len(years))


def reset(cfg):
    with connect(cfg) as conn:
        conn.cursor().callproc("granary_load.reset")
    print("warehouse is empty")


def wait_for_run(cur, run_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        cur.execute("select status, error_message from load_run where run_id = :r", r=run_id)
        status, error = cur.fetchone()
        if status != "RUNNING":
            return status, error
        if time.monotonic() > deadline:
            raise SystemExit(f"run {run_id} is still running after {timeout} s")
        time.sleep(2)


def chain(cfg, year, timeout):
    with connect(cfg) as conn:
        cur = conn.cursor()
        run_id = cur.callfunc("granary_load.submit_chain", int, [year])
        print(f"run {run_id} for {year} handed to GRANARY_CHAIN", flush=True)
        status, error = wait_for_run(cur, run_id, timeout)
        cur.execute(
            "select job_subname, status, run_duration from user_scheduler_job_run_details "
            "where job_name = :j and job_subname is not null order by log_id",
            j=f"GRANARY_RUN_{run_id}",
        )
        table(["chain step", "status", "duration"], cur.fetchall())
        print(f"run {run_id} finished as {status}")
        if status != "DONE":
            raise SystemExit(error)


def timed(cur, sql, repeats):
    cur.execute(sql).fetchall()
    runs = []
    for _ in range(repeats):
        start = time.perf_counter()
        cur.execute(sql).fetchall()
        runs.append((time.perf_counter() - start) * 1000)
    return statistics.median(runs)


def plan(cur, sql, tag):
    cur.execute("delete from plan_table where statement_id = :t", t=tag)
    cur.execute(f"explain plan set statement_id = '{tag}' for {sql}")
    cur.execute(
        "select trim(operation || ' ' || options) from plan_table where statement_id = :t order by id",
        t=tag,
    )
    return [r[0] for r in cur]


def bench(cfg):
    with connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute(
            "select max(run_id) from load_run where status = 'DONE' group by data_year order by data_year"
        )
        run_ids = [r[0] for r in cur]
        if not run_ids:
            raise SystemExit("nothing loaded yet, run: python granary.py load")
        times = step_times(cur, run_ids)
        binds = ",".join(f":{i}" for i in range(len(run_ids)))
        cur.execute(
            f"select run_id, data_year, rows_staged, rows_rejected, fact_rows from load_run "
            f"where run_id in ({binds}) order by data_year",
            run_ids,
        )
        rows = []
        for run_id, year, staged, rejected, fact_rows in cur.fetchall():
            parts = [times.get(run_id, {}).get(s, 0.0) for s in STEPS]
            total = sum(parts)
            rate = f"{staged / total:,.0f}" if total else "-"
            counts = [year, f"{staged:,}", rejected, f"{fact_rows:,}"]
            rows.append(counts + [f"{p:.1f}" for p in parts] + [f"{total:.1f}", rate])
        print("load, seconds per step")
        table(["year", "staged", "rejected", "fact rows"] + [s.lower() for s in STEPS] + ["total", "rows/s"], rows)

        cur.execute(
            "select segment_name, round(sum(bytes) / 1048576, 1) from user_segments "
            "where segment_name in ('FACT_PRESCRIPTION', 'BX_FACT_PRESCRIBER', 'BX_FACT_DRUG', "
            "'DIM_PRESCRIBER', 'DIM_DRUG', 'MV_DRUG_YEAR', 'MV_SPECIALTY_YEAR') "
            "group by segment_name order by 2 desc"
        )
        sizes = cur.fetchall()
        cur.execute("drop table if exists bench_nocompress purge")
        cur.execute("create table bench_nocompress nocompress as select * from fact_prescription")
        cur.execute("select round(sum(bytes) / 1048576, 1) from user_segments where segment_name = 'BENCH_NOCOMPRESS'")
        plain = cur.fetchone()[0]
        cur.execute("drop table bench_nocompress purge")
        fact = dict(sizes).get("FACT_PRESCRIPTION")
        print("storage, MB")
        table(["segment", "MB"], sizes + [("fact without compression", plain)])
        if fact:
            print(f"basic compression keeps the fact at {fact / plain:.0%} of its plain size\n")

        year = cfg["years"][-1]
        cur.execute("select max(data_year) from load_run where status = 'DONE'")
        year = cur.fetchone()[0] or year
        repeats = cfg["repeats"]
        rows = []
        for i, (name, sql) in enumerate(MART_QUERIES):
            base = sql.format(hint="/*+ no_rewrite */", year=year)
            fast = sql.format(hint="", year=year)
            slow_ms = timed(cur, base, repeats)
            fast_ms = timed(cur, fast, repeats)
            ops = plan(cur, fast, f"q{i}")
            base_ops = plan(cur, base, f"b{i}")
            rewrite = "yes" if any(op.startswith("MAT_VIEW REWRITE ACCESS") for op in ops) else "no"
            pruned = "yes" if "PARTITION RANGE SINGLE" in base_ops else "no"
            rows.append([name, f"{slow_ms:.1f}", f"{fast_ms:.1f}", f"{slow_ms / fast_ms:.0f}x", rewrite, pruned])
        print(f"queries, median of {repeats} runs in ms")
        table(["query", "fact tables", "with marts", "speedup", "rewritten to mart", "one partition read"], rows)

        cur.execute(
            "select p.npi from fact_prescription f join dim_prescriber p on p.prescriber_key = f.prescriber_key "
            "where f.data_year = :y group by p.npi order by count(*) desc, p.npi fetch first 1 row only",
            y=year,
        )
        npi = cur.fetchone()[0]
        full_sql = LOOKUP_QUERY.format(hint="/*+ full(f) */", npi=npi, year=year)
        index_sql = LOOKUP_QUERY.format(hint="", npi=npi, year=year)
        full_ms = timed(cur, full_sql, repeats)
        index_ms = timed(cur, index_sql, repeats)
        ops = plan(cur, index_sql, "lookup")
        uses_bitmap = "yes" if any(op.startswith("BITMAP") for op in ops) else "no"
        print("one prescriber, latest year, median ms")
        table(
            ["full partition scan", "bitmap index", "speedup", "bitmap in plan"],
            [[f"{full_ms:.1f}", f"{index_ms:.1f}", f"{full_ms / index_ms:.0f}x", uses_bitmap]],
        )


def status(cfg):
    with connect(cfg) as conn:
        cur = conn.cursor()
        show_runs(cur)
        cur.execute("select max(run_id) from load_run")
        run_id = cur.fetchone()[0]
        if run_id:
            cur.execute(
                "select check_name, severity, failed_rows from dq_result where run_id = :r order by check_name",
                r=run_id,
            )
            print(f"quality checks for run {run_id}")
            table(["check", "severity", "failed rows"], cur.fetchall())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="granary")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="download the CMS files and keep the configured states")
    p.add_argument("--force", action="store_true", help="download again even if the file is there")
    p = sub.add_parser("load", help="run the pipeline for each year")
    p.add_argument("--years", type=int, nargs="+", help="years to load, default is every year in config.toml")
    sub.add_parser("reset", help="empty the warehouse")
    p = sub.add_parser("chain", help="run one year through the DBMS_SCHEDULER chain")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--timeout", type=int, default=1800)
    sub.add_parser("bench", help="measure load, storage and query numbers")
    sub.add_parser("status", help="show recent runs and quality checks")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "fetch":
        fetch(cfg, args.force)
    elif args.command == "load":
        load(cfg, args.years or cfg["years"])
    elif args.command == "reset":
        reset(cfg)
    elif args.command == "chain":
        chain(cfg, args.year, args.timeout)
    elif args.command == "bench":
        bench(cfg)
    else:
        status(cfg)


if __name__ == "__main__":
    sys.exit(main())
