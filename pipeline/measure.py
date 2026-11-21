#!/usr/bin/env python3
"""
measure.py: measure one ripped CD and add it to the site's data files.

    python3 pipeline/measure.py ~/Rips/rush-2112-1989 \
        --album-id rush-2112 --title "2112" --artist Rush --album-year 1976 \
        --year 1989 --label "Early CD" --catalog "822 545-2" --cover ~/Rips/2112.jpg

What it does
  1. Finds the audio files (FLAC or WAV) in the folder, in cue-sheet order if there is a cue.
  2. Measures every track and the whole album: integrated loudness (ITU-R BS.1770-4 with
     EBU Tech 3341 gating), loudness range (EBU Tech 3342), crest factor, sample peak and the
     share of clipped samples.
  3. Builds a short-term loudness curve across the album for the site's chart.
  4. Reads the rip log (XLD or Exact Audio Copy) and records the ripper and AccurateRip result.
  5. Writes docs/data/albums/<album-id>.json (adding or replacing this version) and updates
     docs/data/index.json. The site reads those files, so no site change is needed.

Other commands
    python3 pipeline/measure.py --check        validate every data file
    add --dry-run to any measurement to print the result without writing anything
"""
import argparse, datetime as dt, hashlib, json, math, os, re, shutil, sys, tempfile
from pathlib import Path
import numpy as np

try:
    import soundfile as sf
    from scipy.signal import lfilter
except ImportError:
    sys.exit("Missing packages. Run: pip install -r pipeline/requirements.txt")

SCHEMA = 1
TOOL = "up-to-eleven measure.py 1"
BIN_S = 0.1            # all gating blocks are built from 100 ms bins
CURVE_POINTS = 100     # points in the album loudness curve
CLIP_LEVEL = 0.999     # samples at or above this count as clipped
AUDIO_EXT = {".flac", ".wav", ".aif", ".aiff"}


# ---------- K-weighting (ITU-R BS.1770-4), valid for any sample rate ----------
def k_filters(fs):
    G, f0, Q = 3.999843853973347, 1681.974450955533, 0.7071752369554196
    K = math.tan(math.pi * f0 / fs); Vh = 10 ** (G / 20); Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    shelf = ([(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0],
             [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / fs); a0 = 1 + K / Q + K * K
    hp = ([1 / a0, -2 / a0, 1 / a0], [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    return shelf, hp


def k_energy(x, fs):
    """Per-sample K-weighted energy summed over channels. x has shape (frames, channels)."""
    shelf, hp = k_filters(fs)
    e = np.zeros(x.shape[0])
    for c in range(x.shape[1]):
        y = lfilter(*hp, lfilter(*shelf, x[:, c]))
        e += y * y
    return e


class Binner:
    """Sums sample energy into 100 ms bins. Carries leftovers so tracks join without gaps."""
    def __init__(self, fs):
        self.B = int(round(BIN_S * fs)); self.chunks = []; self.carry = 0.0; self.cn = 0

    def add(self, e):
        i, n = 0, len(e)
        if self.cn:
            take = min(self.B - self.cn, n)
            self.carry += float(e[:take].sum()); self.cn += take; i = take
            if self.cn == self.B:
                self.chunks.append(np.array([self.carry])); self.carry = 0.0; self.cn = 0
        m = (n - i) // self.B
        if m:
            self.chunks.append(e[i:i + m * self.B].reshape(m, self.B).sum(axis=1)); i += m * self.B
        if i < n:
            self.carry += float(e[i:].sum()); self.cn += n - i

    def bins(self):
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0)


def win_ms(bins, B, k):
    """Mean-square of every window of k bins, hop one bin."""
    if len(bins) < k:
        return np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(bins)])
    return (c[k:] - c[:-k]) / (k * B)


def to_lufs(ms):
    return -0.691 + 10 * np.log10(np.maximum(ms, 1e-20))


def integrated(bins, B):
    ms = win_ms(bins, B, 4)                        # 400 ms blocks, 75 % overlap
    ms = ms[to_lufs(ms) > -70]
    if not len(ms):
        return None
    rel = to_lufs(ms.mean()) - 10
    ms = ms[to_lufs(ms) > rel]
    return float(to_lufs(ms.mean())) if len(ms) else None


def loudness_range(bins, B):
    ms = win_ms(bins, B, 30)                       # 3 s short-term blocks, 100 ms hop
    ms = ms[to_lufs(ms) > -70]
    if not len(ms):
        return None
    rel = to_lufs(ms.mean()) - 20
    g = np.sort(to_lufs(ms[to_lufs(ms) > rel]))
    if len(g) < 2:
        return 0.0
    return float(np.percentile(g, 95) - np.percentile(g, 10))


def curve(bins, B):
    """Short-term loudness (3 s windows, 1 s hop) resampled to CURVE_POINTS over album time."""
    st = to_lufs(win_ms(bins, B, 30))[::10]
    if len(st) < 2:
        return None
    st = np.maximum(st, -70)
    xs = np.linspace(0, 1, len(st)); xi = np.linspace(0, 1, CURVE_POINTS)
    return [round(float(v), 1) for v in np.interp(xi, xs, st)]


# ---------- Files ----------
def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def find_tracks(folder):
    files = [p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT]
    if not files:
        sys.exit(f"No FLAC or WAV files found in {folder}")
    cues = sorted(folder.glob("*.cue"))
    if cues:
        text = cues[0].read_text(errors="replace")
        order = [m.group(1) for m in re.finditer(r'^\s*FILE\s+"(.+?)"', text, re.M)]
        by = {p.name: p for p in files}
        ordered = [by[n] for n in order if n in by]
        if len(ordered) == len(files):
            return ordered
    return sorted(files, key=natural_key)


def read_text(path):
    b = path.read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", errors="replace")
    return b.decode("utf-8", errors="replace")


def read_log(folder):
    logs = sorted(folder.glob("*.log"))
    if not logs:
        return {"ripper": "No log", "accuraterip": "No log", "log_sha256": None}
    raw = logs[0].read_bytes(); t = read_text(logs[0])
    ripper = "XLD" if re.search(r"XLD extraction logfile|X Lossless Decoder", t, re.I) else \
             "EAC" if re.search(r"Exact Audio Copy", t, re.I) else "Unrecognized"
    acc = "All tracks accurate" if re.search(r"All tracks accurately ripped", t, re.I) else \
          "Partly, see log" if re.search(r"accurately ripped|AccurateRip", t, re.I) else "No AccurateRip info"
    return {"ripper": ripper, "accuraterip": acc, "log_sha256": hashlib.sha256(raw).hexdigest()}


def title_of(path, info):
    t = (info or {}).get("title")
    return t if t else re.sub(r"^\s*\d+[\s._-]*", "", path.stem) or path.stem


# ---------- Measurement ----------
def measure_folder(folder):
    tracks = find_tracks(folder)
    fs0 = None; album_bins = None; stats = {"peak": 0.0, "sumsq": 0.0, "clip": 0, "n": 0}
    out = []
    for i, p in enumerate(tracks, 1):
        with sf.SoundFile(str(p)) as f:
            fs = f.samplerate; ch = f.channels; meta = {}
            try:
                meta = f.copy_metadata()
            except Exception:
                pass
            x = f.read(dtype="float64", always_2d=True)
        if ch > 2:
            sys.exit(f"{p.name} has {ch} channels. Only mono and stereo CDs are supported.")
        if fs0 is None:
            fs0 = fs; album_bins = Binner(fs)
        elif fs != fs0:
            sys.exit(f"{p.name} is {fs} Hz but the first track is {fs0} Hz. All tracks must match.")
        e = k_energy(x, fs)
        tb = Binner(fs); tb.add(e); album_bins.add(e)
        ax = np.abs(x); peak = float(ax.max()) if ax.size else 0.0
        sumsq = float((x * x).sum()); clip = int((ax >= CLIP_LEVEL).sum()); n = x.size
        stats["peak"] = max(stats["peak"], peak); stats["sumsq"] += sumsq; stats["clip"] += clip; stats["n"] += n
        b = tb.bins(); B = tb.B
        peak_db = 20 * math.log10(peak + 1e-12); rms_db = 10 * math.log10(sumsq / max(n, 1) + 1e-20)
        out.append({"n": i, "file": p.name, "title": title_of(p, meta), "seconds": round(x.shape[0] / fs, 1),
                    "lufs": r1(integrated(b, B)), "lra": r1(loudness_range(b, B)),
                    "crest": r1(peak_db - rms_db), "peak": r1(peak_db),
                    "clip_pct": round(100 * clip / max(n, 1), 3)})
        print(f"  {i:2d}  {out[-1]['title'][:34]:34s} {fmt(out[-1]['lufs']):>7} LUFS  LRA {fmt(out[-1]['lra']):>5}  crest {out[-1]['crest']:>5}")
    b = album_bins.bins(); B = album_bins.B
    peak_db = 20 * math.log10(stats["peak"] + 1e-12); rms_db = 10 * math.log10(stats["sumsq"] / max(stats["n"], 1) + 1e-20)
    metrics = {"lufs": r1(integrated(b, B)), "lra": r1(loudness_range(b, B)), "crest": r1(peak_db - rms_db),
               "peak": r1(peak_db), "clip_pct": round(100 * stats["clip"] / max(stats["n"], 1), 3)}
    return {"sample_rate": fs0, "seconds": round(sum(t["seconds"] for t in out), 1), "metrics": metrics,
            "curve": curve(b, B), "tracks": out}


def r1(v):
    return None if v is None else round(float(v), 1)


def fmt(v):
    return "n/a" if v is None else f"{v:.1f}"


# ---------- Data files ----------
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False); f.write("\n")
    os.replace(tmp, path)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def update_data(args, result, log):
    data = Path(args.data_dir); apath = data / "albums" / f"{args.album_id}.json"
    album = load_json(apath, {})
    title = args.title or album.get("title"); artist = args.artist or album.get("artist"); year = args.album_year or album.get("year")
    if not (title and artist and year):
        sys.exit("A new album needs --title, --artist and --album-year.")
    cover = album.get("cover")
    if args.cover:
        src = Path(args.cover).expanduser(); dest = data / "covers" / f"{args.album_id}{src.suffix.lower()}"
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dest)
        cover = f"covers/{dest.name}"
    version = {
        "id": slug(f"{args.year}-{args.label}"), "year": args.year, "label": args.label,
        "catalog": args.catalog or "", "barcode": args.barcode or "", "note": args.note or "",
        "rip": log, "sample_rate": result["sample_rate"], "seconds": result["seconds"],
        "metrics": result["metrics"], "curve": result["curve"], "tracks": result["tracks"],
        "measured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": TOOL,
    }
    versions = [v for v in album.get("versions", []) if v["id"] != version["id"]] + [version]
    versions.sort(key=lambda v: (v["year"], v["label"]))
    album = {"schema": SCHEMA, "id": args.album_id, "title": title, "artist": artist, "year": year,
             "cover": cover, "versions": versions}
    index = load_json(data / "index.json", {"schema": SCHEMA, "albums": []})
    entry = {"id": args.album_id, "title": title, "artist": artist, "year": year, "cover": cover,
             "file": f"albums/{args.album_id}.json", "versions": len(versions)}
    index["albums"] = [a for a in index["albums"] if a["id"] != args.album_id] + [entry]
    index["albums"].sort(key=lambda a: (a["artist"].lower(), a["year"]))
    index["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if args.dry_run:
        print("\n(dry run) nothing written. Version record:\n" + json.dumps({k: v for k, v in version.items() if k not in ("curve", "tracks")}, indent=2))
        return
    write_json(apath, album); write_json(data / "index.json", index)
    print(f"\nWrote {apath} ({len(versions)} version{'s' if len(versions) != 1 else ''}) and {data / 'index.json'}")


# ---------- Validation ----------
def check(data_dir):
    data = Path(data_dir); problems = []
    idx = load_json(data / "index.json", None)
    if idx is None:
        sys.exit(f"{data / 'index.json'} not found")
    for a in idx["albums"]:
        p = data / a["file"]
        if not p.exists():
            problems.append(f"index lists {a['file']} but it does not exist"); continue
        j = json.loads(p.read_text())
        for k in ("id", "title", "artist", "year", "versions"):
            if k not in j: problems.append(f"{p.name}: missing {k}")
        if j.get("cover") and not (data / j["cover"]).exists(): problems.append(f"{p.name}: cover {j['cover']} is missing")
        for v in j.get("versions", []):
            tag = f"{p.name} / {v.get('id')}"
            m = v.get("metrics") or {}
            for k in ("lufs", "lra", "crest", "peak", "clip_pct"):
                if not isinstance(m.get(k), (int, float)) or not math.isfinite(m[k]): problems.append(f"{tag}: metric {k} is missing or not a number")
            c = v.get("curve") or []
            if len(c) != CURVE_POINTS or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in c): problems.append(f"{tag}: curve should be {CURVE_POINTS} numbers")
            if not v.get("tracks"): problems.append(f"{tag}: no tracks")
            if not v.get("catalog"): problems.append(f"{tag}: no catalog number (allowed, but the pressing is then unverified)")
    for pr in problems: print("  -", pr)
    print(f"Checked {len(idx['albums'])} albums: {'no problems' if not problems else str(len(problems)) + ' problem(s)'}")
    return not problems


def main():
    ap = argparse.ArgumentParser(description="Measure a ripped CD and add it to the site data.")
    ap.add_argument("rip_dir", nargs="?", help="folder with the FLAC/WAV files, .cue and .log")
    ap.add_argument("--album-id"); ap.add_argument("--title"); ap.add_argument("--artist"); ap.add_argument("--album-year", type=int)
    ap.add_argument("--year", type=int, help="release year of this pressing"); ap.add_argument("--label", help='for example "Early CD" or "2011 Remaster"')
    ap.add_argument("--catalog"); ap.add_argument("--barcode"); ap.add_argument("--note"); ap.add_argument("--cover")
    ap.add_argument("--data-dir", default="docs/data"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check(args.data_dir) else 1)
    if not (args.rip_dir and args.album_id and args.year and args.label):
        ap.error("need rip_dir, --album-id, --year and --label (or use --check)")
    args.album_id = slug(args.album_id)
    folder = Path(args.rip_dir).expanduser()
    if not folder.is_dir():
        sys.exit(f"{folder} is not a folder")
    print(f"Measuring {folder}")
    result = measure_folder(folder); log = read_log(folder)
    m = result["metrics"]
    print(f"\nAlbum: {fmt(m['lufs'])} LUFS, LRA {fmt(m['lra'])} LU, crest {fmt(m['crest'])} dB, peak {fmt(m['peak'])} dBFS, clipped {m['clip_pct']} %")
    print(f"Rip log: {log['ripper']}, AccurateRip: {log['accuraterip']}")
    if log["accuraterip"] != "All tracks accurate":
        print("  Note: the log does not confirm every track. Read it before publishing.")
    update_data(args, result, log)


if __name__ == "__main__":
    main()
