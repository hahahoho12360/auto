#!/usr/bin/env python3
"""Public education-audit change monitor. No paid API, credentials or email sending.
State is persisted on a dedicated Git branch. Mail is a separate scheduled task.
Never treats an inaccessible board, unknown parser or budget exhaustion as no news.
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo
import zipfile
import zlib
import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo('Asia/Seoul')
UA = 'EducationAuditMonitor/1.0 (+https://github.com/hahahoho12360/auto/tree/main/education-audit)'
DATE = re.compile(r'(?<!\d)(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})(?!\d)')
AUDIT = re.compile(r'감사(?!합니다|드립니다|인사|의\s*말씀)|처분\s*요구|공직\s*기강|부패\s*공직|청렴|시정\s*요구')
BOARD = re.compile(r'감사.*(공개|결과|자료|계획|사례|현황|정보)|(?:종합|특정|자체|내부|일상|재무|복무)\s*감사|처분\s*요구')
MENU = re.compile(r'감사|청렴|사전정보|정보공개|사이트맵|전체메뉴|누리집지도')
POSTKEYS = ('nttSn','nttId','boardSeq','q_bbsDocNo','dataSid','articleId','seq','idx','no','dataNo')
FILEEXT = re.compile(r'\.(pdf|hwpx|hwp|zip|txt|docx)(?:\b|$)', re.I)
SKIP = re.compile(r'로그인|회원가입|신고|상담|참여|설문|인쇄|스크랩|만족도|개인정보|저작권')
MAX_BYTES = 12 * 1024 * 1024
MAX_EXPAND = 32 * 1024 * 1024

def compact(s):
    return re.sub(r'\s+', ' ', str(s)).strip()

def digest(s):
    if not isinstance(s, bytes):
        s = json.dumps(s, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(s).hexdigest()

def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('HTTP(S) public URL required')
    if p.port not in (None, 80, 443):
        raise ValueError('Nonstandard port rejected')
    drop = {'utm_source','utm_medium','utm_campaign','fbclid','gclid','jsessionid'}
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', urlencode(sorted(q)), ''))

def date_of(text):
    m = DATE.search(text)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).date().isoformat()
    except ValueError:
        return None

def changed(previous, current):
    if previous is None:
        return 'baseline'
    return 'modified' if previous != current else 'unchanged'

def is_post(url):
    p = urlsplit(url)
    q = dict(parse_qsl(p.query))
    return any(q.get(k) for k in POSTKEYS) or bool(re.search(r'/(?:view|article)/\d+', p.path))

def href_url(a, base, soup=None):
    raw = a.get('href') or a.get('value') or ''
    js = ' '.join([raw, a.get('onclick', '')])
    if raw and not raw.startswith(('javascript:', '#')):
        return urljoin(base, raw)
    # Never execute source JavaScript; accept only literal URLs and narrow CMS adapters.
    literal = re.search(r'''["']((?:https?://|/)[^"'<>\s]+)["']''', js)
    if literal:
        return urljoin(base, literal.group(1))
    q = dict(parse_qsl(urlsplit(base).query))
    if soup:
        for inp in soup.select('input[name][value]'):
            if inp['name'] in ('bbsId','mi','q_bbsSn','boardID','s','m'):
                q.setdefault(inp['name'], inp['value'])
    if 'moe.go.kr' in urlsplit(base).netloc:
        m = re.search(r'''(?:view|View)[\w]*\s*\(\s*["']?(\d{4,})''', js)
        if m and q.get('boardID'):
            q['boardSeq'] = m.group(1)
            return urljoin(base, 'viewRenew.do') + '?' + urlencode(q)
    ntt = a.get('data-id') or a.get('data-nttsn') or a.get('data-ntt-sn')
    if ntt and str(ntt).isdigit() and 'selectNttList.do' in base:
        q['nttSn'] = str(ntt)
        return urlsplit(base)._replace(path=urlsplit(base).path.replace('selectNttList.do','selectNttInfo.do'), query=urlencode(q), fragment='').geturl()
    m = re.search(r'''(?:jsView|fnView|viewBbs|bbsView)\s*\(\s*["'](\d{8,})''', js)
    if m and q.get('q_bbsSn'):
        return urljoin(base, 'BD_selectBbs.do') + '?' + urlencode({'q_bbsSn':q['q_bbsSn'], 'q_bbsDocNo':m.group(1)})
    return None

def links(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for a in soup.select('a, option[value]'):
        label = compact(a.get_text(' ', strip=True) or a.get('title',''))
        u = href_url(a, base, soup)
        if not u or not label:
            continue
        try:
            result.append((label, canonical(u)))
        except ValueError:
            pass
    return list(dict.fromkeys(result))

def parse_posts(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    results = {}
    for row in soup.select('tr, li'):
        txt = compact(row.get_text(' ', strip=True))
        dt = date_of(txt)
        if not dt or len(txt) > 3500:
            continue
        candidates = []
        for a in row.select('a, button[data-id]'):
            label = compact(a.get_text(' ', strip=True) or a.get('title',''))
            u = href_url(a, base, soup)
            if not u or len(label) < 5 or SKIP.search(label) or FILEEXT.search(label):
                continue
            try:
                u = canonical(u)
            except ValueError:
                continue
            if not is_post(u):
                continue
            candidates.append((len(label), label, u))
        if candidates:
            _, title, url = max(candidates)
            results[url] = {'title':title, 'url':url, 'posted_at':dt}
    return list(results.values())

def page_links(html, base):
    result = []
    for label, url in links(html, base):
        if (re.fullmatch(r'\d{1,3}', label) or label in ('다음','다음 페이지')) and not is_post(url):
            if urlsplit(url).netloc == urlsplit(base).netloc and urlsplit(url).path == urlsplit(base).path:
                result.append(url)
    return result

class FetchError(RuntimeError):
    pass

class Fetcher:
    def __init__(self, max_requests=2500, seconds=1000):
        self.max_requests = max_requests
        self.deadline = time.monotonic() + seconds
        self.requests = 0
        self.lock = threading.Lock()
        self.host_locks = {}
        self.robots = {}
        self.cache = {}
        self.trusted = set()

    def trust(self, url):
        h = urlsplit(url).hostname
        if h:
            with self.lock:
                self.trusted.add(h.removeprefix('www.'))

    def validate(self, url):
        url = canonical(url)
        h = urlsplit(url).hostname
        base = h.removeprefix('www.')
        with self.lock:
            trusted = any(base == x or base.endswith('.' + x) for x in self.trusted)
        if not trusted:
            raise FetchError('untrusted_domain')
        try:
            ips = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
        except OSError as e:
            raise FetchError('dns_failure') from e
        if not ips or any(not ipaddress.ip_address(x[4][0]).is_global for x in ips):
            raise FetchError('nonpublic_address')
        return url

    def _raw(self, url, limit=MAX_BYTES):
        for redirect in range(6):
            url = self.validate(url)
            host = urlsplit(url).hostname
            with self.lock:
                if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
                    raise FetchError('run_budget_exhausted')
                self.requests += 1
                host_lock = self.host_locks.setdefault(host, threading.Lock())
            with host_lock:
                try:
                    with requests.get(url, headers={'User-Agent':UA, 'Accept':'*/*'}, timeout=(6,18), stream=True, allow_redirects=False) as r:
                        time.sleep(0.25)
                        if r.is_redirect:
                            url = urljoin(url, r.headers.get('Location',''))
                            continue
                        if r.status_code != 200:
                            raise FetchError('http_' + str(r.status_code))
                        data = bytearray()
                        for chunk in r.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > limit or time.monotonic() >= self.deadline:
                                raise FetchError('response_limit')
                        encoding = r.encoding
                        if encoding in (None, 'ISO-8859-1'):
                            from bs4 import UnicodeDammit
                            encoding = UnicodeDammit(bytes(data), ['utf-8','cp949']).original_encoding or 'utf-8'
                        return bytes(data), url, encoding, r.headers.get('Content-Type','')
                except requests.RequestException as e:
                    raise FetchError(type(e).__name__) from e
        raise FetchError('redirect_limit')

    def allowed(self, url):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        with self.lock:
            policy = self.robots.get(origin)
        if policy is None:
            try:
                raw, _, enc, _ = self._raw(origin + '/robots.txt', 256000)
                policy = RobotFileParser()
                policy.parse(raw.decode(enc, errors='replace').splitlines())
            except FetchError as e:
                policy = True if str(e) in ('http_404','http_410') else False
            with self.lock:
                self.robots[origin] = policy
        return policy if isinstance(policy, bool) else policy.can_fetch('EducationAuditMonitor', url)

    def get(self, url, cache=True):
        url = canonical(url)
        with self.lock:
            hit = self.cache.get(url)
        if cache and hit:
            return hit
        if not self.allowed(url):
            raise FetchError('robots_denied_or_unavailable')
        res = self._raw(url)
        if cache and len(res[0]) < 2000000:
            with self.lock:
                self.cache[url] = res
        return res

    def html(self, url):
        raw, final, enc, ctype = self.get(url)
        text = raw.decode(enc, errors='replace')
        if len(BeautifulSoup(text, 'html.parser').get_text(strip=True)) < 80:
            m = re.search(r'''(?:url\s*=\s*|(?:location(?:\.href)?\s*=|location\.replace\()\s*["'])([^"'<>;\s)]+)''', text, re.I)
            if m:
                target = urljoin(final, m.group(1))
                if canonical(target) != canonical(final):
                    raw, final, enc, ctype = self.get(target)
                    text = raw.decode(enc, errors='replace')
        if 'html' not in ctype and '<' not in text[:200]:
            raise FetchError('not_html')
        if re.search(r'captcha|access denied|접근이 차단|비정상적인 접근', text[:10000], re.I):
            raise FetchError('access_restricted')
        return text, final

def same_site(url, homepage):
    a, b = urlsplit(url), urlsplit(homepage)
    if a.hostname.removeprefix('www.') != b.hostname.removeprefix('www.'):
        return False
    prefix = b.path.strip('/').split('/')[0]
    if prefix and prefix not in ('main.do','index.do','index.jsp','main','www','goe','web') and '.' not in prefix:
        return a.path.startswith('/' + prefix + '/')
    return True

def establish_registry(config, state, fetcher):
    offices = state.setdefault('offices', {})
    for region in config['regions']:
        rid = region['id']
        root = offices.setdefault(rid, {'id':rid, 'name':region['name'], 'kind':'시도교육청','home':region['home'],'boards':[]})
        root['home'] = region['home']
        root['directory_sources'] = region.get('directories', [])
        root['seed_boards'] = region.get('boards', [])
        for name in region['children']:
            oid = rid + '-' + name
            offices.setdefault(oid, {'id':oid,'name':name+'교육지원청','region':region['name'],'parent':rid,'kind':'교육지원청','home':None,'boards':[]})
        for name, home in region.get('child_homes', {}).items():
            obj = offices.get(rid + '-' + name)
            if obj:
                obj['home'] = home
                obj['home_source'] = region.get('directories', [region['home']])[0]
    offices.setdefault('moe', {'id':'moe','name':'교육부','kind':'교육부','home':config['moe']['home'],'boards':[]})
    offices['moe']['seed_boards'] = config['moe']['boards']
    for office in offices.values():
        if office.get('home'):
            fetcher.trust(office['home'])
    for region in config['regions']:
        for url in [region['home']] + region.get('directories', []):
            fetcher.trust(url)

    def discover_region(region):
        rid = region['id']
        missing = [x for x in offices.values() if x.get('parent') == rid and not x.get('home')]
        if not missing:
            return
        queue = list(dict.fromkeys(region.get('directories', []) + [region['home']]))
        visited = set()
        while queue and len(visited) < 5 and missing:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                html, final = fetcher.html(url)
                for label, link in links(html, final):
                    norm = re.sub(r'\s+', '', label)
                    if '교육지원청' in norm:
                        for office in missing[:]:
                            short = office['name']
                            if norm.endswith(short) and len(norm) <= len(short)+16:
                                office['home'] = link
                                office['home_source'] = final
                                fetcher.trust(link)
                                missing.remove(office)
                        if not is_post(link) and same_site(link, region['home']) and '안내' in norm:
                            queue.append(link)
                offices[rid]['registry_error'] = None
            except Exception as e:
                offices[rid]['registry_error'] = str(e)[:160]
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(discover_region, config['regions']))
    return offices

def discover_boards(office, fetcher, limit=8):
    existing = list(dict.fromkeys(office.get('seed_boards', []) + office.get('boards', [])))
    if existing:
        return existing
    home = office.get('home')
    if not home:
        raise FetchError('homepage_unresolved')
    queue = [(home, 0)]
    visited = set()
    boards = []
    while queue and len(visited) < limit:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html, final = fetcher.html(url)
        except FetchError:
            if len(visited) == 1:
                raise
            continue
        entries = sorted(links(html, final), key=lambda x: (not bool(BOARD.search(x[0])), len(x[0])))
        for label, link in entries:
            if not same_site(link, home) or is_post(link) or SKIP.search(label) or len(label)>70:
                continue
            if BOARD.search(label):
                if link not in boards:
                    boards.append(link)
            elif MENU.search(label) and depth < 2:
                queue.append((link, depth+1))
        if depth > 0 and parse_posts(html, final) and AUDIT.search(compact(BeautifulSoup(html, 'html.parser').title or '')):
            boards.append(final)
        if len(boards) >= 8:
            break
    office['boards'] = list(dict.fromkeys(boards))[:8]
    return office['boards']

def bounded_inflate(raw):
    d = zlib.decompressobj(-15)
    result = d.decompress(raw, MAX_EXPAND + 1)
    if len(result)>MAX_EXPAND or d.unconsumed_tail:
        raise ValueError('decompression_limit')
    return result

def extract_bytes(raw, name):
    """Subprocess extraction: no macros, no OCR, bounded pages and ZIP expansion."""
    out = []
    if raw.startswith(b'%PDF'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError('encrypted_pdf')
        count = len(reader.pages)
        for n, page in enumerate(reader.pages[:200], 1):
            out.append(f'[PDF {n}쪽] {page.extract_text() or ""}')
        status = 'text_extracted' if count<=200 else 'partial_page_limit'
    elif raw[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        import olefile
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            header = ole.openstream('FileHeader').read()
            flags = struct.unpack_from('<I', header, 36)[0]
            if flags & (2|4):
                raise ValueError('encrypted_or_distributed_hwp')
            paths = sorted((x for x in ole.listdir() if len(x)==2 and x[0]=='BodyText' and x[1].startswith('Section')), key=lambda x:int(x[1][7:]))
            for path in paths:
                payload = ole.openstream(path).read(MAX_EXPAND+1)
                if len(payload)>MAX_EXPAND:
                    raise ValueError('hwp_section_limit')
                data = bounded_inflate(payload) if flags&1 else payload
                pos=0
                while pos+4<=len(data):
                    head=struct.unpack_from('<I',data,pos)[0]; pos+=4
                    tag,size=head&1023,head>>20
                    if size==4095:
                        if pos+4>len(data): break
                        size=struct.unpack_from('<I',data,pos)[0]; pos+=4
                    if pos+size>len(data): raise ValueError('truncated_hwp_record')
                    chunk=data[pos:pos+size]; pos+=size
                    if tag==67:
                        units=list(struct.unpack('<'+'H'*(len(chunk)//2),chunk[:len(chunk)//2*2])); i=0; text=[]
                        while i<len(units):
                            c=units[i]
                            if c in (10,13): text.append('\n')
                            elif c==9: text.append(' '); i+=7
                            elif c in (1,2,3,4,5,6,7,8,11,12,14,15,16,17,18,19,20,21,22,23): i+=7
                            elif c>=32: text.append(chr(c))
                            i+=1
                        out.append(f'[HWP {path[1]}] '+''.join(text))
        status='text_extracted_no_page_map'
    elif zipfile.is_zipfile(io.BytesIO(raw)):
        from defusedxml import ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            members=z.infolist()
            if sum(x.file_size for x in members)>MAX_EXPAND or len(members)>2500:
                raise ValueError('zip_expansion_limit')
            sections=sorted((x for x in members if re.fullmatch(r'Contents/section\d+\.xml',x.filename)), key=lambda x:int(re.search(r'\d+', x.filename)[0]))
            if not sections:
                sections=[x for x in members if x.filename=='word/document.xml']
            if not sections:
                raise ValueError('archive_unexpanded_manual_review')
            for part in sections:
                tree=ET.fromstring(z.read(part))
                text=' '.join(x.text for x in tree.iter() if x.tag.rsplit('}',1)[-1] in ('t','text') and x.text)
                out.append(f'[{part.filename}] {text}')
        status='text_extracted_no_page_map'
    elif name.lower().endswith('.txt'):
        out=[raw.decode('utf-8',errors='replace')]; status='text_extracted'
    else:
        raise ValueError('unsupported_attachment')
    text='\n'.join(out).encode('utf-8',errors='replace').decode('utf-8')
    if not re.search(r'[가-힣A-Za-z]{3}', re.sub(r'\[[^\]]*\]', '', text)):
        raise ValueError('no_text_or_scanned_document')
    if len(text)>400000:
        text=text[:400000]; status='partial_text_limit'
    return {'text':text,'status':status}

def extract_sandbox(raw,name):
    suffix=Path(name).suffix[:12] or '.bin'
    with tempfile.TemporaryDirectory() as td:
        file=Path(td)/('input'+suffix); file.write_bytes(raw)
        try:
            run=subprocess.run([sys.executable,__file__,'--extract',str(file)],capture_output=True,text=True,timeout=25)
            if run.returncode:
                return {'text':'','status':'extract_failed'}
            return json.loads(run.stdout)
        except (subprocess.TimeoutExpired,ValueError):
            return {'text':'','status':'extract_timeout_or_invalid'}

def main_content(html):
    soup=BeautifulSoup(html,'html.parser')
    for selector in ('.bbsV_cont','.bbs_view_cont','.bbs_contents','.view_con','.view_cont','.bbsV_conts','.nttCn','#nttCn','#bbsCn','.board_view_con','.viewContent','.cont_view','.bbsViewCont','article'):
        node=soup.select_one(selector)
        if node and len(node.get_text(strip=True))>=10:
            return node,soup,False
    node=soup.select_one('#contents, #content, main')
    return node or soup,soup,True

def public_excerpt(text,limit=480):
    text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[메일주소 생략]', text)
    text=re.sub(r'(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)', '[식별번호 생략]', text)
    text=re.sub(r'(?<!\d)0\d{1,2}[- ]\d{3,4}[- ]\d{4}(?!\d)', '[전화번호 생략]', text)
    sentences=[compact(x) for x in re.split(r'\n|(?<=[다요음임])\.', text) if len(compact(x))>12]
    scored=sorted(enumerate(sentences), key=lambda x:(-len(re.findall(r'부적정|부당|미흡|소홀|환수|회수|시정|징계|주의|개선|위반|처분|계약|시설|인건비|예산',x[1])),x[0]))
    chosen=sorted(scored[:3])
    return ' / '.join(x[1][:220] for x in chosen)[:limit]

def classify(title):
    return {
        'relationship':'자체·내부' if re.search(r'자체|내부',title) else '원문 확인 필요',
        'audit_type':next((x for x in ['국정감사','행정사무감사','종합감사','특정감사','재무감사','일상감사','복무감사'] if x in title),'기타 감사자료'),
        'document_type':next((x for x in ['조치결과','이행결과','처분','결과','계획','사례','매뉴얼'] if x in title),'기타'),
    }

def detail(post, fetcher, old=None):
    html,final=fetcher.html(post['url'])
    node,soup,fallback=main_content(html)
    for x in node.select('script,style,nav,header,footer,form,.satisfaction,.view_count,.hit'):
        x.decompose()
    body=node.get_text('\n',strip=True)
    body=re.sub(r'(?:조회\s*수|조회|열람)\s*[:：]?\s*[\d,]+','',body)
    attachments=[]; seen=set()
    for a in soup.select('a[href]'):
        label=compact(a.get_text(' ',strip=True) or a.get('title',''))
        url=href_url(a,final,soup)
        if not url:
            continue
        if FILEEXT.search(label) or re.search(r'down(?:load)?(?:File)?|fileDown',url,re.I):
            try:
                url=canonical(url)
            except ValueError:
                continue
            if url in seen: continue
            seen.add(url)
            attachments.append({'name':label[:160] or '첨부파일','url':url})
    fingerprint_parts=[post['title'],compact(body)]
    texts=[]
    old_files={x['url']:x for x in (old or {}).get('attachments',[])}
    for a in attachments[:6]:
        try:
            raw,finalfile,_,_=fetcher.get(a['url'],cache=False)
            a['sha256']=digest(raw)
            prev=old_files.get(a['url'],{})
            if prev.get('sha256')==a['sha256'] and prev.get('excerpt'):
                a.update({k:prev[k] for k in ('excerpt','status') if k in prev})
            else:
                extracted=extract_sandbox(raw,a['name'])
                a['status']=extracted['status']
                a['excerpt']=public_excerpt(extracted['text'])
            if a.get('excerpt'): texts.append(a['excerpt'])
            fingerprint_parts.append([a['url'],a['sha256']])
        except Exception as e:
            a['status']='attachment_unread:'+str(e)[:60]
            if old_files.get(a['url'],{}).get('sha256'):
                a['sha256']=old_files[a['url']]['sha256']
                fingerprint_parts.append([a['url'],a['sha256']])
            else:
                fingerprint_parts.append([a['url'],'unread'])
    for a in attachments[6:]: a['status']='attachment_limit_unread'
    if fallback:
        excerpt='게시글 본문 영역을 확정하지 못했습니다. 원문 확인이 필요합니다.'
        body_hash=None
    else:
        excerpt=public_excerpt(body)
        body_hash=digest(fingerprint_parts)
    complete=not fallback and all(a.get('status','').startswith('text_extracted') for a in attachments)
    return {'url':final,'content_hash':body_hash,'excerpt':' / '.join(texts)[:650] or excerpt,'attachments':attachments,'read_status':'본문·첨부 텍스트 확인' if complete and attachments else ('본문 확인' if not attachments and not fallback else '일부만 확인'),'body_selector_fallback':fallback}

def process_office(office,state,fetcher,config):
    health={'id':office['id'],'institution':office['name'],'region':office.get('region'),'kind':office['kind'],'home':office.get('home'),'status':'unverified','boards':[],'issues':[],'listed_posts':0,'details_checked':0}
    events=[]; initial=[]
    store=state.setdefault('board_state',{})
    try:
        boards=discover_boards(office,fetcher)
        if not boards:
            raise FetchError('audit_board_unresolved')
        for board in boards:
            bh={'url':board,'status':'unverified','issues':[]}
            health['boards'].append(bh)
            bs=store.setdefault(board,{'initialized':False,'posts':{}})
            posts={}; visited=set(); queue=[board]; raw_count=0
            try:
                while queue and len(visited)<config['max_list_pages']:
                    page=queue.pop(0)
                    if page in visited: continue
                    visited.add(page)
                    html,final=fetcher.html(page)
                    parsed=parse_posts(html,final)
                    raw_count+=len(parsed)
                    for p in parsed:
                        if AUDIT.search(p['title']): posts[p['url']]=p
                    if not parsed:
                        if re.search(r'등록된\s*(?:게시물|자료|글)이?\s*없|게시물이 없습니다',html):
                            bh['empty_board']=True
                        else:
                            hs=BeautifulSoup(html,'html.parser')
                            bh['parser_hints']=[{'label':compact(a.get_text(' ',strip=True))[:100],'href':a.get('href'),'onclick':a.get('onclick'),'data_id':a.get('data-id')} for a in hs.select('a') if AUDIT.search(a.get_text())][:8]
                            raise FetchError('list_parser_unverified')
                    if bs['initialized'] and all(x['url'] in bs['posts'] for x in parsed): break
                    queue.extend(x for x in page_links(html,final) if x not in visited)
                if queue and len(visited)>=config['max_list_pages']:
                    bh['issues'].append('pagination_limit_older_pages_unchecked')
                health['listed_posts']+=len(posts)
                today=datetime.now(TZ).date().isoformat()
                ordered=sorted(posts.values(),key=lambda p:p['posted_at'] or '',reverse=True)
                priority=sorted(ordered,key=lambda p:(bool(bs['posts'].get(p['url'],{}).get('last_detail_check')),bs['posts'].get(p['url'],{}).get('last_detail_check','')))
                detail_targets={p['url'] for p in priority[:config['max_details_per_board']]}
                for index,p in enumerate(ordered):
                    prev=bs['posts'].get(p['url'])
                    change='baseline' if not bs['initialized'] else ('new' if prev is None else changed(digest([prev.get('title'),prev.get('posted_at')]),digest([p['title'],p['posted_at']])) )
                    record={**(prev or {}),**p,'last_listed':today}
                    record.setdefault('first_seen',datetime.now(TZ).isoformat())
                    if p['url'] in detail_targets:
                        try:
                            d=detail(p,fetcher,prev)
                            health['details_checked']+=1
                            if change=='unchanged' and d.get('content_hash') and prev.get('content_hash') and d['content_hash']!=prev['content_hash']:
                                change='modified'
                            record.update(d)
                            record['url']=p['url']
                            record['last_detail_check']=datetime.now(TZ).isoformat()
                            record['retry_detail']=False
                            if d['read_status']=='일부만 확인': bh['issues'].append('some_content_unread')
                        except Exception as e:
                            record['read_status']='목록만 확인'
                            record['excerpt']='본문·첨부 확인 실패: '+str(e)[:100]
                            record['retry_detail']=True
                            bh['issues'].append('detail_fetch_failed')
                    else:
                        record.setdefault('read_status','목록만 확인')
                        record.setdefault('excerpt','처리 상한으로 본문·첨부 미확인. 원문 링크를 확인해 주세요.')
                        bh['issues'].append('detail_limit_unchecked')
                    bs['posts'][p['url']]=record
                    event={k:record.get(k) for k in ('title','url','posted_at','first_seen','excerpt','read_status','attachments')}
                    event.update({'institution':office['name'],'region':office.get('region'),'board_url':board,'kind':change,**classify(p['title'])})
                    event['event_id']=digest([p['url'],record.get('content_hash'),p['title'],p['posted_at']])[:24]
                    if change in ('new','modified'): events.append(event)
                    elif change=='baseline' and p['posted_at']>= (datetime.now(TZ).date()-timedelta(days=14)).isoformat(): initial.append(event)
                bs['initialized']=True
                bs['last_success']=datetime.now(TZ).isoformat()
                bh['status']='listing_checked'
                bh['raw_posts']=raw_count
                bh['audit_posts']=len(posts)
                bh['issues']=sorted(set(bh['issues']))
            except Exception as e:
                bh['status']='failed'; bh['issues'].append(str(e)[:140])
        done=sum(b['status']=='listing_checked' for b in health['boards'])
        health['status']='listing_checked' if done==len(health['boards']) else ('partial' if done else 'failed')
        if done and any(b['issues'] for b in health['boards']): health['status']='partial'
        if not done: office['boards']=[]
    except Exception as e:
        health['status']='failed'; health['issues'].append(str(e)[:160])
    return health,events,initial

def write_json(path,obj):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)

def report_markdown(report):
    h=report['coverage']; out=[f"# 전국 교육감사 변경 점검 | {report['report_date']}",'',f"수집 시작: {report['started_at']} / 종료: {report['finished_at']}",f"대상 {h['target_institutions']}기관 · 목록 확인 {h['listing_checked']} · 일부 확인 {h['partial']} · 미확인/실패 {h['failed']}",'','**범위 주의:** 등록 공개 경로의 최근 목록을 점검했습니다. 전국 모든 웹페이지·오래된 첨부 교체까지 확인했다는 의미가 아닙니다.','',f"신규 {h['new']}건 / 수정 {h['modified']}건",'','## 신규·수정 자료']
    if not report['events']: out.append('확인된 신규·수정 자료가 없습니다. 확인 실패 기관의 자료 유무는 판단할 수 없습니다.')
    for e in report['events']:
        out += ['',f"### [{e['kind']}] {e['institution']} | {e['title']}",f"게시일: {e['posted_at']} · {e['audit_type']} · {e['document_type']} · {e.get('read_status')}",f"원문: {e['url']}",f"자동 발췌(판단·해석 아님): {e.get('excerpt') or '본문 미확인'}"]
        for a in e.get('attachments') or []:
            out.append(f"첨부: {a['name']} — {a['url']} ({a.get('status')})")
    if report['initial_reference']:
        out += ['','## 최초 관측 참고자료 — 신규 발생 건수에 미포함']
        for e in report['initial_reference'][:15]: out.append(f"- {e['institution']} | {e['posted_at']} | {e['title']} | {e['url']}")
    out += ['','## 미확인·부분 확인 기관']
    for x in report['institutions']:
        if x['status']=='listing_checked': continue
        reasons=x['issues']+[r for b in x['boards'] for r in b['issues']]
        out.append(f"- {x.get('region') or ''} {x['institution']}: {', '.join(sorted(set(reasons)))}")
    out += ['','## 처리기록',f"report_id: {report['report_id']}",'보고서는 이메일 발송 성공을 뜻하지 않습니다. 발송 여부는 별도 Gmail 보낸편지함으로 확인합니다.']
    return '\n'.join(out)+'\n'

def run(args):
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    state_path=out/'state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'schema':1}
    started=datetime.now(TZ)
    fetcher=Fetcher(config['max_http_requests'],config['max_run_seconds'])
    offices=establish_registry(config,state,fetcher)
    selected=list(offices.values())
    if args.limit: selected=selected[:args.limit]
    results=[]
    with cf.ThreadPoolExecutor(max_workers=config['workers']) as pool:
        futures={pool.submit(process_office,o,state,fetcher,config):o for o in selected}
        for future in cf.as_completed(futures):
            result=future.result(); results.append(result)
            x=result[0]
            print(f"{x['id']} {x['status']} boards={len(x['boards'])} posts={x['listed_posts']} details={x['details_checked']}",flush=True)
    health=sorted([x[0] for x in results],key=lambda x:x['id'])
    events={}
    for _,es,_ in results:
        for e in es:
            events.setdefault(e['event_id'],e)
    initial=[e for _,_,es in results for e in es]
    finished=datetime.now(TZ)
    report={'schema':1,'report_date':finished.date().isoformat(),'started_at':started.isoformat(),'finished_at':finished.isoformat(),'events':list(events.values()),'initial_reference':initial,'institutions':health,'scope':{'mode':'daily_changes_only','monthly_job':False,'max_list_pages':config['max_list_pages'],'max_details_per_board':config['max_details_per_board'],'historical_files_full_scan':False},'coverage':{'target_institutions':len(offices),'attempted_institutions':len(health),'listing_checked':sum(x['status']=='listing_checked' for x in health),'partial':sum(x['status']=='partial' for x in health),'failed':sum(x['status']=='failed' for x in health),'new':sum(x['kind']=='new' for x in events.values()),'modified':sum(x['kind']=='modified' for x in events.values()),'http_requests':fetcher.requests},'billing':{'paid_api_calls':0,'email_sent':False}}
    report['report_id']=digest([report['report_date'],report['finished_at'],sorted(events)])[:24]
    state['last_run_at']=finished.isoformat()
    write_json(state_path,state)
    write_json(out/'registry.json',{'generated_at':finished.isoformat(),'expected':193,'offices':list(offices.values())})
    dayfile=out/'reports'/f"{report['report_date']}.json"
    if dayfile.exists():
        older=json.loads(dayfile.read_text(encoding='utf-8'))
        merged={e['event_id']:e for e in older.get('events',[])}; merged.update(events)
        report['events']=list(merged.values())
        report['coverage']['new']=sum(e['kind']=='new' for e in report['events'])
        report['coverage']['modified']=sum(e['kind']=='modified' for e in report['events'])
    write_json(dayfile,report); write_json(out/'latest.json',report)
    (out/'latest.md').write_text(report_markdown(report),encoding='utf-8')
    (out/'reports'/f"{report['report_date']}.md").write_text(report_markdown(report),encoding='utf-8')
    cutoff=(finished.date()-timedelta(days=31)).isoformat()
    for p in (out/'reports').iterdir():
        if p.stem<cutoff: p.unlink()
    write_json(out/'report-index.json',{'latest_report_date':report['report_date'],'reports':[{'date':p.stem,'path':str(p.relative_to(out))} for p in sorted((out/'reports').glob('*.json'))]})
    print('COVERAGE '+json.dumps(report['coverage'],ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--out',default='audit-data')
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--extract')
    args=parser.parse_args()
    if args.extract:
        try:
            p=Path(args.extract); result=extract_bytes(p.read_bytes(),p.name)
        except Exception as e:
            result={'text':'','status':'unread:'+type(e).__name__+':'+str(e)[:80]}
        print(json.dumps(result,ensure_ascii=False))
    else:
        raise SystemExit(run(args))
