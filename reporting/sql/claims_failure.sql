SELECT
    claim_id,
    state,
    failure_reason,
    failed_at,
    status
FROM claims_failure
WHERE failed_at::date >= %(start_date)s
  AND failed_at::date <= %(end_date)s
  AND (%(state)s IS NULL OR state = %(state)s)
ORDER BY failed_at DESC
