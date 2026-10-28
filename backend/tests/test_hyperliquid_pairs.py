"""v2 多 pair 目录接口：配置校验与边界，无外部进程、无交易所查询。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_pairs as pairs

ADDRESS = "0x7F2120F612B78D579197762196B8A202AB654096"
ADDRESS_LOWER = ADDRESS.lower()

VALID = {
    "config_version": 1,
    "pairs": [
        {"id": "hype-btc", "label": "HYPE / BTC", "enabled": True, "network": "mainnet",
         "dex": "", "coins": ["HYPE", "BTC"], "account_address": None, "unlock_anchors": []},
        {"id": "nbis-crwv", "label": "NBIS / CRWV", "enabled": True, "network": "mainnet",
         "dex": "xyz", "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": ADDRESS,
         "unlock_anchors": None},
    ],
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    config_file = tmp_path / 'hl-pairs.json'
    monkeypatch.setenv('HL_APP_PAIRS_CONFIG', str(config_file))
    pairs.clear_cache()
    pairs.clear_runtime_cache()
    app = FastAPI()
    app.include_router(hyperliquid.router, prefix='/api/v1')
    app.dependency_overrides[require_session] = lambda: None
    yield TestClient(app), config_file
    pairs.clear_cache()
    pairs.clear_runtime_cache()


def write_config(client, data=None, raw=None):
    _, config_file = client
    if raw is not None:
        config_file.write_text(raw, encoding='utf-8')
    else:
        config_file.write_text(json.dumps(data or VALID), encoding='utf-8')
    pairs.clear_cache()


def test_directory_returns_enabled_pairs_with_revision(client):
    write_config(client)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 200
    body = response.json()
    data = body['data']
    assert data['schema_version'] == 2
    assert len(data['config_revision']) == 64
    hype, nbis = data['pairs']
    assert hype['id'] == 'hype-btc' and hype['account_address'] is None
    assert hype['unlocks_enabled'] is True
    assert nbis['account_address'] == ADDRESS_LOWER  # 大写输入归一化
    assert nbis['unlocks_enabled'] is False


def test_disabled_pair_excluded_from_directory(client):
    data = json.loads(json.dumps(VALID))
    data['pairs'][0]['enabled'] = False
    write_config(client, data)
    body = client[0].get('/api/v1/hyperliquid/pairs').json()['data']
    assert [p['id'] for p in body['pairs']] == ['nbis-crwv']


def test_revision_tracks_config_content(client):
    write_config(client)
    first = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    write_config(client)  # 同内容再写：revision 相同
    again = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    assert first == again
    data = json.loads(json.dumps(VALID))
    data['pairs'][1]['account_address'] = None
    write_config(client, data)
    changed = client[0].get('/api/v1/hyperliquid/pairs').json()['data']['config_revision']
    assert changed != first


@pytest.mark.parametrize('raw', [
    '{"config_version": 1, "config_version": 1, "pairs": []}',          # 重复 JSON 键
])
def test_duplicate_json_key_is_503(client, raw):
    write_config(client, raw=raw)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Hyperliquid 多 pair 配置不可用'


@pytest.mark.parametrize('mutate,match', [
    (lambda d: d.update(config_version=2), 'config_version'),
    (lambda d: d.pop('pairs'), '缺少字段'),
    (lambda d: d.update(extra=1), '未知字段'),
    (lambda d: d['pairs'][0].update(id='HYPE/BTC'), 'id 非法'),
    (lambda d: d['pairs'][0].update(id='nbis-crwv'), '重复 pair id'),
    (lambda d: d['pairs'][0].pop('enabled'), '缺少字段'),
    (lambda d: d['pairs'][0].update(enabled='yes'), '布尔'),
    (lambda d: d['pairs'][0].update(network='devnet'), 'network'),
    (lambda d: d['pairs'][1]['coins'].__setitem__(0, 'NBIS'), '形式'),
    (lambda d: d['pairs'][1]['coins'].__setitem__(1, 'xyz:NBIS'), '必须不同'),
    (lambda d: d['pairs'][1]['coins'].append('xyz:HYPE'), '恰好两个'),
    (lambda d: d['pairs'][0]['coins'].__setitem__(0, 'xyz:HYPE'), '冒号'),
    (lambda d: d['pairs'][0].update(account_address=''), 'account_address 非法'),
    (lambda d: d['pairs'][0].update(account_address='0xZZZ'), 'account_address 非法'),
    (lambda d: d['pairs'][0].update(account_address='0x' + '0' * 40), '零地址'),
    (lambda d: d['pairs'][0].update(unlock_anchors=[{'date_utc': '2026-10-20'}]), '字段'),
    (lambda d: d['pairs'][0].update(unlock_anchors=[{'date_utc': '10/20', 'label': 'x',
                                                      'source_url': 'https://a', 'verified_utc': '2026-10-18'}]), 'YYYY'),
])
def test_invalid_config_is_503_generic_detail(client, mutate, match):
    import copy
    data = copy.deepcopy(VALID)
    mutate(data)
    write_config(client, data)
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert response.json()['detail'] == 'Hyperliquid 多 pair 配置不可用'


def test_missing_env_var_is_specific_503(client, monkeypatch):
    monkeypatch.delenv('HL_APP_PAIRS_CONFIG')
    response = client[0].get('/api/v1/hyperliquid/pairs')
    assert response.status_code == 503
    assert 'HL_APP_PAIRS_CONFIG' in response.json()['detail']


def test_valid_anchor_passes(client):
    import copy
    data = copy.deepcopy(VALID)
    data['pairs'][0]['unlock_anchors'] = [
        {'date_utc': '2026-10-20', 'label': '团队份额解锁',
         'source_url': 'https://example.com', 'verified_utc': '2026-10-18'},
    ]
    write_config(client, data)
    body = client[0].get('/api/v1/hyperliquid/pairs').json()['data']
    assert body['pairs'][0]['unlocks_enabled'] is True


def test_v1_dashboard_is_not_registered(client):
    write_config(client)
    response = client[0].get('/api/v1/hyperliquid/dashboard')
    assert response.status_code == 404


# ── Stage B：v2 快照与公告 ────────────────────────────────

TS = "2026-10-19T08:00:00Z"


def v2_block(name, data, status="ok", diags=None):
    sources = {key: {"status": "ok", "queried_at": TS} for key in pairs.BLOCK_SOURCE_KEYS[name]}
    return {"status": status, "queried_at": TS,
            "source_queries": sources,
            "diagnostics": diags or [], **data}


def descriptor_of(client, pair_id):
    _revision, descriptors = pairs.load_pairs()
    return next(d for d in descriptors if d["id"] == pair_id)


def v2_snapshot(descriptor, revision, mutate=None):
    coins = descriptor["coins"]
    snapshot = {
        "schema_version": 2, "config_revision": revision, "queried_at": TS,
        "pair": {k: descriptor[k] for k in ("id", "label", "network", "dex", "coins", "account_address")},
        "blocks": {
            "account": v2_block("account", {"account_mode": "default", "equity": "1000.5", "equity_scope": "perp_dex"}),
            "positions": v2_block("positions", {"items": [], "position_state": "flat", "unrealized_pnl": "0",
                                                "notional_ratio": None, "ratio_drift_pct": None, "open_orders": 0}),
            "funding": v2_block("funding", {"hourly_rates": {c: "0.00001" for c in coins},
                                            "annualized_pct": {c: "8.76" for c in coins},
                                            "daily_estimate": None,
                                            "window_start": "2026-10-17T08:00:00Z",
                                            "window_end": "2026-10-19T08:00:00Z",
                                            "window_complete": True, "actual_48h_net": "0",
                                            "recent_payments": []}),
            "ratio": v2_block("ratio", {"live": "0.0005", "latest_close": "0.00049", "ma30": "0.0005",
                                        "vs_ma30_pct": "-2.0", "as_of_date": "2026-10-18", "points": []}),
            "unlocks": None if not descriptor["unlocks_enabled"] else v2_block("unlocks", {"anchors": [], "note": "n"}),
        },
    }
    if mutate:
        mutate(snapshot)
    return snapshot


@pytest.fixture
def runner(monkeypatch):
    calls = []

    def install(func):
        def fake(collect_argv):
            calls.append(list(collect_argv))
            return func(collect_argv)
        monkeypatch.setattr(pairs, '_run_dashboard', fake)
        return calls
    return install


def test_pair_dashboard_happy_path_and_cache(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')
    calls = runner(lambda argv: v2_snapshot(nbis, revision))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')

    first = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    assert first.status_code == 200
    body = first.json()['data']
    assert body['schema_version'] == 2
    assert calls == [["--pair", "nbis-crwv"]]

    second = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    assert second.status_code == 200
    assert len(calls) == 1  # 缓存命中，不再起子进程


def test_unknown_or_disabled_pair_is_404(client, runner, monkeypatch):
    write_config(client)
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    revision, _ = pairs.load_pairs()

    def respond(argv):
        return v2_snapshot(descriptor_of(client, argv[1]), revision)
    calls = runner(respond)
    assert client[0].get('/api/v1/hyperliquid/pairs/nope/dashboard').status_code == 404
    assert client[0].get('/api/v1/hyperliquid/pairs/hype-btc/dashboard').status_code == 200
    assert calls == [["--pair", "hype-btc"]]
    data = json.loads(json.dumps(VALID))
    data['pairs'][1]['enabled'] = False
    write_config(client, data)
    assert client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').status_code == 404


@pytest.mark.parametrize('mutate,expect_detail', [
    (lambda s: s.update(schema_version=1), '快照版本不受支持'),
    (lambda s: s.update(config_revision='0' * 64), '配置已变更，请重试'),
    (lambda s: s['pair'].update(account_address='0x' + '9' * 40), '快照身份与配置不符'),
    (lambda s: s['pair'].update(network='testnet'), '快照身份与配置不符'),
])
def test_identity_and_revision_mismatch_is_503(client, monkeypatch, runner, mutate, expect_detail):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')
    calls = runner(lambda argv: v2_snapshot(nbis, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    response = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    assert response.status_code == 503
    assert response.json()['detail'] == expect_detail
    assert calls == [["--pair", "nbis-crwv"]]  # 失败不进缓存，但本次确实发起了采集


def test_failed_validation_not_cached(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')
    calls = runner(lambda argv: v2_snapshot(nbis, '9' * 64))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    assert client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').status_code == 503
    calls.clear()
    runner(lambda argv: v2_snapshot(nbis, revision))
    assert client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').status_code == 200


def test_bad_decimal_block_becomes_unavailable(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')

    def mutate(s):
        s['blocks']['funding']['hourly_rates']['xyz:NBIS'] = 'NaN'
    runner(lambda argv: v2_snapshot(nbis, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    body = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']
    funding = body['blocks']['funding']
    assert funding['status'] == 'unavailable'
    assert funding['diagnostics']
    assert body['blocks']['ratio']['status'] == 'ok'


def test_null_items_preserved_not_coerced(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')
    runner(lambda argv: v2_snapshot(nbis, revision, lambda s: s['blocks']['positions'].__setitem__('items', None)))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    positions = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']['blocks']['positions']
    assert positions['status'] == 'ok'
    assert positions['items'] is None


def test_all_applicable_unavailable_is_503(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')

    def mutate(s):
        for name in ('account', 'positions', 'funding', 'ratio'):
            s['blocks'][name] = v2_block(name, {
                "items" if name == 'positions' else "equity": None,
            }, status="unavailable", diags=["源不可用"])
    runner(lambda argv: v2_snapshot(nbis, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    response = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    assert response.status_code == 503
    assert response.json()['detail'] == '看板数据暂不可用'


def test_unlocks_block_semantics(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()

    def respond(argv):
        descriptor = descriptor_of(client, argv[1])
        snap = v2_snapshot(descriptor, revision)
        if descriptor['unlocks_enabled']:
            snap['blocks']['unlocks'] = None  # 启用时返回 null：契约违反，规范为 unavailable
        return snap

    runner(respond)
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    hype_body = client[0].get('/api/v1/hyperliquid/pairs/hype-btc/dashboard').json()['data']
    assert hype_body['blocks']['unlocks']['status'] == 'unavailable'  # null 被规范化
    nbis_body = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']
    assert nbis_body['blocks']['unlocks'] is None  # 未启用解锁时保持 null


def test_announcements_endpoint(client, monkeypatch, runner):
    write_config(client)
    payload = {
        "schema_version": 2, "queried_at": TS,
        "block": v2_block("announcements", {"items": None}),
    }
    calls = runner(lambda argv: payload)
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    response = client[0].get('/api/v1/hyperliquid/announcements')
    assert response.status_code == 200
    body = response.json()['data']
    assert body['block']['items'] is None  # null 不伪装成空数组
    assert calls == [["--announcements"]]


def test_internal_error_not_exposed(client, monkeypatch, runner):
    write_config(client)

    def boom(argv):
        from hl_app.services.process import HyperliquidUnavailable
        raise HyperliquidUnavailable('internal /home/vinci/secret detail')
    runner(boom)
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    response = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard')
    assert response.status_code == 503
    assert 'secret' not in response.json()['detail']
    assert response.json()['detail'] == 'Hyperliquid pair 看板暂不可用'


# ── 整改 B1（R4/R9）：降级 DTO 完整性与语义校验 ──────────────

def test_degraded_block_has_full_dto_and_fixed_sources(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')

    def mutate(s):
        del s['blocks']['funding']  # 缺整块：必须降级为完整 DTO
    runner(lambda argv: v2_snapshot(nbis, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    funding = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']['blocks']['funding']
    assert funding['status'] == 'unavailable'
    assert set(funding['source_queries']) == {'positions', 'market', 'user_funding'}
    assert all(q == {'status': 'unavailable', 'queried_at': None} for q in funding['source_queries'].values())
    assert funding['hourly_rates'] == {'xyz:NBIS': None, 'xyz:CRWV': None}
    assert funding['annualized_pct'] == {'xyz:NBIS': None, 'xyz:CRWV': None}
    assert funding['window_start'] is None and funding['window_end'] is None
    assert funding['window_complete'] is False
    assert funding['recent_payments'] is None and funding['actual_48h_net'] is None
    positions = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']['blocks']['positions']
    assert positions['status'] == 'ok'  # 其他块不受影响


@pytest.mark.parametrize('block_name,mutate,reason_part', [
    ('account', lambda s: s['blocks']['account'].__setitem__('diagnostics', ['失败但状态 ok']), '结构非法'),
    ('account', lambda s: s['blocks']['account'].__setitem__('diagnostics', [42]), '结构非法'),
    ('account', lambda s: s['blocks']['account'].pop('equity'), '缺少字段'),
    ('account', lambda s: s['blocks']['account']['source_queries']['mode'].__setitem__(
        'queried_at', None), '结构非法'),
    ('positions', lambda s: s['blocks']['positions'].pop('items'), '缺少字段'),
    ('ratio', lambda s: s['blocks']['ratio'].__setitem__('source_queries',
                                                        {'market': {'status': 'ok', 'queried_at': TS}}), '结构非法'),
    ('ratio', lambda s: s['blocks']['ratio'].__setitem__('points', {}), '必须是数组'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('window_start', '2026-10-19T09:00:00Z'), '反向'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('window_end', '2026-10-18T09:00:00Z'), '恰好 48 小时'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('window_complete', False), '不能为 ok'),
    ('funding', lambda s: (s['blocks']['funding'].__setitem__('actual_48h_net', None),
                           s['blocks']['funding'].__setitem__('recent_payments', None)), '完整资金费历史'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('recent_payments', None), '同时已知'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('recent_payments', [
        {'time': '2026-10-16T07:59:59Z', 'coin': 'xyz:NBIS', 'usdc': '1', 'rate': '0.1'}]), '不在窗口内'),
    ('funding', lambda s: s['blocks']['funding'].__setitem__('recent_payments', [
        {'time': '2026-10-18T04:00:00Z', 'coin': 'xyz:NBIS', 'usdc': '1', 'rate': '0.1'},
        {'time': '2026-10-18T08:00:00Z', 'coin': 'xyz:NBIS', 'usdc': '2', 'rate': '0.1'}]), '倒序'),
    ('unlocks', lambda s: s['blocks']['unlocks'].__setitem__('anchors', {}), '必须是数组'),
])
def test_invalid_block_semantics_degraded(client, monkeypatch, runner, block_name, mutate, reason_part):
    write_config(client)
    revision, _ = pairs.load_pairs()
    pair_id = 'hype-btc' if block_name == 'unlocks' else 'nbis-crwv'
    descriptor = descriptor_of(client, pair_id)
    runner(lambda argv: v2_snapshot(descriptor, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    body = client[0].get(f'/api/v1/hyperliquid/pairs/{pair_id}/dashboard').json()['data']
    block = body['blocks'][block_name]
    assert block['status'] == 'unavailable'
    assert any(reason_part in d for d in block['diagnostics'])


def test_disabled_pair_forces_unlocks_null_even_when_rust_sends_block(client, monkeypatch, runner):
    write_config(client)
    revision, _ = pairs.load_pairs()
    nbis = descriptor_of(client, 'nbis-crwv')

    def mutate(s):
        s['blocks']['unlocks'] = v2_block('unlocks', {'anchors': [{'date': '2026-10-20', 'days_ahead': 1,
                                                                  'label': 'x', 'source_url': 'https://a',
                                                                  'verified_utc': '2026-10-18'}], 'note': 'n'})
    runner(lambda argv: v2_snapshot(nbis, revision, mutate))
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    body = client[0].get('/api/v1/hyperliquid/pairs/nbis-crwv/dashboard').json()['data']
    assert body['blocks']['unlocks'] is None  # unlocks_enabled=false 不用错误对象替代


def test_announcements_rejects_object_items(client, monkeypatch, runner):
    write_config(client)
    payload = {
        "schema_version": 2, "queried_at": TS,
        "block": v2_block("announcements", {"items": {}}),
    }
    runner(lambda argv: payload)
    monkeypatch.setenv('HL_APP_TRADER_BIN', '/bin/hl')
    response = client[0].get('/api/v1/hyperliquid/announcements')
    assert response.status_code == 503
    assert response.json()['detail'] == '快照结构非法'
