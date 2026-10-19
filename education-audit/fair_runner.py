"""Two-phase, fair-budget collection: all listings before expensive attachments."""
from __future__ import annotations
import concurrent.futures as cf
from datetime import datetime, timedelta
from itertools import zip_longest
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import engine as e


class FairFetcher(e.Fetcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.circuit_locks = {}
        self.transport_failures = {}

    def _raw(self, url, limit=e.MAX_BYTES):
        host = urlsplit(url).hostname
        with self.lock:
            gate = self.circuit_locks.setdefault(host, threading.Lock())
        with gate:
            if time.monotonic() >= self.deadline:
                raise e.FetchError('run_budget_exhausted')
            count, reason = self.transport_failures.get(host, (0, ''))
            if count >= 2:
                raise e.FetchError('host_circuit_open:' + reason)
            try:
                result = super()._raw(url, limit)
                self.transport_failures[host] = (0, '')
                return result
            except e.FetchError as err:
                if str(err) in ('ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'SSLError', 'dns_failure'):
                    self.transport_failures[host] = (count + 1, str(err))
                raise

    def allowed(self, url):
        if time.monotonic() >= self.deadline:
            raise e.FetchError('run_budget_exhausted')
        return super().allowed(url)


def fair_order(offices):
    """Ministry and provincial offices first, then round-robin districts by region."""
    roots = [x for x in offices.values() if x['kind'] != '교육지원청']
    roots.sort(key=lambda x: (x['id'] != 'moe', x['id']))
    groups = {}
    for office in offices.values():
        if office['kind'] == '교육지원청':
            groups.setdefault(office.get('parent', ''), []).append(office)
    streams = [sorted(v, key=lambda x: x['id']) for _, v in sorted(groups.items())]
    districts = [x for row in zip_longest(*streams) for x in row if x]
    return roots + districts


def enrich_candidates(health, state, events, baseline):
    """Allocate enrichment round-robin; new records precede older unknown records."""
    events_by_url = {x['url']: x for x in events + baseline}
    groups = []
    for item in health:
        candidates = []
        for board in item['boards']:
            if board['status'] != 'listing_checked':
                continue
            bs = state['board_state'].get(board['url'], {})
            for url, record in bs.get('posts', {}).items():
                if record.get('last_listed') != datetime.now(e.TZ).date().isoformat():
                    continue
                candidates.append((item, board, record))
        def key(candidate):
            rec = candidate[2]
            event = events_by_url.get(rec['url'], {})
            return (event.get('kind') not in ('new', 'modified'), bool(rec.get('last_detail_check')), rec.get('last_detail_check', ''), -(int((rec.get('posted_at') or '2000-01-01').replace('-', ''))))
        candidates.sort(key=key)
        groups.append(candidates)
    return [x for row in zip_longest(*groups) for x in row if x]


def run(args):
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'schema': 1}
    started = datetime.now(e.TZ)
    fetcher = FairFetcher(config['max_http_requests'], min(420, config['max_run_seconds']))
    offices = e.establish_registry(config, state, fetcher)
    selected = fair_order(offices)
    if args.limit:
        selected = selected[:args.limit]
    list_config = {**config, 'max_details_per_board': 0}
    health, events, baseline = [], {}, []

    # No attachments are read until all institutions have had a listing attempt.
    with cf.ThreadPoolExecutor(max_workers=min(12, config.get('workers', 8))) as pool:
        futures = {pool.submit(e.process_office, o, state, fetcher, list_config): o for o in selected}
        for future in cf.as_completed(futures):
            item, changes, initial = future.result()
            health.append(item)
            for event in changes:
                events[event['event_id']] = event
            baseline.extend(initial)
            print(f"LIST {item['id']} {item['status']} boards={len(item['boards'])} posts={item['listed_posts']}", flush=True)
    listing_finished = datetime.now(e.TZ)
    e.write_json(state_path, state)
    e.write_json(out / 'listing-checkpoint.json', {'finished_at': listing_finished.isoformat(), 'institutions': health, 'events': list(events.values())})

    # Reserve a separate bounded enrichment phase, without extending the listing phase.
    fetcher.deadline = time.monotonic() + 100
    all_candidates = enrich_candidates(health, state, list(events.values()), baseline)
    candidates = all_candidates[:32]
    detail_by_url = {}

    def one_detail(candidate):
        institution, board, record = candidate
        old_hash = record.get('content_hash')
        try:
            data = e.detail(record, fetcher, record)
            return candidate, data, old_hash, None
        except Exception as err:
            return candidate, None, old_hash, str(err)[:160]

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for candidate, data, old_hash, error in pool.map(one_detail, candidates):
            institution, board, record = candidate
            if error:
                board['issues'].append('detail_fetch_failed:' + error)
                record['retry_detail'] = True
                continue
            source_url = record['url']
            record.update(data)
            record['url'] = source_url
            record['last_detail_check'] = datetime.now(e.TZ).isoformat()
            record['retry_detail'] = False
            institution['details_checked'] += 1
            detail_by_url[record['url']] = data
            if data['read_status'] == '일부만 확인':
                board['issues'].append('some_content_unread')
            if old_hash and data.get('content_hash') and old_hash != data['content_hash']:
                ev = {k: record.get(k) for k in ('title', 'url', 'posted_at', 'first_seen', 'excerpt', 'read_status', 'attachments')}
                ev.update({'institution': institution['institution'], 'region': institution.get('region'), 'board_url': board['url'], 'kind': 'modified', **e.classify(record['title'])})
                ev['event_id'] = e.digest([record['url'], record.get('content_hash'), record['title'], record.get('posted_at')])[:24]
                events[ev['event_id']] = ev

    for event in list(events.values()) + baseline:
        data = detail_by_url.get(event['url'])
        if data:
            event.update({k: data.get(k) for k in ('excerpt', 'read_status', 'attachments')})
    for item in health:
        for board in item['boards']:
            board['issues'] = sorted(set('detail_deferred_by_daily_budget' if x == 'detail_limit_unchecked' else x for x in board['issues']))
    health.sort(key=lambda x: x['id'])
    finished = datetime.now(e.TZ)
    coverage = {
        'target_institutions': len(offices), 'attempted_institutions': len(health),
        'listing_checked': sum(x['status'] == 'listing_checked' for x in health),
        'partial': sum(x['status'] == 'partial' for x in health),
        'failed': sum(x['status'] == 'failed' for x in health),
        # These counters explicitly measure listing coverage separately from attachment coverage.
        'institutions_with_any_listing_checked': sum(any(b['status'] == 'listing_checked' for b in x['boards']) for x in health),
        'institutions_with_all_registered_listings_checked': sum(bool(x['boards']) and all(b['status'] == 'listing_checked' for b in x['boards']) for x in health),
        'detail_checks_succeeded': sum(x['details_checked'] for x in health),
        'detail_checks_attempted': len(candidates),
        'detail_candidates_not_attempted': max(0, len(all_candidates) - len(candidates)),
        'new': sum(x['kind'] == 'new' for x in events.values()),
        'modified': sum(x['kind'] == 'modified' for x in events.values()),
        'http_requests': fetcher.requests,
    }
    report = {'schema': 2, 'report_date': finished.date().isoformat(), 'started_at': started.isoformat(), 'listing_finished_at': listing_finished.isoformat(), 'finished_at': finished.isoformat(), 'events': list(events.values()), 'initial_reference': baseline, 'institutions': health, 'coverage': coverage,
              'scope': {'mode': 'daily_changes_only', 'monthly_job': False, 'strategy': 'all_listings_before_attachments', 'max_list_pages': config['max_list_pages'], 'max_detail_checks_per_run': 32, 'historical_files_full_scan': False},
              'billing': {'paid_api_calls': 0, 'email_sent': False}}
    dayfile = out / 'reports' / (report['report_date'] + '.json')
    if dayfile.exists():
        older = json.loads(dayfile.read_text(encoding='utf-8'))
        merged = {x['event_id']: x for x in older.get('events', [])}
        merged.update(events)
        report['events'] = list(merged.values())
        report['coverage']['new'] = sum(x['kind'] == 'new' for x in merged.values())
        report['coverage']['modified'] = sum(x['kind'] == 'modified' for x in merged.values())
    report['report_id'] = e.digest([report['report_date'], report['finished_at'], sorted(x['event_id'] for x in report['events'])])[:24]
    state['last_run_at'] = finished.isoformat()
    e.write_json(state_path, state)
    e.write_json(out / 'registry.json', {'generated_at': finished.isoformat(), 'expected': 193, 'offices': list(offices.values())})
    e.write_json(dayfile, report)
    e.write_json(out / 'latest.json', report)
    md = e.report_markdown(report)
    metrics = (f"\n목록 단계: 1개 이상 등록 경로 확인 {coverage['institutions_with_any_listing_checked']}기관 / "
               f"등록 경로 모두의 목록 확인 {coverage['institutions_with_all_registered_listings_checked']}기관. "
               f"이는 모든 감사 공개 경로를 찾아냈다는 뜻이 아닙니다.\n"
               f"본문·첨부 단계: 시도 {coverage['detail_checks_attempted']}건 / 상세 응답 처리 {coverage['detail_checks_succeeded']}건 / "
               f"미시도 후보 {coverage['detail_candidates_not_attempted']}건. 첨부 미확인 상태는 각 자료에서 별도 확인합니다.\n")
    md = md.replace('\n**범위 주의:**', metrics + '\n**범위 주의:**', 1)
    (out / 'latest.md').write_text(md, encoding='utf-8')
    (out / 'reports' / (report['report_date'] + '.md')).write_text(md, encoding='utf-8')
    cutoff = (finished.date() - timedelta(days=31)).isoformat()
    for path in (out / 'reports').iterdir():
        if path.stem < cutoff:
            path.unlink()
    e.write_json(out / 'report-index.json', {'latest_report_date': report['report_date'], 'reports': [{'date': p.stem, 'path': str(p.relative_to(out))} for p in sorted((out / 'reports').glob('*.json'))]})
    print('COVERAGE ' + json.dumps(coverage, ensure_ascii=False), flush=True)
    return 0
