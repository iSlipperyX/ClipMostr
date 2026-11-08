"""The local language models ClipMonster can download, which one suits this PC, and the download itself.

Models are instruction-tuned GGUF files quantised to Q4_K_M (about 4.8 bits a weight: a quarter of the
full size, and hardly any worse at following instructions). They're saved once to
%LOCALAPPDATA%\\ClipMonster\\models\\llm, downloaded from Hugging Face on first use. A download that
stops part-way carries on from where it got to next time.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..settings import models_dir
from .hardware import Hardware

HF = "https://huggingface.co/{repo}/resolve/main/{file}"
GGUF_MAGIC = b"GGUF"


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    repo: str
    file: str
    size_gb: float  # download size, for the settings and for checking there's room
    context: int  # tokens of context ClipMonster asks for (the model may support more)
    min_vram_gb: float  # graphics memory it wants to run fully on the card (model + context)
    min_ram_gb: float  # memory it wants on the CPU

    @property
    def url(self) -> str:
        return HF.format(repo=self.repo, file=self.file)


MODELS: dict[str, Model] = {
    "llama-3.2-3b": Model("llama-3.2-3b", "Llama 3.2 3B Instruct (2 GB, quick, any PC)",
                          "bartowski/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
                          2.0, 16384, 4.0, 6.0),
    "mistral-7b": Model("mistral-7b", "Mistral 7B Instruct v0.3 (4.4 GB, better, 8 GB+ graphics)",
                        "bartowski/Mistral-7B-Instruct-v0.3-GGUF", "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
                        4.4, 16384, 7.5, 12.0),
}
CHOICES = (("auto", "Pick for this PC"),) + tuple((m.key, m.label) for m in MODELS.values()) + (
    ("custom", "My own GGUF file"),)


def folder() -> Path:
    path = models_dir() / "llm"
    path.mkdir(parents=True, exist_ok=True)
    return path


def choose(hw: Hardware) -> Model:
    """The model that suits this PC: the 7B when the graphics card can hold it, else the 3B, which also
    stays quick on a CPU (a 7B on the CPU alone takes minutes per video)."""
    big = MODELS["mistral-7b"]
    if hw.has_gpu and hw.vram_gb >= big.min_vram_gb:
        return big
    return MODELS["llama-3.2-3b"]


def resolve(s, hw: Hardware) -> tuple[Model | None, Path]:
    """(the catalogue model, its file) for Settings.local_model. A custom file has no catalogue entry."""
    key = str(getattr(s, "local_model", "auto") or "auto")
    if key == "custom":
        return None, Path(str(getattr(s, "local_model_path", "") or "")).expanduser()
    model = MODELS.get(key) or choose(hw)
    return model, folder() / model.file


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
    label = f"Downloading the local AI model, {model.label.split(' (')[0]} (once)"
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
        with open(part, "ab" if have else "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(label, done / total if total else None)
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
