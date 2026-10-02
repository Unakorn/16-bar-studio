# 16 Bar Studio · AI Full Song 4.5.1

A desktop MIDI arranger for turning two musical patterns and an optional chord variation into a full song. Assign track roles, choose manual pattern order or an AI arrangement, and export aligned MIDI parts for your DAW.

## Updated in 4.5.1

Eight-bar FL Studio MIDI patterns no longer gain a ninth bar when their only late events are a complete duplicate instrument-setup packet. The repair checks that the packet matches the bar boundary and falls within a bounded one-beat tail. Genuine later notes, meaningful expression, held pedals, and standalone end markers keep their duration. The classic import path uses the same check.

Original MIDI files and their note data remain unchanged; normalization applies to the working arrangement only.

## Run

Use Python 3.10 or newer with Tkinter. The standard Windows Python installer includes Tkinter.

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

On Windows, `Start.cmd` uses the local `.venv` when available, otherwise `python` on PATH. Optional drag-and-drop support is available by installing `tkinterdnd2`; file browse buttons work without it. The desktop app is designed for Windows. Core MIDI processing uses portable Python; folder-opening actions use Windows integration.

## Make an arrangement

1. Load Master A and Master B MIDI, plus a chord-variation MIDI if wanted.
2. Mark tracks as Chords, Drums, Keep, or the appropriate musical role. Review custom drum-note labels.
3. Choose the target length and AI direction, or build a manual order from A, B, AC, and BC.
4. Export to a new song folder, then import the MIDI into FL Studio or another DAW from the same song start.

The export includes the full song, pattern/reference MIDI, separate instrument parts, editing reports, and a `Song Structure.json` handoff for compatible lyric writers. MIDI does not include instruments, samples, presets, or audio. Choose those in your DAW. Source MIDI is protected from overwriting.

AI phrase edits make musical rests and handoffs. Drum trimming removes selected existing hits; retained drum hits preserve their timing, pitch, length, and velocity. Built-in “My arrangement style” and “My drum style” options use readable arranging preferences inferred from example edits; they are not trained models. Source reference songs and their file fingerprints are not included.

The classic arranger remains available through `python app.py --classic`. Its automation files are MIDI control data; compatible receiver plugins are separate products and are not included here.

## Optional AI connection

Manual arrangements work locally without a key. AI arranging uses the OpenAI Responses API and requires your own API access. Set `OPENAI_API_KEY` locally or enter a session-only override in the window. On Windows, the app can also read that saved user/system environment variable. Do not commit keys or local configuration.

AI requests send musical metadata such as track names, note information, and your creative direction. No account credentials or user music are bundled in this repository. Model access is account-dependent; the model field can be changed. Copy-prompt and paste-JSON controls also support arranging through your own AI chat, with local validation before export.

## Tests

```text
python -m unittest discover -s tests -p "test_*.py"
```

Tests create synthetic MIDI in temporary folders and mock API calls. Optional Tk widget tests require an explicit `--ui` run. `licenses/` preserves notices from the original distribution; dependency executables and runtimes are not bundled.

## License

No application license has been selected for this source release. Public visibility alone does not grant a license to reuse, modify, or redistribute it. Existing third-party notices, where supplied, are retained and apply to their respective components.
