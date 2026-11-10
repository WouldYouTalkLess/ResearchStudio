"""Portable Sciverse adapter, mirrored in paper_search for standalone installs.

Use only the provider's HTTPS hosts; never include tokens or response bodies in
errors. Metadata and chunk evidence remain distinct. API shape verified against
/meta-catalog: /meta-search accepts filters/fields, not filters_advanced/year_from.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date


class SciverseError(RuntimeError):
    pass


def available():
    return bool(os.environ.get('SCIVERSE_API_TOKEN')) and os.environ.get('SCIVERSE_DISABLED') != '1'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SciverseError('Sciverse redirect refused to protect credentials')


FIELDS = ['unique_id', 'doc_id', 'metadata_type', 'title', 'abstract', 'author',
          'doi', 'publication_published_year', 'publication_published_date',
          'publication_venue_name_unified', 'publication_venue_type', 'type',
          'citation_count', 'access_oa_url', 'locations']


def title_key(value):
    return ' '.join(re.sub(r'[^\w]+', ' ', str(value or '').lower()).split())


def doi_key(value):
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:)', '', str(value or '').strip(), flags=re.I).lower()


def merge_evidence(target, other):
    """Preserve Sciverse pointers even when a different source wins dedup."""
    records = target.setdefault('sciverse_records', [])
    for item in other.get('sciverse_records') or []:
        existing = next((r for r in records if (r.get('doc_id'), r.get('unique_id')) ==
                         (item.get('doc_id'), item.get('unique_id'))), None)
        if existing is None:
            records.append(dict(item, snippets=list(item.get('snippets') or [])))
        else:
            for snippet in item.get('snippets') or []:
                if snippet not in existing.setdefault('snippets', []):
                    existing['snippets'].append(snippet)
    if not records:
        target.pop('sciverse_records', None)


def normalize(hit):
    title = str(hit.get('title') or '')
    try:
        year = int(hit.get('publication_published_year') or 0) or None
    except (ValueError, TypeError, OverflowError):
        year = None
    published = str(hit.get('publication_published_date') or '')[:10]
    try:
        parsed = date.fromisoformat(published)
        if year and parsed.year != year:
            published = ''  # provider dates and year can disagree; don't invent a precise date
    except ValueError:
        published = ''
    doi = doi_key(hit.get('doi'))
    authors = []
    for a in hit.get('author') or []:
        name = a.get('name', '') if isinstance(a, dict) else str(a)
        if name and name not in authors:
            authors.append(name)
    links = hit.get('access_oa_url') or []
    if isinstance(links, str):
        links = [links]
    url = next((u for u in links if isinstance(u, str) and u.startswith(('https://', 'http://'))), '')
    if not url and doi:
        url = 'https://doi.org/' + doi
    doc_id, unique_id = hit.get('doc_id'), hit.get('unique_id')
    evidence = {'doc_id': doc_id, 'unique_id': unique_id, 'snippets': []}
    if hit.get('chunk'):
        evidence['snippets'].append({k: hit.get(k) for k in
                                    ('chunk_id', 'offset', 'page_no', 'score', 'chunk')})
    source_id = unique_id or doc_id or doi or hashlib.sha256((title_key(title)+str(year)).encode()).hexdigest()
    record = {'title': title, 'title_norm': title_key(title), 'year': year,
              'abstract': hit.get('abstract') or '', 'authors': authors,
              'venue': hit.get('publication_venue_name_unified') or '',
              'publication_date': published or None, 'published_iso': published,
              'publication_types': hit.get('type') or [],
              'venue_type': hit.get('publication_venue_type') or '',
              'citation_count': int(hit.get('citation_count') or 0),
              'source': 'sciverse', 'source_id': source_id,
              'doi': doi or None, 'arxiv_id': None, 'url': url, 'paper_url': url,
              'sciverse_records': [evidence]}
    record['citations'] = record['citation_count']
    for link in links:
        match = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?', str(link))
        if match:
            record['arxiv_id'] = match.group(1)
            break
    return record


class Client:
    def __init__(self, timeout=30, deadline=None):
        self.token = os.environ.get('SCIVERSE_API_TOKEN', '')
        if not available():
            raise SciverseError('SCIVERSE_API_TOKEN is not configured or Sciverse is disabled')
        self.base = os.environ.get('SCIVERSE_BASE_URL', 'https://api.sciverse.space').rstrip('/')
        u = urllib.parse.urlsplit(self.base)
        if (u.scheme != 'https' or u.username or u.password or u.query or u.fragment or
                u.port not in (None, 443) or not (u.hostname == 'sciverse.space' or
                                                (u.hostname or '').endswith('.sciverse.space'))):
            raise SciverseError('SCIVERSE_BASE_URL must be an HTTPS Sciverse host')
        self.timeout, self.deadline = timeout, deadline
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, path, payload=None, params=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(2):
            timeout = self.timeout
            if self.deadline is not None:
                timeout = min(timeout, self.deadline - time.monotonic())
                if timeout <= 0:
                    raise SciverseError('Sciverse time budget exhausted')
            req = urllib.request.Request(url, data=body, headers={
                'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                'X-Sciverse-Source': 'researchstudio-skills', 'X-Request-Id': str(uuid.uuid4()),
                'User-Agent': 'ResearchStudio-Sciverse/1.0'})
            try:
                with self.opener.open(req, timeout=timeout) as response:
                    data = json.loads(response.read(4 * 1024 * 1024))
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                exc.close()
                if status in (429, 502, 503, 504) and attempt == 0:
                    wait = float(retry_after) if retry_after.replace('.', '', 1).isdigit() else 2.0
                    if wait > 10 or (self.deadline and time.monotonic() + wait >= self.deadline):
                        raise SciverseError(f'Sciverse HTTP {status}; retry budget exceeded') from None
                    time.sleep(max(wait, 1.0))
                    continue
                raise SciverseError(f'Sciverse HTTP {status}') from None
            except (urllib.error.URLError, OSError, ValueError):
                raise SciverseError('Sciverse connection failed or returned invalid JSON') from None
            if not isinstance(data, dict) or data.get('code', 'SUCCESS') != 'SUCCESS' or data.get('biz_code', 0) != 0:
                raise SciverseError('Sciverse returned an unsuccessful response')
            return data
        raise SciverseError('Sciverse request failed')

    def search(self, query, start_year, end_year, limit=10, published_only=False):
        if limit <= 0:
            return []
        filters = [{'field': 'publication_published_year', 'operator': 'FILTER_OP_GTE', 'value': start_year},
                   {'field': 'publication_published_year', 'operator': 'FILTER_OP_LTE', 'value': end_year}]
        if published_only:
            filters.append({'field': 'publication_venue_type', 'operator': 'FILTER_OP_IN',
                            'value': ['journal', 'conference']})
        hits = []
        pages = max(1, min(int(os.environ.get('SCIVERSE_MAX_PAGES', '4')), 20))
        for page in range(1, pages + 1):
            data = self.request('/meta-search', {'query': query, 'page': page,
                                'page_size': min(50, max(limit, 10)), 'fields': FIELDS, 'filters': filters})
            rows = data.get('results') or []
            hits.extend(rows)
            if not rows or page >= data.get('total_pages', page) or len(hits) >= limit:
                break
        # Semantic chunks are a supplementary recall channel, not full-text reads.
        if os.environ.get('SCIVERSE_SEMANTIC', '1') != '0':
            try:
                data = self.request('/agentic-search', {'query': query, 'top_k': min(30, limit), 'mode': 'fast'})
                semantic = data.get('hits') or []
                combined = []
                for i in range(max(len(hits), len(semantic))):
                    if i < len(hits): combined.append(hits[i])
                    if i < len(semantic): combined.append(semantic[i])
                hits = combined
            except SciverseError as exc:
                import sys
                print(f'[sciverse] semantic recall unavailable: {exc}; metadata retained', file=sys.stderr)
        papers = {}
        for hit in hits:
            p = normalize(hit)
            if not p['title'] or not p['year'] or not start_year <= p['year'] <= end_year:
                continue
            if published_only and p['venue_type'].lower() not in ('journal', 'conference'):
                continue
            key = (p['title_norm'], p['year'])
            if key in papers:
                merge_evidence(papers[key], p)
            else:
                papers[key] = p
        return list(papers.values())[:limit]

    def locate(self, paper):
        records = [r for r in paper.get('sciverse_records') or [] if r.get('doc_id')]
        if records:
            return records
        doi = doi_key(paper.get('doi'))
        title = paper.get('title') or ''
        if not doi and not title:
            return []
        filters = [{'field': 'doi', 'operator': 'FILTER_OP_EQ', 'value': doi}] if doi else []
        data = self.request('/meta-search', {'query': title if not doi else '', 'page_size': 5,
                                            'filters': filters, 'fields': FIELDS})
        return [{'doc_id': h['doc_id'], 'unique_id': h.get('unique_id'), 'snippets': []}
                for h in data.get('results') or [] if h.get('doc_id') and
                ((doi and doi_key(h.get('doi')) == doi) or
                 (not doi and title_key(h.get('title')) == title_key(title)))]

    def read_document(self, doc_id, max_pages=16):
        text, ranges, offset, complete = [], [], 0, False
        for _ in range(max_pages):
            data = self.request('/content', params={'doc_id': doc_id, 'offset': offset, 'limit': 16384})
            chunk = data.get('text')
            if not isinstance(chunk, str) or not chunk:
                raise SciverseError('Sciverse content page is empty or malformed')
            more = data.get('more')
            next_offset = data.get('next_offset')
            ranges.append({'offset': offset, 'next_offset': next_offset,
                           'bytes_returned': data.get('bytes_returned')})
            text.append(chunk)
            if more is False:
                complete = True
                break
            if more is not True or not isinstance(next_offset, int) or next_offset <= offset:
                raise SciverseError('Sciverse content cursor did not advance')
            offset = next_offset
        return {'text': '\n'.join(text), 'doc_id': doc_id, 'ranges': ranges,
                'complete': complete, 'next_offset': next_offset}
