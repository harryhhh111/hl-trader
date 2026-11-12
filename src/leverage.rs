use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use hypersdk::Address;
use hypersdk::hypercore::api::{Action, ActionRequest, UpdateLeverage};
use hypersdk::hypercore::{Chain, PrivateKeySigner, Signature};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::adjust::{
    AdjustError, BrowserSignature, new_cloid, normalize_signature, now_utc, signed_position,
    typed_data_for,
};
use crate::dashboard::config::{self, DashboardPair};
use crate::hyperliquid::{Adapter, find_market};
use crate::write_lock::WriteLock;

pub struct LeverageRequest<'a> {
    pub pair: &'a DashboardPair,
    pub config_path: &'a Path,
    pub config_revision: &'a str,
    pub account: Address,
    pub coin: &'a str,
    pub leverage: u32,
    pub margin_mode: &'a str,
    pub state_dir: &'a Path,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LeverageReport {
    pub schema_version: u8,
    pub status: String,
    pub action_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    pub pair_id: String,
    pub network: String,
    pub config_revision: String,
    pub account_address: String,
    pub coin: String,
    pub asset: usize,
    pub current_leverage: u32,
    pub current_margin_mode: String,
    pub leverage: u32,
    pub margin_mode: String,
    pub max_leverage: u64,
    pub start_positions: [Decimal; 2],
    pub expected_signer: String,
    pub nonce: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub typed_data: Option<serde_json::Value>,
    pub result: Option<String>,
    pub created_at: String,
}

struct Store {
    dir: PathBuf,
}

impl Store {
    fn new(request: &LeverageRequest<'_>) -> Self {
        Self {
            dir: request
                .state_dir
                .join(request.pair.descriptor.id.clone())
                .join(format!("{}", request.account).to_lowercase()),
        }
    }

    fn acquire(&self) -> Result<WriteLock, AdjustError> {
        fs::create_dir_all(&self.dir).map_err(|e| AdjustError::State(e.to_string()))?;
        let mut permissions = fs::metadata(&self.dir)
            .map_err(|e| AdjustError::State(e.to_string()))?
            .permissions();
        permissions.set_mode(0o700);
        fs::set_permissions(&self.dir, permissions)
            .map_err(|e| AdjustError::State(e.to_string()))?;
        WriteLock::at(&self.dir.join("adjust.lock")).map_err(AdjustError::State)
    }

    fn path(&self, action_id: &str) -> PathBuf {
        self.dir.join(format!("leverage-{action_id}.json"))
    }

    fn save(&self, report: &LeverageReport) -> Result<(), AdjustError> {
        fs::create_dir_all(&self.dir).map_err(|e| AdjustError::State(e.to_string()))?;
        let json =
            serde_json::to_string_pretty(report).map_err(|e| AdjustError::State(e.to_string()))?;
        let temporary = self
            .dir
            .join(format!("leverage.tmp.{}", std::process::id()));
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(|e| AdjustError::State(e.to_string()))?;
        use std::io::Write as _;
        file.write_all(json.as_bytes())
            .and_then(|_| file.sync_all())
            .map_err(|e| AdjustError::State(e.to_string()))?;
        fs::rename(&temporary, self.path(&report.action_id))
            .map_err(|e| AdjustError::State(e.to_string()))
    }

    fn load(&self, action_id: &str) -> Result<LeverageReport, AdjustError> {
        let text = fs::read_to_string(self.path(action_id))
            .map_err(|_| AdjustError::Refused("找不到待签名杠杆调整，请重新确认".into()))?;
        serde_json::from_str(&text).map_err(|e| AdjustError::State(format!("杠杆回执损坏: {e}")))
    }
}

fn chain_for(request: &LeverageRequest<'_>) -> Chain {
    match request.pair.descriptor.network {
        config::Network::Mainnet => Chain::Mainnet,
        config::Network::Testnet => Chain::Testnet,
    }
}

fn ensure_config(request: &LeverageRequest<'_>) -> Result<(), AdjustError> {
    let current =
        config::revision_at(request.config_path).map_err(|e| AdjustError::State(e.to_string()))?;
    if current != request.config_revision {
        return Err(AdjustError::Refused(
            "pair 配置已变化，请重新确认杠杆调整".into(),
        ));
    }
    Ok(())
}

fn validate_request(
    request: &LeverageRequest<'_>,
    expected_signer: Address,
) -> Result<(), AdjustError> {
    if request.leverage > crate::preflight::MAX_LEVERAGE {
        return Err(AdjustError::Refused(
            "本应用杠杆上限为 3x，请设置 1 到 3 倍".into(),
        ));
    }
    if expected_signer == Address::ZERO || expected_signer == request.account {
        return Err(AdjustError::Refused(
            "必须使用与原交易账户不同的 API Wallet 地址".into(),
        ));
    }
    if !request
        .pair
        .descriptor
        .coins
        .iter()
        .any(|coin| coin == request.coin)
    {
        return Err(AdjustError::Refused("合约不属于当前 pair".into()));
    }
    if request.margin_mode != "cross" && request.margin_mode != "isolated" {
        return Err(AdjustError::Refused(
            "保证金模式只支持 cross 或 isolated".into(),
        ));
    }
    ensure_config(request)
}

async fn market_for(
    adapter: &Adapter,
    request: &LeverageRequest<'_>,
) -> Result<hypersdk::hypercore::PerpMarket, AdjustError> {
    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let markets = adapter.perp_markets_for(dex).await?;
    let market = find_market(&markets, request.coin)?.clone();
    if market.delisted {
        return Err(AdjustError::Refused("目标市场已退市，拒绝调整杠杆".into()));
    }
    if request.leverage == 0 || u64::from(request.leverage) > market.max_leverage {
        return Err(AdjustError::Refused(format!(
            "杠杆 {} 超出该市场允许范围 1..={}",
            request.leverage, market.max_leverage
        )));
    }
    if request.margin_mode == "cross" && !crate::preflight::cross_available(&market) {
        return Err(AdjustError::Refused("该市场只允许逐仓模式".into()));
    }
    Ok(market)
}

fn action_for(report: &LeverageReport) -> Action {
    Action::UpdateLeverage(UpdateLeverage {
        asset: report.asset,
        is_cross: report.margin_mode == "cross",
        leverage: report.leverage,
    })
}

fn positions(
    adapter_account: &crate::hyperliquid::AccountView,
    request: &LeverageRequest<'_>,
) -> [Decimal; 2] {
    [
        signed_position(adapter_account, &request.pair.descriptor.coins[0]),
        signed_position(adapter_account, &request.pair.descriptor.coins[1]),
    ]
}

fn leverage_value(value: Decimal) -> Result<u32, AdjustError> {
    if value != value.trunc() || value < Decimal::ONE {
        return Err(crate::hyperliquid::AdapterError::BadResponse(
            "交易所当前杠杆不是正整数".into(),
        )
        .into());
    }
    value.normalize().to_string().parse().map_err(|_| {
        crate::hyperliquid::AdapterError::BadResponse("交易所当前杠杆超出支持范围".into()).into()
    })
}

pub async fn prepare(
    adapter: &Adapter,
    request: &LeverageRequest<'_>,
    expected_signer: Address,
    request_id: Option<&str>,
) -> Result<LeverageReport, AdjustError> {
    validate_request(request, expected_signer)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let store = Store::new(request);
    let _guard = store.acquire()?;
    let market = market_for(adapter, request).await?;
    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let account = adapter.account_for(request.account, dex).await?;
    let current = adapter.leverage_for(request.account, request.coin).await?;
    let current_leverage = leverage_value(current.value)?;
    if current.leverage_type != "cross" && current.leverage_type != "isolated" {
        return Err(crate::hyperliquid::AdapterError::BadResponse(
            "交易所当前保证金模式无法识别".into(),
        )
        .into());
    }
    let action_id = format!("leverage-{}", crate::hyperliquid::cloid_hex(new_cloid()?));
    let nonce = adapter.next_nonce()?;
    let mut report = LeverageReport {
        schema_version: 1,
        status: "prepared".into(),
        action_id,
        request_id: request_id.map(str::to_owned),
        pair_id: request.pair.descriptor.id.clone(),
        network: request.pair.descriptor.network.label().into(),
        config_revision: request.config_revision.into(),
        account_address: format!("{}", request.account).to_lowercase(),
        coin: request.coin.into(),
        asset: market.index,
        current_leverage,
        current_margin_mode: current.leverage_type,
        leverage: request.leverage,
        margin_mode: request.margin_mode.into(),
        max_leverage: market.max_leverage,
        start_positions: positions(&account, request),
        expected_signer: format!("{expected_signer}").to_lowercase(),
        nonce,
        typed_data: None,
        result: None,
        created_at: now_utc(),
    };
    report.typed_data = Some(typed_data_for(
        &action_for(&report),
        nonce,
        chain_for(request),
    )?);
    store.save(&report)?;
    Ok(report)
}

pub async fn submit(
    adapter: &Adapter,
    request: &LeverageRequest<'_>,
    expected_signer: Address,
    action_id: &str,
    signature: Signature,
) -> Result<LeverageReport, AdjustError> {
    validate_request(request, expected_signer)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let store = Store::new(request);
    let _guard = store.acquire()?;
    let mut report = store.load(action_id)?;
    if report.pair_id != request.pair.descriptor.id
        || report.account_address != format!("{}", request.account).to_lowercase()
        || report.config_revision != request.config_revision
        || report.coin != request.coin
        || report.leverage != request.leverage
        || report.margin_mode != request.margin_mode
        || report.expected_signer != format!("{expected_signer}").to_lowercase()
    {
        return Err(AdjustError::Refused(
            "杠杆调整身份已变化，请重新确认".into(),
        ));
    }
    if report.status == "succeeded" {
        return Ok(report);
    }
    if report.status != "prepared" {
        return Err(AdjustError::Refused(
            "杠杆调整已提交或结果未知，禁止直接重试".into(),
        ));
    }
    let signed = ActionRequest {
        action: action_for(&report),
        nonce: report.nonce,
        signature: normalize_signature(signature)?,
        vault_address: None,
        expires_after: None,
    };
    let recovered = signed
        .recover(chain_for(request))
        .map_err(|error| AdjustError::Refused(format!("签名无法验证: {error}")))?;
    if recovered != expected_signer {
        return Err(AdjustError::Refused("签名地址不是所选 API Wallet".into()));
    }
    let market = market_for(adapter, request).await?;
    if market.index != report.asset {
        return Err(AdjustError::Refused(
            "市场资产 ID 已变化，请重新确认".into(),
        ));
    }
    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let account = adapter.account_for(request.account, dex).await?;
    if positions(&account, request) != report.start_positions {
        return Err(AdjustError::Refused(
            "仓位在签名前后发生变化，请重新确认".into(),
        ));
    }
    let current = adapter.leverage_for(request.account, request.coin).await?;
    if leverage_value(current.value)? != report.current_leverage
        || current.leverage_type != report.current_margin_mode
    {
        return Err(AdjustError::Refused(
            "杠杆在签名前后已变化，请重新确认".into(),
        ));
    }
    report.status = "executing".into();
    report.typed_data = None;
    report.result = None;
    store.save(&report)?;
    match adapter.update_signed_leverage(signed).await {
        Ok(Some(message)) => {
            report.status = "rejected".into();
            report.result = Some(message);
            store.save(&report)?;
            return Ok(report);
        }
        Err(error) => {
            report.status = "unknown".into();
            report.result = Some(error.to_string());
            store.save(&report)?;
            return Ok(report);
        }
        Ok(None) => {}
    }
    let verification: Result<_, AdjustError> = async {
        let actual = adapter.leverage_for(request.account, request.coin).await?;
        let account = adapter.account_for(request.account, dex).await?;
        Ok((actual, positions(&account, request)))
    }
    .await;
    let (actual, actual_positions) = match verification {
        Ok(value) => value,
        Err(error) => {
            report.status = "unknown".into();
            report.result = Some(format!("杠杆调整后无法核对交易所状态: {error}"));
            store.save(&report)?;
            return Ok(report);
        }
    };
    if leverage_value(actual.value)? != request.leverage
        || (actual.leverage_type == "cross") != (request.margin_mode == "cross")
        || actual_positions != report.start_positions
    {
        report.status = "unknown".into();
        report.result = Some("交易所返回成功，但杠杆或仓位核对不一致，请人工核对".into());
    } else {
        report.status = "succeeded".into();
        report.result = Some(format!(
            "已设置 {} {}{}x",
            request.coin, request.margin_mode, request.leverage
        ));
    }
    store.save(&report)?;
    Ok(report)
}

pub async fn execute_managed(
    adapter: &Adapter,
    signer: &PrivateKeySigner,
    request: &LeverageRequest<'_>,
    request_id: &str,
) -> Result<LeverageReport, AdjustError> {
    let prepared = prepare(adapter, request, signer.address(), Some(request_id)).await?;
    let signed = action_for(&prepared)
        .sign_sync(signer, prepared.nonce, None, None, chain_for(request))
        .map_err(|error| AdjustError::State(format!("托管杠杆签名失败: {error}")))?;
    submit(
        adapter,
        request,
        signer.address(),
        &prepared.action_id,
        BrowserSignature {
            leg_index: 0,
            signature: signed.signature,
        }
        .signature,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use httpmock::MockServer;
    use std::str::FromStr;
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

    static DIR_SEQ: AtomicU64 = AtomicU64::new(0);
    static WALLET_SEQ: AtomicU64 = AtomicU64::new(100);
    const USER: &str = "0x7f2120f612b78d579197762196b8a202ab654096";

    struct Fixture {
        config: config::DashboardConfig,
        pair: DashboardPair,
        path: PathBuf,
        state_dir: PathBuf,
    }

    impl Fixture {
        fn request(&self, leverage: u32) -> LeverageRequest<'_> {
            LeverageRequest {
                pair: &self.pair,
                config_path: &self.path,
                config_revision: &self.config.revision,
                account: USER.parse().unwrap(),
                coin: "xyz:NBIS",
                leverage,
                margin_mode: "isolated",
                state_dir: &self.state_dir,
            }
        }
    }

    fn account() -> String {
        serde_json::json!({
            "marginSummary": {"accountValue": "10000", "totalNtlPos": "0", "totalRawUsd": "10000", "totalMarginUsed": "0"},
            "crossMarginSummary": {"accountValue": "10000", "totalNtlPos": "0", "totalRawUsd": "10000", "totalMarginUsed": "0"},
            "crossMaintenanceMarginUsed": "0", "withdrawable": "9000", "time": 1760000000000u64,
            "assetPositions": [
                {"type": "oneWay", "position": {"coin": "xyz:NBIS", "szi": "10", "leverage": {"type": "cross", "value": 3}, "entryPx": "10", "positionValue": "100", "unrealizedPnl": "0", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "33", "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}},
                {"type": "oneWay", "position": {"coin": "xyz:CRWV", "szi": "-2", "leverage": {"type": "cross", "value": 3}, "entryPx": "50", "positionValue": "100", "unrealizedPnl": "0", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": "33", "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
            ]
        }).to_string()
    }

    async fn setup() -> (MockServer, Fixture) {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"perpDexs""#);
            then.status(200).body(r#"[null,{"name":"xyz"}]"#);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info").body_includes(r#""type":"spotMeta""#);
            then.status(200).body(r#"{"universe":[],"tokens":[{"name":"USDC","index":0,"tokenId":"0x00000000000000000000000000000000","szDecimals":8,"weiDecimals":8,"evmContract":null}]}"#);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info").body_includes(r#""type":"meta""#).body_includes(r#""dex":"xyz""#);
            then.status(200).body(r#"{"universe":[{"szDecimals":2,"name":"xyz:NBIS","maxLeverage":5,"onlyIsolated":false},{"szDecimals":2,"name":"xyz:CRWV","maxLeverage":5,"onlyIsolated":false}],"collateralToken":0}"#);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.status(200).body(account());
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders");
            then.status(200).body("[]");
        });
        let dir = std::env::temp_dir().join(format!(
            "hltrader-leverage-{}-{}",
            std::process::id(),
            DIR_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("pairs.json");
        fs::write(&path, format!(r#"{{"config_version":1,"pairs":[{{"id":"nbis-crwv","label":"NBIS / CRWV","enabled":true,"network":"mainnet","dex":"xyz","coins":["xyz:NBIS","xyz:CRWV"],"account_address":"{USER}","unlock_anchors":null}}]}}"#)).unwrap();
        let config = config::load(&path).unwrap();
        let pair = config.enabled_pair("nbis-crwv").unwrap().clone();
        (
            server,
            Fixture {
                config,
                pair,
                path,
                state_dir: dir.join("state"),
            },
        )
    }

    fn wallet() -> PrivateKeySigner {
        PrivateKeySigner::from_str(&format!(
            "{:064x}",
            WALLET_SEQ.fetch_add(1, Ordering::Relaxed)
        ))
        .unwrap()
    }

    fn sign(report: &LeverageReport, signer: &PrivateKeySigner) -> Signature {
        action_for(report)
            .sign_sync(signer, report.nonce, None, None, Chain::Mainnet)
            .unwrap()
            .signature
    }

    #[tokio::test]
    async fn browser_leverage_roundtrip_verifies_and_does_not_replay() {
        let (server, fixture) = setup().await;
        let active_calls = AtomicUsize::new(0);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(move |req| {
                let request: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                let call = active_calls.fetch_add(1, Ordering::SeqCst);
                let (value, mode) = if call < 2 {
                    (3, "cross")
                } else {
                    (2, "isolated")
                };
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(
                        serde_json::json!({
                            "user": request["user"], "coin": request["coin"],
                            "leverage": {"type": mode, "value": value}
                        })
                        .to_string(),
                    )
                    .build()
            });
        });
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes("updateLeverage")
                .body_includes(r#""leverage":2"#)
                .body_includes(r#""isCross":false"#);
            then.status(200)
                .body(r#"{"status":"ok","response":{"type":"default","data":null}}"#);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let prepared = prepare(&adapter, &fixture.request(2), signer.address(), None)
            .await
            .unwrap();
        assert_eq!(prepared.current_leverage, 3);
        assert_eq!(prepared.current_margin_mode, "cross");
        let report = submit(
            &adapter,
            &fixture.request(2),
            signer.address(),
            &prepared.action_id,
            sign(&prepared, &signer),
        )
        .await
        .unwrap();
        assert_eq!(report.status, "succeeded");
        assert_eq!(exchange.calls(), 1);
        let replay = submit(
            &adapter,
            &fixture.request(2),
            signer.address(),
            &prepared.action_id,
            sign(&prepared, &signer),
        )
        .await
        .unwrap();
        assert_eq!(replay.status, "succeeded");
        assert_eq!(exchange.calls(), 1);
    }

    #[tokio::test]
    async fn wrong_wallet_and_over_limit_are_rejected_without_exchange() {
        let (server, fixture) = setup().await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(|req| {
                let request: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(
                        serde_json::json!({
                            "user": request["user"], "coin": request["coin"],
                            "leverage": {"type": "cross", "value": 3}
                        })
                        .to_string(),
                    )
                    .build()
            });
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let error = prepare(&adapter, &fixture.request(4), signer.address(), None)
            .await
            .unwrap_err();
        assert!(matches!(error, AdjustError::Refused(_)));
        let prepared = prepare(&adapter, &fixture.request(2), signer.address(), None)
            .await
            .unwrap();
        let wrong = wallet();
        let error = submit(
            &adapter,
            &fixture.request(2),
            signer.address(),
            &prepared.action_id,
            sign(&prepared, &wrong),
        )
        .await
        .unwrap_err();
        assert!(matches!(error, AdjustError::Refused(_)));
        assert_eq!(exchange.calls(), 0);
    }

    #[tokio::test]
    async fn leverage_change_between_signing_and_submit_is_rejected() {
        let (server, fixture) = setup().await;
        let active_calls = AtomicUsize::new(0);
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(move |req| {
                let request: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                let value = 3 + active_calls.fetch_add(1, Ordering::SeqCst);
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(
                        serde_json::json!({
                            "user": request["user"], "coin": request["coin"],
                            "leverage": {"type": "cross", "value": value}
                        })
                        .to_string(),
                    )
                    .build()
            });
        });
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let prepared = prepare(&adapter, &fixture.request(2), signer.address(), None)
            .await
            .unwrap();
        let error = submit(
            &adapter,
            &fixture.request(2),
            signer.address(),
            &prepared.action_id,
            sign(&prepared, &signer),
        )
        .await
        .unwrap_err();
        assert!(matches!(error, AdjustError::Refused(_)));
        assert_eq!(exchange.calls(), 0);
    }

    #[tokio::test]
    async fn exchange_rejection_is_saved_as_known_rejection() {
        let (server, fixture) = setup().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("activeAssetData");
            then.respond_with(|req| {
                let request: serde_json::Value = serde_json::from_str(&req.body_string()).unwrap();
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(
                        serde_json::json!({
                            "user": request["user"], "coin": request["coin"],
                            "leverage": {"type": "cross", "value": 3}
                        })
                        .to_string(),
                    )
                    .build()
            });
        });
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200)
                .body(r#"{"status":"err","response":"position would exceed leverage limit"}"#);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let prepared = prepare(&adapter, &fixture.request(2), signer.address(), None)
            .await
            .unwrap();
        let report = submit(
            &adapter,
            &fixture.request(2),
            signer.address(),
            &prepared.action_id,
            sign(&prepared, &signer),
        )
        .await
        .unwrap();
        assert_eq!(report.status, "rejected");
        assert!(report.result.unwrap().contains("leverage limit"));
        assert_eq!(exchange.calls(), 1);
    }
}
