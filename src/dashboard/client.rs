//! v2 看板只读 info 客户端（多 pair 方案 §5.1）。
//!
//! - 固定官方 URL（mainnet/testnet 映射），无任意 URL 配置；只允许 `/info` 类请求，
//!   不暴露 /exchange 或任何签名/写方法。
//! - 优先复用 hypersdk 0.3.0 只读方法；SDK 缺项（abstraction 模式：封闭枚举会丢弃
//!   "default"/"dexAbstraction" 等原始值，且请求不带 dex）用固定 /info DTO 兜底，
//!   原始字符串原样返回。
//! - 超时由调用侧用 tokio::time::timeout 包裹；错误统一为通用中文描述。

use std::time::Duration;

use hypersdk::Address;
use hypersdk::hypercore::types::{
    BasicOrder, CandleInterval, ClearinghouseState, UserFundingEntry,
};
use hypersdk::hypercore::{Chain, HttpClient};
use rust_decimal::Decimal;
use serde::Serialize;

use super::config::Network;

pub const MAINNET_API_URL: &str = "https://api.hyperliquid.xyz";
pub const TESTNET_API_URL: &str = "https://api.hyperliquid-testnet.xyz";

const DAY_MS: u64 = 86_400_000;

fn fixed_url(network: Network) -> &'static str {
    match network {
        Network::Mainnet => MAINNET_API_URL,
        Network::Testnet => TESTNET_API_URL,
    }
}

fn build_http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(10))
        // 与交易适配器同策略：不跟随任何重定向。
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("构建 HTTP 客户端失败")
}

fn classify(e: &anyhow::Error) -> String {
    let msg = format!("{e:#}");
    let lower = msg.to_lowercase();
    if lower.contains("timed out") || lower.contains("timeout") || lower.contains("deadline") {
        "网络错误或超时".to_string()
    } else if lower.contains("body=")
        || lower.contains("error decoding")
        || lower.contains("expected")
    {
        "响应格式无法识别".to_string()
    } else if msg.starts_with("HTTP 401") || msg.starts_with("HTTP 403") {
        "无权限".to_string()
    } else if msg.starts_with("HTTP") {
        format!("HTTP 层错误 {}", &msg[..msg.len().min(40)])
    } else {
        "网络错误或超时".to_string()
    }
}

/// 账户抽象模式查询的固定 /info DTO（SDK 的 abstraction_mode 是封闭枚举，
/// 会丢掉未知值且不带 dex，不符合"未知值保留+诊断"的契约要求）。
/// 注意：官方端点 type 为 "userAbstraction" 且不接受 dex 字段（实测带 dex 返回 422）。
#[derive(Serialize)]
struct AbstractionRequest {
    #[serde(rename = "type")]
    request_type: &'static str,
    user: Address,
}

pub struct InfoClient {
    client: HttpClient,
    /// 仅用于 abstraction 固定 DTO；只 POST {base}/info。
    raw: reqwest::Client,
    base: &'static str,
}

impl InfoClient {
    /// 生产入口：固定官方 URL。
    pub fn new(network: Network) -> Self {
        let client = HttpClient::new(match network {
            Network::Mainnet => Chain::Mainnet,
            Network::Testnet => Chain::Testnet,
        })
        .with_http_client(build_http_client());
        Self {
            client,
            raw: build_http_client(),
            base: fixed_url(network),
        }
    }

    /// 测试入口：hypersdk 客户端与 raw 客户端都指向本地 mock。
    #[cfg(test)]
    pub fn with_base_url(base: &str, network: Network) -> Self {
        let chain = match network {
            Network::Mainnet => Chain::Mainnet,
            Network::Testnet => Chain::Testnet,
        };
        let client = HttpClient::new(chain)
            .with_url(base.parse().expect("mock url"))
            .with_http_client(build_http_client());
        Self {
            client,
            raw: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .expect("raw client"),
            base: Box::leak(base.to_string().into_boxed_str()),
        }
    }

    /// 测试入口：仅 raw 客户端（abstraction 固定 DTO）指向本地 mock。
    #[cfg(test)]
    pub fn with_raw_base(raw_base: &str, network: Network) -> Self {
        let chain = match network {
            Network::Mainnet => Chain::Mainnet,
            Network::Testnet => Chain::Testnet,
        };
        // hypersdk 客户端保留在固定官方 URL（本测试不触发它）。
        let client = HttpClient::new(chain).with_http_client(build_http_client());
        Self {
            client,
            raw: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .expect("raw client"),
            base: Box::leak(raw_base.to_string().into_boxed_str()),
        }
    }

    pub async fn meta_and_asset_ctxs(
        &self,
        dex: Option<&str>,
    ) -> Result<serde_json::Value, String> {
        self.client
            .meta_and_asset_ctxs(dex.map(str::to_string))
            .await
            .map_err(|e| classify(&e))
    }

    pub async fn clearinghouse_state(
        &self,
        user: Address,
        dex: Option<&str>,
    ) -> Result<ClearinghouseState, String> {
        self.client
            .clearinghouse_state(user, dex.map(str::to_string))
            .await
            .map_err(|e| classify(&e))
    }

    pub async fn open_orders(
        &self,
        user: Address,
        dex: Option<&str>,
    ) -> Result<Vec<BasicOrder>, String> {
        self.client
            .open_orders(user, dex.map(str::to_string))
            .await
            .map_err(|e| classify(&e))
    }

    /// userFunding 官方 API 未声明 dex 参数：不得凭空加入（契约 §6.2）。
    pub async fn user_funding_page(
        &self,
        user: Address,
        start_ms: u64,
        end_ms: u64,
    ) -> Result<Vec<UserFundingEntry>, String> {
        self.client
            .user_funding(user, start_ms, Some(end_ms))
            .await
            .map_err(|e| classify(&e))
    }

    /// 最近 days 天的日线收盘价（蜡烛收盘毫秒 + 收盘价）。
    pub async fn daily_closes(&self, coin: &str, days: u32) -> Result<Vec<(u64, Decimal)>, String> {
        let end = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        let start = end.saturating_sub(u64::from(days) * DAY_MS);
        let candles = self
            .client
            .candle_snapshot(coin.to_string(), CandleInterval::OneDay, start, end)
            .await
            .map_err(|e| classify(&e))?;
        Ok(candles
            .into_iter()
            .map(|c| (c.close_time, c.close))
            .collect())
    }

    /// 账户抽象模式原始枚举字符串（unifiedAccount|portfolioMargin|disabled|default|
    /// dexAbstraction 或未来未知值都原样返回，由调用方决定是否诊断）。
    pub async fn abstraction_mode(
        &self,
        user: Address,
        dex: Option<&str>,
    ) -> Result<String, String> {
        // 官方 userAbstraction 端点是用户级查询，不接受 dex（实测 422）；忽略该参数。
        let _ = dex;
        let req = AbstractionRequest {
            request_type: "userAbstraction",
            user,
        };
        let response = self
            .raw
            .post(format!("{}/info", self.base))
            .json(&req)
            .send()
            .await
            .map_err(|_| "网络错误或超时".to_string())?;
        if !response.status().is_success() {
            return Err(format!("HTTP 层错误 {}", response.status().as_u16()));
        }
        // 官方响应为纯字符串，如 "unifiedAccount"；任何 JSON 字符串都原样保留。
        response
            .json::<String>()
            .await
            .map_err(|_| "响应格式无法识别".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use httpmock::MockServer;

    const USER: &str = "0x2222222222222222222222222222222222222222";

    fn user() -> Address {
        USER.parse().unwrap()
    }

    const CLEARINGHOUSE: &str = r#"{
        "marginSummary": {"accountValue": "1000.0", "totalNtlPos": "60.0", "totalRawUsd": "1000.0", "totalMarginUsed": "12.0"},
        "crossMarginSummary": {"accountValue": "1000.0", "totalNtlPos": "60.0", "totalRawUsd": "1000.0", "totalMarginUsed": "12.0"},
        "crossMaintenanceMarginUsed": "5.0", "withdrawable": "988.0", "time": 1760000000000,
        "assetPositions": []
    }"#;

    #[tokio::test]
    async fn dex_param_serialization_and_read_only_paths() {
        let server = MockServer::start_async().await;
        // /exchange 绝不应被调用。
        let exchange = server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/exchange");
            then.status(200).body("{}");
        });
        let meta = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("metaAndAssetCtxs")
                .body_includes(r#""dex":"xyz""#);
            then.status(200).body(r#"[{"universe": []}, []]"#);
        });
        let ch = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("clearinghouseState")
                .body_includes(r#""dex":"xyz""#);
            then.status(200).body(CLEARINGHOUSE);
        });
        let orders = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("frontendOpenOrders")
                .body_includes(r#""dex":"xyz""#);
            then.status(200).body("[]");
        });
        let funding_no_dex = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("userFunding")
                .body_excludes(r#""dex""#);
            then.status(200).body("[]");
        });

        let client = InfoClient::with_base_url(&server.base_url(), Network::Mainnet);
        client.meta_and_asset_ctxs(Some("xyz")).await.unwrap();
        client
            .clearinghouse_state(user(), Some("xyz"))
            .await
            .unwrap();
        client.open_orders(user(), Some("xyz")).await.unwrap();
        client.user_funding_page(user(), 0, 1).await.unwrap();

        assert_eq!(meta.calls(), 1);
        assert_eq!(ch.calls(), 1);
        assert_eq!(orders.calls(), 1);
        assert!(funding_no_dex.calls() >= 1, "userFunding 不得带 dex");
        assert_eq!(exchange.calls(), 0, "只允许 /info 请求");
    }

    #[tokio::test]
    async fn abstraction_mode_returns_raw_string() {
        let server = MockServer::start_async().await;
        let m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#)
                .body_includes(USER)
                .body_excludes(r#""dex""#);
            then.status(200).body(r#""dexAbstraction""#);
        });
        let client = InfoClient::with_raw_base(&server.base_url(), Network::Mainnet);
        let mode = client.abstraction_mode(user(), Some("xyz")).await.unwrap();
        assert_eq!(mode, "dexAbstraction", "未知枚举原样保留");
        assert_eq!(m.calls(), 1);

        let user2: Address = "0x3333333333333333333333333333333333333333"
            .parse()
            .unwrap();
        let no_dex = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes(r#""type":"userAbstraction""#)
                .body_includes("0x3333333333333333333333333333333333333333")
                .body_excludes(r#""dex""#);
            then.status(200).body(r#""default""#);
        });
        let mode = client.abstraction_mode(user2, None).await.unwrap();
        assert_eq!(mode, "default");
        assert_eq!(no_dex.calls(), 1);
    }

    #[tokio::test]
    async fn abstraction_mode_bad_response_is_error() {
        let server = MockServer::start_async().await;
        server.mock(|when, then| {
            when.method(httpmock::Method::POST).path("/info");
            then.status(200).body(r#"{"not":"a string"}"#);
        });
        let client = InfoClient::with_raw_base(&server.base_url(), Network::Mainnet);
        assert!(client.abstraction_mode(user(), None).await.is_err());
    }

    #[tokio::test]
    async fn daily_closes_parse_full_coin_name() {
        let server = MockServer::start_async().await;
        let m = server.mock(|when, then| {
            when.method(httpmock::Method::POST)
                .path("/info")
                .body_includes("candleSnapshot")
                .body_includes(r#""coin":"xyz:NBIS""#);
            then.status(200).body(
                r#"[{"t": 1759276800000, "T": 1759363200000, "s": "xyz:NBIS", "i": "1d",
                     "o": "1", "h": "1", "l": "1", "c": "42.5", "v": "1", "n": 1}]"#,
            );
        });
        let client = InfoClient::with_base_url(&server.base_url(), Network::Mainnet);
        let closes = client.daily_closes("xyz:NBIS", 121).await.unwrap();
        assert_eq!(closes.len(), 1);
        assert_eq!(closes[0].1, Decimal::new(425, 1));
        assert_eq!(m.calls(), 1);
    }
}
