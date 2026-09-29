"""Настройки ghostgen: всё из переменных окружения, чтобы один и тот же образ работал на любой машине.
Рецепт генерации зафиксирован здесь и входит в ключи S3 (outputs/<RECIPE_ID>/...): смена рецепта не перезапишет старые картинки."""
import os, socket, uuid

RECIPE = {
    'model': 'Qwen-Image-2.1 SDNQ-4bit',
    'turbo_lora': 'Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors', 'turbo_weight': 1.0,
    'ghost_lora': 'ghost_v2_lora_000000500.safetensors', 'ghost_weight': 0.5,
    'steps': 6, 'sigmas': [1.0, 0.9375, 0.875, 0.75, 0.5, 0.25], 'resolution': 512, 'output_resolution': 512,
    'true_cfg_scale': 1.0, 'int8_matmul': True,
}
RECIPE_ID = os.environ.get('GG_RECIPE_ID', 'ghost-t6-gw050-512-v1')


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v in (None, '') else cast(v)


def _redis_url():
    if os.environ.get('GG_REDIS_URL'): return os.environ['GG_REDIS_URL']
    h = os.environ.get('REDIS_HOST')
    if not h: return 'redis://localhost:6379/0'
    from urllib.parse import quote
    u, pw = os.environ.get('REDIS_USERNAME', ''), os.environ.get('REDIS_PASSWORD', '')
    auth = f"{quote(u)}:{quote(pw)}@" if (u or pw) else ''
    return f"redis://{auth}{h}:{os.environ.get('REDIS_PORT', '6379')}/0"


class Cfg:
    redis_url = _redis_url()
    storage = env('GG_STORAGE', 's3')                 # s3 | local
    # те же имена, что в .env приложения (AWS_S3_*), либо свои GG_S3_*
    s3_bucket = env('GG_S3_BUCKET') or env('AWS_S3_BUCKET')
    s3_endpoint = env('GG_S3_ENDPOINT') or env('AWS_S3_ENDPOINT')   # S3-совместимое хранилище
    s3_region = env('GG_S3_REGION') or env('AWS_REGION', 'us-east-1')
    s3_prefix = env('GG_S3_PREFIX', 'ghostgen/')         # всё своё в общем бакете — под этим префиксом
    ghost_sha256 = env('GG_GHOST_SHA256', 'aede721ed535013a3f3c88520b1b87b23d9c11f2b9c60bbaf38fb759aea31694')             # контрольная сумма адаптера в хранилище (models/…)
    local_store = env('GG_LOCAL_STORE', '/tmp/ghostgen_store')
    work = env('GG_WORK', '/root/ghostgen_work')
    # Salad задаёт SALAD_MACHINE_ID; иначе — имя хоста + случайный хвост (две машины не получат один id)
    node = env('GG_NODE_ID') or f"{os.environ.get('SALAD_MACHINE_ID') or socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    lease_ttl = env('GG_LEASE_TTL', 900, int)          # аренда пачки, с
    heartbeat = env('GG_HEARTBEAT', 60, int)           # продление аренды, с
    prefetch = env('GG_PREFETCH', 48, int)             # сколько товаров держать скачанными впереди генерации
    engine = env('GG_ENGINE', 'qwen')                  # qwen | fake (для проверки без видеокарты)
    max_step_sec = env('GG_MAX_STEP_SEC', 0.45, float) # шаг медленнее (карты 24 ГБ) — машина плохая, просим Salad переселить
    small_gpu_factor = env('GG_SMALL_GPU_FACTOR', 2.5, float)   # карты < 22 ГБ: порог × этот множитель (замер уточнит)
    fake_sec = env('GG_FAKE_SEC', 0.2, float)
    model_dir = env('GG_MODEL_DIR', '/models/qwen-image-21-sdnq4')
    lora_dir = env('GG_LORA_DIR', '/models/lora')
    on_salad = bool(os.environ.get('SALAD_MACHINE_ID'))
    # подготовка на машине (сырые пачки, 29.09): детектор вещей m896c14 — наш, закрытый, из хранилища models/ со сверкой sha256
    item_det = env('GG_ITEM_DET', '/models/det/m896c14.pth')
    item_det_key = env('GG_ITEM_DET_KEY', 'models/m896c14_checkpoint_best_ema.pth')
    item_det_sha256 = env('GG_ITEM_DET_SHA256', 'c6b17fe43aa9f8eff4ad0b9ddffdd6018d3fcf8c84b0cdb10f6c2324f45ca6c0')
    prep_threads = env('GG_PREP_THREADS', 4, int)      # сколько товаров готовить одновременно (скачивание фото — основное время)

    @classmethod
    def dirs(cls):
        d = {k: os.path.join(cls.work, k) for k in ('inbox', 'out', 'chunks', 'quarantine', 'state')}
        for p in d.values(): os.makedirs(p, exist_ok=True)
        return d
