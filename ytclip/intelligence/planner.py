"""How a model fits this PC: how many of its layers go on the graphics card, and how long its memory is.

llama.cpp fills the graphics card from the end of the model: `-ngl 1` puts just the output layer there,
`-ngl N` the output layer and the last N-1 layers, `-ngl n_layer+1` everything. Each layer takes its
slice of the KV cache (the model's memory of what it has read) with it. Whatever isn't on the card stays
in RAM and runs on the CPU: slower, but it runs. So, for a context of C tokens and N layers on the card:

    on the card: output layer + the last N-1 layers + their KV cache for C tokens + working space
    in RAM:      token embeddings + the other layers + their KV cache + working space

`plan` picks the context and N, keeping a margin of graphics memory free for Windows and other programs
so loading never runs the card out of memory. It prefers the whole model on the card, giving up to half
of the wanted context for it (a model that's all on the card runs several times faster), and otherwise
keeps the wanted context and splits the layers between the card and RAM.

A long context's KV cache is stored at 8 bits (q8_0) instead of 16: half the memory, and llama.cpp's own
measurements show no real loss of quality. Pure arithmetic: no files or programs, so it's quick to test.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gguf import Shape

GIB = 1024 ** 3
MIB = 1024 ** 2
UBATCH = 512  # llama.cpp's default physical batch: sizes the working space
CONTEXT_STEPS = (131072, 65536, 49152, 32768, 24576, 16384, 12288, 8192, 6144, 4096, 2048)
MIN_CONTEXT = 4096
CPU_CONTEXT = 8192  # on the CPU alone a long read takes minutes, so the transcript is read in parts instead
Q8_FROM = 8192  # contexts longer than this keep their KV cache at 8 bits
KV_BYTES = {"f16": 2.0, "q8_0": 34 / 32}  # bytes per value: q8_0 is 32 values in 34 bytes
RAM_RESERVE = int(1.5 * GIB)  # left for Windows, ClipMonster itself and ffmpeg
PROCESS_BYTES = 350 * MIB  # llama-server's own code and buffers


def margin_bytes(vram_total: int) -> int:
    """Graphics memory always left free: the larger of 1 GiB and 7% of the card."""
    return max(GIB, int(vram_total * 0.07))


@dataclass
class Plan:
    context: int  # tokens of context
    gpu_layers: int  # llama.cpp's -ngl: 0 = all on the CPU, total_layers = all on the card
    total_layers: int  # n_layer + 1 (the output layer counts as one)
    kv_type: str  # f16 | q8_0
    gpu_bytes: int  # expected on the graphics card
    ram_bytes: int  # expected in RAM
    fits: bool  # False: even the smallest context needs more memory than this PC has free
    vram_usable: int = 0  # free graphics memory minus the margin
    ram_usable: int = 0
    note: str = ""  # why it doesn't fit, or what it gave up to fit

    @property
    def on_gpu(self) -> bool:
        return self.gpu_layers > 0

    @property
    def full_gpu(self) -> bool:
        return self.gpu_layers >= self.total_layers

    @property
    def split(self) -> bool:
        return 0 < self.gpu_layers < self.total_layers

    def describe(self, card: str = "the graphics card") -> str:
        """'all 41 layers on AMD Radeon RX 9060 XT, 32,768-token memory' (or the split, or the CPU)."""
        memory = f"{self.context:,}-token memory"
        if self.full_gpu:
            where = f"all {self.total_layers} layers on {card}"
        elif self.split:
            where = f"{self.gpu_layers} of {self.total_layers} layers on {card}, the rest in RAM (slower)"
        else:
            where = "on the CPU"
        return f"{where}, {memory}"


def kv_type_for(context: int, quantize: bool = True) -> str:
    return "q8_0" if quantize and context > Q8_FROM else "f16"


def kv_bytes(shape: Shape, layers: list[int], context: int, kv_type: str) -> float:
    """The KV cache of the given layers for `context` tokens."""
    b = KV_BYTES.get(kv_type, 2.0)
    per_token = sum(shape.kv_heads[i] * (shape.head_dim_k + shape.head_dim_v) * b for i in layers
                    if i < len(shape.kv_heads))
    return per_token * context


def working_bytes(shape: Shape, context: int, output_here: bool, flash: bool = True) -> int:
    """llama.cpp's compute buffer: the logits (when the output layer is here) and the feed-forward
    intermediates for one batch, plus, without flash attention, the attention scores for the whole
    context."""
    out = UBATCH * 4 * ((shape.n_vocab if output_here else 0) + 3 * shape.n_ff + 8 * shape.n_embd) + 192 * MIB
    if not flash:
        out += UBATCH * context * max(1, shape.n_head) * 4
    return int(out)


def gpu_need(shape: Shape, ngl: int, context: int, kv_type: str, flash: bool = True) -> int:
    """Graphics memory for `ngl` layers (llama.cpp's count: the output layer, then the last layers)."""
    if ngl <= 0:
        return 0
    n = shape.n_layer
    rep = min(ngl - 1, n)
    layers = list(range(n - rep, n))
    return int(shape.output_bytes + sum(shape.layer_bytes[n - rep:]) + kv_bytes(shape, layers, context, kv_type)
               + working_bytes(shape, context, True, flash))


def ram_need(shape: Shape, ngl: int, context: int, kv_type: str, flash: bool = True) -> int:
    """RAM for the rest: the token embeddings (always in RAM), the layers not on the card, their KV cache."""
    n = shape.n_layer
    rep = min(max(ngl - 1, 0), n)
    cpu_layers = list(range(0, n - rep))
    output_here = ngl <= 0
    # A tied output layer in RAM is the token embeddings themselves: only its norm is extra.
    output = (max(0, shape.output_bytes - shape.embed_bytes) if shape.tied_output else shape.output_bytes)
    return int(shape.embed_bytes + (output if output_here else 0) + sum(shape.layer_bytes[:n - rep])
               + kv_bytes(shape, cpu_layers, context, kv_type)
               + working_bytes(shape, context, output_here, flash if output_here else True)
               + PROCESS_BYTES)


def contexts(want: int, floor: int = MIN_CONTEXT) -> list[int]:
    """The wanted context, then smaller steps down to `floor`."""
    want = max(2048, int(want))
    return [want] + [c for c in CONTEXT_STEPS if c < want and c >= min(floor, want)]


def plan(shape: Shape, *, vram_total: int, vram_free: int | None, ram_free: int, want_context: int,
         gpu: bool = True, layers: int | None = None, quantize_kv: bool = True, flash: bool = True,
         min_context: int = MIN_CONTEXT) -> Plan:
    """The context and layer split for this model on this PC.

    `vram_free` None means no source could say: 90% of the card less half a GiB is assumed. `layers`
    (from Settings) fixes how many layers go on the card. `quantize_kv` False keeps the KV cache at 16
    bits (a llama.cpp that can't store it at 8). `flash` False sizes for llama.cpp without flash attention,
    whose working space grows with the context."""
    total = shape.n_layer + 1
    if shape.n_ctx_train:
        want_context = min(want_context, max(shape.n_ctx_train, 2048))
    steps = contexts(want_context, min_context)
    ram_usable = max(0, int(ram_free) - RAM_RESERVE)
    if gpu and vram_total > 0:
        free = int(vram_free) if vram_free is not None else max(0, int(vram_total * 0.9) - GIB // 2)
        vram_usable = max(0, free - margin_bytes(vram_total))
    else:
        vram_usable = 0

    def kv(ctx: int) -> str:
        return kv_type_for(ctx, quantize_kv)

    def make(ctx: int, ngl: int, note: str = "", fits: bool = True) -> Plan:
        return Plan(ctx, ngl, total, kv(ctx), gpu_need(shape, ngl, ctx, kv(ctx), flash),
                    ram_need(shape, ngl, ctx, kv(ctx), flash), fits, vram_usable, ram_usable, note)

    def ram_ok(ctx: int, ngl: int) -> bool:
        return ram_need(shape, ngl, ctx, kv(ctx), flash) <= ram_usable

    def most_layers(ctx: int) -> int:
        """The most layers that fit on the card at this context (what fits only grows with fewer layers)."""
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if gpu_need(shape, mid, ctx, kv(ctx), flash) <= vram_usable:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def short_of_ram(ctx: int, ngl: int, note: str) -> Plan:
        """Nothing fits with RAM to spare: still worth a try if it fits in what's free at all."""
        if ram_need(shape, ngl, ctx, kv(ctx), flash) <= int(ram_free):
            return make(ctx, ngl, (note + "; " if note else "") + "very little RAM is left free while it runs")
        return make(ctx, ngl, "not enough free RAM for it, even with a short memory", fits=False)

    def on_cpu(note: str = "") -> Plan:
        cpu_steps = contexts(min(want_context, CPU_CONTEXT), min(min_context, CPU_CONTEXT))
        for ctx in cpu_steps:
            if ram_ok(ctx, 0):
                return make(ctx, 0, note)
        return short_of_ram(cpu_steps[-1], 0, note)

    if layers is not None and layers >= 0 and vram_usable > 0:  # fixed in Settings
        ngl = min(int(layers), total)
        if ngl == 0:
            return on_cpu()
        for ctx in steps:
            if ram_ok(ctx, ngl):
                return make(ctx, ngl, "" if ctx == steps[0] else f"memory cut to {ctx:,} tokens to fit in RAM")
        return short_of_ram(steps[-1], ngl, "")

    if vram_usable <= 0:
        return on_cpu("not enough free graphics memory, so it runs on the CPU" if gpu and vram_total else "")

    # The whole model on the card, giving up to half the wanted context for it.
    for ctx in steps:
        if ctx < steps[0] / 2:
            break
        if most_layers(ctx) >= total and ram_ok(ctx, total):
            note = "" if ctx == steps[0] else f"memory cut to {ctx:,} tokens so all of it fits on the graphics card"
            return make(ctx, total, note)

    # Split between the card and RAM, keeping the wanted context unless RAM is short.
    for ctx in steps:
        ngl = most_layers(ctx)
        if ngl <= 1:  # the card holds next to nothing: the CPU alone is as quick and leaves the card alone
            return on_cpu("not enough free graphics memory, so it runs on the CPU")
        if ram_ok(ctx, ngl):
            note = "" if ctx == steps[0] else f"memory cut to {ctx:,} tokens to fit in RAM"
            return make(ctx, ngl, note)
    return short_of_ram(steps[-1], most_layers(steps[-1]), "")
