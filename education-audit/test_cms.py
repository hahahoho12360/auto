import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent))
import monitor as m

class CMSIntegrationTests(unittest.TestCase):
    def test_moe_live_markup(self):
        html='''<tr><td><a href="#" onclick="goView('345','107421','0',null,'W','1','N','');" title="2026년도 국정감사 계획서">2026년도 국정감사 계획서</a></td><td>2026-10-13</td><td>324</td></tr>'''
        item=m.parse_posts(html,'https://www.moe.go.kr/boardCnts/listRenew.do?boardID=345&m=041202&s=moe')[0]
        self.assertIn('boardSeq=107421',item['url'])
        self.assertIn('lev=0',item['url'])
    def test_gwe_live_markup(self):
        html='''<tr><td><a href="#none" onclick="goView('56810','N');" title="자체감사 결과 공개"><span class="new">NEW</span>자체감사 결과 공개</a></td><td>2026-10-12</td></tr>'''
        item=m.parse_posts(html,'https://www.gwe.go.kr/open/bbs/list.do?key=abcd')[0]
        self.assertIn('bbsSn=56810',item['url']); self.assertEqual(item['title'],'자체감사 결과 공개')
    def test_registration_date_not_title_date(self):
        html='''<tr><td><a href="/v?nttSn=12">2026. 7. 1. 실시 자체감사 결과</a></td><td>2026-10-19</td></tr>'''
        item=m.parse_posts(html,'https://a.go.kr/list')[0]
        self.assertEqual(item['posted_at'],'2026-10-19')
    def test_new_badge_not_modified(self):
        base='https://a.go.kr/list'
        a='<tr><td><a href="/v?nttSn=12">감사 결과 공개 새로운 글</a></td><td>2026-10-19</td></tr>'
        b=a.replace(' 새로운 글','')
        self.assertEqual(m.parse_posts(a,base),m.parse_posts(b,base))
    def test_soft_error_not_body(self):
        f=m.Fetcher(); f.get=Mock()
        text='요청하신 페이지를 찾을수 없습니다'
        f.get.return_value=(text.encode(),'https://a.go.kr/v','utf-8','text/html')
        with self.assertRaises(m.FetchError): f.html('https://a.go.kr/v')
    def test_two_literal_redirects(self):
        f=m.Fetcher()
        f.get=Mock(side_effect=[(b'<script>location.href="/index.do";</script>','https://a.go.kr/','utf-8','text/html'),(b'<script>location.href="/main.do";</script>','https://a.go.kr/index.do','utf-8','text/html'),(b'<p>'+b'A'*100+b'</p>','https://a.go.kr/main.do','utf-8','text/html')])
        html,url=f.html('https://a.go.kr/')
        self.assertEqual(url,'https://a.go.kr/main.do'); self.assertEqual(f.get.call_count,3)
    def test_access_gate_not_bypassed(self):
        f=m.Fetcher(); f.get=Mock(return_value=(b'<script>NetFunnel_Action({},"/main.do")</script>','https://a.go.kr/','utf-8','text/html'))
        with self.assertRaises(m.FetchError): f.html('https://a.go.kr/')
        self.assertEqual(f.get.call_count,1)
    def test_body_copy_preserves_attachments(self):
        html='<div class="bd-view__vcontent"><p>공개 감사 결과 첨부를 참고해 주세요.</p><form><a href="/download">결과.hwp</a></form></div>'
        node,soup,fallback=m.main_content(html)
        node.select_one('form').decompose()
        self.assertTrue(soup.select_one('a')); self.assertFalse(fallback)

if __name__=='__main__': unittest.main()
