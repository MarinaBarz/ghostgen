"""Движки генерации с одним интерфейсом:
  eng = make_engine(cfg); eng.generate(images: [PIL], prompt, seed) -> PIL 512x512; eng.bench() -> секунд на шаг
QwenEngine — боевой (перенесён из experiments/2026-09-26_turbo/code/fast.py): Qwen-Image-2.1 SDNQ-4bit, turbo LoRA 1,0 +
ghost LoRA 0,5 хуками, 6 шагов, INT8-перемножение; на картах 24 ГБ всё держится на видеокарте (без выгрузки энкодера).
FakeEngine — для проверки всей системы без видеокарты: картинка детерминированно выводится из первого входа."""
import os, time
from PIL import Image
from .config import RECIPE


class FakeEngine:
    def __init__(self, cfg): self.sec = cfg.fake_sec

    def generate(self, images, prompt, seed):
        time.sleep(self.sec)
        im = images[0].convert('RGB').resize((512, 512))
        from PIL import ImageDraw
        ImageDraw.Draw(im).line([(0, 0), (511, 511)], fill=(0, 0, 0), width=3)   # не однотонная: проверка «пустой картинки» её пропускает
        return im

    def bench(self): return 0.01


class QwenEngine:
    def __init__(self, cfg):
        import torch, sdnq  # noqa: F401
        from diffusers import QwenImage21Pipeline, FlowMatchEulerDiscreteScheduler
        from safetensors.torch import load_file
        self.torch = torch
        pipe = QwenImage21Pipeline.from_pretrained(cfg.model_dir, torch_dtype=torch.bfloat16)
        pipe.scheduler = FlowMatchEulerDiscreteScheduler.from_config(pipe.scheduler.config, shift_terminal=None)
        if RECIPE['int8_matmul']:
            from sdnq.loader import apply_sdnq_options_to_model
            pipe.transformer = apply_sdnq_options_to_model(pipe.transformer, use_quantized_matmul=True)
        vram = self.vram_gb = torch.cuda.get_device_properties(0).total_memory / 2**30
        if vram >= 22:
            pipe.to('cuda')                                    # всё на карте: энкодер 6,3 + трансформер 3,8 + VAE
        else:                                                  # карты 16 ГБ: энкодер ездит на карту на время разбора промпта
            from accelerate import cpu_offload_with_hook
            pipe.to('cuda'); pipe.text_encoder.to('cpu'); torch.cuda.empty_cache()
            pipe.text_encoder, te_hook = cpu_offload_with_hook(pipe.text_encoder, 'cuda')
            _enc = pipe.encode_prompt

            def enc(*a, **k):                                  # без выгрузки энкодер остаётся на карте: память переполнена, 37 с/товар
                r = _enc(*a, **k); te_hook.offload(); torch.cuda.empty_cache(); return r
            pipe.encode_prompt = enc
        mods = dict(pipe.transformer.named_modules()); deltas = {}

        def hook(m, i, o):
            x = i[0]
            for A, B, w in deltas[m]: o.add_((x.to(A.dtype) @ A.T) @ B.T, alpha=w)
            return o

        def add(name, A, B, w):
            m = mods[name]
            if m not in deltas: deltas[m] = []; m.register_forward_hook(hook)
            deltas[m].append((A, B, w))

        def attach(path, prefix, w):
            sd = load_file(path, device='cuda')
            for k in [k for k in sd if k.endswith('.lora_A.weight')]:
                name = k[len(prefix):-len('.lora_A.weight')]; A = sd[k].bfloat16(); B = sd[k.replace('lora_A', 'lora_B')].bfloat16()
                if name.endswith('img_mlp.gate_up'):             # в формате ComfyUI gate_up = [gate_layer; proj]
                    h = B.shape[0] // 2; base = name[:-len('gate_up')]
                    add(base + 'gate_layer', A, B[:h], w); add(base + 'proj', A, B[h:], w)
                else:
                    add(name, A, B, w)
        attach(os.path.join(cfg.lora_dir, RECIPE['turbo_lora']), 'transformer.', RECIPE['turbo_weight'])
        attach(os.path.join(cfg.lora_dir, RECIPE['ghost_lora']), 'diffusion_model.', RECIPE['ghost_weight'])
        self.pipe = pipe

    def _call(self, images, prompt, seed, cb=None):
        r = RECIPE
        return self.pipe(prompt=prompt, image=images, width=r['resolution'], height=r['resolution'], output_resolution=r['output_resolution'],
                         num_inference_steps=r['steps'], sigmas=r['sigmas'], true_cfg_scale=r['true_cfg_scale'],
                         generator=self.torch.Generator('cuda').manual_seed(int(seed)), callback_on_step_end=cb).images[0]

    def generate(self, images, prompt, seed): return self._call(images, prompt, seed)

    def bench(self):
        """Скорость шага на эталонном входе (серое пятно 768x1024): 3 прогона, берём последний (первые — прогрев)."""
        im = Image.new('RGB', (768, 1024), (200, 200, 200)); step = None
        for _ in range(3):
            st = []
            def cb(p, i, t, kw):
                self.torch.cuda.synchronize(); st.append(time.time()); return kw
            self._call([im], 'A grey rectangle on a solid #fafaf7 background.', 1, cb)
            step = (st[-1] - st[0]) / max(1, len(st) - 1)
        return step


def make_engine(cfg): return FakeEngine(cfg) if cfg.engine == 'fake' else QwenEngine(cfg)
