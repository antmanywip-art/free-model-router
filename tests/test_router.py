import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from free_router.app import ROUTER_KEY, create_app
from free_router.config import Config, ConfigError
from free_router.state import State
from tests.helpers import configuration, completion, MAGI_KEY, NARA_KEY, UPSTREAM_KEY


class ConfigurationTests(unittest.TestCase):
    def test_rejects_unverified_account_and_missing_billing_guard(self):
        for change in [{"account_verified":False},{"billing_guard":None},{"evidence":[]}]:
            raw, env = configuration("https://example.test")
            raw["models"][0]["free"].update(change)
            with self.assertRaises(ConfigError): Config(raw,environ=env)

    def test_evidence_expiry_and_promotion_expiry(self):
        raw, env = configuration("https://example.test")
        cfg = Config(raw,environ=env)
        model = cfg.models["a"]
        model["free"]["kind"] = "promotion"
        model["free"]["expires_at"] = datetime.now(timezone.utc).isoformat()
        self.assertFalse(cfg.eligible(model))
        model["free"].pop("expires_at")
        self.assertFalse(cfg.eligible(model,datetime.now(timezone.utc)+timedelta(days=2)))

    def test_future_evidence_not_usable(self):
        raw, env = configuration("https://example.test")
        cfg = Config(raw,environ=env)
        self.assertFalse(cfg.eligible(cfg.models['a'], datetime.now(timezone.utc)-timedelta(days=1)))

    def test_http_requires_explicit_loopback_test_mode(self):
        raw, env = configuration("http://127.0.0.1:1234")
        with self.assertRaises(ConfigError): Config(raw,environ=env)
        Config(raw,environ=env,allow_local=True)
        raw['providers'][0]['base_url']='http://example.test/v1'
        with self.assertRaises(ConfigError): Config(raw,environ=env,allow_local=True)

    def test_client_keys_cannot_be_reused(self):
        raw, env = configuration("https://example.test")
        env['NARA_KEY']=env['MAGI_KEY']
        with self.assertRaises(ConfigError): Config(raw,environ=env)

    def test_counters_and_cooldown_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/'usage.db')
            s = State(path)
            self.assertTrue(s.reserve('account:a',1,3))
            s.cool('account:a',30)
            s.record('magi','a','ok',{'prompt_tokens':9})
            s.close()
            s = State(path)
            self.assertFalse(s.reserve('account:a',1,3))
            self.assertTrue(s.blocked('account:a'))
            self.assertEqual(s.summary('magi')[0]['input_tokens'],9)
            self.assertEqual(s.summary('nara'),[])
            s.close()


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.behaviors = {}
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        upstream = web.Application()
        upstream.router.add_post('/{provider}/v1/chat/completions',self.upstream)
        self.upstream_server = TestServer(upstream,handler_cancellation=True)
        await self.upstream_server.start_server()
        self.raw,self.env = configuration(str(self.upstream_server.make_url('')).rstrip('/'))
        self.cfg = Config(self.raw,environ=self.env,allow_local=True)
        self.app = create_app(self.cfg)
        self.client = TestClient(TestServer(self.app,handler_cancellation=True))
        await self.client.start_server()
        self.router = self.app[ROUTER_KEY]

    async def asyncTearDown(self):
        self.gate.set()
        await self.client.close()
        await self.upstream_server.close()

    async def upstream(self, request):
        provider = request.match_info['provider']
        body = await request.json()
        self.seen.append((provider,body,request.headers.get('Authorization')))
        mode = self.behaviors.get(provider,'ok')
        if mode == 'wait':
            self.entered.set()
            try: await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if mode == '429': return web.Response(status=429,headers={'Retry-After':'120'},text='private upstream error')
        if mode == '401': return web.Response(status=401,text=UPSTREAM_KEY)
        if mode == 'redirect': return web.Response(status=307,headers={'Location':str(self.upstream_server.make_url('/b/v1/chat/completions'))})
        if mode == 'badjson': return web.json_response(completion('{"x":1,"x":2}'))
        if mode == 'truncated':
            obj=completion();obj['choices'][0]['finish_reason']='length'
            return web.json_response(obj)
        if mode in {'stream','partial-stream'}:
            r = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await r.prepare(request)
            await r.write(b'data: {"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n')
            if mode == 'stream': await r.write(b'data: [DONE]\n\n')
            await r.write_eof()
            return r
        if mode == 'tools':
            obj=completion(None)
            obj['choices'][0]['message']['tool_calls']=[{'id':'call-1','type':'function','function':{'name':'lookup','arguments':'{}'}}]
            obj['choices'][0]['finish_reason']='tool_calls'
            return web.json_response(obj)
        if mode == 'large': return web.Response(body=b'x'*5000000)
        return web.json_response(completion())

    async def post(self, **changes):
        body={'model':'general','messages':[{'role':'user','content':'synthetic JSON'}], 'response_format':{'type':'json_object'}}
        body.update(changes)
        return await self.client.post('/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+MAGI_KEY})

    async def test_completion_and_authentication_isolation(self):
        response=await self.post()
        self.assertEqual(response.status,200)
        body=await response.json()
        self.assertEqual(body['router']['route'],'a')
        self.assertEqual(self.seen[0][2],'Bearer '+UPSTREAM_KEY)
        self.assertNotIn('router',self.seen[0][1])
        self.assertNotIn(MAGI_KEY,json.dumps(self.router.state.summary('magi')))

    async def test_missing_credentials_rejected(self):
        response=await self.client.post('/v1/chat/completions',json={})
        self.assertEqual(response.status,401)
        self.assertEqual(self.seen,[])

    async def test_models_are_client_scoped(self):
        response=await self.client.get('/v1/models',headers={'Authorization':'Bearer '+NARA_KEY})
        self.assertEqual([x['id'] for x in (await response.json())['data']],['nara-text'])
        response=await self.client.post('/v1/chat/completions',json={'model':'a','messages':[{'role':'user','content':'x'}]},headers={'Authorization':'Bearer '+NARA_KEY})
        self.assertEqual(response.status,403)

    async def test_paid_unknown_expired_routes_never_called(self):
        for status in ['paid','unknown','expired']:
            self.cfg.models['a']['free']['status']=status
            self.cfg.models['b']['free']['status']=status
            response=await self.post()
            self.assertEqual(response.status,503)
        self.assertEqual(self.seen,[])

    async def test_expiry_after_config_load_blocks_request(self):
        for route in self.cfg.models.values():
            route['free']['verify_until']=datetime.now(timezone.utc).isoformat()
        self.assertEqual((await self.post()).status,503)
        self.assertEqual(self.seen,[])

    async def test_rate_limit_fallback_and_cooldown(self):
        self.behaviors['a']='429'
        response=await self.post()
        self.assertEqual((await response.json())['router']['route'],'b')
        await self.post()
        self.assertEqual([x[0] for x in self.seen],['a','b','b'])

    async def test_exact_model_does_not_fallback(self):
        self.behaviors['a']='429'
        self.assertEqual((await self.post(model='a')).status,503)
        self.assertEqual([x[0] for x in self.seen],['a'])

    async def test_invalid_json_falls_back_without_format_downgrade(self):
        self.behaviors['a']='badjson'
        response=await self.post()
        self.assertEqual((await response.json())['router']['route'],'b')
        self.assertTrue(all(x[1]['response_format']=={'type':'json_object'} for x in self.seen))

    async def test_truncated_response_not_success(self):
        self.behaviors['a']='truncated'
        self.assertEqual((await self.post(model='a')).status,503)

    async def test_redirect_is_not_followed(self):
        self.behaviors['a']='redirect'
        self.assertEqual((await self.post(model='a')).status,503)
        self.assertEqual(len(self.seen),1)

    async def test_upstream_error_body_not_exposed(self):
        self.behaviors['a']='401'
        body=await (await self.post(model='a')).text()
        self.assertNotIn(UPSTREAM_KEY,body)
        self.assertTrue(self.router.state.blocked('account:a'))

    async def test_capability_and_family_filters(self):
        self.cfg.models['a']['capabilities']=['text']
        response=await self.post()
        self.assertEqual((await response.json())['router']['route'],'b')
        self.assertEqual((await self.post(router={'exclude_families':['family-b']})).status,503)

    async def test_context_and_output_budget(self):
        self.assertEqual((await self.post(max_tokens=9000)).status,503)
        self.assertEqual((await self.post(messages=[{'role':'user','content':'가'*15000}])).status,503)
        self.assertEqual(self.seen,[])

    async def test_shared_account_quota_applies_to_both_endpoints(self):
        for p in self.cfg.providers.values():
            p['quota_group']='shared';p['requests_per_day']=1
        self.behaviors['a']='429'
        self.assertEqual((await self.post()).status,503)
        self.assertEqual([x[0] for x in self.seen],['a'])

    async def test_client_quota_and_isolated_usage(self):
        self.cfg.clients['magi']['requests_per_day']=1
        self.assertEqual((await self.post()).status,200)
        self.assertEqual((await self.post()).status,429)
        response=await self.client.get('/v1/usage',headers={'Authorization':'Bearer '+NARA_KEY})
        self.assertEqual((await response.json())['data'],[])

    async def test_concurrency_limit(self):
        self.cfg.max_parallel=1
        self.behaviors['a']='wait'
        first=asyncio.create_task(self.post())
        await asyncio.wait_for(self.entered.wait(),1)
        self.assertEqual((await self.post()).status,429)
        self.gate.set()
        self.assertEqual((await first).status,200)

    async def test_deadline_is_bounded(self):
        self.cfg.timeout=1
        self.behaviors['a']='wait'
        started=asyncio.get_running_loop().time()
        response=await self.post()
        self.assertEqual(response.status,504)
        self.assertLess(asyncio.get_running_loop().time()-started,2)
        self.assertEqual(self.router.active,0)

    async def test_client_disconnect_cancels_upstream(self):
        self.behaviors['a']='wait'
        url=self.client.make_url('/v1/chat/completions')
        reader,writer=await asyncio.open_connection(url.host,url.port)
        body=json.dumps({'model':'a','messages':[{'role':'user','content':'synthetic'}]}).encode()
        writer.write((f'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {MAGI_KEY}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body)
        await writer.drain()
        await asyncio.wait_for(self.entered.wait(),1)
        writer.close();await writer.wait_closed()
        await asyncio.wait_for(self.cancelled.wait(),1)
        self.assertEqual(self.router.active,0)

    async def test_stream_and_partial_stream_never_replay(self):
        for mode in ['stream','partial-stream']:
            self.behaviors['a']=mode
            response=await self.post(stream=True,response_format={'type':'text'})
            text=await response.text()
            self.assertIn('hello',text)
            self.assertIn('[DONE]' if mode=='stream' else 'stream_interrupted',text)
        self.assertEqual([x[0] for x in self.seen],['a','a'])

    async def test_tool_call_forwarded(self):
        self.behaviors['a']='tools'
        response=await self.post(response_format={'type':'text'},tools=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}])
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['choices'][0]['message']['tool_calls'][0]['id'],'call-1')

    async def test_large_body_rejected(self):
        response=await self.post(messages=[{'role':'user','content':'x'*600000}])
        self.assertEqual(response.status,413)
        self.assertEqual(self.seen,[])

    async def test_large_upstream_rejected(self):
        self.behaviors['a']='large'
        self.assertEqual((await self.post(model='a')).status,503)

    async def test_no_credentials_not_ready(self):
        self.cfg.env.clear()
        self.assertEqual((await self.client.get('/readyz')).status,503)
        self.assertEqual((await self.client.get('/healthz')).status,200)

    async def test_invalid_reload_retains_configuration(self):
        self.cfg.env['ROUTER_ADMIN_KEY']='admin-test-'+'a'*40
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'broken.json';path.write_text('{broken')
            self.router.config_path=path
            response=await self.client.post('/admin/reload',headers={'Authorization':'Bearer '+self.cfg.env['ROUTER_ADMIN_KEY']})
            self.assertEqual(response.status,400)
            self.assertEqual((await self.post()).status,200)
