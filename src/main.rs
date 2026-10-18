//! hl-trader：Hyperliquid 测试网最小交易 CLI。
//!
//! 默认 dry-run（MVP_PLAN §4）：不加载私钥、不生成签名、不发送写请求。
//! 下单和撤单必须显式加 --execute，且需要用户另外授权测试网联调。

mod config;
mod dashboard;
mod hyperliquid;
mod intel;
mod pair;
mod receipt;
mod snapshot;
mod write_lock;

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand, ValueEnum};
use config::{Config, NETWORK, load_agent_signer, load_config};
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
    about = "Hyperliquid 测试网最小 CLI（默认 dry-run；--execute 才会签名提交）"
)]
struct Cli {
    /// 配置文件路径（TOML；示例见 hl-trader.example.toml）。
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
    /// 多 pair 主网只读看板（v2）：--config 是 JSON pair 配置；只读、不装载私钥。
    Dashboard {
        /// 采集指定 pair id 的快照；与 --announcements 二选一。
        #[arg(long, conflicts_with = "announcements")]
        pair: Option<String>,
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
        Commands::Dashboard {
            pair,
            announcements,
            json: _,
        } => cmd_dashboard_v2(&config_path, pair, announcements).await,
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

    let client = dashboard::client::InfoClient::new(pair.descriptor.network);
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let snapshot = dashboard::snapshot::collect_pair(&client, pair, &cfg.revision, now_ms).await;

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

async fn cmd_margin(
    config_path: &std::path::Path,
    coin: String,
    leverage: u32,
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
