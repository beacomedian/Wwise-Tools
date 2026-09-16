'''
 * Name: Bounce Layers
 * Author: Jesse Rope
 * AI: Claude (Opus 4.8)
 * Wwise: 2025.1.8
 * Script Version: 1.0
 * About:
    # Flattens a multi-layer Wwise container (e.g. a Blend container holding
    # several Random containers) into a single baked Random container of
    # pre-rendered variation takes, by capturing the container's live output
    # through the Wwise Recorder. High-level flow:
    #   1. Resolve the target container (arg GUID or current Wwise selection).
    #   2. Inspect its subtree; detect looping sources among objects to bounce.
    #   3. Prompt for the number of variations N (and a fixed record length if a
    #      loop was detected).
    #   4. Route the target's Output Bus to a pre-made ISOLATED capture bus (an
    #      Audio Bus carrying only a Wwise Recorder), so the container's layers
    #      are SUMMED there and no higher-level mix colours the capture. (The bus
    #      is made once by hand -- a WAAPI-created bus does not engage Wwise's
    #      live audio render.)
    #   5. Drive the transport N times, writing one finalized WAV per take.
    #   6. Import the takes into a new "<Name>_COMP" Random container child.
    #   7. (unless --dry-run) Group the original source containers into a disabled
    #      "preComp" container inside the target (Inclusion off -> excluded).
    #   8. Restore the target's original routing, disconnect.
    # Requires: Wwise Authoring running with WAAPI enabled (User Preferences ->
    # "Enable Wwise Authoring API") and `pip install waapi-client`, plus the
    # one-time capture bus + Recorder ShareSet (see BounceLayers/README.md).
 * Changelog:
    # 1.0 - Initial Release
 * To Do:
    # Copy looping settings to new bounces on infinite loop runs
'''

from __future__ import annotations

if __name__ != '__main__':
    print(f'error: {__file__} should not be imported, aborting script')
    exit(1)

import argparse
import os
import re
import sys
import time
import wave
from dataclasses import dataclass, field

import tkinter
from tkinter.messagebox import showinfo, showerror
from waapi import WaapiClient, CannotConnectToWaapiException

# Hidden Tk root for messagebox dialogs; the interactive windows below parent
# off it (Toplevel) so every dialog shares one interpreter. Destroyed at exit.
_TK_ROOT = tkinter.Tk()
_TK_ROOT.withdraw()


# ---------------------------------------------------------------------------
# CONFIG  (things you may want to tune; VERIFY-LIVE items need a running Wwise)
# ---------------------------------------------------------------------------

# --- Names (change these to taste) -----------------------------------------
# Suffix for the comp Random container and its take .wav files. With the default,
# a target "Foo" produces the container "Foo_COMP" holding takes "Foo_COMP_01",
# "Foo_COMP_02", ... (and matching .wav files).
COMP_SUFFIX = "_COMP"
# Name of the disabled container created INSIDE the target that holds the
# original (pre-comp) source layers as an editable, excluded backup.
PRECOMP_NAME = "preComp"

# Default number of variations (takes) pre-filled in the dialog / used by
# --no-gui when -n/--variations is not passed.
DEFAULT_VARIATIONS = 5

# Hierarchy voice-gain compensation. Voice Volume + Make-Up Gain set on the
# target and its ancestors is baked into the bounce AND re-applied when the
# _COMP is re-imported under the same hierarchy -> double attenuation. The tool
# compensates by the negative of that summed gain (e.g. -12 dB Voice + +4 dB
# Make-Up = -8 dB net -> +8 dB compensation). Modes:
#   "pre"  - temporarily boost the target's Voice Volume during capture so the
#            recording excludes the inherited gain (re-import re-applies it once).
#            Raw WAVs come out un-attenuated; can CLIP if that gain was headroom.
#   "post" - leave the recording at natural level and set the _COMP container's
#            Voice Volume to cancel the inherited gain. Never clips.
#   "off"  - no compensation.
VOLUME_COMPENSATION = "pre"
# ---------------------------------------------------------------------------

WAAPI_URL = "ws://127.0.0.1:8080/waapi"

# One-time setup (see README): a dedicated Audio Bus carrying ONLY a Wwise
# Recorder ShareSet. A bus created via WAAPI at runtime does NOT get wired into
# Wwise's live audio render (confirmed: it records nothing), so the capture bus
# is made once by hand and reused. The tool reroutes the target's Output Bus to
# it and sets the Recorder's filename per take. Looked up by name so it works
# wherever it sits in the bus hierarchy.
CAPTURE_BUS_NAME = "BounceLayers_CaptureBus"
# The Wwise Recorder ShareSet on the capture bus (its AuthoringFilename is set
# per take). Looked up by NAME so it works wherever it sits in the Effects
# section (any Work Unit) -- relocation-safe, like the capture bus.
RECORDER_SHARESET_NAME = "BounceLayers_Recorder"
RECORDER_FILE_PROPERTY = "AuthoringFilename"   # authoring-transport output path

# Bus property toggled to arm/flush the Recorder between takes (validated live:
# bypass->unbypass finalizes each take's file cleanly, one file per take).
BUS_BYPASS_PROPERTY = "BypassEffect"

# Scratch dir for the raw take WAVs before they are imported (import copies them
# into the project's Originals folder, after which these are deleted).
SCRATCH_DIR = os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), "BounceLayers"
)

# Timing (seconds).
TRANSPORT_POLL = 0.1        # how often to poll transport state
IDLE_TAIL = 0.6             # extra time recorded after the object goes idle (tail)
INTER_TAKE_PAUSE = 0.25     # settle time between takes
AUTO_MAX_DURATION = 60.0    # safety cap when auto-stopping (one-shots/finite loops
                            # play out before this; guards an undetected inf. loop)
WARMUP_TIMEOUT = 3.0        # max wait for transport to reach "playing"

# Container object types we accept as a bounce target.
CONTAINER_TYPES = {
    "BlendContainer",
    "RandomSequenceContainer",  # Random AND Sequence share this Wwise type
    "SwitchContainer",
    "ActorMixer",
}


def log(msg: str) -> None:
    print(f"[bounce] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Target:
    id: str
    name: str
    type: str
    path: str
    parent_id: str                         # parent object id (create/move target)
    output_bus: str | None                 # original Output Bus id (to restore)
    override_output: bool                  # original @OverrideOutput (to restore)
    sources: list[dict] = field(default_factory=list)   # immediate child containers
    has_infinite_loop: bool = False        # a descendant loops forever (IsLoopingInfinite)
    looping_sources: list[dict] = field(default_factory=list)
    originals_subfolder: str = ""          # mirrored from an existing sibling sound
    rerouting_descendants: list[dict] = field(default_factory=list)  # override own output
    own_volume: float = 0.0                # target's own @Volume (pre-bounce restore)
    hierarchy_gain: float = 0.0            # sum of Volume+MakeUpGain, target + ancestors
    volume_chain: list[dict] = field(default_factory=list)  # for logging
    pre_comp_applied: bool = False         # was the target volume boosted for capture?


@dataclass
class RunOptions:
    variations: int
    record_length: float | None            # None => auto-stop on idle
    dry_run: bool
    comp_as_makeup: bool = False           # apply hierarchy-gain comp as
    #                                        Make-Up Gain on _COMP (post-bounce),
    #                                        instead of the pre-bounce Voice boost


# ---------------------------------------------------------------------------
# WAAPI helpers
# ---------------------------------------------------------------------------

def waql(client: WaapiClient, query: str, return_fields: list[str]) -> list[dict]:
    res = client.call(
        "ak.wwise.core.object.get",
        {"waql": query},
        options={"return": return_fields},
    )
    return (res or {}).get("return", [])


def get_selected(client: WaapiClient) -> str | None:
    res = client.call(
        "ak.wwise.ui.getSelectedObjects",
        {},
        options={"return": ["id", "type"]},
    )
    objs = (res or {}).get("objects", [])
    return objs[0]["id"] if objs else None


def resolve_target(client: WaapiClient, target_id: str | None) -> Target:
    if not target_id:
        target_id = get_selected(client)
    if not target_id:
        raise SystemExit("No target given and nothing selected in Wwise.")

    fields = [
        "id", "name", "type", "path", "parent",
        "@OverrideOutput", "@OutputBus", "@Volume",
    ]
    rows = waql(client, f'$ "{target_id}"', fields)
    if not rows:
        raise SystemExit(f"Target {target_id} not found.")
    row = rows[0]
    if row["type"] not in CONTAINER_TYPES:
        raise SystemExit(
            f"Target '{row['name']}' is a {row['type']}; expected one of "
            f"{sorted(CONTAINER_TYPES)}."
        )

    parent = row.get("parent") or {}
    output_bus = row.get("@OutputBus")
    if isinstance(output_bus, dict):
        output_bus = output_bus.get("id")

    tgt = Target(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        path=row["path"],
        parent_id=parent.get("id", ""),
        output_bus=output_bus,
        override_output=bool(row.get("@OverrideOutput")),
    )

    # Immediate child containers = the "sources" that will be bounced/disabled.
    tgt.sources = waql(
        client,
        f'$ "{target_id}" select children where type != "Sound"',
        ["id", "name", "type", "path"],
    )

    # Looping-source detection. Only an ACTIVE, INFINITELY-looping source needs a
    # record-length cap (it never ends -> no natural stop point). A source is
    # active only if it will actually play: its own Inclusion AND every ancestor's
    # Inclusion up to the target are true. A disabled/excluded (Inclusion=false)
    # container is silent in transport audition, so its (even infinite) loops must
    # be ignored. Finite loops (IsLoopingInfinite=false) and one-shots play out and
    # auto-stop, so they must NOT be capped. Discriminator = @IsLoopingInfinite
    # (NOT @LoopCount). Walk the subtree with inclusion + parent to judge activity.
    subtree = waql(
        client,
        f'$ "{target_id}" select this, descendants',
        ["id", "name", "type", "parent", "@Inclusion",
         "@IsLoopingEnabled", "@IsLoopingInfinite", "@LoopCount"],
    )
    by_id = {o["id"]: o for o in subtree}

    def _active(oid: str) -> bool:
        o = by_id.get(oid)
        if o is None:                       # walked above the target's subtree
            return True
        if o.get("@Inclusion") is False:    # this node (or an ancestor) is excluded
            return False
        if oid == target_id:
            return True
        return _active((o.get("parent") or {}).get("id"))

    tgt.looping_sources = [
        {**o, "_active": _active(o["id"])}
        for o in subtree if o.get("@IsLoopingEnabled")
    ]
    tgt.has_infinite_loop = any(
        s["_active"] and s.get("@IsLoopingInfinite") for s in tgt.looping_sources
    )

    # Descendants that override their own Output Bus route straight to that bus,
    # bypassing the target's (rerouted) output — so they are NOT captured. Flag
    # them so the user knows those layers will be missing from the bounce.
    tgt.rerouting_descendants = waql(
        client,
        f'$ "{target_id}" select descendants where @OverrideOutput = true',
        ["id", "name", "type", "@OutputBus"],
    )

    # Trace the target + ancestors and sum the per-voice hierarchy gain (Voice
    # Volume + Make-Up Gain). This is baked into the bounce and re-applied when
    # the _COMP is re-imported under the same hierarchy, so the tool compensates
    # by its negative. WorkUnits/Folders carry no such gain (return None -> 0).
    tgt.volume_chain = waql(
        client,
        f'$ "{target_id}" select this, ancestors',
        ["name", "type", "@Volume", "@MakeUpGain"],
    )
    tgt.hierarchy_gain = sum(
        (r.get("@Volume") or 0.0) + (r.get("@MakeUpGain") or 0.0)
        for r in tgt.volume_chain
    )
    tgt.own_volume = row.get("@Volume") or 0.0

    # Mirror the Originals subfolder from an existing descendant Sound so the
    # baked WAVs land next to the sources they replace.
    tgt.originals_subfolder = _derive_originals_subfolder(client, target_id)
    return tgt


def _derive_originals_subfolder(client: WaapiClient, target_id: str) -> str:
    rows = waql(
        client,
        f'$ "{target_id}" select descendants where type = "Sound"',
        ["id", "sound:originalWavFilePath"],
    )
    for r in rows:
        p = r.get("sound:originalWavFilePath")
        if p:
            # .../Originals/SFX/<subfolder>/<file>.wav  ->  <subfolder>
            m = re.search(r"[/\\]Originals[/\\]SFX[/\\](.*)[/\\][^/\\]+$", p)
            if m:
                return m.group(1).replace("\\", "/")
    return ""  # import will place at Originals/SFX root


# ---------------------------------------------------------------------------
# Capture rig  (pre-made dedicated bus + Recorder ShareSet)
# ---------------------------------------------------------------------------

@dataclass
class CaptureRig:
    bus_id: str        # the pre-made capture bus (NOT deleted in teardown)
    shareset_id: str   # the Recorder ShareSet on that bus


def build_capture_rig(client: WaapiClient, tgt: Target) -> CaptureRig:
    """Route the target's output to the pre-made capture bus (Recorder-only) so
    we record the container's blended output in isolation from the wider mix.
    The bus + Recorder ShareSet are created once by hand (see README) because a
    WAAPI-created bus does not engage Wwise's live audio render."""
    bus_rows = waql(
        client,
        f'$ from type Bus where name = "{CAPTURE_BUS_NAME}"',
        ["id", "name", "path"],
    )
    if not bus_rows:
        raise SystemExit(
            f"Capture bus '{CAPTURE_BUS_NAME}' not found.\n"
            "Create it once (see README 'One-time setup'): an Audio Bus under the "
            "Master Audio Bus, with the Recorder ShareSet as its only effect."
        )
    # Found by NAME, so the bus can live anywhere in the Master-Mixer hierarchy
    # (relocation-safe). Bus names aren't required unique, though, so if a move or
    # duplicate left more than one, don't silently pick one — say which we used.
    if len(bus_rows) > 1:
        chosen = bus_rows[0].get("path", bus_rows[0]["id"])
        others = ", ".join(b.get("path", b["id"]) for b in bus_rows[1:])
        log(f"WARNING: {len(bus_rows)} buses named '{CAPTURE_BUS_NAME}' exist; "
            f"using {chosen} (others: {others}). Rename or remove the extras so "
            "the capture bus is unambiguous.")
    bus_id = bus_rows[0]["id"]

    shareset = _find_recorder_shareset(client)
    if not shareset:
        raise SystemExit(
            f"Recorder ShareSet '{RECORDER_SHARESET_NAME}' not found.\n"
            "Create it once (see README 'One-time setup'): a Wwise Recorder "
            "ShareSet under Effects, then rerun."
        )

    # Reroute the target to the capture bus (originals already saved on tgt).
    client.call("ak.wwise.core.object.setProperty",
                {"object": tgt.id, "property": "OverrideOutput", "value": True})
    client.call("ak.wwise.core.object.setReference",
                {"object": tgt.id, "reference": "OutputBus", "value": bus_id})
    log("Routed target -> capture bus")

    return CaptureRig(bus_id=bus_id, shareset_id=shareset)


def _find_recorder_shareset(client: WaapiClient) -> str | None:
    """Find the Recorder ShareSet by NAME (relocation-safe: it can live in any
    Effects Work Unit). Warns if the name is ambiguous, mirroring the bus lookup."""
    rows = waql(client,
                f'$ from type Effect where name = "{RECORDER_SHARESET_NAME}"',
                ["id", "name", "path"])
    if not rows:
        return None
    if len(rows) > 1:
        chosen = rows[0].get("path", rows[0]["id"])
        others = ", ".join(r.get("path", r["id"]) for r in rows[1:])
        log(f"WARNING: {len(rows)} effects named '{RECORDER_SHARESET_NAME}' exist; "
            f"using {chosen} (others: {others}). Rename or remove the extras so "
            "the Recorder ShareSet is unambiguous.")
    return rows[0]["id"]


def _arm_recorder(client: WaapiClient, rig: CaptureRig) -> None:
    client.call("ak.wwise.core.object.setProperty",
                {"object": rig.bus_id, "property": BUS_BYPASS_PROPERTY,
                 "value": False})


def _flush_recorder(client: WaapiClient, rig: CaptureRig) -> None:
    """Bypass the capture bus effect to finalize the current take's file."""
    client.call("ak.wwise.core.object.setProperty",
                {"object": rig.bus_id, "property": BUS_BYPASS_PROPERTY,
                 "value": True})


def _set_recorder_output(client: WaapiClient, rig: CaptureRig, out_path: str) -> None:
    client.call("ak.wwise.core.object.setProperty", {
        "object": rig.shareset_id,
        "property": RECORDER_FILE_PROPERTY,
        "value": out_path,
    })


# ---------------------------------------------------------------------------
# Transport playback  ->  one finalized WAV per take
# ---------------------------------------------------------------------------

def capture_takes(client: WaapiClient, tgt: Target, rig: CaptureRig,
                  opts: RunOptions) -> list[str]:
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    safe = _safe_name(tgt.name)

    transport = client.call("ak.wwise.core.transport.create", {"object": tgt.id})
    transport_id = transport["transport"]
    take_files: list[str] = []
    try:
        for i in range(1, opts.variations + 1):
            out = os.path.join(SCRATCH_DIR, f"{safe}{COMP_SUFFIX}_{i:02d}.wav")
            if os.path.exists(out):
                os.remove(out)
            _set_recorder_output(client, rig, out)
            _arm_recorder(client, rig)                        # bypass off

            log(f"Take {i}/{opts.variations} -> {os.path.basename(out)}")
            client.call("ak.wwise.core.transport.executeAction",
                        {"transport": transport_id, "action": "play"})
            _wait_for_take(client, transport_id, opts)
            client.call("ak.wwise.core.transport.executeAction",
                        {"transport": transport_id, "action": "stop"})

            _flush_recorder(client, rig)                      # bypass on -> flush
            time.sleep(INTER_TAKE_PAUSE)

            if os.path.exists(out):
                take_files.append(out)
            else:
                log(f"  WARNING: no file written for take {i} (see VERIFY-LIVE).")
    finally:
        client.call("ak.wwise.core.transport.destroy",
                    {"transport": transport_id})
    return take_files


def _wait_for_take(client: WaapiClient, transport_id: str, opts: RunOptions) -> None:
    """Record until the take plays out — one-shots and finite loops end naturally
    (transport goes idle) and get their tail. `record_length` (given only when an
    infinite loop is present) is a CAP, not a fixed duration: content that finishes
    before it still stops at idle, so non-looping layers are never cut short. With
    no record_length, AUTO_MAX_DURATION is the safety cap."""
    cap = opts.record_length if opts.record_length is not None else AUTO_MAX_DURATION
    start = time.monotonic()
    # Wait until it actually starts...
    while time.monotonic() - start < WARMUP_TIMEOUT:
        if _state(client, transport_id) == "playing":
            break
        time.sleep(TRANSPORT_POLL)
    # ...then until it plays out (idle) or we hit the cap.
    play_start = time.monotonic()
    reached_idle = False
    while time.monotonic() - play_start < cap:
        if _state(client, transport_id) != "playing":
            reached_idle = True
            break
        time.sleep(TRANSPORT_POLL)
    if reached_idle:                       # natural end -> keep the tail
        time.sleep(IDLE_TAIL)


def _state(client: WaapiClient, transport_id: str) -> str:
    res = client.call("ak.wwise.core.transport.getState",
                      {"transport": transport_id})
    return (res or {}).get("state", "")


# ---------------------------------------------------------------------------
# Import  ->  new comped Random container
# ---------------------------------------------------------------------------

def import_takes(client: WaapiClient, tgt: Target, take_files: list[str]) -> str:
    comp = f"{_safe_name(tgt.name)}{COMP_SUFFIX}"
    subfolder = tgt.originals_subfolder or _safe_name(tgt.name)
    imports = []
    for wav in take_files:
        stem = os.path.splitext(os.path.basename(wav))[0]
        imports.append({
            "audioFile": wav,
            "objectPath": (
                f"{tgt.path}\\<Random Container>{comp}\\<Sound SFX>{stem}"
            ),
            "originalsSubFolder": subfolder,
        })
    client.call("ak.wwise.core.audio.import", {
        "importOperation": "createNew",
        "default": {"importLanguage": "SFX"},
        "imports": imports,
    })
    log(f"Imported {len(take_files)} takes into '{comp}'")
    rows = waql(client, f'$ "{tgt.id}" select children where name = "{comp}"', ["id"])
    return rows[0]["id"] if rows else ""


# ---------------------------------------------------------------------------
# Hierarchy voice-gain compensation  (see VOLUME_COMPENSATION in CONFIG)
# ---------------------------------------------------------------------------

def _compensation(tgt: Target) -> float:
    """dB to add so the re-imported comp plays at the original level: the negative
    of the summed hierarchy voice gain (Volume + Make-Up Gain, target+ancestors),
    which the bounce bakes in and the re-import re-applies."""
    return -tgt.hierarchy_gain


def apply_pre_bounce_compensation(client: WaapiClient, tgt: Target,
                                  opts: "RunOptions") -> None:
    """"pre" mode: temporarily boost the target's Voice Volume so the recording
    excludes the inherited gain (teardown restores it). Skipped when the user
    asked to apply the compensation post-bounce as Make-Up Gain instead."""
    comp = _compensation(tgt)
    if opts.comp_as_makeup or VOLUME_COMPENSATION != "pre" or abs(comp) < 1e-6:
        return
    client.call("ak.wwise.core.object.setProperty",
                {"object": tgt.id, "property": "Volume",
                 "value": tgt.own_volume + comp})
    tgt.pre_comp_applied = True
    log(f"Pre-bounce compensation: target Volume {tgt.own_volume:+.2f} -> "
        f"{tgt.own_volume + comp:+.2f} dB")


def apply_post_import_compensation(client: WaapiClient, comp_id: str,
                                   tgt: Target, opts: "RunOptions") -> None:
    """Post-import compensation on the _COMP container. Recording stays at natural
    level (never clips). Applied as Make-Up Gain when the user ticked the option
    (`opts.comp_as_makeup`, overrides the config mode), otherwise as Voice Volume
    only when VOLUME_COMPENSATION == "post"."""
    comp = _compensation(tgt)
    if abs(comp) < 1e-6 or not comp_id:
        return
    if opts.comp_as_makeup:
        client.call("ak.wwise.core.object.setProperty",
                    {"object": comp_id, "property": "MakeUpGain", "value": comp})
        log(f"Post-import compensation: {_safe_name(tgt.name)}{COMP_SUFFIX} "
            f"Make-Up Gain = {comp:+.2f} dB")
        return
    if VOLUME_COMPENSATION != "post":
        return
    client.call("ak.wwise.core.object.setProperty",
                {"object": comp_id, "property": "Volume", "value": comp})
    log(f"Post-import compensation: {_safe_name(tgt.name)}{COMP_SUFFIX} "
        f"Voice Volume = {comp:+.2f} dB")


# ---------------------------------------------------------------------------
# Disable originals  ->  move into a disabled PRECOMP container inside the target
# ---------------------------------------------------------------------------

def disable_sources(client: WaapiClient, tgt: Target) -> None:
    """Group the bounced source containers into a `PRECOMP_NAME` container created
    INSIDE the target, and set its Inclusion=false — so they leave the target's
    live structure and drop out of SoundBanks (Inclusion is hierarchical, so one
    flag covers them all), while staying in the project as an editable backup.
    A container (not a Folder) is used because a Folder can't nest inside a
    playback container and Wwise would relocate it outside the target."""
    if not tgt.sources:
        log("No source child-containers to disable.")
        return

    precomp = client.call("ak.wwise.core.object.create", {
        "parent": tgt.id,
        "type": "BlendContainer",
        "name": PRECOMP_NAME,
        "onNameConflict": "merge",
    })
    precomp_id = precomp["id"]

    for src in tgt.sources:
        client.call("ak.wwise.core.object.move",
                    {"object": src["id"], "parent": precomp_id})
        log(f"Moved source '{src['name']}' -> {PRECOMP_NAME}")

    # Exclude the whole PRECOMP container from SoundBanks (property confirmed live
    # as "Inclusion"; hierarchical, so descendants are excluded too).
    client.call("ak.wwise.core.object.setProperty",
                {"object": precomp_id, "property": "Inclusion", "value": False})
    log(f"Set {PRECOMP_NAME} Inclusion = false")


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def teardown(client: WaapiClient, tgt: Target, rig: CaptureRig | None) -> None:
    # Restore the target's Voice Volume if pre-bounce compensation boosted it.
    if tgt is not None and tgt.pre_comp_applied:
        try:
            client.call("ak.wwise.core.object.setProperty",
                        {"object": tgt.id, "property": "Volume",
                         "value": tgt.own_volume})
            log(f"Restored target Voice Volume ({tgt.own_volume:+.2f} dB)")
        except Exception as e:
            log(f"WARNING: could not restore target Voice Volume: {e}")
    if rig is None:
        return
    # Restore the target's original routing (order: set bus while overriding,
    # then restore the original override flag so no dangling ref remains).
    try:
        if tgt.output_bus:
            client.call("ak.wwise.core.object.setReference",
                        {"object": tgt.id, "reference": "OutputBus",
                         "value": tgt.output_bus})
        client.call("ak.wwise.core.object.setProperty",
                    {"object": tgt.id, "property": "OverrideOutput",
                     "value": tgt.override_output})
        log("Restored target routing")
    except Exception as e:
        log(f"WARNING: could not fully restore routing: {e}")
    # Leave the capture bus effect un-bypassed (its idle default). The capture
    # bus is a permanent one-time-setup object and is NOT deleted.
    try:
        client.call("ak.wwise.core.object.setProperty",
                    {"object": rig.bus_id, "property": BUS_BYPASS_PROPERTY,
                     "value": False})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "Container"


def _trim_wavs(files: list[str]) -> None:
    """Best-effort lead/tail silence trim (stdlib only, no numpy dependency)."""
    for f in files:
        try:
            _trim_one(f)
        except Exception as e:
            log(f"  (trim skipped for {os.path.basename(f)}: {e})")


def _trim_one(path: str, thresh: int = 512) -> None:
    with wave.open(path, "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    if params.sampwidth != 2:
        return  # only handle 16-bit here; leave others untouched
    import array
    a = array.array("h")
    a.frombytes(frames)
    ch = params.nchannels
    # find first/last frame above threshold
    n = len(a) // ch
    first, last = 0, n - 1
    while first < n and max(abs(a[first * ch + c]) for c in range(ch)) < thresh:
        first += 1
    while last > first and max(abs(a[last * ch + c]) for c in range(ch)) < thresh:
        last -= 1
    if first == 0 and last == n - 1:
        return
    sliced = a[first * ch:(last + 1) * ch]
    with wave.open(path, "wb") as w:
        w.setparams(params)
        w.writeframes(sliced.tobytes())


# ---------------------------------------------------------------------------
# UI  (tiny tkinter prompt; falls back to CLI args in --no-gui)
# ---------------------------------------------------------------------------

def prompt_options(tgt: Target, args) -> RunOptions:
    if args.no_gui:
        rec_len = args.record_length
        if tgt.has_infinite_loop and rec_len is None:
            raise SystemExit(
                "Target has an infinitely-looping source; pass --record-length "
                "SECONDS"
            )
        return RunOptions(args.variations, rec_len, args.dry_run,
                          args.comp_makeup)

    import tkinter as tk
    from tkinter import ttk

    result: dict = {}
    root = tk.Toplevel(_TK_ROOT)
    root.title("Bounce Layers")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text=f"Target:  {tgt.name}  ({tgt.type})").grid(
        column=0, row=0, columnspan=2, sticky="w", pady=(0, 8))

    ttk.Label(frm, text="Variations:").grid(column=0, row=1, sticky="w")
    var_n = tk.StringVar(value=str(args.variations))
    ttk.Entry(frm, textvariable=var_n, width=8).grid(column=1, row=1, sticky="w")

    rec_var = tk.StringVar(value=str(args.record_length or 3.0))
    if tgt.has_infinite_loop:
        ttk.Label(frm, text="Record length (s):\n(infinite-loop cap)").grid(
            column=0, row=2, sticky="w")
        ttk.Entry(frm, textvariable=rec_var, width=8).grid(
            column=1, row=2, sticky="w")

    dry = tk.BooleanVar(value=args.dry_run)
    ttk.Checkbutton(frm, text="Test Run (don't collect originals)", variable=dry).grid(
        column=0, row=3, columnspan=2, sticky="w", pady=(8, 0))

    # Optional: apply the hierarchy-gain compensation post-bounce as Make-Up Gain
    # on the _COMP container. Only offered when there IS inherited gain to cancel.
    comp = _compensation(tgt)
    makeup = tk.BooleanVar(value=args.comp_makeup)
    if VOLUME_COMPENSATION != "off" and abs(comp) > 1e-6:
        ttk.Checkbutton(
            frm,
            text=(f"Post-bounce gain compensation "
                  f"({comp:+.2f} dB)"),
            variable=makeup,
        ).grid(column=0, row=4, columnspan=2, sticky="w", pady=(4, 0))

    if tgt.rerouting_descendants:
        names = "\n".join(f"  • {d['name']}" for d in tgt.rerouting_descendants[:6])
        more = ("\n  …" if len(tgt.rerouting_descendants) > 6 else "")
        warn = tk.Label(
            frm, justify="left", fg="#b00",
            text=(f"⚠ {len(tgt.rerouting_descendants)} descendant(s) override their "
                  f"Output Bus and\nwon't be captured (they route elsewhere):\n"
                  f"{names}{more}"))
        warn.grid(column=0, row=5, columnspan=2, sticky="w", pady=(8, 0))

    def ok():
        result["variations"] = max(1, int(var_n.get()))
        result["record_length"] = (
            float(rec_var.get()) if tgt.has_infinite_loop else None)
        result["dry_run"] = dry.get()
        result["comp_as_makeup"] = makeup.get()
        root.destroy()

    def cancel():
        # Leave `result` empty; the post-mainloop check turns that into a clean
        # SystemExit. (Raising inside a Tk callback is swallowed by Tk, so don't.)
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=6, columnspan=2, sticky="ew", pady=(12, 0))
    # "?" help button at the bottom-left; opens the tutorial window.
    ttk.Button(btns, text="?", width=3,
               command=lambda: _show_help(root, tgt)).grid(
                   column=0, row=0, sticky="w")
    ttk.Frame(btns).grid(column=1, row=0)          # spacer
    btns.columnconfigure(1, weight=1)
    ttk.Button(btns, text="Bounce", command=ok).grid(column=2, row=0, padx=4)
    ttk.Button(btns, text="Cancel", command=cancel).grid(column=3, row=0, padx=4)
    root.bind("<Return>", lambda e: ok())
    # Closing the window (the "X") is a clean cancel, not an orphaned process.
    root.protocol("WM_DELETE_WINDOW", cancel)
    # Force the dialog in front of the Wwise window so it can never get lost
    # behind it (a hidden dialog = a process stuck in mainloop = a wedge).
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.after(600, lambda: root.attributes("-topmost", False))
    _TK_ROOT.wait_window(root)

    if not result:
        raise SystemExit("Cancelled.")
    return RunOptions(result["variations"], result["record_length"],
                      result["dry_run"], result.get("comp_as_makeup", False))


# ---------------------------------------------------------------------------
# Help / tutorial window (opened by the "?" button)
# ---------------------------------------------------------------------------

def _show_help(parent, tgt: Target) -> None:
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(parent)
    win.title("Bounce Layers — How it works")
    win.transient(parent)
    win.resizable(True, True)

    frm = ttk.Frame(win, padding=12)
    frm.grid(sticky="nsew")
    win.rowconfigure(0, weight=1)
    win.columnconfigure(0, weight=1)
    frm.rowconfigure(0, weight=1)
    frm.columnconfigure(0, weight=1)

    txt = tk.Text(frm, wrap="word", width=76, height=30,
                  padx=10, pady=8, relief="flat")
    sb = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.grid(column=0, row=0, sticky="nsew")
    sb.grid(column=1, row=0, sticky="ns")

    txt.tag_configure("h1", font=("Segoe UI", 12, "bold"), spacing3=6,
                      spacing1=10)
    txt.tag_configure("h2", font=("Segoe UI", 10, "bold"), spacing3=3,
                      spacing1=8)
    txt.tag_configure("body", font=("Segoe UI", 9), spacing3=3)
    txt.tag_configure("bul", font=("Segoe UI", 9), lmargin1=16, lmargin2=30,
                      spacing3=3)

    def h1(s): txt.insert("end", s + "\n", "h1")
    def h2(s): txt.insert("end", s + "\n", "h2")
    def p(s):  txt.insert("end", s + "\n", "body")
    def b(s):  txt.insert("end", "•  " + s + "\n", "bul")

    h1("Bounce Layers")
    p("Flatten a multi-layered sound (e.g. a Blend of Random containers) into "
      "a single container of N variations. "
      "The tool captures the container's LIVE "
      "output through the Wwise Recorder, re-imports the takes, and disables the "
      "originals.")

    h2("What it does, step by step")
    b("Temporarily reroutes the target's Output Bus to a dedicated capture bus "
      "(\"" + CAPTURE_BUS_NAME + "\"), so "
      "the layers are summed protected from adjustments in the mix structure.")
    b("Transport triggers the target N times.")
    b(f"Imports the takes into a new Random container \"<Name>{COMP_SUFFIX}\" "
      f"under the target (takes named \"<Name>{COMP_SUFFIX}_01\", _02, …).")
    b(f"Groups the original source layers into a disabled \"{PRECOMP_NAME}\" "
      f"container inside the target. ")
    b("Restores the target's routing and volume")

    h2("User Inputs:")
    h2("Variations")
    b("How many takes to record. Each play samples the existing Random/Blend "
      "logic, so repeats are possible.")
    h2("Record length (shown only for infinite loops)")
    b("Set a fixed duration per take. "
      "This option is not shown when bouncing one-shots and finite loops.")
    h2("Test Run")
    b("Capture and import the takes but skip the collection of the original containers. Recommended "
      "for a test pass, so you can audition the result before committing.")
    h2("Gain Compensation")
    b("Gain adjustments inherited from the container hierarchy cannot be avoided. "
      "Without compensation, the bounce would become louder or quieter than the source it is intending to mirror. "
      "By default, compensation is applied PRE-bounce (temporarily boosting the "
      "target's Voice Volume during capture); however, this can clip if the source "
      "files are already loud/maximized. Enable this option to instead without "
      "pre-bounce compensation and add Make-Up Gain to the "
      f"\"<Name>{COMP_SUFFIX}\" container POST-bounce instead. This option is only shown when the target "
      "actually inherits gain modification.")

    if tgt.rerouting_descendants:
        h2("Output-bus override warning")
        b("One or more descendants override their own Output Bus, so they route "
          "elsewhere and WON'T be captured in the bounce. Clear those overrides "
          "first if you want them included.")

    txt.configure(state="disabled")
    ttk.Button(frm, text="Close", command=win.destroy).grid(
        column=0, row=1, columnspan=2, pady=(10, 0))
    win.bind("<Escape>", lambda e: win.destroy())
    win.lift()
    win.focus_force()
    win.grab_set()


# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------
# A Windows named mutex, owned by the OS and released automatically when this
# process dies. So even a hard-killed / orphaned run leaves NO stale lock, and a
# second launch (while a dialog is still open behind Wwise, say) is refused
# instead of piling up and wedging the Wwise command.

_SINGLE_INSTANCE_MUTEX = "BounceLayers_SingleInstance_v1"


def _acquire_single_instance():
    """Become the one running instance. Returns an opaque handle on success, or
    None if another instance already holds the lock. Fails OPEN (returns a truthy
    sentinel) off Windows or if the guard itself errors — never block the tool
    over the guard."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        ERROR_ALREADY_EXISTS = 183
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True  # couldn't create it -> don't block the run
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)   # we're the loser; drop our handle
            return None
        return handle                       # keep alive for the process lifetime
    except Exception:
        return True


def _notify_already_running(no_gui: bool) -> None:
    msg = ("Bounce Layers is already running.\n\n"
           "Finish or close that run first. If nothing is visibly open, a "
           "previous run may be stuck behind the Wwise window or orphaned — "
           "look for a 'Bounce Layers' dialog (Alt+Tab), or end any stray "
           "python.exe / pythonw.exe in Task Manager.")
    log("Another instance already holds the single-instance lock; aborting.")
    if no_gui:
        return
    try:
        from tkinter import messagebox
        _TK_ROOT.attributes("-topmost", True)
        messagebox.showwarning("Bounce Layers — already running", msg)
        _TK_ROOT.attributes("-topmost", False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Bounce a multi-layer Wwise "
                                             "container into a baked Random container.")
    ap.add_argument("target", nargs="?", help="Target object GUID {id}. "
                    "If omitted, uses the current Wwise selection.")
    ap.add_argument("-n", "--variations", type=int, default=DEFAULT_VARIATIONS)
    ap.add_argument("--record-length", type=float, default=None,
                    help="Duration for loops (seconds).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Capture + import but do move and disable originals.")
    ap.add_argument("--comp-makeup", action="store_true",
                    help="Apply gain compensation post-bounce"
                         "(default is pre-bounce).")
    ap.add_argument("--no-gui", action="store_true",
                    help="Skip the tkinter prompt; use CLI args only.")
    ap.add_argument("--url", default=WAAPI_URL)
    args = ap.parse_args()

    # Refuse to start if another run is already going (see the guard above). The
    # handle is kept in `lock` for the whole process lifetime; the OS releases it
    # on exit, so there is nothing to clean up.
    lock = _acquire_single_instance()          # noqa: F841 (held, not used)
    if lock is None:
        _notify_already_running(args.no_gui)
        return 3

    try:
        with WaapiClient(url=args.url) as client:
            return _run(client, args)
    except CannotConnectToWaapiException:
        showerror('Error', 'Could not establish the WAAPI connection. '
                           'Is the Wwise Authoring Tool running?')
        return 2
    except RuntimeError as e:
        showerror('Error', f'{e}')
        return 1
    except Exception as e:
        import traceback
        showerror('Error', f'{e}\n\n{traceback.format_exc()}')
        return 1


def _run(client: WaapiClient, args) -> int:
    """Do the bounce with an open WAAPI client. Always restores routing, ends the
    undo group, and clears scratch WAVs on every exit path — main()'s context
    manager handles the disconnect."""
    rig = None
    tgt = None
    undo_open = False
    takes: list[str] = []
    rc = 0
    try:
        tgt = resolve_target(client, _clean_guid(args.target))
        log(f"Target: {tgt.name} ({tgt.type}); "
            f"{len(tgt.sources)} source child-container(s); "
            f"infinite-loop={tgt.has_infinite_loop}")
        for d in tgt.looping_sources:
            if not d.get("_active"):
                kind = "disabled (excluded) -> ignored"
            elif d.get("@IsLoopingInfinite"):
                kind = "infinite + active -> needs record-length cap"
            else:
                kind = f"finite ({d.get('@LoopCount')}x) -> plays out"
            log(f"    looping: {d['name']} ({d['type']}) [{kind}]")
        if tgt.rerouting_descendants:
            log("WARNING: these descendants override their Output Bus and will "
                "not be captured. Disable Output Bus Override to include them."
                "capture bus):")
            for d in tgt.rerouting_descendants:
                bus = (d.get("@OutputBus") or {}).get("name", "?")
                log(f"    - {d['name']} ({d['type']}) -> {bus}")

        if VOLUME_COMPENSATION != "off" and abs(tgt.hierarchy_gain) > 1e-6:
            log(f"Hierarchy voice gain (Volume+Make-Up over target+ancestors): "
                f"{tgt.hierarchy_gain:+.2f} dB -> compensating {_compensation(tgt):+.2f} "
                f"dB ({VOLUME_COMPENSATION})")

        opts = prompt_options(tgt, args)
        log(f"Options: variations={opts.variations}, "
            f"record_length={opts.record_length}, dry_run={opts.dry_run}, "
            f"comp_as_makeup={opts.comp_as_makeup}")

        client.call("ak.wwise.core.undo.beginGroup", {})
        undo_open = True
        rig = build_capture_rig(client, tgt)
        apply_pre_bounce_compensation(client, tgt, opts)
        takes = capture_takes(client, tgt, rig, opts)
        if not takes:
            raise SystemExit("No takes were captured; aborting before import.")
        _trim_wavs(takes)
        comp_id = import_takes(client, tgt, takes)
        apply_post_import_compensation(client, comp_id, tgt, opts)

        if opts.dry_run:
            log("Dry run: leaving original sources in place.")
        else:
            disable_sources(client, tgt)
    except SystemExit as e:
        log(f"Stopped: {e}")
        rc = 1
    except Exception as e:
        log(f"ERROR: {e}")
        rc = 1
    finally:
        # Always undo whatever we touched on every exit path — a half-restored
        # project is what leaves Wwise in a stuck state. (main()'s WaapiClient
        # context manager disconnects the session.)
        _safe_teardown(client, tgt, rig)
        # Clean up scratch WAVs (originals are now copied into the project).
        for f in takes:
            try:
                os.remove(f)
            except OSError:
                pass
        _end_undo(client, undo_open)
        log("Done." if rc == 0 else "Aborted.")
    return rc


def _end_undo(client, undo_open: bool) -> None:
    if not undo_open:
        return
    try:
        client.call("ak.wwise.core.undo.endGroup",
                    {"displayName": "Bounce Layers"})
    except Exception as e:
        log(f"WARNING: could not end undo group: {e}")


def _safe_teardown(client, tgt, rig) -> None:
    if tgt is not None:
        try:
            teardown(client, tgt, rig)
        except Exception as e:
            log(f"WARNING during teardown: {e}")


def _clean_guid(s: str | None) -> str | None:
    """Normalize the target arg. Returns a {GUID}/path, or None to fall back to
    the current Wwise selection (also when an unsubstituted ${...} token leaks
    through, or the arg is blank)."""
    if not s:
        return None
    s = s.strip().strip('"')
    if not s or "${" in s:
        return None
    if re.fullmatch(r"[0-9A-Fa-f\-]{36}", s):
        return "{" + s + "}"
    return s


# Entry point. The `if __name__ != '__main__'` guard at the top already aborts an
# import, so this runs unconditionally.
_code = main()
try:
    _TK_ROOT.destroy()
except Exception:
    pass
try:
    sys.stdout.flush()
    sys.stderr.flush()
except Exception:
    pass
# Hard-exit so a lingering waapi-client or tkinter background thread can NEVER
# keep this process alive after the work is done — a hung process is exactly
# what wedges the Wwise command (redirectOutputs waits for it to exit). All
# cleanup already ran in main()'s finally block, so this is safe.
os._exit(_code)
