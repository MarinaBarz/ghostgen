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
SHOES = {'boot', 'sneaker', 'sandal', 'shoe', 'loafer', 'ankle boot', 'slip-on shoe', 'clog'}
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
    return p + (HEM if noun in UPPER else '')

