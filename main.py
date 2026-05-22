"""
TaxteamWork Python Service
==========================
Flask web service for archive extraction and PDF processing.
Deployed on Render.com, called by n8n workflows.

Endpoints:
  GET  /health           - Health check
  POST /extract          - Extract ZIP/RAR archive from Drive, upload files back
  POST /merge-pdfs       - Merge multiple PDFs with page labels

Authentication: Uses Google OAuth access token passed in request body
                (token comes from n8n credentials / claude account)
"""

import os
import io
import zipfile
import tempfile
import logging
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify
from pypdf import PdfReader, PdfWriter

# rarfile requires 'unrar' binary (installed via Dockerfile)
import rarfile

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("taxteamwork")

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
MAX_FILE_SIZE_MB = 200  # safety limit


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------
def drive_download(file_id: str, access_token: str) -> bytes:
    """Download file content from Google Drive."""
    url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, stream=True, timeout=120)
    r.raise_for_status()
    return r.content


def drive_get_metadata(file_id: str, access_token: str) -> Dict[str, Any]:
    """Get file metadata (name, mimeType, size)."""
    url = f"{DRIVE_API_BASE}/files/{file_id}?fields=id,name,mimeType,size"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def drive_upload(
    filename: str,
    content: bytes,
    folder_id: str,
    access_token: str,
    mime_type: str = "application/octet-stream",
) -> Dict[str, Any]:
    """Upload a file to a Drive folder using multipart upload."""
    url = f"{DRIVE_UPLOAD_BASE}/files?uploadType=multipart&fields=id,name,webViewLink,size,mimeType"
    headers = {"Authorization": f"Bearer {access_token}"}

    metadata = {"name": filename, "parents": [folder_id]}

    files = {
        "metadata": ("metadata", str(metadata).replace("'", '"'), "application/json"),
        "file": (filename, content, mime_type),
    }
    # requests multipart fallback (safer): use explicit boundary
    import json as _json
    boundary = "tw_boundary_8aXyZ91"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{_json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers["Content-Type"] = f"multipart/related; boundary={boundary}"
    r = requests.post(url, headers=headers, data=body, timeout=300)
    if r.status_code >= 400:
        log.error("Drive upload failed: %s %s", r.status_code, r.text[:500])
        r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------
def extract_archive(content: bytes, original_name: str) -> List[Dict[str, Any]]:
    """
    Extract archive (ZIP or RAR) and return list of files.
    Each item: { 'name': str, 'content': bytes, 'mime': str }
    """
    files_out = []
    ext = original_name.lower().rsplit(".", 1)[-1] if "." in original_name else ""

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, original_name)
        with open(archive_path, "wb") as f:
            f.write(content)

        if ext == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # decode filename safely (handle CP437, UTF-8, TIS-620)
                    name = _decode_zip_name(info)
                    with zf.open(info) as fp:
                        files_out.append({
                            "name": _safe_name(name),
                            "content": fp.read(),
                            "mime": _guess_mime(name),
                        })
        elif ext in ("rar",):
            with rarfile.RarFile(archive_path) as rf:
                for info in rf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    with rf.open(info) as fp:
                        files_out.append({
                            "name": _safe_name(name),
                            "content": fp.read(),
                            "mime": _guess_mime(name),
                        })
        else:
            raise ValueError(f"Unsupported archive extension: {ext}")

    return files_out


def _decode_zip_name(info) -> str:
    """ZipInfo filenames can be in CP437/UTF-8/TIS-620 — try to decode safely."""
    try:
        if info.flag_bits & 0x800:
            return info.filename  # already UTF-8
        raw = info.filename.encode("cp437", errors="replace")
        for enc in ("utf-8", "tis-620", "cp874"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return info.filename


def _safe_name(name: str) -> str:
    """Flatten path and sanitize filename."""
    name = name.replace("\\", "/").split("/")[-1]
    bad = '<>:"|?*\x00'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip() or "unnamed_file"


def _guess_mime(name: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    mapping = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "txt": "text/plain",
        "csv": "text/csv",
        "zip": "application/zip",
        "rar": "application/vnd.rar",
    }
    return mapping.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "taxteamwork-python-service"})


def _get_access_token(data: Dict[str, Any]) -> str | None:
    """Read access token from Authorization header (Bearer) or body."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return data.get("accessToken")


@app.post("/extract")
def extract_endpoint():
    """
    Request body (JSON):
        {
          "fileId":      "<Drive file ID of the archive>",
          "folderId":    "<Drive folder ID to upload extracted files into>",
          "accessToken": "<optional if Authorization: Bearer header provided>"
        }

    Auth: prefer Authorization: Bearer <token> header (sent by n8n
    Predefined Credential Type). Falls back to body.accessToken.

    Response:
        {
          "ok": true,
          "archiveName": "...",
          "extracted": [
            { "id": "...", "name": "...", "size": ..., "webViewLink": "..." },
            ...
          ]
        }
    """
    data = request.get_json(force=True, silent=True) or {}
    file_id = data.get("fileId")
    folder_id = data.get("folderId")
    access_token = _get_access_token(data)

    if not (file_id and folder_id and access_token):
        return jsonify({"ok": False, "error": "fileId, folderId, and access token (header or body) required"}), 400

    try:
        log.info("Extract request fileId=%s folderId=%s", file_id, folder_id)
        meta = drive_get_metadata(file_id, access_token)
        archive_name = meta.get("name", "archive")
        size_mb = int(meta.get("size", 0)) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            return jsonify({"ok": False, "error": f"File too large ({size_mb:.1f} MB)"}), 413

        content = drive_download(file_id, access_token)
        files = extract_archive(content, archive_name)
        log.info("Extracted %d files from %s", len(files), archive_name)

        results = []
        for f in files:
            uploaded = drive_upload(
                filename=f["name"],
                content=f["content"],
                folder_id=folder_id,
                access_token=access_token,
                mime_type=f["mime"],
            )
            results.append({
                "id": uploaded.get("id"),
                "name": uploaded.get("name"),
                "size": uploaded.get("size"),
                "webViewLink": uploaded.get("webViewLink"),
            })

        return jsonify({
            "ok": True,
            "archiveName": archive_name,
            "extracted": results,
            "count": len(results),
        })

    except Exception as e:
        log.exception("Extract failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/merge-pdfs")
def merge_pdfs_endpoint():
    """
    Merge multiple PDFs from Drive into a single PDF with page labels.

    Request body (JSON):
        {
          "fileIds":     ["id1", "id2", ...],     # PDFs to merge in order
          "folderId":    "<Drive folder ID>",
          "outputName":  "merged.pdf",
          "accessToken": "<OAuth token>"
        }

    Response:
        { "ok": true, "merged": {...drive metadata...} }
    """
    data = request.get_json(force=True, silent=True) or {}
    file_ids: List[str] = data.get("fileIds") or []
    folder_id = data.get("folderId")
    output_name = data.get("outputName", "merged.pdf")
    access_token = _get_access_token(data)

    if not (file_ids and folder_id and access_token):
        return jsonify({"ok": False, "error": "fileIds, folderId, and access token (header or body) required"}), 400

    try:
        writer = PdfWriter()
        for fid in file_ids:
            blob = drive_download(fid, access_token)
            reader = PdfReader(io.BytesIO(blob))
            meta = drive_get_metadata(fid, access_token)
            label_base = meta.get("name", fid).rsplit(".", 1)[0]
            for i, page in enumerate(reader.pages):
                writer.add_page(page)
                # add bookmark per source PDF (first page only)
                if i == 0:
                    try:
                        writer.add_outline_item(label_base, len(writer.pages) - 1)
                    except Exception:
                        pass

        out_buf = io.BytesIO()
        writer.write(out_buf)
        out_bytes = out_buf.getvalue()

        uploaded = drive_upload(
            filename=output_name,
            content=out_bytes,
            folder_id=folder_id,
            access_token=access_token,
            mime_type="application/pdf",
        )
        return jsonify({"ok": True, "merged": uploaded})

    except Exception as e:
        log.exception("Merge failed")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
