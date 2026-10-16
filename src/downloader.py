#!/usr/bin/env python3
"""Verified HTTP -> GCS producers; existing objects are never deleted."""
import argparse
import base64
import contextlib
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.parse import urljoin, urlsplit, unquote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

LOG = logging.getLogger('downloads')
CHUNK = 8 * 1024 * 1024
MAX_BYTES = int(os.getenv('MAX_LOCAL_BYTES', str(12 * 1024**3)))
TIMEOUT = 180


def request(url, method='GET', headers=None):
    for attempt in range(4):
        try:
            return urlopen(Request(url, method=method, headers={
                'User-Agent': 'NOAA-ERD-SourceDownloads/1.0',
                'Accept-Encoding': 'identity', **(headers or {})}), timeout=TIMEOUT)
        except (HTTPError, URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in [408, 429, 500, 502, 503, 504]:
                raise
            if attempt == 3:
                raise
            time.sleep(min(2**attempt, 8))


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.extend(v for k, v in attrs if k == 'href' and v)


def discover(product):
    if 'filename' in product:
        return [(product['source'], product['filename'])]
    with request(product['source']) as response:
        parser = Links()
        parser.feed(response.read(4 * 1024 * 1024).decode('utf-8'))
    result = {}
    base = urlsplit(product['source'])
    for href in parser.hrefs:
        url = urljoin(product['source'], href)
        parts = urlsplit(url)
        name = unquote(parts.path.rsplit('/', 1)[-1])
        # Use only the exact top-level filenames in Roy's XML. No by_month/byYear.
        if (parts.scheme == 'https' and parts.netloc == base.netloc
                and parts.path.rsplit('/', 1)[0] == base.path.rstrip('/')
                and not parts.query and re.fullmatch(product['regex'], name)):
            result[name] = url
    if not result:
        raise RuntimeError('No matching files in source listing: ' + product['dataset_id'])
    return [(url, name) for name, url in sorted(result.items())]


def source_headers(url):
    with request(url, 'HEAD') as r:
        size = int(r.headers.get('Content-Length', '0'))
        return {'source_url': url, 'source_etag': r.headers.get('ETag', ''),
                'source_last_modified': r.headers.get('Last-Modified', ''),
                'source_length': str(size)}


def source_matches(saved, current):
    # Require a source validator plus a previous validated NetCDF, not just size.
    return (bool(current['source_etag'] or current['source_last_modified'])
            and saved.get('validated') == 'netcdf-v1'
            and all(saved.get(k) == v for k, v in current.items()))


def download(url, path, expected):
    for attempt in range(4):
        try:
            with request(url) as r, open(path, 'wb') as out:
                actual_headers = {'source_url': url, 'source_etag': r.headers.get('ETag', ''),
                                  'source_last_modified': r.headers.get('Last-Modified', ''),
                                  'source_length': r.headers.get('Content-Length', '0')}
                # HEAD and GET must refer to the same source revision.
                for key in ['source_etag', 'source_last_modified', 'source_length']:
                    if expected.get(key) not in ['', '0'] and actual_headers.get(key) != expected[key]:
                        raise RuntimeError('Source changed between HEAD and GET')
                count = 0
                while True:
                    data = r.read(CHUNK)
                    if not data:
                        break
                    count += len(data)
                    if count > MAX_BYTES:
                        raise RuntimeError('Source exceeds MAX_LOCAL_BYTES; increase job memory first')
                    out.write(data)
                length = int(actual_headers['source_length'])
                if not count or (length and count != length):
                    raise RuntimeError(f'Incomplete download: {count} / {length}')
            return actual_headers
        except Exception:
            Path(path).unlink(missing_ok=True)
            if attempt == 3:
                raise
            LOG.warning('Download retry %s for %s', attempt + 1, url)
            time.sleep(min(2**attempt, 8))


def unpack_if_needed(path):
    with open(path, 'rb') as f:
        compressed = f.read(2) == b'\x1f\x8b'
    if not compressed:
        return path  # Met Office documents .gz URLs sometimes returning uncompressed data.
    output = Path(str(path) + '.nc')
    count = 0
    with gzip.open(path, 'rb') as src, open(output, 'wb') as dst:
        while True:
            data = src.read(CHUNK)
            if not data:
                break
            count += len(data)
            if count + Path(path).stat().st_size > MAX_BYTES:
                raise RuntimeError('Compressed + decompressed files exceed local byte budget')
            dst.write(data)
    Path(path).unlink()
    return output


def validate(path, variable):
    import numpy as np
    from netCDF4 import Dataset, num2date
    with Dataset(path) as nc:
        required = ['time', 'latitude', 'longitude', variable]
        if any(name not in nc.variables for name in required):
            raise RuntimeError('NetCDF missing required variables: ' + ', '.join(required))
        axes = {}
        for name in ['time', 'latitude', 'longitude']:
            v = nc.variables[name]
            if v.ndim != 1 or not len(v):
                raise RuntimeError('Invalid coordinate: ' + name)
            arr = v[:]
            if np.ma.getmaskarray(arr).any() or not np.isfinite(arr).all():
                raise RuntimeError('Missing coordinate: ' + name)
            delta = np.diff(arr)
            if len(delta) and not (np.all(delta > 0) or (name != 'time' and np.all(delta < 0))):
                raise RuntimeError('Non-monotonic coordinate: ' + name)
            axes[name] = v
        data = nc.variables[variable]
        if data.dimensions != tuple(axes[n].dimensions[0] for n in ['time','latitude','longitude']):
            raise RuntimeError('Unexpected data dimensions: ' + str(data.dimensions))
        # Read one value at both time ends to catch invalid variable access without loading a grid.
        data[0, 0, 0]
        data[-1, -1, -1]
        t = axes['time']
        dates = num2date(t[[0, -1]], t.units, calendar=getattr(t, 'calendar', 'standard'))
        return {'time_start': dates[0].isoformat(), 'time_end': dates[-1].isoformat(),
                'time_count': str(len(t)), 'validated': 'netcdf-v1'}


def digests(path):
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            md5.update(block)
            sha.update(block)
    return base64.b64encode(md5.digest()).decode(), sha.hexdigest()


class Store:
    def __init__(self, bucket, state_prefix=None):
        from google.cloud import storage
        self.bucket = storage.Client().bucket(bucket)
        self.state_prefix = (state_prefix or os.getenv(
            'STATE_PREFIX', 'operations/workflow-state/source-downloads')).strip('/')
        if not self.state_prefix or any(part.startswith('_') for part in self.state_prefix.split('/')):
            raise ValueError('STATE_PREFIX must use a non-underscore bucket path')

    def state_path(self, suite, name):
        return f'{self.state_prefix}/{suite}/{name}'

    def get(self, name):
        return self.bucket.get_blob(name)

    def state(self, name):
        blob = self.get(name)
        return json.loads(blob.download_as_text()) if blob else {}

    def put_state(self, name, data):
        blob = self.get(name)
        target = self.bucket.blob(name)
        target.upload_from_string(json.dumps(data), content_type='application/json',
                                  if_generation_match=int(blob.generation) if blob else 0)
        return target

    @contextlib.contextmanager
    def lock(self, suite):
        from google.api_core.exceptions import PreconditionFailed
        name = self.state_path(suite, 'lock.json')
        old = self.get(name)
        if old and old.updated > datetime.now(timezone.utc) - timedelta(hours=25):
            raise RuntimeError('Another execution holds the suite lock; retry after it finishes')
        lock = self.bucket.blob(name)
        try:
            lock.upload_from_string(json.dumps({'execution': os.getenv('CLOUD_RUN_EXECUTION','local')}),
                                    if_generation_match=int(old.generation) if old else 0)
        except PreconditionFailed as exc:
            raise RuntimeError('Another execution acquired the suite lock') from exc
        try:
            yield
        finally:
            lock.delete(if_generation_match=int(lock.generation))


def flag(dataset_id, key):
    base = os.environ.get('ERDDAP_BASE_URL', '').rstrip('/')
    if not base or not key:
        raise RuntimeError('ERDDAP_BASE_URL or dataset flag key is missing')
    context = None
    if os.getenv('ERDDAP_VERIFY_TLS', 'true').lower() == 'false':
        context = ssl._create_unverified_context()
    url = base + '/setDatasetFlag.txt?' + urlencode({'datasetID': dataset_id, 'flagKey': key})
    # Do not include the URL/key in errors or logs.
    try:
        with urlopen(url, timeout=60, context=context) as response:
            body = response.read(65536).decode('utf-8', errors='replace').strip()
            if response.status != 200 or 'success' not in body.lower():
                raise RuntimeError('Flag response did not acknowledge success')
    except Exception:
        raise RuntimeError('ERDDAP flag failed for ' + dataset_id) from None
    LOG.info('ERDDAP flag acknowledged: %s', dataset_id)


def load_flag_keys():
    local = os.getenv('ERDDAP_FLAG_JSON')
    if local:
        return json.loads(Path(local).read_text())
    resource = os.environ.get('ERDDAP_FLAG_SECRET', '')
    match = re.fullmatch(r'projects/[^/]+/locations/([^/]+)/secrets/[^/]+/versions/[^/]+', resource)
    if not match:
        raise RuntimeError('Set ERDDAP_FLAG_SECRET to the regional secret version resource')
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    import google_crc32c
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    with AuthorizedSession(credentials) as session:
        response = session.get(f'https://secretmanager.{match[1]}.rep.googleapis.com/v1/{resource}:access', timeout=60)
        if response.status_code != 200:
            raise RuntimeError('Regional flag-secret access failed; HTTP ' + str(response.status_code))
        payload = response.json()['payload']
        data = base64.b64decode(payload['data'], validate=True)
        if 'dataCrc32c' not in payload or google_crc32c.value(data) != int(payload['dataCrc32c']):
            raise RuntimeError('Secret payload checksum mismatch')
        return json.loads(data)


def run(args):
    import yaml
    root = Path(__file__).resolve().parents[1]
    settings = yaml.safe_load(Path(os.getenv('ROYLIB_CONFIG', str(root / 'config/config.yml'))).read_text())
    args.bucket = args.bucket or settings['output_bucket']
    configured = Path(settings['suites'][args.suite]['products'])
    if not configured.exists():
        configured = root / 'config' / configured.name
    products = json.loads(Path(args.config or configured).read_text())
    if args.dataset:
        products = [p for p in products if p['dataset_id'] in args.dataset]
        if len(products) != len(set(args.dataset)):
            raise ValueError('Unknown dataset selection')
    if args.plan_only:
        for p in products:
            files = discover(p)
            LOG.info('PLAN %s: %s matching files -> gs://%s/%s', p['dataset_id'], len(files), args.bucket, p['prefix'])
            for url, name in files:
                h = source_headers(url)
                LOG.info('SOURCE %s bytes=%s modified=%s', name, h['source_length'], h['source_last_modified'])
        return
    store = Store(args.bucket)
    keys = {} if args.no_flags else load_flag_keys()
    if not args.no_flags:
        # Shared secrets may contain datasetID -> key or {datasetID: {flagKey: key}}.
        keys = keys.get('flagKeys', keys)
        for p in products:
            value = keys.get(p['dataset_id'])
            if isinstance(value, dict):
                value = value.get('flagKey')
            if not isinstance(value, str) or not value:
                raise RuntimeError('Missing flag key for ' + p['dataset_id'])
            keys[p['dataset_id']] = value
        if not os.getenv('ERDDAP_BASE_URL'):
            raise RuntimeError('ERDDAP_BASE_URL required')
    errors = []
    with store.lock(args.suite):
        for p in products:
            id = p['dataset_id']
            pending_path = store.state_path(args.suite, f'{id}.json')
            try:
                for url, name in discover(p):
                    target = p['prefix'] + name
                    if p['gzip'] and store.get(target + '.gz'):
                        raise RuntimeError('Both .nc and .nc.gz would match ERDDAP; review existing object ' + target + '.gz before publishing')
                    blob = store.get(target)
                    oldmeta = (blob.metadata or {}) if blob else {}
                    headers = source_headers(url)
                    if not args.force and source_matches(oldmeta, headers):
                        LOG.info('Unchanged source: %s', name)
                        continue
                    # HEAD is optional length-wise, but reject known oversized files immediately.
                    if int(headers['source_length']) > MAX_BYTES:
                        raise RuntimeError('Source too large for local byte budget: ' + name)
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / 'download'
                        actual = download(url, path, headers)
                        path = unpack_if_needed(path) if p['gzip'] else path
                        coverage = validate(path, p['variable'])
                        if oldmeta.get('time_end') and coverage['time_end'] < oldmeta['time_end']:
                            raise RuntimeError('Refusing source time-coverage regression: ' + name)
                        md5, sha = digests(path)
                        same = bool(blob and (blob.md5_hash == md5 or oldmeta.get('sha256') == sha))
                        metadata = {**oldmeta, **actual, **coverage, 'sha256': sha}
                        if same:
                            blob.metadata = metadata
                            blob.patch(if_generation_match=int(blob.generation))
                            LOG.info('Content unchanged; source fingerprint recorded: %s', name)
                        else:
                            # Persist reload intent before upload, so a crash cannot lose flagging.
                            store.put_state(pending_path, {'pending_flag': True})
                            out = store.bucket.blob(target)
                            out.metadata = metadata
                            out.upload_from_filename(str(path), content_type='application/x-netcdf',
                                                     if_generation_match=int(blob.generation) if blob else 0,
                                                     checksum='crc32c', timeout=TIMEOUT)
                            LOG.info('Published %s (%s to %s)', target, coverage['time_start'], coverage['time_end'])
            except Exception as exc:
                LOG.error('%s: %s', id, exc)
                errors.append(id)
            # Also retry a flag left pending by a previous failed execution.
            if store.state(pending_path).get('pending_flag') and not args.no_flags:
                try:
                    flag(id, keys[id])
                    store.put_state(pending_path, {'pending_flag': False})
                except Exception as exc:
                    LOG.error('%s', exc)
                    errors.append(id + ':flag')
    if errors:
        raise RuntimeError('Failed datasets: ' + ', '.join(errors))


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['chirps','hadisst'], required=True)
    parser.add_argument('--bucket', default=os.getenv('OUTPUT_BUCKET'))
    parser.add_argument('--config')
    parser.add_argument('--dataset', action='append')
    parser.add_argument('--plan-only', action='store_true', help='Source discovery and HEAD only; no GCS/flags')
    parser.add_argument('--force', action='store_true', help='Re-download even with matching source metadata')
    parser.add_argument('--no-flags', action='store_true', help='For isolated test buckets only; reload intent retained')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run(args)


if __name__ == '__main__':
    cli()
