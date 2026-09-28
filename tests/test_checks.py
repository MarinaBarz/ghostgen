"""Проверка защит от путаницы (без Redis): выгрузчик должен отвергнуть подменённые результаты.
Случаи: чужая картинка под чужим именем; json от другого товара; входы не те, что в манифесте; картинка испорчена после генерации;
чужой рецепт. Правильный результат — должен пройти.
  ghostgen/.venv/bin/python -m ghostgen.tests.test_checks   (из папки looks_pipeline/services)"""
import io, json, os, shutil, tempfile
from PIL import Image, ImageDraw, PngImagePlugin
T = tempfile.mkdtemp(prefix='gg_chk_'); os.environ['GG_WORK'] = T
from ghostgen.common import sha256          # noqa: E402
from ghostgen.config import Cfg, RECIPE_ID  # noqa: E402
from ghostgen.uploader import validate      # noqa: E402
dirs = Cfg.dirs()
A, B, CH = 'cmtestaaaaaaaaaa01', 'cmtestbbbbbbbbbb02', 'c00000-deadbeef'
man = {'chunk': CH, 'recipe_id': RECIPE_ID, 'items': [{'pid': A, 'inputs': [{'k': 0, 'sha256': 'a' * 64}]},
                                                       {'pid': B, 'inputs': [{'k': 0, 'sha256': 'b' * 64}]}]}
json.dump(man, open(os.path.join(dirs['chunks'], f'{CH}.json'), 'w'))


def make(pid, inputs, recipe=RECIPE_ID, chunk=CH, color=(200, 30, 30)):
    im = Image.new('RGB', (512, 512), color); ImageDraw.Draw(im).line([(0, 0), (511, 511)], fill=0, width=3)
    info = PngImagePlugin.PngInfo()
    for k, v in (('gg_pid', pid), ('gg_chunk', chunk), ('gg_recipe', recipe), ('gg_seed', '42'), ('gg_inputs', ','.join(inputs))): info.add_text(k, v)
    b = io.BytesIO(); im.save(b, 'PNG', pnginfo=info); png = b.getvalue()
    return png, {'pid': pid, 'chunk': chunk, 'recipe_id': recipe, 'seed': 42, 'inputs': inputs, 'out_sha256': sha256(png), 'sec': 1}


def put(name, png, meta):
    open(os.path.join(dirs['out'], f'{name}.png'), 'wb').write(png); json.dump(meta, open(os.path.join(dirs['out'], f'{name}.json'), 'w'))


def expect(label, name, ok):
    try: validate(name, dirs); got = True; why = ''
    except Exception as e: got = False; why = str(e)
    print(f"{'OK ' if got == ok else 'ОШИБКА'} {label}: {'принято' if got else 'отвергнуто — ' + why}")
    for ext in ('png', 'json'):
        p = os.path.join(dirs['out'], f'{name}.{ext}')
        if os.path.exists(p): os.remove(p)
    return got == ok


res = []
png, meta = make(A, ['a' * 64]); put(A, png, meta); res.append(expect('правильный результат', A, True))
png, meta = make(A, ['a' * 64]); put(B, png, {**meta, 'pid': B}); res.append(expect('картинка товара A под именем B', B, False))
pngB, metaB = make(B, ['b' * 64]); png, meta = make(A, ['a' * 64]); put(A, png, metaB); res.append(expect('json от товара B у картинки A', A, False))
png, meta = make(A, ['b' * 64]); put(A, png, meta); res.append(expect('входы не те, что у товара в манифесте', A, False))
png, meta = make(A, ['a' * 64]); png2, _ = make(A, ['a' * 64], color=(10, 200, 10)); put(A, png2, meta); res.append(expect('картинка заменена после генерации', A, False))
png, meta = make(A, ['a' * 64], recipe='other-recipe'); put(A, png, meta); res.append(expect('чужой рецепт', A, False))
png, meta = make('cmtestzzzzzzzzzz99', ['a' * 64]); put('cmtestzzzzzzzzzz99', png, meta); res.append(expect('товара нет в пачке', 'cmtestzzzzzzzzzz99', False))
shutil.rmtree(T)
print('ВСЕ ЗАЩИТЫ СРАБОТАЛИ' if all(res) else 'ЕСТЬ ПРОБЛЕМЫ')
raise SystemExit(0 if all(res) else 1)
