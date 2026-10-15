#!/usr/bin/env python3
"""Prompt without echoing keys; create/update the project's regional Secret Manager secret."""
import argparse
import getpass
import json
from pathlib import Path
import subprocess
p = argparse.ArgumentParser()
p.add_argument('suite', choices=['chirps', 'hadisst'])
p.add_argument('--project', default='ggn-nmfs-swfscerddap-dev-1')
p.add_argument('--region', default='us-east4')
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
products = json.loads((root / f'config/{a.suite}.json').read_text())
keys = {}
for product in products:
    id = product['dataset_id']
    key = getpass.getpass(f'Existing ERDDAP flag key for {id}: ').strip()
    if not key:
        raise SystemExit('Missing key; no secret written')
    keys[id] = key
name = f'{a.suite}-erddap-flag-keys'
base = ['gcloud', 'secrets']
common = ['--project', a.project, '--location', a.region]
exists = subprocess.run(base + ['describe', name] + common, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
if not exists:
    subprocess.run(base + ['create', name] + common, check=True)
subprocess.run(base + ['versions', 'add', name] + common + ['--data-file=-'],
               input=json.dumps(keys).encode(), check=True)
print(f'Regional secret {name} ready. No keys written to local files.')
