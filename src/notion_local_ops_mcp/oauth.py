from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


DEFAULT_SCOPE = "local-ops"


@dataclass(frozen=True)
class OAuthRuntimeConfig:
    auth_mode: str
    auth_token: str
    public_base_url: str
    state_dir: Path
    oauth_login_token: str
    oauth_scopes: tuple[str, ...]
    oauth_token_ttl_seconds: int

    @property
    def normalized_auth_mode(self) -> str:
        mode = self.auth_mode.strip().lower()
        if mode:
            return mode
        return "shared_token" if self.auth_token else "none"

    @property
    def login_token(self) -> str:
        return self.oauth_login_token or self.auth_token

    @property
    def scopes(self) -> tuple[str, ...]:
        return self.oauth_scopes or (DEFAULT_SCOPE,)


class OAuthManager:
    def __init__(self, config: OAuthRuntimeConfig, *, mcp_path: str) -> None:
        self.config = config
        self.mcp_path = mcp_path
        self.store_path = config.state_dir / "oauth.json"

    def metadata_base_url(self, fallback_base_url: str) -> str:
        configured = self.config.public_base_url.strip()
        return (configured or fallback_base_url).rstrip("/")

    def resource_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}{self.mcp_path}"

    def resource_metadata_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/.well-known/oauth-protected-resource{self.mcp_path}"

    def scope_string(self) -> str:
        return " ".join(self.config.scopes)

    def authorization_server_metadata(self, base_url: str) -> dict[str, Any]:
        return {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": list(self.config.scopes),
        }

    def protected_resource_metadata(self, base_url: str) -> dict[str, Any]:
        return {
            "resource": self.resource_url(base_url),
            "authorization_servers": [base_url],
            "scopes_supported": list(self.config.scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "notion-local-ops-mcp",
        }

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ValueError("redirect_uris must be a non-empty list")
        if not all(isinstance(uri, str) and _is_allowed_redirect_uri(uri) for uri in redirect_uris):
            raise ValueError("redirect_uris must use https or localhost")

        client_id = "mcp_client_" + secrets.token_urlsafe(24)
        now = int(time.time())
        store = self._read_store()
        store["clients"][client_id] = {
            "client_id": client_id,
            "client_name": str(payload.get("client_name") or "ChatGPT"),
            "redirect_uris": redirect_uris,
            "created_at": now,
        }
        self._write_store(store)
        return {
            "client_id": client_id,
            "client_name": store["clients"][client_id]["client_name"],
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_id_issued_at": now,
        }

    def authorize(self, payload: dict[str, str], *, base_url: str) -> str:
        if not self.config.login_token:
            raise PermissionError("oauth login token is not configured")
        if not hmac.compare_digest(payload.get("login_token", ""), self.config.login_token):
            raise PermissionError("invalid login token")

        client_id = payload.get("client_id", "")
        redirect_uri = payload.get("redirect_uri", "")
        code_challenge = payload.get("code_challenge", "")
        if payload.get("response_type") != "code":
            raise ValueError("response_type must be code")
        if payload.get("code_challenge_method") != "S256" or not code_challenge:
            raise ValueError("PKCE S256 is required")
        if payload.get("resource") != self.resource_url(base_url):
            raise ValueError("resource does not match this MCP server")

        store = self._read_store()
        client = store["clients"].get(client_id)
        if not client:
            raise ValueError("unknown client_id")
        if redirect_uri not in client.get("redirect_uris", []):
            raise ValueError("redirect_uri is not registered")

        requested_scopes = _scope_set(payload.get("scope", ""))
        if requested_scopes and not requested_scopes.issubset(set(self.config.scopes)):
            raise ValueError("requested scope is not supported")

        code = "mcp_code_" + secrets.token_urlsafe(32)
        store["codes"][code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": payload.get("scope") or self.scope_string(),
            "resource": payload.get("resource"),
            "expires_at": int(time.time()) + 300,
        }
        self._write_store(store)

        query = {"code": code}
        if payload.get("state"):
            query["state"] = payload["state"]
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(query)}"

    def exchange_code(self, payload: dict[str, str], *, base_url: str) -> dict[str, Any]:
        if payload.get("grant_type") != "authorization_code":
            raise ValueError("grant_type must be authorization_code")
        if payload.get("resource") != self.resource_url(base_url):
            raise ValueError("resource does not match this MCP server")

        code = payload.get("code", "")
        store = self._read_store()
        code_record = store["codes"].pop(code, None)
        if not code_record:
            self._write_store(store)
            raise ValueError("invalid authorization code")
        if int(code_record.get("expires_at", 0)) < int(time.time()):
            self._write_store(store)
            raise ValueError("authorization code expired")
        if payload.get("client_id") != code_record.get("client_id"):
            self._write_store(store)
            raise ValueError("client_id mismatch")
        if payload.get("redirect_uri") != code_record.get("redirect_uri"):
            self._write_store(store)
            raise ValueError("redirect_uri mismatch")

        verifier = payload.get("code_verifier", "")
        expected_challenge = _pkce_s256(verifier)
        if not hmac.compare_digest(expected_challenge, str(code_record.get("code_challenge", ""))):
            self._write_store(store)
            raise ValueError("invalid code_verifier")

        access_token = "mcp_at_" + secrets.token_urlsafe(40)
        expires_in = max(int(self.config.oauth_token_ttl_seconds), 60)
        store["tokens"][access_token] = {
            "client_id": payload.get("client_id"),
            "scope": code_record.get("scope") or self.scope_string(),
            "resource": code_record.get("resource"),
            "expires_at": int(time.time()) + expires_in,
        }
        self._write_store(store)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": store["tokens"][access_token]["scope"],
        }

    def verify_access_token(self, token: str, *, base_url: str) -> bool:
        if not token:
            return False
        store = self._read_store()
        record = store["tokens"].get(token)
        if not record:
            return False
        if int(record.get("expires_at", 0)) < int(time.time()):
            store["tokens"].pop(token, None)
            self._write_store(store)
            return False
        if record.get("resource") != self.resource_url(base_url):
            return False
        return _scope_set(str(record.get("scope", ""))).issuperset(set(self.config.scopes))

    def authorize_page(self, payload: dict[str, str]) -> str:
        hidden_inputs = "\n".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
            for key, value in payload.items()
        )
        return f"""
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Authorize notion-local-ops-mcp</title></head>
  <body>
    <main style="font-family: system-ui; max-width: 480px; margin: 48px auto;">
      <h1>Authorize local MCP access</h1>
      <p>Enter your local ops token to let this ChatGPT session call the MCP server.</p>
      <form method="post" action="/oauth/authorize">
        {hidden_inputs}
        <label>Token <input name="login_token" type="password" autofocus></label>
        <button type="submit">Authorize</button>
      </form>
    </main>
  </body>
</html>
""".strip()

    def _read_store(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"clients": {}, "codes": {}, "tokens": {}}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"clients": {}, "codes": {}, "tokens": {}}
        if not isinstance(data, dict):
            return {"clients": {}, "codes": {}, "tokens": {}}
        return {
            "clients": data.get("clients") if isinstance(data.get("clients"), dict) else {},
            "codes": data.get("codes") if isinstance(data.get("codes"), dict) else {},
            "tokens": data.get("tokens") if isinstance(data.get("tokens"), dict) else {},
        }

    def _write_store(self, store: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.store_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.store_path)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _scope_set(scope: str) -> set[str]:
    return {item for item in scope.split() if item}


def _is_allowed_redirect_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
