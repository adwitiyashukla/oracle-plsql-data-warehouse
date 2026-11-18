# oracle-plsql-data-warehouse

A data warehouse for Medicare Part D prescriptions, written in PL/SQL on Oracle AI Database 26ai Free. It loads three years of CMS prescriber data for Ohio (3,306,327 rows) into a star schema, and the whole load runs inside the database.

I wanted to use the Oracle features that real warehouses depend on, like external tables, partition exchange, materialized views with query rewrite and DBMS_SCHEDULER, and see what each one buys on real data.

## What it does

The load has five steps. Each one is a procedure in the `granary_load` package and writes its start time, end time and row count to a run log.

| step | what happens |
|---|---|
| stage | An external table reads the year's CSV in place, and `EXTERNAL MODIFY` picks the file at query time. A direct path insert fills the staging table. Rows that fail a type or NOT NULL check go to an error log table with the Oracle error instead of stopping the load. |
| dims | The prescriber dimension keeps history (type 2). If a prescriber's city, name or specialty changes between years, the old row gets an end year and a new row starts. Drugs are added once per brand and generic pair. |
| fact | Rows are joined to the prescriber version that was valid in their year and inserted into a work table. The work table is swapped into the year's partition with `EXCHANGE PARTITION`, then the local bitmap indexes are rebuilt. Statistics are gathered on the work table before the swap, so the fact table only needs incremental statistics. |
| refresh | Two materialized views (drug by year, specialty by year) are refreshed. Query rewrite is on, so normal queries on the fact table get answered from them without changing the SQL. |
| quality | Nine checks run against the load. Six fail the run: row counts and cost totals match staging, no duplicate fact rows, one current version per prescriber, no overlapping versions and no negative amounts. Three are warnings: reject rate, NPI check digit, and age 65+ amounts above the total. |

The fact table is interval partitioned by year, so a new year gets its partition the first time it is loaded. It uses basic table compression and two local bitmap indexes. Loading a year that is already there first undoes that year's dimension changes and then swaps the partition again, so a reload gives the same warehouse. Loading an older year after a newer one is refused, because it would break the dimension history.

The same five steps also exist as a DBMS_SCHEDULER chain (`GRANARY_CHAIN`). Each step runs as its own job, and the next one only starts if the previous one succeeded.

## Results

Measured on my HP OmniBook laptop (Intel Core 5 120U, 8 GB RAM, Windows 11) with Oracle AI Database 26ai Free 23.26.3, which is capped at 2 CPU threads and 2 GB of memory.

The three national CMS files are 3.7 to 4.1 GB each and hold 80,688,291 rows together. `granary.py fetch` streams them and keeps only the Ohio rows, so 478 MB lands on disk. That took 359 s.

Load, seconds per step:

| year | rows | stage | dims | fact | refresh | quality | total | rows/s |
|---|---|---|---|---|---|---|---|---|
| 2022 | 1,064,656 | 16.7 | 3.5 | 29.5 | 3.7 | 3.8 | 57.2 | 18,622 |
| 2023 | 1,099,684 | 16.5 | 4.1 | 35.9 | 3.0 | 3.9 | 63.5 | 17,316 |
| 2024 | 1,141,987 | 16.5 | 4.5 | 29.0 | 4.1 | 4.6 | 58.7 | 19,459 |

No row was rejected and every quality check passed in all three years.

Two changes made the load faster. My first full load took 87.4 s, 98.8 s and 116.1 s for the three years. The quality step took about 30 s every year because it called the NPI check digit function once per row, which is about a million calls. Now it bulk collects the distinct NPIs and checks each one once, and the step takes about 4 s. The fact step also grew from 33 s to 58 s as the table got bigger, because it gathered statistics on the whole fact table after every load. Now the work table gets its statistics before the exchange and the fact table uses incremental statistics, so the step stays around 30 s.

Storage:

| segment | MB |
|---|---|
| fact table, compressed | 137 |
| same rows without compression | 197 |
| bitmap index on drug | 12 |
| prescriber dimension | 9 |
| bitmap index on prescriber | 6 |
| both materialized views | 0.7 |

Basic compression keeps the fact table at 70% of its plain size.

Queries, median of 7 runs in ms:

| query | on the fact table | with query rewrite | speedup |
|---|---|---|---|
| top 10 generics by cost, 2024 | 772.4 | 1.0 | 761x |
| cost per claim by specialty and year | 3927.6 | 1.7 | 2358x |
| generic share of claims by year | 3254.8 | 2.4 | 1375x |

The first column uses the `NO_REWRITE` hint and the second is the same SQL without it. All three plans show `MAT_VIEW REWRITE ACCESS`, and on the fact table the first query reads only the 2024 partition.

Looking up one prescriber's drugs for 2024 took 804.4 ms with a full scan of the partition and 4.2 ms with the bitmap index (190x).

The scheduler chain reloaded 2024 in 73 s: stage 18 s, dims 7 s, fact 33 s, refresh 10 s and quality 5 s. It gave the same counts as the first 2024 load: 3,649 closed versions, 8,929 new prescribers and 1,141,987 fact rows.

## What the data says

- Part D drug cost in Ohio went from $7.35 billion in 2022 to $8.46 billion in 2023 and $9.04 billion in 2024. Claims went from 56.4 million to 62.3 million, so the cost per claim went from $130.31 to $145.17.
- Generics are about three quarters of the claims (77.15% in 2022, 76.21% in 2024), but their share of the cost fell from 13.83% to 10.05%.
- The most expensive drugs in 2024 were apixaban ($889.7 million), semaglutide ($477.1 million) and empagliflozin ($420.1 million).
- 3,174 prescribers got a new version in 2023 and 3,649 in 2024. The most common change was the field that says where the specialty came from, then the city, then the specialty itself. The prescriber dimension has 60,342 rows for 53,519 prescribers.

## Running it

You need Oracle AI Database 26ai Free and Python 3.13. Commands are for Windows cmd.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

The CSV folder has to be readable by the Oracle service. On Windows the service runs as its own account, so a folder under the Oracle base folder works.

```
set GRANARY_DATA_DIR=C:\app\<you>\product\26ai\granary_data
.venv\Scripts\python granary.py fetch
```

Create the user and the directory objects as SYSDBA, then install the schema:

```
sqlplus / as sysdba @sql\admin.sql granary <password> %GRANARY_DATA_DIR% %GRANARY_DATA_DIR%
sqlplus granary/<password>@localhost:1521/FREEPDB1 @sql\install.sql
```

Load, run the chain and measure:

```
set GRANARY_PASSWORD=<password>
.venv\Scripts\python granary.py load
.venv\Scripts\python granary.py chain --year 2024
.venv\Scripts\python granary.py bench
.venv\Scripts\python granary.py status
```

Years, states, download links and the connection are in `config.toml`.

## Tests

`pytest` runs 32 tests. 15 of them cover the download filter and the config checks and need no database. The other 17 run the real pipeline on small hand written CSV files in `tests/fixtures`. Those files have a bad number, a missing NPI, a missing drug name, a prescriber who moves cities, one who changes specialty and an NPI with a wrong check digit. The expected counts, totals, version history and check digits are computed again in Python straight from the CSV files, not read back from Oracle.

I run them against a second schema, so the real warehouse is not touched:

```
set GRANARY_USER=granary_test
.venv\Scripts\pytest
```

CI on GitHub Actions starts the `gvenzl/oracle-free` 23.26.3 container, creates the user, installs the schema and runs all the tests. That image sets `JOB_QUEUE_PROCESSES` to 1, and my first CI run hung there: the chain job itself holds that one job slot, so its steps never start. CI now raises it to 10 before the tests.

## Files

```
granary.py             fetch, load, chain, bench and status commands
config.toml            years, states, CMS links and the connection
sql/admin.sql          user, directory objects and grants, run as SYSDBA
sql/install.sql        runs the three scripts below
sql/schema.sql         external table, staging, dimensions, fact, materialized views, run log
sql/granary_load.sql   the load package
sql/scheduler.sql      DBMS_SCHEDULER programs and chain
tests/                 pytest tests and fixture CSV files
```

Data: CMS "Medicare Part D Prescribers - by Provider and Drug", data years 2022 to 2024, from data.cms.gov.
