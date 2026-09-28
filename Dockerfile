# Образ машины ghostgen (вариант А): окружение и код, БЕЗ модели и без закрытых файлов — образ публичный.
# Модель и turbo-адаптер качаются с Hugging Face при старте, наш ghost-адаптер — из хранилища со сверкой sha256 (bootstrap.py).
# Собирается GitHub Actions (.github/workflows/image.yml) в ghcr.io; локально: docker build -t ghostgen:v1 .
FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HUB_ENABLE_HF_TRANSFER=1
RUN apt-get update -q && apt-get install -y -q --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN (command -v uv || pip install -q --break-system-packages uv) && uv pip install --system --break-system-packages -q \
      "git+https://github.com/huggingface/diffusers@9f1246971270c84dcbe71233edb7a519596a5d02" \
      "transformers==5.17.0" "accelerate==1.15.0" "sdnq==0.2.6" triton safetensors "huggingface_hub[hf_transfer]" \
      redis boto3 pillow numpy
COPY . /app/ghostgen
WORKDIR /app
ENV GG_MODEL_DIR=/models/qwen-image-21-sdnq4 GG_LORA_DIR=/models/lora GG_WORK=/root/ghostgen_work GG_ENGINE=qwen
CMD ["python", "-m", "ghostgen.bootstrap"]
