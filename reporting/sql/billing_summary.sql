SELECT
    invoice_id,
    state,
    billing_period,
    amount,
    invoice_status,
    created_at
FROM billing_summary
WHERE created_at::date >= %(start_date)s
  AND created_at::date <= %(end_date)s
  AND (%(state)s IS NULL OR state = %(state)s)
ORDER BY created_at DESC
