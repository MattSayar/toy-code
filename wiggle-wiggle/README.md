# wiggle-wiggle (with Google Photos support)

Find and extract **wiggle stereographs** (a.k.a. *wigglegrams*) hiding in your
photo library, and export them as looping boomerang GIFs.

A wigglegram is an animation that fakes a 3D effect by rapidly cycling through a
short burst of nearly-identical photos shot from slightly different viewpoints.
You make them by accident all the time — a quick burst, a couple of hand-held
frames of the same scene, the leftovers of a Live Photo. This tool finds those
runs automatically.

This is a re-imagining of [JCLemme/wiggle-wiggle](https://github.com/JCLemme/wiggle-wiggle),
which read from a local directory or a macOS/iCloud library. **This version keeps
local-directory mode and adds Google Photos as a first-class source**, so it
works off-Mac and against your cloud library.

![example wigglegram](https://github.com/JCLemme/wiggle-wiggle/raw/master/media/example.gif)

## How it works

The same two-step rhythm as the original:

1. **`hash`** — scan a source, fingerprint every photo with a perceptual hash
   (pHash), and cache the result in `_hashes.json`. Re-running only fingerprints
   newly-added photos.
2. **`export`** — sort by capture time, find runs of consecutive frames whose
   hashes are within a distance threshold *and* shot within a few seconds of each
   other, then write each run out as a boomerang GIF.

There's also a **`list`** action to preview the detected candidates without
writing any files.

## Install

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

The Google-auth packages are only needed for the Google Photos source; local
directory mode works with just Pillow + ImageHash + pillow-heif.

## Usage

### Local directory

```bash
python wigglewiggle.py -d ~/Pictures hash
python wigglewiggle.py -d ~/Pictures list      # preview candidates
python wigglewiggle.py -d ~/Pictures export    # writes GIFs to ./wigglegrams
```

### Google Photos

1. In [Google Cloud Console](https://console.cloud.google.com/): create a
   project, **enable the Photos Library API**, and create an **OAuth client ID**
   of type *Desktop app*. Download the JSON as `client_secrets.json` next to the
   script (or point at it with `--client-secrets`).
2. Run it — the first run opens a browser for read-only consent and caches the
   token in `google_photos_token.json`:

```bash
python wigglewiggle.py --google-photos hash
python wigglewiggle.py --google-photos list
python wigglewiggle.py --google-photos export
```

Use `--max-items N` to cap the scan while testing.

> **Heads up on Google Photos API access:** since 2025 the `photoslibrary.readonly`
> scope only returns media your *own app* created, unless your Cloud project still
> holds the legacy broad-access grant. If a full-library listing comes back empty,
> that restriction is why. For brand-new projects Google now steers you toward the
> [Photos Picker API](https://developers.google.com/photos/picker/guides/get-started),
> where the user hand-picks an album/session in a Google-hosted UI and the app
> reads only those items. This tool implements the Library `mediaItems.list` flow
> (which still works for app-created content and legacy grants); swapping in the
> Picker session id is a small, self-contained change to `GooglePhotosSource`.

## Tuning detection

| flag | default | meaning |
|------|---------|---------|
| `--distance` | `8` | max perceptual-hash distance between frames (lower = stricter) |
| `--min-frames` | `2` | minimum frames to count as a wigglegram |
| `--max-frames` | `12` | cap frames per wigglegram |
| `--max-gap` | `4.0` | max seconds between consecutive frames |
| `--frame-ms` | `100` | GIF frame duration (ms) |
| `--no-boomerang` | off | play forward only instead of forward+reverse |
| `--out` | `wigglegrams` | output directory |
| `--cache` | `_hashes.json` | hash cache file |

If you're getting too many false positives, lower `--distance` and/or
`--max-gap`. If real bursts are being missed, raise them.

## Notes / limitations

- HEIC/HEIF is supported via `pillow-heif`.
- Capture time comes from EXIF (`DateTimeOriginal`) and falls back to file mtime,
  so directory mode is only as good as your files' timestamps.
- Videos are skipped — wigglegrams are built from stills.

## Files

- `wigglewiggle.py` — the whole tool (source abstraction, hashing, detection, export)
- `requirements.txt` — dependencies
