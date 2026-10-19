import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import operations as ops

NOW = datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc)

def sample():
    report={'report_id':'new','report_date':'2026-10-21',
            'started_at':'2026-10-21T06:35:00+09:00','finished_at':'2026-10-21T06:50:00+09:00',
            'coverage':{'target_institutions':193,'attempted_institutions':193,
                        'listing_checked':1,'partial':65,'failed':127,
                        'institutions_with_any_listing_checked':66}, 'events':[], 'scope':{}}
    heartbeat={'report_id':'new','target_date':'2026-10-21','collection_outcome':'success'}
    return report,heartbeat

class OperationsTests(unittest.TestCase):
    def test_ready_partial_coverage(self):
        r,h=sample(); self.assertTrue(ops.readiness(r,h,NOW)[0])
    def test_skip_duplicate_daily_collection(self):
        r,h=sample(); self.assertFalse(ops.should_collect(r,h,NOW)[0])
    def test_force(self):
        r,h=sample(); self.assertTrue(ops.should_collect(r,h,NOW,True)[0])
    def test_stale_report(self):
        r,h=sample(); r['report_date']='2026-10-20'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_failed_heartbeat(self):
        r,h=sample(); h['collection_outcome']='failure'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_running_heartbeat(self):
        r,h=sample(); h['collection_outcome']='running'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_legacy_heartbeat_not_trusted(self):
        r,h=sample(); h.pop('report_id')
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_report_id_mismatch(self):
        r,h=sample(); h['report_id']='other'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_timezone_required(self):
        r,h=sample(); r['finished_at']='2026-10-21T06:50:00'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_future_timestamp(self):
        r,h=sample(); r['finished_at']='2026-10-21T08:50:00+09:00'
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_counts(self):
        r,h=sample(); r['coverage']['failed']=128
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_partial_run_not_complete(self):
        r,h=sample(); r['coverage']['attempted_institutions']=192;r['coverage']['failed']=126
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_all_sources_failed_still_reported_but_retry(self):
        r,h=sample(); r['coverage'].update(listing_checked=0,partial=0,failed=193,institutions_with_any_listing_checked=0)
        self.assertTrue(ops.readiness(r,h,NOW)[0]); self.assertTrue(ops.should_collect(r,h,NOW)[0])
    def test_early_manual_run_does_not_skip_morning(self):
        r,h=sample(); r['started_at']='2026-10-21T01:00:00+09:00'
        self.assertTrue(ops.should_collect(r,h,NOW)[0])
    def test_finish_old_report_not_success(self):
        r,h=sample(); t=ops.make_ticket({},NOW-timedelta(minutes=26),r)
        self.assertEqual(ops.finish_record(t,r,'success','success',NOW)['collection_outcome'],'failure')
    def test_finish_new_report(self):
        r,h=sample(); t=ops.make_ticket({},NOW-timedelta(minutes=26),{'report_id':'old'})
        result=ops.finish_record(t,r,'success','success',NOW)
        self.assertEqual(result['collection_outcome'],'success');self.assertFalse(result['email_sent'])
    def test_finish_collector_failure(self):
        r,h=sample(); t=ops.make_ticket({},NOW-timedelta(minutes=26),{'report_id':'old'})
        self.assertEqual(ops.finish_record(t,r,'success','failure',NOW)['collection_outcome'],'failure')
    def test_finish_test_failure(self):
        r,h=sample(); t=ops.make_ticket({},NOW-timedelta(minutes=26),{'report_id':'old'})
        self.assertEqual(ops.finish_record(t,r,'failure','success',NOW)['collection_outcome'],'failure')
    def test_stale_briefing_has_no_today_events(self):
        r,h=sample();r['report_date']='2026-10-20';r['events']=[{'event_id':'x','title':'old'}]
        b=ops.compact_briefing(r,h,NOW)
        self.assertEqual(b['status'],'not_ready');self.assertNotIn('events',b);self.assertNotIn('coverage',b)
    def test_briefing_is_not_email_delivery(self):
        r,h=sample();b=ops.compact_briefing(r,h,NOW)
        self.assertEqual(b['status'],'ready');self.assertFalse(b['mail_sent'])
    def test_json(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'x.json';ops.write_json(p,{'감사':1})
            self.assertEqual(ops.read_json(p),{'감사':1})
    def test_no_json_array(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'x.json';p.write_text('[]')
            with self.assertRaises(ValueError):ops.read_json(p)
    def test_no_events_list(self):
        r,h=sample();r['events']=None
        self.assertFalse(ops.readiness(r,h,NOW)[0])
    def test_empty_objects(self):
        self.assertFalse(ops.readiness({}, {}, NOW)[0])

if __name__=='__main__': unittest.main()
