"""Build whisper.cpp's whisper-cli for ClipMonster (whisper_cpp.py runs it).

    python packaging/build_whisper_cpp.py OUT_DIR [--vulkan | --metal]

--vulkan builds it for the graphics card through Vulkan (AMD, NVIDIA and Intel); it needs the
Vulkan SDK (VULKAN_SDK set, glslc on PATH). --metal (on a Mac) builds it for the Mac's graphics, with
its Metal shaders inside the program. Without either, a CPU-only build, which the tests use. The
release workflow builds it with --vulkan and build.py copies OUT_DIR into the app as whisper-cpp/;
on a Mac with --metal, and build_mac.py puts it in ClipMonster.app.

It's one static program: the only thing it needs from the PC is the graphics driver's vulkan-1.dll
(on a Mac, only macOS itself).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/ggml-org/whisper.cpp"
TAG = "v1.9.5"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=cwd)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument("--vulkan", action="store_true")
    p.add_argument("--metal", action="store_true", help="a Mac build for its graphics (Metal)")
    p.add_argument("--tag", default=TAG)
    p.add_argument("--source", type=Path, help="an existing whisper.cpp checkout instead of cloning one")
    args = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix="whisper-cpp-"))
    src = args.source or work / "src"
    if not args.source:
        run(["git", "clone", "--depth", "1", "--branch", args.tag, REPO, str(src)])
    build = work / "build"
    flags = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF", "-DWHISPER_BUILD_TESTS=OFF",
             "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF",
             # Not tuned to the build machine's CPU: it runs on any PC (or Mac) from the last ten years.
             "-DGGML_NATIVE=OFF"]
    if platform.machine().lower() in ("x86_64", "amd64"):
        flags += ["-DGGML_AVX=ON", "-DGGML_AVX2=ON", "-DGGML_FMA=ON", "-DGGML_F16C=ON"]
    if args.vulkan:
        flags.append("-DGGML_VULKAN=ON")
    if sys.platform == "darwin":
        # The Mac's graphics with --metal (its shaders built into the program), else the CPU with Apple's
        # Accelerate; for the macOS the app supports, on this Mac's kind of processor
        flags += [f"-DGGML_METAL={'ON' if args.metal else 'OFF'}", "-DGGML_METAL_EMBED_LIBRARY=ON",
                  "-DCMAKE_OSX_DEPLOYMENT_TARGET=12.0", f"-DCMAKE_OSX_ARCHITECTURES={platform.machine()}"]
    if sys.platform == "win32":
        # The C++ runtime built in, so there's no Visual C++ runtime to install
        flags += ["-DCMAKE_POLICY_DEFAULT_CMP0091=NEW", "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded"]
    run(["cmake", "-S", str(src), "-B", str(build), *flags])
    run(["cmake", "--build", str(build), "--config", "Release", "--target", "whisper-cli", "-j",
         str(os.cpu_count() or 4)])
    name = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"
    found = next((f for f in build.rglob(name) if f.is_file()), None)
    if found is None:
        print(f"{name} wasn't built", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found, args.out / name)
    for extra in ("LICENSE",):
        if (src / extra).is_file():
            shutil.copy2(src / extra, args.out / f"whisper.cpp-{extra}.txt")
    kind = " (Vulkan)" if args.vulkan else " (Metal)" if args.metal else ""
    (args.out / "VERSION.txt").write_text(f"whisper.cpp {args.tag}{kind}\n", encoding="utf-8")
    print(f"Built {args.out / name}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
