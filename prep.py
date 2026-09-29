"""Подготовка товара на машине (перенесено с Mac, experiments/2026-09-28_fullrun/build_jobs.py, 29.09) — «сырые» пачки.
Сырой товар: {pid, title, category, urls: [фото магазина по порядку], pi: главное фото или None, in_look, check_only}.
  скачать фото (крупная версия ссылки по правилам хостов prep_upsize_rules.json; общий срок 25 с на фото; кривые сертификаты — без проверки)
  → фото < 380 точек по короткой стороне не берём → дубли (после обрезки полей) → на главном фото нет человека (RF-DETR COCO < 0,05)
  → «уже предметное»: копия фото в shots/<pid>.jpg, итог skip (для поля «фото для коллажа»); check_only — дальше не идём
  → вид вещи по названию (prompts.noun); «не понятен» — skip → вещь ищем детектором m896c14 на ВСЕХ фото (до 4), главное — где она крупнее;
    не нашли — skip → кадр вещи (чужие рамки закрашены, +6 % поля) + ещё до 2 фото → все входы 768×1024 → промпт prompts.prompt.
Возвращает ('job', [PIL…], prompt, noun) или ('skip', причина, {доп. поля итога}).
Детекторы — на видеокарте, если есть (доли секунды на фото); общий замок: из нескольких потоков модель вызывается по очереди."""
import io, json, os, re, ssl, threading, time, urllib.error, urllib.parse, urllib.request
import numpy as np
from PIL import Image, ImageChops, ImageDraw
from .prompts import prompt, noun, GARMENT, ACC, SHOES

HERE = os.path.dirname(os.path.abspath(__file__))
MIN = 380
RULES = json.load(open(os.path.join(HERE, 'prep_upsize_rules.json')))
CLASSES = ['outer', 'top', 'bottom', 'dress', 'shoes', 'bag', 'headwear', 'jewelry', 'eyewear', 'belt', 'scarf', 'gloves', 'legwear', 'lingerie']
try:
    import certifi; _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception: _CTX = ssl.create_default_context()
_NOVERIFY = ssl._create_unverified_context()     # публичные картинки: у части магазинов кривые сертификаты


# --- крупная версия ссылки (правила подобраны experiments/2026-09-28_fullrun/upsize.py) ---
BIG = ['2000', '1600', '1280', '1200', '1024', '1000', '900', '800', '600']


def variants(u):
    s = urllib.parse.urlsplit(u); v = []; p = s.path
    m = re.match(r'(.*/upload/)resize_cache/(.+?)/\d+_\d+_\d+[^/]*/(.+)$', p)
    if m: v.append(('битрикс-оригинал', s._replace(path=m.group(1) + m.group(2) + '/' + m.group(3)).geturl()))
    for a, b in [('/small/', '/big/'), ('/small/', '/large/'), ('/small/', '/'), ('_small', '_big'), ('_small', ''), ('/thumb/', '/'),
                 ('/md/', '/lg/'), ('/md/', '/xl/'), ('/md/', '/orig/'), ('profile=medium', 'profile=large'), ('profile=medium', 'profile=original'),
                 ('/small/compress/', '/big/compress/'), ('/small/compress/', '/'), ('d_large', 'd_original')]:
        if a in u: v.append((f'{a}→{b}', u.replace(a, b)))
    q = urllib.parse.parse_qsl(s.query)
    if any(k in ('width', 'w', 'wid') for k, _ in q):
        q2 = [(k, '2000' if k in ('width', 'w', 'wid') else val) for k, val in q if k not in ('height', 'h', 'hei')]
        v.append(('ширина=2000', s._replace(query=urllib.parse.urlencode(q2)).geturl()))
    for m in re.finditer(r'(?<=[/_x=])(\d{2,4})(?=[/_xWH.]|$)', p):
        n = int(m.group(1))
        if 16 <= n <= 1000:
            for b in BIG:
                if int(b) > n: v.append((f'число {n}→{b} (поз {m.start()})', s._replace(path=p[:m.start()] + b + p[m.end():]).geturl()))
    for m in re.finditer(r'(\d{2,4})x(\d{2,4})', p):
        w, h = int(m.group(1)), int(m.group(2))
        for k in (4, 3, 2): v.append((f'{w}x{h}×{k}', s._replace(path=p[:m.start()] + f'{w*k}x{h*k}' + p[m.end():]).geturl()))
    seen = set(); out = []
    for name, x in v:
        if x != u and x not in seen: seen.add(x); out.append((name, x))
    return out[:40]


def big(u):
    rule = RULES.get(urllib.parse.urlsplit(u).netloc)
    if not rule: return u
    for n2, x in variants(u):
        if n2 == rule: return x
    m = re.match(r'число (\d+)→(\d+)', rule)
    if m:
        for n2, x in variants(u):
            if n2.startswith(f'число {m.group(1)}→{m.group(2)} '): return x
    return u


# --- скачивание ---
def _open(u):
    req = urllib.request.Request(urllib.parse.quote(u, safe=':/?&=%'), headers={'User-Agent': 'Mozilla/5.0'})
    try: return urllib.request.urlopen(req, timeout=15, context=_CTX)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, 'reason', None), ssl.SSLError): return urllib.request.urlopen(req, timeout=15, context=_NOVERIFY)
        raise


def fetch(u):
    """Картинка RGB (прозрачность — на белом), не меньше MIN по короткой стороне, иначе None. Общий срок 25 с."""
    try:
        t0 = time.time(); buf = io.BytesIO()
        with _open(u) as resp:
            while True:
                b = resp.read(65536)
                if not b: break
                buf.write(b)
                if time.time() - t0 > 25 or buf.tell() > 40_000_000: return None
        im = Image.open(io.BytesIO(buf.getvalue()))
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGBA'); bg = Image.new('RGB', im.size, (255, 255, 255)); bg.paste(im, mask=im.getchannel('A')); im = bg
        im = im.convert('RGB')
        if min(im.size) < MIN: return None
        im.thumbnail((1280, 1280)); return im
    except Exception:
        return None


def fetch_big(u):
    """(картинка, ссылка, по которой она скачана) — сначала крупная версия ссылки."""
    x = big(u); im = fetch(x) if x != u else None
    if im is None: im, x = fetch(u), u
    return im, x


# --- картинки ---
def trim(im):
    bb = ImageChops.difference(im, Image.new('RGB', im.size, im.getpixel((0, 0)))).convert('L').point(lambda v: 255 if v > 12 else 0).getbbox()
    return im.crop(bb) if bb else im


def sig(im): return np.asarray(trim(im).convert('L').resize((32, 32)), float)


def cut(im, anchor, others):
    W, H = im.size; x, y, w, h = anchor; im = im.copy(); d = ImageDraw.Draw(im); bg = im.getpixel((2, 2))
    ax0, ay0, ax1, ay1 = x * W, y * H, (x + w) * W, (y + h) * H
    for _, (ox, oy, ow, oh) in others:
        bx0, by0, bx1, by1 = ox * W, oy * H, (ox + ow) * W, (oy + oh) * H
        for r in [(bx0, by0, bx1, min(by1, ay0)), (bx0, max(by0, ay1), bx1, by1), (bx0, by0, min(bx1, ax0), by1), (max(bx0, ax1), by0, bx1, by1)]:
            if r[2] > r[0] and r[3] > r[1]: d.rectangle(r, fill=bg)
    p = 0.06
    return im.crop((max(0, int((x - p * w) * W)), max(0, int((y - p * h) * H)), min(W, int((x + w * (1 + p)) * W)), min(H, int((y + h * (1 + p)) * H))))


def norm(im, W=768, H=1024):
    """Все входы — один размер 3:4, края цветом фона: под каждый новый размер машина собирает свои INT8-ядра (память росла до 47 ГБ)."""
    a = np.asarray(im); edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]); bg = tuple(int(x) for x in np.median(edge, 0))
    k = min(W / im.width, H / im.height); im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    out = Image.new('RGB', (W, H), bg); out.paste(im, ((W - im.width) // 2, (H - im.height) // 2)); return out


class Prep:
    def __init__(self, coco_weights=None, item_weights=None):
        import rfdetr                                               # устройство (видеокарта, если есть) rfdetr выбирает сам
        self.lock = threading.Lock()
        self.coco = rfdetr.RFDETRMedium(**({'pretrain_weights': coco_weights} if coco_weights else {}))
        self.det = rfdetr.RFDETRMedium(pretrain_weights=item_weights, num_classes=14, resolution=896)

    def person(self, im):
        w, h = im.size
        with self.lock: d = self.coco.predict(im, threshold=0.4)
        return max([float((x2 - x1) * (y2 - y1) / (w * h)) for (x1, y1, x2, y2), c in zip(d.xyxy, d.class_id) if int(c) == 1], default=0.0)

    def boxes(self, im):
        W, H = im.size
        with self.lock: dd = self.det.predict(im, threshold=0.35)
        return [(CLASSES[int(c)], [float(x1 / W), float(y1 / H), float((x2 - x1) / W), float((y2 - y1) / H)]) for (x1, y1, x2, y2), c in zip(dd.xyxy, dd.class_id)]

    def __call__(self, it, pool):
        """Сырой товар -> ('job', ims, prompt, noun) | ('skip', причина, доп. поля)."""
        flux = set(it.get('flux') or [])
        urls = [u for u in dict.fromkeys(it.get('urls') or []) if u and u not in flux]
        got = list(pool.map(fetch_big, urls))
        if it.get('pi') and it['pi'] not in flux:
            im = fetch(it['pi'])
            if im is not None: got.append((im, it['pi']))
        keep = []                                                   # [(картинка, подпись, ссылка)] без дублей
        for im, u in got:
            if im is None: continue
            s_ = sig(im); hit = next((i for i, (g, gs, gu) in enumerate(keep) if np.abs(s_ - gs).mean() < 6), None)
            if hit is None: keep.append((im, s_, u))
            elif im.size[0] * im.size[1] > keep[hit][0].size[0] * keep[hit][0].size[1]: keep[hit] = (im, s_, u)
        if not keep: return ('skip', 'нет фото ≥380', {})
        main, _, main_url = keep[0]
        if self.person(main) < 0.05:
            return ('skip', 'уже предметное', {'shot': main, 'shot_url': main_url})
        if it.get('check_only'): return ('skip', 'не предметное (только проверка)', {})
        nn = noun(it.get('title'), it.get('category'))
        if nn == 'fashion product': return ('skip', 'вид вещи не понятен', {})
        want = GARMENT.get(nn, ACC.get(nn, {'shoes'} if nn in SHOES else None)); mi = 0; crop = None
        if want:
            best = None
            for i, (im, _, _) in enumerate(keep[:4]):
                bx = self.boxes(im)
                if want == 'union':
                    cl = [b for c, b in bx if c in ('top', 'bottom', 'dress', 'outer')]
                    anchor = [min(b[0] for b in cl), min(b[1] for b in cl), max(b[0] + b[2] for b in cl) - min(b[0] for b in cl),
                              max(b[1] + b[3] for b in cl) - min(b[1] for b in cl)] if cl else None
                    others = [[c, b] for c, b in bx if c not in ('top', 'bottom', 'dress', 'outer')]
                else:
                    cand = [b for c, b in bx if c in want]; anchor = max(cand, key=lambda b: b[2] * b[3]) if cand else None
                    others = [[c, b] for c, b in bx if b != anchor]
                if anchor and (best is None or anchor[2] * anchor[3] > best[0]): best = (anchor[2] * anchor[3], i, anchor, others)
            if best is None: return ('skip', 'вещь не найдена на фото', {'noun': nn})
            _, mi, anchor, others = best; crop = cut(keep[mi][0], anchor, others)
        rest = [x for i, (x, _, _) in enumerate(keep) if i != mi][:2]
        ims = [norm(x) for x in ([crop] if crop is not None else [main]) + rest]
        return ('job', ims, prompt(nn, len(ims), it.get('title')), nn)
