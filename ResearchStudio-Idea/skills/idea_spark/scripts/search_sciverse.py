"""Sciverse connector for IdeaSpark literature grounding and collision checks."""
from __future__ import annotations
import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from scripts.sciverse_client import Client


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--queries', required=True)
    ap.add_argument('--window-months', type=int, default=24)
    ap.add_argument('--window-min-months', type=int, default=0)
    ap.add_argument('--published-only', action='store_true')
    ap.add_argument('--as-of', default='')
    ap.add_argument('--max-per-query', type=int, default=40)
    ap.add_argument('--max-results', type=int, default=0)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    from scripts._time_guard import resolve_now
    from scripts._merge import interleave_by_query
    now = resolve_now(args.as_of)
    since = (now - timedelta(days=30 * args.window_months)).date()
    until = (now - timedelta(days=30 * args.window_min_months)).date()
    queries = json.loads(args.queries)
    client, per_query, succeeded = Client(), [], 0
    for query in queries:
        try:
            rows = client.search(query, since.year, until.year, args.max_per_query,
                                 published_only=args.published_only)
            rows = [r for r in rows if not r['published_iso'] or
                    since.isoformat() <= r['published_iso'] <= until.isoformat()]
            per_query.append(rows)
            succeeded += 1
        except Exception as exc:
            print(f'[sciverse] query failed: {exc}', file=sys.stderr)
            per_query.append([])
    if not succeeded:
        return 1  # do not report total failure as a successful empty source
    rows = interleave_by_query(per_query, lambda r: (r['title_norm'], r['year']),
                               args.max_results, queries=queries)
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f'[sciverse] wrote {len(rows)} papers', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
