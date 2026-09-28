"""Обзор и обслуживание очереди (на Mac).
  python -m ghostgen.status                   — сколько пачек/товаров, отказы по причинам, машины и их скорость
  python -m ghostgen.status --reap            — вернуть в очередь пачки с истёкшей арендой (машины делают это и сами)
  python -m ghostgen.status --requeue-failed  — отказы (кроме испорченных входов) собрать в новые пачки и поставить заново"""
import argparse, collections, json, time
from .common import log, sha256
from .config import Cfg, RECIPE, RECIPE_ID
from .queue import Queue
from .storage import make_storage


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--reap', action='store_true'); ap.add_argument('--requeue-failed', action='store_true')
    a = ap.parse_args(); q = Queue(Cfg.redis_url)
    if a.reap: print('возвращены:', q.reap())
    s = q.stats(); now = time.time()
    fails = collections.Counter(); failed = []
    for pid, r in q.results():
        if r.get('status') != 'ok': fails[f"{r.get('stage')}: {r.get('reason', '')[:60]}"] += 1; failed.append((pid, r))
    print(f"рецепт {RECIPE_ID}\nпачек {s['пачек']}: ждут {s['ждут']}, в работе {s['в работе']}, закрыто {s['закрыто']}")
    print(f"товаров {s['товаров']}, с итогом {s['с итогом']} (из них отказов {len(failed)})")
    for why, n in fails.most_common(10): print(f'  отказ ×{n}: {why}')
    bad = q.r.hgetall(q.p + 'bad')
    if bad: print('пачки в карантине:', bad)
    for n, v in sorted(s['машины'].items()):
        g = v.get('gen', {}); b = v.get('bench', {})
        print(f"  машина {n}: {'жива' if now - v['t'] < 3 * Cfg.heartbeat else 'молчит'} ({now - v['t']:.0f} с), "
              f"шаг {b.get('step_sec', 0):.2f} с, сделано {g.get('done', 0)}, {g.get('avg_sec', 0):.1f} с/товар, пачки {v.get('пачки')}")
    if a.requeue_failed:
        st = make_storage(Cfg); retry = [(p, r) for p, r in failed if r.get('stage') != 'fetch']   # испорченный вход — чинить на подготовке
        by_chunk = collections.defaultdict(list)
        for p, r in retry: by_chunk[r.get('chunk')].append(p)
        items = []
        for ch, ps in by_chunk.items():
            man = json.loads(st.get(f'jobs/{ch}.json')); items += [it for it in man['items'] if it['pid'] in set(ps)]
        if not items: print('нечего ставить заново'); return
        g = q.r.pipeline()
        for it in items: g.hdel(q.K['result'], it['pid'])
        g.execute()
        start = int(q.r.scard(q.K['known']))
        for n, i in enumerate(range(0, len(items), 200)):
            part = items[i:i + 200]
            chunk = f'c{start + n:05d}-{sha256(",".join(it["pid"] for it in part).encode())[:8]}'
            st.put(f'jobs/{chunk}.json', json.dumps({'chunk': chunk, 'recipe_id': RECIPE_ID, 'recipe': RECIPE, 'items': part,
                                                     'retry': True}, ensure_ascii=False).encode(), {'chunk': chunk})
            q.enqueue(chunk, len(part)); log('status', 'повтор поставлен', chunk, len(part))


if __name__ == '__main__':
    main()
