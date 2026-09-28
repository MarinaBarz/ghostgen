"""Запись результатов в базу — централизованно (на Mac/сервере), не с машин: у машин доступа к базе нет.
Источник — итоги в хранилище results/<recipe>/<pid>.json (переживают даже потерю Redis). Для каждого: объект outputs/... существует,
размер и sha256 совпадают с итогом, товар есть в базе. По умолчанию — только отчёт (что будет записано); запись — флагом --apply
и отдельным доступом на запись GG_DATABASE_URL_RW. Уже заполненное поле без --overwrite не трогается.
  python -m ghostgen.db_apply [--url-prefix https://cdn.../ghostgen/] [--column ghostImageUrl --recipe-column ghostImageRecipe] [--apply]
  python -m ghostgen.db_apply --apply --follow   — постоянно: каждые 30 с берёт новые «ok» из Redis, сверяет с хранилищем и пишет в базу;
    записанные помнит в db_applied_<рецепт>.txt (перезапуск не пишет повторно, а пропущенное добирается при старте).
  Доступ на запись: GG_DATABASE_URL_RW, иначе DATABASE_URL."""
import argparse, json, os, time
from concurrent.futures import ThreadPoolExecutor
from .config import Cfg, RECIPE_ID
from .storage import make_storage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url-prefix', default=None, help='публичный адрес хранилища с префиксом; по умолчанию AWS_S3_PUBLIC_BASE_URL + префикс ghostgen/')
    ap.add_argument('--follow', action='store_true'); ap.add_argument('--every', type=int, default=30)
    ap.add_argument('--column', default='ghostImageUrl'); ap.add_argument('--recipe-column', default='ghostImageRecipe')
    ap.add_argument('--apply', action='store_true'); ap.add_argument('--overwrite', action='store_true'); ap.add_argument('--batch', type=int, default=500)
    a = ap.parse_args(); cfg = Cfg(); st = make_storage(cfg)
    if not a.url_prefix: a.url_prefix = os.environ['AWS_S3_PUBLIC_BASE_URL'].rstrip('/') + '/' + cfg.s3_prefix
    if a.follow: return follow(a, cfg, st)
    keys = st.list(f'results/{RECIPE_ID}/')

    def check(k): return verify(st, a, k)

    with ThreadPoolExecutor(16) as ex: rows = [x for x in ex.map(check, keys) if x]
    good = [(p, u) for p, u, ok in rows if ok]; bad = [p for p, u, ok in rows if not ok]
    print(f'итогов {len(keys)}, годных к записи {len(good)}, не сошлись с хранилищем {len(bad)} {bad[:5]}')
    out = os.path.abspath(f'db_apply_{RECIPE_ID}.jsonl')
    with open(out, 'w') as f:
        for p, u in good: f.write(json.dumps({'id': p, a.column: u, a.recipe_column: RECIPE_ID}) + '\n')
    print('план записи:', out)
    if not a.apply: print('это отчёт; запись — с --apply'); return
    import psycopg2
    cn = psycopg2.connect(db_url()); cu = cn.cursor(); n = 0
    cond = '' if a.overwrite else f' AND "{a.column}" IS NULL'
    for i in range(0, len(good), a.batch):
        part = good[i:i + a.batch]
        cu.executemany(f'UPDATE "Product" SET "{a.column}" = %s, "{a.recipe_column}" = %s WHERE id = %s{cond}',
                       [(u, RECIPE_ID, p) for p, u in part])
        cn.commit(); n += len(part); print('записано', n, 'из', len(good), flush=True)
    cn.close()


def db_url():
    u = os.environ.get('GG_DATABASE_URL_RW') or os.environ['DATABASE_URL']
    return u.split('?schema=')[0]


def verify(st, a, k):
    """Итог в хранилище → (pid, ссылка, сошлось ли с объектом outputs/…) или None для отказов."""
    r = json.loads(st.get(k))
    if r.get('status') != 'ok': return None
    h = st.head(r['key'])
    ok = bool(h) and h['size'] == r['bytes'] and h['meta'].get('sha256') == r['sha256'] and h['meta'].get('pid') == r['pid']
    return (r['pid'], a.url_prefix.rstrip('/') + '/' + r['key'], ok)


def follow(a, cfg, st):
    import psycopg2
    from .queue import Queue
    q = Queue(cfg.redis_url, RECIPE_ID)
    mem = os.path.abspath(f'db_applied_{RECIPE_ID}.txt'); done = set(open(mem).read().split()) if os.path.exists(mem) else set()
    cond = '' if a.overwrite else f' AND "{a.column}" IS NULL'
    print(f'слежу: уже записано ранее {len(done)}, ссылки {a.url_prefix}', flush=True)
    while True:
        try:
            new = [pid for pid, r in q.results() if r['status'] == 'ok' and pid not in done]
            if new:
                with ThreadPoolExecutor(16) as ex: rows = list(ex.map(lambda p: verify(st, a, f'results/{RECIPE_ID}/{p}.json'), new))
                good = [(p, u) for p, u, ok in (x for x in rows if x) if ok]
                bad = [x[0] for x in rows if x and not x[2]]
                if good:
                    cn = psycopg2.connect(db_url()); cu = cn.cursor(); hit = 0
                    for p, u in good:
                        cu.execute(f'UPDATE "Product" SET "{a.column}" = %s, "{a.recipe_column}" = %s WHERE id = %s{cond}', (u, RECIPE_ID, p)); hit += cu.rowcount
                    cn.commit(); cn.close()
                    with open(mem, 'a') as f: f.write(''.join(p + '\n' for p, _ in good))
                    done.update(p for p, _ in good)
                    print(time.strftime('%H:%M:%S'), f'записано {len(good)} (строк изменено {hit}), всего {len(done)}' + (f'; не сошлись {bad[:3]}' if bad else ''), flush=True)
        except Exception as e:
            print(time.strftime('%H:%M:%S'), 'ошибка, повторю:', repr(e)[:200], flush=True)
        time.sleep(a.every)


if __name__ == '__main__':
    main()
