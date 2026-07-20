# Vendored third-party code

## llama-cpp-python 0.3.34 (MIT)

`llama_cpp/` is the Python package from [abetlen/llama-cpp-python](https://github.com/abetlen/llama-cpp-python)
v0.3.34, vendored so the in-process embedding runtime needs no runtime pip
install, no `--extra-index-url`, and no external Ollama server. License:
`llama_cpp/LICENSE.md` (MIT, includes bundled llama.cpp/ggml — also MIT).

What was changed relative to the upstream wheel:

- `llama_cpp/lib/` (bundled native libs) removed — per-platform libraries live
  in `llama_cpp_libs/<platform>/` instead and are selected at runtime via the
  `LLAMA_CPP_LIB_PATH` env var (upstream-supported override, see
  `llama_cpp/llama_cpp.py`).
- `llama_cpp/server/` removed (FastAPI server — unused, heavy deps).
- `llama_cpp.llama_cache`'s module-level `import diskcache` is satisfied by a
  `sys.modules` stub installed in `kiro_crew.embeddings._install_diskcache_stub`
  (KiroCrew never uses disk-backed LLM state caching; a real installed
  diskcache, if present, wins).

`llama_cpp_libs/` holds the minimal verified shared-library closure per
platform, extracted from the official prebuilt CPU wheels
(https://abetlen.github.io/llama-cpp-python/whl/cpu):

| Dir | Source wheel |
|-----|--------------|
| `linux_x86_64/`  | `llama_cpp_python-0.3.34-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` (sha256 f34c26f51ec4fd4e0355c5384b7056f877bf7a38c9d7897a46c78118ca900366) |
| `linux_aarch64/` | `llama_cpp_python-0.3.34-py3-none-manylinux2014_aarch64.manylinux_2_17_aarch64.whl` (sha256 725d8a324032b3f1143c20eee62e47415476eb85127e8a134e3a431d666d21d1) |
| `macos_arm64/`   | `llama_cpp_python-0.3.34-py3-none-macosx_11_0_arm64.whl` (sha256 d42e069db63c11494f429589fb0b7b5d3862d72d4ad5e8ef311e0ece7865b33d) |
| `win_amd64/`     | `llama_cpp_python-0.3.34-py3-none-win_amd64.whl` (sha256 6526fff614e5ef7e439e6369e076a78073e45e1d791dbe1d5e5d42661f46ca1a) |

Linux libs are manylinux2014 (glibc ≥ 2.17) — they run on both AL2 (2.26) and
AL2023 (2.34). The macOS dylibs embed the Metal shader (no separate
`.metallib`). Windows DLLs are found via `os.add_dll_directory`.

To upgrade: download the four wheels for the new version, re-extract the same
closure (`libllama` + `libggml*` + vendored `libgomp` on Linux; top-level
dylibs on macOS), replace `llama_cpp/` with the new wheel's Python code (minus
`lib/` and `server/`), and re-run the embedding smoke test in
`test/test_embeddings.py`.
