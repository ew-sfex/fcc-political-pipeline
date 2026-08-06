"""Upload filing PDFs to Google Drive and tag them with structured metadata
via Drive file `properties` (works on any Drive with a shared folder -
no Workspace Labels admin setup required).
"""
from __future__ import annotations

import io
import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from . import config

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _load_credentials():
    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if os.path.exists(raw):
        return service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)
    # Otherwise treat it as raw JSON (e.g. from a GitHub Actions secret)
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


class DriveClient:
    def __init__(self):
        creds = _load_credentials()
        self.service = build("drive", "v3", credentials=creds)

    def get_or_create_subfolder(self, name: str, parent_id: str) -> str:
        safe_name = name.replace("'", "\\'")
        query = (
            f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        resp = self.service.files().list(q=query, fields="files(id, name)").execute()
        files = resp.get("files", [])
        if files:
            return files[0]["id"]

        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=metadata, fields="id").execute()
        return folder["id"]

    def upload_pdf(
        self,
        file_bytes: bytes,
        filename: str,
        parent_folder_id: str,
        properties: dict[str, str],
    ) -> tuple[str, str]:
        """Uploads a PDF and tags it with `properties` (arbitrary key-value
        metadata, filterable via the Drive API `properties has {key='...'}`
        query syntax, and visible in the Drive UI's file details panel).

        Returns (drive_file_id, web_view_link).
        """
        # Drive properties values must be strings; None -> omit the key.
        clean_props = {k: str(v) for k, v in properties.items() if v is not None}

        metadata = {
            "name": filename,
            "parents": [parent_folder_id],
            "properties": clean_props,
        }
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/pdf", resumable=True)
        created = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
        ).execute()
        return created["id"], created.get("webViewLink", "")
