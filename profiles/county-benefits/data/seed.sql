-- Synthetic data for a FICTIONAL county benefits office (Maple County Human Services).
-- No real person, agency or case. Names, amounts and dates are invented.
-- Build the database:  sqlite3 profiles/county-benefits/data/county.db < profiles/county-benefits/data/seed.sql
--
-- phone and street_address are stored but deliberately not named in records.yaml:
-- they are never read by the records server, so they can never reach a model.

CREATE TABLE applicants (
    id             INTEGER PRIMARY KEY,   -- cited as apl-1001
    name           TEXT NOT NULL,
    birth_year     INTEGER,
    district       TEXT,
    language       TEXT,
    phone          TEXT,                  -- never mapped, never read
    street_address TEXT                   -- never mapped, never read
);

CREATE TABLE cases (
    id           INTEGER PRIMARY KEY,     -- cited as case-2001
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    program      TEXT NOT NULL,           -- SNAP, Medicaid, Emergency Assistance
    status       TEXT NOT NULL,           -- pending, approved, denied, under review
    opened       TEXT NOT NULL,           -- ISO date
    next_step    TEXT
);

CREATE TABLE documents (
    id           INTEGER PRIMARY KEY,     -- cited as doc-3001
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,           -- received, missing, rejected
    due          TEXT                     -- ISO date, only while still needed
);

CREATE TABLE income (
    id             INTEGER PRIMARY KEY,   -- cited as inc-4001
    applicant_id   INTEGER NOT NULL REFERENCES applicants(id),
    source         TEXT NOT NULL,
    monthly_amount REAL,
    verified       TEXT NOT NULL          -- yes, no
);

CREATE TABLE payments (
    id           INTEGER PRIMARY KEY,     -- cited as pay-5001
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    program      TEXT NOT NULL,
    amount       REAL NOT NULL,
    issued_on    TEXT NOT NULL,           -- ISO date
    status       TEXT NOT NULL            -- issued, scheduled, held
);

INSERT INTO applicants VALUES
 (1001, 'Dana Whitfield',  1988, 'North', 'English', '555-0101', '12 Elm Row'),
 (1002, 'Marcus Oyelaran', 1975, 'River', 'English', '555-0102', '48 Mill Lane'),
 (1003, 'Rosa Delgado',    1992, 'South', 'Spanish', '555-0103', '7 Orchard Way'),
 (1004, 'Lena Kovacs',     1961, 'North', 'Hungarian', '555-0104', '301 Birch Court'),
 (1005, 'Tom Brandt',      2001, 'River', 'English', '555-0105', '9 Quarry Road');

INSERT INTO cases VALUES
 (2001, 1001, 'SNAP',                 'pending',      '2026-09-21', 'Waiting for proof of income'),
 (2002, 1001, 'Medicaid',             'approved',     '2026-03-02', 'Renewal due 2027-03'),
 (2003, 1002, 'SNAP',                 'under review', '2026-09-30', 'Interview scheduled 2026-10-14'),
 (2004, 1003, 'Medicaid',             'pending',      '2026-10-01', 'Waiting for proof of residency'),
 (2005, 1003, 'SNAP',                 'approved',     '2026-06-11', 'Recertification due 2026-12'),
 (2006, 1004, 'Emergency Assistance', 'pending',      '2026-10-05', 'Supervisor review of held payment'),
 (2007, 1005, 'SNAP',                 'denied',       '2026-08-18', 'Appeal window open until 2026-11-17');

INSERT INTO documents VALUES
 (3001, 1001, 'Proof of income (pay stubs, last 30 days)', 'missing',  '2026-10-21'),
 (3002, 1001, 'Photo ID',                                  'received', NULL),
 (3003, 1002, 'Proof of income (pay stubs, last 30 days)', 'received', NULL),
 (3004, 1002, 'Lease or proof of rent',                    'rejected', '2026-10-24'),
 (3005, 1003, 'Proof of residency',                        'missing',  '2026-10-31'),
 (3006, 1003, 'Photo ID',                                  'received', NULL),
 (3007, 1004, 'Utility shut-off notice',                   'received', NULL),
 (3008, 1005, 'Photo ID',                                  'received', NULL);

INSERT INTO income VALUES
 (4001, 1001, 'Part-time retail job', 1240.00, 'no'),
 (4002, 1002, 'Warehouse job',        2310.50, 'yes'),
 (4003, 1003, 'Childcare work',        980.00, 'yes'),
 (4004, 1004, 'Pension',               890.25, 'yes'),
 (4005, 1005, 'Gig delivery work',     1475.00, 'no');

INSERT INTO payments VALUES
 (5001, 1003, 'SNAP',                 412.00, '2026-09-01', 'issued'),
 (5002, 1003, 'SNAP',                 412.00, '2026-10-01', 'issued'),
 (5003, 1003, 'SNAP',                 412.00, '2026-11-01', 'scheduled'),
 (5004, 1004, 'Emergency Assistance', 650.00, '2026-10-07', 'held');