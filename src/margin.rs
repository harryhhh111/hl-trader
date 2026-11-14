use std::fs::{self, OpenOptions};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use hypersdk::Address;
use hypersdk::hypercore::api::{Action, ActionRequest, UpdateIsolatedMargin};
use hypersdk::hypercore::types::{LeverageType, PositionData};
use hypersdk::hypercore::{Chain, PrivateKeySigner, Signature};
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use serde::{Deserialize, Serialize};

use crate::adjust::{
    AdjustError, BrowserSignature, new_cloid, normalize_signature, now_utc, typed_data_for,
};
use crate::dashboard::config::{self, DashboardPair};
use crate::hyperliquid::{Adapter, find_market};
use crate::write_lock::WriteLock;

const TARGET_RATIO: Decimal = Decimal::from_parts(8, 0, 0, false, 1);
const MICRO_USD: Decimal = Decimal::from_parts(1_000_000, 0, 0, false, 0);

pub struct MarginRequest<'a> {
    pub pair: &'a DashboardPair,
    pub config_path: &'a Path,
    pub config_revision: &'a str,
    pub account: Address,
    pub coin: &'a str,
    pub state_dir: &'a Path,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarginReport {
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
    pub position_size: Decimal,
    pub position_notional: Decimal,
    pub current_margin: Decimal,
    pub margin_delta: Decimal,
    pub target_margin: Decimal,
    pub target_ratio_pct: Decimal,
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
    fn new(request: &MarginRequest<'_>) -> Self {
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
        self.dir.join(format!("margin-{action_id}.json"))
    }

    fn save(&self, report: &MarginReport) -> Result<(), AdjustError> {
        fs::create_dir_all(&self.dir).map_err(|e| AdjustError::State(e.to_string()))?;
        let json =
            serde_json::to_string_pretty(report).map_err(|e| AdjustError::State(e.to_string()))?;
        let temporary = self.dir.join(format!("margin.tmp.{}", std::process::id()));
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

    fn load(&self, action_id: &str) -> Result<MarginReport, AdjustError> {
        let text = fs::read_to_string(self.path(action_id))
            .map_err(|_| AdjustError::Refused("找不到待签名保证金调整，请重新确认".into()))?;
        serde_json::from_str(&text).map_err(|e| AdjustError::State(format!("保证金回执损坏: {e}")))
    }

    fn check_unresolved(request: &MarginRequest<'_>) -> Result<(), AdjustError> {
        let fail = || AdjustError::Refused("无法核对历史保证金回执，禁止追加，请先人工核对".into());
        let account = format!("{}", request.account).to_lowercase();
        for pair in fs::read_dir(request.state_dir).map_err(|_| fail())? {
            let pair = pair.map_err(|_| fail())?;
            if !pair.file_type().map_err(|_| fail())?.is_dir() {
                continue;
            }
            let entries = match fs::read_dir(pair.path().join(&account)) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(_) => return Err(fail()),
            };
            for entry in entries {
                let entry = entry.map_err(|_| fail())?;
                let name = entry.file_name();
                let name = name.to_string_lossy();
                if !name.starts_with("margin-") || !name.ends_with(".json") {
                    continue;
                }
                let prior: MarginReport =
                    serde_json::from_slice(&fs::read(entry.path()).map_err(|_| fail())?)
                        .map_err(|_| fail())?;
                if prior.network == request.pair.descriptor.network.label()
                    && prior.account_address == account
                    && prior.coin == request.coin
                    && !matches!(prior.status.as_str(), "prepared" | "succeeded" | "rejected")
                {
                    return Err(AdjustError::Refused(format!(
                        "保证金操作 {} 已提交或结果未知，禁止新建或发送追加动作，请先人工核对",
                        prior.action_id
                    )));
                }
            }
        }
        Ok(())
    }
}

fn account_guard(request: &MarginRequest<'_>) -> Result<WriteLock, AdjustError> {
    let dir = request.state_dir.join("margin-locks");
    fs::create_dir_all(&dir).map_err(|e| AdjustError::State(e.to_string()))?;
    let mut permissions = fs::metadata(&dir)
        .map_err(|e| AdjustError::State(e.to_string()))?
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&dir, permissions).map_err(|e| AdjustError::State(e.to_string()))?;
    WriteLock::at(&dir.join(format!(
        "{}-{}.lock",
        request.pair.descriptor.network.label(),
        request.account
    )))
    .map_err(AdjustError::State)
}

fn chain_for(request: &MarginRequest<'_>) -> Chain {
    match request.pair.descriptor.network {
        config::Network::Mainnet => Chain::Mainnet,
        config::Network::Testnet => Chain::Testnet,
    }
}

fn ensure_config(request: &MarginRequest<'_>) -> Result<(), AdjustError> {
    let current =
        config::revision_at(request.config_path).map_err(|e| AdjustError::State(e.to_string()))?;
    if current != request.config_revision {
        return Err(AdjustError::Refused(
            "pair 配置已变化，请重新确认保证金调整".into(),
        ));
    }
    Ok(())
}

fn validate_request(
    request: &MarginRequest<'_>,
    expected_signer: Address,
) -> Result<(), AdjustError> {
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
    ensure_config(request)
}

async fn market_for(
    adapter: &Adapter,
    request: &MarginRequest<'_>,
) -> Result<hypersdk::hypercore::PerpMarket, AdjustError> {
    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let markets = adapter.perp_markets_for(dex).await?;
    let market = find_market(&markets, request.coin)?.clone();
    if market.delisted {
        return Err(AdjustError::Refused(
            "目标市场已退市，拒绝调整保证金".into(),
        ));
    }
    Ok(market)
}

async fn isolated_position(
    adapter: &Adapter,
    request: &MarginRequest<'_>,
) -> Result<PositionData, AdjustError> {
    let dex =
        (!request.pair.descriptor.dex.is_empty()).then_some(request.pair.descriptor.dex.as_str());
    let position = adapter
        .position_for(request.account, request.coin, dex)
        .await?
        .ok_or_else(|| AdjustError::Refused("目标仓位不存在，无需追加逐仓保证金".into()))?;
    if position.szi == Decimal::ZERO {
        return Err(AdjustError::Refused("目标仓位为零，拒绝追加保证金".into()));
    }
    if position.leverage.leverage_type != LeverageType::Isolated {
        return Err(AdjustError::Refused(
            "仅 isolated 仓位支持追加逐仓保证金".into(),
        ));
    }
    Ok(position)
}

struct MarginTarget {
    size: Decimal,
    notional: Decimal,
    current: Decimal,
    delta: Decimal,
    target: Decimal,
}

fn target_for(position: &PositionData) -> Result<MarginTarget, AdjustError> {
    let notional = position.position_value.abs();
    let current = position.margin_used;
    if notional <= Decimal::ZERO || current < Decimal::ZERO {
        return Err(crate::hyperliquid::AdapterError::BadResponse(
            "逐仓仓位名义或保证金数值非法".into(),
        )
        .into());
    }
    let required = notional * TARGET_RATIO - current;
    if required <= Decimal::ZERO {
        return Err(AdjustError::Refused(
            "当前逐仓保证金已不低于名义价值的 80%，无需追加".into(),
        ));
    }
    let ntli = (required * MICRO_USD)
        .ceil()
        .to_i64()
        .ok_or_else(|| AdjustError::Refused("追加保证金金额超出支持范围".into()))?;
    if ntli <= 0 {
        return Err(AdjustError::Refused("追加保证金金额必须为正数".into()));
    }
    let delta = Decimal::new(ntli, 6);
    Ok(MarginTarget {
        size: position.szi,
        notional,
        current,
        delta,
        target: current + delta,
    })
}

fn action_for(report: &MarginReport) -> Result<Action, AdjustError> {
    let ntli = (report.margin_delta * MICRO_USD)
        .to_i64()
        .ok_or_else(|| AdjustError::State("保证金回执金额超出支持范围".into()))?;
    Ok(Action::UpdateIsolatedMargin(UpdateIsolatedMargin {
        asset: report.asset,
        is_buy: report.position_size > Decimal::ZERO,
        ntli,
    }))
}

pub async fn prepare(
    adapter: &Adapter,
    request: &MarginRequest<'_>,
    expected_signer: Address,
    request_id: Option<&str>,
) -> Result<MarginReport, AdjustError> {
    validate_request(request, expected_signer)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let _account_guard = account_guard(request)?;
    let store = Store::new(request);
    let _guard = store.acquire()?;
    Store::check_unresolved(request)?;
    let market = market_for(adapter, request).await?;
    let position = isolated_position(adapter, request).await?;
    let target = target_for(&position)?;
    let action_id = format!("margin-{}", crate::hyperliquid::cloid_hex(new_cloid()?));
    let nonce = adapter.next_nonce()?;
    let mut report = MarginReport {
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
        position_size: target.size,
        position_notional: target.notional,
        current_margin: target.current,
        margin_delta: target.delta,
        target_margin: target.target,
        target_ratio_pct: Decimal::new(80, 0),
        expected_signer: format!("{expected_signer}").to_lowercase(),
        nonce,
        typed_data: None,
        result: None,
        created_at: now_utc(),
    };
    report.typed_data = Some(typed_data_for(
        &action_for(&report)?,
        nonce,
        chain_for(request),
    )?);
    store.save(&report)?;
    Ok(report)
}

pub async fn submit(
    adapter: &Adapter,
    request: &MarginRequest<'_>,
    expected_signer: Address,
    action_id: &str,
    signature: Signature,
) -> Result<MarginReport, AdjustError> {
    validate_request(request, expected_signer)?;
    let _agent_guard = WriteLock::agent(expected_signer).map_err(AdjustError::State)?;
    let _account_guard = account_guard(request)?;
    let store = Store::new(request);
    let _guard = store.acquire()?;
    let mut report = store.load(action_id)?;
    if report.pair_id != request.pair.descriptor.id
        || report.account_address != format!("{}", request.account).to_lowercase()
        || report.config_revision != request.config_revision
        || report.coin != request.coin
        || report.expected_signer != format!("{expected_signer}").to_lowercase()
    {
        return Err(AdjustError::Refused(
            "保证金调整身份已变化，请重新确认".into(),
        ));
    }
    if report.status == "succeeded" {
        return Ok(report);
    }
    if report.status != "prepared" {
        return Err(AdjustError::Refused(
            "保证金调整已提交或结果未知，禁止直接重试".into(),
        ));
    }
    Store::check_unresolved(request)?;
    let signed = ActionRequest {
        action: action_for(&report)?,
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
    let current = isolated_position(adapter, request).await?;
    if current.szi != report.position_size
        || current.position_value.abs() != report.position_notional
        || current.margin_used != report.current_margin
    {
        return Err(AdjustError::Refused(
            "仓位、名义价值或保证金在签名前后发生变化，请重新确认".into(),
        ));
    }
    report.status = "executing".into();
    report.typed_data = None;
    report.result = None;
    store.save(&report)?;
    match adapter.submit_signed_action(signed, "逐仓保证金调整").await {
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
    let actual = match isolated_position(adapter, request).await {
        Ok(position) => position,
        Err(error) => {
            report.status = "unknown".into();
            report.result = Some(format!("追加保证金后无法核对交易所状态: {error}"));
            store.save(&report)?;
            return Ok(report);
        }
    };
    if actual.szi != report.position_size || actual.margin_used != report.target_margin {
        report.status = "unknown".into();
        report.result = Some("交易所返回成功，但仓位或保证金核对不一致，请人工核对".into());
    } else if actual.margin_used < actual.position_value.abs() * TARGET_RATIO {
        report.status = "unknown".into();
        report.result = Some("追加已确认，但最新名义价值变化后保证金不足 80%，请重新核对".into());
    } else {
        report.status = "succeeded".into();
        report.result = Some(format!(
            "已追加 {} {} USD，目标保证金 {} USD",
            request.coin, report.margin_delta, report.target_margin
        ));
    }
    store.save(&report)?;
    Ok(report)
}

pub async fn execute_managed(
    adapter: &Adapter,
    signer: &PrivateKeySigner,
    request: &MarginRequest<'_>,
    request_id: &str,
) -> Result<MarginReport, AdjustError> {
    let prepared = prepare(adapter, request, signer.address(), Some(request_id)).await?;
    let signed = action_for(&prepared)?
        .sign_sync(signer, prepared.nonce, None, None, chain_for(request))
        .map_err(|error| AdjustError::State(format!("托管保证金签名失败: {error}")))?;
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
    static WALLET_SEQ: AtomicU64 = AtomicU64::new(1_000);
    const USER: &str = "0x7f2120f612b78d579197762196b8a202ab654096";

    struct Fixture {
        config: config::DashboardConfig,
        pair: DashboardPair,
        path: PathBuf,
        state_dir: PathBuf,
    }

    impl Fixture {
        fn request(&self) -> MarginRequest<'_> {
            MarginRequest {
                pair: &self.pair,
                config_path: &self.path,
                config_revision: &self.config.revision,
                account: USER.parse().unwrap(),
                coin: "xyz:NBIS",
                state_dir: &self.state_dir,
            }
        }
    }

    fn account(mode: &str, margin: &str) -> String {
        serde_json::json!({
            "marginSummary": {"accountValue": "10000", "totalNtlPos": "100", "totalRawUsd": "10000", "totalMarginUsed": margin},
            "crossMarginSummary": {"accountValue": "10000", "totalNtlPos": "100", "totalRawUsd": "10000", "totalMarginUsed": "0"},
            "crossMaintenanceMarginUsed": "0", "withdrawable": "9000", "time": 1760000000000u64,
            "assetPositions": [
                {"type": "oneWay", "position": {"coin": "xyz:NBIS", "szi": "10", "leverage": {"type": mode, "value": 2}, "entryPx": "10", "positionValue": "100", "unrealizedPnl": "0", "returnOnEquity": "0", "liquidationPx": null, "marginUsed": margin, "maxLeverage": 5, "cumFunding": {"allTime": "0", "sinceOpen": "0", "sinceChange": "0"}}}
            ]
        }).to_string()
    }

    async fn setup(mode: &'static str, margin: &'static str) -> (MockServer, Fixture) {
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
            then.status(200).body(r#"{"universe":[{"szDecimals":2,"name":"xyz:NBIS","maxLeverage":5,"onlyIsolated":false}],"collateralToken":0}"#);
        });
        let reads = AtomicUsize::new(0);
        server.mock(move |when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState");
            then.respond_with(move |_| {
                let current = if reads.fetch_add(1, Ordering::SeqCst) < 2 {
                    margin
                } else {
                    "80"
                };
                httpmock::HttpMockResponse::builder()
                    .status(200)
                    .body(account(mode, current))
                    .build()
            });
        });
        let dir = std::env::temp_dir().join(format!(
            "hltrader-margin-{}-{}",
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

    fn sign(report: &MarginReport, signer: &PrivateKeySigner) -> Signature {
        action_for(report)
            .unwrap()
            .sign_sync(signer, report.nonce, None, None, Chain::Mainnet)
            .unwrap()
            .signature
    }

    #[tokio::test]
    async fn isolated_margin_roundtrip_adds_to_eighty_percent_and_blocks_replay() {
        let (server, fixture) = setup("isolated", "50").await;
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/exchange")
                .body_includes("updateIsolatedMargin")
                .body_includes(r#""ntli":30000000"#)
                .body_includes(r#""isBuy":true"#);
            then.status(200)
                .body(r#"{"status":"ok","response":{"type":"default","data":null}}"#);
        });
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let prepared = prepare(&adapter, &fixture.request(), signer.address(), None)
            .await
            .unwrap();
        assert_eq!(prepared.margin_delta, Decimal::new(30, 0));
        assert_eq!(prepared.target_margin, Decimal::new(80, 0));
        assert!(prepared.typed_data.is_some());
        let report = submit(
            &adapter,
            &fixture.request(),
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
            &fixture.request(),
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
    async fn cross_and_sufficient_margin_are_rejected_without_exchange() {
        for (mode, margin) in [("cross", "50"), ("isolated", "80")] {
            let (server, fixture) = setup(mode, margin).await;
            let exchange = server.mock(|when, then| {
                when.method(httpmock::Method::POST).path("/exchange");
                then.status(500);
            });
            let adapter = Adapter::with_base_url(&server.base_url());
            let signer = wallet();
            let error = prepare(&adapter, &fixture.request(), signer.address(), None)
                .await
                .unwrap_err();
            assert!(matches!(error, AdjustError::Refused(_)));
            assert_eq!(exchange.calls(), 0);
        }
    }

    #[tokio::test]
    async fn unresolved_receipt_blocks_new_browser_and_managed_actions() {
        for status in ["unknown", "executing"] {
            let (server, fixture) = setup("isolated", "50").await;
            let adapter = Adapter::with_base_url(&server.base_url());
            let signer = wallet();
            let mut prior = prepare(&adapter, &fixture.request(), signer.address(), None)
                .await
                .unwrap();
            prior.status = status.into();
            Store::new(&fixture.request()).save(&prior).unwrap();
            let error = prepare(&adapter, &fixture.request(), wallet().address(), None)
                .await
                .unwrap_err();
            assert!(error.to_string().contains(&prior.action_id));
            assert!(
                execute_managed(&adapter, &signer, &fixture.request(), "new-request")
                    .await
                    .is_err()
            );
        }
    }

    #[tokio::test]
    async fn an_older_prepared_signature_cannot_bypass_an_unresolved_action() {
        let (server, fixture) = setup("isolated", "50").await;
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let mut first = prepare(&adapter, &fixture.request(), signer.address(), None)
            .await
            .unwrap();
        let second = prepare(&adapter, &fixture.request(), signer.address(), None)
            .await
            .unwrap();
        first.status = "unknown".into();
        Store::new(&fixture.request()).save(&first).unwrap();
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(500);
        });
        let error = submit(
            &adapter,
            &fixture.request(),
            signer.address(),
            &second.action_id,
            sign(&second, &signer),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains(&first.action_id));
        assert_eq!(exchange.calls(), 0);
    }

    #[tokio::test]
    async fn switching_pair_cannot_bypass_an_unresolved_same_contract() {
        let (server, mut fixture) = setup("isolated", "50").await;
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let mut prior = prepare(&adapter, &fixture.request(), signer.address(), None)
            .await
            .unwrap();
        prior.status = "unknown".into();
        Store::new(&fixture.request()).save(&prior).unwrap();
        let mut config: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&fixture.path).unwrap()).unwrap();
        let mut other = config["pairs"][0].clone();
        other["id"] = "nbis-other".into();
        config["pairs"].as_array_mut().unwrap().push(other);
        fs::write(&fixture.path, config.to_string()).unwrap();
        fixture.config = config::load(&fixture.path).unwrap();
        let other_pair = fixture.config.enabled_pair("nbis-other").unwrap();
        let request = MarginRequest {
            pair: other_pair,
            ..fixture.request()
        };
        let error = prepare(&adapter, &request, wallet().address(), None)
            .await
            .unwrap_err();
        assert!(error.to_string().contains(&prior.action_id));
    }

    #[tokio::test]
    async fn corrupt_history_fails_closed_and_known_rejection_can_be_retried() {
        let (server, fixture) = setup("isolated", "50").await;
        let adapter = Adapter::with_base_url(&server.base_url());
        let signer = wallet();
        let mut prior = prepare(&adapter, &fixture.request(), signer.address(), None)
            .await
            .unwrap();
        let store = Store::new(&fixture.request());
        fs::write(store.path(&prior.action_id), "broken receipt").unwrap();
        assert!(
            prepare(&adapter, &fixture.request(), signer.address(), None)
                .await
                .unwrap_err()
                .to_string()
                .contains("无法核对")
        );
        prior.status = "rejected".into();
        store.save(&prior).unwrap();
        assert!(
            prepare(&adapter, &fixture.request(), signer.address(), None)
                .await
                .is_ok()
        );
    }

    #[tokio::test]
    async fn margin_writers_for_shared_account_use_one_lock_across_pairs() {
        let (_server, fixture) = setup("isolated", "50").await;
        let mut other_pair = fixture.pair.clone();
        other_pair.descriptor.id = "other-pair".into();
        let other = MarginRequest {
            pair: &other_pair,
            ..fixture.request()
        };
        let held = account_guard(&fixture.request()).unwrap();
        assert!(account_guard(&other).is_err());
        drop(held);
        assert!(account_guard(&other).is_ok());
    }

    #[test]
    fn fractional_micro_usd_is_rounded_up() {
        let mut state: hypersdk::hypercore::types::ClearinghouseState =
            serde_json::from_str(&account("isolated", "80")).unwrap();
        let mut position = state.asset_positions.remove(0).position;
        position.position_value = "100.0000001".parse().unwrap();
        let target = target_for(&position).unwrap();
        assert_eq!(target.delta, Decimal::new(1, 6));
        assert!(target.target >= position.position_value * TARGET_RATIO);
    }
}
