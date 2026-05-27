"""Render the CS-Server pipeline topology to docs/pipelines.png + pipelines.svg.

Run from the repo root with:
    .venv/bin/python docs/generate_pipelines_diagram.py
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

OUT_DIR = Path(__file__).resolve().parent

# Palette (muted, print-friendly)
BG = "#0f172a"          # near-black slate
PANEL = "#1e293b"
TEXT = "#e2e8f0"
MUTED = "#94a3b8"
ACCENT_CORE = "#38bdf8"      # cyan  — always-on
ACCENT_DAILY = "#fbbf24"     # amber — daily
ACCENT_WEEKLY = "#a78bfa"    # violet — weekly
ACCENT_MONTHLY = "#f472b6"   # pink  — monthly
ACCENT_API = "#34d399"       # green — read path
ACCENT_EXT = "#fb7185"       # rose  — external sources
EDGE = "#475569"


def panel(ax, x, y, w, h, title, body_color=PANEL, title_color=TEXT):
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.25",
        linewidth=1.2, edgecolor=MUTED, facecolor=body_color,
    )
    ax.add_patch(box)
    ax.text(x + 0.3, y + h - 0.45, title, color=title_color,
            fontsize=11, weight="bold", family="DejaVu Sans")


def job_card(ax, x, y, w, h, name, cadence, desc, color):
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.18",
        linewidth=1.4, edgecolor=color, facecolor="#0b1220",
    )
    ax.add_patch(box)
    ax.text(x + w / 2, y + h - 0.45, name, color=color,
            ha="center", va="top", fontsize=9.5, weight="bold",
            family="DejaVu Sans Mono")
    ax.text(x + w / 2, y + h - 0.95, cadence, color=TEXT,
            ha="center", va="top", fontsize=8.5, style="italic")
    ax.text(x + w / 2, y + 0.25, desc, color=MUTED,
            ha="center", va="bottom", fontsize=7.6, wrap=True)


def ext_chip(ax, x, y, w, h, label, sub):
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.2",
        linewidth=1.2, edgecolor=ACCENT_EXT, facecolor="#1a0c12",
    )
    ax.add_patch(box)
    ax.text(x + w / 2, y + h - 0.32, label, color=ACCENT_EXT,
            ha="center", va="top", fontsize=9, weight="bold",
            family="DejaVu Sans Mono")
    ax.text(x + w / 2, y + 0.2, sub, color=MUTED,
            ha="center", va="bottom", fontsize=7.4)


def arrow(ax, x1, y1, x2, y2, color=EDGE, style="-|>", lw=1.1, alpha=0.9):
    a = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle=style, color=color, linewidth=lw,
        mutation_scale=12, alpha=alpha,
        connectionstyle="arc3,rad=0.0",
    )
    ax.add_patch(a)


def main():
    fig_w, fig_h = 22, 16
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=140)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 44)
    ax.set_ylim(0, 32)
    ax.set_aspect("equal")
    ax.axis("off")

    # ── Title ──────────────────────────────────────────────────────────
    ax.text(22, 31, "CS-SERVER  ·  Pipeline Topology",
            color=TEXT, ha="center", va="center",
            fontsize=22, weight="bold", family="DejaVu Sans")
    ax.text(22, 30.1,
            "Read path · APScheduler tiers (core / daily / weekly / monthly) · startup phases",
            color=MUTED, ha="center", va="center", fontsize=11, style="italic")

    # ── EXTERNAL SOURCES (top strip) ───────────────────────────────────
    ext_y, ext_h = 27.0, 1.9
    ext_specs = [
        ("BestTime",       "foot traffic"),
        ("Google Places",  "vibe attrs, photos, hours"),
        ("Apify",          "IG handles + posts; menus"),
        ("SerpApi",        "menu photos (primary)"),
        ("OpenAI",         "GPT-4o vibes + menu OCR"),
        ("S3",             "photo / menu blobs"),
        ("Redis",          "geo index + cache + state"),
    ]
    n = len(ext_specs)
    gap = 0.4
    cw = (44 - 1.0 - gap * (n - 1)) / n
    for i, (label, sub) in enumerate(ext_specs):
        x = 0.5 + i * (cw + gap)
        ext_chip(ax, x, ext_y, cw, ext_h, label, sub)

    # ── APSCHEDULER MEGA-PANEL ─────────────────────────────────────────
    panel(ax, 0.5, 8.6, 43, 17.5, "APScheduler  (in-process, asyncio)",
          body_color="#111827")

    # Tier sub-panels (each holds 1-3 job cards)
    # CORE
    panel(ax, 1.0, 21.5, 42, 3.7,
          "Core refresh  ·  always-on",
          body_color="#0b1220", title_color=ACCENT_CORE)
    job_card(ax, 1.6, 21.85, 13.2, 2.55,
             "venue_catalog_refresh",
             "every 30 days  ·  IntervalTrigger",
             "BestTime venue catalog → Redis\n(default 43,200 min)",
             ACCENT_CORE)
    job_card(ax, 15.4, 21.85, 13.2, 2.55,
             "live_forecast_refresh",
             "every 5 minutes  ·  IntervalTrigger",
             "BestTime live busyness per venue\n(only near-real-time pipeline)",
             ACCENT_CORE)
    job_card(ax, 29.2, 21.85, 13.2, 2.55,
             "weekly_forecast_refresh",
             "Sun 00:00  ·  cron \"0 0 * * 0\"",
             "BestTime weekly 7-day curve\nfor every cached venue",
             ACCENT_CORE)

    # DAILY
    panel(ax, 1.0, 17.5, 42, 3.7,
          "Daily enrichment  ·  Google Places",
          body_color="#0b1220", title_color=ACCENT_DAILY)
    job_card(ax, 1.6, 17.85, 20.2, 2.55,
             "google_places_enrichment",
             "daily 03:00  ·  cron \"0 3 * * *\"",
             "Vibe attrs, hours, business status;\nsoft-deprecates permanently closed venues",
             ACCENT_DAILY)
    job_card(ax, 22.2, 17.85, 20.2, 2.55,
             "photo_enrichment",
             "daily 03:00  ·  (same cron)",
             "Up to 20 venues/run × 5 photos\nfrom Google Places → Redis",
             ACCENT_DAILY)

    # WEEKLY
    panel(ax, 1.0, 13.5, 42, 3.7,
          "Weekly enrichment  ·  Instagram",
          body_color="#0b1220", title_color=ACCENT_WEEKLY)
    job_card(ax, 1.6, 13.85, 20.2, 2.55,
             "instagram_enrichment",
             "Mon 04:00  ·  cron \"0 4 * * 1\"",
             "Discover IG handles via Apify;\ncache 30d (7d for not-found)",
             ACCENT_WEEKLY)
    job_card(ax, 22.2, 13.85, 20.2, 2.55,
             "ig_posts_enrichment",
             "Wed 04:00  ·  cron \"0 4 * * 3\"",
             "Scrape ~10 posts × ≤20 venues;\ncaptions feed vibe_classifier",
             ACCENT_WEEKLY)

    # MONTHLY
    panel(ax, 1.0, 9.0, 42, 4.2,
          "Monthly enrichment  ·  Menus + Vibes (staggered on the 1st)",
          body_color="#0b1220", title_color=ACCENT_MONTHLY)
    job_card(ax, 1.6, 9.35, 13.2, 3.1,
             "menu_photo_enrichment",
             "1st @ 05:00  ·  \"0 5 1 * *\"",
             "SerpApi → Apify fallback;\n≤20 photos × ≤10 venues/run;\nblobs stored on S3",
             ACCENT_MONTHLY)
    job_card(ax, 15.4, 9.35, 13.2, 3.1,
             "menu_extraction",
             "1st @ 06:00  ·  \"0 6 1 * *\"",
             "OpenAI GPT-4o reads photos →\nstructured menu JSON → Redis",
             ACCENT_MONTHLY)
    job_card(ax, 29.2, 9.35, 13.2, 3.1,
             "vibe_classifier",
             "1st @ 07:00  ·  \"0 7 1 * *\"",
             "Two-stage GPT-4o on photos + IG\nposts + reviews; escalates to\nstage-B at ≥ 0.80 confidence",
             ACCENT_MONTHLY)

    # Sequential arrows inside monthly tier (vertical-centered on card body)
    for x_from, x_to in [(14.8, 15.4), (28.6, 29.2)]:
        arrow(ax, x_from, 10.9, x_to, 10.9,
              color=ACCENT_MONTHLY, lw=1.6)

    # (no per-source arrows — they crowd the area between the strip and the
    # scheduler panel without adding real information)

    # ── STARTUP PHASES (lower-left) ───────────────────────────────────
    panel(ax, 0.5, 3.2, 22.5, 5.0, "Startup phases  (main.py lifespan)")
    phases = [
        ("Phase 1  ·  BLOCKING",
         "startup_essential()\n• build DI container\n• inject handlers\n• server READY"),
        ("Phase 2  ·  arms scheduler",
         "start_background_jobs()\n• register all tiers\n• scheduler.start()"),
        ("Phase 3  ·  background task",
         "startup_background_pipelines()\n• runs each *_on_startup=true\n  pipeline ONCE, concurrent\n  with live traffic"),
    ]
    ph_w = 6.7
    for i, (t, body) in enumerate(phases):
        x = 1.0 + i * (ph_w + 0.3)
        box = FancyBboxPatch(
            (x, 3.5), ph_w, 4.0,
            boxstyle="round,pad=0.02,rounding_size=0.15",
            linewidth=1.2, edgecolor=ACCENT_CORE, facecolor="#0b1220",
        )
        ax.add_patch(box)
        ax.text(x + ph_w / 2, 7.15, t, color=ACCENT_CORE,
                ha="center", va="top", fontsize=9.2, weight="bold")
        ax.text(x + 0.3, 6.4, body, color=TEXT,
                ha="left", va="top", fontsize=8.2)
    # Phase arrows
    for x_from, x_to in [(7.7, 8.0), (14.4, 14.7)]:
        arrow(ax, x_from, 5.5, x_to, 5.5, color=ACCENT_CORE, lw=1.8)

    # ── READ PATH (lower-right) ───────────────────────────────────────
    panel(ax, 23.5, 3.2, 20, 5.0, "Read path  ·  GET /v1/venues/nearby",
          title_color=ACCENT_API)
    steps = [
        ("Mobile",        "GET ?lat&lon&\nradius&verbose"),
        ("FastAPI",       "venue_router →\nvenue_handler"),
        ("Handler",       "GEOSEARCH +\nHGET keys"),
        ("Redis",         "venue + live +\nweekly payload"),
    ]
    sw = 4.4
    for i, (lbl, body) in enumerate(steps):
        x = 24.0 + i * (sw + 0.2)
        box = FancyBboxPatch(
            (x, 3.6), sw, 3.7,
            boxstyle="round,pad=0.02,rounding_size=0.15",
            linewidth=1.2, edgecolor=ACCENT_API, facecolor="#0b1220",
        )
        ax.add_patch(box)
        ax.text(x + sw / 2, 6.95, lbl, color=ACCENT_API,
                ha="center", va="top", fontsize=10, weight="bold")
        ax.text(x + sw / 2, 6.0, body, color=TEXT,
                ha="center", va="top", fontsize=8.3,
                family="DejaVu Sans Mono")
    for i in range(3):
        x_from = 24.0 + i * (sw + 0.2) + sw
        arrow(ax, x_from, 5.4, x_from + 0.2, 5.4,
              color=ACCENT_API, lw=1.6)
    ax.text(33.5, 3.05,
            "verbose=false → minified mobile shape   ·   "
            "verbose=true → full venue/live/weekly",
            color=MUTED, ha="center", va="top", fontsize=8, style="italic")

    # ── ADMIN / DEBUG strip (bottom) ─────────────────────────────────
    panel(ax, 0.5, 0.4, 43, 2.4,
          "Ops surface", body_color="#111827")
    ops = [
        "POST /admin/trigger/{job_name}  ·  manual fire",
        "GET  /admin/jobs                ·  list registered jobs",
        "POST /admin/recount-discovery-points",
        "GET  /debug/*                   ·  raw Redis introspection",
        "GET  /health  /ping  /metrics   ·  liveness + Prometheus",
    ]
    for i, line in enumerate(ops):
        ax.text(1.0 + (i % 3) * 14.3,
                2.05 - (i // 3) * 0.7,
                "• " + line,
                color=TEXT, fontsize=8.5, family="DejaVu Sans Mono",
                va="top")

    # ── Legend ────────────────────────────────────────────────────────
    legend_items = [
        (ACCENT_CORE,    "Core (always-on)"),
        (ACCENT_DAILY,   "Daily 03:00"),
        (ACCENT_WEEKLY,  "Weekly (Mon / Wed)"),
        (ACCENT_MONTHLY, "Monthly 1st (staggered)"),
        (ACCENT_API,     "Read path"),
        (ACCENT_EXT,     "External source"),
    ]
    lx = 4.0
    ly = 29.55
    ax.text(lx - 1.6, ly + 0.1, "Legend", color=MUTED,
            fontsize=8.5, weight="bold", va="center")
    for i, (color, label) in enumerate(legend_items):
        ax.add_patch(Rectangle((lx + i * 6.4, ly - 0.05), 0.45, 0.3,
                               facecolor=color, edgecolor="none"))
        ax.text(lx + i * 6.4 + 0.6, ly + 0.1, label,
                color=TEXT, fontsize=8.4, va="center")

    # ── Footer ────────────────────────────────────────────────────────
    ax.text(22, 0.1,
            "Sources: main.py · app/config.py · config.example.json    "
            "·    every job emits BACKGROUND_JOB_{DURATION,RUNS,LAST_RUN} metrics    "
            "·    pipelines no-op when their flag/API key is absent",
            color=MUTED, ha="center", va="bottom",
            fontsize=8, style="italic")

    png_path = OUT_DIR / "pipelines.png"
    svg_path = OUT_DIR / "pipelines.svg"
    fig.savefig(png_path, dpi=180, facecolor=BG,
                bbox_inches="tight", pad_inches=0.25)
    fig.savefig(svg_path, facecolor=BG, bbox_inches="tight", pad_inches=0.25)
    print(f"wrote {png_path}  ({png_path.stat().st_size // 1024} KB)")
    print(f"wrote {svg_path}  ({svg_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
