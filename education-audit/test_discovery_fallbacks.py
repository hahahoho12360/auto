import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).parent))
import monitor
import discovery_fallbacks as f

class DiscoveryFallbackTests(unittest.TestCase):
    def test_general_boards_flagged_as_general(self):
        office={'id':'gne-창원','home':'https://cwedu.gne.go.kr/','boards':[]}
        fetcher=Mock()
        fetcher.html.return_value=('<a href="/cwedu/na/ntt/selectNttList.do?bbsId=10">행정지원과</a><a href="/cwedu/na/ntt/selectNttList.do?bbsId=11">공지사항</a>','https://cwedu.gne.go.kr/cwedu/main.do')
        with patch.object(f,'original_discover',return_value=[]):
            result=f.discover_boards(office,fetcher)
        self.assertEqual(len(result),2)
        self.assertEqual(office['board_discovery_mode'],'general_boards_with_audit_title_filter')
    def test_existing_audit_board_preserved(self):
        fetcher=Mock()
        with patch.object(f,'original_discover',return_value=['https://a.go.kr/audit']):
            self.assertEqual(f.discover_boards({},fetcher),['https://a.go.kr/audit'])
        fetcher.html.assert_not_called()
    def test_unresolved_attachment_not_claimed_read(self):
        fetcher=Mock()
        fetcher.html.return_value=('<div class="view_cont">학교 감사결과를 붙임과 같이 공개합니다.</div>','https://a.go.kr/view')
        with patch.object(f,'original_detail',return_value={'attachments':[],'read_status':'본문 확인','excerpt':'감사결과를 공개합니다.'}):
            result=f.detail({'url':'https://a.go.kr/view'},fetcher)
        self.assertEqual(result['read_status'],'일부만 확인')
        self.assertEqual(result['attachment_discovery_status'],'expected_but_unresolved')
        self.assertEqual(result['attachments'],[])
    def test_plain_body_without_attachment_claim_unchanged(self):
        fetcher=Mock()
        fetcher.html.return_value=('<div class="view_cont">학교 감사 실시 일정은 다음과 같습니다.</div>','https://a.go.kr/view')
        with patch.object(f,'original_detail',return_value={'attachments':[],'read_status':'본문 확인','excerpt':'일정 안내'}):
            result=f.detail({'url':'https://a.go.kr/view'},fetcher)
        self.assertEqual(result['read_status'],'본문 확인')
    def test_new_events_precede_baseline_enrichment(self):
        candidates=[({}, {}, {'url':'old'}), ({}, {}, {'url':'new'})]
        with patch.object(f,'original_candidates',return_value=candidates):
            result=f.enrich_candidates([],{},[{'url':'new','kind':'new'}],[])
        self.assertEqual([x[2]['url'] for x in result],['new','old'])

if __name__=='__main__': unittest.main()
