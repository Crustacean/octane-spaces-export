import getpass
import json
import os
from typing import TypedDict, cast

import requests


BASE_URL = os.environ.get("OCTANE_BASE_URL", "https://octane.example.com").rstrip("/")
OUTPUT_FILE = "octane_spaces_mapping.json"
PAGE_LIMIT = 100
REQUEST_TIMEOUT = (10, 60)


class OctaneEntity(TypedDict):
    id: str | int
    name: str


class PageMetadata(TypedDict, total=False):
    total_count: int


class CollectionPage(TypedDict, total=False):
    data: list[OctaneEntity]
    meta: PageMetadata


class WorkspaceExport(TypedDict):
    workspaceId: str | int
    workspaceName: str


class SharedSpaceExport(TypedDict):
    sharedSpaceId: str | int
    sharedSpaceName: str
    specific_url: str
    workspaces: list[WorkspaceExport]


def credentials_from_environment() -> list[dict[str, str]]:
    # Site-admin credentials take precedence over workspace-scoped API clients.
    admin_user = os.environ.get("OCTANE_SITE_ADMIN_USER", "").strip()
    if admin_user:
        password = os.environ.get("OCTANE_SITE_ADMIN_PASSWORD") or getpass.getpass(
            "Octane site-admin password: "
        )
        if not password:
            raise ValueError("The Octane site-admin password cannot be empty.")
        return [{"user": admin_user, "password": password}]

    # OCTANE_API_CLIENTS accepts a JSON array for exports spanning multiple clients.
    clients_json = os.environ.get("OCTANE_API_CLIENTS")
    if clients_json:
        clients: object = json.loads(clients_json)
    else:
        client_id = os.environ.get("OCTANE_CLIENT_ID", "").strip()
        if not client_id:
            raise ValueError("Set OCTANE_SITE_ADMIN_USER, OCTANE_CLIENT_ID or OCTANE_API_CLIENTS.")
        client_secret = os.environ.get("OCTANE_CLIENT_SECRET") or getpass.getpass(
            "Octane API client secret: "
        )
        clients = [{"client_id": client_id, "client_secret": client_secret}]

    if not isinstance(clients, list) or not clients:
        raise ValueError("OCTANE_API_CLIENTS must be a nonempty JSON array.")
    for client in cast(list[object], clients):
        if not isinstance(client, dict):
            raise ValueError("Each API client needs a nonempty client_id and client_secret.")
        client = cast(dict[str, object], client)
        if any(
            not isinstance(client.get(key), str) or not client[key]
            for key in ("client_id", "client_secret")
        ):
            raise ValueError("Each API client needs a nonempty client_id and client_secret.")
    return cast(list[dict[str, str]], clients)


def sign_in(payload: dict[str, str]) -> requests.Session | None:
    session = requests.Session()
    auth_url = f"{BASE_URL}/authentication/sign_in"
    try:
        response = session.post(auth_url, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        session.close()
        raise
    if response.status_code != 200:
        print(f"Authentication failed. Status: {response.status_code}")
        session.close()
        return None
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return session


def get_all_pages(session: requests.Session, url: str) -> list[OctaneEntity]:
    """Fetch every page of an Octane collection endpoint."""
    separator = "&" if "?" in url else "?"
    offset = 0
    items: list[OctaneEntity] = []
    while True:
        response = session.get(
            f"{url}{separator}limit={PAGE_LIMIT}&offset={offset}", timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        page = cast(CollectionPage, response.json())
        items.extend(page.get("data", []))
        total_count = page.get("meta", {}).get("total_count", len(items))
        offset += PAGE_LIMIT
        if offset >= total_count:
            break
    return items


def collect_shared_spaces(
    session: requests.Session,
    shared_spaces_by_id: dict[str | int, SharedSpaceExport],
) -> None:
    shared_spaces_url = f"{BASE_URL}/api/shared_spaces?fields=id,name"
    for space in get_all_pages(session, shared_spaces_url):
        if space["id"] in shared_spaces_by_id:
            continue
        workspaces_url = f"{BASE_URL}/api/shared_spaces/{space['id']}/workspaces?fields=id,name"
        workspaces: list[WorkspaceExport] = [
            {"workspaceId": ws["id"], "workspaceName": ws["name"]}
            for ws in get_all_pages(session, workspaces_url)
        ]
        shared_spaces_by_id[space["id"]] = {
            "sharedSpaceId": space["id"],
            "sharedSpaceName": space["name"],
            "specific_url": "",
            "workspaces": workspaces,
        }


def main(output_file: str | os.PathLike[str] = OUTPUT_FILE) -> int:
    try:
        credentials = credentials_from_environment()
    except ValueError as error:
        print(f"Configuration error: {error}")
        return 1

    shared_spaces_by_id: dict[str | int, SharedSpaceExport] = {}
    for payload in credentials:
        session = sign_in(payload)
        if session is not None:
            try:
                collect_shared_spaces(session, shared_spaces_by_id)
            finally:
                session.close()

    if not shared_spaces_by_id:
        print("No shared spaces retrieved; aborting without touching the output file.")
        return 1

    json_output = {
        "shared_url": BASE_URL,
        "shared_spaces": list(shared_spaces_by_id.values()),
    }
    with open(output_file, mode="w", encoding="utf-8") as file:
        json.dump(json_output, file, indent=4)
        file.write("\n")
    print(f"Export successful. File saved to: {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
