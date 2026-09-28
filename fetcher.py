"""Модуль 1 — скачивание. Берёт пачку в аренду, читает её манифест, качает входные фото товаров из хранилища,
сверяет sha256 каждого файла с манифестом и кладёт готовый товар в work/inbox/<chunk>__<pid>/ (job.json + 0.jpg, 1.jpg …).
Папка собирается во временной и переименовывается целиком — генератор никогда не увидит товар без части фото.
Держит впереди генерации не больше GG_PREFETCH товаров. Уже сделанные товары (есть итог в очереди) не качает.
  python -m ghostgen.fetcher"""
import io, json, os, shutil, time, uuid
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from .common import Stop, atomic_json, check_chunk, check_pid, is_done, log, sha256
from .config import Cfg, RECIPE_ID
from .queue import Queue
from .storage import make_storage


def load_manifest(st, chunk, dirs):
    """Манифест пачки: проверки согласованности до того, как что-то скачано."""
    m = json.loads(st.get(f'jobs/{chunk}.json'))
    if m.get('chunk') != chunk: raise ValueError(f'манифест {chunk}: внутри chunk={m.get("chunk")}')
    if m.get('recipe_id') != RECIPE_ID: raise ValueError(f'манифест {chunk}: рецепт {m.get("recipe_id")} ≠ {RECIPE_ID}')
    pids = [check_pid(it['pid']) for it in m['items']]
    if len(set(pids)) != len(pids): raise ValueError(f'манифест {chunk}: повтор товара')
    for it in m['items']:
        for inp in it['inputs']:
            if inp['key'] != f"inputs/{it['pid']}/{inp['k']}.jpg": raise ValueError(f'манифест {chunk}: чужой ключ входа {inp["key"]} у {it["pid"]}')
    atomic_json(os.path.join(dirs['chunks'], f'{chunk}.json'), m)       # локальная копия — по ней сверяет загрузчик
    return m


def fetch_item(st, it, chunk, dirs, pool):
    pid = it['pid']; final = os.path.join(dirs['inbox'], f'{chunk}__{pid}')
    tmp = os.path.join(dirs['inbox'], f'.tmp-{pid}-{uuid.uuid4().hex[:6]}'); os.makedirs(tmp)
    try:
        datas = list(pool.map(lambda inp: st.get(inp['key']), it['inputs']))
        files = []
        for inp, data in zip(it['inputs'], datas):
            if sha256(data) != inp['sha256']: raise ValueError(f'sha256 входа {inp["key"]} не совпал с манифестом')
            Image.open(io.BytesIO(data)).verify()                           # файл — целая картинка
            name = f"{inp['k']}.jpg"
            with open(os.path.join(tmp, name), 'wb') as f: f.write(data)
            files.append({'file': name, 'sha256': inp['sha256']})
        atomic_json(os.path.join(tmp, 'job.json'), {'pid': pid, 'chunk': chunk, 'recipe_id': RECIPE_ID, 'prompt': it['prompt'],
                                                     'seed': int(it.get('seed', 42)), 'inputs': files})
        os.rename(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True); raise


def inbox_count(dirs): return sum(1 for n in os.listdir(dirs['inbox']) if not n.startswith('.tmp-'))


def local_has(dirs, chunk, pid):
    return os.path.exists(os.path.join(dirs['inbox'], f'{chunk}__{pid}')) or os.path.exists(os.path.join(dirs['out'], f'{pid}.json'))


def main():
    cfg, stop = Cfg, Stop(); dirs = cfg.dirs(); st = make_storage(cfg); q = Queue(cfg.redis_url)
    for n in os.listdir(dirs['inbox']):                                     # недокачанное после рестарта — выбросить
        if n.startswith('.tmp-'): shutil.rmtree(os.path.join(dirs['inbox'], n), ignore_errors=True)
    last_reap = 0
    with ThreadPoolExecutor(8) as pool:
        while not stop:
            if time.time() - last_reap > 60: q.reap(); last_reap = time.time()
            if inbox_count(dirs) >= cfg.prefetch: time.sleep(1); continue
            chunk = q.claim(cfg.node, cfg.lease_ttl)
            if not chunk: time.sleep(10); continue
            check_chunk(chunk); log('fetcher', 'взял пачку', chunk)
            try: m = load_manifest(st, chunk, dirs)
            except (ValueError, KeyError) as e:                             # битый манифест: пачку в карантин, не крутить по кругу
                log('fetcher', 'ОШИБКА манифеста', chunk, e); q.quarantine(chunk, f'{cfg.node}: {e}'); continue
            except Exception as e:                                          # сеть/хранилище: вернуть пачку и подождать
                log('fetcher', 'не прочитал манифест', chunk, e); q.release(chunk, cfg.node); time.sleep(30); continue
            for it in m['items']:
                if stop or not q.owns(chunk, cfg.node): break               # аренду потеряли — пачку делает другая машина
                pid = it['pid']
                if local_has(dirs, chunk, pid) or is_done(q, st, pid, RECIPE_ID): continue
                while inbox_count(dirs) >= cfg.prefetch and not stop: time.sleep(0.5)
                try: fetch_item(st, it, chunk, dirs, pool)
                except Exception as e:                                      # вход испорчен — отказ по товару, пачка идёт дальше
                    log('fetcher', 'отказ', pid, e)
                    q.finish(pid, chunk, {'status': 'failed', 'stage': 'fetch', 'reason': str(e)[:300], 'node': cfg.node, 't': time.time()})
            log('fetcher', 'пачка скачана', chunk)
    log('fetcher', 'остановлен')


if __name__ == '__main__':
    main()
