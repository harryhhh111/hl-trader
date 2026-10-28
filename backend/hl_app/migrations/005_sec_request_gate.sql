CREATE TABLE sec_request_gate (
    gate_key TEXT PRIMARY KEY CHECK (gate_key = 'sec_edgar'),
    next_allowed_at TIMESTAMPTZ NOT NULL,
    blocked_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

INSERT INTO sec_request_gate VALUES ('sec_edgar', clock_timestamp(), clock_timestamp(), clock_timestamp());
