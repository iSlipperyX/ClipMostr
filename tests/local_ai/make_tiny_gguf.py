"""A tiny random-weight Llama model in GGUF, for testing the local AI for real without a 2 GB download.

    python tests/local_ai/make_tiny_gguf.py LLAMA_CPP_SOURCE OUT.gguf

It borrows the Llama 3 tokenizer from llama.cpp's own test vocab (models/ggml-vocab-llama-bpe.gguf) and
writes 2 layers of random weights with llama.cpp's gguf-py (needs numpy and pyyaml). Its words are
nonsense, but llama-server still has to load it, stream an answer and hold it to ClipMonster's JSON
schema, which is what tests/test_local_ai_real.py checks.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)  # (build/ isn't there yet on a fresh checkout)
    sys.path.insert(0, str(src / "gguf-py"))
    import gguf
    import numpy as np

    vocab = gguf.GGUFReader(str(src / "models" / "ggml-vocab-llama-bpe.gguf"))
    w = gguf.GGUFWriter(str(out), "llama")
    n_embd, n_ff, n_layer, n_head = 64, 128, 2, 4
    w.add_name("tiny test llama")
    w.add_context_length(4096)
    w.add_embedding_length(n_embd)
    w.add_block_count(n_layer)
    w.add_feed_forward_length(n_ff)
    w.add_head_count(n_head)
    w.add_head_count_kv(n_head)
    w.add_layer_norm_rms_eps(1e-5)
    w.add_rope_dimension_count(n_embd // n_head)
    w.add_file_type(0)
    n_vocab = 0
    for f in vocab.fields.values():
        if not f.name.startswith("tokenizer."):
            continue
        kind = f.types[0]
        if kind == gguf.GGUFValueType.ARRAY:
            sub = f.types[-1]
            if sub == gguf.GGUFValueType.STRING:
                values = [bytes(f.parts[i]).decode("utf-8", "replace") for i in f.data]
            else:
                values = [f.parts[i].tolist()[0] for i in f.data]
            if f.name == "tokenizer.ggml.tokens":
                n_vocab = len(values)
            w.add_key_value(f.name, values, kind, sub_type=sub)
        elif kind == gguf.GGUFValueType.STRING:
            w.add_key_value(f.name, bytes(f.parts[f.data[0]]).decode(), kind)
        else:
            w.add_key_value(f.name, f.parts[f.data[0]].tolist()[0], kind)
    w.add_vocab_size(n_vocab)
    rng = np.random.default_rng(0)

    def r(*shape):
        return (rng.standard_normal(shape) * 0.02).astype(np.float32)

    w.add_tensor("token_embd.weight", r(n_vocab, n_embd))
    for i in range(n_layer):
        w.add_tensor(f"blk.{i}.attn_norm.weight", np.ones(n_embd, np.float32))
        for name in ("q", "k", "v", "output"):
            w.add_tensor(f"blk.{i}.attn_{name}.weight", r(n_embd, n_embd))
        w.add_tensor(f"blk.{i}.ffn_norm.weight", np.ones(n_embd, np.float32))
        w.add_tensor(f"blk.{i}.ffn_gate.weight", r(n_ff, n_embd))
        w.add_tensor(f"blk.{i}.ffn_up.weight", r(n_ff, n_embd))
        w.add_tensor(f"blk.{i}.ffn_down.weight", r(n_embd, n_ff))
    w.add_tensor("output_norm.weight", np.ones(n_embd, np.float32))
    w.add_tensor("output.weight", r(n_vocab, n_embd))
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
