CREATE TABLE securities (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('US', 'CN_HK', 'CN_A')),
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'HKD', 'CNY')),
    quote_source TEXT NOT NULL CHECK (quote_source = 'tencent'),
    quote_symbol TEXT NOT NULL,
    exchange_suffix TEXT,
    cik CHAR(10) CHECK (cik IS NULL OR cik ~ '^[0-9]{10}$'),
    industry TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (market, stock_code),
    UNIQUE (market, quote_source, quote_symbol),
    CHECK (
        (market = 'US' AND stock_code ~ '^[A-Z0-9]+([.-][A-Z0-9]+)*$' AND currency = 'USD' AND exchange_suffix IS NOT NULL)
        OR (market = 'CN_HK' AND stock_code ~ '^[0-9]{5}$' AND currency = 'HKD' AND exchange_suffix IS NULL)
        OR (market = 'CN_A' AND stock_code ~ '^[0-9]{6}$' AND currency = 'CNY' AND exchange_suffix IS NULL)
    )
);

CREATE TABLE watchlist_items (
    id BIGSERIAL PRIMARY KEY,
    security_id BIGINT NOT NULL REFERENCES securities(id),
    group_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    research_url TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (security_id)
);

CREATE TABLE daily_quotes (
    id BIGSERIAL PRIMARY KEY,
    market TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    trade_date DATE NOT NULL,
    open DOUBLE PRECISION CHECK (open IS NULL OR (open::text NOT IN ('NaN', 'Infinity', '-Infinity') AND open > 0)),
    high DOUBLE PRECISION CHECK (high IS NULL OR (high::text NOT IN ('NaN', 'Infinity', '-Infinity') AND high > 0)),
    low DOUBLE PRECISION CHECK (low IS NULL OR (low::text NOT IN ('NaN', 'Infinity', '-Infinity') AND low > 0)),
    close DOUBLE PRECISION CHECK (close IS NULL OR (close::text NOT IN ('NaN', 'Infinity', '-Infinity') AND close > 0)),
    volume BIGINT CHECK (volume IS NULL OR volume >= 0),
    source TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'HKD', 'CNY')),
    price_basis TEXT NOT NULL DEFAULT 'raw_unadjusted' CHECK (price_basis = 'raw_unadjusted'),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (market, stock_code) REFERENCES securities(market, stock_code),
    UNIQUE (market, stock_code, trade_date)
);

CREATE INDEX daily_quotes_recent_idx ON daily_quotes (market, stock_code, trade_date DESC);
CREATE INDEX watchlist_items_enabled_idx ON watchlist_items (enabled, security_id);
