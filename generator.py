"""Модуль 2 — генерация. Один процесс на видеокарту, модель загружается один раз.
При старте — эталонный замер скорости шага: если машина медленнее GG_MAX_STEP_SEC, просим Salad переселить нас (IMDS reallocate).
Цикл: берёт самый старый товар из work/inbox/, сверяет pid папки с job.json и sha256 входов, проверяет, что товар ещё не сделан
и пачка всё ещё в аренде у этой машины; генерирует; пишет work/out/<pid>.png (pid, пачка, рецепт, sha входов, сид — внутри PNG)
и затем <pid>.json. Оба файла атомарно. Папку товара из inbox удаляет только после записи результата.
  python -m ghostgen.generator"""
import io, json, os, shutil, sys, time, urllib.request
from PIL import Image, PngImagePlugin
from .common import Stop, atomic_json, atomic_write, check_pid, is_done, log, sha256, sha256_file
from .config import Cfg, RECIPE, RECIPE_ID
from .engines import make_engine
from .queue import Queue
from .storage import make_storage


def reallocate(reason):
    req = urllib.request.Request('http://169.254.169.254/v1/reallocate', method='POST', data=json.dumps({'reason': reason}).encode(),
                                 headers={'Content-Type': 'application/json', 'Metadata': 'true'})
    try: urllib.request.urlopen(req, timeout=10); log('generator', 'попросили Salad переселить машину:', reason)
    except Exception as e: log('generator', 'reallocate не удался', e)


def main():
    cfg, stop = Cfg, Stop(); dirs = cfg.dirs(); q = Queue(cfg.redis_url); st = make_storage(cfg)
    t = time.time(); eng = make_engine(cfg); log('generator', f'модель загружена за {time.time() - t:.0f} с')
    step = eng.bench(); vram = getattr(eng, 'vram_gb', 24.0)
    limit = cfg.max_step_sec * (1.0 if vram >= 22 else cfg.small_gpu_factor)   # карты 16 ГБ гоняют энкодер в ОЗУ — шаг медленнее
    log('generator', f'эталонный шаг {step:.3f} с, карта {vram:.0f} ГБ, порог {limit:.2f}')
    q.beat(cfg.node + ':bench', {'step_sec': step, 'vram_gb': vram, 'limit': limit, 'rejected': step > limit})
    atomic_json(os.path.join(dirs['state'], 'bench.json'), {'step_sec': step, 'node': cfg.node, 't': time.time()})
    if step > limit and cfg.on_salad:
        reallocate(f'slow GPU: step {step:.2f}s > {limit:.2f}s'); sys.exit(3)
    n_done, t_sum, fails = 0, 0.0, 0
    batch_n = int(os.environ.get('GG_TWO_PHASE_BATCH', '16')); checked = False

    def load(name):
        """Проверки товара перед генерацией. None — товар пропущен (сделан / аренда потеряна); исключение — карантин."""
        d = os.path.join(dirs['inbox'], name); chunk, _, pid = name.partition('__')
        job = json.load(open(os.path.join(d, 'job.json')))
        check_pid(pid)
        if job['pid'] != pid or job['chunk'] != chunk or job['recipe_id'] != RECIPE_ID:
            raise ValueError(f'папка {name} не совпадает с job.json ({job["pid"]}, {job["chunk"]})')
        if is_done(q, st, pid, RECIPE_ID): log('generator', 'уже сделан, пропуск', pid); shutil.rmtree(d); return None
        if not q.owns(chunk, cfg.node): log('generator', 'аренда пачки потеряна, пропуск', pid); shutil.rmtree(d); return None
        ims = []
        for inp in job['inputs']:
            p = os.path.join(d, inp['file'])
            if sha256_file(p) != inp['sha256']: raise ValueError(f'sha256 входа {inp["file"]} изменился на диске')
            ims.append(Image.open(p).convert('RGB'))
        # ключ разбора — сам товар: pid + промпт + sha входов (разбор чужого товара не подойдёт по ключу)
        key = sha256((pid + '|' + job['prompt'] + '|' + ','.join(i['sha256'] for i in job['inputs'])).encode())
        return {'name': name, 'd': d, 'chunk': chunk, 'pid': pid, 'job': job, 'ims': ims, 'key': key}

    def fail(name, e):
        nonlocal fails
        chunk, _, pid = name.partition('__'); d = os.path.join(dirs['inbox'], name)
        log('generator', 'ОШИБКА', name, repr(e)[:300])
        if os.path.exists(d):
            os.makedirs(dirs['quarantine'], exist_ok=True); shutil.move(d, os.path.join(dirs['quarantine'], f'{name}-{int(time.time())}'))
        try: q.finish(pid, chunk, {'status': 'failed', 'stage': 'generate', 'reason': repr(e)[:300], 'node': cfg.node, 't': time.time()})
        except Exception: pass
        fails += 1
        if fails >= 3: log('generator', '3 ошибки подряд — выхожу, супервизор перезапустит'); sys.exit(1)

    def save(it, out, sec):
        nonlocal n_done, t_sum, fails
        pid, chunk, job = it['pid'], it['chunk'], it['job']
        info = PngImagePlugin.PngInfo()
        for k, v in (('gg_pid', pid), ('gg_chunk', chunk), ('gg_recipe', RECIPE_ID), ('gg_seed', str(job['seed'])),
                     ('gg_inputs', ','.join(i['sha256'] for i in job['inputs']))):
            info.add_text(k, v)
        buf = io.BytesIO(); out.save(buf, 'PNG', pnginfo=info); png = buf.getvalue()
        atomic_write(os.path.join(dirs['out'], f'{pid}.png'), png)
        atomic_json(os.path.join(dirs['out'], f'{pid}.json'), {'pid': pid, 'chunk': chunk, 'recipe_id': RECIPE_ID, 'seed': job['seed'],
                                                              'inputs': [i['sha256'] for i in job['inputs']], 'out_sha256': sha256(png),
                                                              'sec': round(sec, 2), 'node': cfg.node, 't': time.time()})
        shutil.rmtree(it['d']); n_done += 1; t_sum += sec; fails = 0
        if n_done % 20 == 0: log('generator', f'сделано {n_done}, в среднем {t_sum / n_done:.2f} с на товар')
        atomic_json(os.path.join(dirs['state'], 'gen.json'), {'done': n_done, 'avg_sec': t_sum / max(1, n_done), 'step_sec': step,
                                                              'two_phase': bool(eng.two_phase)})

    while not stop:
        names = sorted((n for n in os.listdir(dirs['inbox']) if not n.startswith('.tmp-')),
                       key=lambda n: os.path.getmtime(os.path.join(dirs['inbox'], n)))[:batch_n if eng.two_phase else 1]
        if not names: time.sleep(0.3); continue
        items = []
        for name in names:
            try:
                it = load(name)
                if it: items.append(it)
            except Exception as e: fail(name, e)
        if not items: continue
        embeds = {}
        if eng.two_phase:
            t = time.time()
            try: embeds = eng.encode_batch([(it['key'], it['ims'], it['job']['prompt'], it['job']['seed']) for it in items])
            except Exception as e: log('generator', 'разбор пачки не удался, товары пойдут по одному', repr(e)[:200]); embeds = {}
            enc_sec = (time.time() - t) / len(items)
        else: enc_sec = 0.0
        for it in items:
            if stop: break
            try:
                if not q.owns(it['chunk'], cfg.node): log('generator', 'аренда пачки потеряна, пропуск', it['pid']); shutil.rmtree(it['d']); continue
                t = time.time(); out = eng.generate(it['ims'], it['job']['prompt'], it['job']['seed'], embeds.get(it['key'])); sec = time.time() - t + enc_sec
                if embeds and not checked:
                    # самопроверка на первом товаре: тот же товар обычным способом; расхождение — выключаем двухфазный режим
                    ref = eng.generate(it['ims'], it['job']['prompt'], it['job']['seed'])
                    import numpy as np
                    diff = float(np.abs(np.asarray(out, float) - np.asarray(ref, float)).mean()); checked = True
                    q.beat(cfg.node + ':selfcheck', {'pid': it['pid'], 'diff': diff})
                    log('generator', f'самопроверка двухфазного режима: расхождение {diff:.3f} (0–255)')
                    if diff > 2.0: eng.two_phase = False; out = ref; log('generator', 'расхождение велико — двухфазный режим выключен')
                save(it, out, sec)
            except Exception as e: fail(it['name'], e)
    log('generator', 'остановлен')


if __name__ == '__main__':
    main()
