# TaxteamWork Python Service

Internal Flask service for the TaxteamWork email-to-Drive pipeline. Deployed on
Render.com, called by n8n workflows.

## Endpoints

- `GET  /health` — liveness probe (used by Render)
- `POST /extract` — extract ZIP/RAR archive from Drive, upload contents back
- `POST /merge-pdfs` — merge multiple PDFs (with bookmarks) and upload result

All POST endpoints accept a Google OAuth access token in the body and act on
behalf of that account (the `taxteamworkclaude@gmail.com` service identity in
production).

## Local dev

```bash
pip install -r requirements.txt
python main.py
# -> http://localhost:8080/health
```

## Deploy on Render

1. Push this repo to GitHub.
2. In Render → **New +** → **Web Service** → connect the repo.
3. Render detects `render.yaml` (Docker runtime).
4. Plan: **Free**. Region: **Singapore** (closest to TH).
5. Wait for build. Health check path: `/health`.
6. Note the public URL — paste into the n8n "Extract Archive" HTTP node.

## Architecture notes

- `rarfile` requires the `unrar` binary, installed via `Dockerfile`.
- File size capped at 200 MB to stay within Render free-tier RAM.
- The service streams downloads from Drive but holds extracted bytes in memory
  before uploading. For typical accounting attachments (< 100 MB) this is fine.

## Request example — /extract

```http
POST /extract
Content-Type: application/json

{
  "fileId": "1sdhlwXOYZ4zbqhbYUCYxLormGlk0bLkG",
  "folderId": "16qjG4_ntaoVruRCr0vVfnS1-TJVZFSqn",
  "accessToken": "ya29.a0AfH..."
}
```

Response:

```json
{
  "ok": true,
  "archiveName": "doc kkk 4-26.zip",
  "count": 12,
  "extracted": [
    { "id": "...", "name": "PV001.pdf", "size": "234567", "webViewLink": "https://..." }
  ]
}
```
