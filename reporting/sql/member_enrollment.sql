SELECT
    member_id,
    state,
    enrollment_status,
    enrolled_at,
    source_system
FROM member_enrollment
WHERE enrolled_at::date >= %(start_date)s
  AND enrolled_at::date <= %(end_date)s
  AND (%(state)s IS NULL OR state = %(state)s)
ORDER BY enrolled_at DESC
