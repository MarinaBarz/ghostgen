"""Очередь пачек в Redis. Единица аренды — пачка (chunk, обычно 200 товаров); единица учёта — товар.
Ключи (префикс gg:<RECIPE_ID>:):
  pending            LIST  пачки, ждущие машину (вытесненные возвращаются в начало)
  leased             ZSET  пачка -> момент окончания аренды
  owner              HASH  пачка -> машина
  size               HASH  пачка -> число товаров
  known              SET   все поставленные пачки (защита от повторной постановки)
  pids               SET   все товары во всех пачках (один товар — одна пачка)
  done:<chunk>       SET   товары пачки, по которым есть итог (успех или отказ)
  result             HASH  товар -> json итога (первый итог побеждает, повтор не перезапишет)
  complete           SET   закрытые пачки
  nodes              HASH  машина -> json последнего сигнала жизни
Redis общий (боевой, allkeys-lru): значения короткие, истина — итоги в хранилище (results/), см. done() в fetcher/generator.
Все изменения состояния — Lua-скриптами (атомарно): две машины не возьмут одну пачку, аренду продлевает только владелец."""
import json, time
import redis
from .config import RECIPE_ID

CLAIM = """
local c = redis.call('LPOP', KEYS[1])
if not c then return false end
redis.call('ZADD', KEYS[2], ARGV[2], c)
redis.call('HSET', KEYS[3], c, ARGV[1])
return c"""
RENEW = """
if redis.call('HGET', KEYS[2], ARGV[1]) == ARGV[2] then
  redis.call('ZADD', KEYS[1], 'XX', ARGV[3], ARGV[1]) return 1 end
return 0"""
REAP = """
local ex = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, c in ipairs(ex) do
  redis.call('ZREM', KEYS[1], c); redis.call('HDEL', KEYS[2], c)
  if redis.call('SISMEMBER', KEYS[4], c) == 0 then redis.call('LPUSH', KEYS[3], c) end
end
return ex"""
RELEASE = """
if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('ZREM', KEYS[1], ARGV[1]); redis.call('HDEL', KEYS[2], ARGV[1])
if redis.call('SISMEMBER', KEYS[4], ARGV[1]) == 0 then redis.call('LPUSH', KEYS[3], ARGV[1]) end
return 1"""
# итог по товару: HSETNX — первый итог побеждает; товар в done пачки; если пачка собрана целиком — закрыть её
FINISH = """
local first = redis.call('HSETNX', KEYS[1], ARGV[1], ARGV[3])
redis.call('SADD', KEYS[2], ARGV[1])
if redis.call('SCARD', KEYS[2]) >= tonumber(redis.call('HGET', KEYS[3], ARGV[2]) or '999999') then
  redis.call('SADD', KEYS[4], ARGV[2]); redis.call('ZREM', KEYS[5], ARGV[2]); redis.call('HDEL', KEYS[6], ARGV[2])
end
return first"""
ENQUEUE = """
if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2]); redis.call('RPUSH', KEYS[3], ARGV[1])
return 1"""


class Queue:
    def __init__(self, url, recipe_id=RECIPE_ID):
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=30, socket_connect_timeout=10,
                                      health_check_interval=30, retry_on_timeout=True)
        self.p = f'gg:{recipe_id}:'
        k = lambda n: self.p + n
        self.K = {n: k(n) for n in ('pending', 'leased', 'owner', 'size', 'known', 'pids', 'result', 'complete', 'nodes')}
        self._claim, self._renew, self._reap, self._release, self._finish, self._enqueue = (
            self.r.register_script(s) for s in (CLAIM, RENEW, REAP, RELEASE, FINISH, ENQUEUE))

    def done_key(self, chunk): return f'{self.p}done:{chunk}'

    # --- постановка (Mac) ---
    def register_pids(self, pids):
        """Все товары новой пачки должны быть новыми для этого рецепта: один товар не попадёт в две пачки."""
        pipe = self.r.pipeline()
        for p in pids: pipe.sismember(self.K['pids'], p)
        dup = [p for p, x in zip(pids, pipe.execute()) if x]
        if dup: raise ValueError(f'товары уже стоят в очереди: {dup[:5]}… ({len(dup)})')
        self.r.sadd(self.K['pids'], *pids)

    def enqueue(self, chunk, size):
        return bool(self._enqueue(keys=[self.K['known'], self.K['size'], self.K['pending']], args=[chunk, size]))

    # --- машина ---
    def claim(self, node, ttl):
        c = self._claim(keys=[self.K['pending'], self.K['leased'], self.K['owner']], args=[node, time.time() + ttl])
        return c or None

    def renew(self, chunk, node, ttl):
        return bool(self._renew(keys=[self.K['leased'], self.K['owner']], args=[chunk, node, time.time() + ttl]))

    def owns(self, chunk, node): return self.r.hget(self.K['owner'], chunk) == node

    def owned(self, node): return [c for c, n in self.r.hgetall(self.K['owner']).items() if n == node]

    def release(self, chunk, node):
        return bool(self._release(keys=[self.K['leased'], self.K['owner'], self.K['pending'], self.K['complete']], args=[chunk, node]))

    def quarantine(self, chunk, reason):
        """Пачка с битым манифестом: снять с аренды и не возвращать в очередь (разбирается человеком, см. status.py)."""
        g = self.r.pipeline()
        g.zrem(self.K['leased'], chunk); g.hdel(self.K['owner'], chunk); g.lrem(self.K['pending'], 0, chunk)
        g.hset(self.p + 'bad', chunk, reason[:500]); g.execute()

    def reap(self):
        return self._reap(keys=[self.K['leased'], self.K['owner'], self.K['pending'], self.K['complete']], args=[time.time()])

    def is_done(self, pid): return self.r.hexists(self.K['result'], pid)

    def finish(self, pid, chunk, result):
        """True — это первый итог по товару; False — итог уже был (повтор после вытеснения), новый не записан.
        В Redis — только короткая отметка «ok|пачка» / «failed|пачка|этап|причина» (~60 байт): полный итог лежит в хранилище
        results/…json. Redis общий и с вытеснением (allkeys-lru): чем меньше пишем, тем меньше давим на чужие ключи."""
        v = f"ok|{chunk}" if result.get('status') == 'ok' else f"failed|{chunk}|{result.get('stage', '')}|{str(result.get('reason', ''))[:120]}"
        return bool(self._finish(keys=[self.K['result'], self.done_key(chunk), self.K['size'], self.K['complete'], self.K['leased'], self.K['owner']],
                                 args=[pid, chunk, v]))

    def beat(self, node, info): self.r.hset(self.K['nodes'], node, json.dumps({**info, 't': time.time()}))

    # --- обзор ---
    def stats(self):
        g = self.r.pipeline()
        g.llen(self.K['pending']); g.zcard(self.K['leased']); g.scard(self.K['complete']); g.scard(self.K['known'])
        g.hlen(self.K['result']); g.scard(self.K['pids']); g.hgetall(self.K['nodes'])
        pend, leased, comp, known, res, pids, nodes = g.execute()
        return {'пачек': known, 'ждут': pend, 'в работе': leased, 'закрыто': comp, 'товаров': pids, 'с итогом': res,
                'машины': {n: json.loads(v) for n, v in nodes.items()}}

    def results(self):
        for pid, v in self.r.hscan_iter(self.K['result'], count=1000):
            f = v.split('|', 3); yield pid, {'status': f[0], 'chunk': f[1] if len(f) > 1 else None,
                                              'stage': f[2] if len(f) > 2 else None, 'reason': f[3] if len(f) > 3 else ''}
