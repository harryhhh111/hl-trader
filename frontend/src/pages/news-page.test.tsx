import assert from 'node:assert/strict';
import test from 'node:test';

import { renderToString } from 'react-dom/server';

import { dayHeader, NewsCard, NewsRow, PinnedRow } from './news-page';
import type { NewsEntry } from '@/lib/types/news';


test('日期分组星期不受浏览器本地时区影响', () => {
  const previous = process.env.TZ;
  try {
    for (const timezone of ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']) {
      process.env.TZ = timezone;
      assert.equal(
        dayHeader('2026-10-26', 2),
        '2026-10-26 星期一 · 2 条（北京时间）',
        timezone,
      );
    }
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
});

const multiCompanyEntry: NewsEntry = {
  title: 'Shared story', url: 'https://example.com/story',
  source_kind: 'media', platform: 'google_news_rss', media_source: 'Reuters',
  form_type: 'news', content_nature: 'unclassified',
  stock_code: 'AAA', stock_codes: ['AAA', 'MU', 'WDC', 'Z9'],
  stock_relations: [
    {stock_code: 'AAA', reasons: []},
    {stock_code: 'MU', reasons: [{
      text: 'Google 查询词 Micron', source_id: 1,
      source_record_id: 'mu-story', url: 'https://example.com/mu',
    }]},
    {stock_code: 'WDC', reasons: [{
      text: 'Finnhub symbol WDC', source_id: 2,
      source_record_id: 'wdc-story', url: 'https://example.com/wdc',
    }]},
    {stock_code: 'Z9', reasons: []},
  ],
  relation_reason: null, excerpt: null,
  published_at: '2026-10-27T01:00:00+00:00', published_precision: 'second',
  published_raw: '2026-10-27T01:00:00Z', source_timezone: 'UTC',
  time_unknown: false, date_only: false,
  first_observed_at: '2026-10-27T01:01:00+00:00', is_backfill: false,
  bj_day: '2026-10-27', fold_count: 1, fold_sources: [],
  title_zh: null, summary_zh: null, importance: 'high',
};


test('信息流和置顶折叠公司标签，详情完整展示公司及理由', () => {
  const stripComments = (html: string) => html.replace(/<!-- -->/g, '');
  const row = stripComments(renderToString(
    <NewsRow rank={1} entry={multiCompanyEntry} selectedStock="WDC" />));
  const pinned = stripComments(renderToString(<PinnedRow rank={1} entry={multiCompanyEntry} />));
  for (const html of [row, pinned]) {
    assert.match(html, /关联关注股/);
    assert.match(html, /\+1/);
    assert.doesNotMatch(html, /\[Z9\]/);
    assert.match(html, /type="button"/);
    assert.match(html, /aria-expanded="false"/);
  }
  assert.ok(row.indexOf('[WDC]') < row.indexOf('[MU]'));
  const detail = stripComments(renderToString(<NewsCard entry={multiCompanyEntry} />));
  for (const code of ['AAA', 'MU', 'WDC', 'Z9']) {
    assert.match(detail, new RegExp(`\\[${code}\\]`));
  }
  assert.doesNotMatch(detail, /\+1/);
  assert.match(detail, /Google 查询词 Micron/);
  assert.match(detail, /Finnhub symbol WDC/);
  assert.match(detail, /href="https:\/\/example.com\/mu"/);
  assert.match(detail, /href="https:\/\/example.com\/wdc"/);
});
