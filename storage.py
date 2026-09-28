"""Хранилище объектов: S3 (боевое) или локальная папка (проверки на Mac). Одинаковый интерфейс:
  put(key, data, meta) · get(key) -> bytes · head(key) -> {'size', 'meta'} | None · list(prefix) -> [key]
Метаданные — маленький словарь строк (у S3 они уходят в x-amz-meta-*)."""
import json, os
from .common import atomic_write

ALLOWED = ('jobs/', 'inputs/', 'outputs/', 'results/', 'models/')
PUBLIC = ('outputs/',)                                 # только готовые картинки — публичные (как загрузки приложения)


def _check_key(key):
    if not key.startswith(ALLOWED) or '..' in key or key.startswith('/'): raise ValueError(f'недопустимый ключ: {key!r}')
    return key


class LocalStorage:
    def __init__(self, root): self.root = root

    def _p(self, key): return os.path.join(self.root, _check_key(key))

    def put(self, key, data, meta=None):
        p = self._p(key); os.makedirs(os.path.dirname(p), exist_ok=True)
        atomic_write(p + '.meta', json.dumps(meta or {}).encode()); atomic_write(p, data)

    def get(self, key):
        with open(self._p(key), 'rb') as f: return f.read()

    def download(self, key, path):
        import shutil; shutil.copyfile(self._p(key), path)

    def head(self, key):
        p = self._p(key)
        if not os.path.exists(p): return None
        meta = json.load(open(p + '.meta')) if os.path.exists(p + '.meta') else {}
        return {'size': os.path.getsize(p), 'meta': meta}

    def list(self, prefix):
        base = self._p(prefix); out = []
        for d, _, fs in os.walk(base if os.path.isdir(base) else os.path.dirname(base)):
            for f in fs:
                if f.endswith('.meta') or '.tmp-' in f: continue
                k = os.path.relpath(os.path.join(d, f), self.root)
                if k.startswith(prefix): out.append(k)
        return sorted(out)


class S3Storage:
    """Ключи внутри кода — без префикса (jobs/…, outputs/…); в бакете — под prefix (общий бакет приложения)."""
    def __init__(self, bucket, endpoint=None, region=None, prefix=''):
        self.pre = prefix
        import boto3
        from botocore.config import Config
        self.b = bucket
        self.s3 = boto3.client('s3', endpoint_url=endpoint, region_name=region,
                               config=Config(retries={'max_attempts': 8, 'mode': 'adaptive'}, connect_timeout=10, read_timeout=60))

    def put(self, key, data, meta=None):
        ct = 'image/png' if key.endswith('.png') else 'image/jpeg' if key.endswith('.jpg') else 'application/json'
        extra = {'ACL': 'public-read'} if key.startswith(PUBLIC) else {}
        self.s3.put_object(Bucket=self.b, Key=self.pre + _check_key(key), Body=data, Metadata={k: str(v) for k, v in (meta or {}).items()},
                           ContentType=ct, **extra)

    def get(self, key): return self.s3.get_object(Bucket=self.b, Key=self.pre + _check_key(key))['Body'].read()

    def download(self, key, path):
        self.s3.download_file(self.b, self.pre + _check_key(key), path)

    def head(self, key):
        try: r = self.s3.head_object(Bucket=self.b, Key=self.pre + _check_key(key))
        except self.s3.exceptions.ClientError as e:
            if e.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'): return None
            raise
        return {'size': r['ContentLength'], 'meta': r.get('Metadata', {})}

    def list(self, prefix):
        out = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.b, Prefix=self.pre + _check_key(prefix)):
            out += [o['Key'][len(self.pre):] for o in page.get('Contents', [])]
        return out


def make_storage(cfg):
    if cfg.storage == 'local': return LocalStorage(cfg.local_store)
    if not cfg.s3_bucket: raise SystemExit('GG_S3_BUCKET не задан')
    return S3Storage(cfg.s3_bucket, cfg.s3_endpoint, cfg.s3_region, cfg.s3_prefix)
