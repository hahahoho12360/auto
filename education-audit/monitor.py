#!/usr/bin/env python3
"""Command entry point. Generic engine and explicit CMS adapters are maintained separately."""
import argparse
from pathlib import Path
import cms_adapters
cms_adapters.install()
import discovery_fallbacks
discovery_fallbacks.install()
import recovery
recovery.install()
from engine import *  # Re-export the tested engine interface for maintenance and diagnostics.
from recovery import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--out', default='audit-data')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    raise SystemExit(run(args))
