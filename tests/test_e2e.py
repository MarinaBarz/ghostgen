"""Сквозная проверка без видеокарты: настоящий Redis, локальное хранилище, фальшивый движок (выход = первый вход товара).
Сценарий:
  1) 60 товаров, у каждого свой цвет (от id) и 1–3 фото; у одного товара вход испорчен в хранилище (sha не совпадёт);
  2) машина A работает, пока не наберётся ~20 итогов, и её убивают SIGKILL целиком — как внезапное снятие Salad;
  3) аренда истекает, машина B (с пустым диском) подхватывает пачки и доделывает, затем штатно останавливается.
Проверки: у каждого товара ровно один итог; испорченный — отказ на скачивании; у остальных картинка в хранилище,
sha сходится, id внутри PNG = id товара, ЦВЕТ картинки = цвет своего товара (перепутать нельзя незаметно); все пачки закрыты.
  ghostgen/.venv/bin/python -m ghostgen.tests.test_e2e   (из папки looks_pipeline/services)"""
import hashlib, io, json, os, shutil, signal, socket, subprocess, sys, tempfile, time
import numpy as np
from PIL import Image

T = tempfile.mkdtemp(prefix='gg_e2e_')
s = socket.socket(); s.bind(('127.0.0.1', 0)); PORT = s.getsockname()[1]; s.close()
os.environ.update({'GG_REDIS_URL': f'redis://127.0.0.1:{PORT}/0', 'GG_STORAGE': 'local', 'GG_LOCAL_STORE': f'{T}/store',
                   'GG_ENGINE': 'fake', 'GG_FAKE_SEC': '0.15', 'GG_LEASE_TTL': '8', 'GG_HEARTBEAT': '1', 'GG_PREFETCH': '6',
                   'GG_DRAIN_SEC': '30', 'PYTHONUNBUFFERED': '1'})
redis_srv = subprocess.Popen(['redis-server', '--port', str(PORT), '--save', '', '--appendonly', 'no', '--dir', T],
                             stdout=subprocess.DEVNULL)
time.sleep(0.5)
from ghostgen.queue import Queue          # noqa: E402  (после окружения)
from ghostgen.storage import LocalStorage  # noqa: E402
from ghostgen.config import RECIPE_ID      # noqa: E402

N = 60
def color(pid): h = hashlib.md5(pid.encode()).digest(); return (h[0], h[1], h[2])
pids = [f'cmtest{i:04d}{hashlib.md5(str(i).encode()).hexdigest()[:10]}' for i in range(N)]
os.makedirs(f'{T}/src')
jobs = []
for i, pid in enumerate(pids):
    files = []
    for k in range(1 + i % 3):
        p = f'{T}/src/{pid}_{k}.jpg'
        Image.new('RGB', (300 + 40 * k, 400), color(pid) if k == 0 else (128, 128, 128)).save(p, quality=95); files.append(p)
    jobs.append({'pid': pid, 'prompt': f'товар {pid}', 'seed': 42, 'inputs': files})
json.dump(jobs, open(f'{T}/jobs.json', 'w'))
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
run = lambda *a, **kw: subprocess.run([sys.executable, '-m', *a], cwd=root, check=True, **kw)
run('ghostgen.submit', f'{T}/jobs.json', '--chunk', '10', stdout=subprocess.DEVNULL)
st = LocalStorage(f'{T}/store'); q = Queue(os.environ['GG_REDIS_URL'])
BAD = pids[7]                                                           # портим вход в хранилище: sha не совпадёт с манифестом
with open(f'{T}/store/inputs/{BAD}/0.jpg', 'ab') as f: f.write(b'\x00junk')


def node(name):
    env = {**os.environ, 'GG_WORK': f'{T}/work_{name}', 'GG_NODE_ID': f'node-{name}'}
    return subprocess.Popen([sys.executable, '-m', 'ghostgen.node'], cwd=root, env=env, start_new_session=True,
                            stdout=open(f'{T}/{name}.log', 'w'), stderr=subprocess.STDOUT)


def n_results(): return q.r.hlen(q.K['result'])


try:
    a = node('A'); t0 = time.time()
    while n_results() < 20 and time.time() - t0 < 120: time.sleep(0.3)
    got_a = n_results(); os.killpg(a.pid, signal.SIGKILL); a.wait()
    print(f'машина A убита SIGKILL после {got_a} итогов; в аренде осталось пачек: {q.r.zcard(q.K["leased"])}')
    b = node('B'); t0 = time.time()
    while n_results() < N and time.time() - t0 < 300: time.sleep(0.5)
    b.send_signal(signal.SIGTERM); b.wait(timeout=120)
    # ---- проверки ----
    res = dict(q.results()); errs = []
    if set(res) != set(pids): errs.append(f'итоги не у всех: нет {len(set(pids) - set(res))}, лишних {len(set(res) - set(pids))}')
    if res.get(BAD, {}).get('stage') != 'fetch': errs.append(f'испорченный вход не отказан на скачивании: {res.get(BAD)}')
    ok = [p for p in pids if res.get(p, {}).get('status') == 'ok']
    if len(ok) != N - 1: errs.append(f'успешных {len(ok)}, ожидалось {N - 1}')
    for p in ok:
        r = json.loads(st.get(f'results/{RECIPE_ID}/{p}.json')); key = f'outputs/{RECIPE_ID}/{p}.png'
        data = st.get(key); im = Image.open(io.BytesIO(data))
        if hashlib.sha256(data).hexdigest() != r['sha256']: errs.append(f'{p}: sha не совпал')
        if im.text.get('gg_pid') != p: errs.append(f'{p}: внутри PNG id {im.text.get("gg_pid")}')
        c = np.median(np.asarray(im.convert('RGB'), float).reshape(-1, 3), 0)
        if np.abs(c - np.array(color(p))).max() > 6: errs.append(f'{p}: цвет {c.round()} ≠ {color(p)} — ПЕРЕПУТАНО')
    stt = q.stats()
    if stt['закрыто'] != stt['пачек'] or stt['в работе'] or stt['ждут']: errs.append(f'пачки не закрыты: {stt}')
    for w in ('A', 'B'):
        qd = f'{T}/work_{w}/quarantine'
        if os.path.isdir(qd) and os.listdir(qd): errs.append(f'карантин {w}: {os.listdir(qd)[:3]}')
    dup = sum(1 for l in open(f'{T}/B.log') if 'итог уже был' in l)
    print(f'итогов {len(res)}, успешных {len(ok)}, отказов {N - len(ok)}; повторных генераций после вытеснения: {dup}')
    print('ПРОВЕРКА ПРОЙДЕНА' if not errs else 'ОШИБКИ:\n' + '\n'.join(errs[:20]))
    sys.exit(1 if errs else 0)
finally:
    redis_srv.terminate()
    print('журналы машин:', T)
