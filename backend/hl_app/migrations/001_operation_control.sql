CREATE TABLE operation_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    mode TEXT NOT NULL DEFAULT 'maintenance' CHECK (mode IN ('maintenance', 'active')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO operation_control (singleton) VALUES (true);
