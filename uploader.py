"""Модуль 3 — выгрузка. Только процессор, видеокарту не трогает.
Для каждого готового work/out/<pid>.json (+ <pid>.png) — проверки, чтобы картинки не перепутались:
  1) pid в имени файла = pid в json = pid, зашитый в PNG;
  2) пачка из json есть в локальной копии манифеста, и товар в ней есть;
  3) sha256 входов (json и PNG) = sha256 входов этого товара в манифесте;
  4) sha256 PNG = записанному генератором; картинка открывается, 512x512, не пустая;
  5) рецепт = рецепт этой сборки.
Потом: S3 outputs/<recipe>/<pid>.png (метаданные: pid, sha256, пачка) → перечитать заголовок и сверить размер и sha256 →
S3 results/<recipe>/<pid>.json (итог — из него потом пишется база) → Redis finish → удалить локальные файлы.
Не прошёл проверку — файлы в work/quarantine/, итог «failed» с причиной. В базу отсюда ничего не пишется (db_apply.py).
  python -m ghostgen.uploader"""
import io, json, os, shutil, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image
from .common import Stop, check_pid, log, sha256
from .config import Cfg, RECIPE, RECIPE_ID
from .queue import Queue
from .storage import make_storage


def validate(pid, dirs):
    meta = json.load(open(os.path.join(dirs['out'], f'{pid}.json')))
    png = open(os.path.join(dirs['out'], f'{pid}.png'), 'rb').read()
    im = Image.open(io.BytesIO(png)); txt = im.text if hasattr(im, 'text') else {}
    check_pid(pid)
    if meta['pid'] != pid or txt.get('gg_pid') != pid: raise ValueError(f'pid не совпадает: файл {pid}, json {meta["pid"]}, png {txt.get("gg_pid")}')
    if meta['recipe_id'] != RECIPE_ID or txt.get('gg_recipe') != RECIPE_ID: raise ValueError('чужой рецепт')
    chunk = meta['chunk']
    if txt.get('gg_chunk') != chunk: raise ValueError('пачка в PNG ≠ пачка в json')
    man = json.load(open(os.path.join(dirs['chunks'], f'{chunk}.json')))
    item = next((it for it in man['items'] if it['pid'] == pid), None)
    if item is None: raise ValueError(f'товара {pid} нет в манифесте пачки {chunk}')
    want = [i['sha256'] for i in item['inputs']]
    if meta['inputs'] != want or txt.get('gg_inputs') != ','.join(want): raise ValueError('входы результата ≠ входы товара в манифесте')
    if sha256(png) != meta['out_sha256']: raise ValueError('sha256 PNG изменился после генерации')
    im.load()
    r = RECIPE['output_resolution']
    if im.size != (r, r): raise ValueError(f'размер {im.size}')
    if float(np.asarray(im.convert('L'), float).std()) < 2: raise ValueError('пустая картинка')
    return meta, png


def ship(pid, st, q, dirs, node):
    try:
        meta, png = validate(pid, dirs)
    except Exception as e:
        log('uploader', 'КАРАНТИН', pid, e)
        try: chunk = json.load(open(os.path.join(dirs['out'], f'{pid}.json')))['chunk']
        except Exception: chunk = None
        ts = int(time.time())
        for ext in ('png', 'json'):
            p = os.path.join(dirs['out'], f'{pid}.{ext}')
            if os.path.exists(p): shutil.move(p, os.path.join(dirs['quarantine'], f'{pid}-{ts}.{ext}'))
        if chunk: q.finish(pid, chunk, {'status': 'failed', 'stage': 'upload-check', 'reason': str(e)[:300], 'node': node, 't': time.time()})
        return
    key = f'outputs/{RECIPE_ID}/{pid}.png'; sh = meta['out_sha256']
    st.put(key, png, {'pid': pid, 'sha256': sh, 'chunk': meta['chunk'], 'recipe': RECIPE_ID})
    h = st.head(key)
    if not h or h['size'] != len(png) or h['meta'].get('sha256') != sh or h['meta'].get('pid') != pid:
        raise IOError(f'проверка после загрузки не прошла: {key} {h}')      # файлы остаются, будет повтор
    result = {'status': 'ok', 'pid': pid, 'key': key, 'sha256': sh, 'bytes': len(png), 'chunk': meta['chunk'], 'recipe_id': RECIPE_ID,
              'inputs': meta['inputs'], 'seed': meta['seed'], 'gen_sec': meta['sec'], 'node': node, 't': time.time()}
    st.put(f'results/{RECIPE_ID}/{pid}.json', json.dumps(result, ensure_ascii=False).encode(), {'pid': pid})
    first = q.finish(pid, meta['chunk'], result)
    if not first: log('uploader', 'итог уже был (повтор после вытеснения), объект перезаписан той же версией', pid)
    for ext in ('png', 'json'): os.remove(os.path.join(dirs['out'], f'{pid}.{ext}'))


def main():
    cfg, stop = Cfg, Stop(); dirs = cfg.dirs(); st = make_storage(cfg); q = Queue(cfg.redis_url)
    idle_since = None
    with ThreadPoolExecutor(4) as pool:
        while True:
            pids = sorted(n[:-5] for n in os.listdir(dirs['out']) if n.endswith('.json') and '.tmp-' not in n
                          and os.path.exists(os.path.join(dirs['out'], n[:-5] + '.png')))
            if not pids:
                if stop: break                                          # при остановке — выгрузить всё, что есть, и только потом выйти
                time.sleep(0.5); continue
            futs = {pool.submit(ship, pid, st, q, dirs, cfg.node): pid for pid in pids[:16]}
            for f, pid in futs.items():
                try: f.result()
                except Exception as e: log('uploader', 'повторю позже', pid, repr(e)[:200]); time.sleep(2)
    log('uploader', 'остановлен, всё выгружено')


if __name__ == '__main__':
    main()
