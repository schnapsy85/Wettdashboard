#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path

import auto_market
import auto_market_sources


ROOT = Path(__file__).parent
LOCK_PATH = Path(os.environ.get('AUTO_MARKET_LOCK', str(ROOT / 'auto-market-refresh.lock'))).expanduser()


def run_refresh():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'skipped', 'reason': 'refresh already running'}
        return auto_market.refresh_listings(auto_market_sources.discover_candidates, auto_market_sources.fetch_listing)


if __name__ == '__main__':
    print(json.dumps(run_refresh(), ensure_ascii=False))
