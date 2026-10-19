import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent))
import monitor
import engine as e
import fair_runner as f

class FairRunnerTests(unittest.TestCase):
    def test_regional_round_robin(self):
        items=[{'id':'a1','kind':'교육지원청','parent':'a'}, {'id':'a2','kind':'교육지원청','parent':'a'}, {'id':'b1','kind':'교육지원청','parent':'b'}, {'id':'moe','kind':'교육부'}, {'id':'a','kind':'시도교육청'}]
        result=f.fair_order({x['id']:x for x in items})
        self.assertEqual([x['id'] for x in result],['moe','a','a1','b1','a2'])
    def test_transport_circuit(self):
        fetcher=f.FairFetcher()
        with patch.object(e.Fetcher,'_raw',side_effect=e.FetchError('ConnectTimeout')) as network:
            for _ in range(2):
                with self.assertRaises(e.FetchError): fetcher._raw('https://a.go.kr/')
            with self.assertRaisesRegex(e.FetchError,'host_circuit_open'):
                fetcher._raw('https://a.go.kr/')
            self.assertEqual(network.call_count,2)
    def test_http404_does_not_disable_host(self):
        fetcher=f.FairFetcher()
        with patch.object(e.Fetcher,'_raw',side_effect=e.FetchError('http_404')) as network:
            for _ in range(3):
                with self.assertRaises(e.FetchError): fetcher._raw('https://a.go.kr/no')
            self.assertEqual(network.call_count,3)
    def test_deadline_checked_before_network(self):
        fetcher=f.FairFetcher(); fetcher.deadline=time.monotonic()-1
        with patch.object(e.Fetcher,'_raw') as network:
            with self.assertRaisesRegex(e.FetchError,'budget_exhausted'): fetcher._raw('https://a.go.kr/')
            network.assert_not_called()
    def test_all_listings_precede_attachments(self):
        calls=[]
        today=datetime.now(e.TZ).date().isoformat()
        offices={str(i):{'id':str(i),'kind':'시도교육청','name':str(i)} for i in range(3)}
        def registry(config,state,fetcher):
            state['board_state']={}
            for oid in offices:
                url='https://example.go.kr/view?nttSn='+oid
                state['board_state'][oid]={'posts':{url:{'url':url,'title':'종합감사 결과','posted_at':today,'last_listed':today,'first_seen':today}}}
            return offices
        def listing(office,state,fetcher,config):
            self.assertEqual(config['max_details_per_board'],0)
            oid=office['id']; calls.append('list'+oid)
            return {'id':oid,'institution':oid,'kind':'시도교육청','status':'partial','issues':[],'boards':[{'url':oid,'status':'listing_checked','issues':['detail_limit_unchecked']}],'listed_posts':1,'details_checked':0},[],[]
        def detail(record,fetcher,old):
            self.assertEqual(sum(x.startswith('list') for x in calls),3)
            calls.append('detail')
            return {'url':record['url'],'content_hash':'hash','read_status':'본문 확인','excerpt':'실제 본문','attachments':[]}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/'config.json'
            config.write_text(json.dumps({'max_http_requests':20,'max_run_seconds':500,'workers':3,'max_list_pages':2}))
            with patch.object(e,'establish_registry',side_effect=registry),patch.object(e,'process_office',side_effect=listing),patch.object(e,'detail',side_effect=detail):
                f.run(argparse.Namespace(config=str(config),out=str(root/'out'),limit=0))
            report=json.loads((root/'out/latest.json').read_text())
            self.assertEqual(report['coverage']['institutions_with_all_registered_listings_checked'],3)
            self.assertEqual(report['coverage']['detail_checks_succeeded'],3)
            self.assertFalse(report['billing']['email_sent'])
            self.assertTrue((root/'out/listing-checkpoint.json').is_file())

if __name__=='__main__': unittest.main()
