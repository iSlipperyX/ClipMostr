"""The local language models ClipMonster can download, which one suits this PC, and the download itself.

Four tiers, from a 3B model that's quick on any PC to 32B models for a 24 GB graphics card (or a 16 GB
card and 32 GB of RAM, splitting the layers between them, which is slower):

    Compact         Llama 3.2 3B Instruct       2.0 GB   4-8 GB graphics card, or the CPU
    Standard        Qwen2.5 7B Instruct         4.7 GB   8-12 GB
    High-capacity   Mistral Nemo 12B Instruct   7.5 GB   12-16 GB
    Ultra           Qwen2.5 32B Instruct       19.9 GB   24 GB, or 16 GB + 32 GB RAM
                    Command R 32B (08-2024)    19.8 GB   the same; its licence is non-commercial

"Pick for this PC" takes the biggest one that runs entirely on the graphics card (planner.py works that
out from each model's shape), so automation stays quick; a bigger one can always be picked by hand.
Command R is never picked automatically: Cohere licenses it for non-commercial use only, and clips made
for TikTok's Creator Rewards earn money.

Models are instruction-tuned GGUF files quantised to Q4_K_M (about 4.8 bits a weight: a quarter of the
full size, and hardly any worse at following instructions). They're saved once to
%LOCALAPPDATA%\\ClipMonster\\models\\llm, downloaded from Hugging Face on first use. A download that
stops part-way carries on from where it got to next time.
"""

from __future__ import annotations

import http.client
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..settings import models_dir
from . import gguf, planner
from .hardware import GB as GIB, Hardware

HF = "https://huggingface.co/{repo}/resolve/main/{file}"
GGUF_MAGIC = b"GGUF"
Q4_K = 144 / 256  # bytes per weight
Q6_K = 210 / 256
TYPICAL_DESKTOP = int(0.6 * GIB)  # graphics memory Windows and open programs usually hold


@dataclass(frozen=True)
class Arch:
    """A model's shape, for sizing it before its file is downloaded (the file's own header is used after)."""

    arch: str
    n_layer: int
    n_embd: int
    n_ff: int
    n_head: int
    n_head_kv: int
    head_dim: int
    n_vocab: int
    n_ctx_train: int
    tied: bool = False  # the token embeddings double as the output layer


@dataclass(frozen=True)
class Model:
    key: str
    name: str  # "Qwen2.5 7B Instruct"
    tier: str  # compact | standard | high | ultra
    repo: str
    file: str
    size_gb: float  # download size (GB of 10^9 bytes, as Hugging Face shows it)
    context: int  # tokens of context ClipMonster asks for when there's memory for it
    arch: Arch
    licence: str
    commercial: bool = True  # its licence allows earning money from what it makes
    auto: bool = True  # "Pick for this PC" may choose it
    legacy: bool = False  # 3.1's model: kept working for whoever downloaded it, not offered first

    @property
    def url(self) -> str:
        return HF.format(repo=self.repo, file=self.file)

    @property
    def short(self) -> str:
        """'Qwen2.5 7B'."""
        return self.name.replace(" Instruct", "").replace(" (08-2024)", "")

    @property
    def label(self) -> str:
        """For Settings: 'Qwen2.5 7B · Standard · 4.7 GB · 8 GB+ graphics card'."""
        if self.legacy:
            return f"{self.short} · {self.size_gb:.1f} GB · ClipMonster 3.1's model"
        tier, fits = TIERS[self.tier]
        return f"{self.short} · {tier} · {self.size_gb:.1f} GB · " + (fits if self.commercial else
                                                                      "non-commercial licence")


TIERS = {
    "compact": ("Compact", "any PC, quick on the CPU too"),
    "standard": ("Standard", "8 GB+ graphics card"),
    "high": ("High-capacity", "12 GB+ graphics card"),
    "ultra": ("Ultra", "24 GB graphics card, or 16 GB + 32 GB RAM"),
}

MODELS: dict[str, Model] = {m.key: m for m in (
    Model("llama-3.2-3b", "Llama 3.2 3B Instruct", "compact", "bartowski/Llama-3.2-3B-Instruct-GGUF",
          "Llama-3.2-3B-Instruct-Q4_K_M.gguf", 2.02, 16384,
          Arch("llama", 28, 3072, 8192, 24, 8, 128, 128256, 131072, tied=True), "Llama 3.2 Community License"),
    Model("qwen2.5-7b", "Qwen2.5 7B Instruct", "standard", "bartowski/Qwen2.5-7B-Instruct-GGUF",
          "Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4.68, 32768,
          Arch("qwen2", 28, 3584, 18944, 28, 4, 128, 152064, 32768), "Apache 2.0"),
    Model("mistral-nemo-12b", "Mistral Nemo 12B Instruct", "high", "bartowski/Mistral-Nemo-Instruct-2407-GGUF",
          "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf", 7.48, 32768,
          Arch("llama", 40, 5120, 14336, 32, 8, 128, 131072, 1024000), "Apache 2.0"),
    Model("qwen2.5-32b", "Qwen2.5 32B Instruct", "ultra", "bartowski/Qwen2.5-32B-Instruct-GGUF",
          "Qwen2.5-32B-Instruct-Q4_K_M.gguf", 19.85, 32768,
          Arch("qwen2", 64, 5120, 27648, 40, 8, 128, 152064, 32768), "Apache 2.0"),
    Model("command-r-32b", "Command R 32B (08-2024)", "ultra", "bartowski/c4ai-command-r-08-2024-GGUF",
          "c4ai-command-r-08-2024-Q4_K_M.gguf", 19.8, 32768,
          Arch("command-r", 40, 8192, 24576, 64, 8, 128, 256000, 131072, tied=True),
          "CC BY-NC 4.0 (non-commercial only)", commercial=False, auto=False),
    Model("mistral-7b", "Mistral 7B Instruct v0.3", "standard", "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
          "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf", 4.37, 16384,
          Arch("llama", 32, 4096, 14336, 32, 8, 128, 32768, 32768), "Apache 2.0", auto=False, legacy=True),
)}
COMPACT = "llama-3.2-3b"
CHOICES = (("auto", "Pick for this PC"),) + tuple((m.key, m.label) for m in MODELS.values()) + (
    ("custom", "My own GGUF file"),)


def folder() -> Path:
    path = models_dir() / "llm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_shape(model: Model) -> gguf.Shape:
    """The model's shape from its catalogue entry: Q4_K_M keeps the token embeddings at 4.5 bits a weight
    and the output layer at 6.6, and the layers share the rest of the file."""
    a = model.arch
    size = int(model.size_gb * 1e9)
    vocab = a.n_vocab * a.n_embd
    if a.tied:
        embed = int(vocab * Q6_K)
        output = embed  # llama.cpp copies the shared tensor onto the card for the output layer
        layers = size - embed
    else:
        embed, output = int(vocab * Q4_K), int(vocab * Q6_K)
        layers = size - embed - output
    per = max(1, layers // a.n_layer)
    return gguf.Shape(arch=a.arch, name=model.name, n_layer=a.n_layer, n_embd=a.n_embd, n_ff=a.n_ff,
                      n_head=a.n_head, n_vocab=a.n_vocab, n_ctx_train=a.n_ctx_train, head_dim_k=a.head_dim,
                      head_dim_v=a.head_dim, kv_heads=[a.n_head_kv] * a.n_layer, layer_bytes=[per] * a.n_layer,
                      output_bytes=output, embed_bytes=embed, file_bytes=size, file_type=15, tied_output=a.tied)


def shape_of(model: Model | None, path: Path) -> gguf.Shape:
    """The exact shape from the file's header once it's here, else the catalogue's estimate. Raises
    gguf.GGUFError for a custom file that can't be read."""
    if path.is_file() and (model is None or installed(path)):
        try:
            return gguf.read(path)
        except gguf.GGUFError:
            if model is None:
                raise
    if model is None:
        raise gguf.GGUFError(f"{path.name or 'The model file'} wasn't found.")
    return estimate_shape(model)


def runs_on_card(model: Model, hw: Hardware, ram_free_gb: float | None = None) -> planner.Plan:
    """How the model would run on this PC on a typical day (the card's memory less what Windows and open
    programs usually hold), for choosing a model: the real run is sized to what's free at the time."""
    total = int(hw.vram_gb * GIB) if hw.has_gpu else 0
    ram = int((ram_free_gb if ram_free_gb is not None else hw.ram_gb * 0.6) * GIB)
    return planner.plan(estimate_shape(model), vram_total=total, vram_free=max(0, total - TYPICAL_DESKTOP),
                        ram_free=ram, want_context=model.context, gpu=hw.has_gpu)


def choose(hw: Hardware) -> Model:
    """The model that suits this PC: the biggest that runs entirely on its graphics card, else the 3B,
    which also stays quick on a CPU (a 7B on the CPU alone takes minutes per video)."""
    if hw.has_gpu:
        for model in sorted((m for m in MODELS.values() if m.auto), key=lambda m: -m.size_gb):
            p = runs_on_card(model, hw)
            if p.fits and p.full_gpu:
                return model
    return MODELS[COMPACT]


def resolve(s, hw: Hardware) -> tuple[Model | None, Path]:
    """(the catalogue model, its file) for Settings.local_model. A custom file has no catalogue entry.

    "Pick for this PC" uses the best model for it, except that while that one isn't downloaded yet and
    one that was (ClipMonster 3.1's pick, say) is, it keeps using the downloaded one: a better model is
    never a surprise download of several GB in the middle of a run. `ytclip local-ai download` (or
    choosing it in Settings) fetches the better one."""
    key = str(getattr(s, "local_model", "auto") or "auto")
    if key == "custom":
        return None, Path(str(getattr(s, "local_model_path", "") or "")).expanduser()
    if key in MODELS:
        return MODELS[key], folder() / MODELS[key].file
    best = choose(hw)
    path = folder() / best.file
    if installed(path):
        return best, path
    have = [m for m in MODELS.values() if (m.auto or m.legacy) and m.size_gb <= best.size_gb
            and installed(folder() / m.file)]
    if have:
        stand_in = max(have, key=lambda m: m.size_gb)
        return stand_in, folder() / stand_in.file
    return best, path


def better_choice(s, hw: Hardware) -> Model | None:
    """With "Pick for this PC" on a downloaded stand-in: the model that suits this PC better."""
    if str(getattr(s, "local_model", "auto") or "auto") != "auto":
        return None
    best = choose(hw)
    model, _ = resolve(s, hw)
    return best if model is not None and model.key != best.key else None


def is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == GGUF_MAGIC
    except OSError:
        return False


def installed(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 50_000_000 and is_gguf(path)


class DownloadError(RuntimeError):
    pass


Progress = Callable[[str, "float | None"], None]


def download(model: Model, progress: Progress, opener=urllib.request.urlopen, dest: Path | None = None) -> Path:
    """The model's file, downloaded first when it isn't here yet. Resumes a stopped download; the file
    only takes its real name once it's whole and starts like a GGUF file, so a half file is never used."""
    path = dest or folder() / model.file
    if installed(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    have = part.stat().st_size if part.is_file() else 0
    label = f"Downloading the local AI model, {model.name} (once)"
    progress(label, None)
    req = urllib.request.Request(model.url, headers={"User-Agent": "ClipMonster"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        r = opener(req, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # asked to start past the end: the part file is already whole
            part.replace(path)
            return _check(path)
        raise DownloadError(f"Hugging Face answered {exc.code} for {model.file}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"Couldn't reach Hugging Face to download {model.file}: {exc}") from exc
    with r:
        status = getattr(r, "status", 200)
        if have and status != 206:  # the server ignored the range: start again
            have = 0
        length = int(r.headers.get("Content-Length") or 0)
        total = have + length if length else 0
        done = have
        try:
            with open(part, "ab" if have else "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    progress(label, done / total if total else None)
        except (OSError, http.client.HTTPException) as exc:  # the connection dropped, or the disk filled up
            raise DownloadError(f"The model download stopped at {done / 1e9:.1f} GB ({exc}). It carries on from "
                                "there next time.") from exc
    if total and part.stat().st_size != total:
        raise DownloadError("The model download was cut short. It carries on from there next time.")
    part.replace(path)
    return _check(path)


def _check(path: Path) -> Path:
    if not is_gguf(path):
        path.unlink(missing_ok=True)
        raise DownloadError(f"{path.name} isn't a GGUF model file. It was deleted; it downloads again next time.")
    return path


def free_space_ok(model: Model, path: Path) -> bool:
    import shutil

    try:
        return shutil.disk_usage(path.parent if path.parent.exists() else Path(os.sep)).free > model.size_gb * 1.1e9
    except OSError:
        return True
