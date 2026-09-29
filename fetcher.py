"""Модуль 1 — скачивание. Берёт пачку в аренду, читает её манифест, качает входные фото товаров из хранилища,
сверяет sha256 каждого файла с манифестом и кладёт готовый товар в work/inbox/<chunk>__<pid>/ (job.json + 0.jpg, 1.jpg …).
Папка собирается во временной и переименовывается целиком — генератор никогда не увидит товар без части фото.
Держит впереди генерации не больше GG_PREFETCH товаров. Уже сделанные товары (есть итог в очереди) не качает.
Сырые пачки (kind='raw', с 29.09): товар готовится здесь же (prep.py) — фото магазина, отсев, кадр вещи, промпт; входы кладутся в
хранилище (inputs/<pid>/k.jpg) и в локальную копию манифеста (по ней сверяет загрузчик); пропуск — итог «skip» с причиной,
«уже предметное» — копия фото в shots/<pid>.jpg (поле «фото для коллажа»).
  python -m ghostgen.fetcher"""
import io, json, os, shutil, threading, time, uuid
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
    if m.get('kind') == 'raw':
        for it in m['items']:
            if not isinstance(it.get('urls'), list): raise ValueError(f'манифест {chunk}: у {it["pid"]} нет списка фото')
            it['inputs'] = []                                              # заполнит подготовка
        atomic_json(os.path.join(dirs['chunks'], f'{chunk}.json'), m); return m
    for it in m['items']:
        for inp in it['inputs']:
            if inp['key'] != f"inputs/{it['pid']}/{inp['k']}.jpg": raise ValueError(f'манифест {chunk}: чужой ключ входа {inp["key"]} у {it["pid"]}')
    atomic_json(os.path.join(dirs['chunks'], f'{chunk}.json'), m)       # локальная копия — по ней сверяет загрузчик
    return m


def fetch_item(st, it, chunk, dirs, pool):
    pid = it['pid']; final = os.path.join(dirs['inbox'], f'{chunk}__{pid}')
    tmp = os.path.join(dirs['inbox'], f'.tmp-{pid}-{uuid.uuid4().hex[:6]}'); os.makedirs(tmp)
    try:
        def get(key):
            for a in range(5):
                try: return st.get(key)
                except Exception as e:
                    if a == 4: raise IOError(f'сеть: {e}')
                    time.sleep(2 * (a + 1))
        datas = list(pool.map(lambda inp: get(inp['key']), it['inputs']))
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


_PREP = [None]; _MAN_LOCK = threading.Lock()


def get_prep(cfg):
    """Детекторы подготовки — один раз на процесс; веса детектора вещей кладёт bootstrap (ждём до 15 мин)."""
    if _PREP[0] is None:
        t = time.time()
        while not os.path.exists(cfg.item_det):
            if time.time() - t > 900: raise IOError('детектор вещей не появился за 15 мин')
            time.sleep(5)
        try:                                                              # детекторам — не больше доли видеопамяти: иначе кеш PyTorch
            import torch                                                  # разрастается и генерация задыхается (29.09: 24/24 ГБ, 23 с/товар)
            if torch.cuda.is_available(): torch.cuda.set_per_process_memory_fraction(float(os.environ.get('GG_PREP_GPU_FRACTION', '0.12')))
        except Exception as e: log('fetcher', 'ограничение видеопамяти не задано:', repr(e)[:120])
        from .prep import Prep
        _PREP[0] = Prep(item_weights=cfg.item_det); log('fetcher', f'детекторы подготовки загружены за {time.time() - t:.0f} с')
    return _PREP[0]


def prep_item(cfg, st, q, it, chunk, dirs, pool):
    """Сырой товар: подготовить; job — в inbox (входы в хранилище и в локальный манифест), skip — итог с причиной."""
    pid = it['pid']
    kind, *rest = get_prep(cfg)(it, pool)
    if kind == 'skip':
        reason, extra = rest; res = {'status': 'skip', 'pid': pid, 'chunk': chunk, 'recipe_id': RECIPE_ID, 'reason': reason, 'node': cfg.node, 't': time.time()}
        if extra.get('shot') is not None:
            b = io.BytesIO(); extra['shot'].save(b, 'JPEG', quality=92); key = f'shots/{pid}.jpg'
            st.put(key, b.getvalue(), {'pid': pid, 'sha256': sha256(b.getvalue())}); res['shot_key'] = key
            res['shot_src'] = extra.get('shot_url')
        st.put(f'results/{RECIPE_ID}/{pid}.json', json.dumps(res, ensure_ascii=False).encode(), {'pid': pid})
        q.finish(pid, chunk, res); return
    ims, prm, nn = rest
    final = os.path.join(dirs['inbox'], f'{chunk}__{pid}')
    tmp = os.path.join(dirs['inbox'], f'.tmp-{pid}-{uuid.uuid4().hex[:6]}'); os.makedirs(tmp)
    try:
        inputs, files = [], []
        for k, im in enumerate(ims):
            b = io.BytesIO(); im.save(b, 'JPEG', quality=94); data = b.getvalue(); sh = sha256(data); key = f'inputs/{pid}/{k}.jpg'
            st.put(key, data, {'pid': pid, 'sha256': sh})
            with open(os.path.join(tmp, f'{k}.jpg'), 'wb') as f: f.write(data)
            inputs.append({'k': k, 'key': key, 'sha256': sh}); files.append({'file': f'{k}.jpg', 'sha256': sh})
        with _MAN_LOCK:                                                    # локальная копия манифеста — по ней сверяет загрузчик
            mp = os.path.join(dirs['chunks'], f'{chunk}.json'); man = json.load(open(mp))
            for x in man['items']:
                if x['pid'] == pid: x.update({'inputs': inputs, 'prompt': prm, 'seed': 42, 'noun': nn})
            atomic_json(mp, man)
        atomic_json(os.path.join(tmp, 'job.json'), {'pid': pid, 'chunk': chunk, 'recipe_id': RECIPE_ID, 'prompt': prm, 'seed': 42, 'inputs': files})
        os.rename(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True); raise


def inbox_count(dirs): return sum(1 for n in os.listdir(dirs['inbox']) if not n.startswith('.tmp-'))


def local_has(dirs, chunk, pid):
    return os.path.exists(os.path.join(dirs['inbox'], f'{chunk}__{pid}')) or os.path.exists(os.path.join(dirs['out'], f'{pid}.json'))


def prep_chunk(cfg, st, q, m, chunk, dirs, pool, stop):
    """Сырая пачка: до GG_PREP_THREADS товаров готовятся одновременно (основное время — скачивание фото с сайтов магазинов)."""
    from concurrent.futures import ThreadPoolExecutor as TPE
    with TPE(cfg.prep_threads) as ex:
        futs = {}
        for it in m['items']:
            if stop or not q.owns(chunk, cfg.node): break
            pid = it['pid']
            if local_has(dirs, chunk, pid) or is_done(q, st, pid, RECIPE_ID): continue
            while (inbox_count(dirs) + sum(not f.done() for f in futs)) >= cfg.prefetch and not stop: time.sleep(0.5)
            futs[ex.submit(prep_item, cfg, st, q, it, chunk, dirs, pool)] = pid
        for f, pid in futs.items():
            try: f.result()
            except Exception as e:
                stage = 'prep-net' if isinstance(e, IOError) else 'prep'
                log('fetcher', 'отказ подготовки', stage, pid, repr(e)[:200])
                q.finish(pid, chunk, {'status': 'failed', 'stage': stage, 'reason': str(e)[:300], 'node': cfg.node, 't': time.time()})


def main():
    cfg, stop = Cfg, Stop(); dirs = cfg.dirs(); st = make_storage(cfg); q = Queue(cfg.redis_url)
    for n in os.listdir(dirs['inbox']):                                     # недокачанное после рестарта — выбросить
        if n.startswith('.tmp-'): shutil.rmtree(os.path.join(dirs['inbox'], n), ignore_errors=True)
    last_reap = 0
    resume = list(q.owned(cfg.node))                                        # после перезапуска модуля — сначала доделать свои пачки
    if resume: log('fetcher', 'доделываю свои пачки после перезапуска:', resume)
    with ThreadPoolExecutor(8) as pool:
        while not stop:
            if time.time() - last_reap > 60: q.reap(); last_reap = time.time()
            if inbox_count(dirs) >= cfg.prefetch: time.sleep(1); continue
            chunk = resume.pop(0) if resume else q.claim(cfg.node, cfg.lease_ttl)
            if not chunk: time.sleep(10); continue
            check_chunk(chunk); log('fetcher', 'взял пачку', chunk)
            try: m = load_manifest(st, chunk, dirs)
            except (ValueError, KeyError) as e:                             # битый манифест: пачку в карантин, не крутить по кругу
                log('fetcher', 'ОШИБКА манифеста', chunk, e); q.quarantine(chunk, f'{cfg.node}: {e}'); continue
            except Exception as e:                                          # сеть/хранилище: вернуть пачку и подождать
                log('fetcher', 'не прочитал манифест', chunk, e); q.release(chunk, cfg.node); time.sleep(30); continue
            if m.get('kind') == 'raw':
                prep_chunk(cfg, st, q, m, chunk, dirs, pool, stop); log('fetcher', 'пачка подготовлена', chunk); continue
            for it in m['items']:
                if stop or not q.owns(chunk, cfg.node): break               # аренду потеряли — пачку делает другая машина
                pid = it['pid']
                if local_has(dirs, chunk, pid) or is_done(q, st, pid, RECIPE_ID): continue
                while inbox_count(dirs) >= cfg.prefetch and not stop: time.sleep(0.5)
                try: fetch_item(st, it, chunk, dirs, pool)
                except Exception as e:                                      # отказ по товару, пачка идёт дальше
                    stage = 'fetch-net' if isinstance(e, IOError) else 'fetch'  # сеть — поставить заново; испорченный вход — чинить подготовку
                    log('fetcher', 'отказ', stage, pid, e)
                    q.finish(pid, chunk, {'status': 'failed', 'stage': stage, 'reason': str(e)[:300], 'node': cfg.node, 't': time.time()})
            log('fetcher', 'пачка скачана', chunk)
    log('fetcher', 'остановлен')


if __name__ == '__main__':
    main()
