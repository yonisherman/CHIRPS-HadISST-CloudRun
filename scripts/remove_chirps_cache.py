#!/usr/bin/env python3
"""Create a narrow XML candidate, preserving original formatting and all other text."""
import argparse
from pathlib import Path
import re
import xml.etree.ElementTree as ET
IDS = {'chirps20GlobalDailyP05','chirps20GlobalPentadP05','chirps20GlobalMonthlyP05','chirps20GlobalAnnualP05'}
p = argparse.ArgumentParser()
p.add_argument('input', help='Fresh copy of LIVE datasets.xml, after downloader verification')
p.add_argument('output', help='Candidate path; input is never modified')
a = p.parse_args()
src, dst = Path(a.input), Path(a.output)
if src.resolve() == dst.resolve():
    raise SystemExit('Input and output must differ')
raw = src.read_bytes()
oldroot = ET.fromstring(raw)
counts = {}
pattern = rb'(<dataset\b[^>]*\bdatasetID="([^"]+)"[^>]*>)(.*?)(</dataset>)'
def replace(m):
    id = m.group(2).decode()
    if id not in IDS:
        return m.group(0)
    body, n = re.subn(rb'[ \t]*<cacheFromUrl>[^<]*</cacheFromUrl>[^\S\n]*\n?', b'', m.group(3))
    counts[id] = n
    return m.group(1) + body + m.group(4)
candidate = re.sub(pattern, replace, raw, flags=re.S)
if set(counts) != IDS or any(n != 1 for n in counts.values()):
    raise SystemExit(f'Expected exactly four cacheFromUrl removals: {counts}')
newroot = ET.fromstring(candidate)
if [d.get('datasetID') for d in oldroot.iter('dataset')] != [d.get('datasetID') for d in newroot.iter('dataset')]:
    raise SystemExit('Dataset identity mismatch')
for id in IDS:
    old = next(d for d in oldroot.findall('dataset') if d.get('datasetID') == id)
    old.remove(old.find('cacheFromUrl'))
    new = next(d for d in newroot.findall('dataset') if d.get('datasetID') == id)
    # Ignore indentation differences; compare every element, attribute and nonblank value.
    signature = lambda d: [(e.tag, sorted(e.attrib.items()), (e.text or '').strip()) for e in d.iter()]
    if signature(old) != signature(new):
        raise SystemExit('Unexpected XML change for ' + id)
dst.write_bytes(candidate)
print('Candidate written; exactly four cacheFromUrl tags removed. Not installed.')
