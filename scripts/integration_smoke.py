"""Real loopback HTTP with synthetic providers; never calls a live AI or sends mail."""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestServer
from free_router.config import Config
from free_router.app import create_app
from tests.helpers import configuration, completion, MAGI_KEY, NARA_KEY


async def main(magi_root, nara_root):
    sys.path.insert(0,str(magi_root/'tools'/'magi'))
    sys.path.insert(0,str(nara_root))
    from orchestrator import MagiOrchestrator
    from magi_core import canonical_hash
    from nara_reporter.config import Settings
    from nara_reporter.ai import ProcurementAiRouter
    from nara_reporter.models import Opportunity
    item=Opportunity(source='synthetic',source_type='bid',external_id='000',external_order='0',
        title='가상 특허 조사',business_type='일반용역',status='bid',agency='가상기관',demand_agency='가상기관',
        budget_amount=None,estimated_price=None,notice_at=None,close_at=None,opening_at=None,
        url='https://example.test/synthetic',region_limit='',eligible_regions='',industry_limit='',eligible_industries='')
    calls=[]
    async def upstream(request):
        body=await request.json()
        assert body['stream'] is False
        assert body['response_format']=={'type':'json_object'}
        calls.append(body['model'])
        if 'ASSIGNED ROLE:' in body['messages'][0]['content']:
            answer='Compare the evidence and note its limitations.' if body['model']=='mock-a' else 'Investigate alternative explanations before making a decision.'
            content={'status':'ok','answer':answer,'claims':[],'alternatives':[],'objections':[],
                     'risks':[],'uncertainties':['Synthetic test only.'],'recommended_next_checks':[]}
        elif '"reports"' in body['messages'][-1]['content']:
            content={'reports':[{'key':item.key,'summary':'가상 특허 조사 공고입니다.',
                'task_period':'확인 필요','manual_checks':['발표 여부는 확인되지 않음']}]}
        else:
            content={'matches':[{'key':item.key,'priority':90,'reason':'합성 특허 조사 공고'}]}
        return web.json_response(completion(json.dumps(content,ensure_ascii=False)))
    app=web.Application()
    app.router.add_post('/{provider}/v1/chat/completions',upstream)
    async with TestServer(app) as provider:
        raw,env=configuration(str(provider.make_url('')).rstrip('/'))
        async with TestServer(create_app(Config(raw,environ=env,allow_local=True))) as gateway:
            url=str(gateway.make_url('/v1'))
            def clients():
                with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{
                    'MAGI_ROUTER_ENABLED':'1','PERSONAL_ROUTER_BASE_URL':url,
                    'PERSONAL_ROUTER_API_KEY':MAGI_KEY,'PERSONAL_ROUTER_ALLOW_LOOPBACK':'1'},clear=True):
                    engine=MagiOrchestrator(workspace=Path(root))
                    task={'task_id':'integration-synthetic','task_type':'review','objective':'Review a synthetic example.',
                        'audience':'','constraints':[],'source_manifest':[],'privacy':'public','mode':'standard'}
                    workers=engine._select_workers(mode='standard',task_id=task['task_id'])
                    roles=engine._assign_roles(workers=workers,task_type='review',task_id=task['task_id'])
                    approved=canonical_hash(engine.approval_manifest(task=task,source_payloads=[],workers=workers,roles=roles))
                    _,report=engine.run(task_type='review',objective=task['objective'],audience='',constraints=[],input_paths=[],
                        privacy='public',mode='standard',allow_external=True,allow_internal=False,approved_outbound_hash=approved,task_id=task['task_id'])
                    assert all(w['status']=='ok' for w in report['workers']),report['workers']
                    assert {w['model'] for w in report['workers']}=={'mock-a','mock-b'}
                    assert len(report['provider_families'])==2
                with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{
                    'PERSONAL_ROUTER_ENABLED':'1','PERSONAL_ROUTER_BASE_URL':url,
                    'PERSONAL_ROUTER_API_KEY':NARA_KEY,'PERSONAL_ROUTER_ALLOW_LOOPBACK':'1'},clear=True):
                    settings=Settings.from_env(Path(root))
                    nara=ProcurementAiRouter(settings)
                    selections=nara.select_opportunities('합성 특허 분석',[item])
                    assert len(selections)==1 and selections[0].opportunity_key==item.key, nara.errors
                    summaries=nara.summarize_opportunities('합성 특허 분석',[item],{})
                    assert summaries[item.key].provider=='personal_router',nara.errors
                    assert summaries[item.key].manual_checks==['발표 여부는 확인되지 않음']
                    assert summaries[item.key].model=='mock-a'
                    assert not nara.errors,nara.errors
            await asyncio.to_thread(clients)
    assert len(calls)==4,calls
    print('PASS: MAGI two workers and Nara selection/summary completed through real loopback HTTP (4 synthetic upstream requests).')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--magi',type=Path,default=Path('/workspace/magi'))
    parser.add_argument('--nara',type=Path,default=Path('/workspace/nara-monitor'))
    args=parser.parse_args()
    asyncio.run(main(args.magi,args.nara))
