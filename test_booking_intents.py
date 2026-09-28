from __future__ import annotations
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

class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.client=Mock()
        response=Mock(); response.raise_for_status=Mock()
        self.client.patch=AsyncMock(return_value=response)
        self.client.post=AsyncMock(return_value=response)
        self.ns={'Account':object,'re':__import__('re'),'json':__import__('json'),'client':lambda:self.client,
                 'KAME_SUPABASE_URL':'https://example.test','KAME_HOUSEHOLD_ID':'home','_kame_headers':lambda:{}}
        for node in tree.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in ('registration_state','reservation_match','reconcile_course','reconciliation_courses'):
                exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),self.ns)
        self.acc=SimpleNamespace(key='alice',name='Alice')
        self.course={'session_id':'class','start_time':'09:00','end_time':'10:00','activity':'crossfit','title':'Wod'}
        self.row={'id':'event','version':4,'assignee':'Alice','event_date':'2026-09-28','event_time':'09:00:00','end_time':'10:00:00','sport_activity':'crossfit','sport_status':'done','konx_session_id':'class','konx_booking_status':'booked'}
    def test_only_matching_authenticated_occurrence_is_evidence(self):
        import json
        parse=self.ns['registration_state']
        def page(state, **extra):
            occ={'template_id':'class','date':'2026-09-28','is_booked_by_me':state,'my_waitlist_position':None,**extra}
            return '<script>self.__next_f.push('+json.dumps([1,json.dumps({'occ':occ})])+')</script>'
        self.assertIsNone(parse('<button>Me désinscrire</button>', 'class', '2026-09-28'))
        self.assertTrue(parse(page(True),'class','2026-09-28'))
        self.assertFalse(parse(page(False),'class','2026-09-28'))
        self.assertIsNone(parse(page(False),'class','2026-09-29'))
        self.assertIsNone(parse(page(False),'other','2026-09-28'))
        self.assertIsNone(parse(page(False,my_waitlist_position=2),'class','2026-09-28'))
        self.assertIsNone(parse(page('false'),'class','2026-09-28'))
    async def test_unknown_never_changes_anything(self):
        await self.ns['reconcile_course'](self.acc,'2026-09-28',self.course,None,[self.row])
        self.client.patch.assert_not_awaited();self.client.post.assert_not_awaited()
    async def test_confirmed_removal_preserves_attendance_and_uses_cas(self):
        await self.ns['reconcile_course'](self.acc,'2026-09-28',self.course,False,[self.row])
        call=self.client.patch.call_args.kwargs
        self.assertEqual(call['json'],{'konx_booking_status':None})
        self.assertEqual(call['params']['version'],'eq.4')
        self.assertEqual(call['params']['konx_booking_status'],'eq.booked')
    async def test_user_intents_are_not_overwritten(self):
        for status in ('pending','cancel'):
            self.row['konx_booking_status']=status
            await self.ns['reconcile_course'](self.acc,'2026-09-28',self.course,False,[self.row])
        self.client.patch.assert_not_awaited()
    async def test_manual_completed_session_is_linked_not_duplicated(self):
        self.row.update(konx_session_id=None,konx_booking_status=None)
        await self.ns['reconcile_course'](self.acc,'2026-09-28',self.course,True,[self.row])
        self.client.post.assert_not_awaited()
        self.assertEqual(self.client.patch.call_args.kwargs['json'],{'konx_session_id':'class','konx_booking_status':'booked'})
    def test_matching_is_scoped_to_person_and_date(self):
        match=self.ns['reservation_match']
        self.assertIsNone(match([self.row],'Bob','2026-09-28',self.course))
        self.assertIsNone(match([self.row],'Alice','2026-09-29',self.course))
    def test_hidden_past_bookings_are_still_checked(self):
        courses=self.ns['reconciliation_courses']([], [self.row], '2026-09-28')
        self.assertEqual([c['session_id'] for c in courses], ['class'])
        self.assertEqual(self.ns['reconciliation_courses']([], [self.row], '2026-09-29'), [])
        self.assertEqual(len(self.ns['reconciliation_courses']([self.course], [self.row], '2026-09-28')), 1)
    async def test_retry_inserts_same_id(self):
        for _ in range(2):
            await self.ns['reconcile_course'](self.acc,'2026-09-28',self.course,True,[])
        calls=self.client.post.call_args_list
        self.assertEqual(calls[0].kwargs['json']['id'],calls[1].kwargs['json']['id'])

if __name__=='__main__': unittest.main()
