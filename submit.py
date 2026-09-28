"""Постановка заданий (на Mac). Вход — json-список готовых заданий (после отбора и подготовки: дубли убраны, кадр вещи, промпт):
  [{"pid": "...", "prompt": "...", "seed": 42, "inputs": ["локальный/путь/0.jpg", ...]}, ...]
Входные фото уходят в хранилище inputs/<pid>/<k>.jpg (JPEG), sha256 каждого — в манифест пачки jobs/<chunk>.json;
пачка ставится в очередь. Один товар не может попасть в две пачки (проверка в Redis).
  python -m ghostgen.submit jobs.json [--chunk 200]"""
import argparse, io, json, os
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from .common import check_pid, log, sha256
from .config import Cfg, RECIPE, RECIPE_ID
from .queue import Queue
from .storage import make_storage


def to_jpeg(path):
    data = open(path, 'rb').read()
    im = Image.open(io.BytesIO(data))
    if im.format == 'JPEG' and im.mode == 'RGB': return data
    if im.mode in ('RGBA', 'LA', 'P'):
        im = im.convert('RGBA'); bg = Image.new('RGB', im.size, (255, 255, 255)); bg.paste(im, mask=im.getchannel('A')); im = bg
    b = io.BytesIO(); im.convert('RGB').save(b, 'JPEG', quality=95); return b.getvalue()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('jobs'); ap.add_argument('--chunk', type=int, default=200)
    a = ap.parse_args()
    jobs = json.load(open(a.jobs)); st = make_storage(Cfg); q = Queue(Cfg.redis_url)
    pids = [check_pid(j['pid']) for j in jobs]
    if len(set(pids)) != len(pids): raise SystemExit('в файле заданий повторяются товары')
    for j in jobs:
        if not j.get('prompt') or not j.get('inputs'): raise SystemExit(f'у {j["pid"]} нет промпта или входов')
    q.register_pids(pids)                                                  # до загрузки: ошибка — ничего не поставлено
    start = int(q.r.scard(q.K['known']))

    def upload(j):
        inputs = []
        for k, path in enumerate(j['inputs']):
            data = to_jpeg(path); key = f"inputs/{j['pid']}/{k}.jpg"; sh = sha256(data)
            st.put(key, data, {'pid': j['pid'], 'sha256': sh})
            h = st.head(key)
            if not h or h['size'] != len(data): raise IOError(f'не загрузился {key}')
            inputs.append({'k': k, 'key': key, 'sha256': sh})
        return {'pid': j['pid'], 'prompt': j['prompt'], 'seed': int(j.get('seed', 42)), 'inputs': inputs}

    with ThreadPoolExecutor(16) as ex:
        for n, i in enumerate(range(0, len(jobs), a.chunk)):
            part = jobs[i:i + a.chunk]
            items = list(ex.map(upload, part))
            chunk = f'c{start + n:05d}-{sha256(",".join(it["pid"] for it in items).encode())[:8]}'
            man = {'chunk': chunk, 'recipe_id': RECIPE_ID, 'recipe': RECIPE, 'items': items}
            st.put(f'jobs/{chunk}.json', json.dumps(man, ensure_ascii=False).encode(), {'chunk': chunk})
            q.enqueue(chunk, len(items)); log('submit', 'поставлена', chunk, len(items), 'товаров')
    print(json.dumps(q.stats(), ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
