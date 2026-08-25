CREATE TABLE IF NOT EXISTS claims_failure (
    claim_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    failed_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_enrollment (
    member_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    enrollment_status TEXT NOT NULL,
    enrolled_at TIMESTAMP NOT NULL,
    source_system TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_summary (
    invoice_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    billing_period TEXT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    invoice_status TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_job_failure (
    run_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    failed_at TIMESTAMP NOT NULL,
    failure_reason TEXT NOT NULL,
    severity TEXT NOT NULL
);

INSERT INTO claims_failure (claim_id, state, failure_reason, failed_at, status) VALUES
    ('CLM-1001', 'Texas', 'Eligibility service timeout', '2026-08-18 08:15:00', 'FAILED'),
    ('CLM-1002', 'Texas', 'Provider validation failed', '2026-08-21 13:45:00', 'FAILED'),
    ('CLM-1003', 'Florida', 'Duplicate claim detected', '2026-08-20 09:30:00', 'FAILED'),
    ('CLM-1004', 'Texas', 'Missing member identifier', '2026-07-11 10:10:00', 'FAILED')
ON CONFLICT (claim_id) DO NOTHING;

INSERT INTO member_enrollment (member_id, state, enrollment_status, enrolled_at, source_system) VALUES
    ('MBR-2001', 'Texas', 'ACTIVE', '2026-07-03 11:25:00', 'PORTAL'),
    ('MBR-2002', 'Texas', 'PENDING', '2026-07-18 16:40:00', 'BATCH'),
    ('MBR-2003', 'California', 'ACTIVE', '2026-07-20 12:05:00', 'PORTAL'),
    ('MBR-2004', 'Texas', 'ACTIVE', '2026-08-04 09:00:00', 'BATCH')
ON CONFLICT (member_id) DO NOTHING;

INSERT INTO billing_summary (invoice_id, state, billing_period, amount, invoice_status, created_at) VALUES
    ('INV-3001', 'Texas', '2026-07', 12850.25, 'POSTED', '2026-07-31 18:00:00'),
    ('INV-3002', 'Texas', '2026-07', 9420.00, 'PENDING', '2026-07-30 17:20:00'),
    ('INV-3003', 'New York', '2026-07', 7200.50, 'POSTED', '2026-07-29 14:15:00'),
    ('INV-3004', 'Texas', '2026-08', 3300.00, 'DRAFT', '2026-08-08 10:00:00')
ON CONFLICT (invoice_id) DO NOTHING;

INSERT INTO batch_job_failure (run_id, job_name, failed_at, failure_reason, severity) VALUES
    ('RUN-4001', 'claims-loader', '2026-08-18 01:15:00', 'Input file schema mismatch', 'HIGH'),
    ('RUN-4002', 'member-enrollment-sync', '2026-08-22 02:30:00', 'Source API timeout', 'MEDIUM'),
    ('RUN-4003', 'billing-rollup', '2026-07-19 03:00:00', 'Database lock timeout', 'MEDIUM')
ON CONFLICT (run_id) DO NOTHING;
