# Образ машины ghostgen v2: окружение, код, модель и turbo-адаптер внутри; закрытого ничего — образ публичный.
# Наш ghost-адаптер качается из хранилища со сверкой sha256 (bootstrap.py) параллельно со скачиванием фото.
# Собирается GitHub Actions (.github/workflows/image.yml) в ghcr.io; локально: docker build -t ghostgen:v1 .
FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HUB_ENABLE_HF_TRANSFER=1
RUN apt-get update -q && apt-get install -y -q --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN (command -v uv || pip install -q --break-system-packages uv) && uv pip install --system --break-system-packages -q \
      "git+https://github.com/huggingface/diffusers@9f1246971270c84dcbe71233edb7a519596a5d02" \
      "transformers==5.17.0" "accelerate==1.15.0" "sdnq==0.2.6" triton safetensors "huggingface_hub[hf_transfer]" \
      redis boto3 pillow numpy "rfdetr==1.10.1" certifi \
      $(python -c "import torch,torchvision;print(f'torch=={torch.__version__} torchvision=={torchvision.__version__}')")   # torch из основы образа не менять
# 28.09: sdnq собирает INT8-ядра под каждую длину промпта (dynamic=False) — память до 47 ГБ; один динамический блок на все длины
RUN F=$(python -c "import sdnq,os;print(os.path.dirname(sdnq.__file__))")/common.py && \
    sed -i 's/kwargs\["dynamic"\] = False/kwargs["dynamic"] = None/' $F && grep -q 'kwargs\["dynamic"\] = None' $F
# подготовка на машине (29.09): веса RF-DETR COCO (публичные) — в образ; наш детектор вещей m896c14 — закрытый, из хранилища (bootstrap.py)
RUN mkdir -p /root/.roboflow && cd /root && python -c "import rfdetr; rfdetr.RFDETRMedium()" && ls -la /root/.roboflow/models /root/*.pth 2>/dev/null | head
# модель и turbo-адаптер — публичные, кладём в образ (Salad хранит слои у себя: новая машина стартует за минуты, а не качает 11 ГБ
# с Hugging Face через домашний интернет). Версия модели закреплена. Наш ghost-адаптер — закрытый, НЕ в образе (bootstrap.py).
RUN python -c "from huggingface_hub import snapshot_download, hf_hub_download; \
snapshot_download('SamuelTallet/Qwen-Image-2.1-SDNQ-4bit-dynamic-hadamard256', revision='20cb2507528dc423bf32d51b60490c5ec3bd5eea', local_dir='/models/qwen-image-21-sdnq4', max_workers=16); \
hf_hub_download('Viggle/Qwen-Image-2.1-viggle-turbo', 'Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors', local_dir='/models/lora')" \
 && rm -rf /models/qwen-image-21-sdnq4/.cache /models/lora/.cache
COPY . /app/ghostgen
WORKDIR /app
ENV GG_MODEL_DIR=/models/qwen-image-21-sdnq4 GG_LORA_DIR=/models/lora GG_WORK=/root/ghostgen_work GG_ENGINE=qwen
CMD ["python", "-m", "ghostgen.node"]
