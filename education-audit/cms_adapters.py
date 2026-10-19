"""Explicit, testable CMS integration layer. Never executes website JavaScript."""
import copy
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlsplit, urlencode, parse_qsl
import requests
from bs4 import BeautifulSoup, UnicodeDammit
import engine as e

_original_href = e.href_url
_original_registry = e.establish_registry
_original_process = e.process_office
_original_body = e.main_content


def href_url(a, base, soup=None):
    js = (a.get('onclick') or '') + ' ' + (a.get('href') or '')
    q = dict(parse_qsl(urlsplit(base).query))
    if urlsplit(base).hostname == 'www.moe.go.kr':
        m = re.search(r'''goView\s*\(\s*['"](\d+)['"]\s*,\s*['"](\d+)['"]\s*,\s*['"](\d+)['"]''', js)
        if m:
            q.update(boardID=m[1], boardSeq=m[2], lev=m[3], statusYN='W', opType='N')
            return urljoin(base, 'viewRenew.do') + '?' + urlencode(q)
    if '.gwe.go.kr' in urlsplit(base).netloc:
        m = re.search(r'''goView\s*\(\s*['"](\d+)['"]''', js)
        if m:
            q['bbsSn'] = m[1]
            return urljoin(base, 'view.do') + '?' + urlencode(q)
    return _original_href(a, base, soup)


def parse_posts(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    results = {}
    for row in soup.select('tr, li'):
        txt = e.compact(row.get_text(' ', strip=True))
        if len(txt) > 3500:
            continue
        # A title may contain an audit date; prefer an actual registration-date cell.
        dates = []
        for cell in row.select('td, time, .date, .regDate, .b_date'):
            value = e.compact(cell.get('datetime') or cell.get_text(' ', strip=True))
            if re.fullmatch(r'20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}\.?', value):
                dates.append(e.date_of(value))
        dt = next((x for x in dates if x), None) or e.date_of(txt)
        if not dt:
            continue
        candidates = []
        for a in row.select('a, button[data-id]'):
            label = e.compact(a.get('title') or a.get_text(' ', strip=True))
            label = re.sub(r'\s*(?:새로운 글|새글|NEW)\s*$', '', label).strip()
            url = href_url(a, base, soup)
            if not url or len(label) < 5 or e.SKIP.search(label) or e.FILEEXT.search(label):
                continue
            try:
                url = e.canonical(url)
            except ValueError:
                continue
            if e.is_post(url):
                candidates.append((len(label), label, url))
        if candidates:
            _, title, url = max(candidates)
            results[url] = dict(title=title, url=url, posted_at=dt)
    return list(results.values())


class SessionFetcher(e.Fetcher):
    """Each worker owns a session; a host lock applies across all sessions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local = threading.local()

    def _session(self):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
        return self.local.session

    def _raw(self, url, limit=e.MAX_BYTES):
        session = self._session()
        for _ in range(8):
            url = self.validate(url)
            host = urlsplit(url).hostname
            with self.lock:
                if self.requests >= self.max_requests or time.monotonic() >= self.deadline:
                    raise e.FetchError('run_budget_exhausted')
                self.requests += 1
                lock = self.host_locks.setdefault(host, threading.Lock())
            with lock:
                try:
                    with session.get(url, headers={'User-Agent':e.UA, 'Accept':'*/*'},
                                     timeout=(8,18), stream=True, allow_redirects=False) as r:
                        time.sleep(.3)
                        if r.is_redirect:
                            url = urljoin(url, r.headers.get('Location', ''))
                            continue
                        if r.status_code != 200:
                            raise e.FetchError('http_' + str(r.status_code))
                        data = bytearray()
                        for chunk in r.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > limit or time.monotonic() >= self.deadline:
                                raise e.FetchError('response_limit')
                        enc = r.encoding
                        if enc in (None, 'ISO-8859-1'):
                            enc = UnicodeDammit(bytes(data), ['utf-8','cp949']).original_encoding or 'utf-8'
                        return bytes(data), url, enc, r.headers.get('Content-Type', '')
                except requests.RequestException as err:
                    raise e.FetchError(type(err).__name__) from err
        raise e.FetchError('redirect_limit')

    def html(self, url):
        visited = set()
        for _ in range(6):
            url = e.canonical(url)
            if url in visited:
                raise e.FetchError('literal_redirect_loop')
            visited.add(url)
            raw, final, enc, content_type = self.get(url)
            text = raw.decode(enc, errors='replace')
            soup = BeautifulSoup(text, 'html.parser')
            visible = soup.get_text(' ', strip=True)
            if re.search(r'요청하신 페이지를 찾을|유효하지 않은 요청|접근이 차단|비정상적인 접근', visible[:1200]):
                raise e.FetchError('soft_error_page')
            if len(visible) < 80:
                # Do not skip access gates, queueing services, CAPTCHA or other JS challenges.
                if re.search(r'NetFunnel_Action|captcha|challenge', text, re.I):
                    raise e.FetchError('browser_or_access_gate_required')
                match = re.search(r'''(?:location(?:\.href)?\s*=|location\.replace\()\s*['"]([^'"<>]+)['"]''', text)
                if not match:
                    meta = soup.find('meta', attrs={'http-equiv':re.compile('refresh', re.I)})
                    match = re.search(r'url\s*=\s*[\'\"]?([^\'\"<>;]+)', meta.get('content', '') if meta else '', re.I)
                if match:
                    url = urljoin(final, match[1].strip())
                    continue
            if 'html' not in content_type and '<' not in text[:200]:
                raise e.FetchError('not_html')
            return text, final
        raise e.FetchError('literal_redirect_limit')


def main_content(html):
    soup = BeautifulSoup(html, 'html.parser')
    # Deleting a form from the body must not destroy attachment links in the full page.
    for selector in ('.bd-view__vcontent', '.view_cont', '.bbsV_cont', '.bbs_view_cont',
                     '.bbs_contents', '.view_con', '.bbsV_conts', '.nttCn', '#nttCn',
                     '#bbsCn', '.board_view_con', '.viewContent', '.cont_view', '.bbsViewCont'):
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) >= 10:
            return copy.copy(node), soup, False
    node, _, fallback = _original_body(html)
    return copy.copy(node), soup, fallback


def establish_registry(config, state, fetcher):
    config = copy.deepcopy(config)
    overrides = Path(__file__).with_name('verified_sources.json')
    extra = json.loads(overrides.read_text(encoding='utf-8')) if overrides.exists() else {}
    for region in config['regions']:
        change = extra.get('regions', {}).get(region['id'], {})
        for key in ('home', 'directories', 'boards'):
            if key in change:
                region[key] = change[key]
        region.setdefault('child_homes', {}).update(change.get('child_homes', {}))
    config['moe']['boards'] = list(dict.fromkeys(config['moe']['boards'] + extra.get('moe_boards', [])))
    offices = _original_registry(config, state, fetcher)
    # Parser changes must not be misreported as revisions to official documents.
    if state.get('parser_revision') != 2:
        for bs in state.get('board_state', {}).values():
            for record in bs.get('posts', {}).values():
                record['title'] = re.sub(r'\s*(?:새로운 글|새글|NEW)\s*$', '', record.get('title', '')).strip()
                record.pop('content_hash', None)
        state['parser_revision'] = 2
    return offices


def process_office(office, state, fetcher, config):
    health, events, initial = _original_process(office, state, fetcher, config)
    return health, events, initial


def install():
    e.POSTKEYS = tuple(dict.fromkeys(e.POSTKEYS + ('bbsSn',)))
    e.href_url = href_url
    e.parse_posts = parse_posts
    e.Fetcher = SessionFetcher
    e.main_content = main_content
    e.establish_registry = establish_registry
    e.process_office = process_office
