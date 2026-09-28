import getpass
import json
import os

import requests


BASE_URL = os.environ.get("OCTANE_BASE_URL", "https://octane.example.com").rstrip("/")
OUTPUT_FILE = "octane_spaces_mapping.json"
PAGE_LIMIT = 100
REQUEST_TIMEOUT = (10, 60)


def credentials_from_environment():
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
        clients = json.loads(clients_json)
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
    for client in clients:
        if not isinstance(client, dict) or any(
            not isinstance(client.get(key), str) or not client[key]
            for key in ("client_id", "client_secret")
        ):
            raise ValueError("Each API client needs a nonempty client_id and client_secret.")
    return clients


def sign_in(payload):
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


def get_all_pages(session, url):
    """Fetch every page of an Octane collection endpoint."""
    separator = "&" if "?" in url else "?"
    offset = 0
    items = []
    while True:
        response = session.get(
            f"{url}{separator}limit={PAGE_LIMIT}&offset={offset}", timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        page = response.json()
        items.extend(page.get("data", []))
        total_count = page.get("meta", {}).get("total_count", len(items))
        offset += PAGE_LIMIT
        if offset >= total_count:
            break
    return items


def collect_shared_spaces(session, shared_spaces_by_id):
    shared_spaces_url = f"{BASE_URL}/api/shared_spaces?fields=id,name"
    for space in get_all_pages(session, shared_spaces_url):
        if space["id"] in shared_spaces_by_id:
            continue
        workspaces_url = f"{BASE_URL}/api/shared_spaces/{space['id']}/workspaces?fields=id,name"
        workspaces = [
            {"workspaceId": ws["id"], "workspaceName": ws["name"]}
            for ws in get_all_pages(session, workspaces_url)
        ]
        shared_spaces_by_id[space["id"]] = {
            "sharedSpaceId": space["id"],
            "sharedSpaceName": space["name"],
            "specific_url": "",
            "workspaces": workspaces,
        }


def main(output_file=OUTPUT_FILE):
    try:
        credentials = credentials_from_environment()
    except ValueError as error:
        print(f"Configuration error: {error}")
        return 1

    shared_spaces_by_id = {}
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
