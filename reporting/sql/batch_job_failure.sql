SELECT
    job_name,
    run_id,
    failed_at,
    failure_reason,
    severity
FROM batch_job_failure
WHERE failed_at::date >= %(start_date)s
  AND failed_at::date <= %(end_date)s
  AND (%(job_name)s IS NULL OR job_name = %(job_name)s)
ORDER BY failed_at DESC
