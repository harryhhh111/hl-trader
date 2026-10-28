CREATE TABLE auth_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    auth_epoch BIGINT NOT NULL CHECK (auth_epoch >= 1),
    token_sha256 CHAR(64) NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth_sessions (
    session_sha256 CHAR(64) PRIMARY KEY CHECK (session_sha256 ~ '^[0-9a-f]{64}$'),
    csrf_sha256 CHAR(64) NOT NULL CHECK (csrf_sha256 ~ '^[0-9a-f]{64}$'),
    auth_epoch BIGINT NOT NULL CHECK (auth_epoch >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX auth_sessions_active_idx ON auth_sessions (auth_epoch, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE login_rate_limit (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    locked_until TIMESTAMPTZ
);

INSERT INTO login_rate_limit (singleton) VALUES (true);
