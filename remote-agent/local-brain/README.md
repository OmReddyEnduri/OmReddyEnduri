# Local Brain: Qwen3.6-27B on your GPU

One file, `qwen_brain.py`, standard-library Python only (no pip installs). It:

- **Installs** llama.cpp (the Windows CUDA build plus its CUDA DLLs) and `unsloth/Qwen3.6-27B-GGUF` into `D:\om\qwen`. The quant is picked from your VRAM: 32 GB gets Q6_K with a 64k context.
- **Checks and repairs itself at every start:** GPU, llama-server, model file size, autostart, relay token and disk. It also picks up a newer copy of the script after a `git pull`.
- **Runs the model on the GPU only.** Weights stream straight into VRAM, so about 3 GB of RAM is enough. After 30 idle minutes it unloads the model, and it loads it again automatically when a message or API call arrives. If it crashes, it restarts.
- **Serves an OpenAI-compatible API** at `http://127.0.0.1:8766/v1`, using model `qwen` and the `qwk_...` key from `D:\om\qwen\config.json`.
- **Joins the relay team silently as Qwen** (`@qwen`): no greeting and no idle work. It keeps one waiting request open to the relay, and the relay answers that request the moment a message for Qwen arrives.
- **Starts hidden at every login,** via `QwenBrain.vbs` in your Startup folder.

```powershell
python D:\om\OmReddyEnduri\remote-agent\local-brain\qwen_brain.py   # install + start (asks for the Brain token once)
python D:\om\qwen\qwen_brain.py check       # run every check, repair, print the report
python D:\om\qwen\qwen_brain.py status      # health, name, model state, API key
python D:\om\qwen\qwen_brain.py stop        # stop until next login
python D:\om\qwen\qwen_brain.py uninstall   # remove autostart
```

**Needs:** an NVIDIA driver, about 30 GB free on D:, and Python 3.10+.

**Settings** in `config.json`:
- `idle_unload_min`: set it to 0 to keep the model loaded all the time.
- `think`: set it to false for faster, shorter answers.

**Logs:** `D:\om\qwen\logs`.
