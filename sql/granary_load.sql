create or replace package granary_load as
  function npi_is_valid(p_npi in number) return varchar2 deterministic;
  procedure reset;
  function start_run(p_year in number, p_via_chain in boolean default false) return number;
  procedure stage(p_run_id in number);
  procedure load_dims(p_run_id in number);
  procedure load_fact(p_run_id in number);
  procedure refresh_marts(p_run_id in number);
  procedure check_quality(p_run_id in number);
  procedure run_year(p_year in number);
  function submit_chain(p_year in number) return number;
  procedure chain_step(p_step in varchar2);
end granary_load;
/

create or replace package body granary_load as

  type number_list is table of number;

  function year_of(p_run_id in number) return number is
    l_year number;
  begin
    select data_year into l_year from load_run where run_id = p_run_id;
    return l_year;
  end year_of;

  procedure step_begin(p_run_id in number, p_step in varchar2) is
  begin
    dbms_application_info.set_module('granary_load', p_step);
    insert into load_step (run_id, step_name, started_at) values (p_run_id, p_step, systimestamp);
    commit;
  end step_begin;

  procedure step_end(p_run_id in number, p_step in varchar2, p_rows in number) is
  begin
    update load_step
       set finished_at = systimestamp, rows_processed = p_rows
     where run_id = p_run_id and step_name = p_step;
    commit;
    dbms_application_info.set_module(null, null);
  end step_end;

  procedure fail_run(p_run_id in number, p_message in varchar2) is
    pragma autonomous_transaction;
  begin
    update load_run
       set status = 'FAILED', finished_at = systimestamp, error_message = substr(p_message, 1, 4000)
     where run_id = p_run_id;
    commit;
  end fail_run;

  procedure add_check(p_run_id in number, p_name in varchar2, p_severity in varchar2, p_failed in number) is
  begin
    insert into dq_result (run_id, check_name, severity, failed_rows)
    values (p_run_id, p_name, p_severity, nvl(p_failed, 0));
  end add_check;

  function npi_is_valid(p_npi in number) return varchar2 deterministic is
    pragma udf;
    l_text  varchar2(40) := to_char(p_npi);
    l_sum   pls_integer := 24;
    l_digit pls_integer;
  begin
    if p_npi is null or length(l_text) != 10 or translate(l_text, 'x0123456789', 'x') is not null then
      return 'N';
    end if;
    for i in 1 .. 9 loop
      l_digit := to_number(substr(l_text, i, 1));
      if mod(i, 2) = 1 then
        l_digit := l_digit * 2;
        if l_digit > 9 then
          l_digit := l_digit - 9;
        end if;
      end if;
      l_sum := l_sum + l_digit;
    end loop;
    if mod(10 - mod(l_sum, 10), 10) = to_number(substr(l_text, 10, 1)) then
      return 'Y';
    end if;
    return 'N';
  end npi_is_valid;

  procedure reset is
  begin
    execute immediate 'truncate table fact_prescription';
    execute immediate 'truncate table wrk_fact_prescription';
    execute immediate 'truncate table stg_partd';
    execute immediate 'truncate table err_stg_partd';
    execute immediate 'truncate table dim_prescriber';
    execute immediate 'truncate table dim_drug';
    execute immediate 'truncate table dq_result';
    execute immediate 'truncate table load_step';
    execute immediate 'truncate table load_run';
    dbms_mview.refresh('MV_DRUG_YEAR,MV_SPECIALTY_YEAR', method => 'CC', atomic_refresh => false);
  end reset;

  function start_run(p_year in number, p_via_chain in boolean default false) return number is
    l_latest number;
    l_run_id number;
  begin
    if p_year is null or p_year != trunc(p_year) or p_year not between 2013 and 2100 then
      raise_application_error(-20002, 'year must be a whole number between 2013 and 2100');
    end if;
    select max(data_year) into l_latest from load_run where status = 'DONE';
    if p_year < l_latest then
      raise_application_error(-20001, 'year ' || p_year || ' is older than the latest loaded year '
        || l_latest || ', reset and load the years in order');
    end if;
    insert into load_run (data_year, status, via_chain, started_at)
    values (p_year, 'RUNNING', p_via_chain, systimestamp)
    returning run_id into l_run_id;
    commit;
    return l_run_id;
  end start_run;

  procedure stage(p_run_id in number) is
    l_year     number := year_of(p_run_id);
    l_rows     number;
    l_rejected number;
  begin
    step_begin(p_run_id, 'STAGE');
    execute immediate q'[alter session set nls_numeric_characters = '.,']';
    execute immediate 'truncate table stg_partd';
    execute immediate
      'insert /*+ append */ into stg_partd (
         data_year, npi, last_org_name, first_name, city, state, state_fips, prescriber_type,
         type_source, brnd_name, gnrc_name, tot_clms, tot_30day_fills, tot_day_suply,
         tot_drug_cst, tot_benes, ge65_sprsn_flag, ge65_tot_clms, ge65_tot_30day_fills,
         ge65_tot_drug_cst, ge65_tot_day_suply, ge65_bene_sprsn_flag, ge65_tot_benes)
       select :year, prscrbr_npi, prscrbr_last_org_name, prscrbr_first_name, prscrbr_city,
              prscrbr_state_abrvtn, prscrbr_state_fips, prscrbr_type, prscrbr_type_src,
              brnd_name, gnrc_name, tot_clms, tot_30day_fills, tot_day_suply, tot_drug_cst,
              tot_benes, ge65_sprsn_flag, ge65_tot_clms, ge65_tot_30day_fills,
              ge65_tot_drug_cst, ge65_tot_day_suply, ge65_bene_sprsn_flag,
              rtrim(ge65_tot_benes, chr(13))
         from ext_partd external modify (location (''partd_' || l_year || '.csv''))
       log errors into err_stg_partd (''' || p_run_id || ''') reject limit unlimited'
      using l_year;
    l_rows := sql%rowcount;
    commit;
    select count(*) into l_rejected from err_stg_partd where ora_err_tag$ = to_char(p_run_id);
    update load_run set rows_staged = l_rows, rows_rejected = l_rejected where run_id = p_run_id;
    commit;
    step_end(p_run_id, 'STAGE', l_rows);
  end stage;

  procedure load_dims(p_run_id in number) is
    l_year   number := year_of(p_run_id);
    l_closed number;
    l_added  number;
    l_drugs  number;
  begin
    step_begin(p_run_id, 'DIMS');
    delete from dim_prescriber where valid_from_year = l_year;
    update dim_prescriber
       set valid_to_year = 9999, is_current = true
     where valid_to_year = l_year - 1;
    update dim_prescriber d
       set d.valid_to_year = l_year - 1, d.is_current = false
     where d.is_current
       and exists (select 1 from v_stg_prescriber s where s.npi = d.npi and s.row_hash != d.row_hash);
    l_closed := sql%rowcount;
    insert into dim_prescriber (
      npi, last_org_name, first_name, city, state, state_fips, prescriber_type, type_source,
      row_hash, valid_from_year, valid_to_year, is_current, run_id)
    select s.npi, s.last_org_name, s.first_name, s.city, s.state, s.state_fips, s.prescriber_type,
           s.type_source, s.row_hash, l_year, 9999, true, p_run_id
      from v_stg_prescriber s
     where not exists (select 1 from dim_prescriber d where d.npi = s.npi and d.is_current);
    l_added := sql%rowcount;
    insert into dim_drug (brand_name, generic_name, is_generic, run_id)
    select s.brnd_name, s.gnrc_name,
           case when upper(s.brnd_name) = upper(s.gnrc_name) then true else false end, p_run_id
      from (select distinct brnd_name, gnrc_name from stg_partd) s
     where not exists (
       select 1 from dim_drug d where d.brand_name = s.brnd_name and d.generic_name = s.gnrc_name);
    l_drugs := sql%rowcount;
    update load_run
       set prescribers_closed = l_closed, prescribers_added = l_added, drugs_added = l_drugs
     where run_id = p_run_id;
    commit;
    step_end(p_run_id, 'DIMS', l_closed + l_added + l_drugs);
  end load_dims;

  procedure load_fact(p_run_id in number) is
    l_year number := year_of(p_run_id);
    l_rows number;
  begin
    step_begin(p_run_id, 'FACT');
    execute immediate 'lock table fact_prescription partition for (' || l_year || ') in share mode';
    execute immediate 'truncate table wrk_fact_prescription';
    insert /*+ append */ into wrk_fact_prescription (
      data_year, prescriber_key, drug_key, tot_clms, tot_30day_fills, tot_day_suply, tot_drug_cst,
      tot_benes, ge65_sprsn_flag, ge65_tot_clms, ge65_tot_30day_fills, ge65_tot_drug_cst,
      ge65_tot_day_suply, ge65_bene_sprsn_flag, ge65_tot_benes, run_id)
    select s.data_year, p.prescriber_key, d.drug_key, s.tot_clms, s.tot_30day_fills, s.tot_day_suply,
           s.tot_drug_cst, s.tot_benes, s.ge65_sprsn_flag, s.ge65_tot_clms, s.ge65_tot_30day_fills,
           s.ge65_tot_drug_cst, s.ge65_tot_day_suply, s.ge65_bene_sprsn_flag, s.ge65_tot_benes, p_run_id
      from stg_partd s
      join dim_prescriber p
        on p.npi = s.npi and s.data_year between p.valid_from_year and p.valid_to_year
      join dim_drug d
        on d.brand_name = s.brnd_name and d.generic_name = s.gnrc_name;
    l_rows := sql%rowcount;
    commit;
    dbms_stats.gather_table_stats(user, 'WRK_FACT_PRESCRIPTION');
    execute immediate 'alter table fact_prescription exchange partition for (' || l_year
      || ') with table wrk_fact_prescription without validation';
    execute immediate 'alter table fact_prescription modify partition for (' || l_year
      || ') rebuild unusable local indexes';
    execute immediate 'truncate table wrk_fact_prescription';
    dbms_stats.gather_table_stats(user, 'FACT_PRESCRIPTION');
    dbms_stats.gather_table_stats(user, 'DIM_PRESCRIBER');
    dbms_stats.gather_table_stats(user, 'DIM_DRUG');
    update load_run set fact_rows = l_rows where run_id = p_run_id;
    commit;
    step_end(p_run_id, 'FACT', l_rows);
  end load_fact;

  procedure refresh_marts(p_run_id in number) is
    l_types varchar2(400);
  begin
    step_begin(p_run_id, 'REFRESH');
    dbms_mview.refresh('MV_DRUG_YEAR,MV_SPECIALTY_YEAR', method => '??', atomic_refresh => false);
    select listagg(lower(mview_name) || '=' || lower(last_refresh_type), ', ') within group (order by mview_name)
      into l_types
      from user_mviews;
    update load_run set refresh_types = l_types where run_id = p_run_id;
    commit;
    step_end(p_run_id, 'REFRESH', 2);
  end refresh_marts;

  procedure check_quality(p_run_id in number) is
    l_year       number := year_of(p_run_id);
    l_staged     number;
    l_rejected   number;
    l_fact_rows  number;
    l_fact_cost  number;
    l_stage_cost number;
    l_count      number;
    l_errors     number;
    l_npis       number_list;
  begin
    step_begin(p_run_id, 'QUALITY');
    delete from dq_result where run_id = p_run_id;
    select rows_staged, rows_rejected into l_staged, l_rejected from load_run where run_id = p_run_id;
    add_check(p_run_id, 'reject_rate_over_1_percent', 'WARN',
              case when l_rejected > 0.01 * (l_staged + l_rejected) then l_rejected else 0 end);

    select count(*), nvl(sum(tot_drug_cst), 0) into l_fact_rows, l_fact_cost
      from fact_prescription where data_year = l_year;
    select nvl(sum(tot_drug_cst), 0) into l_stage_cost from stg_partd;
    add_check(p_run_id, 'fact_rows_match_stage', 'ERROR', abs(l_fact_rows - l_staged));
    add_check(p_run_id, 'fact_cost_matches_stage', 'ERROR',
              case when abs(l_fact_cost - l_stage_cost) > 0.005 then 1 else 0 end);

    select count(*) into l_count
      from (select prescriber_key, drug_key
              from fact_prescription
             where data_year = l_year
             group by prescriber_key, drug_key
            having count(*) > 1);
    add_check(p_run_id, 'duplicate_fact_grain', 'ERROR', l_count);

    select count(*) into l_count
      from (select npi from dim_prescriber where is_current group by npi having count(*) > 1);
    add_check(p_run_id, 'many_current_versions', 'ERROR', l_count);

    select count(*) into l_count
      from (select valid_from_year, valid_to_year,
                   lag(valid_to_year) over (partition by npi order by valid_from_year) prev_to
              from dim_prescriber)
     where prev_to >= valid_from_year or valid_to_year < valid_from_year;
    add_check(p_run_id, 'overlapping_versions', 'ERROR', l_count);

    select count(*) into l_count
      from stg_partd
     where tot_clms < 0 or tot_drug_cst < 0 or tot_day_suply < 0 or tot_30day_fills < 0;
    add_check(p_run_id, 'negative_amounts', 'ERROR', l_count);

    select distinct npi bulk collect into l_npis from stg_partd;
    l_count := 0;
    for i in 1 .. l_npis.count loop
      if npi_is_valid(l_npis(i)) = 'N' then
        l_count := l_count + 1;
      end if;
    end loop;
    add_check(p_run_id, 'npi_check_digit', 'WARN', l_count);

    select count(*) into l_count
      from stg_partd
     where ge65_tot_clms > tot_clms or ge65_tot_drug_cst > tot_drug_cst;
    add_check(p_run_id, 'age_65_part_over_total', 'WARN', l_count);
    commit;

    select count(*) into l_errors
      from dq_result
     where run_id = p_run_id and severity = 'ERROR' and failed_rows > 0;
    if l_errors > 0 then
      raise_application_error(-20003, l_errors || ' data quality checks failed for run ' || p_run_id);
    end if;
    step_end(p_run_id, 'QUALITY', 9);
    update load_run set status = 'DONE', finished_at = systimestamp where run_id = p_run_id;
    commit;
  end check_quality;

  procedure run_year(p_year in number) is
    l_run_id number := start_run(p_year);
  begin
    stage(l_run_id);
    load_dims(l_run_id);
    load_fact(l_run_id);
    refresh_marts(l_run_id);
    check_quality(l_run_id);
  exception
    when others then
      fail_run(l_run_id, dbms_utility.format_error_stack);
      raise;
  end run_year;

  function submit_chain(p_year in number) return number is
    l_run_id number := start_run(p_year, true);
  begin
    dbms_scheduler.run_chain(
      chain_name  => 'GRANARY_CHAIN',
      start_steps => null,
      job_name    => 'GRANARY_RUN_' || l_run_id);
    return l_run_id;
  exception
    when others then
      fail_run(l_run_id, dbms_utility.format_error_stack);
      raise;
  end submit_chain;

  procedure chain_step(p_step in varchar2) is
    l_run_id number;
  begin
    select max(run_id) into l_run_id from load_run where status = 'RUNNING' and via_chain;
    if l_run_id is null then
      raise_application_error(-20004, 'no chain run is waiting');
    end if;
    case p_step
      when 'STAGE' then stage(l_run_id);
      when 'DIMS' then load_dims(l_run_id);
      when 'FACT' then load_fact(l_run_id);
      when 'REFRESH' then refresh_marts(l_run_id);
      when 'QUALITY' then check_quality(l_run_id);
      else raise_application_error(-20005, 'unknown step ' || p_step);
    end case;
  exception
    when others then
      if l_run_id is not null then
        fail_run(l_run_id, p_step || ': ' || dbms_utility.format_error_stack);
      end if;
      raise;
  end chain_step;

end granary_load;
/
