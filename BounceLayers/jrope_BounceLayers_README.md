## What Do

A Wwise Authoring tool that flattens a **multi-layer container** (e.g. a Blend
container holding several Random containers) into a single **baked Random
container** of pre-rendered variation takes. It does this by temporarily rerouting the selected container to a dedicated Recorder bus, capruing the output through the **Wwise Recorder** plugin, then re-imports the recorded takes in place and optionally disables the original container.

```
Select a container ─▶ right-click ─▶ JROPE ▸ Bounce Layers to Single Layer
      │
      ├─ temp private bus + Recorder  (isolated, sums the layers)
      ├─ transport-play N times        (one finalized WAV per take)
      ├─ import → <Name>_COMP          (Random container of takes)
      └─ originals → preComp (inside target, disabled + excluded)  [skipped in Dry run]
```

## Files

| File | Purpose |
|---|---|
| `jrope_BounceLayers.py` | The primary script. |
| `jrope_BounceLayers_setup.py` | One-time setup: creates the capture bus + Recorder ShareSet. |
| `jrope_BounceLayers.json` | Wwise Command Add-on (right-click menu definition). |
| `jrope_BounceLayers_README.md` | This file. |


## Requirements

- **Wwise Authoring running** with WAAPI enabled: *Project → User Preferences →
  Enable Wwise Authoring API* (default port **8080**).
- **Python 3.14** at `C:\Program Files\Python314\python.exe`.
- `waapi-client`:
  ```
  "C:\Program Files\Python314\python.exe" -m pip install waapi-client
  ```

## First-time setup

1. **Enable WAAPI** (see above) and restart Wwise if you just turned it on.

2. **Install the Command Add-on** — copy `jrope_BounceLayers.json`
   and `jrope_BounceLayers.py` into a Wwise Commands folder, keeping them
   together (the manifest uses `${CurrentCommandDirectory}` to find the script). Where you put them depends on how broadly accessible you want them to be:
   - Project: `<YourProject>\Add-ons\Commands\`  ← recommended
   - User: `%APPDATA%\Audiokinetic\Wwise\Add-ons\Commands\`
   - Install-wide: `<WwiseInstall>\Authoring\Data\Add-ons\Commands\`

   Then reload command add-ons (type "> Reload" in the client Search box).

3. **Create the capture bus + Recorder ShareSet.** Easiest is the setup script. With Wwise open, run jrope_BounceLayers_setup.py. This creates the bus and shareset and wires them together.
   ```
   "C:\Program Files\Python314\python.exe" 
   ```
   It makes the `BounceLayers_CaptureBus` Audio Bus under the Master Audio Bus and
   the `BounceLayers_Recorder` Wwise Recorder ShareSet under Effects, then
   references the ShareSet from the bus's first effect slot. **Then save the
   project (Ctrl+S) and restart Wwise** — a bus created over WAAPI doesn't engage
   Wwise's live audio render until the project reloads.

   *Or create them by hand:*
   - *ShareSets* tab → **Effects** → any Work Unit → new **Wwise Recorder**
     ShareSet named **`BounceLayers_Recorder`** (must match `RECORDER_SHARESET_NAME`;
     found by name, so it can live in any Effects Work Unit).
   - Under **Master Audio Bus**, a child **Audio Bus** named
     **`BounceLayers_CaptureBus`** (must match `CAPTURE_BUS_NAME`; found by name, so
     it can live anywhere in the Master-Mixer hierarchy).
   - On that bus's **Effects** tab, add the `BounceLayers_Recorder` ShareSet, volume 0 dB, no other effects. 


## Usage

**From Wwise:** select the container → right-click → **JROPE ▸ Bounce Layers
to Single Layer**. You can also just run the main script from Python. A small dialog asks for:
- **Variations** — how many takes (N).
- **Record length** — shown only when an active source loops **infinitely**
  (`IsLoopingInfinite`); as an infinite loop never ends, you define a duration.
  One-shots and finite loops play out on their own. 
- **Dry run** — capture + import but don't disable/move the originals. 
- **Compensate as Make-Up Gain on Bounces** — shown only when the target inherits
  voice gain. Ticking it records at natural level and cancels the inherited gain
  *post-bounce* as **Make-Up Gain** on the new container instead of the default *pre-bounce* Voice Volume boost. CLI: `--comp-makeup`.
- **?** (bottom-left) — opens a tutorial window explaining the process and each
  input.

`redirectOutputs` is on, so the script's log appears in the Wwise log after it runs.

**From a terminal** (handy while validating):
```
"C:\Program Files\Python314\python.exe" jrope_BounceLayers.py "{GUID}" -n 8 --dry-run
"C:\Program Files\Python314\python.exe" jrope_BounceLayers.py --no-gui -n 6 --record-length 3
```
With no GUID it operates on the current Wwise selection. Everything runs inside a
single **undo group** named "Bounce Layers", so one Ctrl+Z reverts the whole run.


## Result

- A new Random container **`<Name>_COMP`** as a child of the target, containing
  one Sound SFX per take (`<Name>_COMP_01` … `<Name>_COMP_NN`). Originals for the
  takes land in the target's `Originals/SFX/<subfolder>` (mirrored from a sibling
  sound).
- The bounced source containers grouped into a disabled **`preComp`** container
  created *inside* the target (`Inclusion` off → excluded from SoundBanks), kept as
  an editable backup (unless `--dry-run`).


### CONFIG constants (modifyable at the top of `jrope_BounceLayers.py`)

Names you may want to change:

| Constant | Default | Effect |
|---|---|---|
| `COMP_SUFFIX` | `"_COMP"` | Comp container `"<Target>_COMP"` and take files `"<Target>_COMP_01"`, `_02`, … |
| `PRECOMP_NAME` | `"preComp"` | Name of the disabled container (inside the target) holding the original layers. |
| `VOLUME_COMPENSATION` | `"pre"` | Hierarchy voice-gain compensation (see below). `"pre"` \| `"post"` \| `"off"`. |

Setup constants (match what you created, confirmed live otherwise):

| Constant | What to confirm |
|---|---|
| `CAPTURE_BUS_NAME` | Name of the capture bus you created (Recorder as its only effect); found by name, so relocation-safe. |
| `RECORDER_SHARESET_NAME` | Name of the Recorder ShareSet you created; found by name, so it can live in any Effects Work Unit. |
| `RECORDER_FILE_PROPERTY` | `AuthoringFilename` (confirmed live) — the Recorder's authoring output path. |
| `BUS_BYPASS_PROPERTY` | `BypassEffect` — toggled to arm/flush the Recorder per take. |
| `${id}` (in the manifest) | That Wwise substitutes the object GUID; the script falls back to the current selection if not. |


## Notes / known limits

- **Hierarchy voice-gain compensation.** Voice Volume + Make-Up Gain set on the
  target *or any ancestor* (Actor-Mixers, Property/Blend/Random/Switch containers;
  Work Units and Folders carry none) is baked into the bounce by the transport, and
  then re-applied when the `_COMP` is re-imported under the same hierarchy — so
  without compensation the bounce plays back doubly attenuated (e.g. a −12 dB
  ancestor → −24 dB). The tool traces `target + ancestors`, sums `Volume + Make-Up
  Gain` (e.g. −12 dB Voice + +4 dB Make-Up = −8 dB net), and compensates by its
  negative (+8 dB). `VOLUME_COMPENSATION`:
  - `"pre"` (default) — temporarily boosts the target's Voice Volume during capture
    so the recording excludes the inherited gain; raw WAVs come out un-attenuated
    but **can clip** if that gain was providing headroom. *(Verified live: a −12 dB
    ancestor made the recording exactly +12 dB louder.)*
  - `"post"` — records at natural level and sets the `_COMP` container's Voice
    Volume to cancel the inherited gain instead; never clips.
  - `"off"` — no compensation.

  The GUI **"Compensate as Make-Up Gain on `_COMP`"** checkbox (or `--comp-makeup`)
  is a per-run override: when ticked, it records at natural level and applies the
  compensation *post-bounce* as **Make-Up Gain** on the `_COMP` container
  (regardless of the `VOLUME_COMPENSATION` mode) — like `"post"` but writing
  Make-Up Gain instead of Voice Volume, leaving the container's Voice Volume free.
- **Overridden output busses aren't captured.** Any descendant of the target that
  overrides its own Output Bus routes straight to that bus, bypassing the target's
  (rerouted) output — so it won't be in the bounce. The tool detects these and
  **warns before bouncing** (in the log and, in GUI mode, the dialog), listing each
  object and where it routes. Clear those overrides first if you want them included.
- **The capture bus must be pre-made** (the one-time setup). A bus created via WAAPI
  at runtime is not wired into Wwise's live audio render, so it records nothing —
  hence the tool reuses a hand-made bus rather than creating a throwaway one.
- **RTPC-driven Blend containers** bake at whatever the current/default blend
  value is; sweeping a Game Parameter across takes is out of scope.
- **No native region import.** Wwise cannot split one multi-region WAV into many
  objects on import (that is a REAPER/ReaWwise workflow), so takes are separated at
  record time (one finalized file per play), not by post-import region detection.
- **Coverage is sampled**, not exhaustive: N plays record whatever the existing
  Random/Blend logic produces (repeats possible) — matching in-game behavior.


## If the tool won't launch / Wwise seems stuck

The command is configured with `redirectOutputs: true`, so **Wwise waits for the
script process to exit** before it will run the command again. If a run's dialog
gets hidden behind Wwise or a process is orphaned, that wait never ends and the
command appears dead. Safeguards now in place:

- **Single-instance lock.** A second launch is refused (with a message) while one
  is already running, instead of piling up. The lock is a Windows named mutex the
  OS releases automatically when the process exits — a hard-killed run leaves no
  stale lock.
- **Dialog forced to the front.** The prompt is raised topmost / focused so it
  can't hide behind Wwise, and closing its **X** cancels cleanly.
- **Guaranteed cleanup + exit.** Routing/volume are always restored and WAAPI is
  always disconnected (a `finally` block), and the process hard-exits so no
  lingering background thread can keep it alive.

If it still gets wedged, clear it manually (PowerShell):
```powershell
# Kill any orphaned Bounce Layers script process
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'jrope_BounceLayers' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# Confirm Wwise (not another app) owns the WAAPI port
Get-NetTCPConnection -LocalPort 8080 |
  Select-Object State,OwningProcess,@{n='Proc';e={(Get-Process -Id $_.OwningProcess).ProcessName}}
```
Also Alt+Tab for a stray **"Bounce Layers"** window and close it. If WAAPI itself
stops responding (a client was hard-killed mid-session), fully quit Wwise — verify
no `Wwise.exe` remains in Task Manager — and relaunch; that's the only reliable
reset for a wedged WAAPI server.