"""Сквозная проверка подготовки на машине (сырые пачки, 29.09): настоящий Redis, локальное хранилище, фальшивый движок, НАСТОЯЩАЯ подготовка
(фото с сайтов магазинов, детекторы на процессоре). 12 товаров на генерацию + 4 «только проверка»; машину A убивают SIGKILL посреди работы,
машина B доделывает. Проверки: у каждого товара ровно один итог; «ok» — картинка в хранилище, pid внутри PNG; входы в хранилище = входы
в итоге; «уже предметное» — копия фото в shots/<pid>.jpg; загрузчик ничего не отправил в карантин.
  ../.venv_attr/bin/python -m ghostgen.tests.test_raw   (из папки looks_pipeline/services; нужен интернет)"""
import glob, io, json, os, random, shutil, signal, socket, subprocess, sys, tempfile, time
from PIL import Image

LP = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
T = tempfile.mkdtemp(prefix='gg_raw_')
s = socket.socket(); s.bind(('127.0.0.1', 0)); PORT = s.getsockname()[1]; s.close()
os.environ.update({'GG_REDIS_URL': f'redis://127.0.0.1:{PORT}/0', 'GG_STORAGE': 'local', 'GG_LOCAL_STORE': f'{T}/store',
                   'GG_ENGINE': 'fake', 'GG_FAKE_SEC': '0.1', 'GG_LEASE_TTL': '20', 'GG_HEARTBEAT': '2', 'GG_PREFETCH': '6',
                   'GG_DRAIN_SEC': '60', 'PYTHONUNBUFFERED': '1', 'GG_ITEM_DET': f'{LP}/detector/backup_m896c14/checkpoint_best_ema.pth'})
redis_srv = subprocess.Popen(['redis-server', '--port', str(PORT), '--save', '', '--appendonly', 'no', '--dir', T], stdout=subprocess.DEVNULL)
time.sleep(0.5)
from ghostgen.queue import Queue          # noqa: E402
from ghostgen.storage import LocalStorage  # noqa: E402
from ghostgen.config import RECIPE_ID      # noqa: E402

rules = {r['shop']: r['have'] for r in json.load(open(f'{LP}/experiments/2026-09-28_shops/shop_rules_final.json'))}
rows = [r for f in glob.glob(f'{LP}/experiments/2026-09-28_fullrun/products/*.json') for r in json.load(open(f))]
random.seed(5); random.shuffle(rows)
gen = [r for r in rows if rules.get(r['shop'], 0.5) < 0.95 and r.get('images') and not r.get('ghost') and r.get('category') != 'Care, Beauty & Home'][:12]
chk = [r for r in rows if rules.get(r['shop'], 0) >= 0.95 and r.get('pi') and not r.get('ghost')][:4]
os.makedirs(f'{T}/prod')
json.dump(gen, open(f'{T}/prod/gen.json', 'w')); json.dump(chk, open(f'{T}/prod/chk.json', 'w'))
json.dump([{'shop': r['shop'], 'have': rules.get(r['shop'], 0.5)} for r in gen + chk], open(f'{T}/rules.json', 'w'))
json.dump([{'full': True, 'items': [{'pid': gen[0]['id']}]}], open(f'{T}/looks.json', 'w'))
root = os.path.join(LP, 'services')
run = lambda *a: subprocess.run([sys.executable, '-m', *a], cwd=root, check=True, stdout=subprocess.DEVNULL)
base = ['--products', f'{T}/prod', '--rules', f'{T}/rules.json', '--looks', f'{T}/looks.json', '--chunk', '4']
run('ghostgen.submit_raw', *base); run('ghostgen.submit_raw', *base, '--check-only')
st = LocalStorage(f'{T}/store'); q = Queue(os.environ['GG_REDIS_URL'])
pids = [r['id'] for r in gen + chk]


def node(name):
    env = {**os.environ, 'GG_WORK': f'{T}/work_{name}', 'GG_NODE_ID': f'machine-{name}'}
    return subprocess.Popen([sys.executable, '-m', 'ghostgen.node'], cwd=root, env=env, stdout=open(f'{T}/{name}.log', 'w'), stderr=subprocess.STDOUT,
                            start_new_session=True)       # своя группа процессов — SIGKILL снимает машину целиком


A = node('A'); t0 = time.time()
while sum(1 for _ in q.results()) < 5 and time.time() - t0 < 600: time.sleep(1)
os.killpg(A.pid, signal.SIGKILL)
print('машина A убита после', sum(1 for _ in q.results()), 'итогов', flush=True)
time.sleep(25)                                                           # аренда истекает
B = node('B'); t0 = time.time()
while sum(1 for _ in q.results()) < len(pids) and time.time() - t0 < 900: time.sleep(2)
B.send_signal(signal.SIGTERM); B.wait(timeout=120)
R = dict(q.results()); bad = []
from collections import Counter
print('итоги:', Counter(r['status'] + (':' + r['stage'] if r['status'] != 'ok' else '') for r in R.values()))
for pid in pids:
    r = R.get(pid)
    if not r: bad.append((pid, 'нет итога')); continue
    res = json.loads(st.get(f'results/{RECIPE_ID}/{pid}.json')) if r['status'] in ('ok', 'skip') else None
    if r['status'] == 'ok':
        im = Image.open(io.BytesIO(st.get(res['key'])))
        if im.text.get('gg_pid') != pid: bad.append((pid, 'чужой pid в PNG'))
        from ghostgen.common import sha256
        ins = [sha256(st.get(f'inputs/{pid}/{k}.jpg')) for k in range(len(res['inputs']))]
        if ins != res['inputs']: bad.append((pid, 'входы в хранилище ≠ входы в итоге'))
    if r['status'] == 'skip' and r['stage'].startswith('уже предметное'):
        if not st.head(f'shots/{pid}.jpg'): bad.append((pid, 'нет копии предметного фото'))
quar = [f for w in ('A', 'B') for f in glob.glob(f'{T}/work_{w}/quarantine/*')]
redis_srv.terminate()
print('карантин:', len(quar), quar[:3]); print('ПРОВЕРКА ПРОЙДЕНА' if not bad and not quar else f'ПРОВАЛ {bad}')
print('журналы:', T)
