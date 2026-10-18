//! Public, read-only official channel previews. No login, tokens or message sending.
use chrono::{DateTime, Utc};
use scraper::{Html, Selector};
use std::collections::BTreeMap;
use std::time::Duration;

const MAX_BODY_BYTES: usize = 1_048_576;
const DISPLAY_COUNT: usize = 3;

struct Source {
    label: &'static str,
    channel: &'static str,
    url: &'static str,
}

// Channel identities are listed in the official support/API support documentation.
const SOURCES: [Source; 2] = [
    Source {
        label: "官方公告",
        channel: "hyperliquid_announcements",
        url: "https://t.me/s/hyperliquid_announcements",
    },
    Source {
        label: "API 更新",
        channel: "hyperliquid_api",
        url: "https://t.me/s/hyperliquid_api",
    },
];

// ---------- 结构化采集（status --json 看板契约 §4.1；文本看板保持原样） ----------

/// 看板来源：kind 为契约固定的 "general" / "api"；URL 由 base + channel 组成，便于测试注入。
pub(crate) struct DashboardSource {
    pub kind: &'static str,
    pub channel: &'static str,
}

pub(crate) fn dashboard_sources() -> [DashboardSource; 2] {
    [
        DashboardSource {
            kind: "general",
            channel: SOURCES[0].channel,
        },
        DashboardSource {
            kind: "api",
            channel: SOURCES[1].channel,
        },
    ]
}

pub(crate) struct DashboardEntry {
    pub text: String,
    /// 原文发布时间；公开预览缺时间元素或时间不可信时为 None（输出 null + 诊断）。
    pub published: Option<DateTime<Utc>>,
    pub url: String,
    pub id: u64,
}

pub(crate) struct DashboardOutcome {
    /// 按 (published 倒序, id 倒序) 已排序；None 时间的条目排在最后。
    pub entries: Vec<DashboardEntry>,
    /// 来源可验证但缺发布时间的条目数（published_at 输出 null）。
    pub missing_time: usize,
    /// 来源/链接无法验证而被丢弃的条目数。
    pub invalid: usize,
}

/// 采集单个频道的结构化公告；任何传输/解析失败都返回 Err（该 kind 整体降级）。
pub(crate) async fn collect_dashboard(
    client: &reqwest::Client,
    base: &str,
    source: &DashboardSource,
) -> Result<DashboardOutcome, String> {
    let url = format!("{base}/{}", source.channel);
    let html = fetch(client, &url).await?;
    parse_dashboard(&html, source.channel, Utc::now())
}

fn parse_dashboard(
    html: &str,
    channel: &str,
    now: DateTime<Utc>,
) -> Result<DashboardOutcome, String> {
    let doc = Html::parse_document(html);
    let messages = Selector::parse(".tgme_widget_message[data-post]").unwrap();
    let links = Selector::parse("a.tgme_widget_message_date").unwrap();
    let times = Selector::parse("time[datetime]").unwrap();
    let texts = Selector::parse(".tgme_widget_message_text").unwrap();
    let mut entries = BTreeMap::new();
    let mut missing_time = 0usize;
    let mut invalid = 0usize;
    for message in doc.select(&messages) {
        let item = (|| {
            let post = message.value().attr("data-post")?;
            let (owner, id) = post.split_once('/')?;
            let id: u64 = id.parse().ok()?;
            if owner != channel || id == 0 {
                return None;
            }
            let url = format!("https://t.me/{channel}/{id}");
            let link = message.select(&links).next()?;
            if link.value().attr("href")? != url {
                return None;
            }
            // 时间缺失/不可解析/晚于当前时间（不可信）：条目保留，published_at 输出 null。
            let published = link
                .select(&times)
                .next()
                .and_then(|t| t.value().attr("datetime"))
                .and_then(|raw| DateTime::parse_from_rfc3339(raw).ok())
                .map(|dt| dt.with_timezone(&Utc))
                .filter(|dt| *dt <= now + chrono::Duration::minutes(5));
            let text = message
                .select(&texts)
                .next()
                .map(|text| clean_excerpt(&text.text().collect::<Vec<_>>().join(" ")))
                .filter(|text| !text.is_empty())
                .unwrap_or_else(|| "[媒体公告，请查看原文]".into());
            Some(DashboardEntry {
                text,
                published,
                url,
                id,
            })
        })();
        match item {
            Some(entry) => {
                if entry.published.is_none() {
                    missing_time += 1;
                }
                entries.insert(entry.id, entry);
            }
            None => invalid += 1,
        }
    }
    if entries.is_empty() {
        return Err("未识别到带有效来源的公告，页面可能受限或结构已变化".into());
    }
    let mut entries: Vec<_> = entries.into_values().collect();
    entries.sort_by_key(|e| {
        std::cmp::Reverse((e.published.unwrap_or(DateTime::<Utc>::MIN_UTC), e.id))
    });
    Ok(DashboardOutcome {
        entries,
        missing_time,
        invalid,
    })
}

#[derive(Debug)]
struct Announcement {
    id: u64,
    published: DateTime<Utc>,
    url: String,
    excerpt: String,
}

struct Parsed {
    entries: Vec<Announcement>,
    invalid: usize,
}

pub(super) async fn render() -> Vec<String> {
    let client = match reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(6))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(client) => client,
        Err(_) => return vec!["  官方动态: 不可用（HTTP 客户端构建失败）".into()],
    };
    let (general, api) = tokio::join!(
        render_source(&client, &SOURCES[0]),
        render_source(&client, &SOURCES[1]),
    );
    let mut lines = vec!["  官方动态（公开频道，内容可能涉及主网或测试网，以原文为准）:".into()];
    lines.extend(general);
    lines.extend(api);
    lines.push("  仅展示公开预览中最近可见公告，不代表完整历史或没有其他更新；X: https://x.com/HyperliquidX / https://x.com/HyperFND".into());
    lines
}

async fn render_source(client: &reqwest::Client, source: &Source) -> Vec<String> {
    let result = fetch(client, source.url).await;
    let fetched = Utc::now();
    let parsed = result.and_then(|html| parse(&html, source.channel, fetched));
    format_source(source, parsed, fetched)
}

fn format_source(
    source: &Source,
    parsed: Result<Parsed, String>,
    fetched: DateTime<Utc>,
) -> Vec<String> {
    let mut lines = vec![format!(
        "  {}: {}（查询时间 {}）",
        source.label,
        source.url,
        fetched.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    )];
    match parsed {
        Ok(parsed) => {
            if let Some(latest) = parsed.entries.first() {
                let age = (fetched - latest.published).num_days();
                if age >= 7 {
                    lines.push(format!(
                        "    最近可见公告发表于 {age} 天前；请核对原频道的新鲜度"
                    ));
                }
            }
            for item in parsed.entries.iter().take(DISPLAY_COUNT) {
                lines.push(format!(
                    "    {} | {} | {}",
                    item.published.format("%Y-%m-%d %H:%M UTC"),
                    item.excerpt,
                    item.url
                ));
            }
            if parsed.invalid > 0 {
                lines.push(format!(
                    "    {} 条公告无法验证来源/时间，未展示；结果可能不完整",
                    parsed.invalid
                ));
            }
        }
        Err(reason) => lines.push(format!(
            "    不可用（{reason}）；本次未取得可验证公告，不代表没有新公告，请查看原频道"
        )),
    }
    lines
}

async fn fetch(client: &reqwest::Client, url: &str) -> Result<String, String> {
    let mut response = client.get(url).send().await.map_err(|e| {
        if e.is_timeout() {
            "请求超时".to_string()
        } else {
            "连接失败".to_string()
        }
    })?;
    if !response.status().is_success() {
        return Err(format!("HTTP {}", response.status().as_u16()));
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !content_type
        .split(';')
        .next()
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("text/html"))
    {
        return Err("响应不是 HTML 公告页".into());
    }
    if response
        .content_length()
        .is_some_and(|len| len > MAX_BODY_BYTES as u64)
    {
        return Err("公告页超过大小限制".into());
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "读取公告页失败")? {
        if bytes.len() + chunk.len() > MAX_BODY_BYTES {
            return Err("公告页超过大小限制".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    String::from_utf8(bytes).map_err(|_| "公告页不是 UTF-8".into())
}

fn parse(html: &str, channel: &str, now: DateTime<Utc>) -> Result<Parsed, String> {
    let doc = Html::parse_document(html);
    let messages = Selector::parse(".tgme_widget_message[data-post]").unwrap();
    let links = Selector::parse("a.tgme_widget_message_date").unwrap();
    let times = Selector::parse("time[datetime]").unwrap();
    let texts = Selector::parse(".tgme_widget_message_text").unwrap();
    let mut entries = BTreeMap::new();
    let mut invalid = 0;
    for message in doc.select(&messages) {
        let item = (|| {
            let post = message.value().attr("data-post")?;
            let (owner, id) = post.split_once('/')?;
            let id: u64 = id.parse().ok()?;
            if owner != channel || id == 0 {
                return None;
            }
            let url = format!("https://t.me/{channel}/{id}");
            let link = message.select(&links).next()?;
            if link.value().attr("href")? != url {
                return None;
            }
            let timestamp = link.select(&times).next()?.value().attr("datetime")?;
            let published = DateTime::parse_from_rfc3339(timestamp)
                .ok()?
                .with_timezone(&Utc);
            if published > now + chrono::Duration::minutes(5) {
                return None;
            }
            let excerpt = message
                .select(&texts)
                .next()
                .map(|text| clean_excerpt(&text.text().collect::<Vec<_>>().join(" ")))
                .filter(|text| !text.is_empty())
                .unwrap_or_else(|| "[媒体公告，请查看原文]".into());
            Some(Announcement {
                id,
                published,
                url,
                excerpt,
            })
        })();
        if let Some(item) = item {
            entries.insert(item.id, item);
        } else {
            invalid += 1;
        }
    }
    if entries.is_empty() {
        return Err("未识别到带有效来源和时间的公告，页面可能受限或结构已变化".into());
    }
    let mut entries: Vec<_> = entries.into_values().collect();
    entries.sort_by_key(|item| std::cmp::Reverse((item.published, item.id)));
    Ok(Parsed { entries, invalid })
}

fn clean_excerpt(text: &str) -> String {
    let clean: String = text
        .chars()
        .filter(|c| {
            (!c.is_control() || c.is_whitespace())
                && !matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .collect();
    let clean = clean.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut excerpt: String = clean.chars().take(180).collect();
    if clean.chars().count() > 180 {
        excerpt.push('…');
    }
    excerpt
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(channel: &str, id: u64, date: &str, body: &str) -> String {
        format!(
            r#"<div class="tgme_widget_message" data-post="{channel}/{id}"><div class="tgme_widget_message_text">{body}</div><a class="tgme_widget_message_date" href="https://t.me/{channel}/{id}"><time datetime="{date}"></time></a></div>"#
        )
    }

    #[test]
    fn parses_deduplicates_sorts_and_checks_provenance() {
        let now = "2026-10-16T12:00:00Z".parse().unwrap();
        let first = fixture(
            "hyperliquid_announcements",
            1,
            "2026-10-15T10:00:00Z",
            "A &amp; <b>B</b>",
        );
        let latest = fixture(
            "hyperliquid_announcements",
            2,
            "2026-10-16T10:00:00Z",
            "New<br>update",
        );
        let wrong = fixture("other", 3, "2026-10-16T10:00:00Z", "Wrong source");
        let future = fixture(
            "hyperliquid_announcements",
            4,
            "2027-10-16T10:00:00Z",
            "Future",
        );
        let bad_link = first.replace("href=\"https://t.me/", "href=\"https://example.com/");
        let parsed = parse(
            &format!("{first}{latest}{first}{wrong}{future}{bad_link}"),
            "hyperliquid_announcements",
            now,
        )
        .unwrap();
        assert_eq!(parsed.entries.len(), 2);
        assert_eq!(parsed.entries[0].id, 2);
        assert_eq!(parsed.entries[1].excerpt, "A & B");
        assert_eq!(parsed.invalid, 3);
        assert!(
            parse(
                "<html><title>Login</title></html>",
                "hyperliquid_announcements",
                now
            )
            .is_err()
        );
    }

    #[test]
    fn sanitizes_terminal_controls_and_limits_excerpt() {
        let excerpt = clean_excerpt(&format!("\x1b\u{202e}hello\n{}", "中".repeat(200)));
        assert!(!excerpt.contains('\x1b'));
        assert!(!excerpt.contains('\u{202e}'));
        assert_eq!(excerpt.chars().count(), 181);
    }

    #[tokio::test]
    async fn fetch_rejects_redirects_errors_and_wrong_content() {
        let server = httpmock::MockServer::start_async().await;
        let redirect = server.mock(|when, then| {
            when.path("/redirect");
            then.status(302)
                .header("location", format!("{}/target", server.base_url()));
        });
        let target = server.mock(|when, then| {
            when.path("/target");
            then.status(200);
        });
        server.mock(|when, then| {
            when.path("/json");
            then.status(200)
                .header("content-type", "application/json")
                .body("{}");
        });
        server.mock(|when, then| {
            when.path("/large");
            then.status(200)
                .header("content-type", "text/html")
                .body("x".repeat(MAX_BODY_BYTES + 1));
        });
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        assert!(
            fetch(&client, &format!("{}/redirect", server.base_url()))
                .await
                .unwrap_err()
                .contains("302")
        );
        assert!(
            fetch(&client, &format!("{}/json", server.base_url()))
                .await
                .is_err()
        );
        assert!(
            fetch(&client, &format!("{}/large", server.base_url()))
                .await
                .unwrap_err()
                .contains("大小")
        );
        assert_eq!(redirect.calls(), 1);
        assert_eq!(target.calls(), 0);
    }

    #[tokio::test]
    async fn source_output_keeps_publication_and_fetch_times() {
        let server = httpmock::MockServer::start_async().await;
        let html = fixture(
            "hyperliquid_announcements",
            123,
            "2025-01-01T00:00:00Z",
            "Synthetic update",
        );
        server.mock(|when, then| {
            when.path("/");
            then.status(200)
                .header("content-type", "text/html; charset=utf-8")
                .body(html);
        });
        let client = reqwest::Client::new();
        let body = fetch(&client, &server.base_url()).await.unwrap();
        let parsed = parse(&body, SOURCES[0].channel, Utc::now()).unwrap();
        assert_eq!(
            parsed.entries[0].url,
            "https://t.me/hyperliquid_announcements/123"
        );
        assert_eq!(
            parsed.entries[0].published.to_rfc3339(),
            "2025-01-01T00:00:00+00:00"
        );
        let fetched = "2026-10-16T12:00:00Z".parse().unwrap();
        let text = format_source(&SOURCES[0], Ok(parsed), fetched).join("\n");
        assert!(text.contains("查询时间 2026-10-16T12:00:00Z"));
        assert!(text.contains("2025-01-01 00:00 UTC"));
        assert!(text.contains("请核对原频道的新鲜度"));
        let failed = format_source(&SOURCES[1], Err("HTTP 429".into()), fetched).join("\n");
        assert!(failed.contains("HTTP 429"));
        assert!(failed.contains("不代表没有新公告"));
    }

    #[tokio::test]
    #[ignore = "manual public-channel check; never run in the offline suite"]
    async fn public_sources_smoke() {
        let lines = render().await;
        for line in &lines {
            println!("{line}");
        }
        assert!(
            lines
                .iter()
                .any(|line| line.contains(" | https://t.me/hyperliquid_announcements/"))
        );
    }

    #[test]
    fn dashboard_parse_keeps_missing_time_as_null() {
        let now = "2026-10-16T12:00:00Z".parse().unwrap();
        let with_time = fixture(
            "hyperliquid_announcements",
            2,
            "2026-10-16T10:00:00Z",
            "Latest",
        );
        let no_time = r#"<div class="tgme_widget_message" data-post="hyperliquid_announcements/3"><div class="tgme_widget_message_text">No clock</div><a class="tgme_widget_message_date" href="https://t.me/hyperliquid_announcements/3"></a></div>"#.to_string();
        let wrong = fixture("other", 4, "2026-10-16T10:00:00Z", "Wrong source");
        let future = fixture(
            "hyperliquid_announcements",
            5,
            "2027-10-16T10:00:00Z",
            "Future",
        );
        let parsed = parse_dashboard(
            &format!("{with_time}{no_time}{wrong}{future}"),
            "hyperliquid_announcements",
            now,
        )
        .unwrap();
        assert_eq!(parsed.entries.len(), 3, "未来时间保留但 published 为 null");
        assert_eq!(parsed.entries[0].id, 2, "有时间的排最前");
        assert!(parsed.entries[0].published.is_some());
        let mut tail: Vec<u64> = parsed.entries[1..].iter().map(|e| e.id).collect();
        tail.sort_unstable();
        assert_eq!(tail, vec![3, 5], "缺时间/未来时间的条目排在最后");
        assert!(parsed.entries[1..].iter().all(|e| e.published.is_none()));
        assert_eq!(parsed.missing_time, 2, "缺时间 + 未来时间均记为缺时间");
        assert_eq!(parsed.invalid, 1, "来源不符的丢弃");
        assert!(
            parse_dashboard(
                "<html><title>Login</title></html>",
                "hyperliquid_announcements",
                now
            )
            .is_err()
        );
    }
}
