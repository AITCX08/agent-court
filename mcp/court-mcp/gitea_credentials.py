from __future__ import annotations

import netrc
import os
import subprocess
from dataclasses import dataclass


class CredentialNotFoundError(RuntimeError):
    """Raised when no usable Gitea credential source is available."""


@dataclass(frozen=True)
class Credential:
    username: str
    token: str
    source: str


class KeychainCredentialProvider:
    def __init__(self, host: str = "git.k2lab.ai") -> None:
        self.host = host

    def get_username(self) -> str:
        credential = self._resolve()
        return credential.username or "oauth2"

    def get_token(self) -> str:
        return self._resolve().token

    def _resolve(self) -> Credential:
        credential = self._from_keychain()
        if credential is not None:
            return credential

        env_token = os.environ.get("K2LAB_GIT_TOKEN", "").strip()
        if env_token:
            return Credential(
                username=os.environ.get("K2LAB_GIT_USER", "oauth2"),
                token=env_token,
                source="env",
            )

        credential = self._from_netrc()
        if credential is not None:
            return credential

        raise CredentialNotFoundError(
            f"no credential found for {self.host}; tried keychain, K2LAB_GIT_TOKEN, ~/.netrc"
        )

    def _from_keychain(self) -> Credential | None:
        payload = f"protocol=https\nhost={self.host}\n\n"
        try:
            proc = subprocess.run(
                ["git", "credential-osxkeychain", "get"],
                input=payload,
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return None

        if proc.returncode != 0 or not proc.stdout.strip():
            return None

        fields: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()

        token = fields.get("password", "").strip()
        if not token:
            return None
        return Credential(
            username=fields.get("username", "oauth2"),
            token=token,
            source="keychain",
        )

    def _from_netrc(self) -> Credential | None:
        try:
            auth = netrc.netrc().authenticators(self.host)
        except (FileNotFoundError, netrc.NetrcParseError):
            return None

        if auth is None:
            return None
        login, _, password = auth
        if not password:
            return None
        return Credential(username=login or "oauth2", token=password, source="netrc")
