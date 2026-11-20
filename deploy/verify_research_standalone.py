"""Read-only check of the installed HTML handler and mounted snapshot.

TestClient runs an isolated router with test authentication. It creates no real
session and changes no production authentication; this is not a public browser check.
"""
import argparse
import json
from pathlib import Path

from research_release import ResearchRelease, write_record


PROBE = r'''
import hashlib,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from html.parser import HTMLParser
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hl_app.auth import require_session
from hl_app.routes.research import router
from hl_app.services.research_reports import load_snapshot,report_content_url
import hl_app.routes.research,hl_app.services.research_display
root=Path(os.environ['HL_APP_RESEARCH_REPORTS_PATH'])
loaded=load_snapshot(root,sys.argv[1])
entry=loaded.manifest.reports[sys.argv[2]]
app=FastAPI();app.include_router(router,prefix='/api/v1')
app.state.settings=SimpleNamespace(research_reports_path=str(root))
app.dependency_overrides[require_session]=lambda:SimpleNamespace(account_id=1)
client=TestClient(app)
class Anchors(HTMLParser):
 def __init__(self,text):
  super().__init__();self.links=[];self.feed(text)
 def handle_starttag(self,tag,attrs):
  if tag=='a':self.links.append(dict(attrs))
response=client.get(report_content_url(loaded.snapshot_id,entry.entry_path))
assert response.status_code==200,'HTML handler failed'
anchors=Anchors(response.text).links
assert not any(a.get('href','').startswith('/research/reports/') for a in anchors),'Reader link leaked into standalone HTML'
prefix='/api/v1/research/content/'+loaded.snapshot_id+'/'
targets=sorted({a['href'] for a in anchors if a.get('href','').startswith(prefix)})
assert targets,'No module links found for this report'
assert all(a.get('target') in (None,'_self') for a in anchors if a.get('href','').startswith(prefix))
for url in targets:assert client.get(url).status_code==200,'Module handler failed'
reader=client.get('/api/v1/research/reports/'+entry.report_id,params={'snapshot':loaded.snapshot_id})
assert reader.status_code==200
assert any(a.get('href','').startswith('/research/reports/') for a in Anchors(reader.json()['data']['document_html']).links)
modules={m.__name__:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
 for m in (hl_app.routes.research,hl_app.services.research_display)}
print(json.dumps({'verification_scope':'installed_handler_and_mounted_files',
 'public_authenticated_browser_verified':False,'snapshot_id':loaded.snapshot_id,
 'report_id':entry.report_id,'targets_http_200':targets,'installed_module_sha256':modules}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--report-id', required=True)
    parser.add_argument('--record', type=Path, help='可选：保存私有核验记录')
    args = parser.parse_args()
    release = ResearchRelease(args.root)
    state = release.capture()
    result = json.loads(release.run(
        ['docker', 'exec', '-i', state['web']['Id'], 'python', '-', args.snapshot, args.report_id],
        input=PROBE.encode(),
    ))
    if args.record:
        write_record(args.record, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
