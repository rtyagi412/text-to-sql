"""A small client for the SSRS REST API v2.0 (<portal>/api/v2.0): the calls publishing a report and subscribing to it need."""

import base64
from typing import Any
from urllib.parse import quote, unquote

import requests
from requests.auth import HTTPBasicAuth

from app.core.config import get_settings

settings = get_settings()


class SsrsConfigError(RuntimeError):
    """SSRS is not configured (no URL or credentials), so it cannot be called."""


class SsrsApiError(RuntimeError):
    """SSRS answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _odata_key(value: str) -> str:
    """A string as an OData key literal: single quotes doubled, then percent-encoded for the URL."""
    return quote("'" + value.replace("'", "''") + "'", safe="'")


def _error_message(response: requests.Response, request: str) -> str:
    try:
        body = response.json()
        message = body["error"]["message"] if isinstance(body.get("error"), dict) else body.get("message")
    except (ValueError, KeyError, AttributeError):
        message = None
    return f"SSRS returned {response.status_code} for {request}: {message or response.text[:300] or response.reason}"


class SsrsClient:
    def __init__(self) -> None:
        if not settings.ssrs_url:
            raise SsrsConfigError("SSRS_URL is not set; it is required to publish reports and create subscriptions")
        if not (settings.ssrs_username and settings.ssrs_password):
            raise SsrsConfigError("SSRS_USERNAME and SSRS_PASSWORD are required to call the SSRS REST API")
        self._base = settings.ssrs_url.rstrip("/") + "/api/v2.0"
        self._session = requests.Session()
        self._session.verify = settings.ssrs_verify_tls
        if settings.ssrs_auth == "ntlm":
            # Imported here: the library loads a native Windows DLL, which a locked-down machine may refuse, and
            # that must not stop the app (or Basic auth) from working.
            try:
                from requests_ntlm import HttpNtlmAuth
            except ImportError as exc:
                raise SsrsConfigError(f"NTLM auth is unavailable here ({exc}); set SSRS_AUTH=basic or fix the install") from exc
            user = f"{settings.ssrs_domain}\\{settings.ssrs_username}" if settings.ssrs_domain else settings.ssrs_username
            self._session.auth = HttpNtlmAuth(user, settings.ssrs_password)
        else:
            self._session.auth = HTTPBasicAuth(settings.ssrs_username, settings.ssrs_password)

    def __enter__(self) -> "SsrsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._session.close()

    def _request(self, method: str, path: str, *, ok_missing: bool = False, **kwargs: Any) -> dict | None:
        # Named in every error, so a 403 says which call SSRS refused (e.g. "POST /Subscriptions").
        request = f"{method} {unquote(path)}"
        try:
            response = self._session.request(method, self._base + path, timeout=settings.ssrs_timeout, **kwargs)
        except requests.RequestException as exc:
            raise SsrsApiError(f"SSRS could not be reached for {request}: {exc}") from exc
        if ok_missing and response.status_code == 404:
            return None
        if not response.ok:
            raise SsrsApiError(_error_message(response, request), response.status_code)
        return response.json() if response.content else {}

    def _item(self, path: str) -> dict | None:
        return self._request("GET", f"/CatalogItems(Path={_odata_key(path)})", ok_missing=True)

    def find_report(self, path: str) -> dict | None:
        """The report at `path` (e.g. "/Folder/Name"), or None when there is none. Something else at that path
        (a folder, a data source) is an error: uploading over it would fail or replace the wrong thing."""
        item = self._item(path)
        if item is not None and item.get("Type") != "Report":
            raise SsrsApiError(f"'{path}' already exists on the server as a {item.get('Type')}, not a report")
        return item

    def folder_exists(self, path: str) -> bool:
        item = self._item(path)
        return item is not None and item.get("Type") == "Folder"

    def shared_data_source(self, path: str) -> dict:
        """The shared data source at `path`. Missing is a setup problem, so it is reported before anything is uploaded."""
        item = self._item(path)
        if item is None or item.get("Type") != "DataSource":
            raise SsrsConfigError(
                f"there is no shared data source at '{path}' on the SSRS server; create it in the portal "
                "(New > Data Source) or correct SSRS_DATA_SOURCE_PATH"
            )
        return item

    def bind_data_source(self, report_id: str, name: str, shared: dict) -> None:
        """Points the report's data source `name` at the shared data source. Uploading an RDL only records that
        it references one; until this call the report's data source has no path and the report cannot run
        (rsInvalidDataSourceReference). A replaced report loses the binding, so publish does this every time."""
        body = [{"@odata.type": "#Model.DataSource", "Name": name, "IsReference": True, "Path": shared["Path"], "Id": shared["Id"]}]
        self._request("PUT", f"/Reports({report_id})/DataSources", json=body)

    def upload_report(self, path: str, name: str, rdl: bytes, existing_id: str | None, description: str | None) -> str:
        """Creates the report at `path` (its full path, name included), or, given the id of the one already there,
        replaces it (same body, PUT to that id). Returns its id."""
        body = {
            "@odata.type": "#Model.Report",
            "Name": name,
            "Path": path,
            "Content": base64.b64encode(rdl).decode("ascii"),
            "ContentType": "",
            "Hidden": False,
        }
        if description:
            body["Description"] = description
        if existing_id:
            self._request("PUT", f"/CatalogItems({existing_id})", json=body)
            return existing_id
        return self._request("POST", "/CatalogItems", json=body)["Id"]

    def delete_item(self, item_id: str) -> None:
        self._request("DELETE", f"/CatalogItems({item_id})")

    def create_subscription(self, body: dict) -> dict:
        return self._request("POST", "/Subscriptions", json=body)
