whenever sqlerror exit failure
set verify off
set feedback off

define app_user = &1
define app_password = &2
define data_path = &3
define log_path = &4

alter session set container = FREEPDB1;

drop user if exists &app_user cascade;

create user &app_user identified by "&app_password"
  default tablespace users
  quota unlimited on users;

grant create session, create table, create view, create sequence, create procedure,
      create type, create materialized view, create job, query rewrite
  to &app_user;

create or replace directory &app_user._data as '&data_path';
create or replace directory &app_user._log as '&log_path';

grant read on directory &app_user._data to &app_user;
grant read, write on directory &app_user._log to &app_user;

begin
  dbms_rule_adm.grant_system_privilege(dbms_rule_adm.create_rule_obj, upper('&app_user'));
  dbms_rule_adm.grant_system_privilege(dbms_rule_adm.create_rule_set_obj, upper('&app_user'));
  dbms_rule_adm.grant_system_privilege(dbms_rule_adm.create_evaluation_context_obj, upper('&app_user'));
end;
/

prompt user &app_user is ready
exit
