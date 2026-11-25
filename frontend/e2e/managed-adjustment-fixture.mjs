export const reply = (route, data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });

export async function mockManagedWallet(page, { configured = true, activeExecution = null } = {}) {
  await page.route("**/api/v1/hyperliquid/pairs/nbis-crwv/api-wallet?*", route => reply(route, {
    configured, network: "mainnet", pair_id: "nbis-crwv",
    account_address: "0x0000000000000000000000000000000000000043",
    wallet_address: "0x1111111111111111111111111111111111111111", wallet_version: 1,
    active_execution: activeExecution,
  }));
}

export function managedReport(body, overrides = {}) {
  return {
    schema_version: 1, status: "preview", managed_status: "previewed", action_id: null,
    pair_id: "nbis-crwv", network: "mainnet", config_revision: body.config_revision,
    account_address: body.account_address, target_notional: body.target_notional,
    max_slippage_bps: body.max_slippage_bps ?? 5, request_id: body.request_id, wallet_version: 1,
    legs: ["xyz:NBIS", "xyz:CRWV"].map((coin, index) => ({
      coin, position_side: index ? "short" : "long", current_size: "0", current_notional: "0",
      target_size: "20", estimated_target_notional: body.target_notional, action: index ? "sell" : "buy",
      reduce_only: false, order_qty: "1", mid_px: "100", protect_px: index ? "99.95" : "100.05",
      status: "prepared", confirmed_qty: "0", avg_px: null, nonce: null, cloid: null, oid: null, result: null,
      final_size: null, final_notional: null,
    })), diagnostics: [], ...overrides,
  };
}
