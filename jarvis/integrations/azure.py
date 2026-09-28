"""Azure: archive reports to Blob Storage and deploy Salts FSM builds to App Service (Kudu zip deploy)."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from datetime import datetime, timezone

import httpx
import msal

from ..config import Settings

log = logging.getLogger(__name__)


class BlobArchive:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = None
        if settings.azure_storage_connection_string:
            from azure.storage.blob import BlobServiceClient

            self._client = BlobServiceClient.from_connection_string(settings.azure_storage_connection_string)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _upload(self, name: str, data: bytes, content_type: str) -> str:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        container = self._client.get_container_client(self.s.azure_storage_container)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        blob = container.upload_blob(name, data, overwrite=True,
                                     content_settings=ContentSettings(content_type=content_type))
        return blob.url

    async def upload(self, name: str, content: str | bytes, content_type: str = "text/markdown") -> str:
        if not self._client:
            raise RuntimeError("Azure Blob Storage is not configured (AZURE_STORAGE_CONNECTION_STRING).")
        stamp = datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S")
        data = content.encode() if isinstance(content, str) else content
        return await asyncio.to_thread(self._upload, f"{stamp}-{name}", data, content_type)


def strip_top_folder(github_zip: bytes) -> bytes:
    """GitHub zipballs wrap everything in '<owner>-<repo>-<sha>/'; App Service wants files at the root."""
    src = zipfile.ZipFile(io.BytesIO(github_zip))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1] or info.is_dir():
                continue
            dst.writestr(parts[1], src.read(info))
    return out.getvalue()


class KuduDeployer:
    """Zip-deploys a build to the FSM App Service through its Kudu (SCM) endpoint."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.s.azure_fsm_scm_url and (
            (self.s.azure_kudu_user and self.s.azure_kudu_password)
            or (self.s.azure_tenant_id and self.s.azure_client_id and self.s.azure_client_secret)))

    async def _auth(self) -> dict[str, str] | httpx.BasicAuth:
        if self.s.azure_kudu_user:
            return httpx.BasicAuth(self.s.azure_kudu_user, self.s.azure_kudu_password)
        app = msal.ConfidentialClientApplication(
            self.s.azure_client_id, authority=f"https://login.microsoftonline.com/{self.s.azure_tenant_id}",
            client_credential=self.s.azure_client_secret)
        tok = await asyncio.to_thread(app.acquire_token_for_client, scopes=["https://management.azure.com/.default"])
        if "access_token" not in tok:
            raise RuntimeError(f"Azure auth failed: {tok.get('error_description', tok)}")
        return {"Authorization": f"Bearer {tok['access_token']}"}

    async def deploy_zip(self, package: bytes, timeout_s: int = 900) -> dict[str, str]:
        base = self.s.azure_fsm_scm_url.rstrip("/")
        auth = await self._auth()
        kw = {"auth": auth} if isinstance(auth, httpx.BasicAuth) else {"headers": auth}
        r = await self.http.post(f"{base}/api/zipdeploy", params={"isAsync": "true"}, content=package,
                                 headers={"Content-Type": "application/zip", **kw.get("headers", {})},
                                 auth=kw.get("auth"), timeout=300)
        r.raise_for_status()
        waited = 0
        while waited < timeout_s:
            await asyncio.sleep(10)
            waited += 10
            s = await self.http.get(f"{base}/api/deployments/latest", timeout=30, **kw)
            if s.status_code == 200:
                info = s.json()
                if info.get("complete"):
                    ok = info.get("status") == 4
                    return {"status": "success" if ok else "failed", "message": info.get("status_text") or "",
                            "id": str(info.get("id", ""))}
        return {"status": "timeout", "message": f"Deployment still running after {timeout_s}s"}
