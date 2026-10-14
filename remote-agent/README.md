# Remote Laptop Agent + Relay

```
Brain (LLM) --HTTPS--> Relay (Render + Postgres) <--HTTPS polling-- Laptop agent (PySide6) --> your computer
```
The laptop only makes **outbound** HTTPS calls (poll every 2-5 s, heartbeat every 10 s). No tunnel, no open port.

| Part | Path | Notes |
|---|---|---|
| Relay | `relay/` | FastAPI + SQLAlchemy; Postgres on Render. Dashboard at `/dashboard` |
| Agent | `agent/` | PySide6 desktop app + local UI on `127.0.0.1:8765` |
| Brain prompt | `docs/BRAIN_PROMPT.md` | Paste into LLM Arena (fill in token + device id) |
| Blueprint | `render.yaml` | Optional one-click Render deploy |

## Relay API (all JSON over HTTPS)
Brain token: `GET /api/v1/devices/status`, `POST /api/v1/tasks`, `GET /api/v1/tasks[/{request_id}]`, `POST /api/v1/tasks/{request_id}/cancel`.
Device token: `POST /api/v1/devices/heartbeat`, `GET /api/v1/agent/tasks/next`, `POST /api/v1/tasks/{id}/decision`, `POST /api/v1/tasks/{id}/result`.
Enroll token: `POST /api/v1/devices/register`. Admin token: `GET /api/v1/admin/overview`, `POST /api/v1/admin/devices/{id}/revoke|disconnect`.
Task states: `queued → claimed → running → completed|failed|timeout|cancelled`, or `rejected|blocked|expired`.

## Deploy the relay (Render)
1. New Postgres (free or paid) and a Python web service from this repo/branch.
   Build: `cd remote-agent/relay && pip install -r requirements.txt`
   Start: `cd remote-agent/relay && uvicorn app.asgi:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`
2. Env vars: `DATABASE_URL` (internal URL), `BRAIN_API_TOKEN`, `ADMIN_TOKEN`, `ENROLL_TOKEN` (each >= 24 random chars; `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
3. Check `GET /healthz` -> `{"ok":true,"database":"postgresql"}`.

## Install the agent (normal user, never admin/root)
```
cd agent && python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m laptop_agent init --relay-url https://laptop-relay.onrender.com   # prompts: enroll token, workspace folder
python -m laptop_agent run            # desktop window; add --headless for console approvals
```
Config (device token, mode 600): Linux `~/.config/laptop-agent/`, macOS `~/Library/Application Support/LaptopAgent/`, Windows `%APPDATA%\LaptopAgent\`. Local audit log lives beside it (rotating, 5 x 1 MB).

## Ask level (set in the desktop window only)
| Level | Automatic (no popup) | Always asks |
|---|---|---|
| 1 Low | reads, creating/editing files and folders inside the workspace | run commands/scripts, installs, deletes, screenshots |
| 2 Medium (default) | reads, creating NEW files and folders | overwriting files, plus everything level 1 asks |
| 3 High | status, listing, reading only | every change |

Blocked actions stay blocked at every level. Lowering the level asks you to confirm. The level can't be changed from the web UI, chat, or the Brain, and the change is written to the local audit log.

## Permissions (enforced on the laptop, the relay cannot override)
- **Auto**: device_status, list_dir and read_file inside approved workspaces (non-secret text only).
- **Ask** (native dialog: APPROVE ONCE / REJECT / CANCEL; default button REJECT; no timeout): write_file, make_dir, delete_file (single file), run_command, run_script, install_package.
- **Block**: anything outside workspaces, credential/key/.env files, shell startup files, git hooks, the agent's own config, sudo/su, disk formatting, recursive deletes of broad paths, security-disabling commands, pipe-to-shell, URL/VCS package specs, unknown task types, malformed args.
- Approval is re-verified right before execution (state may change while the dialog is open). Approvals are only possible in the native dialog/console, never the web UI or chat.

## Reliability / safety
Exponential backoff + jitter, bounded output (128 KB) with `output_truncated`, secret redaction (tokens, keys, `password=…`), process-tree kill on timeout/cancel/disconnect, duplicate `request_id` runs once, one task at a time, in-flight tasks fail with `device_disconnected` if the laptop goes silent for 90 s, scrubbed child environment, no shell strings (argv only).

## Talking to the Brain, memory, previews
- **App chat** (Chat tab) goes to the Brain; the Brain's replies appear there. The red **STOP the Brain** button sends the word "stop".
- **Local endpoint** (OpenAI-compatible, 127.0.0.1 only): `http://127.0.0.1:8765/v1` with the API key shown on the app's **Connect** page (`POST /v1/chat/completions`, model `brain`).
- **Relay endpoint** (works from anywhere, needs `USER_API_TOKEN` set on the relay): `POST https://laptop-relay.onrender.com/api/v1/chat` with `{"text":"..."}` and `Authorization: Bearer <USER_API_TOKEN>`; it waits up to 20 s for the Brain's reply. `GET /api/v1/chat` shows the conversation.
- **Memory:** the Brain keeps notes, preferences, skills and lessons in the relay (`/api/v1/memory*`). Blocked, rejected and failed tasks are recorded as lessons automatically. View/delete entries on the dashboard's Memory tab.
- **Previews** open in the app's Preview tab (images, text, markdown, basic HTML). Full JavaScript pages, video and PDF need `pip install PySide6-Addons` (large, ~200+ MB).

## Multi-agent
- Run the Brain prompt in several LLM Arena windows. Each registers with the relay and gets a **name** (Nova, Atlas, Echo, ...). Call one with `@nova ...` in any chat.
- The **laptop app is the router**: it picks the agent per chat (sticky, load-balanced), and if an agent goes offline or does not pick a message up within 30 s it retracts the message and hands the chat to another agent **with the same context**.
- **Chat context lives only on your laptop** (`chats.db` in the app's config folder: history, notes the agents write, task events, hand-over log). The relay only holds short-lived messages (deleted after 1 h) and tasks (after 48 h).
- Shared Brain memory (lessons, skills, preferences) stays on the relay.
- **Preview** tab: library of every photo/video the agents send, viewer with details, and an in-app video player (needs ffmpeg: `pip install imageio-ffmpeg`; silent).

## Tests
```
cd relay && pip install -r requirements.txt && pytest -q
cd agent && pip install -r requirements.txt && pytest -q      # needs relay/ alongside; Qt runs offscreen
```
Known limits: free Render tier sleeps after 15 idle minutes (cold start ~30-60 s) and free Postgres expires after 30 days; the desktop window has been tested headless only.
