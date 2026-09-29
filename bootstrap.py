"""Модель и адаптеры на диск (если их ещё нет). Запускается супервизором node.py отдельным процессом ПАРАЛЛЕЛЬНО со скачиванием
фото и выгрузкой: генератор стартует, когда этот процесс завершился успешно. В образе v2 модель и turbo-адаптер уже внутри —
здесь докачивается только наш ghost-адаптер из хранилища (секунды).
Модель и turbo-адаптер — публичные, с Hugging Face; наш ghost-адаптер — закрытый, из хранилища (models/…), со сверкой sha256.
В образе ничего закрытого нет — образ можно держать публичным.
  python -m ghostgen.bootstrap   (выход 0 — всё на месте)"""
import os, time
from .common import log, sha256_file
from .config import Cfg, RECIPE
from .storage import make_storage


def main():
    cfg = Cfg
    if cfg.engine == 'qwen':
        from huggingface_hub import hf_hub_download, snapshot_download
        os.makedirs(cfg.lora_dir, exist_ok=True); t = time.time()
        if not os.path.exists(os.path.join(cfg.model_dir, 'model_index.json')):
            snapshot_download('SamuelTallet/Qwen-Image-2.1-SDNQ-4bit-dynamic-hadamard256', local_dir=cfg.model_dir, max_workers=16)
        if not os.path.exists(os.path.join(cfg.lora_dir, RECIPE['turbo_lora'])):
            hf_hub_download('Viggle/Qwen-Image-2.1-viggle-turbo', RECIPE['turbo_lora'], local_dir=cfg.lora_dir)
        g = os.path.join(cfg.lora_dir, RECIPE['ghost_lora'])
        if not os.path.exists(g):
            tmp = g + '.part'; make_storage(cfg).download(f"models/{RECIPE['ghost_lora']}", tmp)
            if sha256_file(tmp) != cfg.ghost_sha256: os.remove(tmp); raise SystemExit('sha256 ghost-адаптера не совпал — стоп')
            os.replace(tmp, g)
        log('bootstrap', f'модель и адаптеры на месте за {time.time() - t:.0f} с')
    # детектор вещей для подготовки сырых пачек — только на настоящей машине; сбой НЕ мешает генерации обычных пачек
    # (подготовка сама ждёт файл и без него откажет по товару с причиной «prep»)
    if cfg.engine == 'qwen' and cfg.item_det_sha256 and not os.path.exists(cfg.item_det):
        try:
            os.makedirs(os.path.dirname(cfg.item_det), exist_ok=True); tmp = cfg.item_det + '.part'
            make_storage(cfg).download(cfg.item_det_key, tmp)
            if sha256_file(tmp) != cfg.item_det_sha256: os.remove(tmp); raise ValueError('sha256 детектора вещей не совпал')
            os.replace(tmp, cfg.item_det); log('bootstrap', 'детектор вещей на месте')
        except Exception as e: log('bootstrap', 'детектор вещей не скачан (генерация идёт дальше):', repr(e)[:200])


if __name__ == '__main__':
    main()
