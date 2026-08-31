"""A stand-in Plex Media Server for end-to-end testing.

Serves the same JSON shapes as a real PMS from the repo's fixtures, mutates its
watch state when scrobbled, and records every call so a test can assert on what
actually reached "Plex" rather than on what the UI claimed.

THE IMPORTANT DIFFERENCE FROM v1's MOCK: watch state is keyed BY TOKEN. Plex
watch state is per-account, and v1's mock held one global state, so no test
could catch the app using the wrong user's token — the single most important
invariant in the application. Here, unscrobbling as Alice leaves Bob untouched,
and a test can prove it.

Run standalone with:  python tests/e2e/mock_plex.py [port]
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Query, Request, Response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

app = FastAPI()

MOVIES = json.loads((FIXTURES / "movies_section.json").read_text(encoding="utf-8"))
SHOWS = json.loads((FIXTURES / "shows_section.json").read_text(encoding="utf-8"))
EPISODES = json.loads((FIXTURES / "episodes_all_leaves.json").read_text(encoding="utf-8"))

# token -> {ratingKey -> item}
STATE: dict[str, dict[str, dict]] = {}
# (call, token, ratingKey)
CALLS: list[dict[str, str]] = []

# Tokens the mock will answer for. Anything else gets a 401, so a test can prove
# an unlinked user is genuinely skipped.
VALID_TOKENS = {"tok-owner", "tok-alice", "tok-bob", "mock-token"}


def _fresh() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for payload in (MOVIES, SHOWS, EPISODES):
        for item in payload["MediaContainer"]["Metadata"]:
            out[item["ratingKey"]] = copy.deepcopy(item)
    return out


def state_for(token: str) -> dict[str, dict]:
    """Per-token watch state, created lazily so each account starts identical."""
    if token not in STATE:
        STATE[token] = _fresh()
    return STATE[token]


def reset_state() -> None:
    STATE.clear()
    CALLS.clear()
    for token in VALID_TOKENS:
        STATE[token] = _fresh()


reset_state()


def _unauthorised(token: str) -> Response | None:
    if token not in VALID_TOKENS:
        return Response(status_code=401, content="invalid token")
    return None


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------

@app.get("/identity")
def identity():
    """Answers without a token, exactly like a real PMS."""
    return {
        "MediaContainer": {
            "machineIdentifier": "mock-machine-123",
            "version": "1.41.0.0",
        }
    }


@app.get("/library/sections")
def sections(x_plex_token: str = Header("")):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    return {
        "MediaContainer": {
            "size": 3,
            "Directory": [
                {"key": "1", "title": "Movies", "type": "movie", "uuid": "uuid-1"},
                {"key": "2", "title": "TV Shows", "type": "show", "uuid": "uuid-2"},
                # Must be filtered out by the app: watch state does not apply,
                # and nobody wants their albums "unwatched".
                {"key": "3", "title": "Music", "type": "artist", "uuid": "uuid-3"},
            ],
        }
    }


@app.get("/accounts")
def accounts(x_plex_token: str = Header("")):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    return {
        "MediaContainer": {
            "size": 2,
            "Account": [
                {"id": "1", "name": "alice", "thumb": ""},
                {"id": "7", "name": "bob", "thumb": ""},
            ],
        }
    }


@app.get("/status/sessions")
def sessions(x_plex_token: str = Header("")):
    return {"MediaContainer": {"size": 0}}


# ---------------------------------------------------------------------------
# Library reads
# ---------------------------------------------------------------------------

@app.get("/library/sections/{key}/all")
def section_all(
    key: str, request: Request, type: int = Query(1), x_plex_token: str = Header("")
):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    mine = state_for(x_plex_token)
    start = int(request.headers.get("X-Plex-Container-Start", 0))
    size = int(request.headers.get("X-Plex-Container-Size", 500))

    if key == "1" and type == 1:
        items = [mine[m["ratingKey"]] for m in MOVIES["MediaContainer"]["Metadata"]]
    elif key == "2" and type == 2:
        items = []
        for show in SHOWS["MediaContainer"]["Metadata"]:
            live = dict(mine[show["ratingKey"]])
            # Recomputed from live episode state the way a real server does, so
            # unscrobbling one episode really does stop its series counting as
            # finished.
            eps = [
                e
                for e in EPISODES["MediaContainer"]["Metadata"]
                if e.get("grandparentRatingKey") == show["ratingKey"]
            ]
            live["leafCount"] = len(eps)
            live["viewedLeafCount"] = sum(
                1 for e in eps if mine[e["ratingKey"]].get("viewCount", 0) > 0
            )
            items.append(live)
    else:
        items = []

    page = items[start : start + size]
    return {
        "MediaContainer": {"size": len(page), "totalSize": len(items), "Metadata": page}
    }


@app.get("/library/metadata/{key}/allLeaves")
def all_leaves(key: str, x_plex_token: str = Header("")):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    mine = state_for(x_plex_token)
    eps = [
        mine[e["ratingKey"]]
        for e in EPISODES["MediaContainer"]["Metadata"]
        if e.get("grandparentRatingKey") == key
    ]
    return {"MediaContainer": {"size": len(eps), "totalSize": len(eps), "Metadata": eps}}


@app.get("/library/metadata/{key}/thumb")
def thumb(key: str, x_plex_token: str = Header("")):
    return Response(content=b"\x89PNG-mock-art", media_type="image/png")


@app.get("/library/sections/{key}/{field}")
def tags(key: str, field: str, x_plex_token: str = Header("")):
    return {"MediaContainer": {"size": 0}}


# ---------------------------------------------------------------------------
# Writes -- each mutates ONLY the calling token's state
# ---------------------------------------------------------------------------

@app.get("/:/unscrobble")
def unscrobble(
    key: str = Query(...), identifier: str = Query(""), x_plex_token: str = Header("")
):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    CALLS.append({"call": "unscrobble", "token": x_plex_token, "key": key})
    mine = state_for(x_plex_token)
    if key in mine:
        mine[key]["viewCount"] = 0
        mine[key].pop("lastViewedAt", None)
        mine[key].pop("viewOffset", None)
    return Response(status_code=200)


@app.get("/:/scrobble")
def scrobble(
    key: str = Query(...), identifier: str = Query(""), x_plex_token: str = Header("")
):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    CALLS.append({"call": "scrobble", "token": x_plex_token, "key": key})
    mine = state_for(x_plex_token)
    if key in mine:
        mine[key]["viewCount"] = mine[key].get("viewCount", 0) + 1
        # A FRESH play -- deliberately not the original date, because that is
        # exactly what Plex does and what Undo cannot restore.
        mine[key]["lastViewedAt"] = 1_755_300_000
    return Response(status_code=200)


@app.get("/:/progress")
def progress(
    key: str = Query(...),
    identifier: str = Query(""),
    time: int = Query(0),
    x_plex_token: str = Header(""),
):
    if (bad := _unauthorised(x_plex_token)) is not None:
        return bad
    CALLS.append({"call": "progress", "token": x_plex_token, "key": key})
    mine = state_for(x_plex_token)
    if key in mine and time == 0:
        mine[key].pop("viewOffset", None)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Test-only introspection
# ---------------------------------------------------------------------------

@app.get("/__calls")
def get_calls() -> list[dict[str, str]]:
    return CALLS


@app.get("/__state")
def get_state() -> dict[str, Any]:
    """Watch state per token, so a test can assert user isolation."""
    return {
        token: {
            key: {
                "viewCount": item.get("viewCount", 0),
                "lastViewedAt": item.get("lastViewedAt"),
            }
            for key, item in items.items()
        }
        for token, items in STATE.items()
    }


@app.post("/__reset")
def reset():
    reset_state()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(sys.argv[1]) if len(sys.argv) > 1 else 32400,
        log_level="warning",
    )
