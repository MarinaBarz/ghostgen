"""Запись результатов в базу — централизованно (на Mac/сервере), не с машин: у машин доступа к базе нет.
Источник — итоги в хранилище results/<recipe>/<pid>.json (переживают даже потерю Redis). Для каждого: объект outputs/... существует,
размер и sha256 совпадают с итогом, товар есть в базе. По умолчанию — только отчёт (что будет записано); запись — флагом --apply
и отдельным доступом на запись GG_DATABASE_URL_RW. Уже заполненное поле без --overwrite не трогается.
  python -m ghostgen.db_apply --url-prefix https://cdn.../ [--column ghostImageUrl --recipe-column ghostImageRecipe] [--apply]"""
import argparse, json, os
from concurrent.futures import ThreadPoolExecutor
from .config import Cfg, RECIPE_ID
from .storage import make_storage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url-prefix', required=True, help='публичный адрес хранилища, к нему добавляется ключ outputs/…')
    ap.add_argument('--column', default='ghostImageUrl'); ap.add_argument('--recipe-column', default='ghostImageRecipe')
    ap.add_argument('--apply', action='store_true'); ap.add_argument('--overwrite', action='store_true'); ap.add_argument('--batch', type=int, default=500)
    a = ap.parse_args(); st = make_storage(Cfg)
    keys = st.list(f'results/{RECIPE_ID}/')

    def check(k):
        r = json.loads(st.get(k))
        if r.get('status') != 'ok': return None
        h = st.head(r['key'])
        ok = bool(h) and h['size'] == r['bytes'] and h['meta'].get('sha256') == r['sha256'] and h['meta'].get('pid') == r['pid']
        return (r['pid'], a.url_prefix.rstrip('/') + '/' + r['key'], ok)

    with ThreadPoolExecutor(16) as ex: rows = [x for x in ex.map(check, keys) if x]
    good = [(p, u) for p, u, ok in rows if ok]; bad = [p for p, u, ok in rows if not ok]
    print(f'итогов {len(keys)}, годных к записи {len(good)}, не сошлись с хранилищем {len(bad)} {bad[:5]}')
    out = os.path.abspath(f'db_apply_{RECIPE_ID}.jsonl')
    with open(out, 'w') as f:
        for p, u in good: f.write(json.dumps({'id': p, a.column: u, a.recipe_column: RECIPE_ID}) + '\n')
    print('план записи:', out)
    if not a.apply: print('это отчёт; запись — с --apply'); return
    import psycopg2
    cn = psycopg2.connect(os.environ['GG_DATABASE_URL_RW']); cu = cn.cursor(); n = 0
    cond = '' if a.overwrite else f' AND "{a.column}" IS NULL'
    for i in range(0, len(good), a.batch):
        part = good[i:i + a.batch]
        cu.executemany(f'UPDATE "Product" SET "{a.column}" = %s, "{a.recipe_column}" = %s WHERE id = %s{cond}',
                       [(u, RECIPE_ID, p) for p, u in part])
        cn.commit(); n += len(part); print('записано', n, 'из', len(good), flush=True)
    cn.close()


if __name__ == '__main__':
    main()
