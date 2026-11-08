"""Sizing the local AI to the PC: a model's shape from its GGUF header, free graphics memory and RAM from
each source, and the plan (how long a memory, how many layers on the card, the rest in RAM)."""

from __future__ import annotations

import struct
import sys

import pytest
from conftest import write_gguf

from ytclip.intelligence import gguf, hardware, llama_engine, models, planner
from ytclip.intelligence.hardware import Card, Hardware, Memory
from ytclip.settings import Settings

GIB = planner.GIB


# ---------------------------------------------------------------- the GGUF header


def test_the_header_gives_each_layers_size_and_its_memory_per_token(tmp_path):
    path = write_gguf(tmp_path / "m.gguf", n_layer=3, n_embd=64, n_head=4, kv_heads=[2, 0, 2], key_length=16,
                      ctx=32768, vocab=48, layer_bytes=[4096, 8192, 4096], output_bytes=2048, embed_bytes=8192,
                      name="Test 3L")
    shape = gguf.read(path)
    assert (shape.arch, shape.name, shape.n_layer, shape.n_embd, shape.n_ff, shape.n_head) == ("llama", "Test 3L",
                                                                                               3, 64, 128, 4)
    assert (shape.n_vocab, shape.n_ctx_train, shape.file_type) == (48, 32768, 15)  # vocab from the tokenizer's list
    assert shape.layer_bytes == [4096, 8192, 4096]  # sized from the offsets, whatever order the table lists them in
    assert (shape.output_bytes, shape.embed_bytes, shape.tied_output) == (2048 + 256, 8192, False)
    assert shape.kv_heads == [2, 0, 2] and (shape.head_dim_k, shape.head_dim_v) == (16, 16)
    assert shape.kv_bytes_per_token() == 2 * (16 + 16) * 2 * 2  # two layers with a cache, K and V at 16 bits
    assert shape.kv_bytes_per_token(1) == 2 * 32 * 2  # the last layer only
    assert shape.gpu_layer_bytes(2) == 8192 + 4096 and shape.weights_bytes == 16384 + 2304 + 8192
    assert shape.file_bytes == path.stat().st_size
    assert "llama · 3 layers" in gguf.describe(shape)


def test_a_tied_output_and_a_big_endian_file_read_the_same(tmp_path):
    tied = gguf.read(write_gguf(tmp_path / "tied.gguf", tied=True, embed_bytes=8192))
    assert tied.tied_output and tied.output_bytes == 256 + 8192  # the embeddings are copied to the card for it
    assert tied.weights_bytes == 2 * 4096 + 256 + 8192  # but they're in the file once
    big = gguf.read(write_gguf(tmp_path / "big.gguf", endian=">", kv_heads=2))
    little = gguf.read(write_gguf(tmp_path / "little.gguf", kv_heads=2))
    assert (big.layer_bytes, big.kv_heads, big.output_bytes) == (little.layer_bytes, little.kv_heads,
                                                                little.output_bytes)


def test_a_file_that_isnt_a_model_is_refused_plainly(tmp_path):
    def bad(data: bytes, match: str):
        path = tmp_path / "bad.gguf"
        path.write_bytes(data)
        with pytest.raises(gguf.GGUFError, match=match):
            gguf.read(path)

    bad(b"<html>sign in</html>", "isn't a GGUF")
    bad(b"GGUF" + struct.pack("<I", 1) + b"\0" * 32, "too old")
    bad(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 1 << 40) + struct.pack("<Q", 0), "impossible count")
    whole = write_gguf(tmp_path / "whole.gguf").read_bytes()
    bad(whole[:60], "ends inside its header")  # an unfinished download
    bad(whole.replace(b"general.architecture", b"general.architectur_"), "which model architecture")
    with pytest.raises(gguf.GGUFError, match="Can't open"):
        gguf.read(tmp_path / "missing.gguf")


def test_a_changed_file_is_read_again(tmp_path):
    path = write_gguf(tmp_path / "m.gguf", n_layer=2)
    assert gguf.read(path).n_layer == 2
    write_gguf(path, n_layer=5, layer_bytes=1024)
    assert gguf.read(path).n_layer == 5


def test_the_catalogue_shapes_match_each_models_published_numbers():
    """KV cache per token at 16 bits: 2 (K and V) × layers × KV heads × head size × 2 bytes. These are the
    models' own figures (their config.json files), so a typo in the catalogue shows up here."""
    per_token = {m.key: models.estimate_shape(m).kv_bytes_per_token() for m in models.MODELS.values()}
    assert per_token == {"llama-3.2-3b": 114_688, "qwen2.5-7b": 57_344, "mistral-nemo-12b": 163_840,
                         "qwen2.5-32b": 262_144, "command-r-32b": 163_840, "mistral-7b": 131_072}
    for m in models.MODELS.values():
        shape = models.estimate_shape(m)
        assert shape.weights_bytes == pytest.approx(m.size_gb * 1e9, rel=0.01), m.key
        assert shape.tied_output == m.arch.tied and len(shape.layer_bytes) == m.arch.n_layer


# ---------------------------------------------------------------- the plan


def _plan(key, vram_total, vram_free, ram_free, **kw):
    m = models.MODELS[key]
    return planner.plan(models.estimate_shape(m), vram_total=int(vram_total * GIB),
                        vram_free=None if vram_free is None else int(vram_free * GIB), ram_free=int(ram_free * GIB),
                        want_context=kw.pop("want", m.context), **kw)


def test_justyns_card_holds_mistral_nemo_whole_and_splits_the_32b():
    """An RX 9060 XT: 15.9 GiB, about 15.3 free with Windows running; 32 GB of RAM, about 24 free."""
    nemo = _plan("mistral-nemo-12b", 15.92, 15.3, 24)
    assert nemo.full_gpu and nemo.context == 32768 and nemo.kv_type == "q8_0" and nemo.fits and not nemo.note
    assert nemo.gpu_bytes <= nemo.vram_usable
    assert nemo.describe("the RX 9060 XT") == "all 41 layers on the RX 9060 XT, 32,768-token memory"
    big = _plan("qwen2.5-32b", 15.92, 15.3, 24)
    assert big.split and big.context == 32768 and big.fits
    assert 30 <= big.gpu_layers <= 45 and big.total_layers == 65
    assert big.gpu_bytes <= big.vram_usable and big.ram_bytes <= big.ram_usable
    shape = models.estimate_shape(models.MODELS["qwen2.5-32b"])
    assert planner.gpu_need(shape, big.gpu_layers + 1, big.context, big.kv_type) > big.vram_usable  # as many as fit
    assert big.describe("the card").startswith(f"{big.gpu_layers} of 65 layers on the card, the rest in RAM")


def test_it_gives_up_memory_before_layers_and_layers_before_speed():
    whole = _plan("qwen2.5-7b", 8, 7.4, 12)
    assert whole.full_gpu and whole.context == 32768
    shorter = _plan("qwen2.5-7b", 8, 6.2, 12)  # all of it fits with half the memory: worth it
    assert shorter.full_gpu and shorter.context == 16384 and "so all of it fits" in shorter.note
    split = _plan("qwen2.5-7b", 8, 5.0, 12)  # not even then: the full memory, part of it in RAM
    assert split.split and split.context == 32768 and split.gpu_layers < whole.gpu_layers


def test_a_card_too_small_to_help_leaves_it_to_the_cpu():
    p = _plan("qwen2.5-32b", 2, 1.8, 24)
    assert p.gpu_layers == 0 and p.context == planner.CPU_CONTEXT and p.fits
    assert "runs on the CPU" in p.note and p.gpu_bytes == 0
    cpu = _plan("llama-3.2-3b", 0, None, 24, gpu=False)
    assert cpu.gpu_layers == 0 and cpu.context == 8192 and cpu.kv_type == "f16" and not cpu.note


def test_settings_can_fix_the_layer_count():
    assert _plan("mistral-nemo-12b", 15.92, 15.3, 24, layers=10).gpu_layers == 10
    assert _plan("mistral-nemo-12b", 15.92, 15.3, 24, layers=0).gpu_layers == 0
    assert _plan("mistral-nemo-12b", 15.92, 15.3, 24, layers=999).gpu_layers == 41


def test_short_of_ram_it_says_so_instead_of_crashing_the_pc():
    tight = _plan("llama-3.2-3b", 0, None, 3.6, gpu=False)
    assert tight.fits and "very little RAM" in tight.note and tight.context == 4096
    none = _plan("llama-3.2-3b", 0, None, 1.0, gpu=False)
    assert not none.fits and "not enough free RAM" in none.note
    assert not _plan("qwen2.5-32b", 15.92, 15.3, 6).fits


def test_unknown_free_memory_assumes_some_is_in_use():
    p = _plan("mistral-nemo-12b", 15.92, None, 24)
    assert p.vram_usable == int(15.92 * GIB * 0.9) - GIB // 2 - planner.margin_bytes(int(15.92 * GIB))


def test_the_memory_stays_inside_what_the_model_was_trained_on(tmp_path):
    shape = gguf.read(write_gguf(tmp_path / "m.gguf", ctx=4096))
    p = planner.plan(shape, vram_total=8 * GIB, vram_free=7 * GIB, ram_free=16 * GIB, want_context=32768)
    assert p.context == 4096 and p.full_gpu


def test_margin_and_cache_precision():
    assert planner.margin_bytes(8 * GIB) == GIB and planner.margin_bytes(24 * GIB) == int(24 * GIB * 0.07)
    assert planner.kv_type_for(8192) == "f16" and planner.kv_type_for(16384) == "q8_0"
    assert planner.kv_type_for(32768, quantize=False) == "f16"
    assert planner.contexts(32768) == [32768, 24576, 16384, 12288, 8192, 6144, 4096]
    assert _plan("qwen2.5-7b", 8, 7.4, 12, quantize_kv=False).kv_type == "f16"


# ---------------------------------------------------------------- free memory, from each source


def test_llama_cpps_device_list_is_read_as_it_prints_it():
    text = ("ggml_vulkan: Found 2 Vulkan devices:\nAvailable devices:\n"
            "  Vulkan0: AMD Radeon RX 9060 XT (RADV GFX1200) (16304 MiB, 15890 MiB free)\n"
            "  Vulkan1: Intel(R) UHD Graphics 770 (8000 MiB, 7000 MiB free)\n"
            "  CUDA0: NVIDIA GeForce RTX 4070 (12281 MiB, 11000 MiB free)\n")
    amd, intel, nvidia = hardware.parse_devices(text)
    assert (amd.vendor, amd.name, amd.device, amd.index, amd.source) == ("amd", "AMD Radeon RX 9060 XT (RADV GFX1200)",
                                                                         "Vulkan0", 0, "Vulkan")
    assert amd.total_gb == pytest.approx(15.92, abs=0.01) and amd.free_gb == pytest.approx(15.52, abs=0.01)
    assert intel.vendor == "intel" and nvidia.vendor == "nvidia" and nvidia.source == "CUDA"
    assert hardware.parse_devices("") == []


def test_the_same_card_is_recognised_across_sources():
    vulkan = Card("amd", "AMD Radeon RX 9060 XT (RADV GFX1200)", 15.92)
    assert hardware.same_card(vulkan, Card("amd", "AMD Radeon RX 9060 XT", 15.92, source="DXGI"))
    assert hardware.same_card(vulkan, Card("amd", "AMD Radeon", 16.0, source="sysfs"))  # a generic name: by memory
    assert not hardware.same_card(vulkan, Card("nvidia", "NVIDIA GeForce RTX 4070", 12.0))
    assert not hardware.same_card(vulkan, Card("amd", "AMD Radeon", 8.0))


@pytest.fixture
def fresh_memory(monkeypatch):
    monkeypatch.setattr(hardware, "_memory", None)
    monkeypatch.setattr(hardware, "ram_free_bytes", lambda meminfo="/proc/meminfo": 20 * GIB)
    for source in ("nvml_cards", "nvidia_cards", "dxgi_cards", "linux_cards", "vulkan_cards"):
        monkeypatch.setattr(hardware, source, lambda *a, **k: [])
    return monkeypatch


def test_free_graphics_memory_comes_from_the_most_exact_source(fresh_memory, tmp_path):
    amd = Hardware("Linux", 16, 32, "amd", "AMD Radeon", 16.0, "sysfs")
    fresh_memory.setattr(hardware, "linux_cards", lambda *a: [Card("amd", "AMD Radeon", 16.0, 12.0, "sysfs", 1)])
    mem = hardware.memory_now(amd)
    assert (mem.vram_free_gb, mem.source, mem.ram_free_gb) == (12.0, "sysfs", 20.0)
    server = tmp_path / "llama-server"
    fresh_memory.setattr(hardware, "vulkan_cards", lambda exe: [
        Card("amd", "AMD Radeon RX 9060 XT (RADV GFX1200)", 15.92, 14.5, "Vulkan", 0, "Vulkan0")] if exe else [])
    mem = hardware.memory_now(amd, server, refresh=True)  # what llama.cpp itself will get beats /sys
    assert (mem.vram_free_gb, mem.source, mem.device) == (14.5, "Vulkan", "Vulkan0")
    nvidia = Hardware("Linux", 16, 32, "nvidia", "NVIDIA GeForce RTX 4070", 12.0, "NVML")
    fresh_memory.setattr(hardware, "nvml_cards", lambda: [Card("nvidia", "NVIDIA GeForce RTX 4070", 12.0, 10.0,
                                                               "NVML", 0)])
    fresh_memory.setattr(hardware, "vulkan_cards", lambda exe: [
        Card("nvidia", "NVIDIA GeForce RTX 4070", 12.0, 11.0, "Vulkan", 0, "Vulkan0")])
    mem = hardware.memory_now(nvidia, server)
    assert (mem.vram_free_gb, mem.source, mem.device) == (10.0, "NVML", "Vulkan0")  # NVML is exact
    assert hardware.memory_now(nvidia, server) is mem  # read again only after a few seconds
    none = hardware.memory_now(Hardware("Linux", 8, 16), refresh=True)
    assert none.vram_free_gb is None and none.ram_free_gb == 20.0


def test_on_windows_free_graphics_memory_is_dxgis_budget(fresh_memory):
    """The reading follows the PC that was detected, so this runs the same on any test machine."""
    amd = Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 15.9, "DXGI")
    fresh_memory.setattr(hardware, "dxgi_cards", lambda *a: [
        Card("amd", "AMD Radeon RX 9060 XT", 15.9, 13.25, "DXGI", 0)])
    fresh_memory.setattr(hardware, "linux_cards", lambda *a: [Card("amd", "AMD Radeon", 16.0, 1.0, "sysfs", 1)])
    mem = hardware.memory_now(amd)
    assert (mem.vram_free_gb, mem.source) == (13.25, "DXGI")  # /sys isn't read for a Windows PC
    mem = hardware.memory_now(Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 15.9, "DXGI"),
                              refresh=True)
    assert mem.vram_free_gb == 13.25


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="Linux's /proc/meminfo")
def test_free_ram_is_what_linux_says_is_available(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32768000 kB\nMemFree:         1000000 kB\nMemAvailable:   20480000 kB\n")
    assert hardware.ram_free_bytes(str(meminfo)) == 20480000 * 1024
    assert hardware.ram_free_bytes(str(tmp_path / "missing")) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="DirectX is Windows'")
def test_dxgi_lists_the_display_adapters_with_their_free_memory():
    """Every Windows PC has at least Microsoft's software adapter, so this calls DXGI for real."""
    cards = hardware.dxgi_cards(include_software=True)
    assert cards, "DXGI listed no adapters"
    for card in cards:
        assert card.name and card.source == "DXGI" and card.total_gb >= 0
        assert card.free_gb is None or card.free_gb >= 0
    assert all(c.name for c in hardware.dxgi_cards())  # without the software one
    assert isinstance(hardware.detect(refresh=True), Hardware)


# ---------------------------------------------------------------- the plan for a run, and the status line


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """Justyn's PC: an RX 9060 XT and 32 GB of RAM, with ClipMonster's llama-server."""
    monkeypatch.setattr(hardware, "_cached", Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 16))
    monkeypatch.setattr(hardware, "memory_now", lambda hw=None, exe=None, refresh=False:
                        Memory(16.0, 15.3, 32.0, 24.0, "Vulkan", "Vulkan0"))
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    monkeypatch.setenv("CLIPMONSTER_LLAMA_SERVER", str(server))
    monkeypatch.setattr(llama_engine, "server_flags", lambda exe: frozenset(llama_engine.FLAGS))
    monkeypatch.setattr(llama_engine, "_gpu_failed_at", 0.0)
    have: set[str] = set()
    monkeypatch.setattr(models, "installed", lambda path: path.name in have)
    return have


def test_the_run_plan_on_justyns_pc(pc):
    plan = llama_engine.make_plan(Settings(local_model="auto"))
    assert plan.name == "Mistral Nemo 12B Instruct" and plan.path.name == "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"
    assert (plan.context, plan.gpu_layers, plan.total_layers, plan.kv_type) == (32768, 41, 41, "q8_0")
    assert plan.card == "AMD Radeon RX 9060 XT" and plan.device == "Vulkan0" and plan.margin_mib == 1146
    assert plan.fits and not plan.fixed_layers and 9 < plan.gpu_gb < 12
    cpu = llama_engine.make_plan(Settings(local_model="auto"), cpu=True)
    assert cpu.gpu_layers == 0 and cpu.context == 8192 and cpu.card == ""
    assert llama_engine.make_plan(Settings(local_model="auto", local_gpu=False)).gpu_layers == 0
    fixed = llama_engine.make_plan(Settings(local_model="auto", local_gpu_layers=20))
    assert fixed.gpu_layers == 20 and fixed.fixed_layers
    assert llama_engine.room(Settings(local_model="auto")) == 32768 - llama_engine.ANSWER_ROOM


def test_the_status_line_says_what_runs_where(pc):
    line = llama_engine.status(Settings(local_model="auto"))
    assert line == ("Mistral Nemo 12B Instruct (downloads once, 7.5 GB): all 41 layers on AMD Radeon RX 9060 XT, "
                    "32,768-token memory, through llama-server.")
    pc.add("Mistral-7B-Instruct-v0.3-Q4_K_M.gguf")  # 3.1's model is here, the better one isn't yet
    line = llama_engine.status(Settings(local_model="auto"))
    assert line.startswith("Mistral 7B Instruct v0.3 (downloaded): all 33 layers on AMD Radeon RX 9060 XT")
    assert "Mistral Nemo 12B Instruct suits this PC better: run `ytclip local-ai download`" in line
    line = llama_engine.status(Settings(local_model="qwen2.5-32b"))
    assert "of 65 layers on AMD Radeon RX 9060 XT, the rest in RAM (slower), 32,768-token memory" in line
    assert "non-commercial" in llama_engine.status(Settings(local_model="command-r-32b"))
    line = llama_engine.status(Settings(local_model="auto", local_gpu=False))
    assert ": on the CPU (16 threads), 8,192-token memory" in line


def test_a_custom_model_that_isnt_gguf_is_named_in_the_status(pc, tmp_path):
    bad = tmp_path / "notes.gguf"
    bad.write_text("not a model")
    line = llama_engine.status(Settings(local_model="custom", local_model_path=str(bad)))
    assert line.startswith("notes.gguf (ready): ") and "isn't a GGUF" in line
    good = write_gguf(tmp_path / "mine.gguf", n_layer=8, ctx=131072)
    line = llama_engine.status(Settings(local_model="custom", local_model_path=str(good)))
    assert line.startswith("mine.gguf (ready): all 9 layers on AMD Radeon RX 9060 XT, 16,384-token memory")
