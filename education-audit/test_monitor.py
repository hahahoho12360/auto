import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import monitor as m

class MonitorTests(unittest.TestCase):
    def test_date(self):
        self.assertEqual(m.date_of('등록 2026. 10. 19.'),'2026-10-19')
        self.assertIsNone(m.date_of('2026-02-31'))
    def test_canonical(self):
        self.assertEqual(m.canonical('https://EXAMPLE.go.kr/a?b=2&a=1&utm_source=x#t'),'https://example.go.kr/a?a=1&b=2')
    def test_reject_scheme(self):
        for x in ['file:///etc/passwd','javascript:alert(1)','http://user:pass@example.com','https://example.com:1234/']:
            with self.assertRaises(ValueError): m.canonical(x)
    def test_ssrf(self):
        f=m.Fetcher(); f.trust('http://127.0.0.1/')
        with self.assertRaises(m.FetchError): f.validate('http://127.0.0.1/')
    def test_untrusted(self):
        f=m.Fetcher(); f.trust('https://www.moe.go.kr/')
        with self.assertRaises(m.FetchError): f.validate('https://moe.go.kr.attacker.com/')
    def test_baseline_not_new(self):
        self.assertEqual(m.changed(None,'x'),'baseline')
        self.assertEqual(m.changed('x','x'),'unchanged')
        self.assertEqual(m.changed('x','y'),'modified')
    def test_table_link(self):
        h='<table><tr><td>1</td><td><a href="/view?nttSn=100">종합감사 결과 공개</a></td><td>2026-10-19</td><td>100</td></tr></table>'
        p=m.parse_posts(h,'https://www.moe.go.kr/list')
        self.assertEqual(len(p),1); self.assertEqual(p[0]['title'],'종합감사 결과 공개')
    def test_view_count_ignored(self):
        h='<tr><td><a href="/view?nttSn=100">종합감사 결과 공개</a></td><td>2026-10-19</td><td>{}</td></tr>'
        self.assertEqual(m.parse_posts(h.format(1),'https://a.go.kr/list'),m.parse_posts(h.format(300),'https://a.go.kr/list'))
    def test_js_adapter(self):
        h='''<table><tr><td><a href="#" onclick="view('12345')">종합감사 결과 공개</a></td><td>2026-10-19</td></tr></table>'''
        p=m.parse_posts(h,'https://www.moe.go.kr/boardCnts/listRenew.do?boardID=345')
        self.assertIn('boardSeq=12345',p[0]['url'])
    def test_no_js_execution(self):
        h='''<tr><td><a href="javascript:fetch('https://evil.com')">감사 결과 자료</a></td><td>2026-10-19</td></tr>'''
        self.assertEqual(m.parse_posts(h,'https://a.go.kr/list'),[])
    def test_shared_site_boundary(self):
        self.assertTrue(m.same_site('https://www.gbe.kr/pohe/na/ntt/selectNttList.do','https://www.gbe.kr/pohe/main.do'))
        self.assertFalse(m.same_site('https://www.gbe.kr/gumi/na/ntt/selectNttList.do','https://www.gbe.kr/pohe/main.do'))
    def test_source_counts(self):
        c=json.loads(Path(__file__).with_name('config.json').read_text())
        self.assertEqual(len(c['regions']),16)
        self.assertEqual(sum(len(r['children']) for r in c['regions']),176)
    def test_privacy(self):
        x=m.public_excerpt('개인연락 010-1234-5678 메일 test@example.com 및 861024-1234567 환수 조치 필요합니다')
        self.assertNotIn('test@example.com',x); self.assertNotIn('1234567',x)
    def test_hwpx(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('Contents/section0.xml','<root xmlns:h="urn:test"><h:t>종합감사 시정 요구</h:t></root>')
        r=m.extract_bytes(buf.getvalue(),'test.hwpx')
        self.assertIn('종합감사 시정 요구',r['text'])
    def test_xml_entities_blocked(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('Contents/section0.xml','<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>')
        with self.assertRaises(Exception): m.extract_bytes(buf.getvalue(),'test.hwpx')
    def test_archive_not_silently_empty(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z: z.writestr('unread.hwp','anything')
        with self.assertRaises(ValueError): m.extract_bytes(buf.getvalue(),'data.zip')
    def test_document_classification(self):
        c=m.classify('2026 자체 종합감사 결과')
        self.assertEqual(c['relationship'],'자체·내부'); self.assertEqual(c['audit_type'],'종합감사')
    def test_unknown_page_not_healthy(self):
        f=unittest.mock.Mock(); f.html.return_value=('<html>Not a bulletin board</html>','https://a.go.kr/list')
        o={'id':'test','name':'시험기관','kind':'시도교육청','home':'https://a.go.kr/','seed_boards':['https://a.go.kr/list']}
        h,ev,init=m.process_office(o,{},f,{'max_list_pages':3,'max_details_per_board':12})
        self.assertEqual(h['status'],'failed'); self.assertEqual(ev,[])
        self.assertIn('list_parser_unverified',h['boards'][0]['issues'])
    def test_registered_empty_board(self):
        f=unittest.mock.Mock(); f.html.return_value=('<html>등록된 게시물이 없습니다</html>','https://a.go.kr/list')
        o={'id':'test','name':'시험기관','kind':'시도교육청','home':'https://a.go.kr/','seed_boards':['https://a.go.kr/list']}
        h,ev,init=m.process_office(o,{},f,{'max_list_pages':3,'max_details_per_board':12})
        self.assertEqual(h['status'],'listing_checked')
    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.json'; m.write_json(p,{'가':1})
            self.assertEqual(json.loads(p.read_text()),{'가':1})
    def test_baseline_then_new(self):
        f=unittest.mock.Mock()
        first='<table><tr><td><a href="/view?nttSn=100">종합감사 결과 공개</a></td><td>2026-10-18</td></tr></table>'
        second=first+'<table><tr><td><a href="/view?nttSn=101">특정감사 결과 공개</a></td><td>2026-10-19</td></tr></table>'
        o={'id':'test','name':'시험기관','kind':'시도교육청','home':'https://a.go.kr/','seed_boards':['https://a.go.kr/list']}
        c={'max_list_pages':3,'max_details_per_board':0}; state={}
        f.html.return_value=(first,'https://a.go.kr/list')
        _,ev,_=m.process_office(o,state,f,c); self.assertEqual(ev,[])
        f.html.return_value=(second,'https://a.go.kr/list')
        _,ev,_=m.process_office(o,state,f,c)
        self.assertEqual(len(ev),1); self.assertEqual(ev[0]['kind'],'new')
        _,ev,_=m.process_office(o,state,f,c); self.assertEqual(ev,[])

if __name__=='__main__': unittest.main()
