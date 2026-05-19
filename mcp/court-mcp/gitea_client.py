from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from gitea_credentials import CredentialNotFoundError, KeychainCredentialProvider


class GiteaClientError(RuntimeError):
    pass


class GiteaTransportError(GiteaClientError):
    pass


class GiteaAuthError(GiteaClientError):
    pass


class GiteaPermissionError(GiteaClientError):
    pass


class GiteaNotFoundError(GiteaClientError):
    pass


class GiteaValidationError(GiteaClientError):
    pass


class GiteaRateLimitError(GiteaClientError):
    pass


class GiteaServerError(GiteaClientError):
    pass


class GiteaClient:
    def __init__(
        self,
        base_url: str = "https://git.k2lab.ai/api/v1",
        provider: KeychainCredentialProvider | None = None,
        timeout: float = 10.0,
        per_page: int = 50,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider = provider or KeychainCredentialProvider()
        self.timeout = timeout
        self.per_page = per_page

    def whoami(self) -> dict[str, Any]:
        return self._request_json("GET", "/user")

    def list_assigned_issues(self, state: str = "open", since: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "assigned": "true",
            "state": state,
            "type": "issues",
        }
        if since:
            params["since"] = since

        issues: list[dict[str, Any]] = []
        for page in self._paginate("/repos/issues/search", params=params):
            for item in page:
                if item.get("pull_request") is not None:
                    continue
                issues.append(item)
        return issues

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        owner, name = self._split_repo(repo)
        return self._request_json("GET", f"/repos/{owner}/{name}/issues/{number}")

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        owner, name = self._split_repo(repo)
        comments: list[dict[str, Any]] = []
        for page in self._paginate(f"/repos/{owner}/{name}/issues/{number}/comments"):
            comments.extend(page)
        return comments

    def comment_on_issue(self, repo: str, number: int, body: str) -> dict[str, Any]:
        owner, name = self._split_repo(repo)
        return self._request_json(
            "POST",
            f"/repos/{owner}/{name}/issues/{number}/comments",
            json_body={"body": body},
        )

    def transition_issue(self, repo: str, number: int, state: str) -> dict[str, Any]:
        if state not in {"open", "closed"}:
            raise ValueError("state must be 'open' or 'closed'")
        owner, name = self._split_repo(repo)
        return self._request_json(
            "PATCH",
            f"/repos/{owner}/{name}/issues/{number}",
            json_body={"state": state},
        )

    def _paginate(self, path: str, params: dict[str, str] | None = None) -> list[list[dict[str, Any]]]:
        page = 1
        pages: list[list[dict[str, Any]]] = []
        base_params = dict(params or {})
        while True:
            page_params = dict(base_params)
            page_params["page"] = str(page)
            page_params["limit"] = str(self.per_page)
            payload = self._request_json("GET", path, params=page_params)
            rows = payload if isinstance(payload, list) else payload.get("data", [])
            if not isinstance(rows, list):
                raise GiteaValidationError(f"unexpected pagination payload for {path!r}")
            pages.append(rows)
            if len(rows) < self.per_page:
                break
            page += 1
        return pages

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = self.provider.get_token()
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        payload_bytes = None
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            payload_bytes = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=payload_bytes,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return payload
        except urllib.error.HTTPError as exc:
            payload = self._decode_error_body(exc)
            self._raise_http_error(exc.code, payload)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise GiteaTransportError(str(exc)) from exc

    def _decode_error_body(self, exc: urllib.error.HTTPError) -> Any:
        raw = exc.read().decode("utf-8", errors="replace")
        if not raw:
            return {"detail": f"http {exc.code}"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def _raise_http_error(self, status: int, payload: Any) -> None:
        detail = payload if isinstance(payload, dict) else {"detail": str(payload)}
        if status == 401:
            raise GiteaAuthError(str(detail))
        if status == 403:
            raise GiteaPermissionError(str(detail))
        if status == 404:
            raise GiteaNotFoundError(str(detail))
        if status == 422:
            raise GiteaValidationError(str(detail))
        if status == 429:
            raise GiteaRateLimitError(str(detail))
        if status >= 500:
            raise GiteaServerError(str(detail))
        raise GiteaClientError(str(detail))

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        if "/" not in repo:
            raise ValueError(f"repo must be owner/name, got {repo!r}")
        owner, name = repo.split("/", 1)
        return owner, name


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gitea_client")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami")

    p = sub.add_parser("list_assigned_issues")
    p.add_argument("--state", default="open")
    p.add_argument("--since")

    p = sub.add_parser("get_issue")
    p.add_argument("--repo", required=True)
    p.add_argument("--num", required=True, type=int)

    p = sub.add_parser("comment")
    p.add_argument("--repo", required=True)
    p.add_argument("--num", required=True, type=int)
    p.add_argument("--body", required=True)

    p = sub.add_parser("transition")
    p.add_argument("--repo", required=True)
    p.add_argument("--num", required=True, type=int)
    p.add_argument("--state", required=True, choices=["open", "closed"])
    return parser


def main() -> int:
    parser = _make_parser()
    args = parser.parse_args()
    client = GiteaClient()
    try:
        if args.command == "whoami":
            result = client.whoami()
        elif args.command == "list_assigned_issues":
            result = client.list_assigned_issues(state=args.state, since=args.since)
        elif args.command == "get_issue":
            result = client.get_issue(args.repo, args.num)
        elif args.command == "comment":
            result = client.comment_on_issue(args.repo, args.num, args.body)
        elif args.command == "transition":
            result = client.transition_issue(args.repo, args.num, args.state)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
    except CredentialNotFoundError as exc:
        print(json.dumps({"error": "credential_not_found", "detail": str(exc)}))
        return 2
    except GiteaClientError as exc:
        print(json.dumps({"error": exc.__class__.__name__, "detail": str(exc)}))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
