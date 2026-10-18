"""Rebuild docs/architecture.png (needs matplotlib): python docs/make_architecture.py"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

INK, MUTED, TEAL, BLUE, AMBER, GREY = "#1f2937", "#6b7280", "#0f766e", "#2563eb", "#d97706", "#9ca3af"
plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK})
fig, ax = plt.subplots(figsize=(11.4, 5.2))
ax.set_xlim(0, 11.4)
ax.set_ylim(0, 5.2)
ax.axis("off")


def box(x, y, w, h, colour, title, body, fill="white", title_colour=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.1", fc=fill, ec=colour, lw=1.8))
    ax.text(x + 0.18, y + h - 0.3, title, fontsize=11.5, fontweight="bold", color=title_colour or colour, va="center")
    ax.text(x + 0.18, y + h - 0.62, body, fontsize=9.3, va="top", linespacing=1.5)


def arrow(x1, y1, x2, y2):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15, color=MUTED, lw=1.5,
                                 connectionstyle="arc3,rad=0"))


box(0.25, 2.0, 2.3, 1.5, INK, "Telegram", "aiogram bot\nreplies to one\nallow-listed user")
box(3.0, 2.0, 2.0, 1.5, INK, "Router", "decides what\neach message needs")
arrow(2.6, 2.75, 2.95, 2.75)

box(5.9, 3.75, 5.25, 1.2, BLUE, "Model fallback chain", "6 Gemini tiers, best quality first, quota-aware:\nfalls back to higher-quota models as limits are hit")
box(5.9, 2.1, 5.25, 1.5, TEAL, "RAG memory", "embed each message (text-embedding-004)\n-> SQLite BLOB vectors -> cosine top-k\n-> retrieved history goes into the prompt")
box(5.9, 0.45, 5.25, 1.5, AMBER, "Tools", "Google Calendar and Gmail (OAuth 2.0)\nTodoist · web search · PDF parsing\nesports fixtures · image generation")
for y in (4.35, 2.85, 1.2):
    arrow(5.05, 2.75, 5.85, y)

ax.text(0.25, 1.35, "Daily jobs: email digest (8 AM) and a job watcher\nover public Greenhouse, Lever and Ashby feeds", fontsize=9, color=MUTED, va="top", linespacing=1.5)
ax.text(5.7, 0.05, "One Linux server, supervised by systemd · memory in a single SQLite file · free-tier API quotas",
        ha="center", fontsize=9, color=MUTED)
out = Path(__file__).resolve().parent / "architecture.png"
fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
print("wrote", out)
