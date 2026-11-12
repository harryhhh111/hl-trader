//! hl-trader：Hyperliquid 看板与交易 CLI。
//!
//! 默认 dry-run：不加载私钥、不生成签名、不发送写请求。
//! 下单和撤单必须显式加 --execute；主网或测试网联调均需另外授权。

mod adjust;
mod config;
mod dashboard;
mod hyperliquid;
mod intel;
mod leverage;
mod pair;
mod preflight;
mod receipt;
mod snapshot;
mod write_lock;

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand, ValueEnum};
use config::{Config, NETWORK, load_agent_signer, load_agent_signer_fd, load_config};
use hyperliquid::{
    Adapter, AdapterError, CancelOutcome, OrderIntent, PlaceOutcome, TESTNET_API_URL, cloid_hex,
    parse_cloid,
};
use hypersdk::Address;
use hypersdk::hypercore::PrivateKeySigner;
use receipt::{Receipt, ReceiptState, ReceiptStore};
use rust_decimal::Decimal;

const EXIT_OK: u8 = 0;
const EXIT_ERROR: u8 = 1;
const EXIT_USAGE: u8 = 2;
const EXIT_UNKNOWN: u8 = 3;

#[derive(Parser)]
#[command(
    name = "hl-trader",
    version,
    about = "Hyperliquid 看板与交易 CLI（默认 dry-run；--execute 才会签名提交）"
)]
struct Cli {
    /// 配置路径（旧命令用 TOML；dashboard/adjust 用多 pair JSON）。
    #[arg(long, global = true, default_value = "hl-trader.toml")]
    config: PathBuf,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// 查测试网行情：最新中间价、数量/价格精度、查询时间。
    Market {
        #[arg(long, default_value = "BTC")]
        coin: String,
    },
    /// 查询实际交易账户的持仓和挂单（不需要私钥）。
    Account {
        /// 实际交易账户地址；缺省用配置文件里的 account_address。
        #[arg(long)]
        address: Option<String>,
    },
    /// 限价单。默认 ALO（post-only）只预览；--execute 才签名提交。
    Order {
        #[arg(long)]
        coin: String,
        #[arg(long, value_enum)]
        side: SideArg,
        /// 十进制限价，例如 60000 或 60000.5。
        #[arg(long)]
        price: String,
        /// 十进制数量（单位：基础资产，例如 BTC）。
        #[arg(long)]
        size: String,
        /// 客户端订单 ID：0x 开头的 128 位十六进制（32 个 hex 字符）。
        #[arg(long)]
        cloid: String,
        /// 生效时间指令：alo（默认，maker-only）或 ioc（taker，未成交即撤）。
        #[arg(long, value_enum, default_value = "alo")]
        tif: TifArg,
        /// 显式授权：加载 agent 私钥并签名提交。不加则只预览校验。
        #[arg(long)]
        execute: bool,
    },
    /// 不带 --cloid：看板（仓位 + 资金费 + 动态，只读）。带 --cloid：查订单真实状态。
    Status {
        #[arg(long)]
        address: Option<String>,
        /// 按 cloid 查询订单真实状态；不带则显示看板。
        #[arg(long, conflicts_with = "json")]
        cloid: Option<String>,
        /// 输出结构化 JSON 快照（看板数据源）：使用配置中的账户地址，只读、不装载私钥。
        /// 与 --cloid 互斥；不带 --cloid 的文本看板不受影响。
        #[arg(long, conflicts_with = "cloid")]
        json: bool,
    },
    /// 按交易所订单 ID 撤指定单。默认只预览；--execute 才签名提交。
    Cancel {
        #[arg(long)]
        coin: String,
        #[arg(long)]
        oid: u64,
        #[arg(long)]
        execute: bool,
    },
    /// 把某永续标的设置为全仓模式并指定杠杆（pair 开仓的前提）。默认只预览。
    Margin {
        #[arg(long)]
        coin: String,
        /// 目标杠杆（1..=该市场上限）。
        #[arg(long)]
        leverage: u32,
        #[arg(long)]
        execute: bool,
    },
    /// 开 pair：多 HYPE 永续 + 空 BTC 永续，等名义，taker IOC。默认只预览。
    Open {
        /// 每腿 USD 名义；缺省用 min(配置单笔上限, 配置比例×权益)。
        #[arg(long)]
        notional: Option<String>,
        #[arg(long)]
        execute: bool,
    },
    /// 关掉整个 pair（reduce-only 平掉两腿）。默认只预览。
    Close {
        #[arg(long)]
        execute: bool,
    },
    /// 只读获取多 pair 两腿 BBO，不加载私钥、不读取调仓回执。
    AdjustQuote {
        #[arg(long)]
        pair: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 把多 pair 两腿调整到每腿目标 USD 名义；默认只预览，--execute 才签名下单。
    Adjust {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        notional: String,
        #[arg(long)]
        max_notional: String,
        /// 仅调整指定腿（0 或 1）；可重复，默认两腿。
        #[arg(long = "leg-index")]
        leg_indices: Vec<usize>,
        /// agent 私钥文件；仅 --execute 使用，dry-run 不读取。
        #[arg(long, conflicts_with = "secret_fd")]
        secret_file: Option<PathBuf>,
        /// 托管 Web 传入的匿名内存文件描述符；仅 --execute 使用。
        #[arg(long, requires = "execute")]
        secret_fd: Option<std::os::fd::RawFd>,
        /// pair + 账户隔离的调仓回执根目录；仅 --execute 使用。
        #[arg(long)]
        state_dir: Option<PathBuf>,
        /// 托管请求的持久化 UUID；写入发送前回执。
        #[arg(long, requires = "execute")]
        request_id: Option<String>,
        #[arg(long)]
        execute: bool,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 从继承的匿名内存文件描述符派生 API Wallet 公开地址，不访问网络。
    ApiWalletAddress {
        #[arg(long)]
        secret_fd: std::os::fd::RawFd,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 生成由浏览器 API Wallet 签名的调仓订单，不读取私钥、不发送订单。
    AdjustPrepare {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        notional: String,
        #[arg(long)]
        max_notional: String,
        /// 仅调整指定腿（0 或 1）；可重复，默认两腿。
        #[arg(long = "leg-index")]
        leg_indices: Vec<usize>,
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        signer: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 校验浏览器 API Wallet 签名并提交调仓订单，不读取私钥。
    AdjustSubmit {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        notional: String,
        #[arg(long)]
        max_notional: String,
        /// 仅调整指定腿（0 或 1）；可重复，默认两腿。
        #[arg(long = "leg-index")]
        leg_indices: Vec<usize>,
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        signer: String,
        #[arg(long)]
        action_id: String,
        /// JSON 数组：[{"leg_index":0,"signature":"0x..."}]。
        #[arg(long)]
        signatures: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 读取当前账户两腿保证金和杠杆，检查 cross 优先和最多 3x；只读。
    AdjustPreflight {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 生成由浏览器 API Wallet 签名的单合约杠杆调整，不发送写请求。
    LeveragePrepare {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        coin: String,
        #[arg(long)]
        leverage: u32,
        #[arg(long)]
        margin_mode: String,
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        signer: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 校验浏览器签名并提交单合约杠杆调整。
    LeverageSubmit {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        coin: String,
        #[arg(long)]
        leverage: u32,
        #[arg(long)]
        margin_mode: String,
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        signer: String,
        #[arg(long)]
        action_id: String,
        #[arg(long)]
        signature: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 使用托管 API Wallet 显式执行单合约杠杆调整。
    LeverageExecute {
        #[arg(long)]
        pair: String,
        #[arg(long)]
        address: String,
        #[arg(long)]
        coin: String,
        #[arg(long)]
        leverage: u32,
        #[arg(long)]
        margin_mode: String,
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long, conflicts_with = "secret_fd")]
        secret_file: Option<PathBuf>,
        #[arg(long)]
        secret_fd: Option<std::os::fd::RawFd>,
        #[arg(long)]
        request_id: String,
        #[arg(long, required = true)]
        json: bool,
    },
    /// 多 pair 主网只读看板（v2）：--config 是 JSON pair 配置；只读、不装载私钥。
    Dashboard {
        /// 采集指定 pair id 的快照；与 --announcements 二选一。
        #[arg(long, conflicts_with = "announcements")]
        pair: Option<String>,
        /// 覆盖 pair 的只读查询账户；地址由后端从已配置账户中选择。
        #[arg(long, requires = "pair", conflicts_with = "announcements")]
        address: Option<String>,
        /// 只输出平台公告块（复用 v1 结构化采集）；与 --pair 二选一。
        #[arg(long, conflicts_with = "pair")]
        announcements: bool,
        /// 必须显式给出：输出单行 JSON（stdout 唯一内容）。
        #[arg(long, required = true)]
        json: bool,
    },
}

#[derive(Clone, ValueEnum)]
enum SideArg {
    Buy,
    Sell,
}

#[derive(Clone, ValueEnum)]
enum TifArg {
    Alo,
    Ioc,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    let config_path = cli.config.clone();
    match cli.command {
        Commands::Market { coin } => cmd_market(&coin).await,
        Commands::Account { address } => cmd_account(&config_path, address).await,
        Commands::Order {
            coin,
            side,
            price,
            size,
            cloid,
            tif,
            execute,
        } => cmd_order(&config_path, coin, side, price, size, cloid, tif, execute).await,
        Commands::Status {
            address,
            cloid,
            json,
        } => cmd_status(&config_path, address, cloid, json).await,
        Commands::Cancel { coin, oid, execute } => {
            cmd_cancel(&config_path, coin, oid, execute).await
        }
        Commands::Open { notional, execute } => cmd_open(&config_path, notional, execute).await,
        Commands::Close { execute } => cmd_close(&config_path, execute).await,
        Commands::AdjustQuote { pair, json: _ } => cmd_adjust_quote(&config_path, &pair).await,
        Commands::Adjust {
            pair,
            address,
            notional,
            max_notional,
            leg_indices,
            secret_file,
            secret_fd,
            state_dir,
            request_id,
            execute,
            json: _,
        } => {
            cmd_adjust(
                &config_path,
                AdjustCommand {
                    pair_id: pair,
                    address,
                    notional,
                    max_notional,
                    leg_indices,
                    secret_file,
                    secret_fd,
                    state_dir,
                    request_id,
                    execute,
                },
            )
            .await
        }
        Commands::ApiWalletAddress { secret_fd, json: _ } => cmd_api_wallet_address(secret_fd),
        Commands::AdjustPrepare {
            pair,
            address,
            notional,
            max_notional,
            leg_indices,
            state_dir,
            signer,
            json: _,
        } => {
            cmd_adjust_prepare(
                &config_path,
                BrowserCommand {
                    pair_id: pair,
                    address,
                    notional,
                    max_notional,
                    leg_indices,
                    state_dir,
                    signer,
                },
            )
            .await
        }
        Commands::AdjustSubmit {
            pair,
            address,
            notional,
            max_notional,
            leg_indices,
            state_dir,
            signer,
            action_id,
            signatures,
            json: _,
        } => {
            cmd_adjust_submit(
                &config_path,
                BrowserCommand {
                    pair_id: pair,
                    address,
                    notional,
                    max_notional,
                    leg_indices,
                    state_dir,
                    signer,
                },
                action_id,
                signatures,
            )
            .await
        }
        Commands::AdjustPreflight { pair, address, .. } => {
            cmd_adjust_preflight(&config_path, &pair, &address).await
        }
        Commands::LeveragePrepare {
            pair,
            address,
            coin,
            leverage,
            margin_mode,
            state_dir,
            signer,
            json: _,
        } => {
            cmd_leverage_prepare(
                &config_path,
                pair,
                address,
                coin,
                leverage,
                margin_mode,
                state_dir,
                signer,
            )
            .await
        }
        Commands::LeverageSubmit {
            pair,
            address,
            coin,
            leverage,
            margin_mode,
            state_dir,
            signer,
            action_id,
            signature,
            json: _,
        } => {
            cmd_leverage_submit(
                &config_path,
                pair,
                address,
                coin,
                leverage,
                margin_mode,
                state_dir,
                signer,
                action_id,
                signature,
            )
            .await
        }
        Commands::LeverageExecute {
            pair,
            address,
            coin,
            leverage,
            margin_mode,
            state_dir,
            secret_file,
            secret_fd,
            request_id,
            json: _,
        } => {
            cmd_leverage_execute(
                &config_path,
                pair,
                address,
                coin,
                leverage,
                margin_mode,
                state_dir,
                secret_file,
                secret_fd,
                request_id,
            )
            .await
        }
        Commands::Dashboard {
            pair,
            address,
            announcements,
            json: _,
        } => cmd_dashboard_v2(&config_path, pair, address, announcements).await,
        Commands::Margin {
            coin,
            leverage,
            execute,
        } => cmd_margin(&config_path, coin, leverage, execute).await,
    }
}

fn usage_err(msg: impl std::fmt::Display) -> ExitCode {
    eprintln!("错误: {msg}");
    ExitCode::from(EXIT_USAGE)
}

fn runtime_err(msg: impl std::fmt::Display) -> ExitCode {
    eprintln!("失败: {msg}");
    ExitCode::from(EXIT_ERROR)
}

fn unknown_result(cloid: &str, detail: &str) -> ExitCode {
    eprintln!("结果未知: {detail}");
    eprintln!(
        "请不要直接重复下单。人工核对: hl-trader status --address <账户地址> --cloid {cloid}"
    );
    ExitCode::from(EXIT_UNKNOWN)
}

fn load_cfg(config_path: &std::path::Path) -> Result<Config, ExitCode> {
    load_config(config_path).map_err(|e| usage_err(format!("配置错误: {e}")))
}

fn resolve_address(flag: Option<String>, cfg: Option<&Config>) -> Result<Address, ExitCode> {
    let raw = match (flag, cfg) {
        (Some(a), _) => a,
        (None, Some(c)) => c.account_address.clone(),
        (None, None) => {
            return Err(usage_err(
                "缺少账户地址：用 --address 指定，或在配置文件中设置 account_address",
            ));
        }
    };
    raw.parse::<Address>()
        .map_err(|e| usage_err(format!("账户地址不是合法 EVM 地址: {e}")))
}

/// execute 路径公共准备：配置 + agent 私钥 + 误用检测 + 时间自检。
fn prepare_signer(cfg: &Config) -> Result<PrivateKeySigner, ExitCode> {
    let signer = load_agent_signer(&cfg.secret_file)
        .map_err(|e| usage_err(format!("私钥加载被拒绝: {e}")))?;
    let signer_addr = signer.address();
    if signer_addr == cfg.account_address().map_err(usage_err)? {
        return Err(usage_err(
            "签名地址与账户地址相同：本工具只允许使用 agent 私钥，且 agent 地址必须与账户地址不同。\
             若这是主钱包私钥，请立即改用单独的 agent 私钥（主钱包授权请在可信设备上完成）",
        ));
    }
    Adapter::check_clock().map_err(runtime_err)?;
    Ok(signer)
}

fn prepare_writer(cfg: &Config) -> Result<(PrivateKeySigner, write_lock::WriteLock), ExitCode> {
    let signer = prepare_signer(cfg)?;
    let guard = write_lock::WriteLock::agent(signer.address()).map_err(runtime_err)?;
    Ok((signer, guard))
}

async fn cmd_market(coin: &str) -> ExitCode {
    let adapter = Adapter::testnet();
    match adapter.market(coin).await {
        Ok(view) => {
            println!("network: {NETWORK}");
            println!("endpoint: {TESTNET_API_URL}");
            println!("coin: {}", view.coin);
            match view.mid {
                Some(mid) => println!("mid: {mid}"),
                None => println!("mid: 不可用"),
            }
            println!("sz_decimals: {}", view.sz_decimals);
            println!("price_tick: {}", view.tick);
            println!("query_time_utc: {}", view.query_time_utc);
            ExitCode::from(EXIT_OK)
        }
        Err(AdapterError::UnknownCoin(c)) => usage_err(format!(
            "测试网元数据中找不到交易对 {c}（首版仅支持标准 BTC 永续）"
        )),
        Err(e) => runtime_err(e),
    }
}

async fn cmd_account(config_path: &std::path::Path, address: Option<String>) -> ExitCode {
    let cfg = if address.is_none() {
        match load_cfg(config_path) {
            Ok(c) => Some(c),
            Err(code) => return code,
        }
    } else {
        load_config(config_path).ok()
    };
    let user = match resolve_address(address, cfg.as_ref()) {
        Ok(u) => u,
        Err(code) => return code,
    };
    let adapter = Adapter::testnet();
    match adapter.account(user).await {
        Ok(view) => {
            println!("network: {NETWORK}");
            println!("account: {user}");
            println!("account_value: {}", view.account_value);
            println!("available_margin: {}", view.available_margin);
            for p in &view.positions {
                println!(
                    "position: coin={} side={} size={} entry_px={} unrealized_pnl={}",
                    p.coin,
                    p.side,
                    p.size,
                    p.entry_px
                        .map(|x| x.to_string())
                        .unwrap_or_else(|| "n/a".into()),
                    p.unrealized_pnl
                );
            }
            if view.positions.is_empty() {
                println!("position: 无持仓");
            }
            for o in &view.open_orders {
                println!(
                    "open_order: coin={} side={} limit_px={} sz={} oid={}",
                    o.coin, o.side, o.limit_px, o.sz, o.oid
                );
            }
            if view.open_orders.is_empty() {
                println!("open_order: 无挂单");
            }
            println!("query_time_utc: {}", view.query_time_utc);
            ExitCode::from(EXIT_OK)
        }
        Err(e) => runtime_err(e),
    }
}

#[allow(clippy::too_many_arguments)] // CLI 命令处理函数：参数即 clap 解析结果
async fn cmd_order(
    config_path: &std::path::Path,
    coin: String,
    side: SideArg,
    price: String,
    size: String,
    cloid: String,
    tif: TifArg,
    execute: bool,
) -> ExitCode {
    let price: Decimal = match price.parse() {
        Ok(p) => p,
        Err(e) => return usage_err(format!("价格不是合法十进制数: {e}")),
    };
    let size: Decimal = match size.parse() {
        Ok(s) => s,
        Err(e) => return usage_err(format!("数量不是合法十进制数: {e}")),
    };
    let cloid = match parse_cloid(&cloid) {
        Ok(c) => c,
        Err(e) => return usage_err(format!("cloid 非法: {e}")),
    };
    let cloid_str = cloid_hex(cloid);
    let buy = matches!(side, SideArg::Buy);
    let side_str = if buy { "buy" } else { "sell" };
    let tif = match tif {
        TifArg::Alo => hypersdk::hypercore::types::TimeInForce::Alo,
        TifArg::Ioc => hypersdk::hypercore::types::TimeInForce::Ioc,
    };
    let tif_str = match tif {
        hypersdk::hypercore::types::TimeInForce::Alo => "Alo（post-only）",
        hypersdk::hypercore::types::TimeInForce::Ioc => "Ioc（taker，未成交即撤）",
        _ => "其他",
    };

    let adapter = Adapter::testnet();
    let perps = match adapter.perp_markets().await {
        Ok(p) => p,
        Err(e) => return runtime_err(e),
    };
    let market = match hyperliquid::find_market(&perps, &coin) {
        Ok(m) => m,
        Err(e) => return usage_err(e),
    };
    let cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    if let Err(e) = hyperliquid::validate_order(market, price, size, cfg.max_notional_usd) {
        return usage_err(e);
    }

    println!("network: {NETWORK}");
    println!("coin: {coin}");
    println!("side: {side_str}");
    println!("price: {price}");
    println!("size: {size}");
    println!("cloid: {cloid_str}");
    println!("tif: {tif_str}；挂单后仍可能成交，不保证可撤销");
    println!("notional_usd: {}", price * size);

    if !execute {
        println!("dry-run: 未加载私钥、未签名、未发送。确认无误后加 --execute 显式提交。");
        return ExitCode::from(EXIT_OK);
    }

    let account = match cfg.account_address() {
        Ok(account) => account,
        Err(error) => return usage_err(error),
    };
    match preflight::read_leg(&adapter, account, market).await {
        Ok(settings) if settings.ready_for_increase => {}
        Ok(settings) => return usage_err(settings.diagnostics.join("；")),
        Err(error) => return runtime_err(error),
    }

    let (signer, _writer_guard) = match prepare_writer(&cfg) {
        Ok(s) => s,
        Err(code) => return code,
    };

    let intent = OrderIntent {
        asset: market.index,
        buy,
        price,
        size,
        cloid,
        tif,
        reduce_only: false,
    };

    let store = ReceiptStore::new(cfg.receipt_dir.clone());
    let receipt = Receipt {
        version: 1,
        network: NETWORK.to_string(),
        account: cfg.account_address.clone(),
        coin: coin.clone(),
        side: side_str.to_string(),
        price: price.to_string(),
        size: size.to_string(),
        cloid: cloid_str.clone(),
        created_at_utc: chrono::Utc::now().to_rfc3339(),
        state: ReceiptState::Submitted,
        oid: None,
        detail: Some("intent recorded before submit".into()),
    };
    let guard = match store.begin(&receipt) {
        Ok(g) => g,
        Err(receipt::ReceiptError::Duplicate(_)) => {
            return usage_err(format!(
                "cloid {cloid_str} 已提交过（本地回执存在）。不要重复下单；请先查询: hl-trader status --address {} --cloid {cloid_str}",
                cfg.account_address
            ));
        }
        Err(receipt::ReceiptError::InProgress(_)) => {
            return unknown_result(
                &cloid_str,
                "另一进程正在用相同 cloid 提交，或上次在发送前中断",
            );
        }
        Err(e) => return runtime_err(e),
    };

    match adapter.place_order(&signer, &intent).await {
        Ok(PlaceOutcome::Resting { oid }) => {
            guard.update(
                ReceiptState::Resting,
                Some(oid),
                Some("resting on book".into()),
            );
            println!("submitted: resting oid={oid} cloid={cloid_str}");
            ExitCode::from(EXIT_OK)
        }
        Ok(PlaceOutcome::Filled {
            oid,
            total_sz,
            avg_px,
        }) => {
            guard.update(
                ReceiptState::Filled,
                Some(oid),
                Some(format!("filled total_sz={total_sz} avg_px={avg_px}")),
            );
            println!(
                "submitted: filled oid={oid} total_sz={total_sz} avg_px={avg_px} cloid={cloid_str}"
            );
            ExitCode::from(EXIT_OK)
        }
        Ok(PlaceOutcome::Rejected(msg)) => {
            guard.update(ReceiptState::Rejected, None, Some(msg.clone()));
            eprintln!("rejected: {msg}");
            ExitCode::from(EXIT_ERROR)
        }
        Err(AdapterError::Permission(msg)) => {
            guard.update(ReceiptState::Unknown, None, Some(msg.clone()));
            eprintln!("rejected-permission: {msg}");
            ExitCode::from(EXIT_ERROR)
        }
        Err(e @ (AdapterError::Unknown(_) | AdapterError::BadResponse(_))) => {
            guard.update(ReceiptState::Unknown, None, Some(e.to_string()));
            unknown_result(&cloid_str, &e.to_string())
        }
        Err(e) => runtime_err(e),
    }
}

async fn cmd_status(
    config_path: &std::path::Path,
    address: Option<String>,
    cloid: Option<String>,
    json: bool,
) -> ExitCode {
    if json {
        return cmd_status_json(config_path).await;
    }
    let Some(cloid) = cloid else {
        return cmd_dashboard(config_path, address).await;
    };
    let cloid = match parse_cloid(&cloid) {
        Ok(c) => c,
        Err(e) => return usage_err(format!("cloid 非法: {e}")),
    };
    let cfg = if address.is_none() {
        match load_cfg(config_path) {
            Ok(c) => Some(c),
            Err(code) => return code,
        }
    } else {
        load_config(config_path).ok()
    };
    let user = match resolve_address(address, cfg.as_ref()) {
        Ok(u) => u,
        Err(code) => return code,
    };
    let adapter = Adapter::testnet();
    match adapter.query_status(user, cloid).await {
        Ok(Some(view)) => {
            println!("network: {NETWORK}");
            println!("account: {user}");
            println!("oid: {}", view.oid);
            println!("status: {}", view.status);
            println!("coin: {}", view.coin);
            println!("side: {}", view.side);
            println!("limit_px: {}", view.limit_px);
            println!("sz: {}", view.sz);
            println!("cloid: {}", cloid_hex(cloid));
            ExitCode::from(EXIT_OK)
        }
        Ok(None) => {
            println!("network: {NETWORK}");
            println!("account: {user}");
            println!("cloid: {}", cloid_hex(cloid));
            println!("status: 交易所未找到该订单（可能已完结、已撤销，或从未提交成功）");
            ExitCode::from(EXIT_OK)
        }
        Err(e) => runtime_err(e),
    }
}

/// `status --json`：只读结构化快照（stock_data 看板契约 §4.1）。
/// 致命配置错误 → stderr + 非 0 退出，stdout 不出 JSON；成功 → stdout 单行紧凑 JSON。
async fn cmd_status_json(config_path: &std::path::Path) -> ExitCode {
    let cfg = match config::load_dashboard_config(config_path) {
        Ok(c) => c,
        Err(e) => return usage_err(format!("配置错误: {e}")),
    };
    let adapter = Adapter::testnet();
    let snapshot = snapshot::collect(&adapter, &cfg).await;
    let line = match snapshot::to_json_line(&snapshot) {
        Ok(l) => l,
        Err(e) => return runtime_err(format!("快照序列化失败: {e}")),
    };
    println!("{line}");
    ExitCode::from(EXIT_OK)
}

/// `dashboard`（v2 多 pair 主网只读看板；多 pair 方案 §5.1）。
///
/// 退出码：0 = 快照成功；1 = 配置错误/进程失败（Python 转 503）；
/// 2 = 未知或已停用 pair id（Python 转 404）。stdout 只含一行 JSON。
async fn cmd_dashboard_v2(
    config_path: &std::path::Path,
    pair_id: Option<String>,
    address: Option<String>,
    announcements: bool,
) -> ExitCode {
    if announcements {
        let snapshot = dashboard::snapshot::collect_announcements().await;
        return match dashboard::snapshot::announcements_json(&snapshot) {
            Ok(line) => {
                println!("{line}");
                ExitCode::from(EXIT_OK)
            }
            Err(e) => runtime_err(format!("快照序列化失败: {e}")),
        };
    }

    let Some(pair_id) = pair_id else {
        return usage_err("dashboard 需要 --pair <id> 或 --announcements 之一");
    };
    let cfg = match dashboard::config::load(config_path) {
        Ok(c) => c,
        // 配置非法 → 进程失败类（1），由后端转 503，不继续服务旧配置。
        Err(e) => return runtime_err(format!("看板配置错误: {e}")),
    };
    let Some(pair) = cfg.enabled_pair(&pair_id) else {
        // 未知/停用 pair：404 语义（2），与进程失败（1）区分。
        eprintln!("错误: 未知或已停用的 pair id \"{pair_id}\"");
        return ExitCode::from(EXIT_USAGE);
    };
    let mut pair = pair.clone();
    if let Some(raw) = address {
        let address = match raw.parse::<Address>() {
            Ok(address) if address != Address::ZERO => address,
            Ok(_) => return usage_err("账户地址不允许零地址"),
            Err(e) => return usage_err(format!("账户地址不是合法 EVM 地址: {e}")),
        };
        pair.descriptor.account_address = Some(format!("{address}").to_lowercase());
    }

    let client = dashboard::client::InfoClient::new(pair.descriptor.network);
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let snapshot = dashboard::snapshot::collect_pair(&client, &pair, &cfg.revision, now_ms).await;

    // 采集结束重读配置：文件在中途被替换必须明确报错重试（旧快照不进新配置缓存）。
    match dashboard::config::revision_at(config_path) {
        Ok(final_revision) if final_revision == cfg.revision => {}
        Ok(_) => {
            eprintln!("失败: 配置已变更，请重试");
            return ExitCode::from(EXIT_ERROR);
        }
        Err(e) => {
            eprintln!("失败: 配置已变更或不可读，请重试（{e}）");
            return ExitCode::from(EXIT_ERROR);
        }
    }

    match dashboard::snapshot::pair_snapshot_json(&snapshot) {
        Ok(line) => {
            println!("{line}");
            ExitCode::from(EXIT_OK)
        }
        Err(e) => runtime_err(format!("快照序列化失败: {e}")),
    }
}

async fn cmd_open(
    config_path: &std::path::Path,
    notional: Option<String>,
    execute: bool,
) -> ExitCode {
    let cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let requested: Option<Decimal> = match notional {
        Some(n) => match n.parse() {
            Ok(v) => Some(v),
            Err(e) => return usage_err(format!("--notional 不是合法十进制数: {e}")),
        },
        None => None,
    };
    let writer = if execute {
        match prepare_writer(&cfg) {
            Ok(s) => Some(s),
            Err(code) => return code,
        }
    } else {
        None
    };
    let signer = writer.as_ref().map(|(signer, _guard)| signer);
    let adapter = Adapter::testnet();
    match pair::run_open(&adapter, signer, &cfg, requested, execute).await {
        Ok(report) => {
            let (code, lines) = report.into_exit_code();
            for l in lines {
                if code == std::process::ExitCode::from(EXIT_OK) {
                    println!("{l}");
                } else {
                    eprintln!("{l}");
                }
            }
            code
        }
        Err(pair::PairError::Unresolved(m)) => {
            eprintln!("结果待核对: {m}");
            ExitCode::from(EXIT_UNKNOWN)
        }
        Err(pair::PairError::Refused(m)) => runtime_err(m),
        Err(pair::PairError::Locked(p)) => runtime_err(format!(
            "无法获取 pair 写锁 {}；请等待现有写进程结束，不要删除锁文件",
            p.display()
        )),
        Err(e) => runtime_err(e),
    }
}

async fn cmd_close(config_path: &std::path::Path, execute: bool) -> ExitCode {
    let cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let writer = if execute {
        match prepare_writer(&cfg) {
            Ok(s) => Some(s),
            Err(code) => return code,
        }
    } else {
        None
    };
    let signer = writer.as_ref().map(|(signer, _guard)| signer);
    let adapter = Adapter::testnet();
    match pair::run_close(&adapter, signer, &cfg, execute).await {
        Ok(report) => {
            let (code, lines) = report.into_exit_code();
            for l in lines {
                if code == std::process::ExitCode::from(EXIT_OK) {
                    println!("{l}");
                } else {
                    eprintln!("{l}");
                }
            }
            code
        }
        Err(pair::PairError::Unresolved(m)) => {
            eprintln!("结果待核对: {m}");
            ExitCode::from(EXIT_UNKNOWN)
        }
        Err(pair::PairError::Refused(m)) => runtime_err(m),
        Err(pair::PairError::Locked(p)) => runtime_err(format!(
            "无法获取 pair 写锁 {}；请等待现有写进程结束，不要删除锁文件",
            p.display()
        )),
        Err(e) => runtime_err(e),
    }
}

async fn cmd_dashboard(config_path: &std::path::Path, address: Option<String>) -> ExitCode {
    let mut cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let adapter = Adapter::testnet();
    if let Some(address) = address {
        if let Err(e) = address.parse::<Address>() {
            return usage_err(e);
        }
        cfg.account_address = address;
    }
    for line in intel::render(&adapter, &cfg).await {
        println!("{line}");
    }
    ExitCode::from(EXIT_OK)
}

async fn cmd_cancel(
    config_path: &std::path::Path,
    coin: String,
    oid: u64,
    execute: bool,
) -> ExitCode {
    let adapter = Adapter::testnet();
    let perps = match adapter.perp_markets().await {
        Ok(p) => p,
        Err(e) => return runtime_err(e),
    };
    let market = match hyperliquid::find_market(&perps, &coin) {
        Ok(m) => m,
        Err(e) => return usage_err(e),
    };

    println!("network: {NETWORK}");
    println!("coin: {coin}");
    println!("oid: {oid}");
    if !execute {
        println!("dry-run: 未加载私钥、未签名、未发送。确认无误后加 --execute 显式撤单。");
        return ExitCode::from(EXIT_OK);
    }

    let cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let (signer, _writer_guard) = match prepare_writer(&cfg) {
        Ok(s) => s,
        Err(code) => return code,
    };

    match adapter.cancel_oid(&signer, market.index, oid).await {
        Ok(CancelOutcome::Canceled) => {
            println!("canceled: oid={oid}");
            ExitCode::from(EXIT_OK)
        }
        Ok(CancelOutcome::Rejected(msg)) => {
            eprintln!("cancel-failed: {msg}");
            eprintln!(
                "提示: 订单可能已成交或已撤销；请用 status/account 核对实际状态，工具不会声称已消除敞口。"
            );
            ExitCode::from(EXIT_ERROR)
        }
        Err(AdapterError::Permission(msg)) => {
            eprintln!("cancel-failed-permission: {msg}");
            ExitCode::from(EXIT_ERROR)
        }
        Err(e @ (AdapterError::Unknown(_) | AdapterError::BadResponse(_))) => {
            unknown_result(&format!("oid={oid}"), &e.to_string())
        }
        Err(e) => runtime_err(e),
    }
}

struct AdjustCommand {
    pair_id: String,
    address: String,
    notional: String,
    max_notional: String,
    leg_indices: Vec<usize>,
    secret_file: Option<PathBuf>,
    secret_fd: Option<std::os::fd::RawFd>,
    state_dir: Option<PathBuf>,
    request_id: Option<String>,
    execute: bool,
}

fn leg_selection(indices: &[usize]) -> Result<[bool; 2], String> {
    if indices.is_empty() {
        return Ok([true, true]);
    }
    let mut selected = [false, false];
    for index in indices {
        if *index > 1 || selected[*index] {
            return Err("--leg-index 只能分别指定 0 和 1，且不能重复".into());
        }
        selected[*index] = true;
    }
    Ok(selected)
}

fn valid_request_id(value: &str) -> bool {
    value.len() == 36
        && value.char_indices().all(|(index, character)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                character == '-'
            } else {
                character.is_ascii_hexdigit()
            }
        })
}

fn cmd_api_wallet_address(secret_fd: std::os::fd::RawFd) -> ExitCode {
    match load_agent_signer_fd(secret_fd) {
        Ok(signer) => {
            let output = serde_json::json!({
                "schema_version": 1,
                "address": format!("{}", signer.address()).to_lowercase(),
            });
            println!("{output}");
            EXIT_OK.into()
        }
        Err(error) => usage_err(format!("API Wallet 私钥加载被拒绝: {error}")),
    }
}

async fn cmd_adjust_preflight(
    config_path: &std::path::Path,
    pair_id: &str,
    address: &str,
) -> ExitCode {
    let config = match dashboard::config::load(config_path) {
        Ok(config) => config,
        Err(error) => return usage_err(format!("pair 配置错误: {error}")),
    };
    let pair = match config.enabled_pair(pair_id) {
        Some(pair) => pair,
        None => return usage_err(format!("未知或停用的 pair: {pair_id}")),
    };
    let account = match address.parse::<Address>() {
        Ok(account) if account != Address::ZERO => account,
        _ => return usage_err("账户地址必须是合法非零地址"),
    };
    let adapter = Adapter::for_network(pair.descriptor.network);
    match preflight::collect(&adapter, pair, account, &config.revision).await {
        Ok(report) => {
            if dashboard::config::revision_at(config_path).ok().as_deref()
                != Some(config.revision.as_str())
            {
                return usage_err("配置在检查期间发生变化，请刷新页面");
            }
            match serde_json::to_string(&report) {
                Ok(json) => {
                    println!("{json}");
                    EXIT_OK.into()
                }
                Err(error) => runtime_err(error),
            }
        }
        Err(error) => runtime_err(error),
    }
}

async fn cmd_adjust_quote(config_path: &std::path::Path, pair_id: &str) -> ExitCode {
    let config = match dashboard::config::load(config_path) {
        Ok(config) => config,
        Err(error) => return usage_err(format!("pair 配置错误: {error}")),
    };
    let pair = match config.enabled_pair(pair_id) {
        Some(pair) => pair,
        None => return usage_err(format!("未知或停用的 pair: {pair_id}")),
    };
    let adapter = Adapter::for_network(pair.descriptor.network);
    match adjust::collect_adjustment_quote(&adapter, pair, config_path, &config.revision).await {
        Ok(quote) => match serde_json::to_string(&quote) {
            Ok(json) => {
                println!("{json}");
                EXIT_OK.into()
            }
            Err(error) => runtime_err(format!("盘口序列化失败: {error}")),
        },
        Err(adjust::AdjustError::Refused(message)) => usage_err(message),
        Err(error) => runtime_err(error),
    }
}

async fn cmd_adjust(config_path: &std::path::Path, command: AdjustCommand) -> ExitCode {
    let AdjustCommand {
        pair_id,
        address,
        notional,
        max_notional,
        leg_indices,
        secret_file,
        secret_fd,
        state_dir,
        request_id,
        execute,
    } = command;
    let config = match dashboard::config::load(config_path) {
        Ok(config) => config,
        Err(error) => return usage_err(format!("pair 配置错误: {error}")),
    };
    let pair = match config.enabled_pair(&pair_id) {
        Some(pair) => pair,
        None => return usage_err(format!("未知或停用的 pair: {pair_id}")),
    };
    let account = match address.parse::<Address>() {
        Ok(account) if account != Address::ZERO => account,
        Ok(_) => return usage_err("账户地址不能是零地址"),
        Err(error) => return usage_err(format!("账户地址非法: {error}")),
    };
    let target = match notional.parse::<Decimal>() {
        Ok(target) => target,
        Err(error) => return usage_err(format!("--notional 非法: {error}")),
    };
    let maximum = match max_notional.parse::<Decimal>() {
        Ok(maximum) => maximum,
        Err(error) => return usage_err(format!("--max-notional 非法: {error}")),
    };
    let leg_indices = match leg_selection(&leg_indices) {
        Ok(selected) => selected,
        Err(error) => return usage_err(error),
    };
    if request_id
        .as_deref()
        .is_some_and(|value| !valid_request_id(value))
    {
        return usage_err("--request-id 必须是 UUID 格式");
    }
    let (signer, _writer_guard) = if execute {
        let signer = match (secret_file, secret_fd) {
            (Some(secret_file), None) => load_agent_signer(&secret_file),
            (None, Some(secret_fd)) => load_agent_signer_fd(secret_fd),
            _ => return usage_err("--execute 必须且只能提供 --secret-file 或 --secret-fd"),
        };
        let Some(_state_dir) = state_dir.as_ref() else {
            return usage_err("--execute 必须显式提供 --state-dir");
        };
        let signer = match signer {
            Ok(signer) => signer,
            Err(error) => return usage_err(format!("私钥加载被拒绝: {error}")),
        };
        if signer.address() == account {
            return usage_err("只允许使用与交易账户不同的 agent 私钥，拒绝主钱包私钥");
        }
        if let Err(error) = Adapter::check_clock() {
            return runtime_err(error);
        }
        let guard = match write_lock::WriteLock::agent(signer.address()) {
            Ok(guard) => guard,
            Err(error) => return runtime_err(error),
        };
        (Some(signer), Some(guard))
    } else {
        (None, None)
    };
    let adapter = Adapter::for_network(pair.descriptor.network);
    let request = adjust::AdjustRequest {
        pair,
        config_path,
        config_revision: &config.revision,
        account,
        target_notional: target,
        max_notional: maximum,
        leg_indices,
        state_dir: state_dir
            .as_deref()
            .unwrap_or_else(|| std::path::Path::new("")),
        request_id: request_id.as_deref(),
    };
    match adjust::run_adjust(&adapter, signer.as_ref(), &request, execute).await {
        Ok(report) => {
            let code = match report.status {
                adjust::AdjustmentStatus::Succeeded | adjust::AdjustmentStatus::Preview => EXIT_OK,
                adjust::AdjustmentStatus::Unknown => EXIT_UNKNOWN,
                _ => EXIT_ERROR,
            };
            match serde_json::to_string(&report) {
                Ok(json) => println!("{json}"),
                Err(error) => return runtime_err(format!("调仓结果序列化失败: {error}")),
            }
            ExitCode::from(code)
        }
        Err(adjust::AdjustError::Refused(message)) => usage_err(message),
        Err(adjust::AdjustError::Unresolved(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_UNKNOWN)
        }
        Err(error) => runtime_err(error),
    }
}

struct BrowserCommand {
    pair_id: String,
    address: String,
    notional: String,
    max_notional: String,
    leg_indices: Vec<usize>,
    state_dir: PathBuf,
    signer: String,
}

struct BrowserContext {
    config: dashboard::config::DashboardConfig,
    path: PathBuf,
    pair: dashboard::config::DashboardPair,
    account: Address,
    target: Decimal,
    maximum: Decimal,
    leg_indices: [bool; 2],
    state_dir: PathBuf,
    signer: Address,
}

impl BrowserContext {
    fn request(&self) -> adjust::AdjustRequest<'_> {
        adjust::AdjustRequest {
            pair: &self.pair,
            config_path: &self.path,
            config_revision: &self.config.revision,
            account: self.account,
            target_notional: self.target,
            max_notional: self.maximum,
            leg_indices: self.leg_indices,
            state_dir: &self.state_dir,
            request_id: None,
        }
    }
}

fn load_browser_context(
    config_path: &std::path::Path,
    command: BrowserCommand,
) -> Result<BrowserContext, ExitCode> {
    let config = match dashboard::config::load(config_path) {
        Ok(config) => config,
        Err(error) => return Err(usage_err(format!("pair 配置错误: {error}"))),
    };
    let pair = match config.enabled_pair(&command.pair_id) {
        Some(pair) => pair.clone(),
        None => return Err(usage_err(format!("未知或停用的 pair: {}", command.pair_id))),
    };
    let account = match command.address.parse::<Address>() {
        Ok(account) if account != Address::ZERO => account,
        Ok(_) => return Err(usage_err("账户地址不能是零地址")),
        Err(error) => return Err(usage_err(format!("账户地址非法: {error}"))),
    };
    let signer = match command.signer.parse::<Address>() {
        Ok(signer) if signer != Address::ZERO && signer != account => signer,
        Ok(_) => return Err(usage_err("签名地址必须是非零且不同于交易账户的 API Wallet")),
        Err(error) => return Err(usage_err(format!("API Wallet 地址非法: {error}"))),
    };
    let target = match command.notional.parse::<Decimal>() {
        Ok(target) => target,
        Err(error) => return Err(usage_err(format!("--notional 非法: {error}"))),
    };
    let maximum = match command.max_notional.parse::<Decimal>() {
        Ok(maximum) => maximum,
        Err(error) => return Err(usage_err(format!("--max-notional 非法: {error}"))),
    };
    let leg_indices = match leg_selection(&command.leg_indices) {
        Ok(selected) => selected,
        Err(error) => return Err(usage_err(error)),
    };
    if let Err(error) = Adapter::check_clock() {
        return Err(runtime_err(error));
    }
    Ok(BrowserContext {
        config,
        pair,
        account,
        target,
        maximum,
        leg_indices,
        state_dir: command.state_dir,
        signer,
        path: config_path.to_path_buf(),
    })
}

fn adjust_result(result: Result<adjust::AdjustmentReport, adjust::AdjustError>) -> ExitCode {
    match result {
        Ok(report) => {
            let code = match report.status {
                adjust::AdjustmentStatus::Preview
                | adjust::AdjustmentStatus::Prepared
                | adjust::AdjustmentStatus::Succeeded => EXIT_OK,
                adjust::AdjustmentStatus::Unknown => EXIT_UNKNOWN,
                _ => EXIT_ERROR,
            };
            match serde_json::to_string(&report) {
                Ok(json) => println!("{json}"),
                Err(error) => return runtime_err(format!("调仓结果序列化失败: {error}")),
            }
            ExitCode::from(code)
        }
        Err(adjust::AdjustError::Refused(message)) => usage_err(message),
        Err(adjust::AdjustError::Unresolved(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_UNKNOWN)
        }
        Err(error) => runtime_err(error),
    }
}

async fn cmd_adjust_prepare(config_path: &std::path::Path, command: BrowserCommand) -> ExitCode {
    let context = match load_browser_context(config_path, command) {
        Ok(context) => context,
        Err(code) => return code,
    };
    let adapter = Adapter::for_network(context.pair.descriptor.network);
    adjust_result(
        adjust::prepare_browser_adjustment(&adapter, &context.request(), context.signer).await,
    )
}

async fn cmd_adjust_submit(
    config_path: &std::path::Path,
    command: BrowserCommand,
    action_id: String,
    signatures: String,
) -> ExitCode {
    let context = match load_browser_context(config_path, command) {
        Ok(context) => context,
        Err(code) => return code,
    };
    let signatures = match serde_json::from_str::<Vec<adjust::BrowserSignature>>(&signatures) {
        Ok(signatures) => signatures,
        Err(error) => return usage_err(format!("--signatures 不是合法签名 JSON: {error}")),
    };
    let adapter = Adapter::for_network(context.pair.descriptor.network);
    adjust_result(
        adjust::submit_browser_adjustment(
            &adapter,
            &context.request(),
            context.signer,
            &action_id,
            signatures,
        )
        .await,
    )
}

struct LeverageCommand {
    pair_id: String,
    address: String,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    signer: String,
}

struct LeverageContext {
    config: dashboard::config::DashboardConfig,
    path: PathBuf,
    pair: dashboard::config::DashboardPair,
    account: Address,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    signer: Address,
}

impl LeverageContext {
    fn request(&self) -> leverage::LeverageRequest<'_> {
        leverage::LeverageRequest {
            pair: &self.pair,
            config_path: &self.path,
            config_revision: &self.config.revision,
            account: self.account,
            coin: &self.coin,
            leverage: self.leverage,
            margin_mode: &self.margin_mode,
            state_dir: &self.state_dir,
        }
    }
}

fn load_leverage_context(
    config_path: &std::path::Path,
    command: LeverageCommand,
) -> Result<LeverageContext, ExitCode> {
    let config = match dashboard::config::load(config_path) {
        Ok(config) => config,
        Err(error) => return Err(usage_err(format!("pair 配置错误: {error}"))),
    };
    let pair = match config.enabled_pair(&command.pair_id) {
        Some(pair) => pair.clone(),
        None => return Err(usage_err(format!("未知或停用的 pair: {}", command.pair_id))),
    };
    let account = match command.address.parse::<Address>() {
        Ok(account) if account != Address::ZERO => account,
        Ok(_) => return Err(usage_err("账户地址不能是零地址")),
        Err(error) => return Err(usage_err(format!("账户地址非法: {error}"))),
    };
    let signer = match command.signer.parse::<Address>() {
        Ok(signer) if signer != Address::ZERO && signer != account => signer,
        Ok(_) => return Err(usage_err("签名地址必须是非零且不同于交易账户的 API Wallet")),
        Err(error) => return Err(usage_err(format!("API Wallet 地址非法: {error}"))),
    };
    if let Err(error) = Adapter::check_clock() {
        return Err(runtime_err(error));
    }
    Ok(LeverageContext {
        config,
        pair,
        account,
        coin: command.coin,
        leverage: command.leverage,
        margin_mode: command.margin_mode,
        state_dir: command.state_dir,
        signer,
        path: config_path.to_path_buf(),
    })
}

#[allow(clippy::too_many_arguments)]
fn leverage_command(
    pair: String,
    address: String,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    signer: String,
) -> LeverageCommand {
    LeverageCommand {
        pair_id: pair,
        address,
        coin,
        leverage,
        margin_mode,
        state_dir,
        signer,
    }
}

fn leverage_result(result: Result<leverage::LeverageReport, adjust::AdjustError>) -> ExitCode {
    match result {
        Ok(report) => {
            let code = match report.status.as_str() {
                "prepared" | "succeeded" => EXIT_OK,
                "unknown" => EXIT_UNKNOWN,
                _ => EXIT_ERROR,
            };
            match serde_json::to_string(&report) {
                Ok(json) => println!("{json}"),
                Err(error) => return runtime_err(format!("杠杆结果序列化失败: {error}")),
            }
            ExitCode::from(code)
        }
        Err(adjust::AdjustError::Refused(message)) => usage_err(message),
        Err(error) => runtime_err(error),
    }
}

#[allow(clippy::too_many_arguments)]
async fn cmd_leverage_prepare(
    config_path: &std::path::Path,
    pair: String,
    address: String,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    signer: String,
) -> ExitCode {
    let command = leverage_command(
        pair,
        address,
        coin,
        leverage,
        margin_mode,
        state_dir,
        signer,
    );
    let context = match load_leverage_context(config_path, command) {
        Ok(context) => context,
        Err(code) => return code,
    };
    let adapter = Adapter::for_network(context.pair.descriptor.network);
    leverage_result(leverage::prepare(&adapter, &context.request(), context.signer, None).await)
}

#[allow(clippy::too_many_arguments)]
async fn cmd_leverage_submit(
    config_path: &std::path::Path,
    pair: String,
    address: String,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    signer: String,
    action_id: String,
    signature: String,
) -> ExitCode {
    let command = leverage_command(
        pair,
        address,
        coin,
        leverage,
        margin_mode,
        state_dir,
        signer,
    );
    let context = match load_leverage_context(config_path, command) {
        Ok(context) => context,
        Err(code) => return code,
    };
    let signature = match signature.parse() {
        Ok(signature) => signature,
        Err(error) => return usage_err(format!("签名非法: {error}")),
    };
    let adapter = Adapter::for_network(context.pair.descriptor.network);
    leverage_result(
        leverage::submit(
            &adapter,
            &context.request(),
            context.signer,
            &action_id,
            signature,
        )
        .await,
    )
}

#[allow(clippy::too_many_arguments)]
async fn cmd_leverage_execute(
    config_path: &std::path::Path,
    pair: String,
    address: String,
    coin: String,
    leverage: u32,
    margin_mode: String,
    state_dir: PathBuf,
    secret_file: Option<PathBuf>,
    secret_fd: Option<std::os::fd::RawFd>,
    request_id: String,
) -> ExitCode {
    if !valid_request_id(&request_id) {
        return usage_err("--request-id 必须是 UUID 格式");
    }
    let signer = match (secret_file, secret_fd) {
        (Some(path), None) => load_agent_signer(&path),
        (None, Some(fd)) => load_agent_signer_fd(fd),
        _ => return usage_err("必须且只能提供 --secret-file 或 --secret-fd"),
    };
    let signer = match signer {
        Ok(signer) => signer,
        Err(error) => return usage_err(format!("私钥加载被拒绝: {error}")),
    };
    let command = leverage_command(
        pair,
        address,
        coin,
        leverage,
        margin_mode,
        state_dir,
        format!("{}", signer.address()).to_lowercase(),
    );
    let context = match load_leverage_context(config_path, command) {
        Ok(context) => context,
        Err(code) => return code,
    };
    let adapter = Adapter::for_network(context.pair.descriptor.network);
    leverage_result(
        leverage::execute_managed(&adapter, &signer, &context.request(), &request_id).await,
    )
}

async fn cmd_margin(
    config_path: &std::path::Path,
    coin: String,
    leverage: u32,
    execute: bool,
) -> ExitCode {
    if leverage > preflight::MAX_LEVERAGE {
        return usage_err("本应用杠杆上限为 3x，请设置 1 到 3 倍");
    }
    let adapter = Adapter::testnet();
    let perps = match adapter.perp_markets().await {
        Ok(p) => p,
        Err(e) => return runtime_err(e),
    };
    let market = match hyperliquid::find_market(&perps, &coin) {
        Ok(m) => m,
        Err(e) => return usage_err(e),
    };
    if leverage == 0 || u64::from(leverage) > market.max_leverage {
        return usage_err(format!(
            "杠杆 {leverage} 超出该市场允许范围 1..={}",
            market.max_leverage
        ));
    }
    let cfg = match load_cfg(config_path) {
        Ok(c) => c,
        Err(code) => return code,
    };
    let user = match cfg.account_address() {
        Ok(u) => u,
        Err(e) => return usage_err(e),
    };
    let current = match adapter.cross_margin(user, &market.name).await {
        Ok(cross) => {
            if cross {
                "cross"
            } else {
                "isolated"
            }
        }
        Err(e) => return runtime_err(e),
    };

    println!("network: {NETWORK}");
    println!("coin: {}", market.name);
    println!("asset_index: {}", market.index);
    println!("current_margin_mode: {current}");
    println!("planned: 设为全仓(cross) {leverage}x");
    if !execute {
        println!("dry-run: 未加载私钥、未签名、未发送。确认无误后加 --execute 显式设置。");
        return ExitCode::from(EXIT_OK);
    }

    let (signer, _writer_guard) = match prepare_writer(&cfg) {
        Ok(s) => s,
        Err(code) => return code,
    };
    match adapter
        .set_cross_leverage(&signer, market.index, leverage)
        .await
    {
        Ok(()) => {
            println!(
                "已设置: {} → cross {leverage}x (asset_index={})",
                market.name, market.index
            );
            ExitCode::from(EXIT_OK)
        }
        Err(AdapterError::Permission(msg)) => {
            eprintln!("margin-failed-permission: {msg}");
            ExitCode::from(EXIT_ERROR)
        }
        Err(e @ (AdapterError::Unknown(_) | AdapterError::BadResponse(_))) => {
            unknown_result(&format!("coin={coin}"), &e.to_string())
        }
        Err(e) => runtime_err(e),
    }
}
