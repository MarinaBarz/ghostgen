"""Проверенные промпты (26–28.09): одежда — ghost mannequin (шаблон v3 26.09), обувь — одна туфля в профиль, аксессуары — вещь сама
по себе (ветка acc из runjson.v3_prompt). Название магазина — «The product is: «…».» (пробелы схлопнуты); для верхов — правило подола.
  prompt(noun, n_photos, title) -> str"""
import re


def clean_title(t): return re.sub(r'\s+', ' ', t or '').strip()


# вид вещи -> классы детектора m896c14 для кадра вещи ('union' — комплект: объединение рамок одежды)
GARMENT = {'dress': {'dress'}, 'trousers': {'bottom'}, 'shorts': {'bottom'}, 'skirt': {'bottom'}, 'jacket': {'outer', 'top'},
           'coat': {'outer', 'top'}, 'outerwear garment': {'outer', 'top'}, 'top': {'top', 'outer'}, 'sweater': {'top', 'outer'},
           'shirt': {'top', 'outer'}, 't-shirt': {'top', 'outer'}, 'lingerie item': {'lingerie', 'top'}, 'legwear item': {'legwear'},
           'robe': {'outer', 'dress'}, 'matching set': 'union'}
SHOES = {'boot', 'sneaker', 'sandal', 'shoe', 'loafer', 'ankle boot', 'slip-on shoe', 'clog', 'mule'}
ACC = {'jewelry item': {'jewelry'}, 'bag': {'bag'}, 'hat': {'headwear'}, 'belt': {'belt'}, 'scarf': {'scarf'}, 'glove': {'gloves'},
       'eyewear': {'eyewear'}, 'fashion product': None, 'candle': None, 'keychain': None}
UPPER = {'top', 'sweater', 'shirt', 't-shirt', 'jacket', 'coat', 'outerwear garment'}
HEM = (" Show only this garment: do not show any skirt, trousers, shorts or other garment that appears below or under it in the photos;"
       " the image ends at this garment's own hem.")

def prompt(noun, n, title):
    title = clean_title(title)
    t = f" The product is: «{title}»." if title else ''
    roles = f"Image 1 is the {noun} itself; Images 2 to {n} are more photos of the same {noun} for reference. " if n > 1 else ''
    if noun in SHOES:
        return (roles + f"A professional e-commerce product photo of a single {noun} from Image 1 on a solid #fafaf7 background: one {noun} only, "
                f"shown in side profile (outer side), toe pointing left, standing naturally on its sole. No feet, no legs, no second {noun}, "
                f"no other items. No labels.{t} Keep the exact colour, material, heel, toe shape, straps, buckles, tassels and hardware of the "
                f"{noun} in Image 1. Luxury catalog look, soft even studio lighting with a subtle natural shadow under the sole.")
    if noun in ACC:
        return (roles + f"Create a professional front-view catalog product photo of the {noun} from Image 1 on a solid #fafaf7 background, "
                f"shown on its own, with no person, no body parts and no other items — only the {noun}.{t} Straps, chains and hardware that "
                f"belong to the {noun} stay. Keep the exact colour of the {noun} in Image 1"
                + ("; the other photos may show other colourways — ignore their colour. " if n > 1 else ". ")
                + f"The {noun} is perfectly presented, with its natural texture, construction and realistic shape. Soft, diffused, even studio "
                "lighting with subtle natural shading; luxury fashion house catalog photography.")
    p = (roles + f"Create a cutout image of the {noun} from Image 1 on a solid #fafaf7 background, without a model, using a Ghost Mannequin "
         f"technique or a realistic 3D product render style. Show only this one {noun} — no other garments or accessories.{t} Remove any "
         f"necklace, chain, jewellery, bag or strap worn over it. Keep the exact colour of the {noun} in Image 1 — other photos may show other "
         "colourways of the product; ignore their colour. No labels of any kind. Front view. The final result should have the look and quality "
         "of a luxury fashion house catalog photography. The garment should look professionally steamed, perfectly presented, and meticulously "
         "retouched while preserving the natural texture, construction, and realistic shape. Soft, diffused, even studio lighting with subtle "
         "natural shading. The product in the final image must be the one from Image 1.")
    # 29.09: приписку про изнанку (v6) пользователь отменила — промпт одежды как в v5
    return p + (HEM if noun in UPPER else '')



# вид вещи по названию и, если не узнали, по категории товара в базе.
# 28.09: слова ищутся только С НАЧАЛА слова («оторочка» — не «очки», «джинсовая» — не «джинсы»), а из нескольких найденных берётся
# стоящее в названии раньше («Куртка с поясом» — куртка); «костюм/комплект» побеждает всегда. Прежняя версия (подстрока, первое правило
# в списке) дала «парка с меховой оторочкой» -> очки и «джинсовая куртка» -> брюки.
SHOE_STEMS = {"сапог": "boot", "сапож": "boot", "ботин": "boot", "кроссов": "sneaker", "кеды": "sneaker", "кедах": "sneaker", "лофер": "loafer",
              "туфл": "shoe", "босонож": "sandal", "сандал": "sandal", "мюли": "mule", "ботильон": "ankle boot", "угги": "boot",
              "дутики": "boot", "казаки": "boot", "слипон": "slip-on shoe", "шлепан": "sandal", "шлёпан": "sandal", "вьетнамк": "sandal",
              "клоги": "clog", "балетк": "shoe", "мокасин": "loafer", "эспадрил": "shoe", "челси": "boot"}
SETS = r"\b(?:костюм(?:ы|а|ом)?\b|комплект(?:ы|а|ом|е)?\b|набор(?:ы|а|ом)?\b|пижам[аыуе]\b|термокомби|двойк[аиу]\b|тройк[аиу]\b)"   # «костюмная ткань» — не комплект
WORDS = [(r"плать|сарафан", "dress"), (r"юбк", "skirt"), (r"брюк|джинсы\b|джинс\b|леггинс|легинс|джоггер|кюлот|палаццо|чинос|бананы\b", "trousers"),
         (r"шорт|велосипедк|бермуд", "shorts"), (r"куртк|жакет|пиджак|блейзер|бомбер|олимпийк|ветровк|косух|анорак|жилет|смокинг", "jacket"),
         (r"пальто|плащ|тренч|шуб|дубл[её]нк|парк[аиу]?\b|пуховик|полупальто|бушлат|пончо", "coat"), (r"рубаш|блуз|сорочк", "shirt"),
         (r"футболк|лонгслив|поло\b|топ\b|топик|майк|боди\b|корсет|бюстье", "top"), (r"бра\b", "lingerie item"),   # 28.09: футболка/лонгслив — просто top
         (r"свитер|джемпер|кардиган|худи|толстовк|кофт|рашгард|пуловер|свитшот|водолазк", "sweater"),
         (r"кимоно|накидк|халат", "robe"), (r"комбинезон", "matching set"),
         (r"сумк|рюкзак|шопер|холдер|клатч|кошел[её]к|портмоне|визитниц|картхолдер|чехол|косметичк", "bag"), (r"очки\b|очков\b", "eyewear"),
         (r"ремень|ремн|пояс\b|пояса\b", "belt"),
         (r"кольц|серьг|серёжк|сережк|колье|цепочк|цепь|браслет|кафф|брошь|брош\b|чокер|крестик|подвеск|кулон|украшени|значок|пирсинг", "jewelry item"),
         (r"шапк|кепк|панам|шляп|чепец|бандан|косынк|ободок|берет|бейсболк|балаклав", "hat"), (r"шарф|палантин|платок|снуд", "scarf"),
         (r"митенк|перчатк|варежк", "glove"), (r"стринг|плавк|трусы|бюстгальтер|купальник|бикини", "lingerie item"),
         (r"носк|колгот|гольф", "legwear item"), (r"свеча", "candle"), (r"брелок", "keychain")]
CATEGORY = {'Tops': 'top', 'Outerwear': 'outerwear garment', 'Pants': 'trousers', 'Jewelry': 'jewelry item', 'Dresses & Jumpsuits': 'dress',
            'Dresses': 'dress', 'Footwear': 'shoe', 'Skirts': 'skirt', 'Bags & Luggage': 'bag', 'Underwear & Swimwear': 'lingerie item',
            'Shorts': 'shorts', 'Legwear': 'legwear item', 'Accessories': 'fashion product'}
_CLOTHES = {'dress', 'skirt', 'trousers', 'shorts', 'jacket', 'coat', 'shirt', 'top', 'sweater'}
_RX = [(re.compile(r"\b(?:" + p + ")"), w) for p, w in WORDS] + [(re.compile(r"\b" + st), w) for st, w in SHOE_STEMS.items()]


def noun(title, category=None):
    t = (title or '').lower().replace('ё', 'е')
    if re.search(SETS, t): return 'matching set'
    found = []
    for rx, w in _RX:
        m = rx.search(t)
        if m: found.append((m.start(), m.end(), w))
    found.sort()
    # «Кардиган и юбка», «Топ + шорты», «Жакет, брюки»: две разные вещи одежды через «и» / «+» / запятую — комплект (28.09)
    cl = [f for f in found if f[2] in _CLOTHES]
    for a, b in zip(cl, cl[1:]):
        if a[2] != b[2] and re.fullmatch(r"[\w\s-]*?(\s+и\s+|\s*\+\s*|\s*,\s*)[\w\s-]*", t[a[1]:b[0]] or ' '):
            if re.search(r"\s+и\s+|\+|,", t[a[1]:b[0]]): return 'matching set'
    return found[0][2] if found else CATEGORY.get(category, 'fashion product')
