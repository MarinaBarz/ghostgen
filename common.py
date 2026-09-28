"""Общие мелочи: проверка id товара, хэши, атомарная запись, журнал."""
import hashlib, json, os, re, sys, time, uuid

PID_RE = re.compile(r'^[A-Za-z0-9_-]{6,64}$')        # id товара (cuid) — только безопасные символы, он идёт в пути и ключи S3
CHUNK_RE = re.compile(r'^c[0-9]{5}-[0-9a-f]{8}$')


def check_pid(pid):
    if not isinstance(pid, str) or not PID_RE.match(pid): raise ValueError(f'плохой id товара: {pid!r}')
    return pid


def check_chunk(c):
    if not isinstance(c, str) or not CHUNK_RE.match(c): raise ValueError(f'плохой id пачки: {c!r}')
    return c


def sha256(b): return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def atomic_write(path, data):
    """Писать во временный файл рядом и переименовать: читатель никогда не увидит недописанный файл."""
    tmp = f'{path}.tmp-{uuid.uuid4().hex[:8]}'
    with open(tmp, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_json(path, obj): atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1).encode())


def log(who, *a):
    print(time.strftime('%H:%M:%S'), f'[{who}]', *a, file=sys.stderr, flush=True)


class Stop:
    """Флаг остановки по SIGTERM/SIGINT (Salad предупреждает сигналом перед снятием машины)."""
    def __init__(self):
        import signal
        self.flag = False
        for s in (signal.SIGTERM, signal.SIGINT): signal.signal(s, self._set)

    def _set(self, *a): self.flag = True

    def __bool__(self): return self.flag


def is_done(q, st, pid, recipe_id):
    """Сделан ли товар: отметка в Redis, а если её нет — итог в хранилище. Ключи Redis могут быть вытеснены (общий Redis
    с allkeys-lru) — тогда истина в хранилище, и товар не будет сделан второй раз."""
    if q.is_done(pid): return True
    try: return st.head(f'results/{recipe_id}/{pid}.json') is not None
    except Exception: return False
