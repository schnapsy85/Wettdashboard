#!/usr/bin/env python3
"""Refresh one read-only odds snapshot for the paper betting lab."""

import json

import server


result = server.refresh_betting_snapshot()
print(json.dumps({
    'status': result.get('status'),
    'snapshot_id': result.get('snapshot_id'),
    'event_count': len(result.get('events', [])),
    'reason': result.get('reason'),
}, ensure_ascii=False))
raise SystemExit(0 if result.get('status') == 'available' else 1)
