#!/usr/bin/env python3
"""Read agent work from GitHub without returning untrusted ticket text.

Only reubenfirmin's issues and comments in TuneConsole are eligible. All recorded
editors must also be trusted. Incomplete edit histories are withheld; the user
can create a fresh, reviewed ticket instead. This is a content admission filter,
not a replacement for the agent's sandbox or instruction hierarchy.

    python3 scripts/agent-tickets.py list --status Ready
    python3 scripts/agent-tickets.py read 704
    python3 scripts/agent-tickets.py read 704 --output /tmp/ticket-704.json
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

OWNER = "reubenfirmin"
REPOSITORY = "TuneConsole"
PROJECT_ID = "PVT_kwHOANjzCc4BbZBg"
TRUSTED_AUTHORS = frozenset({OWNER})
STATUSES = ("Backlog", "Ready", "In progress", "In review", "Done")

PROVENANCE = """
    author { __typename login }
    editor { __typename login }
    lastEditedAt
    userContentEdits(first:100) {
      nodes { editor { __typename login } }
      pageInfo { hasNextPage }
    }
"""
TITLE_PROVENANCE = """
    titleEdits:timelineItems(first:100, itemTypes:[RENAMED_TITLE_EVENT]) {
      nodes { ... on RenamedTitleEvent { actor { __typename login } } }
      pageInfo { hasNextPage }
    }
"""
LIST_QUERY = """
query($project:ID!, $cursor:String) {
  node(id:$project) {
    ... on ProjectV2 {
      id owner { ... on User { login } }
      items(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isArchived
          status:fieldValueByName(name:"Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          priority:fieldValueByName(name:"Priority") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            __typename
            ... on Issue {
              id number title state repository { nameWithOwner }
""" + PROVENANCE + TITLE_PROVENANCE + """
            }
          }
        }
      }
    }
  }
}
"""
READ_QUERY = """
query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      __typename id number title body updatedAt repository { nameWithOwner }
""" + PROVENANCE + TITLE_PROVENANCE + """
      comments(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          body url isMinimized
""" + PROVENANCE + """
        }
      }
    }
  }
}
"""


class TicketError(Exception):
    """A request cannot safely return ticket content."""


def api(query, **variables):
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": variables}),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            raise TicketError("GitHub API request failed; no ticket content returned.")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("errors") or not isinstance(response.get("data"), dict):
            raise TicketError("Incomplete GitHub response; no ticket content returned.")
        return response["data"]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise TicketError("Could not read GitHub; no ticket content returned.") from exc


def trusted_actor(actor):
    return (isinstance(actor, dict) and actor.get("__typename") == "User"
            and isinstance(actor.get("login"), str)
            and actor["login"].casefold() in TRUSTED_AUTHORS)


def trusted_edits(history, actor_field):
    if not isinstance(history, dict) or not isinstance(history.get("nodes"), list):
        return False
    info = history.get("pageInfo")
    return (isinstance(info, dict) and info.get("hasNextPage") is False
            and all(isinstance(edit, dict) and trusted_actor(edit.get(actor_field))
                    for edit in history["nodes"]))


def trusted_content(content):
    if not isinstance(content, dict) or not trusted_actor(content.get("author")):
        return False
    if "editor" not in content or "lastEditedAt" not in content:
        return False
    editor = content["editor"]
    if editor is not None and not trusted_actor(editor):
        return False
    history = content.get("userContentEdits")
    if not trusted_edits(history, "editor"):
        return False
    if content["lastEditedAt"] is not None and (not trusted_actor(editor) or not history["nodes"]):
        return False
    return True


def eligible_issue(issue):
    return (isinstance(issue, dict) and issue.get("__typename") == "Issue"
            and isinstance(issue.get("repository"), dict)
            and issue["repository"].get("nameWithOwner") == f"{OWNER}/{REPOSITORY}"
            and type(issue.get("number")) is int and issue["number"] > 0
            and trusted_content(issue) and trusted_edits(issue.get("titleEdits"), "actor"))


def connection_page(connection, seen):
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise TicketError("Incomplete pagination; no ticket content returned.")
    info = connection.get("pageInfo")
    if not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
        raise TicketError("Incomplete pagination; no ticket content returned.")
    cursor = info.get("endCursor") if info["hasNextPage"] else None
    if info["hasNextPage"]:
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise TicketError("Invalid pagination cursor; no ticket content returned.")
        seen.add(cursor)
    return connection["nodes"], cursor


def issue_summary(issue):
    if not isinstance(issue.get("title"), str):
        raise TicketError("Incomplete ticket; no ticket content returned.")
    return {"number": issue["number"], "title": issue["title"],
            "url": f"https://github.com/{OWNER}/{REPOSITORY}/issues/{issue['number']}"}


def list_tickets(statuses):
    tickets, seen, cursor, withheld = [], set(), None, 0
    while True:
        project = api(LIST_QUERY, project=PROJECT_ID, cursor=cursor).get("node")
        if (not isinstance(project, dict) or project.get("id") != PROJECT_ID
                or not isinstance(project.get("owner"), dict)
                or project["owner"].get("login") != OWNER):
            raise TicketError("Unexpected project; no ticket content returned.")
        items, cursor = connection_page(project.get("items"), seen)
        for item in items:
            if not isinstance(item, dict):
                raise TicketError("Incomplete project item; no ticket content returned.")
            status = (item.get("status") or {}).get("name")
            if item.get("isArchived") is not False or status not in statuses:
                continue
            issue = item.get("content")
            if not eligible_issue(issue):
                withheld += 1
                continue
            if issue.get("state") != "OPEN":
                continue
            tickets.append({**issue_summary(issue), "item_id": item["id"],
                            "status": status, "priority": (item.get("priority") or {}).get("name")})
        if cursor is None:
            return {"tickets": tickets, "withheld_items": withheld}


def read_ticket(number):
    seen, cursor, snapshot, comments, withheld = set(), None, None, [], 0
    while True:
        data = api(READ_QUERY, owner=OWNER, repo=REPOSITORY, number=number, cursor=cursor)
        issue = (data.get("repository") or {}).get("issue")
        if not eligible_issue(issue) or issue["number"] != number:
            raise TicketError("Ticket is missing or its authorship/edit history is not trusted.")
        if not isinstance(issue.get("body"), str) or not isinstance(issue.get("updatedAt"), str):
            raise TicketError("Incomplete ticket; no ticket content returned.")
        current = {**issue_summary(issue), "body": issue["body"], "updated_at": issue["updatedAt"]}
        if snapshot is not None and current != snapshot:
            raise TicketError("Ticket changed during reading; retry for a fresh snapshot.")
        snapshot = current
        page, cursor = connection_page(issue.get("comments"), seen)
        for comment in page:
            if (not trusted_content(comment) or comment.get("isMinimized") is not False):
                withheld += 1
                continue
            if not isinstance(comment.get("body"), str) or not isinstance(comment.get("url"), str):
                raise TicketError("Incomplete comment; no ticket content returned.")
            comments.append({"body": comment["body"], "url": comment["url"]})
        if cursor is None:
            return {**snapshot, "comments": comments, "withheld_comments": withheld}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List eligible open project issues")
    listing.add_argument("--status", action="append", choices=STATUSES)
    reading = commands.add_parser("read", help="Read an eligible issue and filtered comments")
    reading.add_argument("number", type=int)
    for command in (listing, reading):
        command.add_argument("--output", type=Path, metavar="PATH",
                             help="Write filtered JSON to PATH instead of stdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "read" and args.number < 1:
            raise TicketError("Ticket number must be positive.")
        result = (list_tickets(args.status or ["Ready"]) if args.command == "list"
                  else read_ticket(args.number))
    except TicketError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rendered = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError:
            print("Could not write ticket output.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
