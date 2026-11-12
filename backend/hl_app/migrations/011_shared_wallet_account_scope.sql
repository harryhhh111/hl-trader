DROP INDEX hyperliquid_api_wallets_active_address_idx;

CREATE TABLE hyperliquid_api_wallet_account_bindings (
    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
    wallet_address CHAR(42) NOT NULL CHECK (wallet_address ~ '^0x[0-9a-f]{40}$' AND wallet_address <> '0x0000000000000000000000000000000000000000'),
    account_address CHAR(42) NOT NULL CHECK (account_address ~ '^0x[0-9a-f]{40}$' AND account_address <> '0x0000000000000000000000000000000000000000' AND account_address <> wallet_address),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (network, wallet_address)
);

INSERT INTO hyperliquid_api_wallet_account_bindings (network, wallet_address, account_address)
SELECT network, wallet_address, account_address
FROM hyperliquid_api_wallets WHERE status = 'active';

CREATE INDEX hyperliquid_api_wallets_active_address_idx
    ON hyperliquid_api_wallets (network, wallet_address) WHERE status = 'active';
