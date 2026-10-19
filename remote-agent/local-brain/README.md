# Local Brain: Qwen3.6-27B on your GPU

One file, `qwen_brain.py`, standard-library Python only (no pip installs). It:

- installs llama.cpp (Windows CUDA build plus its CUDA DLLs) and `unsloth/Qwen3.6-27B-GGUF` into `D:\om\qwen`. The quant is picked from your VRAM: 32 GB gets Q6_K with a 64k context.
- runs the model fully on the GPU and restarts it if it crashes.
- serves an OpenAI-compatible API on `http://127.0.0.1:8766/v1`, using model `qwen` and the `qwk_...` key from `D:\om\qwen\config.json`.
- joins the relay team as **Qwen** (`@qwen`), listens all the time, and works on the laptop through relay tasks.
- starts itself hidden at every login, through `QwenBrain.vbs` in your Startup folder.

```powershell
python D:\om\OmReddyEnduri\remote-agent\local-brain\qwen_brain.py           # install + start (asks for the Brain token once)
python D:\om\qwen\qwen_brain.py status                                     # health, name, API key
python D:\om\qwen\qwen_brain.py stop                                       # stop until next login
python D:\om\qwen\qwen_brain.py uninstall                                  # remove autostart
```

Needs: an NVIDIA driver, about 30 GB free on D:, and Python 3.10+. To override the automatic quant pick, use `--quant Q4_K_M --ctx 16384`. Logs are in `D:\om\qwen\logs`.
