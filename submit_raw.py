"""Сырые пачки для подготовки на машинах (29.09): только список товаров из выгрузки базы — фото здесь не качаются (минуты на весь каталог).
Отбор: товары в наличии без ghostImageUrl, не «Care, Beauty & Home», ещё не стоящие в очереди и не разобранные подготовкой на Mac
(skip.jsonl); магазины с долей предметных фото < 0.95 — генерация, ≥ 0.95 — --check-only: только проверка главного фото на предметность
(поле «фото для коллажа», без генерации). Порядок: сначала товары полных образов (shop_pilot/looks.json), затем по кругу между магазинами.
  python -m ghostgen.submit_raw --products DIR --rules shop_rules_final.json --looks looks.json [--skip skip.jsonl] [--limit N] [--check-only] [--dry]"""
import argparse, glob, json, os, zlib
from .common import check_pid, log, sha256
from .config import Cfg, RECIPE, RECIPE_ID
from .queue import Queue
from .storage import make_storage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--products', required=True); ap.add_argument('--rules', required=True); ap.add_argument('--looks', required=True)
    ap.add_argument('--skip', default=None); ap.add_argument('--limit', type=int, default=10 ** 9); ap.add_argument('--chunk', type=int, default=100)
    ap.add_argument('--check-only', action='store_true'); ap.add_argument('--dry', action='store_true')
    a = ap.parse_args(); q = Queue(Cfg.redis_url); st = make_storage(Cfg)
    have = {r['shop']: r['have'] for r in json.load(open(a.rules))}
    look = set()
    for l in json.load(open(a.looks)):
        if l.get('full'): look.update(it['pid'] for it in l['items'])
    known = set(q.r.smembers(q.K['pids']))
    seen = {json.loads(l)['pid'] for l in open(a.skip)} if a.skip and os.path.exists(a.skip) else set()
    shops = []
    for f in glob.glob(os.path.join(a.products, '*.json')):
        rows = json.load(open(f))
        if not rows: continue
        h = have.get(rows[0]['shop'], 0.5)
        if (h >= 0.95) != a.check_only: continue
        rows = [r for r in rows if not r.get('ghost') and r.get('category') != 'Care, Beauty & Home' and r['id'] not in known and r['id'] not in seen
                and (r.get('images') or r.get('pi'))]
        rows.sort(key=lambda r: (r['id'] not in look, zlib.crc32(r['id'].encode())))
        if rows: shops.append(rows)
    first = [r for rows in shops for r in rows if r['id'] in look]; rest = []
    k = 0; mx = max((len(x) for x in shops), default=0)
    while k < mx:                                                        # по кругу между магазинами
        rest += [rows[k] for rows in shops if k < len(rows) and rows[k]['id'] not in look]; k += 1
    order = (first + rest)[:a.limit]
    log('submit_raw', f'товаров {len(order)} (из образов {len(first)}), магазинов {len(shops)}, режим {"проверка" if a.check_only else "генерация"}')
    if a.dry: return
    def item(r):
        flux = [u for u in (r.get('gpi'), r.get('nobg')) if u]
        return {'pid': check_pid(r['id']), 'title': r.get('title'), 'category': r.get('category'), 'shop': r.get('shop'),
                'urls': (list((r.get('images') or [])[:1]) if not r.get('pi') else []) if a.check_only   # проверка: главное фото, нет его — первое фото магазина
                        else list((r.get('images') or [])[:5]) + list((r.get('li') or [])[:1]),
                'pi': r.get('pi'), 'flux': flux, 'in_look': r['id'] in look, 'check_only': a.check_only}
    start = int(q.r.scard(q.K['known']))
    for n, i in enumerate(range(0, len(order), a.chunk)):
        items = [item(r) for r in order[i:i + a.chunk]]
        q.register_pids([it['pid'] for it in items])
        chunk = f'c{start + n:05d}-{sha256(",".join(it["pid"] for it in items).encode())[:8]}'
        st.put(f'jobs/{chunk}.json', json.dumps({'chunk': chunk, 'recipe_id': RECIPE_ID, 'recipe': RECIPE, 'kind': 'raw', 'items': items},
                                                ensure_ascii=False).encode(), {'chunk': chunk})
        q.enqueue(chunk, len(items))
        if n % 50 == 0: log('submit_raw', 'поставлено пачек', n + 1)
    log('submit_raw', 'готово, пачек', (len(order) + a.chunk - 1) // a.chunk)


if __name__ == '__main__':
    main()
