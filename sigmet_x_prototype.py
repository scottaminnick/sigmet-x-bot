#!/usr/bin/env python3
"""
Starter SIGMET-to-X prototype

What this does:
- Accepts structured SIGMET records (sample data included)
- Generates branded PNG graphics locally
- Builds X post text locally
- Saves outputs to ./output for review
- Prevents duplicate posting via a local SQLite state DB

What this does NOT do yet:
- Poll live AWC feeds
- Post to X
- Handle every SIGMET text edge case
- Manage amendments/cancellations beyond simple status tagging

Designed as a clean starting point for local testing and future deployment.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon, Rectangle


# -----------------------------
# Configuration
# -----------------------------
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / "sigmet_state.sqlite"

FIG_W = 16
FIG_H = 9
DPI = 150

COLOR_BG = "#202124"
COLOR_PANEL = "#2b2d31"
COLOR_TEXT = "#f4f4f4"
COLOR_MUTED = "#c8c8c8"
COLOR_BORDER = "#50545a"
COLOR_WATER = "#d6ebff"
COLOR_LAND = "#f7f7f7"
COLOR_STATE = "#b6b6b6"
COLOR_ARTCC = "#9a4dff"
COLOR_DOMAIN = "#6e6e6e"
COLOR_OUTSIDE_DOMAIN = "#8a8a8a"

HAZARD_STYLES = {
    "severe turbulence": {"bar": "#9a6500", "poly": "#a66b00", "tag": "#turbulence"},
    "severe icing": {"bar": "#2b9ed6", "poly": "#43afe6", "tag": "#icing"},
    "blowing dust": {"bar": "#b100b8", "poly": "#c200ca", "tag": "#blowingdust"},
    "volcanic ash": {"bar": "#7a3b14", "poly": "#8b4513", "tag": "#volcanicash"},
}


# -----------------------------
# Data model
# -----------------------------
@dataclass(frozen=True)
class SigmetRecord:
    sigmet_name: str
    sigmet_number: int
    hazard: str
    bottom: str
    top: str
    valid_until_utc: str
    artccs: Tuple[str, ...]
    polygon: Tuple[Tuple[float, float], ...]  # (lon, lat)
    source_center: str = "KKCI"
    status: str = "NEW"  # NEW / AMD / COR / CAN

    @property
    def sequence_label(self) -> str:
        return f"{self.sigmet_name} {self.sigmet_number}"

    @property
    def altitude_label(self) -> str:
        return f"{self.bottom}-{self.top}"

    @property
    def hazard_key(self) -> str:
        return self.hazard.strip().lower()

    @property
    def style(self) -> dict:
        return HAZARD_STYLES.get(
            self.hazard_key,
            {"bar": "#4a4a4a", "poly": "#8f8f8f", "tag": "#aviationwx"},
        )

    @property
    def valid_until_dt(self) -> datetime:
        return datetime.fromisoformat(self.valid_until_utc.replace("Z", "+00:00"))

    @property
    def stable_hash(self) -> str:
        payload = {
            "sequence_label": self.sequence_label,
            "hazard": self.hazard,
            "bottom": self.bottom,
            "top": self.top,
            "valid_until_utc": self.valid_until_utc,
            "artccs": self.artccs,
            "polygon": self.polygon,
            "source_center": self.source_center,
            "status": self.status,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return digest


# -----------------------------
# Sample records for local testing
# -----------------------------
def sample_sigmets() -> List[SigmetRecord]:
    return [
        SigmetRecord(
            sigmet_name="ROMEO",
            sigmet_number=1,
            hazard="Severe Icing",
            bottom="SFC",
            top="FL150",
            valid_until_utc="2026-04-02T10:17:00Z",
            artccs=("ZSE", "ZOA"),
            polygon=(
                (-124.8, 42.2),
                (-122.1, 42.2),
                (-121.4, 46.8),
                (-124.0, 46.8),
            ),
        ),
        SigmetRecord(
            sigmet_name="NOVEMBER",
            sigmet_number=3,
            hazard="Severe Turbulence",
            bottom="FL240",
            top="FL360",
            valid_until_utc="2026-04-02T13:45:00Z",
            artccs=("ZNY", "ZDC", "ZOB"),
            polygon=(
                (-77.6, 37.3),
                (-73.1, 38.8),
                (-72.4, 41.7),
                (-76.8, 40.5),
            ),
        ),
        SigmetRecord(
            sigmet_name="WHISKEY",
            sigmet_number=2,
            hazard="Blowing Dust",
            bottom="SFC",
            top="FL100",
            valid_until_utc="2026-04-02T02:46:00Z",
            artccs=("ZAB", "ZFW"),
            polygon=(
                (-105.4, 31.2),
                (-101.7, 32.4),
                (-102.0, 35.3),
                (-106.1, 34.0),
            ),
        ),
        SigmetRecord(
            sigmet_name="OSCAR",
            sigmet_number=4,
            hazard="Volcanic Ash",
            bottom="FL200",
            top="FL350",
            valid_until_utc="2026-04-02T08:05:00Z",
            artccs=("ZSE",),
            polygon=(
                (-123.8, 45.2),
                (-121.8, 45.5),
                (-121.0, 47.8),
                (-123.1, 47.5),
            ),
        ),
    ]


# -----------------------------
# State store
# -----------------------------
class StateStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posted_sigmets (
                    stable_hash TEXT PRIMARY KEY,
                    sequence_label TEXT NOT NULL,
                    hazard TEXT NOT NULL,
                    valid_until_utc TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    image_path TEXT,
                    post_text TEXT
                )
                """
            )
            conn.commit()

    def already_posted(self, stable_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted_sigmets WHERE stable_hash = ?", (stable_hash,)
            ).fetchone()
        return row is not None

    def record_post(self, record: SigmetRecord, image_path: Path, post_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO posted_sigmets (
                    stable_hash, sequence_label, hazard, valid_until_utc,
                    created_utc, image_path, post_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.stable_hash,
                    record.sequence_label,
                    record.hazard,
                    record.valid_until_utc,
                    datetime.now(timezone.utc).isoformat(),
                    str(image_path),
                    post_text,
                ),
            )
            conn.commit()


# -----------------------------
# Text composition
# -----------------------------
def format_utc_for_post(dt: datetime) -> str:
    return dt.strftime("%H%MZ %d %b %Y").upper()


def compose_post_text(record: SigmetRecord) -> str:
    valid_str = format_utc_for_post(record.valid_until_dt)
    artcc_text = " ".join(record.artccs)
    tag = record.style["tag"]

    status_prefix = ""
    if record.status in {"AMD", "COR", "CAN"}:
        status_prefix = f"[{record.status}] "

    text = (
        f"{status_prefix}SIGMET {record.sequence_label} has been issued for {record.hazard} "
        f"{record.altitude_label} until {valid_str}. "
        f"Affected ARTCCs: {artcc_text}. See the latest SIGMETs at aviationweather.gov. "
        f"#aviation {tag}"
    )
    return text


# -----------------------------
# Simple map helpers
# -----------------------------
CONUS_DOMAIN = (-127.0, -66.0, 23.0, 50.0)  # minlon, maxlon, minlat, maxlat

STATE_LINES = [
    # simple, sparse reference lines for prototype only
    [(-124, 42), (-114, 42), (-109, 42), (-104, 42), (-95, 42), (-90, 42), (-82, 42)],
    [(-124, 37), (-114, 37), (-109, 37), (-103, 37), (-94, 37), (-87, 37)],
    [(-124, 32), (-114, 32), (-109, 32), (-103, 32), (-94, 32), (-85, 32)],
    [(-120, 49), (-120, 32)],
    [(-111, 49), (-111, 31)],
    [(-104, 49), (-104, 25)],
    [(-97, 49), (-97, 25)],
    [(-90, 49), (-90, 29)],
    [(-82, 45), (-82, 25)],
    [(-75, 45), (-75, 35)],
]

ARTCC_LINES = [
    [(-124, 41), (-117, 41), (-113, 39), (-109, 37), (-104, 36), (-99, 36), (-95, 35), (-90, 34), (-85, 33)],
    [(-123, 46), (-118, 45), (-112, 44), (-107, 44), (-101, 43), (-96, 43), (-90, 42), (-84, 42), (-78, 41)],
    [(-121, 30), (-116, 32), (-110, 33), (-104, 32), (-98, 31), (-92, 30), (-86, 29), (-80, 28)],
]

AIRPORTS = {
    "SEA": (-122.31, 47.45),
    "PDX": (-122.60, 45.59),
    "SLC": (-111.98, 40.79),
    "DEN": (-104.67, 39.86),
    "DFW": (-97.04, 32.90),
    "MCI": (-94.71, 39.30),
    "ORD": (-87.91, 41.98),
    "ATL": (-84.43, 33.64),
    "CLT": (-80.94, 35.21),
    "JFK": (-73.78, 40.64),
    "BOS": (-71.01, 42.36),
}


def padded_extent(polygon: Sequence[Tuple[float, float]], min_pad_deg: float = 3.0) -> Tuple[float, float, float, float]:
    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    minlon, maxlon = min(lons), max(lons)
    minlat, maxlat = min(lats), max(lats)

    dx = max(maxlon - minlon, min_pad_deg)
    dy = max(maxlat - minlat, min_pad_deg)

    padx = dx * 0.9
    pady = dy * 0.9

    out = (
        max(CONUS_DOMAIN[0], minlon - padx),
        min(CONUS_DOMAIN[1], maxlon + padx),
        max(CONUS_DOMAIN[2], minlat - pady),
        min(CONUS_DOMAIN[3], maxlat + pady),
    )
    return out


def draw_background(ax, extent: Tuple[float, float, float, float], show_outside_domain: bool = True) -> None:
    minlon, maxlon, minlat, maxlat = extent
    ax.set_xlim(minlon, maxlon)
    ax.set_ylim(minlat, maxlat)
    ax.set_facecolor(COLOR_WATER)

    # crude land block for prototype
    ax.add_patch(Rectangle((CONUS_DOMAIN[0], CONUS_DOMAIN[2]), CONUS_DOMAIN[1] - CONUS_DOMAIN[0], CONUS_DOMAIN[3] - CONUS_DOMAIN[2],
                           facecolor=COLOR_LAND, edgecolor="none", zorder=0))

    if show_outside_domain:
        # Shade outside domain edges if current extent extends beyond CONUS_DOMAIN.
        if minlon < CONUS_DOMAIN[0]:
            ax.add_patch(Rectangle((minlon, minlat), CONUS_DOMAIN[0] - minlon, maxlat - minlat,
                                   facecolor=COLOR_OUTSIDE_DOMAIN, alpha=0.35, edgecolor="none", zorder=1))
        if maxlon > CONUS_DOMAIN[1]:
            ax.add_patch(Rectangle((CONUS_DOMAIN[1], minlat), maxlon - CONUS_DOMAIN[1], maxlat - minlat,
                                   facecolor=COLOR_OUTSIDE_DOMAIN, alpha=0.35, edgecolor="none", zorder=1))
        if minlat < CONUS_DOMAIN[2]:
            ax.add_patch(Rectangle((minlon, minlat), maxlon - minlon, CONUS_DOMAIN[2] - minlat,
                                   facecolor=COLOR_OUTSIDE_DOMAIN, alpha=0.35, edgecolor="none", zorder=1))
        if maxlat > CONUS_DOMAIN[3]:
            ax.add_patch(Rectangle((minlon, CONUS_DOMAIN[3]), maxlon - minlon, maxlat - CONUS_DOMAIN[3],
                                   facecolor=COLOR_OUTSIDE_DOMAIN, alpha=0.35, edgecolor="none", zorder=1))

    # Domain border
    ax.add_patch(Rectangle((CONUS_DOMAIN[0], CONUS_DOMAIN[2]), CONUS_DOMAIN[1] - CONUS_DOMAIN[0], CONUS_DOMAIN[3] - CONUS_DOMAIN[2],
                           facecolor="none", edgecolor=COLOR_DOMAIN, linewidth=2.0, zorder=6))

    # Sparse state lines
    for line in STATE_LINES:
        xs = [p[0] for p in line]
        ys = [p[1] for p in line]
        ax.plot(xs, ys, color=COLOR_STATE, linewidth=0.8, alpha=0.7, zorder=2)

    # ARTCC-like guide lines, thicker than current legacy look
    for line in ARTCC_LINES:
        xs = [p[0] for p in line]
        ys = [p[1] for p in line]
        ax.plot(xs, ys, color=COLOR_ARTCC, linewidth=1.2, alpha=0.55, zorder=3)

    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)



def draw_airports(ax, extent: Tuple[float, float, float, float]) -> None:
    minlon, maxlon, minlat, maxlat = extent
    for ident, (lon, lat) in AIRPORTS.items():
        if minlon <= lon <= maxlon and minlat <= lat <= maxlat:
            ax.plot(lon, lat, marker="o", markersize=2.5, color="#1f1f1f", zorder=7)
            ax.text(lon + 0.15, lat + 0.10, ident, fontsize=7, color="#1f1f1f", zorder=7)



def draw_polygon(ax, record: SigmetRecord) -> None:
    poly = MplPolygon(
        record.polygon,
        closed=True,
        facecolor=record.style["poly"],
        edgecolor="#222222",
        linewidth=1.2,
        alpha=0.55,
        zorder=5,
    )
    ax.add_patch(poly)


# -----------------------------
# Rendering
# -----------------------------
def render_sigmet_graphic(record: SigmetRecord, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{record.sequence_label.replace(' ', '_').lower()}_{record.hazard_key.replace(' ', '_')}.png"
    out_path = out_dir / fname

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=DPI, facecolor=COLOR_BG)

    # Outer border
    border_ax = fig.add_axes([0, 0, 1, 1])
    border_ax.set_facecolor(COLOR_BG)
    border_ax.add_patch(
        Rectangle((0.005, 0.005), 0.99, 0.99, transform=border_ax.transAxes,
                  facecolor="none", edgecolor=COLOR_BORDER, linewidth=2)
    )
    border_ax.axis("off")

    # Title bar
    title_ax = fig.add_axes([0.19, 0.89, 0.79, 0.085])
    title_ax.set_facecolor(record.style["bar"])
    title_ax.text(
        0.5,
        0.5,
        f"SIGMET for {record.hazard} {record.altitude_label}",
        ha="center",
        va="center",
        color="white",
        fontsize=24,
        fontweight="normal",
    )
    title_ax.set_xticks([])
    title_ax.set_yticks([])
    for s in title_ax.spines.values():
        s.set_visible(False)

    # Left info panel
    side_ax = fig.add_axes([0.02, 0.08, 0.17, 0.81])
    side_ax.set_facecolor(COLOR_PANEL)
    side_ax.set_xticks([])
    side_ax.set_yticks([])
    for s in side_ax.spines.values():
        s.set_visible(False)

    side_ax.text(0.08, 0.92, "AWC", color="white", fontsize=28, fontweight="bold", transform=side_ax.transAxes)
    side_ax.text(0.08, 0.83, record.sequence_label, color=COLOR_TEXT, fontsize=23, transform=side_ax.transAxes)

    valid = record.valid_until_dt
    side_ax.text(0.08, 0.70, "Valid Until", color=COLOR_TEXT, fontsize=18, transform=side_ax.transAxes)
    side_ax.text(0.08, 0.63, valid.strftime("%H%M UTC"), color=COLOR_TEXT, fontsize=22, fontweight="bold", transform=side_ax.transAxes)
    side_ax.text(0.08, 0.56, valid.strftime("%B %-d, %Y") if hasattr(valid, 'strftime') else record.valid_until_utc,
                 color=COLOR_TEXT, fontsize=18, transform=side_ax.transAxes)

    side_ax.plot([0.05, 0.95], [0.50, 0.50], color=record.style["bar"], linewidth=2, transform=side_ax.transAxes)
    side_ax.text(0.08, 0.44, "ARTCCs affected:", color=COLOR_TEXT, fontsize=18, transform=side_ax.transAxes)
    side_ax.text(0.08, 0.36, " ".join(record.artccs), color=COLOR_TEXT, fontsize=22, fontweight="bold", transform=side_ax.transAxes)

    side_ax.plot([0.05, 0.95], [0.27, 0.27], color=record.style["bar"], linewidth=2, transform=side_ax.transAxes)
    side_ax.text(0.50, 0.16, record.altitude_label, color=COLOR_TEXT, fontsize=30,
                 fontweight="bold", ha="center", transform=side_ax.transAxes)
    side_ax.text(0.50, 0.09, record.hazard.lower(), color=COLOR_TEXT, fontsize=18,
                 ha="center", transform=side_ax.transAxes)

    # Main map
    extent = padded_extent(record.polygon)
    map_ax = fig.add_axes([0.24, 0.08, 0.74, 0.81])
    draw_background(map_ax, extent)
    draw_polygon(map_ax, record)
    draw_airports(map_ax, extent)

    # Inset locator map
    inset_ax = fig.add_axes([0.028, 0.095, 0.13, 0.15])
    draw_background(inset_ax, CONUS_DOMAIN, show_outside_domain=False)
    draw_polygon(inset_ax, record)
    inset_ax.add_patch(Rectangle((extent[0], extent[2]), extent[1] - extent[0], extent[3] - extent[2],
                                 facecolor="none", edgecolor="#444444", linewidth=1.0, zorder=8))
    for spine in inset_ax.spines.values():
        spine.set_edgecolor(record.style["bar"])
        spine.set_linewidth(2)
        spine.set_visible(True)

    # Footer note
    fig.text(0.985, 0.015, "Prototype output for local review only", ha="right", va="bottom",
             color=COLOR_MUTED, fontsize=9)

    fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return out_path


# -----------------------------
# Dry-run workflow
# -----------------------------
def process_sigmets(records: Iterable[SigmetRecord], dry_run: bool = True) -> None:
    store = StateStore(DB_PATH)
    manifest = []

    for record in records:
        if store.already_posted(record.stable_hash):
            print(f"Skipping duplicate: {record.sequence_label} / {record.hazard}")
            continue

        image_path = render_sigmet_graphic(record, OUTPUT_DIR)
        post_text = compose_post_text(record)

        # In prototype mode we only save locally.
        if dry_run:
            txt_path = image_path.with_suffix(".txt")
            txt_path.write_text(post_text, encoding="utf-8")
            print(f"Created: {image_path.name}")
            print(f"Caption: {post_text}\n")
        else:
            # Future hook for X posting.
            raise NotImplementedError("Live posting is not enabled in this starter prototype.")

        store.record_post(record, image_path, post_text)
        manifest.append(
            {
                "record": asdict(record),
                "image_path": str(image_path),
                "caption_path": str(image_path.with_suffix('.txt')),
            }
        )

    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Saved manifest: {manifest_path}")


# -----------------------------
# Future parsing hooks
# -----------------------------
def parse_sigmet_text(raw_text: str) -> SigmetRecord:
    """
    Placeholder for future text-parser work.

    Recommended next steps:
    - Parse header / phenomenon / valid-until / altitude range / ARTCCs / vertices
    - Normalize SIGMET naming and status (NEW/AMD/COR/CAN)
    - Build polygon from vertex list
    - Validate against domain bounds
    """
    raise NotImplementedError("Text parsing is not implemented in this starter version.")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    records = sample_sigmets()
    process_sigmets(records, dry_run=True)


if __name__ == "__main__":
    main()
