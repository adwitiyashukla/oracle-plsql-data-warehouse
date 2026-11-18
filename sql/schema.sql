drop materialized view if exists mv_drug_year;
drop materialized view if exists mv_specialty_year;
drop view if exists v_stg_prescriber;
drop table if exists wrk_fact_prescription purge;
drop table if exists fact_prescription purge;
drop table if exists dim_prescriber purge;
drop table if exists dim_drug purge;
drop table if exists err_stg_partd purge;
drop table if exists stg_partd purge;
drop table if exists ext_partd;
drop table if exists dq_result purge;
drop table if exists load_step purge;
drop table if exists load_run purge;

create table load_run (
  run_id             number generated always as identity primary key,
  data_year          number(4) not null,
  status             varchar2(10) not null check (status in ('RUNNING', 'DONE', 'FAILED')),
  via_chain          boolean default false not null,
  started_at         timestamp not null,
  finished_at        timestamp,
  rows_staged        number,
  rows_rejected      number,
  prescribers_closed number,
  prescribers_added  number,
  drugs_added        number,
  fact_rows          number,
  refresh_types      varchar2(400),
  error_message      varchar2(4000)
);

create table load_step (
  run_id         number not null,
  step_name      varchar2(10) not null,
  started_at     timestamp not null,
  finished_at    timestamp,
  rows_processed number,
  constraint pk_load_step primary key (run_id, step_name)
);

create table dq_result (
  run_id      number not null,
  check_name  varchar2(40) not null,
  severity    varchar2(5) not null check (severity in ('ERROR', 'WARN')),
  failed_rows number not null,
  constraint pk_dq_result primary key (run_id, check_name)
);

create table ext_partd (
  prscrbr_npi           varchar2(20),
  prscrbr_last_org_name varchar2(255),
  prscrbr_first_name    varchar2(255),
  prscrbr_city          varchar2(255),
  prscrbr_state_abrvtn  varchar2(20),
  prscrbr_state_fips    varchar2(20),
  prscrbr_type          varchar2(255),
  prscrbr_type_src      varchar2(255),
  brnd_name             varchar2(255),
  gnrc_name             varchar2(255),
  tot_clms              varchar2(40),
  tot_30day_fills       varchar2(40),
  tot_day_suply         varchar2(40),
  tot_drug_cst          varchar2(40),
  tot_benes             varchar2(40),
  ge65_sprsn_flag       varchar2(20),
  ge65_tot_clms         varchar2(40),
  ge65_tot_30day_fills  varchar2(40),
  ge65_tot_drug_cst     varchar2(40),
  ge65_tot_day_suply    varchar2(40),
  ge65_bene_sprsn_flag  varchar2(20),
  ge65_tot_benes        varchar2(40)
)
organization external (
  type oracle_loader
  default directory &_USER._DATA
  access parameters (
    records delimited by 0x'0A'
    characterset al32utf8
    skip 1
    badfile &_USER._LOG:'ext_partd.bad'
    logfile &_USER._LOG:'ext_partd.log'
    nodiscardfile
    fields terminated by ',' optionally enclosed by '"'
    missing field values are null
  )
  location ('partd_2024.csv')
)
reject limit unlimited;

create table stg_partd (
  data_year            number(4) not null,
  npi                  number(10) not null,
  last_org_name        varchar2(200),
  first_name           varchar2(100),
  city                 varchar2(100),
  state                varchar2(2),
  state_fips           varchar2(2),
  prescriber_type      varchar2(200),
  type_source          varchar2(50),
  brnd_name            varchar2(200) not null,
  gnrc_name            varchar2(200) not null,
  tot_clms             number not null,
  tot_30day_fills      number,
  tot_day_suply        number,
  tot_drug_cst         number(14, 2),
  tot_benes            number,
  ge65_sprsn_flag      varchar2(1),
  ge65_tot_clms        number,
  ge65_tot_30day_fills number,
  ge65_tot_drug_cst    number(14, 2),
  ge65_tot_day_suply   number,
  ge65_bene_sprsn_flag varchar2(1),
  ge65_tot_benes       number
) nologging;

begin
  dbms_errlog.create_error_log('STG_PARTD', 'ERR_STG_PARTD');
end;
/

create view v_stg_prescriber as
select npi, last_org_name, first_name, city, state, state_fips, prescriber_type, type_source, row_hash
  from (
    select npi, last_org_name, first_name, city, state, state_fips, prescriber_type, type_source,
           standard_hash(last_org_name || '|' || first_name || '|' || city || '|' || state || '|' ||
                         state_fips || '|' || prescriber_type || '|' || type_source, 'SHA256') row_hash,
           row_number() over (
             partition by npi
             order by count(*) desc, last_org_name, first_name, city, prescriber_type, type_source
           ) pick
      from stg_partd
     group by npi, last_org_name, first_name, city, state, state_fips, prescriber_type, type_source
  )
 where pick = 1;

create table dim_prescriber (
  prescriber_key  number generated always as identity primary key,
  npi             number(10) not null,
  last_org_name   varchar2(200),
  first_name      varchar2(100),
  city            varchar2(100),
  state           varchar2(2),
  state_fips      varchar2(2),
  prescriber_type varchar2(200),
  type_source     varchar2(50),
  row_hash        raw(32) not null,
  valid_from_year number(4) not null,
  valid_to_year   number(4) not null,
  is_current      boolean not null,
  run_id          number not null,
  constraint uq_prescriber_version unique (npi, valid_from_year)
);

create index ix_prescriber_npi on dim_prescriber (npi, valid_from_year, valid_to_year, prescriber_key);

create table dim_drug (
  drug_key     number generated always as identity primary key,
  brand_name   varchar2(200) not null,
  generic_name varchar2(200) not null,
  is_generic   boolean not null,
  run_id       number not null,
  constraint uq_drug unique (brand_name, generic_name)
);

alter table dim_prescriber modify primary key rely;
alter table dim_drug modify primary key rely;

create table fact_prescription (
  data_year            number(4) not null,
  prescriber_key       number not null,
  drug_key             number not null,
  tot_clms             number not null,
  tot_30day_fills      number,
  tot_day_suply        number,
  tot_drug_cst         number(14, 2),
  tot_benes            number,
  ge65_sprsn_flag      varchar2(1),
  ge65_tot_clms        number,
  ge65_tot_30day_fills number,
  ge65_tot_drug_cst    number(14, 2),
  ge65_tot_day_suply   number,
  ge65_bene_sprsn_flag varchar2(1),
  ge65_tot_benes       number,
  run_id               number not null,
  constraint fk_fact_prescriber foreign key (prescriber_key)
    references dim_prescriber (prescriber_key) rely disable novalidate,
  constraint fk_fact_drug foreign key (drug_key)
    references dim_drug (drug_key) rely disable novalidate
)
row store compress basic
partition by range (data_year) interval (1)
(partition p_before_2013 values less than (2013));

create bitmap index bx_fact_prescriber on fact_prescription (prescriber_key) local;
create bitmap index bx_fact_drug on fact_prescription (drug_key) local;

create table wrk_fact_prescription for exchange with table fact_prescription;
alter table wrk_fact_prescription row store compress basic;

begin
  dbms_stats.set_table_prefs(user, 'FACT_PRESCRIPTION', 'INCREMENTAL', 'TRUE');
  dbms_stats.set_table_prefs(user, 'WRK_FACT_PRESCRIPTION', 'INCREMENTAL', 'TRUE');
  dbms_stats.set_table_prefs(user, 'WRK_FACT_PRESCRIPTION', 'INCREMENTAL_LEVEL', 'TABLE');
end;
/

create materialized view mv_drug_year
  build immediate
  refresh force on demand
  enable query rewrite
as
select f.data_year, d.generic_name, d.brand_name, d.is_generic,
       count(*) row_count,
       sum(f.tot_clms) tot_clms, count(f.tot_clms) tot_clms_count,
       sum(f.tot_drug_cst) tot_drug_cst, count(f.tot_drug_cst) tot_drug_cst_count,
       sum(f.tot_30day_fills) tot_30day_fills, count(f.tot_30day_fills) tot_30day_fills_count
  from fact_prescription f
  join dim_drug d on d.drug_key = f.drug_key
 group by f.data_year, d.generic_name, d.brand_name, d.is_generic;

create materialized view mv_specialty_year
  build immediate
  refresh force on demand
  enable query rewrite
as
select f.data_year, p.prescriber_type,
       count(*) row_count,
       sum(f.tot_clms) tot_clms, count(f.tot_clms) tot_clms_count,
       sum(f.tot_drug_cst) tot_drug_cst, count(f.tot_drug_cst) tot_drug_cst_count,
       sum(f.tot_day_suply) tot_day_suply, count(f.tot_day_suply) tot_day_suply_count
  from fact_prescription f
  join dim_prescriber p on p.prescriber_key = f.prescriber_key
 group by f.data_year, p.prescriber_type;
