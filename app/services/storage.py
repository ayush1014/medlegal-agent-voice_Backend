"""GCS storage client.

The service-account credentials come from `GOOGLE_APPLICATION_CREDENTIALS_JSON`,
which may be raw JSON or base64-encoded JSON — both are handled.
"""

from __future__ import annotations

import base64
import binascii
import json
from functools import lru_cache
from typing import Any

from app.config import settings


def _service_account_info() -> dict[str, Any]:
    raw = (settings.google_application_credentials_json or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_JSON is not set")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(raw))
        except (binascii.Error, json.JSONDecodeError) as e:
            raise RuntimeError("Invalid GCS credentials (not JSON or base64 JSON)") from e


@lru_cache
def get_gcs_client():
    from google.cloud import storage
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(_service_account_info())
    return storage.Client(project=settings.google_cloud_project, credentials=creds)


def get_bucket():
    return get_gcs_client().bucket(settings.gcs_bucket_name)
