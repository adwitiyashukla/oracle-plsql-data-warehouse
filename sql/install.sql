whenever sqlerror exit failure
set feedback off
set verify off

prompt tables, external table, marts
@@schema.sql

prompt load package
@@granary_load.sql

column name format a20
column text format a90
select name, line, text from user_errors order by name, sequence;

declare
  l_errors pls_integer;
begin
  select count(*) into l_errors from user_errors;
  if l_errors > 0 then
    raise_application_error(-20000, l_errors || ' compile errors, see the list above');
  end if;
end;
/

prompt scheduler chain
@@scheduler.sql

prompt granary is installed
exit
