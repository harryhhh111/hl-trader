"""Regression cases from the independent company research acceptance review."""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from hl_app.services import research_reports as reports
from .test_research_reports import library, snapshot_root, _client, _load_index, _write_index


def replace_main(library, text, assets=(), unpublished=None):
    path = library / 'MSFT/MSFT.html'
    path.write_text(text, encoding='utf-8')
    document = _load_index(library)
    document['reports'][0].update(assets=list(assets), unpublished_links=unpublished or {})
    _write_index(library, document)
    return path.read_bytes()


def test_new_snapshot_has_traversable_directories_and_readable_files(library, tmp_path):
    output = tmp_path / 'output'
    manifest = reports.build_snapshot(library, output)
    for path in (output, output / 'snapshots', *list((output / 'snapshots').rglob('*'))):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o444 == 0o444
        if path.is_dir():
            assert mode & 0o111 == 0o111
    assert (output / 'snapshots' / manifest['snapshot_id'] / 'manifest.json').is_file()


def test_permission_errors_return_503_without_server_paths(snapshot_root, monkeypatch):
    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == 'manifest.json':
            raise PermissionError(13, 'permission denied', '/private/local/manifest.json')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', denied)
    response = _client(snapshot_root).get('/api/v1/research/companies')
    assert response.status_code == 503
    assert '/private/' not in response.text


def test_identity_updates_replace_the_served_metadata(library, tmp_path):
    output = tmp_path / 'output'
    first = reports.build_snapshot(library, output)
    path = library / 'MSFT/research.yaml'
    for name in ('Microsoft Second', 'Microsoft Third'):
        path.write_text(path.read_text().replace('Microsoft', name, 1))
        second = reports.build_snapshot(library, output)
        assert first['snapshot_id'] != second['snapshot_id']
        loaded = reports.load_snapshot(output)
        assert loaded.manifest.to_dict() == second
        company = next(c for c in loaded.manifest.companies if c.entity_id == 'company:microsoft')
        assert company.name.startswith(name)
        first = second


def test_identity_changes_during_build_do_not_switch_pointer(library, tmp_path, monkeypatch):
    output = tmp_path / 'output'
    reports.build_snapshot(library, output)
    pointer = (output / 'current.json').read_bytes()
    original = reports._relax_directory_tree

    def changed(path):
        original(path)
        if path.name.startswith('.staging-'):
            (library / 'MSFT/research.yaml').write_text('entity_id: company:microsoft\nname: Changed\n')

    monkeypatch.setattr(reports, '_relax_directory_tree', changed)
    with pytest.raises(reports.ResearchReportError):
        reports.build_snapshot(library, output)
    assert (output / 'current.json').read_bytes() == pointer


def test_registered_hash_mismatch_excludes_report(library, tmp_path):
    document = _load_index(library)
    document['reports'][0]['content_sha256'] = '0' * 64
    _write_index(library, document)
    manifest = reports.build_snapshot(library, tmp_path / 'output')
    assert 'msft-main' not in manifest['reports']
    assert any(x['report_id'] == 'msft-main' and '哈希' in x['reason'] for x in manifest['excluded'])


def test_ready_requires_registered_hash(library, tmp_path):
    document = _load_index(library)
    document['reports'][0].update(status='ready', content_sha256=None)
    _write_index(library, document)
    with pytest.raises(reports.ResearchIndexError, match='哈希'):
        reports.build_snapshot(library, tmp_path / 'output')


@pytest.mark.parametrize('exists,unpublished', [(False, True), (True, False)])
def test_unpublished_or_unapproved_required_image_blocks_report(library, tmp_path, exists, unpublished):
    if exists:
        (library / 'MSFT/chart.png').write_bytes(b'image')
    replace_main(library, '<img src="chart.png">', unpublished={'chart.png': '未发布'} if unpublished else {})
    manifest = reports.build_snapshot(library, tmp_path / 'output')
    assert 'msft-main' not in manifest['reports']
    assert 'qxo-main' in manifest['reports']


def test_recursive_styles_images_and_svg_are_embedded_and_original_is_preserved(library, tmp_path):
    assets = ['MSFT/style.css', 'MSFT/nested.css', 'MSFT/chart.svg', 'MSFT/chart.png']
    (library / assets[0]).write_text('@import "nested.css" screen; p {color:rgb(1,2,3)}')
    (library / assets[1]).write_text('body{background:url(chart.png)}')
    (library / assets[2]).write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><image href="chart.png"/></svg>')
    (library / assets[3]).write_bytes(b'fixture-image')
    original = replace_main(library, '<link rel="stylesheet" href=style.css><p>中文</p><img src="chart.svg"><script>alert(1)</script>', assets)
    output = tmp_path / 'output'
    manifest = reports.build_snapshot(library, output)
    payload, _ = reports.snapshot_content_bytes(output, manifest['snapshot_id'], 'MSFT/MSFT.html')
    assert '中文' in payload.decode()
    assert b'@media screen' in payload
    assert b'data:image/png;base64,' in payload and b'data:image/svg+xml;base64,' in payload
    assert b'<script' not in payload and b'<link' not in payload
    assert (output / 'snapshots' / manifest['snapshot_id'] / 'originals/MSFT/MSFT.html').read_bytes() == original
    assert hashlib.sha256(payload).hexdigest() == manifest['reports']['msft-main']['display_sha256']
    assert (library / 'MSFT/MSFT.html').read_bytes() == original


@pytest.mark.parametrize('css', ['@import "missing.css";', 'p{background:url(https://example.invalid/a.png)}', '@import "style.css";'])
def test_css_missing_external_and_cyclic_dependencies_are_rejected(library, tmp_path, css):
    (library / 'MSFT/style.css').write_text(css)
    replace_main(library, '<link rel="stylesheet" href="style.css">', ['MSFT/style.css'])
    manifest = reports.build_snapshot(library, tmp_path / 'output')
    assert 'msft-main' not in manifest['reports']


def test_style_attributes_and_srcset_dependencies_are_embedded(library, tmp_path):
    (library / 'MSFT/chart.png').write_bytes(b'png')
    replace_main(library, '<p style="background:url(chart.png)">x</p><img srcset="chart.png 1x, chart.png 2x">', ['MSFT/chart.png'])
    output = tmp_path / 'output'
    manifest = reports.build_snapshot(library, output)
    payload, _ = reports.snapshot_content_bytes(output, manifest['snapshot_id'], 'MSFT/MSFT.html')
    assert payload.count(b'data:image/png;base64,') == 3


def test_report_fragment_query_and_legacy_paths_use_snapshot_bound_reader(library, tmp_path):
    replace_main(library, '<a href="old.html?source=review#cash">专题</a>')
    document = _load_index(library)
    document['redirects'] = [{'from': 'MSFT/old.html', 'report_id': 'msft-cashflow'}]
    _write_index(library, document)
    output = tmp_path / 'output'
    first = reports.build_snapshot(library, output)
    payload, _ = reports.snapshot_content_bytes(output, first['snapshot_id'], 'MSFT/MSFT.html')
    assert f'/research/reports/msft-cashflow?snapshot={first["snapshot_id"]}#cash'.encode() in payload
    assert b'target="_top"' in payload
    legacy, _ = reports.snapshot_content_bytes(output, first['snapshot_id'], 'MSFT/old.html')
    actual, _ = reports.snapshot_content_bytes(output, first['snapshot_id'], 'MSFT/传统软件与现金流.html')
    assert legacy == actual
    (library / 'MSFT/传统软件与现金流.html').write_text('<p>new</p>')
    reports.build_snapshot(library, output)
    response = _client(output).get('/api/v1/research/reports/msft-cashflow', params={'snapshot': first['snapshot_id']})
    assert response.status_code == 200
    assert response.json()['data']['snapshot_id'] == first['snapshot_id']


def test_original_only_changes_and_rebuilds_are_consistent(library, tmp_path):
    replace_main(library, '<p>same</p><script>old()</script>')
    output = tmp_path / 'output'
    first = reports.build_snapshot(library, output)
    replace_main(library, '<p>same</p><script>new()</script>')
    second = reports.build_snapshot(library, output)
    assert second['snapshot_id'] != first['snapshot_id']
    third = reports.build_snapshot(library, output)
    assert third == second


def test_duplicate_json_keys_are_not_silently_overwritten(library, tmp_path):
    path = library / reports.INDEX_RELATIVE_PATH
    path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1'))
    with pytest.raises(reports.ResearchIndexError, match='重复 JSON'):
        reports.build_snapshot(library, tmp_path / 'output')
