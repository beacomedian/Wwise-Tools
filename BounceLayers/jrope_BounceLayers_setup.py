#!/usr/bin/env python
'''
 * Name: Bounce Layers - Setup
 * Author: Jesse Rope
 * AI: Claude (Opus 4.8)
 * Wwise: 2025.1.8
 * Script Version: 1.0
 * About:
    # First-time setup for the Bounce Layers tool. Creates the two objects the
    # main tool needs, if they don't already exist:
    #   1. an Audio Bus  "BounceLayers_CaptureBus"  under the Master Audio Bus,
    #   2. a Wwise Recorder ShareSet  "BounceLayers_Recorder"  under Effects,
    #      referenced from the capture bus's first effect slot.
    # Idempotent: re-running skips anything that already exists. The whole run is
    # one undo group ("Bounce Layers setup"), so a single Ctrl+Z reverts it.
    #
    # IMPORTANT: a bus created over WAAPI does NOT engage Wwise's live audio
    # render until the project is reloaded. After this script finishes, SAVE the
    # project (Ctrl+S) and RESTART Wwise before running your first bounce.
 * Changelog:
    # 1.0 - Initial Release
'''

from __future__ import annotations

import argparse
import sys

from waapi import WaapiClient, CannotConnectToWaapiException

# ---------------------------------------------------------------------------
# CONFIG  -- keep these two names in sync with jrope_BounceLayers.py
# (CAPTURE_BUS_NAME / RECORDER_SHARESET_NAME). The main script refuses to be
# imported, so they're duplicated here rather than shared.
# ---------------------------------------------------------------------------
WAAPI_URL = "ws://127.0.0.1:8080/waapi"
CAPTURE_BUS_NAME = "BounceLayers_CaptureBus"
RECORDER_SHARESET_NAME = "BounceLayers_Recorder"

# Wwise Recorder plug-in classId (confirmed live). Effect ShareSets are created
# with the classId of the plug-in they wrap.
RECORDER_CLASS_ID = 8650755

# Recorder config properties applied to a freshly-created ShareSet so it matches
# the reference/known-good one exactly, rather than relying on plug-in defaults
# (these ARE the Wwise Recorder defaults, but pinning them makes setup
# deterministic across Wwise versions and machines). Intentionally omitted:
#   AuthoringFilename - set per-take at runtime by the main tool (not config)
#   GameFilename      - game-side output path, unused by the authoring capture
# Only applied when the ShareSet is newly created; an existing one is left as-is.
RECORDER_PROPERTIES = {
    "ApplyDownstreamVolume": False,
    "DownmixToStereo": False,
    "AmbisonicsChannelOrdering": 0,
    "Format": 0,
    "Front": 0.0,
    "Center": -3.0,
    "Surround": -3.0,
    "Rear": -3.0,
    "LFE": -96.3,
}

# Where the ShareSet is created (any Effects Work Unit works; the main tool finds
# it by name wherever it lives). Default Work Unit is the safe default.
EFFECTS_WORKUNIT_PATH = r"\Effects\Default Work Unit"

# Fallbacks for locating the top of the Master-Mixer hierarchy to parent the bus.
MASTER_BUS_NAME = "Master Audio Bus"
MASTER_BUS_PATH = r"\Busses\Default Work Unit\Master Audio Bus"


def log(msg: str) -> None:
    print(f"[bl-setup] {msg}", flush=True)


def waql(client: WaapiClient, query: str, props: list[str] | None = None) -> list[dict]:
    r = client.call("ak.wwise.core.object.get", {
        "waql": query,
        "options": {"return": props or ["id", "name", "type", "path"]},
    }) or {}
    return r.get("return", [])


def find_one(client: WaapiClient, query: str, props: list[str] | None = None) -> dict | None:
    rows = waql(client, query, props)
    return rows[0] if rows else None


def _find_master_bus(client: WaapiClient) -> dict | None:
    """Locate the Master Audio Bus to parent the capture bus under. Try the
    conventional name, then the conventional path (covers a renamed default WU)."""
    row = find_one(client, f'$ from type Bus where name = "{MASTER_BUS_NAME}"',
                   ["id", "name", "path"])
    if row:
        return row
    return find_one(client, f'$ "{MASTER_BUS_PATH}"', ["id", "name", "path"])


def ensure_capture_bus(client: WaapiClient) -> str:
    existing = find_one(client, f'$ from type Bus where name = "{CAPTURE_BUS_NAME}"',
                        ["id", "name", "path"])
    if existing:
        log(f"Capture bus already exists: {existing.get('path', existing['id'])}")
        return existing["id"]

    master = _find_master_bus(client)
    if not master:
        raise SystemExit(
            f"Could not find '{MASTER_BUS_NAME}' (or {MASTER_BUS_PATH}) to parent "
            "the capture bus under. Create the capture bus by hand, or rename your "
            "master bus back to the default."
        )
    created = client.call("ak.wwise.core.object.create", {
        "parent": master["id"],
        "type": "Bus",
        "name": CAPTURE_BUS_NAME,
        "onNameConflict": "fail",
    }) or {}
    log(f"Created capture bus '{CAPTURE_BUS_NAME}' under {master.get('path', master['id'])}")
    return created["id"]


def ensure_recorder_shareset(client: WaapiClient) -> str:
    existing = find_one(client, f'$ from type Effect where name = "{RECORDER_SHARESET_NAME}"',
                        ["id", "name", "path"])
    if existing:
        log(f"Recorder ShareSet already exists: {existing.get('path', existing['id'])}")
        return existing["id"]

    wu = find_one(client, f'$ "{EFFECTS_WORKUNIT_PATH}"', ["id", "name"])
    if not wu:
        raise SystemExit(
            f"Effects Work Unit not found at {EFFECTS_WORKUNIT_PATH}. "
            "Create the Recorder ShareSet by hand, or check the path."
        )
    # NOTE: `classId` is NOT a valid argument on ak.wwise.core.object.create
    # (schema rejects it). A typed effect must be created as a NESTED object
    # inside ak.wwise.core.object.set, where `classId` IS accepted. Creating it
    # as a child of the Effects Work Unit makes it a real ShareSet, findable by
    # the main tool's `$ from type Effect where name = ...` query. (Verified live.)
    res = client.call("ak.wwise.core.object.set", {
        "objects": [{
            "object": wu["id"],
            "children": [
                {"type": "Effect", "name": RECORDER_SHARESET_NAME,
                 "classId": RECORDER_CLASS_ID},
            ],
        }],
    }) or {}
    new_id = res["objects"][0]["children"][0]["id"]
    log(f"Created Wwise Recorder ShareSet '{RECORDER_SHARESET_NAME}'")
    # Pin the config properties so it matches the reference ShareSet exactly.
    for prop, value in RECORDER_PROPERTIES.items():
        client.call("ak.wwise.core.object.setProperty",
                    {"object": new_id, "property": prop, "value": value})
    log(f"Applied {len(RECORDER_PROPERTIES)} Recorder config properties")
    return new_id


def attach_recorder_to_bus(client: WaapiClient, bus_id: str, shareset_id: str) -> None:
    """Reference the Recorder ShareSet from the bus's first effect slot. Setting
    the whole @Effects list to a single slot is what a dedicated capture bus
    wants (its only effect is the Recorder). Re-assigning the same reference is
    harmless, so this stays idempotent on re-runs."""
    client.call("ak.wwise.core.object.set", {
        "objects": [{
            "object": bus_id,
            "@Effects": [
                {"type": "EffectSlot", "name": "", "@Effect": shareset_id},
            ],
        }],
    })
    log("Referenced Recorder ShareSet from capture bus effect slot 0")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="First-time setup for Bounce Layers: create the capture bus "
                    "and Recorder ShareSet.")
    ap.add_argument("--url", default=WAAPI_URL)
    args = ap.parse_args()

    try:
        client = WaapiClient(url=args.url)
    except CannotConnectToWaapiException:
        sys.stderr.write(
            f"ERROR: cannot reach WAAPI at {args.url}.\n"
            "Open Wwise and enable it: User Preferences -> "
            "'Enable Wwise Authoring API'.\n")
        return 2

    undo_open = False
    try:
        client.call("ak.wwise.core.undo.beginGroup", {})
        undo_open = True

        bus_id = ensure_capture_bus(client)
        shareset_id = ensure_recorder_shareset(client)
        attach_recorder_to_bus(client, bus_id, shareset_id)

        log("Setup complete.")
        log("NEXT: save the project (Ctrl+S) and RESTART Wwise before your first "
            "bounce -- a WAAPI-created bus only engages the audio render after a "
            "project reload.")
        return 0
    except (SystemExit, Exception) as e:
        log(f"{'Stopped' if isinstance(e, SystemExit) else 'ERROR'}: {e}")
        return 1
    finally:
        if undo_open:
            try:
                client.call("ak.wwise.core.undo.endGroup",
                            {"displayName": "Bounce Layers setup"})
            except Exception as e:
                log(f"WARNING: could not end undo group: {e}")
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
