import argparse
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import gzip
import numpy as np
from netCDF4 import Dataset
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import downloader as d


def fixture(path, times=(0,1), variable='precip'):
    with Dataset(path, 'w') as nc:
        for name, values in [('time',times),('latitude',[-1,1]),('longitude',[0,1])]:
            nc.createDimension(name,len(values))
            v=nc.createVariable(name,'f8',(name,))
            v[:]=values
            if name=='time':v.units='days since 2000-01-01'
        nc.createVariable(variable,'f4',('time','latitude','longitude'))[:]=1


class Blob:
    def __init__(self,name,data=b''):
        self.name=name;self.data=data;self.generation=1;self.metadata={};self.md5_hash='';self.patches=0
    def patch(self,**kwargs): self.patches+=1
    def upload_from_filename(self,path,**kwargs):
        self.data=Path(path).read_bytes();self.md5_hash=base64.b64encode(hashlib.md5(self.data).digest()).decode()
        self.generation+=1


class FakeStore:
    def __init__(self):self.objects={};self.states={};self.bucket=self
    def get(self,name):return self.objects.get(name)
    def blob(self,name):
        if name not in self.objects:self.objects[name]=Blob(name)
        return self.objects[name]
    def state(self,name):return self.states.get(name,{})
    def put_state(self,name,data):self.states[name]=data
    @contextlib.contextmanager
    def lock(self,suite):yield


class WorkflowTests(unittest.TestCase):
    def test_regional_secret_endpoint_and_integrity(self):
        from unittest.mock import MagicMock
        import google_crc32c
        data=b'{"test":"key"}'
        response=MagicMock(status_code=200)
        response.json.return_value={'payload': {'data':base64.b64encode(data).decode(), 'dataCrc32c':str(google_crc32c.value(data))}}
        session=MagicMock();session.get.return_value=response
        resource='projects/project/locations/us-east4/secrets/keys/versions/latest'
        with patch.dict(d.os.environ,{'ERDDAP_FLAG_SECRET':resource}),patch.object(d.os,'getenv',side_effect=lambda k,default=None: resource if k=='ERDDAP_FLAG_SECRET' else default),patch('google.auth.default',return_value=(object(),'project')),patch('google.auth.transport.requests.AuthorizedSession') as factory:
            factory.return_value.__enter__.return_value=session
            self.assertEqual(d.load_flag_keys(),{'test':'key'})
            self.assertEqual(session.get.call_args.args[0],f'https://secretmanager.us-east4.rep.googleapis.com/v1/{resource}:access')
            response.json.return_value['payload']['dataCrc32c']='0'
            with self.assertRaisesRegex(RuntimeError,'checksum mismatch'):d.load_flag_keys()
    def test_source_fingerprint_requires_validation(self):
        h={'source_url':'x','source_length':'3','source_etag':'etag','source_last_modified':''}
        self.assertFalse(d.source_matches(h,h))
        self.assertTrue(d.source_matches({**h,'validated':'netcdf-v1'},h))
        self.assertFalse(d.source_matches({**h,'validated':'netcdf-v1'}, {**h,'source_etag':'new'}))
    def test_no_validators_never_skips(self):
        h={'source_url':'x','source_length':'3','source_etag':'','source_last_modified':''}
        self.assertFalse(d.source_matches({**h,'validated':'netcdf-v1'},h))
    def test_validate_and_reject_duplicate_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test.nc';fixture(p)
            self.assertEqual(d.validate(p,'precip')['time_count'],'2')
            fixture(p,(0,0))
            with self.assertRaisesRegex(RuntimeError,'Non-monotonic'):d.validate(p,'precip')
    def test_reject_missing_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test.nc';fixture(p)
            with self.assertRaisesRegex(RuntimeError,'missing required'):d.validate(p,'sst')
    def test_gzip_and_plain_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x';p.write_bytes(gzip.compress(b'CDF payload'))
            self.assertEqual(d.unpack_if_needed(p).read_bytes(),b'CDF payload')
            p.write_bytes(b'CDF payload');self.assertEqual(d.unpack_if_needed(p),p)
    def test_discovery_ignores_subdirectories_and_unrelated_files(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return b'<a href="by_month/">x</a><a href="chirps-v2.0.2026.days_p05.nc">x</a><a href="by_month/chirps-v2.0.2025.days_p05.nc">x</a><a href="https://bad.test/chirps-v2.0.2024.days_p05.nc">x</a>'
        product={'source':'https://source.test/data/','regex':r'chirps-v2\.0\.\d{4}\.days_p05\.nc','dataset_id':'test'}
        with patch.object(d,'request',return_value=Response()):
            self.assertEqual(d.discover(product),[('https://source.test/data/chirps-v2.0.2026.days_p05.nc','chirps-v2.0.2026.days_p05.nc')])
    def integration(self,flag_failure=False):
        store=FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);src=tmp/'source.nc';fixture(src)
            config=tmp/'products.json';config.write_text(json.dumps([{'dataset_id':'test','source':'https://source.test/file.nc','filename':'file.nc','prefix':'test/','variable':'precip','gzip':False}]))
            keys=tmp/'keys.json';keys.write_text('{"test":"private-key"}')
            args=argparse.Namespace(suite='chirps',config=str(config),bucket='test-bucket',dataset=None,plan_only=False,no_flags=False,force=False)
            h={'source_url':'https://source.test/file.nc','source_length':str(src.stat().st_size),'source_etag':'version1','source_last_modified':'date'}
            def download(url,path,headers):Path(path).write_bytes(src.read_bytes());return headers
            with patch.object(d,'Store',return_value=store),patch.object(d,'source_headers',return_value=h),patch.object(d,'download',side_effect=download) as get,patch.object(d,'flag') as flag,patch.dict(d.os.environ,{'ERDDAP_FLAG_JSON':str(keys),'ERDDAP_BASE_URL':'https://erddap.test/erddap'}):
                if flag_failure:
                    flag.side_effect=RuntimeError('failed')
                    with self.assertRaises(RuntimeError):d.run(args)
                    self.assertTrue(next(iter(store.states.values()))['pending_flag'])
                    flag.side_effect=None
                else:d.run(args)
                self.assertEqual(get.call_count,1)
                d.run(args)
                self.assertEqual(get.call_count,1) # Fingerprint prevents a second download.
                self.assertFalse(next(iter(store.states.values()))['pending_flag'])
                self.assertEqual(flag.call_count,2 if flag_failure else 1)
    def test_publish_flag_and_unchanged_rerun(self):self.integration()
    def test_pending_flag_retried_without_redownload(self):self.integration(True)


if __name__=='__main__':unittest.main()
