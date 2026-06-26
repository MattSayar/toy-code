#!/usr/bin/env python3
"""wigglewiggle - find and extract wiggle stereographs (wigglegrams) from your photos.

A wigglegram is an animated image that fakes a 3D effect by rapidly cycling
through a short burst of nearly-identical photos taken from slightly different
viewpoints. People shoot them all the time without meaning to: a quick burst, a
few hand-held frames of the same scene, the "live photo" stragglers, etc.

This tool scans a photo source, fingerprints every image with a perceptual hash,
finds runs of consecutive near-duplicate frames, and exports the good ones as
looping boomerang GIFs.

It is a re-imagining of JCLemme's `wiggle-wiggle` (https://github.com/JCLemme/wiggle-wiggle),
which read from a local directory or an iCloud library. This version keeps the
local-directory mode and adds **Google Photos** as a first-class source.

Workflow (same two-step rhythm as the original):

    # 1. fingerprint everything and cache the result
    python wigglewiggle.py -d ~/Pictures hash
    python wigglewiggle.py --google-photos hash

    # 2. find the wigglegrams and write GIFs
    python wigglewiggle.py -d ~/Pictures export
    python wigglewiggle.py --google-photos export

The `hash` step is cached in `_hashes.json` so re-running after adding photos
only fingerprints the new ones.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required. Install with: pip install -r requirements.txt")

try:
    import imagehash
except ImportError:  # pragma: no cover
    sys.exit("ImageHash is required. Install with: pip install -r requirements.txt")

# HEIC/HEIF support is optional but very common for phone photos.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}
HASH_CACHE_NAME = "_hashes.json"


# --------------------------------------------------------------------------- #
# Source abstraction
# --------------------------------------------------------------------------- #
@dataclass
class SourceItem:
    """One photo in a source, independent of where it lives.

    `fetch_bytes` is a lazy callable so that the hashing pass can stream images
    one at a time instead of holding a whole library in memory (and so Google
    Photos downloads only happen when actually needed).
    """

    id: str
    name: str
    creation_time: datetime
    source: str
    fetch_bytes: callable = field(repr=False)


class PhotoSource:
    """Base class for anything that can enumerate photos."""

    name = "source"

    def enumerate(self) -> Iterator[SourceItem]:  # pragma: no cover - interface
        raise NotImplementedError


class DirectorySource(PhotoSource):
    """Read photos recursively from a local folder."""

    name = "directory"

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(self.path):
            sys.exit(f"Not a directory: {self.path}")

    def enumerate(self) -> Iterator[SourceItem]:
        for root, _dirs, files in os.walk(self.path):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in IMAGE_EXTENSIONS:
                    continue
                full = os.path.join(root, fname)
                yield SourceItem(
                    id=full,
                    name=fname,
                    creation_time=_image_datetime(full),
                    source=self.name,
                    fetch_bytes=lambda p=full: open(p, "rb").read(),
                )


class GooglePhotosSource(PhotoSource):
    """Read photos from a Google Photos library via the Library API.

    Auth uses the standard installed-app OAuth flow. Drop your OAuth client
    secrets JSON next to this script (or point at it with --client-secrets) and
    the first run opens a browser to grant read-only access. The resulting token
    is cached in `google_photos_token.json`.

    Note on API access: as of 2025 the Library API's `readonly` scope only
    returns media the app itself created unless your project still has the
    legacy broad-access grant. If a full-library listing comes back empty, that
    restriction is why -- see the README for the Picker-API alternative.
    """

    name = "google-photos"

    SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly"]
    LIST_URL = "https://photoslibrary.googleapis.com/v1/mediaItems"

    def __init__(
        self,
        client_secrets: str = "client_secrets.json",
        token_path: str = "google_photos_token.json",
        page_size: int = 100,
        max_items: Optional[int] = None,
    ):
        self.client_secrets = client_secrets
        self.token_path = token_path
        self.page_size = page_size
        self.max_items = max_items
        self._session = self._authorize()

    def _authorize(self):
        try:
            import requests
            from google.auth.transport.requests import AuthorizedSession, Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            sys.exit(
                "Google Photos support needs extra packages. Install with:\n"
                "    pip install google-auth google-auth-oauthlib requests"
            )

        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, self.SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.client_secrets):
                    sys.exit(
                        f"Missing OAuth client secrets at '{self.client_secrets}'.\n"
                        "Create an OAuth client (Desktop app) in Google Cloud Console,\n"
                        "enable the Photos Library API, download the JSON, and pass it\n"
                        "with --client-secrets."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets, self.SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "w") as fh:
                fh.write(creds.to_json())

        return AuthorizedSession(creds)

    def enumerate(self) -> Iterator[SourceItem]:
        count = 0
        page_token = None
        while True:
            params = {"pageSize": self.page_size}
            if page_token:
                params["pageToken"] = page_token
            resp = self._session.get(self.LIST_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()

            for item in payload.get("mediaItems", []):
                # Skip videos -- wigglegrams are made from stills.
                meta = item.get("mediaMetadata", {})
                if "photo" not in meta:
                    continue
                created = meta.get("creationTime")
                yield SourceItem(
                    id=item["id"],
                    name=item.get("filename", item["id"]),
                    creation_time=_parse_iso(created),
                    source=self.name,
                    # `=d` asks Google for the full-resolution download.
                    fetch_bytes=lambda url=item["baseUrl"]: self._download(url + "=d"),
                )
                count += 1
                if self.max_items and count >= self.max_items:
                    return

            page_token = payload.get("nextPageToken")
            if not page_token:
                return

    def _download(self, url: str) -> bytes:
        resp = self._session.get(url)
        resp.raise_for_status()
        return resp.content


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
@dataclass
class HashedImage:
    """A fingerprinted photo, cacheable to JSON."""

    id: str
    name: str
    source: str
    creation_time: str  # ISO 8601
    phash: str  # hex perceptual hash

    def time(self) -> datetime:
        return _parse_iso(self.creation_time)

    def hash(self) -> "imagehash.ImageHash":
        return imagehash.hex_to_hash(self.phash)


def hash_source(source: PhotoSource, cache_path: str) -> list[HashedImage]:
    """Fingerprint every photo in `source`, reusing cached hashes when possible."""
    cache = _load_cache(cache_path)
    by_id = {h.id: h for h in cache}

    processed = 0
    skipped = 0
    for item in source.enumerate():
        if item.id in by_id:
            skipped += 1
            continue
        try:
            img = Image.open(io.BytesIO(item.fetch_bytes()))
            phash = imagehash.phash(img)
        except Exception as exc:  # noqa: BLE001 - keep going past bad files
            print(f"  ! skipping {item.name}: {exc}", file=sys.stderr)
            continue
        hashed = HashedImage(
            id=item.id,
            name=item.name,
            source=item.source,
            creation_time=item.creation_time.astimezone(timezone.utc).isoformat(),
            phash=str(phash),
        )
        by_id[item.id] = hashed
        processed += 1
        if processed % 25 == 0:
            print(f"  ...hashed {processed} new photos")
            _save_cache(cache_path, list(by_id.values()))

    result = list(by_id.values())
    _save_cache(cache_path, result)
    print(f"Hashed {processed} new photo(s), reused {skipped} cached. Total: {len(result)}.")
    return result


# --------------------------------------------------------------------------- #
# Wigglegram detection
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    frames: list[HashedImage]

    @property
    def start(self) -> datetime:
        return self.frames[0].time()


def find_candidates(
    images: Iterable[HashedImage],
    distance: int = 8,
    min_frames: int = 2,
    max_frames: int = 12,
    max_gap_seconds: float = 4.0,
) -> list[Candidate]:
    """Group images into runs of consecutive near-duplicate frames.

    Two consecutive (by time) photos belong to the same wigglegram when their
    perceptual hashes are within `distance` and they were taken within
    `max_gap_seconds` of each other -- i.e. a burst of the same scene.
    """
    ordered = sorted(images, key=lambda h: h.time())
    candidates: list[Candidate] = []
    run: list[HashedImage] = []

    for img in ordered:
        if not run:
            run = [img]
            continue
        prev = run[-1]
        close_enough = (prev.hash() - img.hash()) <= distance
        gap = (img.time() - prev.time()).total_seconds()
        in_time = gap <= max_gap_seconds
        if close_enough and in_time and len(run) < max_frames:
            run.append(img)
        else:
            if len(run) >= min_frames:
                candidates.append(Candidate(frames=run))
            run = [img]

    if len(run) >= min_frames:
        candidates.append(Candidate(frames=run))

    return candidates


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export_candidates(
    candidates: list[Candidate],
    source: PhotoSource,
    out_dir: str,
    max_size: int = 600,
    frame_ms: int = 100,
    boomerang: bool = True,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # Build an id->fetch map so export works regardless of source ordering.
    fetchers = {item.id: item.fetch_bytes for item in source.enumerate()}

    for idx, cand in enumerate(candidates, start=1):
        frames = []
        for frame in cand.frames:
            fetch = fetchers.get(frame.id)
            if fetch is None:
                print(f"  ! frame {frame.name} no longer in source, skipping", file=sys.stderr)
                continue
            try:
                img = Image.open(io.BytesIO(fetch())).convert("RGB")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! failed to load {frame.name}: {exc}", file=sys.stderr)
                continue
            img.thumbnail((max_size, max_size))
            frames.append(img)

        if len(frames) < 2:
            continue

        sequence = frames + frames[-2:0:-1] if boomerang else frames
        stamp = cand.start.strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(out_dir, f"wiggle_{stamp}_{idx:03d}.gif")
        frames[0].save(
            out_path,
            save_all=True,
            append_images=sequence[1:],
            duration=frame_ms,
            loop=0,
            disposal=2,
        )
        print(f"  wrote {out_path}  ({len(frames)} frames)")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _image_datetime(path: str) -> datetime:
    """EXIF capture time if available, else file mtime."""
    try:
        img = Image.open(path)
        exif = img.getexif()
        # 36867 = DateTimeOriginal, 306 = DateTime
        for tag in (36867, 306):
            value = exif.get(tag)
            if value:
                return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
    except Exception:  # noqa: BLE001
        pass
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


def _parse_iso(value: Optional[str]) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_cache(path: str) -> list[HashedImage]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        raw = json.load(fh)
    return [HashedImage(**row) for row in raw]


def _save_cache(path: str, images: list[HashedImage]) -> None:
    with open(path, "w") as fh:
        json.dump([asdict(h) for h in images], fh, indent=2)


def build_source(args: argparse.Namespace) -> PhotoSource:
    if args.google_photos:
        return GooglePhotosSource(
            client_secrets=args.client_secrets,
            token_path=args.token,
            max_items=args.max_items,
        )
    if args.directory:
        return DirectorySource(args.directory)
    sys.exit("Pick a source: -d/--directory <path> or -g/--google-photos")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Find and extract wiggle stereographs from your photos."
    )

    src = parser.add_argument_group("photo source")
    src.add_argument("-d", "--directory", help="read photos from a local folder")
    src.add_argument(
        "-g",
        "--google-photos",
        action="store_true",
        help="read photos from Google Photos",
    )
    src.add_argument(
        "--client-secrets",
        default="client_secrets.json",
        help="OAuth client secrets JSON for Google Photos",
    )
    src.add_argument(
        "--token",
        default="google_photos_token.json",
        help="where to cache the Google Photos OAuth token",
    )
    src.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="cap how many Google Photos items to scan (handy for testing)",
    )

    parser.add_argument("--cache", default=HASH_CACHE_NAME, help="hash cache file")
    parser.add_argument("--out", default="wigglegrams", help="output directory for GIFs")
    parser.add_argument(
        "--distance",
        type=int,
        default=8,
        help="max perceptual-hash distance between frames (lower = stricter)",
    )
    parser.add_argument("--min-frames", type=int, default=2, help="min frames per wigglegram")
    parser.add_argument("--max-frames", type=int, default=12, help="max frames per wigglegram")
    parser.add_argument(
        "--max-gap",
        type=float,
        default=4.0,
        help="max seconds between consecutive frames",
    )
    parser.add_argument(
        "--frame-ms", type=int, default=100, help="GIF frame duration in milliseconds"
    )
    parser.add_argument(
        "--no-boomerang",
        action="store_true",
        help="play frames forward only instead of forward+reverse",
    )

    parser.add_argument(
        "action",
        choices=["hash", "export", "list"],
        help="hash: fingerprint photos | export: write GIFs | list: print candidates",
    )

    args = parser.parse_args(argv)

    if args.action == "hash":
        source = build_source(args)
        print(f"Hashing photos from {source.name} ...")
        hash_source(source, args.cache)
        return

    # export / list both need cached hashes
    images = _load_cache(args.cache)
    if not images:
        sys.exit(f"No cached hashes in {args.cache}. Run the 'hash' action first.")

    candidates = find_candidates(
        images,
        distance=args.distance,
        min_frames=args.min_frames,
        max_frames=args.max_frames,
        max_gap_seconds=args.max_gap,
    )
    print(f"Found {len(candidates)} wigglegram candidate(s).")

    if args.action == "list":
        for idx, cand in enumerate(candidates, start=1):
            names = ", ".join(f.name for f in cand.frames)
            print(f"  {idx:3d}. {cand.start.isoformat()}  [{len(cand.frames)} frames]  {names}")
        return

    if args.action == "export":
        source = build_source(args)
        export_candidates(
            candidates,
            source,
            out_dir=args.out,
            frame_ms=args.frame_ms,
            boomerang=not args.no_boomerang,
        )


if __name__ == "__main__":
    main()
