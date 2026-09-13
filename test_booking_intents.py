import ast, asyncio, pathlib, unittest
from unittest.mock import AsyncMock, Mock
source=pathlib.Path(__file__).parent/'main.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
functions=['_process_cancel','_intent_lock','_set_booking_status']
class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns={'asyncio':asyncio,'_intent_locks':{},'log':Mock(),'account_for_person':lambda p:object(),'cancel':AsyncMock(return_value=False),'_kame_mark_skipped':AsyncMock()}
        for node in tree.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in functions:
                exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),self.ns)
        self.event={'id':'fixture','assignee':'Alice','konx_session_id':'test','event_date':'2026-09-15'}
    async def test_refusal_and_network_failure_keep_cancellation_pending(self):
        await self.ns['_process_cancel'](self.event)
        self.ns['_kame_mark_skipped'].assert_not_awaited()
        self.ns['cancel'].side_effect=RuntimeError('network failure')
        await self.ns['_process_cancel'](self.event)
        self.ns['_kame_mark_skipped'].assert_not_awaited()
    async def test_confirmation_updates_local_state(self):
        self.ns['cancel'].return_value=True
        await self.ns['_process_cancel'](self.event)
        self.ns['_kame_mark_skipped'].assert_awaited_once_with('fixture')
    async def test_cancellation_waits_for_in_flight_booking(self):
        lock=self.ns['_intent_lock']('fixture')
        await lock.acquire()
        task=asyncio.create_task(self.ns['_process_cancel'](self.event))
        await asyncio.sleep(0)
        self.ns['cancel'].assert_not_awaited()
        lock.release()
        await task
        self.ns['cancel'].assert_awaited_once()
    async def test_booking_result_cannot_overwrite_cancellation(self):
        response=Mock();response.json.return_value=[]
        client=Mock();client.patch=AsyncMock(return_value=response)
        self.ns.update(client=lambda:client,KAME_SUPABASE_URL='https://example.test',_kame_headers=lambda:{},json=__import__('json'))
        result=await self.ns['_set_booking_status']('fixture','booked')
        self.assertFalse(result)
        self.assertEqual(client.patch.call_args.kwargs['params']['konx_booking_status'],'eq.pending')
if __name__=='__main__': unittest.main()
