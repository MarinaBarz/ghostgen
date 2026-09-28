"""Супервизор машины: команда контейнера. Запускает три модуля отдельными процессами, раз в GG_HEARTBEAT продлевает аренду
всех пачек этой машины и шлёт сигнал жизни, перезапускает упавший модуль (генератор с кодом 3 = «машина медленная, попросили
переселить» — тогда выходим целиком).
Остановка (SIGTERM от Salad или Ctrl-C): скачивание прекращается сразу; генератор доделывает текущий товар; выгрузчику даётся до
GG_DRAIN_SEC на отправку готового; затем все недоделанные пачки этой машины сразу возвращаются в очередь (не ждём конца аренды).
  python -m ghostgen.node"""
import json, os, signal, subprocess, sys, time
from .common import log
from .config import Cfg
from .queue import Queue

MODULES = ('fetcher', 'generator', 'uploader')


def start(name):
    return subprocess.Popen([sys.executable, '-m', f'ghostgen.{name}'], env={**os.environ, 'GG_NODE_ID': Cfg.node})


def main():
    cfg = Cfg; dirs = cfg.dirs(); q = Queue(cfg.redis_url); drain = int(os.environ.get('GG_DRAIN_SEC', '90'))
    log('node', 'машина', cfg.node, '| хранилище', cfg.storage, '| движок', cfg.engine)
    boot = start('bootstrap')                                     # модели качаются параллельно со скачиванием фото
    procs = {m: start(m) for m in ('fetcher', 'uploader')}; restarts = {m: 0 for m in MODULES}
    gen_started = False
    stopping = {'v': False}

    def on_stop(*a): stopping['v'] = True
    signal.signal(signal.SIGTERM, on_stop); signal.signal(signal.SIGINT, on_stop)
    last = 0
    while not stopping['v']:
        if time.time() - last >= cfg.heartbeat:
            mine = q.owned(cfg.node)
            for c in mine: q.renew(c, cfg.node, cfg.lease_ttl)
            info = {'пачки': mine, 'inbox': len(os.listdir(dirs['inbox'])), 'out': len([n for n in os.listdir(dirs['out']) if n.endswith('.png')])}
            for f in ('bench.json', 'gen.json'):
                p = os.path.join(dirs['state'], f)
                if os.path.exists(p): info[f[:-5]] = json.load(open(p))
            q.beat(cfg.node, info); last = time.time()
        if not gen_started:
            rc = boot.poll()
            if rc == 0: procs['generator'] = start('generator'); gen_started = True; log('node', 'модели на месте — генератор запущен')
            elif rc is not None:
                restarts['generator'] += 1; log('node', f'скачивание моделей упало (код {rc}), повтор №{restarts["generator"]}')
                if restarts['generator'] > 5: stopping['v'] = True; continue
                time.sleep(30); boot = start('bootstrap')
        for m, p in list(procs.items()):
            rc = p.poll()
            if rc is None: continue
            if m == 'generator' and rc == 3: log('node', 'машина медленная — выходим'); stopping['v'] = True; break
            restarts[m] += 1; log('node', f'{m} завершился с кодом {rc}, перезапуск №{restarts[m]}')
            if restarts[m] > 20: log('node', 'слишком много перезапусков — выходим'); stopping['v'] = True; break
            time.sleep(min(60, 2 ** min(restarts[m], 6))); procs[m] = start(m)
        time.sleep(2)
    # остановка по порядку: скачивание → генерация → выгрузка
    if boot.poll() is None: boot.kill()
    for m in ('fetcher', 'generator'):
        if m in procs and procs[m].poll() is None: procs[m].send_signal(signal.SIGTERM)
    for m in ('fetcher', 'generator'):
        if m not in procs: continue
        try: procs[m].wait(timeout=60)
        except subprocess.TimeoutExpired: procs[m].kill()
    if procs['uploader'].poll() is None: procs['uploader'].send_signal(signal.SIGTERM)
    try: procs['uploader'].wait(timeout=drain)
    except subprocess.TimeoutExpired: procs['uploader'].kill(); log('node', 'выгрузка не успела — неотправленное будет переделано')
    for c in q.owned(cfg.node): q.release(c, cfg.node); log('node', 'пачка возвращена в очередь', c)
    log('node', 'остановлена')


if __name__ == '__main__':
    main()
