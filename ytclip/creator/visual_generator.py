"""Each scene's picture, made on this PC.

Backends (Settings → Creator → Pictures; "auto" takes the first that's ready, scene by scene):

* ComfyUI: a ComfyUI already running on this PC (http://127.0.0.1:8188 by default) is sent a text-to-image
  graph for each scene and its picture fetched back. ComfyUI runs on NVIDIA, AMD and Intel cards and
  manages its own models, so it's the way to Stable Diffusion in the installed app. Your own exported
  workflow (API format) can be used instead of the built-in one: {{prompt}}, {{negative}}, {{seed}},
  {{width}} and {{height}} in it are filled in.
* diffusers: SDXL-Turbo (4 steps, 576x1024) or FLUX.1-schnell (4 steps, 768x1344) on an NVIDIA card (CUDA),
  an AMD or Intel card (DirectML) or an Apple GPU. In the installed app that's the picture engine
  (picture_engine.py: Creator tab -> Set up AI pictures), a Python of its own with PyTorch for this PC's card;
  from a source install with torch and diffusers (pip install "ytclip[diffusion]") it runs in this Python.
  The model downloads once from Hugging Face.
* Your B-roll: clips and pictures in your B-roll folder (Settings → Editing) whose file or folder names
  match the scene's keywords ("money/counting cash.mp4"). Only footage you have the right to use goes there.
* Textures: abstract moving backgrounds drawn here in the style's colours: always works, instantly, on any
  PC, and the fallback for any scene the others couldn't do.

A scene that fails in one backend falls back to the next, so one bad picture never stops a video.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from ..settings import models_dir
from . import picture_engine
from ..vision.broll_selector import VIDEO_EXTS, _stem, tags_for

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

BACKENDS = ("auto", "comfyui", "sdxl_turbo", "flux_schnell", "local_broll", "texture")
BACKEND_NAMES = {"comfyui": "ComfyUI", "sdxl_turbo": "SDXL-Turbo", "flux_schnell": "FLUX.1-schnell",
                 "local_broll": "your B-roll", "texture": "built-in textures"}
DIFFUSERS_MODELS = {
    # key: (Hugging Face repo, width, height, steps, guidance)
    "sdxl_turbo": ("stabilityai/sdxl-turbo", 576, 1024, 4, 0.0),
    "flux_schnell": ("black-forest-labs/FLUX.1-schnell", 768, 1344, 4, 0.0),
}
COMFY_URL = "http://127.0.0.1:8188"
COMFY_TIMEOUT = 300.0  # seconds one picture may take


@dataclass
class SceneVisual:
    scene_id: int
    kind: str  # image | video
    path: Path
    source: str  # BACKENDS key that made it
    prompt: str = ""
    note: str = ""


class VisualError(RuntimeError):
    pass


def seed_for(*parts) -> int:
    return int(hashlib.sha1(":".join(str(p) for p in parts).encode()).hexdigest()[:8], 16) % (2 ** 31)


# ---------------------------------------------------------------- built-in textures

def _hex(c: str) -> np.ndarray:
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], float) / 255.0


def texture(path: Path, palette: tuple[str, str, str], seed: int, size: tuple[int, int] = (1350, 2400),
            words: str = "") -> Path:
    """An abstract background (dark base, soft glowing shapes in the palette, light streaks, film grain,
    a vignette), different for every seed, saved as PNG."""
    import cv2

    w, h = size
    rng = np.random.default_rng(seed)
    dark, mid, light = (_hex(c) for c in palette)
    small_w, small_h = w // 4, h // 4
    yy, xx = np.mgrid[0:small_h, 0:small_w].astype(float)
    xx /= small_w
    yy /= small_h
    # Base: a diagonal gradient from the dark colour towards the mid one.
    angle = rng.uniform(0, 2 * math.pi)
    g = (np.cos(angle) * (xx - 0.5) + np.sin(angle) * (yy - 0.5)) + 0.5
    img = dark[None, None, :] * (1 - g[..., None] * 0.75) + mid[None, None, :] * (g[..., None] * 0.75)
    # Glows: soft blobs of the mid and light colours.
    for _ in range(rng.integers(4, 8)):
        cx, cy = rng.uniform(0.05, 0.95), rng.uniform(0.05, 0.95)
        r = rng.uniform(0.12, 0.42)
        colour = light if rng.random() < 0.35 else mid
        strength = rng.uniform(0.35, 0.8)
        d = ((xx - cx) ** 2 + ((yy - cy) * (small_h / small_w)) ** 2) / r ** 2
        img += colour[None, None, :] * (np.exp(-d * 2.2) * strength)[..., None]
    # Light streaks across.
    for _ in range(rng.integers(1, 4)):
        a = rng.uniform(-0.6, 0.6) + math.pi / 2
        off = rng.uniform(-0.4, 0.4)
        dist = np.abs(np.cos(a) * (xx - 0.5) + np.sin(a) * (yy - 0.5) - off)
        img += light[None, None, :] * (np.exp(-(dist / rng.uniform(0.01, 0.05)) ** 2) * rng.uniform(0.08, 0.2))[..., None]
    img = cv2.resize(np.clip(img, 0, 1).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    img = cv2.GaussianBlur(img, (0, 0), sigmaX=w / 180)
    # Bokeh: soft out-of-focus discs of light.
    layer = np.zeros_like(img)
    for _ in range(rng.integers(10, 22)):
        x, y = int(rng.uniform(0, w)), int(rng.uniform(0, h))
        r = int(rng.uniform(18, 70) * w / 1080)
        colour = light if rng.random() < 0.6 else mid * 1.6
        cv2.circle(layer, (x, y), r, tuple(float(v) for v in np.clip(colour, 0, 1) * rng.uniform(0.12, 0.35)), -1,
                   cv2.LINE_AA)
    img += cv2.GaussianBlur(layer, (0, 0), sigmaX=w / 400)
    # Fine drifting particles.
    for _ in range(rng.integers(60, 140)):
        x, y = int(rng.uniform(0, w)), int(rng.uniform(0, h))
        r = rng.uniform(1, 4) * w / 1080
        cv2.circle(img, (x, y), int(max(1, r)), tuple(float(v) for v in light * rng.uniform(0.15, 0.6)), -1, cv2.LINE_AA)
    img = cv2.GaussianBlur(img, (0, 0), sigmaX=1.2)
    # Vignette and grain.
    vy, vx = np.mgrid[0:h, 0:w].astype(np.float32)
    v = 1 - 0.4 * (((vx / w - 0.5) * 1.6) ** 2 + ((vy / h - 0.5) * 1.1) ** 2)
    img *= np.clip(v, 0.25, 1)[..., None]
    img += rng.normal(0, 0.018, img.shape).astype(np.float32)
    out = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
    return path


# ---------------------------------------------------------------- your B-roll

def broll_library(folder: str | Path) -> list[tuple[Path, tuple[str, ...]]]:
    root = Path(str(folder or "")).expanduser()
    if not str(folder or "").strip() or not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS)
    return [(p, tags_for(p, root)) for p in files]


def match_broll(words: list[str], lib: list[tuple[Path, tuple[str, ...]]], used: dict[str, int],
                need_match: bool = True) -> tuple[Path, str] | None:
    """The B-roll whose tags share the most of `words` (the least used on a tie). None without a match,
    unless `need_match` is off (then the least used of all)."""
    want = [_stem(w) for w in words if w]
    best, best_score, matched = None, 0.0, ""
    for path, tags in lib:
        hits = [w for w in want if w and w in tags]
        score = len(set(hits)) - 0.3 * used.get(str(path), 0)
        if hits and score > best_score:
            best, best_score, matched = path, score, hits[0]
    if best is None and not need_match and lib:
        best = min(lib, key=lambda x: used.get(str(x[0]), 0))[0]
    if best is None:
        return None
    used[str(best)] = used.get(str(best), 0) + 1
    return best, matched


# ---------------------------------------------------------------- ComfyUI

def comfy_url(s) -> str:
    return (str(getattr(s, "creator_comfy_url", "") or COMFY_URL)).rstrip("/")


def _get(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def comfy_running(s, opener: Callable[[str, float], bytes] = _get) -> str:
    """"" when a ComfyUI answers at the address, else why not."""
    try:
        json.loads(opener(comfy_url(s) + "/system_stats", 2.0))
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"ComfyUI isn't running at {comfy_url(s)} ({type(exc).__name__})"


def comfy_checkpoints(s, opener: Callable[[str, float], bytes] = _get) -> list[str]:
    try:
        info = json.loads(opener(comfy_url(s) + "/object_info/CheckpointLoaderSimple", 5.0))
        return list(info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0])
    except Exception:  # noqa: BLE001
        return []


def sampler_for(ckpt: str) -> tuple[int, float, str, str]:
    """(steps, cfg, sampler, scheduler) that suit a checkpoint, from its name."""
    low = ckpt.lower()
    if "schnell" in low or "flux" in low:
        return 4, 1.0, "euler", "simple"
    if "turbo" in low:
        return 4, 1.0, "euler_ancestral", "normal"
    if "lightning" in low or "hyper" in low or "lcm" in low:
        return 6, 1.5, "euler", "sgm_uniform"
    return 26, 6.0, "dpmpp_2m", "karras"


def comfy_graph(prompt: str, negative: str, seed: int, width: int, height: int, ckpt: str) -> dict:
    """The built-in text-to-image graph (ComfyUI's API format)."""
    steps, cfg, sampler, scheduler = sampler_for(ckpt)
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                                                   "scheduler": scheduler, "denoise": 1.0, "model": ["4", 0],
                                                   "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ClipMonster", "images": ["8", 0]}},
    }


def fill_workflow(text: str, prompt: str, negative: str, seed: int, width: int, height: int) -> dict:
    """Your workflow with its placeholders filled in (JSON-escaped)."""
    def esc(v: str) -> str:
        return json.dumps(v)[1:-1]

    for key, value in (("{{prompt}}", esc(prompt)), ("{{negative}}", esc(negative))):
        text = text.replace(key, value)
    for key, value in (("{{seed}}", seed), ("{{width}}", width), ("{{height}}", height)):
        text = text.replace(f'"{key}"', str(value)).replace(key, str(value))
    return json.loads(text)


class Comfy:
    def __init__(self, s, log: Log, check: Callable[[], None] | None = None, opener=None, poster=None):
        self.s, self.log, self.check = s, log, check
        self.url = comfy_url(s)
        self._get = opener or _get
        self._post = poster or self._post_json
        self.client = uuid.uuid4().hex
        self.ckpt = ""

    def _post_json(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def missing(self) -> str:
        return comfy_running(self.s, self._get)

    def checkpoint(self) -> str:
        if self.ckpt:
            return self.ckpt
        want = str(getattr(self.s, "creator_comfy_checkpoint", "") or "").strip()
        have = comfy_checkpoints(self.s, self._get)
        if want:
            self.ckpt = want
        elif have:
            # A fast one first: turbo/schnell/lightning, then SDXL, then anything.
            rank = sorted(have, key=lambda n: (not any(w in n.lower() for w in ("turbo", "schnell", "lightning")),
                                               "xl" not in n.lower(), n))
            self.ckpt = rank[0]
        else:
            raise VisualError("ComfyUI has no checkpoints. Put a model (for example sd_xl_turbo_1.0_fp16.safetensors) "
                              "in ComfyUI's models/checkpoints folder.")
        return self.ckpt

    def generate(self, prompt: str, negative: str, seed: int, dest: Path, width: int = 768, height: int = 1344) -> Path:
        own = str(getattr(self.s, "creator_comfy_workflow", "") or "").strip()
        if own:
            graph = fill_workflow(Path(own).expanduser().read_text(encoding="utf-8"), prompt, negative, seed, width, height)
        else:
            graph = comfy_graph(prompt, negative, seed, width, height, self.checkpoint())
        answer = self._post(self.url + "/prompt", {"prompt": graph, "client_id": self.client})
        if answer.get("error") or answer.get("node_errors"):
            raise VisualError(f"ComfyUI refused the graph: {answer.get('error') or answer.get('node_errors')}")
        pid = answer["prompt_id"]
        end = time.monotonic() + COMFY_TIMEOUT
        while time.monotonic() < end:
            if self.check:
                self.check()
            history = json.loads(self._get(f"{self.url}/history/{pid}", 10.0) or b"{}")
            entry = history.get(pid)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise VisualError("ComfyUI failed on this picture: " + json.dumps(status.get("messages", ""))[:300])
                for node in (entry.get("outputs") or {}).values():
                    for img in node.get("images") or []:
                        q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                                    "type": img.get("type", "output")})
                        data = self._get(f"{self.url}/view?{q}", 30.0)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                        return dest
            time.sleep(0.5)
        raise VisualError(f"ComfyUI took longer than {COMFY_TIMEOUT:.0f} s on one picture.")


# ---------------------------------------------------------------- diffusers

def torch_device() -> tuple[str, str]:
    """(device, why not) for diffusers: cuda, mps or DirectML; ("", reason) when there's none."""
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        return "", f"PyTorch isn't installed ({type(exc).__name__})"
    try:
        import diffusers  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return "", f"diffusers isn't installed ({type(exc).__name__})"
    if torch.cuda.is_available():
        return "cuda", ""
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", ""
    try:
        import torch_directml  # noqa: F401

        return "dml", ""
    except Exception:  # noqa: BLE001
        pass
    return "", "no graphics card PyTorch can use (CUDA, Apple or DirectML)"


_pipe = None
_pipe_key: tuple | None = None


class Diffusers:
    def __init__(self, s, key: str, log: Log, progress: Progress, check=None):
        self.s, self.key, self.log, self.progress, self.check = s, key, log, progress, check

    def via_engine(self) -> bool:
        """True when the picture engine makes this model's pictures (no PyTorch in this Python)."""
        device, why = torch_device()
        if device or (getattr(self.s, "creator_cpu_pictures", False) and "graphics card" in why):
            return False
        return picture_engine.ready()

    def missing(self) -> str:
        device, why = torch_device()
        if device:
            return ""
        if getattr(self.s, "creator_cpu_pictures", False) and "graphics card" in why:
            return ""
        st = picture_engine.status()
        if not st:
            return "AI pictures aren't set up (Creator tab: Set up AI pictures)"
        engine = st.get("device", "")
        if self.key == "flux_schnell" and engine != "cuda":
            return "FLUX.1-schnell needs an NVIDIA card (SDXL-Turbo is used instead)"
        if engine == "cpu" and not getattr(self.s, "creator_cpu_pictures", False):
            return ("the picture engine found no graphics card it can use; turn on \"Let Stable Diffusion use "
                    "the CPU\" in Settings: Creator to draw on the processor (minutes a picture)")
        return ""

    def pipeline(self):
        global _pipe, _pipe_key
        import torch

        device, _ = torch_device()
        device = device or "cpu"
        repo = DIFFUSERS_MODELS[self.key][0]
        key = (repo, device)
        if _pipe is not None and _pipe_key == key:
            return _pipe
        unload()
        self.progress(f"Loading {BACKEND_NAMES[self.key]} (the first time, it downloads several GB)", None)
        dtype = torch.float16 if device in ("cuda", "dml") else (torch.bfloat16 if self.key == "flux_schnell" else torch.float32)
        cache = models_dir() / "diffusers"
        local = not getattr(self.s, "creator_downloads", True)
        if self.key == "flux_schnell":
            from diffusers import FluxPipeline

            pipe = FluxPipeline.from_pretrained(repo, torch_dtype=dtype, cache_dir=cache, local_files_only=local)
        else:
            from diffusers import AutoPipelineForText2Image

            pipe = AutoPipelineForText2Image.from_pretrained(repo, torch_dtype=dtype, cache_dir=cache,
                                                             local_files_only=local,
                                                             **({"variant": "fp16"} if dtype == torch.float16 else {}))
        if device == "dml":
            import torch_directml

            pipe = pipe.to(torch_directml.device())
        elif device == "cuda" and self.key == "flux_schnell" and torch.cuda.get_device_properties(0).total_memory < 22 * 2 ** 30:
            pipe.enable_model_cpu_offload()  # FLUX is 12B: on a smaller card the parts take turns on it
        else:
            pipe = pipe.to(device)
        _pipe, _pipe_key = pipe, key
        return pipe

    def generate(self, prompt: str, negative: str, seed: int, dest: Path) -> Path:
        repo, w, h, steps, guidance = DIFFUSERS_MODELS[self.key]
        if self.via_engine():
            engine = picture_engine.worker(self.log)
            if not engine.alive():
                self.progress(f"Loading {BACKEND_NAMES[self.key]} (the first time, it downloads about 7 GB)", None)
                self.log(f"Pictures: {BACKEND_NAMES[self.key]} in the picture engine "
                         f"({picture_engine.describe().removeprefix('set up: ')}).")
            cache = models_dir() / "diffusers"
            return engine.generate(repo, prompt, negative, seed, dest, width=w, height=h, steps=steps,
                                   guidance=guidance, cache=cache,
                                   local_only=not getattr(self.s, "creator_downloads", True), check=self.check,
                                   waiting=picture_engine.download_watch(cache, repo, "Making pictures",
                                                                         self.progress))
        import torch

        pipe = self.pipeline()
        gen = torch.Generator("cpu").manual_seed(seed)
        args = dict(prompt=prompt, num_inference_steps=steps, guidance_scale=guidance, width=w, height=h, generator=gen)
        if self.key == "flux_schnell":
            args["max_sequence_length"] = 256
        elif guidance > 0:
            args["negative_prompt"] = negative
        image = pipe(**args).images[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest)
        return dest


def unload() -> None:
    """Let go of the diffusion model and its graphics memory (in this Python, and the picture engine's)."""
    global _pipe, _pipe_key
    picture_engine.stop()
    if _pipe is None:
        return
    _pipe, _pipe_key = None, None
    try:
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- choosing and making

def available(s) -> dict[str, str]:
    """Each backend: "" when it's ready on this PC, else why not."""
    out = {"comfyui": comfy_running(s)}
    for key in DIFFUSERS_MODELS:
        out[key] = Diffusers(s, key, lambda m: None, lambda m, f: None).missing()
    out["local_broll"] = "" if broll_library(getattr(s, "broll_folder", "")) else \
        "no B-roll folder (Settings → Editing → B-roll folder)"
    out["texture"] = ""
    return out


def order_for(choice: str, ready: dict[str, str], prefer_model: str = "sdxl_turbo") -> list[str]:
    """Backends to try for a generated picture, best first."""
    if choice in ("comfyui", "sdxl_turbo", "flux_schnell"):
        rest = [k for k in ("comfyui", prefer_model, "flux_schnell", "sdxl_turbo") if k != choice and not ready.get(k)]
        return list(dict.fromkeys([choice, *rest, "local_broll", "texture"]))
    if choice == "local_broll":
        return ["local_broll", "texture"]
    if choice == "texture":
        return ["texture"]
    # auto
    picks = [k for k in ("comfyui", prefer_model, "flux_schnell" if prefer_model != "flux_schnell" else "sdxl_turbo")
             if not ready.get(k)]
    return list(dict.fromkeys([*picks, "local_broll", "texture"]))


def make_all(board, s, folder: Path, *, choice: str = "", style=None, log: Log | None = None,
             progress: Progress | None = None, check: Callable[[], None] | None = None,
             comfy: Comfy | None = None) -> list[SceneVisual]:
    """A picture (or clip) for every scene of `board`, kept in `folder` as scene_NN.png. `choice` is a
    BACKENDS key (default Settings → Creator → Pictures)."""
    from .director import get_style

    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    style = style or get_style(getattr(s, "creator_style", ""))
    choice = choice or getattr(s, "creator_visuals", "auto")
    ready = available(s) if choice not in ("texture",) else {"texture": ""}
    if comfy is not None:
        ready["comfyui"] = ""
    prefer = getattr(s, "creator_image_model", "sdxl_turbo")
    order = order_for(choice, ready, prefer if prefer in DIFFUSERS_MODELS else "sdxl_turbo")
    if any(k in order for k in ("comfyui", "sdxl_turbo", "flux_schnell")):
        try:  # the local AI's model leaves the graphics card to the picture model
            from ..intelligence import llama_engine

            llama_engine.free_graphics_memory()
        except Exception:  # noqa: BLE001
            pass
    lib = broll_library(getattr(s, "broll_folder", "")) if "local_broll" in order else []
    used: dict[str, int] = {}
    failed: set[str] = set()
    out: list[SceneVisual] = []
    makers: dict[str, object] = {}
    n = len(board.scenes)
    names = [BACKEND_NAMES[k] for k in order if k != "texture"]
    log("Pictures: " + (", then ".join(names + ["built-in textures"]) if names else "built-in textures") + ".")
    for i, sc in enumerate(board.scenes):
        if check:
            check()
        progress(f"Making pictures: scene {i + 1} of {n}", i / max(1, n))
        seed = seed_for(board.title, sc.scene_id, sc.visual_prompt)
        dest = folder / f"scene_{sc.scene_id:02d}.png"
        want = list(order)
        if sc.visual_type == "texture":
            want = ["texture"]
        elif sc.visual_type == "broll" and "local_broll" in want:
            want = ["local_broll"] + [k for k in want if k != "local_broll"]
        made = None
        for key in want:
            if key in failed:
                continue
            try:
                if key == "texture":
                    made = SceneVisual(sc.scene_id, "image", texture(dest, style.palette, seed, words=sc.speech_text),
                                       "texture", sc.visual_prompt)
                elif key == "local_broll":
                    hit = match_broll(list(sc.keywords) + sc.speech_text.split(), lib, used)
                    if hit is None:
                        continue
                    path, word = hit
                    made = SceneVisual(sc.scene_id, "video" if path.suffix.lower() in VIDEO_EXTS else "image", path,
                                       "local_broll", sc.visual_prompt, f"matched \"{word}\"")
                elif key == "comfyui":
                    maker = makers.get(key) or comfy or Comfy(s, log, check)
                    makers[key] = maker
                    made = SceneVisual(sc.scene_id, "image", maker.generate(sc.visual_prompt, style.negative, seed, dest),
                                       key, sc.visual_prompt)
                else:
                    maker = makers.get(key) or Diffusers(s, key, log, progress, check)
                    makers[key] = maker
                    made = SceneVisual(sc.scene_id, "image", maker.generate(sc.visual_prompt, style.negative, seed, dest),
                                       key, sc.visual_prompt)
                break
            except Exception as exc:  # noqa: BLE001 - one backend failing moves on to the next
                if type(exc).__name__ in ("Cancelled", "Stopped"):
                    raise
                failed.add(key)
                log(f"{BACKEND_NAMES[key]} couldn't make the picture for scene {sc.scene_id} "
                    f"({type(exc).__name__}: {str(exc)[:200]}); using the next option from here on.")
        if made is None:  # textures never fail, but just in case
            made = SceneVisual(sc.scene_id, "image", texture(dest, style.palette, seed), "texture", sc.visual_prompt)
        out.append(made)
    progress("Making pictures", 1.0)
    unload()
    return out
