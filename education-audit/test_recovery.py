import sys
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).parent))
import monitor as m
import recovery as r
import cms_adapters as cms

class RecoveryTests(unittest.TestCase):
    def test_gne_nttview_markup(self):
        h='''<tr><td><a href="javascript:void(0)" onclick="nttView('3032651');">2026 자체감사 결과</a></td><td>2026.10.19</td></tr>'''
        p=m.parse_posts(h,'https://cwedu.gne.go.kr/cwedu/na/ntt/selectNttList.do?bbsId=1540&mi=5436')
        self.assertEqual(len(p),1)
        self.assertIn('selectNttInfo.do',p[0]['url'])
        self.assertIn('nttSn=3032651',p[0]['url'])
        self.assertEqual(p[0]['posted_at'],'2026-10-19')
    def test_nttview_requires_known_cms(self):
        h='''<tr><td><a href="javascript:void(0)" onclick="nttView('3032651');">2026 자체감사 결과</a></td><td>2026.10.19</td></tr>'''
        self.assertEqual(m.parse_posts(h,'https://cwedu.gne.go.kr/other'),[])
    def test_shared_host_circuits_independent(self):
        self.assertNotEqual(r.RecoveryFetcher.scope_key('https://www.gbe.kr/ph/main.do'),r.RecoveryFetcher.scope_key('https://www.gbe.kr/gj/main.do'))
    def test_temporary_retry_once(self):
        f=r.RecoveryFetcher()
        with patch.object(cms.SessionFetcher,'_raw',side_effect=[m.FetchError('ConnectTimeout'),(b'ok','https://a.go.kr/p','utf-8','text/plain')]) as raw, patch.object(r.time,'sleep'):
            self.assertEqual(f._raw('https://a.go.kr/p')[0],b'ok'); self.assertEqual(raw.call_count,2)
    def test_retry_exhaustion_bounded(self):
        f=r.RecoveryFetcher()
        with patch.object(cms.SessionFetcher,'_raw',side_effect=m.FetchError('ReadTimeout')) as raw,patch.object(r.time,'sleep'):
            with self.assertRaisesRegex(m.FetchError,'ReadTimeout'):f._raw('https://a.go.kr/ph/x')
            with self.assertRaisesRegex(m.FetchError,'source_cooldown'):f._raw('https://a.go.kr/ph/y')
            self.assertEqual(raw.call_count,2)
    def test_cooldown_not_other_institution(self):
        f=r.RecoveryFetcher(); f.source_failures[f.scope_key('https://a.go.kr/ph/x')]=(2,time.monotonic()+30,'ConnectTimeout')
        with patch.object(cms.SessionFetcher,'_raw',return_value=(b'ok','x','utf-8','text/plain')) as raw:
            self.assertEqual(f._raw('https://a.go.kr/gj/x')[0],b'ok');self.assertEqual(raw.call_count,1)
    def test_expired_cooldown_retries(self):
        f=r.RecoveryFetcher(); f.source_failures[f.scope_key('https://a.go.kr/ph/x')]=(2,time.monotonic()-1,'ConnectTimeout')
        with patch.object(cms.SessionFetcher,'_raw',return_value=(b'ok','x','utf-8','text/plain')):
            self.assertEqual(f._raw('https://a.go.kr/ph/x')[0],b'ok')
    def test_tls_not_retried_or_disabled(self):
        f=r.RecoveryFetcher()
        with patch.object(cms.SessionFetcher,'_raw',side_effect=m.FetchError('SSLError')) as raw:
            with self.assertRaisesRegex(m.FetchError,'SSLError'):f._raw('https://a.go.kr/')
            self.assertEqual(raw.call_count,1)
    def test_explicit_robots_denial(self):
        f=r.RecoveryFetcher(); f._raw=Mock(return_value=(b'User-agent: *\nDisallow: /','x','utf-8','text/plain'))
        with self.assertRaisesRegex(m.FetchError,'robots_disallowed$'):f.allowed('https://a.go.kr/')
    def test_policy_unreachable_not_explicit_denial(self):
        f=r.RecoveryFetcher(); f._raw=Mock(side_effect=m.FetchError('ConnectTimeout'))
        with self.assertRaisesRegex(m.FetchError,'robots_unreachable:ConnectTimeout'):f.allowed('https://a.go.kr/')
    def test_policy_404_allows(self):
        f=r.RecoveryFetcher(); f._raw=Mock(side_effect=m.FetchError('http_404'))
        self.assertTrue(f.allowed('https://a.go.kr/'))
    def test_policy_403_stays_restricted(self):
        f=r.RecoveryFetcher(); f._raw=Mock(side_effect=m.FetchError('http_403'))
        with self.assertRaisesRegex(m.FetchError,'robots_access_denied'):f.allowed('https://a.go.kr/')
    def test_policy_cache_single_fetch(self):
        f=r.RecoveryFetcher(); f._raw=Mock(return_value=(b'User-agent: *\nAllow: /','x','utf-8','text/plain'))
        self.assertTrue(f.allowed('https://a.go.kr/a'));self.assertTrue(f.allowed('https://a.go.kr/b'));self.assertEqual(f._raw.call_count,1)
    def test_invalid_html_policy(self):
        f=r.RecoveryFetcher();f._raw=Mock(return_value=(b'<!doctype html><p>error</p>','x','utf-8','text/html'))
        with self.assertRaisesRegex(m.FetchError,'invalid_policy_response'):f.allowed('https://a.go.kr/')
    def test_policy_failed_fetch_expires(self):
        f=r.RecoveryFetcher(); f.policy_cache['https://a.go.kr']=(time.monotonic()-1,None,'robots_unreachable:ConnectTimeout')
        f._raw=Mock(return_value=(b'User-agent: *\nAllow: /','x','utf-8','text/plain'));self.assertTrue(f.allowed('https://a.go.kr/'))
    def test_categories_do_not_hide_unknown_policy(self):
        cases={'homepage_unresolved':'homepage_setup','list_parser_unverified':'parser_setup','robots_denied_or_unavailable':'policy_unchecked','robots_disallowed':'policy_blocked','robots_unreachable:SSLError':'address_tls','ConnectTimeout':'temporary'}
        for code,cat in cases.items():self.assertEqual(r.failure_category({'issues':[code]}),cat)
    def test_failure_counts_and_recovery(self):
        def item(i,status,code):return {'id':i,'institution':i,'status':status,'issues':[code] if code else [],'boards':[{'status':'listing_checked','issues':[]}] if status!='failed' else []}
        old={'institutions':[item('a','failed','homepage_unresolved')],'finished_at':'2026-10-19T12:00:00+09:00','coverage':{'failed':1}}
        report={'institutions':[item('a','partial',''),item('b','failed','list_parser_unverified')],'finished_at':'2026-10-19T16:00:00+09:00','report_date':'2026-10-19'}
        history={};s=r.build_failure_summary(report,old,history)
        self.assertEqual(sum(s['by_category'].values()),s['failed_institutions']);self.assertEqual(s['recovered_since_previous_run'],['a']);self.assertEqual(history['b']['consecutive_failed_runs'],1)
    def test_gne_add_only_actual_links(self):
        o={'id':'gne-창원','parent':'gne','home':'https://cwedu.gne.go.kr/'}
        f=Mock();f.html.return_value=('<a href="/cwedu/na/ntt/selectNttList.do?bbsId=1568">청렴사랑방</a><a href="https://evil.test/list">감사결과</a>',o['home'])
        with patch.object(r,'_original_discover',return_value=[]):
            result=r.discover_boards(o,f)
        self.assertEqual(len(result),1);self.assertIn('bbsId=1568',result[0])

if __name__=='__main__':unittest.main()
