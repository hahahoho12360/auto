"""Public general-board fallbacks and conservative missing-attachment reporting."""
import re
import engine as e
import fair_runner as f

original_discover = e.discover_boards
original_detail = e.detail
original_candidates = f.enrich_candidates
GENERAL_LABEL = re.compile(r'^(?:공지사항|행정지원과|행정과|총무과|감사관|감사담당관|청렴자료실)$')


def discover_boards(office, fetcher, limit=8):
    boards = original_discover(office, fetcher, limit)
    if boards:
        return boards
    # Some district sites publish audit notices in administrative/general boards.
    # Preserve the fact that these are keyword-filtered general boards, not audit-only.
    html, final = fetcher.html(office['home'])
    candidates = []
    for label, url in e.links(html, final):
        if GENERAL_LABEL.fullmatch(e.compact(label)) and e.same_site(url, office['home']) and not e.is_post(url):
            candidates.append(url)
    office['boards'] = list(dict.fromkeys(candidates))[:3]
    if office['boards']:
        office['board_discovery_mode'] = 'general_boards_with_audit_title_filter'
    return office['boards']


def detail(post, fetcher, old=None):
    result = original_detail(post, fetcher, old)
    if result.get('attachments'):
        return result
    html, _ = fetcher.html(post['url'])
    node, _, _ = e.main_content(html)
    text = e.compact(node.get_text(' ', strip=True))
    if re.search(r'(?:붙임|첨부).{0,30}(?:참고|공개|확인)|(?:붙임|첨부)\s*파일|\.(?:hwpx?|pdf)\b', text, re.I):
        result['read_status'] = '일부만 확인'
        result['attachment_discovery_status'] = 'expected_but_unresolved'
        result['excerpt'] = '첨부 언급은 있으나 다운로드 링크를 확인하지 못했습니다. 본문 발췌: ' + result.get('excerpt', '')
    return result


def enrich_candidates(health, state, events, baseline):
    candidates = original_candidates(health, state, events, baseline)
    new_urls = {x['url'] for x in events if x.get('kind') in ('new', 'modified')}
    return sorted(candidates, key=lambda candidate: candidate[2]['url'] not in new_urls)


def install():
    e.discover_boards = discover_boards
    e.detail = detail
    f.enrich_candidates = enrich_candidates
