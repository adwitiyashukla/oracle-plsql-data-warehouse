declare
  l_steps sys.odcivarchar2list := sys.odcivarchar2list('STAGE', 'DIMS', 'FACT', 'REFRESH', 'QUALITY');

  procedure drop_quietly(p_kind in varchar2, p_name in varchar2) is
  begin
    if p_kind = 'CHAIN' then
      dbms_scheduler.drop_chain(p_name, force => true);
    else
      dbms_scheduler.drop_program(p_name, force => true);
    end if;
  exception
    when others then
      if sqlcode not in (-27476, -23308) then
        raise;
      end if;
  end drop_quietly;
begin
  drop_quietly('CHAIN', 'GRANARY_CHAIN');
  for i in 1 .. l_steps.count loop
    drop_quietly('PROGRAM', 'GRANARY_' || l_steps(i));
    dbms_scheduler.create_program(
      program_name        => 'GRANARY_' || l_steps(i),
      program_type        => 'STORED_PROCEDURE',
      program_action      => 'GRANARY_LOAD.CHAIN_STEP',
      number_of_arguments => 1,
      enabled             => false);
    dbms_scheduler.define_program_argument(
      program_name      => 'GRANARY_' || l_steps(i),
      argument_position => 1,
      argument_name     => 'P_STEP',
      argument_type     => 'VARCHAR2',
      default_value     => l_steps(i));
    dbms_scheduler.enable('GRANARY_' || l_steps(i));
  end loop;

  dbms_scheduler.create_chain(chain_name => 'GRANARY_CHAIN');
  for i in 1 .. l_steps.count loop
    dbms_scheduler.define_chain_step('GRANARY_CHAIN', l_steps(i), 'GRANARY_' || l_steps(i));
  end loop;
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'TRUE', 'START STAGE');
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'STAGE SUCCEEDED', 'START DIMS');
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'DIMS SUCCEEDED', 'START FACT');
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'FACT SUCCEEDED', 'START REFRESH');
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'REFRESH SUCCEEDED', 'START QUALITY');
  dbms_scheduler.define_chain_rule('GRANARY_CHAIN', 'QUALITY COMPLETED', 'END');
  dbms_scheduler.define_chain_rule(
    'GRANARY_CHAIN', 'STAGE FAILED OR DIMS FAILED OR FACT FAILED OR REFRESH FAILED', 'END');
  dbms_scheduler.enable('GRANARY_CHAIN');
end;
/
