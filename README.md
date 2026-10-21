# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
  <a href="https://linux.do/" target="_blank"><img src="https://img.shields.io/badge/Community-LINUX%20DO-111827?logo=linux&logoColor=white" alt="LINUX DO" /></a>
</p>

<p align="center">
  🤝 This open-source project is linked with and endorses the <b><a href="https://linux.do/" target="_blank">LINUX DO Community (https://linux.do/)</a></b> — a new kind of ideal community (sincere, friendly, united, professional)
</p>

Reverse-engineers the cutting-edge multimodal capabilities of the **[muse.ai](https://muse.ai/)** web app and wraps them as a standard **OpenAI-compatible RESTful API**. Using headless-browser CDP access, warm-standby WebSocket tunnel reuse and dynamic session management, it natively supports text chat (streaming first token in 2–3 seconds), text-to-image, image-to-image editing, text-to-video and first-frame image-to-video. It also provides multi-account pool affinity rotation, fully automatic 48-hour session renewal, cloud VM wake/keep-alive, and a companion Chrome extension for one-click account import.

---

## 🌟 Key Features

- 💬 **Standard chat endpoints (Chat Completions & Responses API)**
  - Fully compatible with `/v1/chat/completions` and the `/v1/responses` protocol used by default in the new Codex.
  - Native SSE streaming typewriter output (`stream=True`) as well as synchronous full responses. Built-in warm tabs and Noise WebSocket tunnel affinity reuse give **first-token latency of only 2–3 seconds in continuous conversations**.
  - Supports multi-turn context and system prompts.
  - Built-in smart model alias mapping: common model names such as `gpt-4o`, `gpt-5`, `claude-sonnet-4` and `deepseek-chat` are routed automatically.
- 🎨 **High-quality image generation and editing (Images Generations & Edits)**
  - Fully compatible with `/v1/images/generations` (text-to-image) and `/v1/images/edits` (image-to-image / reference image editing).
  - Supports multiple aspect ratios such as `1:1`, `16:9`, `9:16`, `4:3` and `3:4`, direct reference image upload, and clean watermark-free output.
  - Supports both `url` direct links and `b64_json` response formats, with built-in fast detection of text-only refusals to prevent queue deadlocks.
  - Image-to-image returns only media newly generated in this request: upload previews and historical attachments are excluded and the download is bound to the selected result. If the reference image upload or prompt submission cannot be confirmed, an explicit error is returned instead of silently returning the original image.
- 🎬 **Text-to-video / Image-to-video (Videos)**
  - Native integration with Muse's top-tier video generation model. Duration requests of 5 / 6 / 8 / 10 seconds can be passed (actual clip length is determined by Muse's upstream output), with `9:16` portrait / `16:9` landscape output.
  - Supports uploading a first-frame reference image (Data URL / HTTP URL) for strict first-frame image-to-video creation.
  - Asynchronous task architecture (`/v1/videos` to create a task + `/v1/videos/{task_id}` to poll status).
  - Built-in media service `/v1/media/{filename}` that automatically persists generated MP4 / WebP assets.
- ⚡ **True multi-account parallel execution**
  - Based on Chromium's native `Target.createBrowserContext` incognito isolation containers, so tasks run concurrently in independent sandboxes.
  - Multiple accounts can generate images, videos and streaming chats simultaneously with zero mutual blocking; cookies and sessions are 100% isolated with no cross-talk.
  - Built-in smart account conflict scheduler: tasks on the same account are mutually exclusive to avoid collisions, while different accounts run fully in parallel.
  - The concurrency limit can be raised via `MUSE2API_CONCURRENCY` according to your machine's capacity (default: 2).
- 🔄 **Multi-account pool with warm-connection affinity scheduling**
  - Import an unlimited number of Muse accounts.
  - Smart dispatch based on Warm-Tab Affinity and LRU, balancing ~2-second responses with even usage across accounts.
  - When an account runs out of quota or its session fails, it is automatically flagged and requests fail over seamlessly and instantly to a healthy backup account.
- 🛡️ **Fully automatic 48-hour session renewal and cloud VM keep-alive**
  - A background heartbeat coroutine calls `/api/session` directly to renew `hatch_vml` (+48h) and `hatch_sess` (+30d), and calls `/api/hatch/vm/wake` to keep the cloud workspace VM warm.
  - Solves the static 48-hour Meta cookie expiry and VM sleep disconnects, so there is no need to log in again frequently.
- 🧩 **Companion Chrome extension for one-click import**
  - No need to dig through F12 network captures: click the extension icon to extract the current browser login state (including `HttpOnly` core cookies with their real expiry times) and push it securely to the account pool.
- 🖥️ **Modern dark-mode operations console (Web Console) with live online upgrades**
  - Built-in, ready-to-use Web UI: view service health, account pool quota and status in real time, keep the whole pool alive with one click, replay task progress, manage the media library and debug the API online.
  - **Update broadcast and one-click upgrade for all nodes**: when the official GitHub repository publishes a new version or fix, the admin console of every deployed node automatically shows an update banner at the top. Click **"⚡ Upgrade online and restart"** to pull the latest code and restart gracefully (local `.env` configuration and account data are preserved).

---

## 🚀 Quick Deployment

### Option 1: Docker Compose (recommended, one command to get started)

1. **Clone the code and enter the directory**:
   ```bash
   git clone https://github.com/czg86389-hub/muse2api.git
   cd muse2api
   ```

2. **Configure environment variables (optional)**:
   ```bash
   cp .env.example .env
   # Edit .env as needed; it is recommended to set MUSE2API_KEY to your own admin key
   ```

   Docker Compose reads `MUSE2API_KEY` from `.env`; it is no longer overridden by the sample value in the Compose file.

3. **Start the container**:
   ```bash
   docker compose up -d
   ```

4. **Open the admin console**:
   In your browser, go to: `http://<your-server-IP>:18610/admin?key=<your-MUSE2API_KEY>`

---

### Option 2: Bare metal / VPS deployment (Ubuntu / Debian)

1. **Install system dependencies and Chromium**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y chromium fonts-wqy-zenhei python3 python3-pip python3-venv
   ```

2. **Set up a Python virtual environment**:
   ```bash
   cd /opt/muse2api
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure the systemd service**:
   ```bash
   sudo cp deploy/muse2api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now muse2api
   ```

4. **Nginx reverse proxy configuration (important: prevents 502 timeouts and stalled streams)**:
   If you use Nginx / BT Panel / 1Panel as a reverse proxy, make sure to raise `proxy_read_timeout` to `600s` and turn off `proxy_buffering` (see `deploy/nginx.example.conf`):
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:18610;
       proxy_read_timeout 600s;      # Prevents Nginx 502 Bad Gateway on long image/video generations
       proxy_send_timeout 600s;
       proxy_buffering off;          # Ensures zero-delay SSE streaming of chat output
       client_max_body_size 64M;     # Allows large reference image uploads
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```

---

## 🧩 Account Import (Chrome extension / Tampermonkey script)

### Option A: Chrome / Edge extension
The project ships a dedicated Chrome import extension (in the `extension/` directory), so you no longer need to extract cookies manually via F12:

1. Open Chrome / Edge, go to `chrome://extensions`, and enable **"Developer mode"** in the top-right corner.
2. Click **"Load unpacked"** and select the `extension` directory of this project.
3. In that browser, log in to [muse.ai](https://muse.ai/) until you reach the main chat screen.
4. Click the extension icon in the top-right of the browser and enter your service URL (e.g. `http://1.2.3.4:18610`) and `MUSE2API_KEY`.
5. Click **"Read and import"** — the account is synced into the pool within seconds!

### Option B: Tampermonkey userscript (cross-browser, no extension install)
For users who do not want to enable developer mode or who use Firefox / Safari, a userscript is provided (at `tools/muse2api_cookie_importer.user.js`):
1. Install the Tampermonkey add-on in your browser.
2. Create a new script, paste in the contents of `tools/muse2api_cookie_importer.user.js`, and save.
3. Open the [muse.ai](https://muse.ai/) website. A floating **"⚡ Import to muse2api"** button appears in the bottom-right corner. Right-click it to configure the service URL and key; left-click to push the account into the pool with one click (it is also copied to the clipboard as a fallback).

---

## 📖 API Examples

All protected endpoints require this header:
```http
Authorization: Bearer <your-MUSE2API_KEY>
```

### 1. Chat Completions

```bash
curl -X POST "http://localhost:18610/v1/chat/completions" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "Write a short poem about cyberpunk and neon night rain"}
    ],
    "stream": false
  }'
```

### 2. Image Generation

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8k resolution",
    "size": "16:9",
    "response_format": "url"
  }'
```

**Sample response**:
```json
{
  "created": 1790148495,
  "data": [
    {
      "revised_prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8k resolution",
      "url": "http://localhost:18610/v1/media/img_abc123.webp",
      "kind": "image",
      "bytes": 54210
    }
  ]
}
```

### 3. Text-to-video / Image-to-video (Videos)

- **Step 1: Create a generation task**
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Golden maple leaves drifting gently in the breeze, sunlight filtering through the treetops",
      "duration": 5,
      "size": "16:9"
    }'
  ```
  Returns a task ID: `{"id": "task_xyz789", "status": "queued"}`

- **Step 2: Poll task progress**
  ```bash
  curl "http://localhost:18610/v1/videos/task_xyz789" \
    -H "Authorization: Bearer m2a_your_secret_key"
  ```
  When finished, it returns:
  ```json
  {
    "id": "task_xyz789",
    "status": "succeeded",
    "progress": 100,
    "result": {
      "url": "http://localhost:18610/v1/media/vid_xyz789.mp4"
    }
  }
  ```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MUSE2API_KEY` | Auto-generated | Admin console and API authentication key (starts with `m2a_`) |
| `MUSE2API_HOST` | `127.0.0.1` | Listen IP (`0.0.0.0` recommended inside Docker) |
| `MUSE2API_PORT` | `18610` | Service port |
| `MUSE2API_PUBLIC_BASE` | `empty` | Full public URL prefix; when empty, the frontend detects the access origin automatically |
| `MUSE2API_CHROMIUM` | `chromium` | Full path to the browser executable |
| `MUSE2API_CDP_PORT` | `19210` | Internal CDP debugging port |
| `MUSE2API_CONCURRENCY` | `2` | Maximum number of concurrent generation tasks (can be raised freely according to server resources; default 2) |
| `MUSE2API_IMAGE_TIMEOUT` | `240` | Image generation timeout (seconds) |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | Video generation timeout (seconds) |
| `MUSE2API_CHAT_TIMEOUT` | `300` | Chat generation timeout (seconds) |

---

## 🔒 Security and Privacy

- **Zero data leakage**: all data of this software (including account credentials, task queues and media files) is persisted locally in the `data/` directory. It does not depend on any third-party telemetry or external relay service.
- **Open-source compliance**: this project is intended only for technical exchange, research on automated system operations, and automated testing. Do not use it for any purpose that violates the Meta platform terms of service or the laws and regulations of any country.

---

## 🤝 Community and Links

This project is linked with and strongly endorses the **[LINUX DO Community](https://linux.do/)**. Thanks to the community members for their discussions, feedback and support:

- 🌐 **[LINUX DO Community (https://linux.do/)](https://linux.do/)** — a new kind of ideal community (sincere, friendly, united, professional — a community we can all be proud to build together)

---

## 👥 Contributors

Thanks to the following developers for their code contributions and improvements to MUSE2API (in order of PR merge):

- 🌟 **[@cpt-kenvie](https://github.com/cpt-kenvie)** (PR [#2](https://github.com/czg86389-hub/muse2api/pull/2)) — Fixed Docker Compose reading `.env` variables being overridden by the sample configuration
- 🌟 **[@CarloCPP](https://github.com/CarloCPP)** (PR [#6](https://github.com/czg86389-hub/muse2api/pull/6)) — Contributed the Tampermonkey one-click import script and keep-alive configuration for disabled accounts
- 🌟 **[@djs-91](https://github.com/djs-91)** (PR [#9](https://github.com/czg86389-hub/muse2api/pull/9)) — Fixed Base URL collapse causing broken admin page URL resolution when `MUSE2API_PUBLIC_BASE` is not set
- 🌟 **[@toby-bridges](https://github.com/toby-bridges)** (PR [#8](https://github.com/czg86389-hub/muse2api/pull/8)) — Adapted to the live Chinese UI of muse.ai, fixed attachment upload confirmation and false Stop-state detection, and hardened protection against picking the wrong video output or cover image
- 🌟 **[@LF2023](https://github.com/LF2023)** (PR [#18](https://github.com/czg86389-hub/muse2api/pull/18)) — Fixed video generation matching the static cover frame based on tid alone, and standardized expected-match and anti-misselection logic
- 🌟 **[@wuliaolaozi](https://github.com/wuliaolaozi)** (PR [#27](https://github.com/czg86389-hub/muse2api/pull/27)) — Added support for quota lookup in both Chinese and English, fixed quota reading and admin page error messages

PRs and issues are welcome — let's make this project stronger and easier to use together!

---

## 📄 License

This project is released under the [MIT License](LICENSE).


## Media selection regression tests

Fixes uploaded reference images being mistaken for generation results, and downloads being overwritten by other media on the page.
The tests use an isolated temporary Chromium profile and a locally synthesized DOM; they do not access Muse, do not read any accounts, and do not consume generation quota.
After installing the project dependencies and Chromium, run from the repository root:

```bash
python tests/test_media_selection.py
```

If the browser is not on PATH, set `MUSE2API_CHROMIUM` to the executable path.
Coverage includes: excluding upload previews, de-duplicating results, excluding historical attachments, downloading from the exact source, preserving the video source,
forbidding a global download fallback, and aborting when reading the reference image fails. The final image editing result still needs to be verified against the real API.


## v1.5.2: Long-running image generation behind a reverse proxy / CDN

The synchronous OpenAI image endpoints remain for compatibility, but a CDN or client may cut off long requests before generation finishes.
Production clients should use a short submission + polling instead of extending the HTTP timeout and regenerating:

1. `POST /v1/images/tasks`: the JSON body is the same as `/v1/images/generations`; pass `reference_image` for an image-to-image reference. Adding a stable `Idempotency-Key` request header is recommended.
2. Receive HTTP 202 and an `id`, then call `GET /v1/images/tasks/{id}` every 3 seconds.
3. When `status=completed`, read `url` or `data[0]`; when `status=failed`, display `error`. If a status query fails, retry only the query — do not POST the image generation again.
4. The same idempotency key with the same input returns the original task; the same key with different input returns 409; a full queue returns 429. One process shares one browser and accepts at most 8 unfinished image tasks.

The existing `/v1/images/generations` JSON and `/v1/images/edits` JSON/multipart endpoints also accept `async=true` to switch to the same asynchronous processing. The default synchronous behavior is unchanged.
`timeout` is the budget for queueing and waiting for generation (1–600 seconds); browser initialization and result retrieval have their own timeouts. Clients should keep polling until the task reaches a final state.
Task metadata is persisted; on process restart, image tasks that have not finished are marked as failed and are not silently regenerated.
The image API test in the admin page has also been switched to task polling.

Regression checks (no real generation quota consumed):

```bash
python tests/test_async_images.py
python tests/test_vm_wait.py engine.py --assert
```

Fixed a race where shared-browser exception handling reset other tasks after unlocking; leftover
`Connecting...` / `Still sending` text in the page sidebar is no longer treated as evidence that the current image generation failed within 16 seconds.


## v1.5.3: Abnormal account status and self-hosted instance troubleshooting

Keep-alive requests come from the **deployment server's outbound network**, not from the browser the cookies were exported from. The fact that the author's server works does not prove that another server's network, region, proxy configuration or session for the same account is also fine.

- `/api/session HTTP 401`: upstream rejected authentication. First confirm the account can log in on the official muse.ai website, then re-import the cookies.
- `HTTP 403`: access denied. This may involve account permissions, the server's outbound IP, or regional/access restrictions; it **does not mean the cookie has expired**. Check the full error and the deployment network instead of blindly re-importing over and over.
- `HTTP 429`, `5xx`, timeouts, non-JSON responses, or no `assigned` returned: this keep-alive was not confirmed. It no longer falsely reports success or overwrites the last confirmed account status; the notes show the diagnostics. An account whose previous status was "Unchecked" or "Error" is not forcibly changed to "Available" either.
- Browser page load timeouts are no longer treated directly as authentication failures; an account is only marked as abnormal on an explicit authentication failure.
- "Available" is the most recent confirmed result and does not guarantee the network is reachable right now; "Remaining validity" is a cookie time/estimate, not proof that the server-side session is valid. A successful check no longer extends validity by 48 hours out of thin air; renewal is based on the cookies actually returned.

After updating to v1.5.3 and restarting the service, click "Test" on the account to re-confirm it. Errors written by older versions are not unconditionally reset to normal. Hover over the notes to see the full error. When reporting issues, please include the HTTP status, version and deployment environment, and **never publish cookies, API keys, or admin page links containing the key**.

Bare-metal Git deployment: run `git pull --ff-only` and then restart the corresponding service; Docker Compose: `git pull --ff-only && docker compose up -d --build`. Save any local custom changes first and keep `.env` and `data/`.

Regression test that uses no real accounts and consumes no generation quota:

```bash
python tests/test_session_health.py
```
# MUSE2API 1.6.1

## Release notes (2026-10-13)

- Fixed cross-reading of quota between multiple accounts: quota queries are bound to the browser page of the corresponding account, preventing wrong balances and usage from being shown across accounts.
- When reading quota fails or returns no valid data, the old cache is kept and the read failure is reported explicitly, so invalid results are not treated as live quota.
- Fixed false keep-alive success reports: a wake request being accepted is now distinguished from the VM actually running; wake success is only reported when the re-checked status is RUNNING.
- The admin page shows the quota query time and a staleness warning; account tests report session validity and quota read status separately.
- Added offline regression tests covering per-account quota isolation and VM wake status confirmation.
