import time

import granary


def test_chain_runs_every_step_in_order(conn):
    cur = conn.cursor()
    cur.callproc("granary_load.reset")
    run_id = cur.callfunc("granary_load.submit_chain", int, [2022])
    status, error = granary.wait_for_run(cur, run_id, timeout=300)
    assert status == "DONE", error
    cur.execute(
        "select step_name from load_step where run_id = :r and finished_at is not null order by started_at",
        r=run_id,
    )
    assert [r[0] for r in cur] == granary.STEPS
    details = {}
    for _ in range(30):
        cur.execute(
            "select job_subname, status from user_scheduler_job_run_details "
            "where job_name = :j and job_subname is not null",
            j=f"GRANARY_RUN_{run_id}",
        )
        details = dict(cur.fetchall())
        if len(details) == len(granary.STEPS):
            break
        time.sleep(1)
    assert details == {step: "SUCCEEDED" for step in granary.STEPS}
