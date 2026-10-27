#!/usr/bin/env python3
# Generates mocks/jira (WireMock mappings and issue bodies). Edit this file, not the JSON:
#   python3 scripts/gen-jira-mock.py mocks/jira
import json, os, re, sys

out = sys.argv[1]
maps = os.path.join(out, "mappings")
files = os.path.join(out, "__files")
os.makedirs(maps, exist_ok=True)
os.makedirs(files, exist_ok=True)
BASE = "http://jira.internal:8080"
JSON = {"Content-Type": "application/json;charset=UTF-8"}

def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=True)
        f.write("\n")

def jira_user(name, display, email):
    return {
        "self": f"{BASE}/rest/api/2/user?username={name}",
        "key": "JIRAUSER-" + name,
        "name": name,
        "emailAddress": email,
        "displayName": display,
        "active": True,
        "deleted": False,
        "timeZone": "Europe/Warsaw",
        "locale": "pl_PL",
    }

# PAT -> backend user. alice/bob/carol/mallory match Keycloak preferred_username (C4.1 default mapping).
# test-only-jira-pat-0000000000000000 is the value returned by the contract stub of resolve (C4 mock),
# backendUser jkowalski as in the contract examples.
pats = {
    "alice-pat": jira_user("alice", "Alice Tester", "alice@corp.test"),
    "bob-pat": jira_user("bob", "Bob Reader", "bob@corp.test"),
    "carol-pat": jira_user("carol", "Carol Admin", "carol@corp.test"),
    "mallory-pat": jira_user("mallory", "Mallory Nogroups", "mallory@corp.test"),
    "test-only-jira-pat-0000000000000000": jira_user("jkowalski", "Jan Kowalski", "jkowalski@corp.test"),
}
other = jira_user("someone-else", "Someone Else", "someone-else@corp.test")

unauth = {"status": 401, "headers": JSON,
          "jsonBody": {"errorMessages": ["You are not authenticated. Authentication required to perform this operation."], "errors": {}}}

myself = [{
    "name": "myself without credentials",
    "priority": 1,
    "request": {"method": "GET", "urlPath": "/rest/api/2/myself", "headers": {"Authorization": {"absent": True}}},
    "response": unauth,
}]
for pat, u in pats.items():
    myself.append({
        "name": f"myself as {u['name']}",
        "priority": 2,
        "request": {"method": "GET", "urlPath": "/rest/api/2/myself",
                    "headers": {"Authorization": {"equalTo": "Bearer " + pat}}},
        "response": {"status": 200, "headers": JSON, "jsonBody": u},
    })
myself.append({
    "name": "myself for any other bearer token",
    "priority": 5,
    "request": {"method": "GET", "urlPath": "/rest/api/2/myself",
                "headers": {"Authorization": {"matches": "^Bearer .+$"}}},
    "response": {"status": 200, "headers": JSON, "jsonBody": other},
})
myself.append({
    "name": "myself for non-bearer credentials",
    "priority": 6,
    "request": {"method": "GET", "urlPath": "/rest/api/2/myself"},
    "response": unauth,
})
dump(os.path.join(maps, "myself.json"), {"mappings": myself})

categories = {"Restricted": {"self": f"{BASE}/rest/api/2/projectCategory/10001", "id": "10001",
                             "name": "Restricted", "description": "HR and other restricted data"}}

def project(pid, key, name, category=None):
    p = {"self": f"{BASE}/rest/api/2/project/{pid}", "id": str(pid), "key": key, "name": name,
         "projectTypeKey": "software"}
    if category:
        p["projectCategory"] = categories[category]
    return p

projects = [project(10000, "ABC", "Alpha Team"), project(10001, "DEF", "Delta Team"),
            project(10002, "HR", "Human Resources", "Restricted")]
proj_by_key = {p["key"]: p for p in projects}

def issue(iid, key, summary, status="To Do", links=()):
    proj = proj_by_key[key.split("-")[0]]
    return {
        "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
        "id": str(iid),
        "self": f"{BASE}/rest/api/2/issue/{iid}",
        "key": key,
        "fields": {
            "summary": summary,
            "project": {k: proj[k] for k in ("self", "id", "key", "name", "projectTypeKey")} |
                       ({"projectCategory": proj["projectCategory"]} if "projectCategory" in proj else {}),
            "issuetype": {"id": "10002", "name": "Task", "subtask": False},
            "status": {"name": status, "statusCategory": {"key": "new" if status == "To Do" else "done"}},
            "assignee": None,
            "reporter": {"name": "alice", "displayName": "Alice Tester"},
            "created": "2026-10-01T09:00:00.000+0000",
            "updated": "2026-10-20T12:00:00.000+0000",
            "description": f"Test issue {key} (mcp-platform-dev mock data).",
            "issuelinks": [
                {"id": str(90000 + n), "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                 "outwardIssue": {"id": str(lid), "key": lkey, "self": f"{BASE}/rest/api/2/issue/{lid}",
                                  "fields": {"summary": lsum}}}
                for n, (lid, lkey, lsum) in enumerate(links)
            ],
        },
    }

issues = [
    issue(10100, "ABC-1", "Login page returns 500", links=[(10300, "HR-1", "Salary review 2026"),
                                                            (10200, "DEF-1", "Shared CI runner")]),
    issue(10101, "ABC-2", "Update onboarding docs", status="Done"),
    issue(10200, "DEF-1", "Shared CI runner", links=[(10100, "ABC-1", "Login page returns 500")]),
    issue(10205, "DEF-5", "Migrate build cache (moved from ABC-3)"),
    issue(10300, "HR-1", "Salary review 2026"),
]
by_key = {i["key"]: i for i in issues}
# ABC-3 was moved to DEF: Jira DC resolves the old key and returns the issue under its new key.
aliases = {"ABC-3": "DEF-5"}

for i in issues:
    dump(os.path.join(files, f"issue-{i['key']}.json"), i)

# Data only for PATs the mock knows. Anything else (an IdP JWT passed through, a foreign PAT, Basic) gets 401,
# so a demo cannot succeed by token passthrough (invariant 2).
known_pat = "^Bearer (" + "|".join(re.escape(p) for p in pats) + ")$"
auth_required = {"Authorization": {"matches": known_pat}}
data = []
for path in ("/rest/api/2/project.*", "/rest/api/2/issue/.*", "/rest/api/2/search.*"):
    data.append({"name": f"401 for unknown or missing credentials {path}", "priority": 20,
                 "request": {"urlPattern": path},
                 "response": unauth})

data.append({"name": "list projects", "priority": 5,
             "request": {"method": "GET", "urlPath": "/rest/api/2/project", "headers": auth_required},
             "response": {"status": 200, "headers": JSON, "jsonBody": projects}})
for p in projects:
    for ident in (p["key"], p["id"]):
        data.append({"name": f"project {ident}", "priority": 5,
                     "request": {"method": "GET", "urlPath": f"/rest/api/2/project/{ident}", "headers": auth_required},
                     "response": {"status": 200, "headers": JSON, "jsonBody": p}})
data.append({"name": "project not found", "priority": 9,
             "request": {"method": "GET", "urlPathPattern": "/rest/api/2/project/[^/]+", "headers": auth_required},
             "response": {"status": 404, "headers": JSON,
                          "jsonBody": {"errorMessages": ["No project could be found with key 'unknown'."], "errors": {}}}})

for i in issues:
    for ident in (i["key"], i["id"]):
        data.append({"name": f"issue {ident}", "priority": 5,
                     "request": {"method": "GET", "urlPath": f"/rest/api/2/issue/{ident}", "headers": auth_required},
                     "response": {"status": 200, "headers": JSON, "bodyFileName": f"issue-{i['key']}.json"}})
for old, new in aliases.items():
    data.append({"name": f"moved issue {old} -> {new}", "priority": 5,
                 "request": {"method": "GET", "urlPath": f"/rest/api/2/issue/{old}", "headers": auth_required},
                 "response": {"status": 200, "headers": JSON, "bodyFileName": f"issue-{new}.json"}})
data.append({"name": "issue not found", "priority": 9,
             "request": {"method": "GET", "urlPathPattern": "/rest/api/2/issue/[^/]+", "headers": auth_required},
             "response": {"status": 404, "headers": JSON,
                          "jsonBody": {"errorMessages": ["Issue Does Not Exist"], "errors": {}}}})

def search_result(items):
    return {"expand": "names,schema", "startAt": 0, "maxResults": 50, "total": len(items), "issues": items}

for key in ("ABC", "DEF", "HR"):
    items = [i for i in issues if i["key"].startswith(key + "-")]
    jql = f"(?i).*project\\s*(=|in\\s*\\()\\s*\"?{key}\"?\\b.*"
    data.append({"name": f"search project {key} (GET)", "priority": 5,
                 "request": {"method": "GET", "urlPath": "/rest/api/2/search", "headers": auth_required,
                             "queryParameters": {"jql": {"matches": jql}}},
                 "response": {"status": 200, "headers": JSON, "jsonBody": search_result(items)}})
    data.append({"name": f"search project {key} (POST)", "priority": 5,
                 "request": {"method": "POST", "urlPath": "/rest/api/2/search", "headers": auth_required,
                             "bodyPatterns": [{"matchesJsonPath": {"expression": "$.jql", "matches": jql}}]},
                 "response": {"status": 200, "headers": JSON, "jsonBody": search_result(items)}})
for method in ("GET", "POST"):
    data.append({"name": f"search without match ({method})", "priority": 9,
                 "request": {"method": method, "urlPath": "/rest/api/2/search", "headers": auth_required},
                 "response": {"status": 200, "headers": JSON, "jsonBody": search_result([])}})

dump(os.path.join(maps, "data.json"), {"mappings": data})
