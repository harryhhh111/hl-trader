-- ============================================================
-- 关注股官方公告流 MVP — 数据存储 schema（第一片）
-- 合同：docs/WATCHLIST_ANNOUNCEMENTS_MVP_PLAN.md §4（最小数据与时间合同）
-- 执行：psql -U postgres -d stock_data -f scripts/watchlist_announcements.sql
--
-- 只新增公告相关对象，幂等，不修改任何已有表。
-- 通用字段保留 market；表单类型、内容性质、来源类型、状态均为开放 text，
-- 不设计封闭枚举，新闻阶段直接扩展取值。
-- 时间统一 TIMESTAMPTZ 存 UTC，同时保留来源原始时间字符串与时区。
--
-- 整轮任务互斥锁（core/announcements/sync.py）逻辑名固定为
--   watchlist_announcements:US
-- 数值 advisory lock key = 0x414E4E4F554E4345（ASCII "ANNOUNCE"），已避让：
-- SEC HTTP gate（core/sec_client.py 0x53454347415445）、market cap 回算
-- （987654321）、财务版本 worker md5 动态 key、快照事务锁（snapshot_id 序列）。
-- 本迁移本身不使用 advisory lock。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 来源注册表 watchlist_announcement_source
--    受控来源注册表：SEC submissions、公司 IR/新闻室，以及未来新闻媒体源。
--    来源 URL 只来自本表，不接受浏览器提交 URL。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_source (
    source_id           BIGSERIAL PRIMARY KEY,
    market              VARCHAR(10) NOT NULL,
    -- 来源类型，开放 text：'sec' | 'ir' | 未来 'media' 等，不做封闭枚举
    source_kind         TEXT NOT NULL,
    -- 平台/站点结构标识（开放 text）：'nebius_newsroom' | 'q4_press_release'
    -- 等。适配器按平台实现，平台识别显式存储、不靠域名猜测；
    -- 新增公司只需新增注册行（换 entry_url），新平台才写新适配器
    platform            TEXT,
    -- 发行人标识（SEC 为 CIK）。行业/媒体等非发行人绑定来源用空字符串占位
    -- （NOT NULL DEFAULT ''，与 check_state 等表惯例一致；NULL 会破坏
    -- 下方 UNIQUE 约束的去重语义——PostgreSQL 中 NULL ≠ NULL）
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    -- 允许域名与固定入口；采集端校验 HTTPS、重定向目标与解析后地址
    allowed_domain      TEXT NOT NULL,
    entry_url           TEXT NOT NULL,
    -- 来源本地时区（IANA 名，如 'America/New_York'），用于解析原始时间
    source_timezone     VARCHAR(50),
    -- 解析器版本；解析逻辑变更时递增，公告与运行记录引用该版本
    parser_version      INTEGER NOT NULL DEFAULT 1,
    -- 单源覆盖检查间隔（分钟）；NULL 表示使用配置默认值
    interval_minutes    INTEGER CHECK (interval_minutes IS NULL OR interval_minutes > 0),
    enabled             BOOLEAN NOT NULL DEFAULT false,
    -- 来源生命周期状态，开放 text：
    -- 'pending_verification' | 'enabled' | 'disabled' | 'blocked'
    source_status       TEXT NOT NULL DEFAULT 'pending_verification',
    -- 受阻/未配置原因，展示给用户，不用空状态掩盖
    status_reason       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market, source_kind, issuer_identifier, entry_url)
);
CREATE INDEX IF NOT EXISTS idx_announcement_source_issuer
    ON watchlist_announcement_source (market, issuer_identifier);

-- 已建库幂等补列（CREATE TABLE IF NOT EXISTS 不会为既有表补新列）
ALTER TABLE watchlist_announcement_source ADD COLUMN IF NOT EXISTS platform TEXT;

-- ------------------------------------------------------------
-- 2. 公告 + 时间与版本字段 watchlist_announcement
--    每条公告一行；同一 source_record_id 发生变更时插入更高 revision
--    的不可变新行，不覆盖旧证据。/A 修订有独立 accession，天然独立成行。
--    证券关联在 watchlist_announcement_security 独立表中（一对多），
--    本表不携带单一 stock_code。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement (
    id                  BIGSERIAL PRIMARY KEY,
    market              VARCHAR(10) NOT NULL,
    -- 发行人标识（SEC 为 CIK）；媒体等非发行人绑定来源用空字符串占位
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    source_id           BIGINT NOT NULL REFERENCES watchlist_announcement_source(source_id),
    -- 来源稳定身份：SEC 为 accession number；IR 为稳定发布 ID 或规范化 URL。
    -- 与 source_id、revision 构成唯一键，幂等去重。
    source_record_id    TEXT NOT NULL,
    title               TEXT NOT NULL,
    -- 表单类型，开放 text：'8-K' | '10-Q' | 'press_release' 等，
    -- 新闻阶段扩展取值，不做封闭枚举。SEC 使用中性表单描述，不伪造新闻标题。
    form_type           TEXT,
    original_url        TEXT NOT NULL,

    -- ── 时间与版本 ──
    -- 来源发布时间（UTC）。可为空：缺失发布时间的记录单独标注，
    -- 不补午夜伪装精确时间，不按发现时间冒充。
    published_at        TIMESTAMPTZ,
    -- 时间精度，开放 text：'second' | 'minute' | 'day' | 'unknown'；
    -- 'day' 表示仅日期，前端显示"仅日期"
    published_at_precision TEXT NOT NULL DEFAULT 'unknown',
    -- 来源原始时间字符串与时区，原样保留
    source_published_raw   TEXT,
    source_timezone        VARCHAR(50),
    -- 系统实际首次发现时间；90 天回补也不倒填成发布日期，
    -- 重复抓取不改变该值（由写入端 ON CONFLICT DO NOTHING 保证）
    first_observed_at   TIMESTAMPTZ NOT NULL,
    -- 最近一次抓取到该条目的时间
    last_seen_at        TIMESTAMPTZ NOT NULL,
    -- 本条记录入库时间
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 内容摘要哈希（变更检测）与必要短摘录；
    -- 不默认永久存储完整受版权保护网页
    content_hash        TEXT,
    excerpt             TEXT,
    -- 修订版本，从 1 开始；相同发布变更时追加更高版本行
    revision            INTEGER NOT NULL DEFAULT 1,
    -- 历史回补条目标记，避免被误认为刚发生的事件
    is_backfill         BOOLEAN NOT NULL DEFAULT false,
    -- 内容性质（报道/评论/传闻区分；新闻阶段加列）：v1 媒体条目一律
    -- 'unclassified'，UI 显示「内容性质未分类」，不伪造为「报道」；
    -- 官方披露行不受此列约束（NULL）
    content_nature      TEXT,
    -- 关联理由（新闻阶段加列）：机器可核对，如 'ticker 直连：Finnhub symbol MU'
    -- 行业主题层扩展时同列承载主题关联依据
    relation_reason     TEXT,
    -- 媒体名（Reuters/Yahoo/…，媒体条目；官方披露为 NULL，来源名取注册表）
    media_source        TEXT,
    -- LLM 增强（每日资讯 v1.1）：中文标题/摘要/重要性。NULL=未判断或
    -- 判断失败（存量不回填）。官方披露只算 importance，不出中文标题/摘要
    title_zh            TEXT,
    summary_zh          TEXT,
    importance          TEXT,

    UNIQUE (source_id, source_record_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_announcement_issuer
    ON watchlist_announcement (market, issuer_identifier, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_announcement_first_observed
    ON watchlist_announcement (first_observed_at);

-- ------------------------------------------------------------
-- 2b. 公告—证券关联 watchlist_announcement_security（一对多）
--     同一公告（同一 source_record_id）可关联多个证券代码；关联挂在
--     该公告的首个版本行（revision 最小的行）上，后续修订版本通过共享
--     (source_id, source_record_id) 继承同一组关联。
--     无本机 stock_info 外键（同 watchlist_item 先例：允许元数据实例
--     保存其他市场的代码），不依赖会被删除的关注项 ID。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_security (
    -- 关联锚定的公告行（该逻辑公告的首个版本）
    announcement_id     BIGINT NOT NULL REFERENCES watchlist_announcement(id),
    market              VARCHAR(10) NOT NULL,
    stock_code          VARCHAR(20) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    relation_reason     TEXT,
    -- 重复抓取不产生重复关联（写入端 ON CONFLICT DO NOTHING）
    PRIMARY KEY (announcement_id, market, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_announcement_security_stock
    ON watchlist_announcement_security (market, stock_code);

-- ------------------------------------------------------------
-- 3. 来源检查状态 watchlist_announcement_check_state
--    按 (market, issuer_identifier, source_id) 保存。
--    checked_through 是完整检查边界，不是最新公告时间；
--    无新公告的完整检查同样推进。恢复游标不能代替该检查点。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_check_state (
    market              VARCHAR(10) NOT NULL,
    -- 非发行人绑定来源用空字符串占位，保证主键非空
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    source_id           BIGINT NOT NULL REFERENCES watchlist_announcement_source(source_id),
    -- 实际生效检查间隔（分钟）：单源覆盖值或配置默认值，展示实际生效值
    effective_interval_minutes INTEGER NOT NULL CHECK (effective_interval_minutes > 0),
    -- 最近一次尝试时间与状态（区分"最近一次尝试"和"最近一次完整成功"）
    last_attempt_at     TIMESTAMPTZ,
    -- 开放 text：'success_empty' | 'success_new' | 'failed'
    --          | 'not_configured' | 'partial' | 'skipped_running'
    last_attempt_status TEXT,
    -- 最近一次完整成功检查时间；失败或 partial 不推进
    last_success_at     TIMESTAMPTZ,
    -- 下次到期时间；skipped_running 不更新到期时间与成功检查点
    next_due_at         TIMESTAMPTZ,
    -- 完整检查点：仅在目标区间全部获取、解析、入库成功后，
    -- 与最终数据在同一事务中推进；失败/partial 保留旧值
    checked_through     TIMESTAMPTZ,
    -- 已完整覆盖的时间区间（左闭右开），写入端合并连续区间；
    -- 覆盖结论仅限已配置表单类型与来源可枚举范围
    covered_ranges      TSTZRANGE[] NOT NULL DEFAULT '{}',
    -- 失败原因类别（开放 text，如 'http_403' | 'parse_error' | 'budget_exhausted'）
    last_error_class    TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, issuer_identifier, source_id)
);

-- ------------------------------------------------------------
-- 3b. 未补齐缺口 watchlist_announcement_gap（独立行，持久表达）
--     历史不可访问等原因造成的缺口逐行记录；检查点不得跨过缺口
--     宣称完整。补齐后写 resolved_at 并合并 covered_ranges，不删行。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_gap (
    id                  BIGSERIAL PRIMARY KEY,
    market              VARCHAR(10) NOT NULL,
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    source_id           BIGINT NOT NULL REFERENCES watchlist_announcement_source(source_id),
    gap_range           TSTZRANGE NOT NULL,
    -- 缺口原因，开放 text：'history_unavailable' | 'fetch_failed'
    --                    | 'budget_exhausted' 等
    reason              TEXT NOT NULL,
    detail              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULL 表示尚未补齐
    resolved_at         TIMESTAMPTZ
);
-- 同一来源同一区间同一原因的未解决缺口只保留一行
CREATE UNIQUE INDEX IF NOT EXISTS idx_announcement_gap_open
    ON watchlist_announcement_gap (market, issuer_identifier, source_id, gap_range, reason)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_announcement_gap_source
    ON watchlist_announcement_gap (source_id) WHERE resolved_at IS NULL;

-- ------------------------------------------------------------
-- 4. 采集运行记录 watchlist_announcement_run
--    按来源粒度记录；同一整轮（定时/手工/恢复入口）共享 task_id。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_run (
    id                  BIGSERIAL PRIMARY KEY,
    -- 整轮任务 ID（采集进程生成的 UUID）；跨来源归属同一轮
    task_id             UUID NOT NULL,
    market              VARCHAR(10) NOT NULL,
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    source_id           BIGINT REFERENCES watchlist_announcement_source(source_id),
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    -- 开放 text：'success' | 'success_empty' | 'failed' | 'partial'
    --          | 'skipped_running'（整轮锁被占用，未发起采集）
    status              TEXT NOT NULL,
    -- 获取/新增/修订数量；失败或 skipped 时保持 NULL，
    -- 不伪造为 0 或正常结果
    fetched_count       INTEGER,
    inserted_count      INTEGER,
    revised_count       INTEGER,
    -- 错误类别（开放 text）与详情；成功时为 NULL
    error_class         TEXT,
    error_detail        TEXT,
    -- 本轮使用的解析器版本
    parser_version      INTEGER,
    -- 过滤丢弃计数（媒体源相关率过滤）；可见、不静默
    dropped_count       INTEGER,
    -- 触发入口，开放 text：'scheduled' | 'manual' | 'recovery'
    trigger_kind        TEXT
);
CREATE INDEX IF NOT EXISTS idx_announcement_run_task
    ON watchlist_announcement_run (task_id);
CREATE INDEX IF NOT EXISTS idx_announcement_run_source
    ON watchlist_announcement_run (market, issuer_identifier, source_id, started_at DESC);

-- ------------------------------------------------------------
-- 5. 恢复进度 watchlist_announcement_recovery
--    回补/中断恢复进度。backfill_start 在回补开始时固定，
--    跨轮恢复不随日期滚动。本表不能代替 check_state.checked_through：
--    完整检查点只在目标区间整体成功提交后推进。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist_announcement_recovery (
    market              VARCHAR(10) NOT NULL,
    issuer_identifier   VARCHAR(40) NOT NULL DEFAULT '',
    source_id           BIGINT NOT NULL REFERENCES watchlist_announcement_source(source_id),
    -- 固定回补起点与目标截止时间（首次回补开始时固定）
    backfill_start      TIMESTAMPTZ NOT NULL,
    target_through      TIMESTAMPTZ NOT NULL,
    -- 已完成/未完成区间（左闭右开）；下轮优先恢复 pending_ranges
    completed_ranges    TSTZRANGE[] NOT NULL DEFAULT '{}',
    pending_ranges      TSTZRANGE[] NOT NULL DEFAULT '{}',
    -- 分页游标及其适用性：仅来源支持稳定游标时 cursor_applicable=true
    -- 才可续页，否则重新扫描并幂等去重；游标不代表完整检查成功
    cursor              TEXT,
    cursor_applicable   BOOLEAN NOT NULL DEFAULT false,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, issuer_identifier, source_id)
);

-- ============================================================
-- 每日资讯 MVP（公司新闻层）加列：媒体来源查询配置（JSON：公司名/别名
-- 查询词、symbol 等）；公告内容性质与关联理由；运行记录丢弃计数
ALTER TABLE watchlist_announcement_source ADD COLUMN IF NOT EXISTS query_config TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS content_nature TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS relation_reason TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS media_source TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS title_zh TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS summary_zh TEXT;
ALTER TABLE watchlist_announcement ADD COLUMN IF NOT EXISTS importance TEXT;
ALTER TABLE watchlist_announcement_run ADD COLUMN IF NOT EXISTS dropped_count INTEGER;
