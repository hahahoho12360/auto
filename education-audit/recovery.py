"""Bounded recovery and truthful failure diagnoses. No access-gate bypass or paid API."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import cms_adapters as cms
import engine as e
import fair_runner as fair

_original_href = cms.href_url
_original_discover = e.discover_boards
TRANSIENT = {'ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'dns_failure',
             'http_500', 'http_502', 'http_503', 'http_504'}
CATEGORIES = {
    'homepage_setup': ('홈페이지 주소 미확정', '공식 기관 안내에서 주소 확인·등록 필요'),
    'board_setup': ('감사 공개경로 미확정', '감사자료실·행정자료실 경로 확인 필요'),
    'parser_setup': ('게시판 구조 미지원', '글 보기·등록일·첨부 처리 규칙 보완 필요'),
    'address_tls': ('주소·인증서 확인 필요', '이전 주소·이동 경로·TLS 인증서 확인 필요'),
    'policy_blocked': ('자동수집 제한', '제한 우회 금지·허용된 공식 대체 경로 검토'),
    'policy_unchecked': ('자동수집 정책 확인 미완료', '정책 파일 재확인 후에만 자료 수집'),
    'browser_required': ('브라우저·접근 절차 필요', '공식 제공 방식 검토·접근 절차 우회 금지'),
    'temporary': ('통신 오류·실행 한도', '다음 실행에서 재시도·지속 시 접속환경 점검'),
    'other': ('추가 진단 필요', '구체적 오류와 원문 경로 점검'),
}


def href_url(a, base, soup=None):
    # Observed public GNE markup: onclick="nttView('3049960');".
    # This only maps the known CMS action; no JavaScript is executed.
    p = urlsplit(base)
    q = dict(parse_qsl(p.query))
    js = (a.get('onclick') or '') + ' ' + (a.get('href') or '')
    m = re.search(r'''\bnttView\s*\(\s*['"](\d+)['"]\s*\)''', js)
    if m and p.path.endswith('/na/ntt/selectNttList.do') and q.get('bbsId'):
        q['nttSn'] = m[1]
        for key in ('currentPage', 'pageIndex', 'searchValue', 'searchType'):
            q.pop(key, None)
        return p._replace(path=p.path.replace('selectNttList.do', 'selectNttInfo.do'),
                          query=urlencode(q), fragment='').geturl()
    return _original_href(a, base, soup)


def discover_boards(office, fetcher, limit=8):
    boards = _original_discover(office, fetcher, limit)
    # GNE districts often use a named integrity room instead of an audit-only board.
    # Add only actual published links; do not generate board IDs.
    if office.get('parent') != 'gne':
        return boards
    try:
        html, final = fetcher.html(office['home'])
        extra = [url for label, url in e.links(html, final)
                 if re.search(r'청렴|감사', label) and len(label) < 40
                 and urlsplit(url).path.endswith('/na/ntt/selectNttList.do')
                 and e.same_site(url, office['home'])]
        office['boards'] = list(dict.fromkeys(extra + boards))[:8]
        if extra:
            office['board_discovery_mode'] = 'published_integrity_and_general_boards_title_filter'
        return office['boards']
    except e.FetchError:
        if boards:
            return boards
        raise


class RecoveryFetcher(cms.SessionFetcher):
    """Retry transient GET failures once; isolate circuit state by institution path.

    A failed /ph/ resource must not suppress /gj/ on a shared provincial host.
    Robots-policy failures are separate from explicit disallow rules. Neither is
    ignored to inflate coverage. TLS checks and the original domain/IP checks stay on.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_failures = {}
        self.policy_locks = {}
        self.policy_cache = {}

    @staticmethod
    def scope_key(url):
        p = urlsplit(url)
        segment = p.path.strip('/').split('/')[0]
        return (p.scheme, p.netloc, segment)

    def _raw(self, url, limit=e.MAX_BYTES):
        key = self.scope_key(url)
        with self.lock:
            count, until, reason = self.source_failures.get(key, (0, 0, ''))
        if time.monotonic() >= self.deadline:
            raise e.FetchError('run_budget_exhausted')
        if count >= 2 and time.monotonic() < until:
            raise e.FetchError('source_cooldown:' + reason)
        for attempt in range(2):
            try:
                result = super()._raw(url, limit)
                with self.lock:
                    self.source_failures.pop(key, None)
                return result
            except e.FetchError as err:
                reason = str(err)
                if reason not in TRANSIENT:
                    raise
                with self.lock:
                    count = self.source_failures.get(key, (0, 0, ''))[0] + 1
                    self.source_failures[key] = (count, time.monotonic() + 30, reason)
                if attempt or time.monotonic() + 12 >= self.deadline:
                    raise
                time.sleep(0.7)

    def allowed(self, url):
        if time.monotonic() >= self.deadline:
            raise e.FetchError('run_budget_exhausted')
        p = urlsplit(url)
        origin = p.scheme + '://' + p.netloc
        with self.lock:
            guard = self.policy_locks.setdefault(origin, threading.Lock())
        with guard:
            cached = self.policy_cache.get(origin)
            if cached and cached[0] < time.monotonic():
                cached = None
            if cached is None:
                try:
                    raw, _, enc, ctype = self._raw(origin + '/robots.txt', 512 * 1024)
                    text = raw.decode(enc, errors='replace')
                    if re.search(r'<(?:html|!doctype)', text[:200], re.I):
                        raise e.FetchError('invalid_policy_response')
                    policy = RobotFileParser()
                    policy.parse(text.splitlines())
                    cached = (self.deadline + 600, policy, None)
                except e.FetchError as err:
                    code = str(err)
                    if code in ('http_404', 'http_410'):
                        cached = (self.deadline + 600, True, None)
                    else:
                        # Keep safety conservative even where RFC permits 4xx access.
                        message = ('robots_access_denied:' if code in ('http_401','http_403')
                                   else 'robots_unreachable:') + code
                        cached = (time.monotonic() + 30, None, message)
                self.policy_cache[origin] = cached
            _, policy, error = cached
            if error:
                raise e.FetchError(error)
            if policy is True or policy.can_fetch('EducationAuditMonitor', url):
                return True
            raise e.FetchError('robots_disallowed')


def failure_category(item):
    codes = item.get('issues', []) + [x for b in item.get('boards', []) for x in b.get('issues', [])]
    text = ' '.join(codes)
    if 'homepage_unresolved' in text:
        return 'homepage_setup'
    if 'robots_disallowed' in text or 'robots_access_denied' in text or re.search(r'\bhttp_(401|403)\b', text):
        return 'policy_blocked'
    if 'SSLError' in text or 'untrusted_domain' in text or 'http_404' in text:
        return 'address_tls'
    if 'robots_' in text:
        return 'policy_unchecked'
    if 'browser_or_access_gate_required' in text or 'access_restricted' in text:
        return 'browser_required'
    if 'list_parser_unverified' in text:
        return 'parser_setup'
    if 'audit_board_unresolved' in text:
        return 'board_setup'
    if any(x in text for x in ('Timeout', 'ConnectionError', 'dns_failure', 'circuit', 'cooldown', 'budget', 'http_50')):
        return 'temporary'
    return 'other'


def build_failure_summary(report, previous=None, history=None):
    history = history if history is not None else {}
    previous = previous or {}
    old = {x['id']: x for x in previous.get('institutions', [])}
    counts = Counter()
    entries, recovered, regressed = [], [], []
    checked_at = report['finished_at']
    for item in report['institutions']:
        oid = item['id']
        any_list = any(b['status'] == 'listing_checked' for b in item.get('boards', []))
        hist = history.setdefault(oid, {'consecutive_failed_runs': 0})
        if any_list:
            hist.update(last_listing_success_at=checked_at, consecutive_failed_runs=0)
            if oid in old and old[oid]['status'] == 'failed':
                recovered.append(oid)
        elif item['status'] == 'failed':
            hist['consecutive_failed_runs'] += 1
            hist.setdefault('first_failure_seen_at', checked_at)
            hist['last_failed_at'] = checked_at
            if oid in old and old[oid]['status'] != 'failed':
                regressed.append(oid)
        if item['status'] != 'failed':
            continue
        cat = failure_category(item)
        counts[cat] += 1
        codes = sorted(set(item.get('issues', []) + [s for b in item.get('boards', []) for s in b.get('issues', [])]))
        entries.append({'id':oid, 'institution':item['institution'], 'region':item.get('region'),
                        'category':cat, 'label':CATEGORIES[cat][0], 'action':CATEGORIES[cat][1],
                        'raw_errors':codes, 'consecutive_failed_runs':hist['consecutive_failed_runs'],
                        'last_listing_success_at':hist.get('last_listing_success_at')})
    return {'report_date':report['report_date'], 'checked_at':checked_at,
            'failed_institutions':len(entries), 'by_category':dict(counts),
            'recovered_since_previous_run':recovered, 'regressed_since_previous_run':regressed,
            'previous_finished_at':previous.get('finished_at'),
            'previous_failed_institutions':previous.get('coverage', {}).get('failed'),
            'entries':entries,
            'notes':['실패는 해당 실행의 결과이며 영구 수집불가 판정이 아닙니다.',
                     'consecutive_failed_runs는 실행 횟수입니다. 같은 날 재실행도 별도 횟수입니다.',
                     '주소·게시판·파서 미확정은 단순 재실행만으로 해결되지 않을 수 있습니다.',
                     '자동수집 제한을 우회하지 않으며 수집 실패와 메일 발송 실패는 별개입니다.']}


def summary_markdown(summary):
    lines = ['## 미확인 원인과 다음 조치', '',
             '**미확인은 이번 실행의 상태이며 영구적인 수집 불가를 의미하지 않습니다.**', '',
             '| 구분 | 기관 수 | 다음 조치 |', '|---|---:|---|']
    for key, number in sorted(summary['by_category'].items()):
        label, action = CATEGORIES[key]
        lines.append(f'| {label} | {number} | {action} |')
    lines += ['', f"직전 실행 대비 목록 확인 회복 {len(summary['recovered_since_previous_run'])}기관 / 새로 확인 실패 {len(summary['regressed_since_previous_run'])}기관.",
              '반복실행은 접속 상태를 다시 확인할 뿐, 미지원 게시판의 코드를 자동 수정하지 않습니다.']
    return '\n'.join(lines) + '\n'


def install():
    cms.href_url = href_url
    e.href_url = href_url
    e.discover_boards = discover_boards


def run(args):
    out = Path(args.out)
    prev_path = out / 'latest.json'
    previous = json.loads(prev_path.read_text(encoding='utf-8')) if prev_path.exists() else {}
    history_path = out / 'failure-history.json'
    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
    original_fetcher = fair.FairFetcher
    # Install only for this run, keeping compatibility with the existing fair-runner tests.
    fair.FairFetcher = RecoveryFetcher
    try:
        result = fair.run(args)
    finally:
        fair.FairFetcher = original_fetcher
    report = json.loads(prev_path.read_text(encoding='utf-8'))
    summary = build_failure_summary(report, previous, history)
    report['failure_analysis'] = summary
    e.write_json(prev_path, report)
    e.write_json(out / 'reports' / (report['report_date'] + '.json'), report)
    e.write_json(out / 'failure-summary.json', summary)
    e.write_json(history_path, history)
    diagnostic_md = summary_markdown(summary)
    path = out / 'latest.md'
    md = path.read_text(encoding='utf-8')
    md = md.replace('## 신규·수정 자료', diagnostic_md + '\n## 신규·수정 자료', 1)
    path.write_text(md, encoding='utf-8')
    (out / 'reports' / (report['report_date'] + '.md')).write_text(md, encoding='utf-8')
    (out / 'failure-summary.md').write_text(diagnostic_md, encoding='utf-8')
    print('FAILURE_SUMMARY ' + json.dumps({k:v for k,v in summary.items() if k not in ('entries','notes')}, ensure_ascii=False), flush=True)
    return result
