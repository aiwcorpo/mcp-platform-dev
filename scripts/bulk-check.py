#!/usr/bin/env python3
"""What a user with mcp-confluence-read sees at a gateway: the list of tools, page by page (tools/list, 100 per page, nextCursor).
Used by scripts/bulk.sh (main stack) and scripts/e2e-prod-check.py (profile e2e-prod). Runs as a throw-away Keycloak user that is
deleted afterwards; the shared test users are never used. Exit code 0 when the list is what --expect says.

  bulk-check.py --url http://localhost:18000/mcp --expect bulk|real
    bulk: the 3 real tools plus 250 tools confluence__read_item_001 .. 250 (253 for a reader, in three pages: 100, 100, 53)
    real: the tools of the real mcp-confluence for a reader (confluence__get_page, get_space, search_pages), one page
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcpdev  # noqa: E402

GROUPS = ["mcp-client-claude-code", "mcp-confluence-read"]
REAL_READER_TOOLS = {"confluence__get_page", "confluence__get_space", "confluence__search_pages"}


def post(url, token, method, params=None, session=None, ident=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if ident is not None:
        body["id"] = ident
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Mcp-Session-Id"), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, None, error.read().decode()


def list_pages(url, token):
    status, session, text = post(url, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                           "clientInfo": {"name": "bulk-check", "version": "1"}})
    if status != 200:
        raise mcpdev.Fail(f"initialize answered {status}: {text[:200]}")
    post(url, token, "notifications/initialized", session=session, ident=None)
    pages, cursor = [], None
    for _ in range(10):
        status, _, text = post(url, token, "tools/list", {"cursor": cursor} if cursor else {}, session=session, ident=2)
        if status != 200:
            raise mcpdev.Fail(f"tools/list answered {status}: {text[:200]}")
        result = json.loads(text)["result"]
        pages.append([tool["name"] for tool in result["tools"]])
        cursor = result.get("nextCursor")
        if not cursor:
            return pages
    raise mcpdev.Fail("more than 10 pages")


def judge(pages, expect):
    names = [name for page in pages for name in page]
    confluence = [name for name in names if name.startswith("confluence__")]
    if expect == "bulk":
        wanted = sorted(REAL_READER_TOOLS | {f"confluence__read_item_{n:03d}" for n in range(1, 251)})
        problems = []
        if sorted(confluence) != wanted:
            problems.append(f"{len(confluence)} confluence tools, not the 3 real and 250 of the stand-in")
        if [len(page) for page in pages] != [100, 100, 53]:
            problems.append(f"pages of {[len(page) for page in pages]}, expected [100, 100, 53]")
        return problems
    problems = []
    if set(confluence) != REAL_READER_TOOLS:
        problems.append(f"confluence tools {sorted(confluence)}, expected {sorted(REAL_READER_TOOLS)}")
    if len(pages) != 1:
        problems.append(f"{len(pages)} pages, expected 1")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="MCP endpoint of the gateway, e.g. http://localhost:18000/mcp")
    parser.add_argument("--expect", choices=["bulk", "real"], required=True)
    parser.add_argument("--wait", type=int, default=0, help="seconds to retry while the gateway still shows the old list (its definition cache is up to 60 s)")
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait
    with mcpdev.TempUser(GROUPS, otp=False) as user:
        while True:
            token = user.token()
            pages = list_pages(args.url, token)
            problems = judge(pages, args.expect)
            if not problems or time.monotonic() >= deadline:
                break
            time.sleep(5)
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print(f"PASS {sum(len(p) for p in pages)} tools in {len(pages)} page(s) as expected ({args.expect})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except mcpdev.Fail as failure:
        print(f"FAIL {failure}")
        sys.exit(1)
