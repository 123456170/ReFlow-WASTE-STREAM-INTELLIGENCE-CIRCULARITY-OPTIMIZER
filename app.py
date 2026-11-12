"""
ReFlow — Waste Stream Intelligence & Circularity Optimizer (single-file MVP)
===========================================================================
• Auto-seeds a realistic SYNTHETIC demo dataset (SQLite) on first launch.
• Starts a live 12-month simulation immediately — interactive from second one.
• Classifies waste streams, quantifies material flows (Sankey), and optimizes
  reuse / recycling / composting / energy recovery / landfill under capacity,
  budget and diversion constraints (deterministic LP, SciPy HiGHS).
• Full workflow: import (CSV/XLSX) → validate → review → approve → export,
  with roles, audit trail, undo, overrides, lineage and in-app self-tests.

Run:  pip install -r requirements.txt
      streamlit run app.py

Decision support only. All data and factors are synthetic/illustrative.
Human approval is required before any real-world action.
"""

from __future__ import annotations

import io
import json
import math
import random
import sqlite3
import time
import hashlib
import datetime as dt
from contextlib import closing
from dataclasses import dataclass
from html import escape as esc
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from scipy.optimize import linprog
    HAS_SCIPY = True
except Exception:  # graceful degradation — greedy fallback keeps app usable
    HAS_SCIPY = False

# ----------------------------------------------------------------------------
# Constants, catalogs (synthetic, dated, versioned — replace before production)
# ----------------------------------------------------------------------------
APP_NAME = "ReFlow"
APP_TAG = "Waste Stream Intelligence & Circularity Optimizer"
RULES_VERSION = "rules-1.3.0"
FACTORS_VERSION = "factors-2026.11-synthetic"
SCHEMA_VERSION = 1
SEED = 42
HAUL_RATE = 0.12  # € per tonne per km
PERIOD_LABEL = "Nov 2026"
MONTHS = pd.date_range("2026-11-01", periods=12, freq="MS")
SEASON = [1.00, 1.14, 0.88, 0.86, 0.92, 0.98, 1.03, 1.07, 1.06, 1.03, 0.97, 0.99]

EMERALD, CYAN, LIME, AMBER, ROSE, TEAL = "#34D399", "#22D3EE", "#A3E635", "#FBBF24", "#FB7185", "#2DD4BF"
MUTED = "#93B0A4"

PATH_ORDER = ["reuse", "recycle", "compost", "energy", "landfill"]
P = {
    "reuse":    {"label": "Reuse / Refurb",  "color": LIME,     "fee": 28.0,  "yield": 1.00, "residue": 0.00, "cap": 240.0},
    "recycle":  {"label": "Recycling",       "color": CYAN,     "fee": 62.0,  "yield": 0.90, "residue": 0.10, "cap": 1450.0},
    "compost":  {"label": "Composting / AD", "color": EMERALD,  "fee": 46.0,  "yield": 0.85, "residue": 0.15, "cap": 900.0},
    "energy":   {"label": "Energy recovery", "color": AMBER,    "fee": 92.0,  "yield": 0.75, "residue": 0.25, "cap": 1500.0},
    "landfill": {"label": "Landfill",        "color": ROSE,     "fee": 128.0, "yield": 1.00, "residue": 0.00, "cap": 1700.0},
}

STREAM_ORDER = ["MIX", "PET", "OCC", "ORG", "GLS", "FER", "ALU", "WEE", "TEX", "RES"]
STREAMS = {
    "MIX": dict(name="Mixed recyclables", ewc="15 01 06", color="#E879F9", base=118.0, cont=0.14, price=48.0,
                reuse=False, recycle=True, compost=False, energy=True,
                co2={"recycle": -0.85, "energy": 0.45, "landfill": 0.08},
                kw=[("mixed recycl", 3), ("commingled", 3), ("mrf", 2), ("mixed packaging", 2)],
                desc=["commingled household recyclables", "mixed packaging from MRF line"]),
    "PET": dict(name="PET bottles", ewc="15 01 02", color="#60A5FA", base=36.0, cont=0.08, price=310.0,
                reuse=False, recycle=True, compost=False, energy=True,
                co2={"recycle": -1.55, "energy": 1.35, "landfill": 0.05},
                kw=[("pet", 3), ("water bottle", 2), ("plastic bottle", 2), ("polyethylene terephthalate", 3)],
                desc=["clear PET bottles, baled", "PET beverage bottles from deposit scheme"]),
    "OCC": dict(name="Cardboard (OCC)", ewc="15 01 01", color="#FB923C", base=82.0, cont=0.07, price=92.0,
                reuse=True, recycle=True, compost=False, energy=True,
                co2={"reuse": -1.15, "recycle": -0.95, "energy": -0.10, "landfill": 0.38},
                kw=[("cardboard", 3), ("corrugated", 3), ("occ", 2), ("carton", 2), ("paper box", 2)],
                desc=["baled corrugated cardboard", "OCC from retail backhaul"]),
    "ORG": dict(name="Organics / food", ewc="20 02 01", color="#4ADE80", base=148.0, cont=0.09, price=0.0,
                reuse=False, recycle=False, compost=True, energy=True,
                co2={"compost": -0.24, "energy": -0.12, "landfill": 0.62},
                kw=[("food", 3), ("organic", 3), ("kitchen", 2), ("garden", 2), ("green waste", 2), ("scraps", 2)],
                desc=["source-separated food scraps", "kitchen and garden organics"]),
    "GLS": dict(name="Glass packaging", ewc="20 01 02", color=TEAL, base=44.0, cont=0.06, price=26.0,
                reuse=True, recycle=True, compost=False, energy=False,
                co2={"reuse": -0.55, "recycle": -0.32, "landfill": 0.04},
                kw=[("glass", 3), ("jar", 2), ("cullet", 3), ("bottle bank", 2)],
                desc=["mixed container glass", "rinsed clear glass bottles"]),
    "FER": dict(name="Ferrous metals", ewc="20 01 40", color="#94A3B8", base=28.0, cont=0.05, price=175.0,
                reuse=False, recycle=True, compost=False, energy=False,
                co2={"recycle": -1.60, "landfill": 0.04},
                kw=[("steel", 3), ("ferrous", 3), ("scrap metal", 2), ("iron", 2), ("tin can", 2)],
                desc=["shredded ferrous scrap", "steel cans from magnetic separation"]),
    "ALU": dict(name="Aluminium", ewc="20 01 40", color="#A78BFA", base=11.0, cont=0.04, price=940.0,
                reuse=False, recycle=True, compost=False, energy=False,
                co2={"recycle": -9.10, "landfill": 0.04},
                kw=[("aluminium", 3), ("aluminum", 3), ("beverage can", 2), ("alu", 2), ("cans", 1)],
                desc=["baled UBC aluminium cans", "aluminium foil and trays"]),
    "WEE": dict(name="E-waste (WEEE)", ewc="20 01 36", color="#FDE047", base=7.5, cont=0.05, price=230.0,
                reuse=True, recycle=True, compost=False, energy=False,
                co2={"reuse": -4.20, "recycle": -2.40, "landfill": 0.06},
                kw=[("electronic", 3), ("weee", 3), ("e-waste", 3), ("battery", 2), ("laptop", 2), ("cable", 2), ("printer", 2)],
                desc=["small WEEE, depolluted", "mixed electronic equipment"]),
    "TEX": dict(name="Textiles", ewc="20 01 11", color="#F472B6", base=21.0, cont=0.10, price=65.0,
                reuse=True, recycle=True, compost=False, energy=True,
                co2={"reuse": -2.10, "recycle": -1.20, "energy": 0.15, "landfill": 0.30},
                kw=[("textile", 3), ("clothing", 3), ("fabric", 2), ("apparel", 3), ("shoes", 1)],
                desc=["sorted post-consumer textiles", "clothing and household fabrics"]),
    "RES": dict(name="Residual / inert", ewc="20 03 01", color="#9CA3AF", base=138.0, cont=0.18, price=0.0,
                reuse=False, recycle=False, compost=False, energy=True,
                co2={"energy": 0.55, "landfill": 0.10},
                kw=[("residual", 3), ("inert", 2), ("rubble", 2), ("construction", 2), ("demolition", 2), ("mixed waste", 2), ("diaper", 2)],
                desc=["black-bag residual waste", "inert fines and street sweepings"]),
}

FAC = [
    ("F1", "Riverside MRF", 1.00, 1.0, "Materials recovery facility"),
    ("F2", "Northgate Depot", 0.80, 1.3, "Transfer station"),
    ("F3", "Harbor Works", 1.25, 0.8, "Industrial collection hub"),
    ("F4", "Old Mill Yard", 0.60, 1.6, "Civic amenity site"),
    ("F5", "Cedar Park", 0.75, 1.1, "Residential collection"),
    ("F6", "Eastworks Plant", 0.90, 0.9, "Commercial contracts"),
]
FAC_NAME = {f[0]: f[1] for f in FAC}
FAC_SCALE = {f[0]: f[2] for f in FAC}
FAC_DIST = {f[0]: f[3] for f in FAC}
FAC_COLORS = ["#0E9F6E", "#0EA5A5", "#34D399", "#22D3EE", "#10B981", "#2DD4BF"]

NAV = ["📡 Live Overview", "🗂️ Streams & Records", "🧮 Optimizer", "🛡️ Review & Audit", "📚 Reports & Governance"]
LIVE_PAGES = {NAV[0], NAV[2]}
ROLES = ["Administrator", "Analyst", "Reviewer", "Stakeholder"]
ROLE_ICON = {"Administrator": "🛡️ Administrator", "Analyst": "🧪 Analyst", "Reviewer": "✅ Reviewer", "Stakeholder": "👁️ Stakeholder"}
PERMS = {
    "view": set(ROLES), "export": set(ROLES),
    "edit": {"Administrator", "Analyst"}, "import": {"Administrator", "Analyst"},
    "submit": {"Administrator", "Analyst"}, "approve": {"Administrator", "Reviewer"},
    "override": {"Administrator", "Reviewer"}, "admin": {"Administrator"},
}
SC_DEFAULTS = {"target": 60, "carbon": 45, "budget_on": False, "budget": 350000, "capacity": 100,
               "fees": 100, "prices": 100, "distance": 22, "shock": 0, "energy_counts": True, "_v": 0}

DB_PATH = Path(__file__).resolve().parent / "waste_intelligence_demo.db"
IMPORT_DIR = Path(__file__).resolve().parent / "imports_archive"
SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
  id TEXT PRIMARY KEY, date TEXT NOT NULL, facility TEXT NOT NULL, stream_code TEXT NOT NULL,
  description TEXT DEFAULT '', tonnes REAL NOT NULL, contamination REAL NOT NULL,
  status TEXT NOT NULL, source TEXT DEFAULT 'manual', source_file TEXT DEFAULT '',
  source_row INTEGER DEFAULT 0, schema_version INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, role TEXT,
  action TEXT, entity TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY, created_at TEXT, period TEXT, params_json TEXT, result_json TEXT,
  alloc_json TEXT, status TEXT, submitted_by TEXT, reviewed_by TEXT DEFAULT '',
  review_note TEXT DEFAULT '', reviewed_at TEXT DEFAULT '',
  rules_version TEXT, factors_version TEXT);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, filename TEXT, sha256 TEXT,
  rows_total INTEGER, rows_ok INTEGER, rows_err INTEGER, actor TEXT);
"""

SS = st.session_state
CONFIG = {"displayModeBar": False, "responsive": True}

# ----------------------------------------------------------------------------
# Theme / CSS — palette derived from the subject (bio-industrial cockpit)
# ----------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;600;700&display=swap');
:root { --em:#34D399; --cy:#22D3EE; --li:#A3E635; --am:#FBBF24; --ro:#FB7185; --mut:#93B0A4; }
html, body, [data-testid="stAppViewContainer"] { font-family:'Inter',system-ui,sans-serif; color:#DFEFE7; }
#MainMenu, footer, [data-testid="stStatusWidget"], [data-testid="stToolbar"], [data-testid="stDecoration"] { display:none !important; }
[data-testid="stHeader"] { background:transparent; }
[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(1100px 750px at 88% -12%, rgba(16,185,129,.16), transparent 60%),
    radial-gradient(950px 700px at -8% 112%, rgba(34,211,238,.12), transparent 55%),
    radial-gradient(700px 500px at 55% 115%, rgba(163,230,53,.06), transparent 60%),
    linear-gradient(160deg,#0A1210 0%,#0C1714 52%,#0A1416 100%);
}
[data-testid="stAppViewContainer"]::before {
  content:""; position:fixed; inset:-20%; z-index:0; pointer-events:none; opacity:.55;
  background: radial-gradient(600px 420px at 30% 40%, rgba(34,211,238,.07), transparent 65%),
              radial-gradient(520px 420px at 70% 65%, rgba(52,211,153,.08), transparent 65%);
  animation: drift 22s ease-in-out infinite alternate;
}
[data-testid="stAppViewContainer"]::after {
  content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.05;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E");
}
[data-testid="stMain"] { position:relative; z-index:1; }
[data-testid="stMain"] p, [data-testid="stMain"] li, [data-testid="stMain"] label,
[data-testid="stMain"] span:not(.badge):not(.chip):not(.delta):not(.live):not(.dot),
[data-testid="stMain"] h1, [data-testid="stMain"] h2, [data-testid="stMain"] h3, [data-testid="stMain"] h4 { color:inherit; }
h1,h2,h3,h4 { font-family:'Space Grotesk','Inter',sans-serif !important; color:#F2FBF6 !important; letter-spacing:.01em; }
[data-testid="stCaptionContainer"], .stCaption, small { color:#7E998E !important; }
[data-testid="stSidebar"] { background:linear-gradient(180deg,#0B1512,#0A1310); border-right:1px solid rgba(52,211,153,.18); }
[data-testid="stSidebar"] * { color:#CFE6DB; }
[data-testid="stSidebar"] hr { border-color:rgba(52,211,153,.18); }
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-track { background:#0B1512; }
::-webkit-scrollbar-thumb { background:linear-gradient(180deg,#059669,#0891B2); border-radius:8px; border:2px solid #0B1512; }
::-webkit-scrollbar-thumb:hover { background:linear-gradient(180deg,#34D399,#22D3EE); }
* { scrollbar-width:thin; scrollbar-color:#059669 #0B1512; }
.stButton > button, .stDownloadButton > button, [data-testid="stBaseButton-secondary"] {
  background:rgba(16,185,129,.12); border:1px solid rgba(52,211,153,.35); color:#D9FBE9;
  border-radius:10px; font-weight:600; transition:all .18s ease; }
.stButton > button:hover, .stDownloadButton > button:hover { background:rgba(16,185,129,.24); border-color:#34D399; transform:translateY(-1px); box-shadow:0 6px 18px rgba(16,185,129,.18); color:#FFFFFF; }
.stButton > button[kind="primary"], [data-testid="stBaseButton-primary"], [data-testid="stFormSubmitButton"] > button {
  background:linear-gradient(90deg,#059669,#0891B2) !important; border:none !important; color:#fff !important;
  border-radius:10px; font-weight:700; box-shadow:0 4px 18px rgba(8,145,178,.30); }
.stButton > button[kind="primary"]:hover, [data-testid="stFormSubmitButton"] > button:hover { filter:brightness(1.12); transform:translateY(-1px); }
.stButton > button:disabled { opacity:.4; transform:none; box-shadow:none; }
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stDateInput input,
[data-baseweb="select"] > div { background:#0F1D18 !important; color:#E6F2EC !important; border-color:rgba(52,211,153,.30) !important; border-radius:10px !important; }
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] ul { background:#0F1D18 !important; border:1px solid rgba(52,211,153,.3); }
[data-baseweb="popover"] li { color:#DFEFE7 !important; }
[data-baseweb="popover"] li:hover { background:rgba(52,211,153,.15) !important; }
[data-testid="stDataFrame"], [data-testid="stDataEditor"] { background:#F6FAF8; border-radius:14px; border:1px solid rgba(16,185,129,.30); overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.35); }
[data-testid="stExpander"] { background:rgba(255,255,255,.03); border:1px solid rgba(52,211,153,.18); border-radius:14px; }
[data-testid="stExpander"] summary p { color:#BFE3D2 !important; font-weight:600; }
[data-testid="stForm"] { background:rgba(255,255,255,.03); border:1px solid rgba(52,211,153,.20); border-radius:14px; }
[data-testid="stFileUploaderDropzone"] { background:#0F1D18; border:1px dashed rgba(52,211,153,.45); border-radius:12px; }
[data-testid="stFileUploaderDropzone"] button { background:rgba(16,185,129,.15); color:#D9FBE9; border:1px solid rgba(52,211,153,.4); }
.stTabs [data-baseweb="tab-list"] { border-bottom:1px solid rgba(52,211,153,.2); gap:4px; }
.stTabs [data-baseweb="tab"] p { color:#93B0A4 !important; font-weight:600; }
.stTabs [aria-selected="true"] p { color:#34D399 !important; }
.stTabs [data-baseweb="tab-highlight"] { background:#34D399 !important; }
[data-testid="stProgress"] > div > div { background:rgba(52,211,153,.15); }
[data-testid="stProgress"] [role="progressbar"] > div { background:linear-gradient(90deg,#10B981,#22D3EE) !important; }
[data-testid="stToggle"] [aria-checked="true"] { background:#059669 !important; }
[role="slider"] { background-color:#34D399 !important; box-shadow:0 0 0 4px rgba(52,211,153,.15) !important; }
[data-testid="stSlider"] [data-baseweb="slider"] > div > div > div { background:linear-gradient(90deg,#059669,#22D3EE) !important; }
[data-testid="stAlert"] { border-radius:12px; }
pre, code { background:#0B1512 !important; color:#A7F3D0 !important; border:1px solid rgba(52,211,153,.2); border-radius:10px; }
.brand { display:flex; gap:11px; align-items:center; margin-bottom:2px; }
.brand .logo { width:44px; height:44px; border-radius:13px; background:conic-gradient(from 210deg,#059669,#22D3EE,#A3E635,#059669); display:flex; align-items:center; justify-content:center; font-size:1.4rem; box-shadow:0 0 26px rgba(34,211,238,.35); }
.brand .name { font-family:'Space Grotesk'; font-weight:700; font-size:1.35rem; background:linear-gradient(90deg,#6EE7B7,#22D3EE); -webkit-background-clip:text; background-clip:text; color:transparent; line-height:1.1; }
.brand .tag { font-size:.66rem; color:#7E998E; letter-spacing:.04em; }
.sec { display:flex; align-items:baseline; gap:10px; margin:4px 0 10px; font-family:'Space Grotesk'; font-size:1.28rem; font-weight:700; color:#F2FBF6; }
.sec .sub { font-size:.76rem; color:#7E998E; font-weight:400; font-family:'Inter'; }
.kpi-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(172px,1fr)); gap:12px; margin:6px 0 14px; }
.kpi { position:relative; overflow:hidden; padding:13px 15px 9px; border-radius:16px;
  background:linear-gradient(150deg,rgba(255,255,255,.055),rgba(255,255,255,.015));
  border:1px solid rgba(120,220,180,.16); animation:fadeUp .5s ease both; transition:all .2s ease; }
.kpi::before { content:""; position:absolute; top:0; left:0; width:100%; height:3px; background:linear-gradient(90deg,var(--acc),transparent); }
.kpi:hover { transform:translateY(-2px); border-color:rgba(120,220,180,.4); box-shadow:0 10px 30px rgba(16,185,129,.14); }
.kpi .lbl { font-size:.68rem; letter-spacing:.09em; text-transform:uppercase; color:#93B0A4; display:flex; justify-content:space-between; align-items:center; gap:6px; }
.kpi .val { font-family:'Space Grotesk'; font-size:1.5rem; font-weight:700; color:#F2FBF6; margin-top:3px; }
.kpi .sub { font-size:.70rem; color:#7E998E; margin-top:1px; }
.delta { font-size:.68rem; font-weight:700; padding:2px 8px; border-radius:99px; white-space:nowrap; }
.delta.pos { color:#34D399; background:rgba(52,211,153,.13); }
.delta.neg { color:#FB7185; background:rgba(251,113,133,.13); }
.badge { display:inline-block; padding:2px 10px; border-radius:99px; font-size:.71rem; font-weight:700; letter-spacing:.03em; border:1px solid; margin-right:5px; }
.b-em { color:#6EE7B7; border-color:rgba(52,211,153,.5); background:rgba(52,211,153,.12); }
.b-cy { color:#67E8F9; border-color:rgba(34,211,238,.5); background:rgba(34,211,238,.12); }
.b-am { color:#FCD34D; border-color:rgba(251,191,36,.5); background:rgba(251,191,36,.12); }
.b-ro { color:#FDA4AF; border-color:rgba(251,113,133,.5); background:rgba(251,113,133,.12); }
.b-sl { color:#B6C6BD; border-color:rgba(148,163,184,.45); background:rgba(148,163,184,.12); }
.chip { display:inline-block; padding:2px 10px; border-radius:99px; font-size:.71rem; color:#A9C4B8; border:1px solid rgba(120,220,180,.25); background:rgba(255,255,255,.04); margin:0 5px 4px 0; }
.live { display:inline-flex; align-items:center; gap:7px; padding:4px 13px; border-radius:99px; background:rgba(52,211,153,.12); border:1px solid rgba(52,211,153,.45); color:#6EE7B7; font-weight:700; font-size:.76rem; letter-spacing:.09em; }
.live .dot { width:8px; height:8px; border-radius:50%; background:#34D399; animation:pulse 1.4s infinite; }
.live.held { color:#FCD34D; border-color:rgba(251,191,36,.45); background:rgba(251,191,36,.10); }
.live.held .dot { background:#FBBF24; animation:none; }
.live.paused { color:#B6C6BD; border-color:rgba(148,163,184,.4); background:rgba(148,163,184,.10); }
.live.paused .dot { background:#94A3B8; animation:none; }
.feed { font-family:ui-monospace,Consolas,monospace; font-size:.75rem; line-height:1.9; color:#A9C4B8; background:rgba(255,255,255,.03); border:1px solid rgba(120,220,180,.14); border-radius:12px; padding:10px 13px; }
.feed .t { color:#22D3EE; } .feed .a { color:#FBBF24; }
.insight { background:linear-gradient(120deg,rgba(163,230,53,.09),rgba(34,211,238,.08)); border:1px solid rgba(163,230,53,.28); border-radius:14px; padding:12px 16px; color:#D9F5E5; font-size:.9rem; margin:8px 0; }
@keyframes pulse { 0%,100% { box-shadow:0 0 0 0 rgba(52,211,153,.5);} 55% { box-shadow:0 0 0 7px rgba(52,211,153,0);} }
@keyframes drift { from { transform:translate(-2%,-1%) rotate(-1deg);} to { transform:translate(2%,2%) rotate(1deg);} }
@keyframes fadeUp { from { opacity:0; transform:translateY(8px);} to { opacity:1; transform:none;} }
@media (max-width:900px){ .kpi-row { grid-template-columns:repeat(2,1fr);} .sec .sub { display:none; } }
</style>
"""

# ----------------------------------------------------------------------------
# Small helpers (version-tolerant width handling, formatting, HTML)
# ----------------------------------------------------------------------------
_WIDTH_MODE: dict = {}

def stretch(fn, *a, **k):
    name = fn.__name__
    if _WIDTH_MODE.get(name) == "old":
        return fn(*a, use_container_width=True, **k)
    try:
        out = fn(*a, width="stretch", **k)
        _WIDTH_MODE[name] = "new"
        return out
    except Exception as e:
        if isinstance(e, TypeError) or "width" in str(e).lower():
            _WIDTH_MODE[name] = "old"
            return fn(*a, use_container_width=True, **k)
        raise

def btn(label, key=None, type="secondary", disabled=False, help=None):
    return stretch(st.button, label, key=key, type=type, disabled=disabled, help=help)

def dl(label, data, file_name, mime, key=None, disabled=False):
    return stretch(st.download_button, label, data=data, file_name=file_name, mime=mime, key=key, disabled=disabled)

def show_df(df, height=None, column_config=None):
    return stretch(st.dataframe, df, height=height, column_config=column_config, hide_index=True)

def show_fig(fig, key=None):
    stretch(st.plotly_chart, fig, config=CONFIG, key=key)

def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def jdump(o):
    return json.dumps(o, default=lambda x: float(x) if isinstance(x, (np.floating, np.integer)) else str(x))

def rgba(hex_color, a):
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)},{a})"

def fmt_t(v): return f"{v:,.0f} t"
def fmt_co2(v): return f"{v:,.0f} t"
def fmt_eur(v):
    return ("−€" if v < 0 else "€") + f"{abs(v):,.0f}"

def badge(text, kind="sl"):
    return f'<span class="badge b-{kind}">{esc(str(text))}</span>'

STATUS_KIND = {"Draft": "sl", "Validated": "cy", "Approved": "em", "Flagged": "am",
               "Rejected": "ro", "Archived": "sl", "Pending": "am", "Optimal": "em", "Relaxed": "am"}

def chip(text):
    return f'<span class="chip">{esc(str(text))}</span>'

def sec(icon, title, sub=""):
    st.markdown(f'<div class="sec"><span>{icon}</span><span>{esc(title)}</span><span class="sub">{esc(sub)}</span></div>', unsafe_allow_html=True)

def spark_svg(vals, color):
    vals = [float(v) for v in vals][-10:]
    if len(vals) < 2:
        vals = (vals * 2) if vals else [0, 0]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 1e-9)
    pts = []
    for i, v in enumerate(vals):
        x = 4 + i * (102 / (len(vals) - 1))
        y = 26 - (v - lo) / span * 20
        pts.append(f"{x:.1f},{y:.1f}")
    last = pts[-1].split(",")
    return (f'<svg width="110" height="30" viewBox="0 0 110 30" style="margin-top:4px">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" opacity="0.9"/>'
            f'<circle cx="{last[0]}" cy="{last[1]}" r="2.6" fill="{color}"/></svg>')

def delta_html(cur, prev, up_good=True, fmt=lambda v: f"{v:,.0f}"):
    if prev is None:
        return ""
    d = float(cur) - float(prev)
    if abs(d) < 1e-9:
        return '<span class="delta pos">• flat</span>'
    good = (d > 0) if up_good else (d < 0)
    arrow = "▲" if d > 0 else "▼"
    return f'<span class="delta {"pos" if good else "neg"}">{arrow} {fmt(abs(d))}</span>'

def kpi_card(label, value, accent, sub="", delta="", spark=None):
    sp = spark_svg(spark, accent) if spark else ""
    return (f'<div class="kpi" style="--acc:{accent}"><div class="lbl"><span>{esc(label)}</span>{delta}</div>'
            f'<div class="val">{value}</div><div class="sub">{esc(sub)}</div>{sp}</div>')

def kpi_row(cards):
    st.markdown('<div class="kpi-row">' + "".join(cards) + "</div>", unsafe_allow_html=True)

def base_layout(fig, h):
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, sans-serif", color="#CFE6DB", size=12),
                      margin=dict(l=10, r=10, t=34, b=10), height=h,
                      legend=dict(orientation="h", y=1.14, x=0, bgcolor="rgba(0,0,0,0)"),
                      hoverlabel=dict(bgcolor="#0F1D18", font_color="#E6F2EC", bordercolor="#1F3A30"))
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor="rgba(148,190,175,.2)")
    fig.update_yaxes(gridcolor="rgba(148,190,175,.12)", zeroline=False)
    return fig

def can(perm):
    return SS.get("role", "Analyst") in PERMS[perm]

def guard(perm):
    if not can(perm):
        st.toast("Action not permitted for the current role.", icon="🚫")
        return False
    return True

# ----------------------------------------------------------------------------
# Data layer — SQLite, provenance, audit, safe re-import
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def audit(action, entity, details=""):
    with closing(db()) as c:
        c.execute("INSERT INTO audit(ts,actor,role,action,entity,details) VALUES(?,?,?,?,?,?)",
                  (now_iso(), "demo-user", SS.get("role", "Analyst"), action, str(entity), str(details)[:900]))
        c.commit()

def next_record_id(conn):
    row = conn.execute("SELECT id FROM records ORDER BY id DESC LIMIT 1").fetchone()
    n = int(row["id"].split("-")[-1]) + 1 if row else 1
    return f"WS-2026-{n:04d}"

def ensure_db():
    with closing(db()) as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0:
            seed_records(c)

def seed_records(c):
    sup = sim_supply(0, SEED)
    ts = "2026-11-10 08:00:00"
    for i, r in enumerate(sup.itertuples(index=False)):
        status = "Approved" if i % 9 == 0 else ("Draft" if i % 9 == 7 else "Validated")
        date = f"2026-11-{1 + i % 10:02d}"
        notes, cont, desc = "", float(r.cont), r.desc
        if r.facility == "F3" and r.stream == "GLS":
            cont, status, notes = 0.85, "Flagged", "Contamination 85% exceeds 60% bound — verify assay before use."
        if r.facility == "F2" and r.stream == "PET":
            desc, status, notes = "rinsed clear glass bottles, color-sorted", "Flagged", "Description contradicts code PET — reclassification required."
        if r.facility == "F4" and r.stream == "RES":
            date, status, notes = "2026-10-28", "Flagged", "Late arrival — belongs to prior period; confirm accrual."
        c.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"WS-2026-{i+1:04d}", date, r.facility, r.stream, desc, round(float(r.tonnes), 2),
                   round(cont, 4), status, "synthetic-seed-v1", "seed://synthetic-v1", i + 1,
                   SCHEMA_VERSION, ts, ts, notes))
    c.execute("INSERT INTO audit(ts,actor,role,action,entity,details) VALUES(?,?,?,?,?,?)",
              (ts, "system", "Administrator", "demo.seed", "db", "60 synthetic records seeded (seed=42, factors-2026.11-synthetic)"))
    c.commit()

def load_records():
    with closing(db()) as c:
        return pd.read_sql_query("SELECT * FROM records ORDER BY date DESC, id DESC", c)

def load_audit(limit=500, entity=None):
    q = "SELECT ts,actor,role,action,entity,details FROM audit"
    args = []
    if entity:
        q += " WHERE entity=?"
        args.append(str(entity))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with closing(db()) as c:
        return pd.read_sql_query(q, c, params=args)

def insert_record(row):
    with closing(db()) as c:
        rid = row.get("id") or next_record_id(c)
        ts = now_iso()
        c.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, row["date"], row["facility"], row["stream_code"], row.get("description", ""),
                   float(row["tonnes"]), float(row["contamination"]), row["status"], row.get("source", "manual"),
                   row.get("source_file", ""), int(row.get("source_row", 0)), SCHEMA_VERSION, ts, ts, row.get("notes", "")))
        c.commit()
    return rid

def update_record(rid, fields, action="record.update"):
    with closing(db()) as c:
        old = dict(c.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
        changes = []
        for k, v in fields.items():
            if str(old.get(k)) != str(v):
                changes.append(f"{k}: {old.get(k)} → {v}")
        fields = dict(fields)
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE records SET {sets} WHERE id=?", (*fields.values(), rid))
        c.commit()
    audit(action, rid, "; ".join(changes) if changes else "no-op")

def load_plans():
    with closing(db()) as c:
        return pd.read_sql_query("SELECT * FROM plans ORDER BY created_at DESC, id DESC", c)

def reset_demo():
    with closing(db()) as c:
        for t in ("records", "audit", "plans", "imports"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.commit()
    ensure_db()
    SS["sim"] = {"month": 0, "history": [], "running": True, "seed": SEED, "done": False}
    sim_step()
    SS["overrides"] = []
    SS["trash"] = None
    SS["last_tests"] = None
    audit("demo.reset", "db", "database re-seeded from synthetic generator")

# ----------------------------------------------------------------------------
# Domain engine — deterministic, typed, UI-independent
# ----------------------------------------------------------------------------
def classify_text(text):
    t = (text or "").lower()
    scores = {}
    matches = {}
    for code in STREAM_ORDER:
        s, hit = 0.0, []
        for term, w in STREAMS[code]["kw"]:
            if term in t:
                s += w
                hit.append(term)
        scores[code] = s
        matches[code] = hit
    ranked = sorted(STREAM_ORDER, key=lambda k: -scores[k])
    top, second = ranked[0], ranked[1]
    if scores[top] <= 0:
        return {"code": "RES", "confidence": 0.30, "matches": [], "reason": "No rule matched — defaulted to residual; manual review advised.",
                "alts": [(c, scores[c]) for c in ranked[:3]]}
    conf = min(0.98, max(0.35, scores[top] / (scores[top] + scores[second] + 0.5)))
    return {"code": top, "confidence": conf, "matches": matches[top],
            "reason": f"Matched rule terms: {', '.join(matches[top])} (weight {scores[top]:.0f}).",
            "alts": [(c, scores[c]) for c in ranked[:3]]}

def row_checks(date_s, facility, stream, tonnes_s, cont_s):
    """Shared validation for forms and imports. Returns (errors, warnings, normalized)."""
    errors, warns = [], []
    norm = {}
    d = None
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M"):
        try:
            d = dt.datetime.strptime(str(date_s).strip(), f)
            break
        except ValueError:
            continue
    if d is None:
        errors.append(f"date '{date_s}' is not a valid YYYY-MM-DD date")
    else:
        if not (dt.datetime(2020, 1, 1) <= d <= dt.datetime(2027, 12, 31)):
            errors.append("date out of range (2020-01-01 … 2027-12-31)")
        elif d < dt.datetime(2026, 11, 1):
            warns.append("late arrival (before Nov 2026) — imported as Flagged")
        norm["date"] = d.strftime("%Y-%m-%d")
    fac = str(facility).strip()
    fac_id = None
    for fid, name, *_ in FAC:
        if fac.lower() in (fid.lower(), name.lower()):
            fac_id = fid
            break
    if fac_id is None:
        errors.append(f"facility '{fac}' not in catalog (F1–F6 or facility name)")
    norm["facility"] = fac_id or fac
    sc = str(stream).strip().upper()
    if sc not in STREAMS:
        sugg = classify_text(str(stream))["code"]
        errors.append(f"stream_code '{stream}' unknown — nearest match from rules: {sugg}")
    norm["stream_code"] = sc
    try:
        t = float(tonnes_s)
        if not (0 < t <= 2000):
            errors.append("tonnes must be a number in (0, 2000]")
        norm["tonnes"] = t
    except (TypeError, ValueError):
        errors.append(f"tonnes '{tonnes_s}' is not a number — must be in (0, 2000]")
    try:
        cp = float(cont_s)
        if not (0 <= cp <= 60):
            errors.append("contamination_pct must be 0–60")
        norm["contamination"] = cp / 100.0
    except (TypeError, ValueError):
        errors.append(f"contamination_pct '{cont_s}' is not a number — must be 0–60")
    return errors, warns, norm

IMPORT_REQUIRED = ["date", "facility", "stream_code", "tonnes", "contamination_pct"]

def validate_import(raw, existing_ids):
    missing = [c for c in IMPORT_REQUIRED if c not in raw.columns]
    if missing:
        return [], None, f"Missing required columns: {', '.join(missing)}. Required: {', '.join(IMPORT_REQUIRED)} (optional: record_id, description, source)."
    report = raw.copy()
    statuses, errs_col, ok_rows = [], [], []
    seen = set()
    for i, r in raw.iterrows():
        errors, warns = [], []
        for col in IMPORT_REQUIRED:
            if str(r.get(col, "")).strip() == "":
                errors.append(f"missing required field '{col}'")
        e2, w2, norm = row_checks(r.get("date", ""), r.get("facility", ""), r.get("stream_code", ""),
                                  r.get("tonnes", ""), r.get("contamination_pct", ""))
        errors += e2
        warns += w2
        rid = str(r.get("record_id", "")).strip()
        if rid:
            if rid in existing_ids:
                errors.append(f"duplicate record_id '{rid}' (already exists — validated data is never overwritten)")
            if rid in seen:
                errors.append(f"duplicate record_id '{rid}' (repeated in file)")
            seen.add(rid)
        if errors:
            statuses.append("⛔ ERROR")
        elif warns:
            statuses.append("⚠️ WARN")
        else:
            statuses.append("✅ OK")
        errs_col.append("; ".join(errors + warns))
        if not errors:
            ok_rows.append({**norm, "record_id": rid, "description": str(r.get("description", "")).strip(),
                            "source": str(r.get("source", "import")).strip() or "import",
                            "notes": "; ".join(warns), "status": "Flagged" if warns else "Validated",
                            "source_row": int(i) + 2})
    report["_status"] = statuses
    report["_errors"] = errs_col
    return ok_rows, report, None

def season_factor(stream, m):
    f = SEASON[m]
    if m == 1 and stream in ("OCC", "PET", "MIX"):
        f *= 1.22
    if m == 1 and stream == "GLS":
        f *= 1.15
    if stream == "ORG" and m in (7, 8, 9):
        f *= 1.15
    return f

def sim_supply(m, seed=SEED):
    rng = np.random.default_rng(seed * 1000 + m)
    rows = []
    for fid, name, scale, dist, _ in FAC:
        for s in STREAM_ORDER:
            S = STREAMS[s]
            t = S["base"] * scale * season_factor(s, m) * float(rng.lognormal(0, 0.08))
            c = float(np.clip(S["cont"] * rng.normal(1, 0.18), 0.01, 0.60))
            rows.append({"facility": fid, "stream": s, "tonnes": t, "cont": c, "desc": S["desc"][int(rng.integers(0, 2))]})
    return pd.DataFrame(rows)

def aggregate_supply(rows, sc):
    rows = rows.copy()
    rows["cont_adj"] = (rows["cont"] * (1 + sc["shock"] / 100.0)).clip(0.0, 0.60)
    rows["dirty"] = rows["tonnes"] * rows["cont_adj"]
    rows["clean"] = rows["tonnes"] - rows["dirty"]
    rows["dfac"] = rows["facility"].map(FAC_DIST).fillna(1.0)
    recs = []
    for s, d in rows.groupby("stream"):
        tot = float(d["tonnes"].sum())
        recs.append({"stream": s, "supply": tot, "clean": float(d["clean"].sum()),
                     "dirty": float(d["dirty"].sum()),
                     "dist_factor": float((d["dfac"] * d["tonnes"]).sum() / max(tot, 1e-9))})
    return pd.DataFrame(recs).set_index("stream")

def allowed_paths(s, frac, overrides=None):
    S = STREAMS[s]
    if frac == "dirty":
        return (["energy"] if S["energy"] else []) + ["landfill"]
    opts = [p for p in PATH_ORDER if p == "landfill" or S.get(p, False)]
    for ov in (overrides or []):
        if ov["stream"] == s and ov["pathway"] in opts:
            return [ov["pathway"]]
    return opts

def revenue_per_t(s, p, sc):
    m = sc["prices"] / 100.0
    if p == "reuse":
        return STREAMS[s]["price"] * 1.2 * m
    if p == "recycle":
        return STREAMS[s]["price"] * P["recycle"]["yield"] * m
    if p == "compost":
        return 15.0 * P["compost"]["yield"] * m
    return 0.0

@dataclass
class Plan:
    status: str
    message: str
    alloc: pd.DataFrame
    kpis: dict
    capacity: pd.DataFrame
    shadows: dict
    flags: list
    objective: float
    solver: str
    n_vars: int

ALLOC_COLS = ["stream", "pathway", "tonnes", "fee_eur", "transport_eur", "revenue_eur", "residue_eur", "cost_eur", "co2_t", "residue_t"]

def empty_plan(msg="No usable supply — validate or approve records first."):
    kpis = {"input_t": 0, "cost": 0, "co2": 0, "recovered_t": 0, "diversion": 0, "landfill_t": 0,
            "energy_t": 0, "residue_t": 0, "path_t": {p: 0 for p in PATH_ORDER}}
    cap = pd.DataFrame([{"pathway": p, "label": P[p]["label"], "used": 0, "cap": P[p]["cap"], "util": 0} for p in PATH_ORDER])
    return Plan("No data", msg, pd.DataFrame(columns=ALLOC_COLS), kpis, cap, {}, [], 0.0, "—", 0)

def plan_from_x(varlist, x, input_t, sc, status, message, flags, shadows, objective, solver):
    acc = {}
    for v, q in zip(varlist, x):
        if q <= 1e-7:
            continue
        key = (v["s"], v["p"])
        a = acc.setdefault(key, dict.fromkeys(ALLOC_COLS[2:], 0.0))
        a["tonnes"] += q
        a["fee_eur"] += q * v["fee"]
        a["transport_eur"] += q * v["transport"]
        a["revenue_eur"] += q * v["rev"]
        a["residue_eur"] += q * v["res_fin"]
        a["cost_eur"] += q * v["fin"]
        a["co2_t"] += q * v["co2e"]
        a["residue_t"] += q * v["res"]
    alloc = pd.DataFrame([{"stream": s, "pathway": p, **a} for (s, p), a in acc.items()], columns=ALLOC_COLS)
    path_t = {p: float(alloc.loc[alloc.pathway == p, "tonnes"].sum()) for p in PATH_ORDER}
    residue_t = float(alloc["residue_t"].sum())
    recovered = path_t["reuse"] + path_t["recycle"] * P["recycle"]["yield"] + path_t["compost"] * P["compost"]["yield"]
    energy_div = path_t["energy"] if sc["energy_counts"] else 0.0
    kpis = {"input_t": float(input_t), "cost": float(alloc["cost_eur"].sum()), "co2": float(alloc["co2_t"].sum()),
            "recovered_t": recovered, "diversion": 100.0 * (recovered + energy_div) / max(input_t, 1e-9),
            "landfill_t": path_t["landfill"] + residue_t, "energy_t": path_t["energy"],
            "residue_t": residue_t, "path_t": path_t}
    cap_rows = []
    for p in PATH_ORDER:
        used = path_t[p] + (residue_t if p == "landfill" else 0.0)
        cap = P[p]["cap"] * sc["capacity"] / 100.0
        cap_rows.append({"pathway": p, "label": P[p]["label"], "used": used, "cap": cap, "util": 100.0 * used / max(cap, 1e-9)})
    return Plan(status, message, alloc, kpis, pd.DataFrame(cap_rows), shadows, flags, float(objective), solver, len(varlist))

def greedy_solve(varlist, eqs, caps, input_t, sc, reason):
    n = len(varlist)
    x = np.zeros(n)
    rem = {k: q for k, q in eqs.items()}
    capleft = dict(caps)
    for i in sorted(range(n), key=lambda i: varlist[i]["objc"]):
        v = varlist[i]
        key = (v["s"], v["frac"])
        q = rem.get(key, 0.0)
        if q <= 1e-9:
            continue
        room = max(capleft[v["p"]], 0.0)
        if v["res"] > 0:
            room = min(room, max(capleft["landfill"], 0.0) / v["res"])
        take = min(q, room)
        x[i] = take
        rem[key] = q - take
        capleft[v["p"]] -= take
        capleft["landfill"] -= take * v["res"]
    flags = []
    for key, q in rem.items():
        if q > 1e-6:
            for i, v in enumerate(varlist):
                if (v["s"], v["frac"]) == key and v["p"] == "landfill":
                    x[i] += q
                    flags.append(f"Landfill emergency overflow +{q:,.0f} t ({key[0]} {key[1]})")
                    break
    return plan_from_x(varlist, x, input_t, sc, "Greedy fallback",
                       f"{reason} Priority-greedy allocation used — feasible but not proven optimal.",
                       flags, {}, float(sum(v["objc"] * q for v, q in zip(varlist, x))), "Greedy")

def solve_plan(agg, sc, overrides=None):
    streams = [s for s in STREAM_ORDER if s in agg.index and agg.loc[s, "supply"] > 1e-9]
    if not streams:
        return empty_plan()
    input_t = float(agg["supply"].sum())
    varlist, eqs = [], {}
    for s in streams:
        for frac in ("clean", "dirty"):
            q = float(agg.loc[s, frac])
            if q <= 1e-9:
                continue
            eqs[(s, frac)] = q
            for p in allowed_paths(s, frac, overrides):
                fee = P[p]["fee"] * sc["fees"] / 100.0
                transport = HAUL_RATE * sc["distance"] * float(agg.loc[s, "dist_factor"])
                rev = revenue_per_t(s, p, sc)
                res = P[p]["residue"]
                res_fin = res * P["landfill"]["fee"] * sc["fees"] / 100.0
                fin = fee + transport - rev + res_fin
                co2e = STREAMS[s]["co2"][p] + res * STREAMS[s]["co2"]["landfill"]
                divc = {"reuse": 1.0, "recycle": P["recycle"]["yield"], "compost": P["compost"]["yield"],
                        "energy": 1.0 if sc["energy_counts"] else 0.0, "landfill": 0.0}[p]
                varlist.append({"s": s, "frac": frac, "p": p, "fee": fee, "transport": transport, "rev": rev,
                                "res": res, "res_fin": res_fin, "fin": fin, "co2e": co2e, "divc": divc,
                                "objc": fin + sc["carbon"] * co2e})
    n = len(varlist)
    caps = {p: P[p]["cap"] * sc["capacity"] / 100.0 for p in PATH_ORDER}
    if not HAS_SCIPY:
        return greedy_solve(varlist, eqs, caps, input_t, sc, "SciPy unavailable.")

    eq_keys = list(eqs.keys())
    A_eq = np.zeros((len(eq_keys), n))
    b_eq = np.array([eqs[k] for k in eq_keys])
    for j, v in enumerate(varlist):
        A_eq[eq_keys.index((v["s"], v["frac"])), j] = 1.0
    roles = [("cap", p) for p in PATH_ORDER] + [("div", None)] + ([("budget", None)] if sc["budget_on"] else [])
    A_ub = np.zeros((len(roles), n))
    b_ub = np.zeros(len(roles))
    for i, (kind, p) in enumerate(roles):
        if kind == "cap":
            b_ub[i] = caps[p]
            for j, v in enumerate(varlist):
                A_ub[i, j] = (1.0 if v["p"] == p else 0.0) + (v["res"] if p == "landfill" and v["p"] != "landfill" else 0.0)
        elif kind == "div":
            b_ub[i] = -sc["target"] / 100.0 * input_t
            for j, v in enumerate(varlist):
                A_ub[i, j] = -v["divc"]
        else:
            b_ub[i] = float(sc["budget"])
            for j, v in enumerate(varlist):
                A_ub[i, j] = v["fin"]
    obj = np.array([v["objc"] for v in varlist])
    res = linprog(obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=(0, None), method="highs")
    if res.success:
        shadows = {}
        marg = res.ineqlin.marginals
        for i, (kind, p) in enumerate(roles):
            usage = float(A_ub[i] @ res.x)
            if kind == "cap" and usage >= b_ub[i] - 1e-3 and abs(marg[i]) > 1e-6:
                shadows[p] = abs(float(marg[i]))
            if kind == "div" and abs(marg[i]) > 1e-6:
                shadows["diversion"] = abs(float(marg[i]))
        return plan_from_x(varlist, res.x, input_t, sc, "Optimal",
                           "All hard constraints satisfied (mass balance, capacity, diversion" +
                           (", budget" if sc["budget_on"] else "") + ").", [], shadows, res.fun, "HiGHS LP")
    if res.status == 2:  # infeasible → penalty-slack relaxation, never a dead end
        keep = [i for i, r in enumerate(roles) if r[0] != "budget"]
        roles2 = [roles[i] for i in keep]
        m2 = len(roles2)
        nsl = 1 + len(PATH_ORDER)
        A2 = np.zeros((m2, n + nsl))
        A2[:, :n] = A_ub[keep]
        b2 = b_ub[keep]
        for i, (kind, p) in enumerate(roles2):
            if kind == "div":
                A2[i, n] = -1.0
            if kind == "cap":
                A2[i, n + 1 + PATH_ORDER.index(p)] = -1.0
        obj2 = np.concatenate([obj, [900.0], [3 * P[p]["fee"] + 150.0 for p in PATH_ORDER]])
        res2 = linprog(obj2, A_ub=A2, b_ub=b2, A_eq=A_eq, b_eq=b_eq, bounds=(0, None), method="highs")
        if res2.success:
            flags = []
            if res2.x[n] > 1e-6:
                flags.append(f"Diversion target missed by {res2.x[n]:,.0f} t — shortfall penalized at €900/t.")
            for k, p in enumerate(PATH_ORDER):
                ov = res2.x[n + 1 + k]
                if ov > 1e-6:
                    flags.append(f"{P[p]['label']} capacity exceeded by {ov:,.0f} t — emergency rate applied.")
            if sc["budget_on"]:
                flags.append("Budget constraint suspended during relaxation.")
            return plan_from_x(varlist, res2.x[:n], input_t, sc, "Relaxed",
                               "Infeasible at current settings — solved with penalty slack. Review flags before approval.",
                               flags, {}, res2.fun, "HiGHS LP (relaxed)")
    return greedy_solve(varlist, eqs, caps, input_t, sc, "LP solver could not certify a solution.")

def sensitivity_table(agg, sc, overrides):
    rows = []
    for k, lbl in ((0.8, "−20% (optimistic)"), (1.0, "Base"), (1.2, "+20% (pessimistic)")):
        sc2 = dict(sc)
        sc2["shock"] = ((1 + sc["shock"] / 100.0) * k - 1) * 100.0
        pl = solve_plan(agg, sc2, overrides)
        rows.append({"Contamination": lbl, "Net cost": fmt_eur(pl.kpis["cost"]),
                     "Diversion %": round(pl.kpis["diversion"], 1), "Net CO₂e (t)": round(pl.kpis["co2"], 0),
                     "Solver status": pl.status})
    return pd.DataFrame(rows)

# ----------------------------------------------------------------------------
# Simulation — seeded, reproducible arrivals; live scenario coupling
# ----------------------------------------------------------------------------
def sim_step():
    sim = SS["sim"]
    m = sim["month"]
    if m >= 12:
        sim["running"] = False
        sim["done"] = True
        return
    sc = SS["scenario"]
    rows = sim_supply(m, sim["seed"])
    agg = aggregate_supply(rows, sc)
    pl = solve_plan(agg, sc, SS["overrides"])
    sim["history"].append({"label": MONTHS[m].strftime("%b %Y"), "target": sc["target"],
                           **{k: pl.kpis[k] for k in ("input_t", "cost", "co2", "recovered_t", "diversion", "landfill_t", "energy_t")},
                           "path_t": pl.kpis["path_t"], "cap_util": dict(zip(pl.capacity.pathway, pl.capacity.util)),
                           "shadow_recycle": pl.shadows.get("recycle", 0.0), "status": pl.status})
    sim["month"] += 1
    if sim["month"] >= 12:
        sim["running"] = False
        sim["done"] = True

def sim_replay():
    SS["sim"] = {"month": 0, "history": [], "running": True, "seed": SEED, "done": False}
    sim_step()

def init_state():
    SS.setdefault("role", "Analyst")
    SS.setdefault("nav", NAV[0])
    SS.setdefault("speed", 1.0)
    SS.setdefault("scenario", dict(SC_DEFAULTS))
    SS.setdefault("overrides", [])
    SS.setdefault("trash", None)
    SS.setdefault("nr_v", 0)
    if "sim" not in SS:
        SS["sim"] = {"month": 0, "history": [], "running": True, "seed": SEED, "done": False}
        for _ in range(3):  # first paint already shows a living dashboard
            sim_step()

# ----------------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------------
def fig_live(hist):
    x = hist["label"].tolist()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=hist["diversion"], name="Diversion %", line=dict(color=EMERALD, width=3),
                             fill="tozeroy", fillcolor=rgba(EMERALD, 0.10), hovertemplate="%{y:.1f}%<extra>Diversion</extra>"))
    fig.add_trace(go.Scatter(x=x, y=hist["target"], name="Target", line=dict(color="#8AA39A", width=1.5, dash="dash"),
                             hovertemplate="%{y:.0f}%<extra>Target</extra>"))
    fig.add_trace(go.Scatter(x=x, y=hist["cost"] / 1000.0, name="Net cost (€k)", yaxis="y2",
                             line=dict(color=CYAN, width=2.5), hovertemplate="€%{y:,.0f}k<extra>Net cost</extra>"))
    base_layout(fig, 300)
    fig.update_layout(hovermode="x unified",
                      yaxis=dict(range=[0, 100], title="Diversion %"),
                      yaxis2=dict(overlaying="y", side="right", title="€k", showgrid=False))
    return fig

def fig_stack(hist):
    mix = pd.DataFrame(hist["path_t"].tolist()).reindex(columns=PATH_ORDER).fillna(0)
    fig = go.Figure()
    for p in PATH_ORDER:
        fig.add_trace(go.Bar(x=hist["label"], y=mix[p], name=P[p]["label"], marker_color=P[p]["color"],
                             hovertemplate="%{y:,.0f} t<extra>" + P[p]["label"] + "</extra>"))
    base_layout(fig, 260)
    fig.update_layout(barmode="stack", yaxis_title="Tonnes routed")
    return fig

def fig_donut(cur):
    vals = [cur["path_t"][p] for p in PATH_ORDER]
    fig = go.Figure(go.Pie(labels=[P[p]["label"] for p in PATH_ORDER], values=vals, hole=0.62,
                           marker=dict(colors=[P[p]["color"] for p in PATH_ORDER], line=dict(color="#0C1714", width=2)),
                           textinfo="percent", insidetextfont=dict(color="#08130F", size=11),
                           hovertemplate="%{label}: %{value:,.0f} t<extra></extra>"))
    base_layout(fig, 265)
    fig.update_layout(showlegend=True, legend=dict(orientation="v", x=1.0, y=0.5, font=dict(size=10)),
                      annotations=[dict(text=f"{cur['input_t']:,.0f} t", showarrow=False, font=dict(size=16, color="#F2FBF6", family="Space Grotesk"))])
    return fig

def fig_capacity(cap):
    colors = [EMERALD if u < 80 else (AMBER if u < 100 else ROSE) for u in cap["util"]]
    fig = go.Figure(go.Bar(x=cap["util"], y=cap["label"], orientation="h", marker_color=colors,
                           texttemplate="%{x:.0f}%", textposition="outside",
                           textfont=dict(color="#CFE6DB"),
                           customdata=np.stack([cap["used"], cap["cap"]], axis=1),
                           hovertemplate="%{y}: %{customdata[0]:,.0f} / %{customdata[1]:,.0f} t<extra></extra>"))
    base_layout(fig, 240)
    fig.add_vline(x=100, line_dash="dash", line_color=ROSE, annotation_text="capacity", annotation_font_color=ROSE)
    fig.update_layout(xaxis=dict(range=[0, max(112, float(cap["util"].max()) + 12)], title="Utilization %"), showlegend=False)
    return fig

def fig_sankey(usable, plan):
    facs = [f for f in FAC_NAME if f in set(usable["facility"])]
    streams = [s for s in STREAM_ORDER if s in set(usable["stream"])]
    paths = [p for p in PATH_ORDER if plan.kpis["path_t"].get(p, 0) > 1e-6 or p == "landfill"]
    labels = [FAC_NAME[f] for f in facs] + [STREAMS[s]["name"] for s in streams] + [P[p]["label"] for p in paths]
    colors = [FAC_COLORS[i % len(FAC_COLORS)] for i in range(len(facs))] + [STREAMS[s]["color"] for s in streams] + [P[p]["color"] for p in paths]
    idx = {l: i for i, l in enumerate(labels)}
    src, dst, val, col = [], [], [], []
    g = usable.groupby(["facility", "stream"])["tonnes"].sum()
    for (f, s), t in g.items():
        if t > 1e-6 and f in facs and s in streams:
            src.append(idx[FAC_NAME[f]]); dst.append(idx[STREAMS[s]["name"]]); val.append(float(t)); col.append(rgba(STREAMS[s]["color"], 0.26))
    extra_land = plan.alloc.groupby("stream")["residue_t"].sum().to_dict()
    for r in plan.alloc.itertuples(index=False):
        t = float(r.tonnes)
        if t > 1e-6 and r.stream in streams and r.pathway in paths:
            src.append(idx[STREAMS[r.stream]["name"]]); dst.append(idx[P[r.pathway]["label"]]); val.append(t); col.append(rgba(STREAMS[r.stream]["color"], 0.38))
    for s, t in extra_land.items():
        if t > 1e-6 and s in streams:
            src.append(idx[STREAMS[s]["name"]]); dst.append(idx[P["landfill"]["label"]]); val.append(float(t)); col.append(rgba(ROSE, 0.30))
    fig = go.Figure(go.Sankey(arrangement="snap",
                              node=dict(label=labels, color=colors, pad=16, thickness=15,
                                        line=dict(color="rgba(12,23,20,0.6)", width=1),
                                        hovertemplate="%{label}: %{value:,.0f} t<extra></extra>"),
                              link=dict(source=src, target=dst, value=val, color=col,
                                        hovertemplate="%{source.label} → %{target.label}<br>%{value:,.0f} t<extra></extra>")))
    base_layout(fig, 470)
    fig.update_layout(font=dict(size=11.5, color="#E6F2EC", family="Inter"))
    return fig

def fig_waterfall(plan):
    a = plan.alloc
    fees, haul, res, rev = a["fee_eur"].sum(), a["transport_eur"].sum(), a["residue_eur"].sum(), a["revenue_eur"].sum()
    fig = go.Figure(go.Waterfall(
        x=["Processing fees", "Haulage", "Residue disposal", "Material revenue", "Net cost"],
        y=[fees, haul, res, -rev, 0], measure=["relative", "relative", "relative", "relative", "total"],
        text=[fmt_eur(fees), fmt_eur(haul), fmt_eur(res), fmt_eur(-rev), fmt_eur(fees + haul + res - rev)],
        textposition="outside", textfont=dict(color="#CFE6DB", size=11),
        increasing=dict(marker=dict(color=ROSE)), decreasing=dict(marker=dict(color=EMERALD)),
        totals=dict(marker=dict(color=CYAN)), connector=dict(line=dict(color="rgba(148,190,175,.35)", width=1))))
    base_layout(fig, 300)
    fig.update_layout(yaxis_title="€ / month", showlegend=False)
    return fig

# ----------------------------------------------------------------------------
# Samples, reports, self-tests
# ----------------------------------------------------------------------------
def sample_valid_csv():
    rows = [
        {"record_id": "", "date": "2026-11-14", "facility": "F1", "stream_code": "PET", "description": "clear PET bottles, baled", "tonnes": "42.5", "contamination_pct": "7", "source": "weighbridge"},
        {"record_id": "", "date": "2026-11-14", "facility": "F3", "stream_code": "OCC", "description": "baled corrugated cardboard", "tonnes": "96.0", "contamination_pct": "6", "source": "weighbridge"},
        {"record_id": "", "date": "2026-11-15", "facility": "F2", "stream_code": "ORG", "description": "source-separated food scraps", "tonnes": "118.2", "contamination_pct": "9", "source": "route sheets"},
        {"record_id": "", "date": "2026-11-15", "facility": "F5", "stream_code": "GLS", "description": "mixed container glass", "tonnes": "31.4", "contamination_pct": "5", "source": "weighbridge"},
        {"record_id": "", "date": "2026-11-16", "facility": "F6", "stream_code": "ALU", "description": "baled UBC aluminium cans", "tonnes": "9.8", "contamination_pct": "4", "source": "baler log"},
        {"record_id": "", "date": "2026-11-16", "facility": "F4", "stream_code": "TEX", "description": "sorted post-consumer textiles", "tonnes": "12.6", "contamination_pct": "11", "source": "charity partner"},
        {"record_id": "", "date": "2026-11-17", "facility": "F1", "stream_code": "WEE", "description": "small WEEE, depolluted", "tonnes": "6.3", "contamination_pct": "3", "source": "civic site"},
        {"record_id": "", "date": "2026-11-17", "facility": "F3", "stream_code": "RES", "description": "black-bag residual waste", "tonnes": "154.0", "contamination_pct": "18", "source": "weighbridge"},
    ]
    return pd.DataFrame(rows).to_csv(index=False).encode()

def sample_malformed_df():
    return pd.DataFrame([
        {"record_id": "WS-2026-9001", "date": "2026-11-18", "facility": "F1", "stream_code": "PET", "description": "ok row", "tonnes": "20", "contamination_pct": "8", "source": "demo"},
        {"record_id": "WS-2026-9002", "date": "2026-13-40", "facility": "F1", "stream_code": "PET", "description": "bad date", "tonnes": "20", "contamination_pct": "8", "source": "demo"},
        {"record_id": "WS-2026-9003", "date": "2026-11-18", "facility": "F99", "stream_code": "OCC", "description": "unknown facility", "tonnes": "30", "contamination_pct": "7", "source": "demo"},
        {"record_id": "WS-2026-9004", "date": "2026-11-18", "facility": "F2", "stream_code": "PLASTICX", "description": "unknown stream", "tonnes": "15", "contamination_pct": "10", "source": "demo"},
        {"record_id": "WS-2026-9005", "date": "2026-11-18", "facility": "F2", "stream_code": "GLS", "description": "negative tonnes", "tonnes": "-5", "contamination_pct": "6", "source": "demo"},
        {"record_id": "WS-2026-9006", "date": "2026-11-18", "facility": "F3", "stream_code": "ORG", "description": "contamination out of range", "tonnes": "40", "contamination_pct": "85", "source": "demo"},
        {"record_id": "WS-2026-9001", "date": "2026-11-18", "facility": "F3", "stream_code": "MIX", "description": "duplicate id in file", "tonnes": "25", "contamination_pct": "12", "source": "demo"},
        {"record_id": "WS-2026-9008", "date": "2026-11-18", "facility": "F4", "stream_code": "RES", "description": "missing tonnes", "tonnes": "", "contamination_pct": "18", "source": "demo"},
    ])

def md_table(df):
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        vals = []
        for v in r:
            if isinstance(v, float):
                vals.append(f"{v:,.1f}")
            else:
                vals.append(str(v).replace("|", "/"))
        out.append("| " + " | ".join(vals) + " |")
    return "\n".join(out)

def build_report_md(meta, params, result, alloc, overrides):
    k = result["kpis"]
    cap = pd.DataFrame(result["capacity"])
    piv = alloc.pivot_table(index="stream", columns="pathway", values="tonnes", aggfunc="sum", fill_value=0)
    piv = piv.reindex(columns=[p for p in PATH_ORDER if p in piv.columns]).fillna(0).round(1)
    piv["Supply (t)"] = piv.sum(axis=1).round(1)
    piv = piv.reset_index()
    scen = pd.DataFrame([{"Parameter": key, "Value": str(val)} for key, val in params.items()])
    lines = [
        f"# ♻️ ReFlow — Circularity Plan Report ({meta['id']})",
        f"**Status:** {meta['status']}  |  **Period:** {meta['period']}  |  **Generated:** {now_iso()}",
        "",
        "> DECISION SUPPORT — synthetic demo data & illustrative factors. Not a compliance document. "
        "Authorized human approval is required before any operational action.",
        "",
        "## 1. Executive summary",
        f"- Input treated: **{k['input_t']:,.0f} t** across the regional facility network.",
        f"- Diversion rate: **{k['diversion']:.1f}%** (target {params.get('target', '—')}%"
        + (", energy recovery counted" if params.get("energy_counts") else ", excluding energy recovery") + ").",
        f"- Net financial cost: **{fmt_eur(k['cost'])} / month** (negative = net revenue).",
        f"- Net climate impact: **{k['co2']:,.0f} tCO₂e** (negative = avoided emissions).",
        f"- Materials recovered: **{k['recovered_t']:,.0f} t**; landfilled incl. residues: **{k['landfill_t']:,.0f} t**.",
        "",
        "## 2. Method & provenance",
        f"Rules `{meta['rules_version']}`, factors `{meta['factors_version']}`, solver `{result.get('solver', 'HiGHS LP')}`. "
        "Deterministic linear program: min Σ flow × (gate fee + haulage − material revenue + residue disposal + carbon price × net CO₂e), "
        "subject to per-stream mass balance (clean/contaminated split), pathway capacities (incl. process residues), "
        "a diversion floor and an optional budget ceiling. Contaminated fractions are restricted to energy recovery / landfill. "
        "Diversion is input-mass based; process residues are reported separately.",
        "",
        "## 3. Scenario parameters", md_table(scen), "",
        "## 4. Allocation plan (tonnes)", md_table(piv), "",
        "## 5. Capacity utilization", md_table(cap.round(1)), "",
        "## 6. Binding constraints & flags",
    ]
    sh = result.get("shadows", {})
    if sh:
        for key, v in sh.items():
            lbl = P[key]["label"] if key in P else "Diversion target"
            lines.append(f"- **{lbl}** binding — shadow price ≈ €{v:,.0f} per additional tonne.")
    else:
        lines.append("- No binding constraints reported (or relaxed solve).")
    for f in result.get("flags", []):
        lines.append(f"- ⚠️ {f}")
    if overrides:
        lines.append("")
        lines.append("## 7. Manual overrides (audited)")
        for ov in overrides:
            lines.append(f"- {ov['stream']} → {P[ov['pathway']]['label']} — reason: {ov['reason']} (by {ov['actor']}, {ov['ts']})")
    lines += ["", "## 8. Limitations",
              "All tonnages, prices and emission factors are synthetic and illustrative (dated 2026-11). "
              "Replace catalogs with jurisdiction-approved, dated references and qualified practitioner review before production use. "
              "Sensitivity is deterministic (±20% contamination), not a statistical confidence interval.",
              "", "## 9. Sign-off",
              f"Submitted by: {meta.get('submitted_by', '—')}  |  Reviewed by: {meta.get('reviewed_by') or '________________'}",
              f"Review note: {meta.get('review_note') or '________________'}",
              "Approval signature / date: ________________"]
    return "\n".join(lines)

def run_self_tests():
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append({"Test": name, "Result": "✅ PASS", "Detail": detail})
        except Exception as e:
            results.append({"Test": name, "Result": "❌ FAIL", "Detail": str(e)[:220]})

    full = aggregate_supply(sim_supply(0, SEED), SC_DEFAULTS)
    base = solve_plan(full, SC_DEFAULTS, [])

    def t_mass():
        got = base.alloc.groupby("stream")["tonnes"].sum()
        for s in full.index:
            assert abs(got.get(s, 0) - full.loc[s, "supply"]) < 1e-4, f"imbalance at {s}"
        return f"Σ allocations = supply for all {len(full)} streams (tol 1e-4 t)."
    check("Mass balance conserved", t_mass)

    def t_cap():
        assert base.status == "Optimal", base.status
        for r in base.capacity.itertuples():
            assert r.used <= r.cap + 1e-4, f"{r.pathway} over capacity"
        return "All 5 pathway capacities respected incl. process residues."
    check("Hard capacity constraints respected", t_cap)

    def t_hand():
        agg = pd.DataFrame([{"stream": "PET", "supply": 100.0, "clean": 90.0, "dirty": 10.0, "dist_factor": 1.0}]).set_index("stream")
        sc = dict(SC_DEFAULTS); sc.update(carbon=0, target=0, budget_on=False)
        pl = solve_plan(agg, sc, [])
        exp = 90 * (62 + 2.64 - 279 + 12.8) + 10 * (92 + 2.64 + 32)  # = −16,874.0
        assert abs(pl.kpis["cost"] - exp) < 1.0, f"{pl.kpis['cost']} vs {exp}"
        rec = pl.alloc[(pl.alloc.stream == "PET") & (pl.alloc.pathway == "recycle")]["tonnes"].sum()
        assert abs(rec - 90) < 1e-4
        return f"Hand-computed cost −€16,874 matched ({fmt_eur(pl.kpis['cost'])}); 90 t clean → recycling."
    check("Hand-checkable cost example (PET 100 t)", t_hand)

    def t_div():
        assert base.status == "Optimal"
        assert base.kpis["diversion"] >= SC_DEFAULTS["target"] - 0.2
        return f"Diversion {base.kpis['diversion']:.1f}% ≥ target {SC_DEFAULTS['target']}%."
    check("Diversion target honored when feasible", t_div)

    def t_budget():
        sc = dict(SC_DEFAULTS); sc.update(budget_on=True, budget=500000)
        pl = solve_plan(full, sc, [])
        assert pl.status == "Optimal" and pl.kpis["cost"] <= 500000 + 1e-4
        return f"Cost {fmt_eur(pl.kpis['cost'])} ≤ €500,000 ceiling."
    check("Budget constraint honored", t_budget)

    def t_infeas():
        sc = dict(SC_DEFAULTS); sc.update(target=99, energy_counts=False, capacity=60)
        pl = solve_plan(full, sc, [])
        assert pl.status in ("Relaxed", "Greedy fallback") and pl.flags
        got = pl.alloc.groupby("stream")["tonnes"].sum()
        for s in full.index:
            assert abs(got.get(s, 0) - full.loc[s, "supply"]) < 1e-3
        return f"Degrades safely → {pl.status}; {len(pl.flags)} flag(s); mass balance intact."
    check("Infeasible scenario degrades safely", t_infeas)

    def t_clf():
        assert classify_text("clear PET water bottles")["code"] == "PET"
        assert classify_text("food scraps from kitchen")["code"] == "ORG"
        r = classify_text("xyzzy unknown material")
        assert r["code"] == "RES" and r["confidence"] < 0.5
        return "PET / ORG rules hit; unknown → RES @ 30% confidence."
    check("Classifier rules deterministic", t_clf)

    def t_import():
        _, rep, err = validate_import(sample_malformed_df(), set())
        assert err is None
        joined = " ".join(rep["_errors"]).lower()
        for word in ("date", "facility", "stream_code", "tonnes", "contamination_pct", "duplicate", "missing"):
            assert word in joined, word
        n_err = int((rep["_status"] == "⛔ ERROR").sum())
        assert n_err >= 7
        return f"{n_err}/8 malformed rows rejected with actionable, field-specific errors."
    check("Import rejects malformed rows", t_import)

    def t_repro():
        a, b = sim_supply(5, SEED), sim_supply(5, SEED)
        assert float(a.tonnes.sum()) == float(b.tonnes.sum())
        return f"Seed 42, month 6 → identical arrivals ({a.tonnes.sum():,.2f} t)."
    check("Simulation reproducible (seeded)", t_repro)

    def t_co2():
        assert STREAMS["ALU"]["co2"]["recycle"] < -5
        assert STREAMS["ORG"]["co2"]["landfill"] > 0.4
        return "Aluminium recycling credit −9.1; organics landfill +0.62 tCO₂e/t (signs sane)."
    check("CO₂ factors sign sanity", t_co2)

    def t_db():
        c = sqlite3.connect(":memory:")
        c.executescript(SCHEMA)
        c.execute("INSERT INTO records(id,date,facility,stream_code,tonnes,contamination,status) VALUES('T-1','2026-11-01','F1','PET',10,0.08,'Draft')")
        assert c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        c.close()
        return "Schema creates; insert/read round-trip OK (in-memory)."
    check("SQLite round-trip & schema", t_db)

    def t_override():
        pl = solve_plan(full, SC_DEFAULTS, [{"stream": "PET", "pathway": "landfill", "reason": "test", "ts": now_iso(), "actor": "test"}])
        rec = pl.alloc[(pl.alloc.stream == "PET") & (pl.alloc.pathway == "recycle")]["tonnes"].sum()
        assert rec < 1e-6
        return "Override PET→landfill removes recycling allocation; balance preserved."
    check("Overrides restrict pathway", t_override)
    return results

# ----------------------------------------------------------------------------
# Page: Live Overview
# ----------------------------------------------------------------------------
def page_overview():
    sim = SS["sim"]
    sec("📡", "Live circularity overview", f"Regional demo · {PERIOD_LABEL} → Oct 2027 · seed {sim['seed']} · SYNTHETIC")
    held = sim["running"] and SS["nav"] not in LIVE_PAGES
    if sim["done"]:
        cls, txt = "live paused", "✔ PROJECTION COMPLETE"
    elif sim["running"] and not held:
        cls, txt = "live", f"LIVE · {MONTHS[min(sim['month'], 11)].strftime('%b %Y')}"
    elif held:
        cls, txt = "live held", "⏸ HELD"
    else:
        cls, txt = "live paused", "⏸ PAUSED"
    c1, c2 = st.columns([1, 2.2])
    c1.markdown(f'<span class="{cls}"><span class="dot"></span>{txt}</span>', unsafe_allow_html=True)
    c2.progress(sim["month"] / 12, text=f"{sim['month']}/12 months simulated")
    if sim["done"]:
        b1, b2 = st.columns([3, 1])
        b1.success("12-month projection complete. Replay to re-run with a different scenario, or approve a plan in Review.")
        with b2:
            if btn("⟲ Replay", key="ov_replay", type="primary"):
                sim_replay(); st.rerun()

    hist = pd.DataFrame(sim["history"])
    cur = hist.iloc[-1]
    prev = hist.iloc[-2] if len(hist) > 1 else None
    pv = (lambda k: prev[k]) if prev is not None else (lambda k: None)
    kpi_row([
        kpi_card("Diversion rate", f"{cur['diversion']:.1f}%", EMERALD, f"target {cur['target']:.0f}%",
                 delta_html(cur["diversion"], pv("diversion"), True, lambda v: f"{v:.1f} pp"), hist["diversion"].tolist()),
        kpi_card("Net cost / month", fmt_eur(cur["cost"]), CYAN, "negative = net revenue",
                 delta_html(cur["cost"], pv("cost"), False, lambda v: fmt_eur(v).lstrip("−")), hist["cost"].tolist()),
        kpi_card("Net CO₂e", fmt_co2(cur["co2"]), LIME, "negative = avoided",
                 delta_html(cur["co2"], pv("co2"), False, lambda v: f"{v:,.0f} t"), hist["co2"].tolist()),
        kpi_card("Materials recovered", fmt_t(cur["recovered_t"]), TEAL, "reuse + recyclate + compost",
                 delta_html(cur["recovered_t"], pv("recovered_t"), True, lambda v: f"{v:,.0f} t"), hist["recovered_t"].tolist()),
        kpi_card("Landfilled", fmt_t(cur["landfill_t"]), ROSE, "incl. process residues",
                 delta_html(cur["landfill_t"], pv("landfill_t"), False, lambda v: f"{v:,.0f} t"), hist["landfill_t"].tolist()),
    ])
    left, right = st.columns([2.1, 1], gap="large")
    with left:
        st.markdown("##### 📈 Diversion vs target · net cost")
        show_fig(fig_live(hist), key="ov_live")
        st.markdown("##### 🧱 Where the tonnes go, month by month")
        show_fig(fig_stack(hist), key="ov_stack")
    with right:
        st.markdown(f"##### 🍩 Current routing — {cur['label']}")
        show_fig(fig_donut(cur), key="ov_donut")
        st.markdown("##### 🏗️ Capacity utilization")
        cap = pd.DataFrame([{"pathway": p, "label": P[p]["label"], "used": cur["cap_util"][p] / 100 * P[p]["cap"] * SS["scenario"]["capacity"] / 100,
                             "cap": P[p]["cap"] * SS["scenario"]["capacity"] / 100, "util": cur["cap_util"][p]} for p in PATH_ORDER])
        show_fig(fig_capacity(cap), key="ov_cap")
        st.markdown("##### 📟 Event feed")
        lines = []
        for r in hist.tail(4).iloc[::-1].itertuples(index=False):
            lines.append(f'<span class="t">●</span> {r.label} — {r.input_t:,.0f} t in · {r.diversion:.1f}% diverted · {fmt_eur(r.cost)}')
        for a in load_audit(3).itertuples(index=False):
            lines.append(f'<span class="a">✎</span> {esc(a.ts[5:16])} {esc(a.actor)} · {esc(a.action)} · {esc(a.entity)}')
        st.markdown('<div class="feed">' + "<br>".join(lines) + "</div>", unsafe_allow_html=True)

    binds = hist[hist["shadow_recycle"] > 0]
    relaxed = int((hist["status"] == "Relaxed").sum())
    if len(binds):
        avg_sh = float(binds["shadow_recycle"].mean())
        msg = (f"💡 <b>Actionable insight:</b> recycling capacity binds in <b>{len(binds)}/{len(hist)}</b> simulated months "
               f"(avg shadow price ≈ <b>€{avg_sh:,.0f}/t</b>). Adding 100 t/month of recycling capacity could avoid roughly "
               f"<b>{fmt_eur(100 * avg_sh)}</b> per binding month — indicative, verify in the Optimizer.")
    else:
        msg = (f"💡 <b>Actionable insight:</b> no capacity constraint binds yet — average intake {hist['input_t'].mean():,.0f} t/month. "
               f"Each +5 pp of diversion target shifts ≈ {0.05 * hist['input_t'].mean():,.0f} t away from landfill. Try it live in the Optimizer.")
    if relaxed:
        msg += f" ⚠️ {relaxed} month(s) required penalty relaxation — review flags before approving."
    st.markdown(f'<div class="insight">{msg}</div>', unsafe_allow_html=True)

    recs = load_records()
    flagged = int((recs.status == "Flagged").sum())
    drafts = int((recs.status == "Draft").sum())
    approved_plans = int((load_plans().status == "Approved").sum()) if len(load_plans()) else 0
    st.markdown(chip(f"🗂 {len(recs)} records") + chip(f"🚩 {flagged} flagged") + chip(f"📝 {drafts} drafts excluded from planning")
                + chip(f"✅ {approved_plans} approved plans") + badge("SYNTHETIC DEMO", "cy"), unsafe_allow_html=True)
    b1, b2, _ = st.columns([1, 1, 2.4])
    with b1:
        if btn("🛡️ Open review queue", key="ov_review"):
            SS["nav"] = NAV[3]; st.rerun()
    with b2:
        if btn("🗂️ Browse records", key="ov_records"):
            SS["nav"] = NAV[1]; st.rerun()

# ----------------------------------------------------------------------------
# Page: Streams & Records
# ----------------------------------------------------------------------------
def render_detail(row):
    S = STREAMS.get(row.stream_code, STREAMS["RES"])
    c1, c2 = st.columns([1.25, 1], gap="large")
    with c1:
        st.markdown(badge(row.status, STATUS_KIND.get(row.status, "sl"))
                    + f'<span class="badge" style="color:{S["color"]};border-color:{rgba(S["color"],0.5)};background:{rgba(S["color"],0.12)}">{esc(S["name"])}</span>'
                    + chip(f"EWC {S['ewc']}"), unsafe_allow_html=True)
        st.markdown(f"**{row.id}** · {FAC_NAME.get(row.facility, row.facility)} · {row.date} · "
                    f"**{row.tonnes:,.1f} t** · contamination **{row.contamination * 100:.1f}%**")
        st.caption(f"Description: {row.description or '—'}")
        if row.notes:
            st.warning(f"🚩 {row.notes}")
        if row.description:
            clf = classify_text(row.description)
            if clf["code"] != row.stream_code and clf["confidence"] >= 0.6:
                st.warning(f"⚠️ Description suggests **{STREAMS[clf['code']]['name']}** ({clf['confidence']:.0%} confidence) but code is {row.stream_code}.")
                if btn("🔁 Apply suggested code", key=f"fix_{row.id}", disabled=not can("edit"),
                       help="Requires Analyst or Administrator"):
                    if guard("edit"):
                        update_record(row.id, {"stream_code": clf["code"], "notes": ""}, "record.reclassify")
                        st.toast(f"{row.id} reclassified to {clf['code']}", icon="🔁"); st.rerun()
        with st.expander("🧬 Lineage & provenance"):
            st.markdown(f"- Source: `{row.source}`\n- Source file: `{row.source_file or '—'}` · row {row.source_row}\n"
                        f"- Schema version: {row.schema_version}\n- Created: {row.created_at} · Updated: {row.updated_at}")
    with c2:
        st.markdown("**Lifecycle actions**")
        a1, a2 = st.columns(2)
        with a1:
            if row.status in ("Draft", "Flagged", "Rejected"):
                if btn("🧪 Validate", key=f"val_{row.id}", disabled=not can("edit"), help="Runs range/catalog checks"):
                    if guard("edit"):
                        errs, warns, _ = row_checks(row.date, row.facility, row.stream_code, row.tonnes, row.contamination * 100)
                        if errs:
                            update_record(row.id, {"status": "Flagged", "notes": "; ".join(errs)}, "record.validate.fail")
                            st.toast("Validation failed — see notes.", icon="⛔")
                        else:
                            update_record(row.id, {"status": "Validated", "notes": "; ".join(warns)}, "record.validate")
                            st.toast(f"{row.id} validated", icon="✅")
                        st.rerun()
            if row.status == "Validated":
                if btn("✅ Approve", key=f"app_{row.id}", type="primary", disabled=not can("approve"), help="Requires Reviewer"):
                    if guard("approve"):
                        update_record(row.id, {"status": "Approved"}, "record.approve")
                        st.toast(f"{row.id} approved", icon="✅"); st.rerun()
        with a2:
            if row.status == "Validated":
                reason = st.text_input("Reject reason", key=f"rej_{row.id}", placeholder="required for rejection")
                if btn("⛔ Reject", key=f"rc_{row.id}", disabled=not can("approve") or not reason):
                    if guard("approve"):
                        update_record(row.id, {"status": "Rejected", "notes": reason}, "record.reject")
                        st.toast(f"{row.id} rejected", icon="⛔"); st.rerun()
            if row.status != "Archived":
                if btn("📦 Archive", key=f"arc_{row.id}", disabled=not can("edit"), help="Soft delete — undo available"):
                    if guard("edit"):
                        SS["trash"] = [{"id": row.id, "prev": row.status}]
                        update_record(row.id, {"status": "Archived"}, "record.archive")
                        st.toast(f"{row.id} archived — undo available above.", icon="📦"); st.rerun()
        with st.expander("🕐 Entity audit trail"):
            ea = load_audit(20, row.id)
            if len(ea):
                show_df(ea[["ts", "actor", "action", "details"]], height=170)
            else:
                st.caption("No events yet.")
    if can("edit"):
        with st.expander("✏️ Edit record (approved records revert to Draft as a correction)"):
            with st.form(key=f"edit_{row.id}"):
                e1, e2, e3 = st.columns(3)
                fac = e1.selectbox("Facility", list(FAC_NAME), index=list(FAC_NAME).index(row.facility), format_func=lambda f: FAC_NAME[f])
                strm = e2.selectbox("Stream", STREAM_ORDER, index=STREAM_ORDER.index(row.stream_code), format_func=lambda s: STREAMS[s]["name"])
                date = e3.text_input("Date (YYYY-MM-DD)", value=row.date)
                e4, e5 = st.columns(2)
                tonnes = e4.number_input("Tonnes", 0.0, 2000.0, float(row.tonnes), 0.5)
                cont = e5.number_input("Contamination %", 0.0, 60.0, float(row.contamination * 100), 0.5)
                desc = st.text_input("Description", value=row.description)
                ok = stretch(st.form_submit_button, "💾 Save changes", type="primary")
            if ok:
                errs, warns, norm = row_checks(date, fac, strm, tonnes, cont)
                if errs:
                    st.error(" · ".join(errs))
                else:
                    new_status = "Draft" if row.status == "Approved" else ("Flagged" if warns else row.status)
                    update_record(row.id, {**norm, "description": desc, "status": new_status,
                                           "notes": "; ".join(warns)}, "record.correct" if row.status == "Approved" else "record.update")
                    st.toast("Record updated (audited).", icon="💾"); st.rerun()

def render_import(existing_ids):
    st.caption("CSV or XLSX · required columns: date, facility, stream_code, tonnes, contamination_pct · optional: record_id, description, source. "
               "Originals are archived with a SHA-256 checksum; validated data is never overwritten; identical files cannot be re-imported.")
    s1, s2 = st.columns(2)
    with s1:
        dl("⬇️ Sample valid CSV", sample_valid_csv(), "reflow_sample_valid.csv", "text/csv", key="dl_valid")
    with s2:
        dl("⬇️ Sample malformed CSV (demo rejection)", sample_malformed_df().to_csv(index=False).encode(),
           "reflow_sample_malformed.csv", "text/csv", key="dl_bad")
    up = st.file_uploader("Upload intake file", type=["csv", "xlsx", "xls"], key="imp_file")
    if not up:
        return
    data = up.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    try:
        if up.name.lower().endswith(".csv"):
            raw = pd.read_csv(io.BytesIO(data), dtype=str).fillna("")
        else:
            raw = pd.read_excel(io.BytesIO(data), dtype=str).fillna("")
    except Exception as e:
        st.error(f"Could not parse file: {e}")
        return
    ok_rows, report, global_err = validate_import(raw, existing_ids)
    if global_err:
        st.error(global_err)
        return
    n_err = int((report["_status"] == "⛔ ERROR").sum())
    n_warn = int((report["_status"] == "⚠️ WARN").sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Rows", len(report)); m2.metric("Importable", len(ok_rows)); m3.metric("Warnings", n_warn); m4.metric("Errors", n_err)
    show_df(report, height=250)
    if n_err:
        dl("⬇️ Download error report (CSV)", report[report["_errors"] != ""].to_csv(index=False).encode(),
           "reflow_import_errors.csv", "text/csv", key="dl_err")
    with closing(db()) as c:
        already = c.execute("SELECT COUNT(*) FROM imports WHERE sha256=?", (sha,)).fetchone()[0] > 0
    skip = st.checkbox("Skip invalid rows and import the rest", value=True, key="imp_skip")
    disabled = (not can("import")) or len(ok_rows) == 0 or (n_err > 0 and not skip) or already
    if already:
        st.info("🛡️ Safe re-import protection: this exact file (checksum match) was already imported.")
    if btn(f"📥 Import {len(ok_rows)} rows", key="imp_go", type="primary", disabled=disabled,
           help="Requires Analyst or Administrator"):
        if guard("import"):
            IMPORT_DIR.mkdir(exist_ok=True)
            archived = f"{dt.datetime.now().strftime('%Y%m%d%H%M%S')}_{sha[:8]}_{up.name}"
            (IMPORT_DIR / archived).write_bytes(data)
            with closing(db()) as c:
                for r in ok_rows:
                    rid = r["record_id"] or next_record_id(c)
                    ts = now_iso()
                    c.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (rid, r["date"], r["facility"], r["stream_code"], r["description"], float(r["tonnes"]),
                               float(r["contamination"]), r["status"], r["source"], archived, r["source_row"],
                               SCHEMA_VERSION, ts, ts, r["notes"]))
                c.execute("INSERT INTO imports(ts,filename,sha256,rows_total,rows_ok,rows_err,actor) VALUES(?,?,?,?,?,?,?)",
                          (now_iso(), archived, sha, len(report), len(ok_rows), n_err, SS["role"]))
                c.commit()
            audit("data.import", archived, f"{len(ok_rows)}/{len(report)} rows imported · sha256 {sha[:16]}… · {n_err} rejected")
            st.toast(f"Imported {len(ok_rows)} rows — original archived & audited.", icon="📥")
            st.rerun()

def page_records():
    sec("🗂️", "Streams & records", f"Working period {PERIOD_LABEL} · lifecycle: Draft → Validated → Approved · corrections audited")
    recs = load_records()
    if SS["trash"]:
        u1, u2 = st.columns([3.4, 1])
        u1.info(f"📦 {len(SS['trash'])} record(s) archived. Undo restores previous status.")
        with u2:
            if btn("↩ Undo archive", key="undo_arch", type="primary"):
                for t in SS["trash"]:
                    update_record(t["id"], {"status": t["prev"]}, "record.restore")
                SS["trash"] = None
                SS.pop("rec_editor", None)
                st.toast("Archive undone.", icon="↩"); st.rerun()

    c1, c2 = st.columns([1.3, 1], gap="large")
    with c1:
        with st.container(border=True):
            st.markdown("##### 🔎 Smart stream classifier")
            txt = st.text_input("Describe the waste item", value="rinsed glass jars and bottles", key="clf_text")
            clf = classify_text(txt)
            S = STREAMS[clf["code"]]
            st.markdown(f'<span class="badge" style="color:{S["color"]};border-color:{rgba(S["color"],0.55)};background:{rgba(S["color"],0.14)};font-size:.85rem">{esc(S["name"])}</span>'
                        + chip(f"EWC {S['ewc']}") + chip(f"confidence {clf['confidence']:.0%}"), unsafe_allow_html=True)
            st.progress(clf["confidence"])
            st.caption(clf["reason"] + " · Alternatives: " + ", ".join(f"{c} ({s:.0f})" for c, s in clf["alts"]))
            if btn("➕ Prefill new record from this", key="clf_prefill", disabled=not can("edit")):
                SS["nr_stream"] = clf["code"]; SS["nr_desc"] = txt; SS["nr_v"] += 1
                st.toast("New-record form prefilled below.", icon="🔎"); st.rerun()
    with c2:
        usable = recs[recs.status.isin(["Validated", "Approved"])]
        kpi_row([
            kpi_card("Records", f"{len(recs)}", CYAN, "all statuses"),
            kpi_card("Plannable supply", fmt_t(usable.tonnes.sum()), EMERALD, "Validated + Approved"),
            kpi_card("Exceptions", f"{int((recs.status == 'Flagged').sum())}", AMBER, "flagged for review"),
        ])
        st.caption("Only Validated + Approved rows feed the optimizer. Draft, Flagged, Rejected and Archived rows are excluded and reported in Data Quality.")

    with st.expander("➕ New intake record"):
        v = SS["nr_v"]
        with st.form(key=f"newrec_{v}"):
            n1, n2, n3 = st.columns(3)
            fac = n1.selectbox("Facility", list(FAC_NAME), format_func=lambda f: FAC_NAME[f], key=f"nr_fac_{v}")
            strm = n2.selectbox("Stream", STREAM_ORDER, index=STREAM_ORDER.index(SS.get("nr_stream", "MIX")),
                                format_func=lambda s: STREAMS[s]["name"], key=f"nr_strm_{v}")
            date = n3.date_input("Date", value=dt.date(2026, 11, 12), key=f"nr_date_{v}")
            n4, n5 = st.columns(2)
            tonnes = n4.number_input("Tonnes", 0.0, 2000.0, 25.0, 0.5, key=f"nr_t_{v}")
            cont = n5.number_input("Contamination %", 0.0, 60.0, 8.0, 0.5, key=f"nr_c_{v}")
            desc = st.text_input("Description", value=SS.get("nr_desc", ""), key=f"nr_d_{v}")
            as_draft = st.checkbox("Save as Draft (skip auto-validation)", key=f"nr_dr_{v}")
            ok = stretch(st.form_submit_button, "💾 Create record", type="primary", disabled=not can("edit"))
        if ok and guard("edit"):
            errs, warns, norm = row_checks(date.isoformat(), fac, strm, tonnes, cont)
            if errs:
                st.error(" · ".join(errs))
            else:
                status = "Draft" if as_draft else ("Flagged" if warns else "Validated")
                rid = insert_record({**norm, "description": desc, "status": status, "notes": "; ".join(warns), "source": "manual"})
                audit("record.create", rid, f"{norm['stream_code']} {norm['tonnes']:.1f} t @ {norm['facility']} → {status}")
                SS["nr_v"] += 1; SS.pop("nr_stream", None); SS.pop("nr_desc", None)
                st.toast(f"{rid} created ({status})", icon="🧾"); st.rerun()

    f1, f2, f3, f4, f5 = st.columns([1.4, 1.1, 1.1, 1, 1])
    q = f1.text_input("🔍 Search", key="rec_q", placeholder="id, description, facility…")
    facs = f2.multiselect("Facility", list(FAC_NAME), format_func=lambda f: FAC_NAME[f], key="rec_fac")
    strms = f3.multiselect("Stream", STREAM_ORDER, format_func=lambda s: STREAMS[s]["name"], key="rec_strm")
    stats = f4.multiselect("Status", ["Draft", "Validated", "Approved", "Flagged", "Rejected", "Archived"], key="rec_stat")
    sort = f5.selectbox("Sort", ["Newest first", "Tonnes ↓", "Facility A→Z", "Status"], key="rec_sort")
    view = recs.copy()
    if q:
        mask = view.apply(lambda r: q.lower() in f"{r.id} {r.description} {FAC_NAME.get(r.facility,'')} {STREAMS.get(r.stream_code,{}).get('name','')}".lower(), axis=1)
        view = view[mask]
    if facs: view = view[view.facility.isin(facs)]
    if strms: view = view[view.stream_code.isin(strms)]
    if stats: view = view[view.status.isin(stats)]
    sortmap = {"Newest first": ("date", False), "Tonnes ↓": ("tonnes", False), "Facility A→Z": ("facility", True), "Status": ("status", True)}
    col, asc = sortmap[sort]
    view = view.sort_values(col, ascending=asc)
    st.markdown(chip(f"showing {len(view)} of {len(recs)}") + chip(f"Σ {view.tonnes.sum():,.1f} t"), unsafe_allow_html=True)

    if len(view):
        disp = pd.DataFrame({
            "ID": view.id, "Date": view.date, "Facility": view.facility.map(FAC_NAME),
            "Stream": view.stream_code.map(lambda s: STREAMS[s]["name"]), "Code": view.stream_code,
            "Tonnes": view.tonnes.round(1), "Cont %": (view.contamination * 100).round(1),
            "Status": view.status, "Description": view.description})
        disp.insert(0, "Select", False)
        edited = stretch(st.data_editor, disp, key="rec_editor", hide_index=True, height=360,
                         disabled=[c for c in disp.columns if c != "Select"],
                         column_config={"Select": st.column_config.CheckboxColumn("Select"),
                                        "Tonnes": st.column_config.NumberColumn(format="%.1f"),
                                        "Cont %": st.column_config.ProgressColumn(min_value=0, max_value=60, format="%.1f%%")})
        sel_ids = edited.loc[edited["Select"].fillna(False).astype(bool), "ID"].tolist()
        b1, b2, b3, b4 = st.columns([1.1, 1.1, 1.1, 1.6])
        with b1:
            if btn(f"✅ Approve selected ({len(sel_ids)})", key="bulk_app", disabled=not can("approve") or not sel_ids, help="Reviewer only · Validated rows"):
                if guard("approve"):
                    n = 0
                    for rid in sel_ids:
                        if recs.set_index("id").loc[rid, "status"] == "Validated":
                            update_record(rid, {"status": "Approved"}, "record.approve.bulk"); n += 1
                    SS.pop("rec_editor", None)
                    st.toast(f"{n} approved · {len(sel_ids) - n} skipped (not Validated)", icon="✅"); st.rerun()
        with b2:
            if btn(f"📦 Archive selected ({len(sel_ids)})", key="bulk_arc", disabled=not can("edit") or not sel_ids):
                if guard("edit"):
                    SS["trash"] = [{"id": rid, "prev": recs.set_index("id").loc[rid, "status"]} for rid in sel_ids]
                    for rid in sel_ids:
                        update_record(rid, {"status": "Archived"}, "record.archive.bulk")
                    SS.pop("rec_editor", None)
                    st.toast(f"{len(sel_ids)} archived — undo available.", icon="📦"); st.rerun()
        with b3:
            dl("⬇️ Export view (CSV)", view.to_csv(index=False).encode(), "reflow_records_view.csv", "text/csv", key="dl_view")
        with b4:
            st.caption("Tick rows for bulk actions · edits, approvals and archives are written to the audit trail.")
    else:
        st.info("No records match the current filters.")

    st.markdown("#### 📄 Record detail & lineage")
    ids = view.id.tolist() if len(view) else recs.id.tolist()
    if ids:
        pick = SS.pop("detail_pick", None)
        default_id = pick if pick in ids else SS.get("detail_last", ids[0])
        idx = ids.index(default_id) if default_id in ids else 0
        sel = st.selectbox("Open record", ids, index=idx,
                           format_func=lambda i: f"{i} · {recs.set_index('id').loc[i,'stream_code']} · {recs.set_index('id').loc[i,'status']}")
        SS["detail_last"] = sel
        render_detail(recs[recs.id == sel].iloc[0])
    with st.expander("📥 Import CSV / XLSX — preview, row-level errors, archived originals"):
        render_import(set(recs.id))

# ----------------------------------------------------------------------------
# Page: Optimizer
# ----------------------------------------------------------------------------
def skey(name):
    return f"sc_{name}_{SS['scenario']['_v']}"

def scenario_controls():
    sc = SS["scenario"]
    st.markdown("##### 🎛 Scenario workbench")
    st.caption("Applied live — the running simulation picks these up on its next tick.")
    st.slider("🎯 Diversion target (%)", 30, 95, sc["target"], 1, key=skey("target"))
    st.slider("🌍 Carbon shadow price (€/tCO₂e)", 0, 200, sc["carbon"], 5, key=skey("carbon"))
    st.toggle("💰 Enforce monthly budget ceiling", sc["budget_on"], key=skey("budget_on"))
    st.slider("Budget ceiling (€/month)", 100000, 600000, sc["budget"], 10000, key=skey("budget"),
              disabled=not SS[skey("budget_on")], format="€%d")
    st.slider("🏗 Capacity scale (%)", 60, 160, sc["capacity"], 5, key=skey("capacity"))
    st.slider("🧾 Gate-fee multiplier (%)", 60, 160, sc["fees"], 5, key=skey("fees"))
    st.slider("📈 Recyclate price multiplier (%)", 50, 200, sc["prices"], 5, key=skey("prices"))
    st.slider("🚚 Base haul distance (km)", 5, 80, sc["distance"], 1, key=skey("distance"))
    st.slider("🧪 Contamination shock (%)", -40, 60, sc["shock"], 5, key=skey("shock"))
    st.toggle("⚡ Count energy recovery as diversion", sc["energy_counts"], key=skey("energy_counts"))
    for name in ("target", "carbon", "budget_on", "budget", "capacity", "fees", "prices", "distance", "shock", "energy_counts"):
        sc[name] = SS[skey(name)]
    r1, r2 = st.columns(2)
    with r1:
        if btn("⟲ Reset", key="sc_reset"):
            v = sc["_v"] + 1
            sc.update(SC_DEFAULTS); sc["_v"] = v
            if SS["overrides"]:
                audit("plan.override.clear", "scenario", "all overrides cleared by reset")
                SS["overrides"] = []
            st.rerun()
    with r2:
        if btn("🎲 Randomize", key="sc_rand"):
            v = sc["_v"] + 1
            sc.update(target=random.randint(45, 90), carbon=random.randrange(0, 155, 5),
                      capacity=random.randrange(70, 145, 5), fees=random.randrange(70, 155, 5),
                      prices=random.randrange(60, 185, 5), distance=random.randint(8, 60),
                      shock=random.randrange(-20, 45, 5), energy_counts=random.choice([True, False]),
                      budget_on=random.choice([True, False]), budget=random.randrange(250000, 500001, 10000))
            sc["_v"] = v
            st.rerun()
    with st.expander("📌 Manual pathway overrides (audited)"):
        for i, ov in enumerate(SS["overrides"]):
            o1, o2 = st.columns([3.2, 1])
            o1.markdown(f"**{ov['stream']} → {P[ov['pathway']]['label']}** — {esc(ov['reason'])}")
            with o2:
                if btn("✖ Remove", key=f"ov_rm_{i}", disabled=not can("override")):
                    if guard("override"):
                        audit("plan.override.remove", ov["stream"], f"{ov['pathway']} · {ov['reason']}")
                        SS["overrides"].pop(i); st.rerun()
        with st.form("ov_add"):
            o1, o2 = st.columns(2)
            s = o1.selectbox("Stream (clean fraction)", STREAM_ORDER, format_func=lambda x: STREAMS[x]["name"])
            p = o2.selectbox("Force pathway", allowed_paths(s, "clean", []), format_func=lambda x: P[x]["label"])
            reason = st.text_input("Reason (required, min 5 chars)", placeholder="e.g. contracted offtake until Q2")
            ok = stretch(st.form_submit_button, "📌 Pin pathway", type="primary", disabled=not can("override"),
                         help="Requires Reviewer or Administrator")
        if ok and guard("override"):
            if len(reason.strip()) < 5:
                st.error("A meaningful reason is required for the audit record.")
            else:
                SS["overrides"] = [o for o in SS["overrides"] if o["stream"] != s]
                SS["overrides"].append({"stream": s, "pathway": p, "reason": reason.strip(), "ts": now_iso(), "actor": SS["role"]})
                audit("plan.override.add", s, f"→ {p} · {reason.strip()}")
                st.toast(f"{s} pinned to {P[p]['label']}", icon="📌"); st.rerun()
        st.caption("Overrides constrain the clean fraction only — contaminated material still follows physics (energy/landfill).")

def page_optimizer():
    sc = SS["scenario"]
    sec("🧮", "Circularity optimizer", f"Deterministic LP · HiGHS · {RULES_VERSION} · {FACTORS_VERSION}")
    recs = load_records()
    usable = recs[recs.status.isin(["Validated", "Approved"])].rename(
        columns={"stream_code": "stream", "contamination": "cont"})[["facility", "stream", "tonnes", "cont"]]
    ctrl, res = st.columns([0.9, 2.2], gap="large")
    with ctrl:
        scenario_controls()
    agg = aggregate_supply(usable, sc)
    plan = solve_plan(agg, sc, SS["overrides"])
    with res:
        if plan.status == "Optimal":
            st.success(f"✅ **Optimal plan** — {plan.n_vars} decision variables · {plan.message}")
        elif plan.status == "Relaxed":
            st.warning("⚠️ **Relaxed solve** — " + plan.message + "\n\n" + "\n".join(f"- {f}" for f in plan.flags))
        elif plan.status == "No data":
            st.info(plan.message)
        else:
            st.info(f"ℹ️ **{plan.status}** — {plan.message}")
        if SS["overrides"]:
            st.markdown("".join(badge(f"📌 {o['stream']} → {P[o['pathway']]['label']}", "am") for o in SS["overrides"]), unsafe_allow_html=True)
        k = plan.kpis
        kpi_row([
            kpi_card("Net cost / month", fmt_eur(k["cost"]), CYAN, "financial, excl. carbon shadow"),
            kpi_card("Diversion", f"{k['diversion']:.1f}%", EMERALD, f"target {sc['target']}%"),
            kpi_card("Net CO₂e", fmt_co2(k["co2"]), LIME, f"priced @ €{sc['carbon']}/t in objective"),
            kpi_card("Materials recovered", fmt_t(k["recovered_t"]), TEAL, f"of {k['input_t']:,.0f} t input"),
        ])
        if plan.status != "No data":
            st.markdown("##### 🔀 Material flow — facilities → streams → pathways (incl. residues)")
            show_fig(fig_sankey(usable, plan), key="opt_sankey")
            t1, t2, t3, t4, t5 = st.tabs(["🧾 Allocation", "📊 Capacity & cost", "🔍 Explanation", "📈 Sensitivity", "⚠️ Data quality"])
            with t1:
                piv = plan.alloc.pivot_table(index="stream", columns="pathway", values="tonnes", aggfunc="sum", fill_value=0)
                piv = piv.reindex(columns=PATH_ORDER).fillna(0).round(1)
                piv.columns = [P[p]["label"] for p in piv.columns]
                piv["Supply (t)"] = piv.sum(axis=1).round(1)
                piv = piv.reset_index().rename(columns={"stream": "Stream"})
                piv["Stream"] = piv["Stream"].map(lambda s: STREAMS[s]["name"])
                show_df(piv, height=380)
                st.caption(f"Process residues to landfill: {k['residue_t']:,.0f} t · direct landfill: {k['path_t']['landfill']:,.0f} t · total landfilled: {k['landfill_t']:,.0f} t.")
            with t2:
                g1, g2 = st.columns(2)
                with g1:
                    st.markdown("**Capacity utilization**")
                    show_fig(fig_capacity(plan.capacity), key="opt_cap")
                with g2:
                    st.markdown("**Cost build-up (€/month)**")
                    show_fig(fig_waterfall(plan), key="opt_wf")
            with t3:
                st.markdown(chip(RULES_VERSION) + chip(FACTORS_VERSION) + chip(f"solver: {plan.solver}")
                            + chip(f"objective {fmt_eur(plan.objective)} (incl. carbon)"), unsafe_allow_html=True)
                st.markdown("**Binding constraints (shadow prices)**")
                if plan.shadows:
                    for key, v in plan.shadows.items():
                        lbl = P[key]["label"] + " capacity" if key in P else "Diversion target"
                        st.markdown(f"- **{lbl}** binds — ≈ **€{v:,.0f}/t** cost sensitivity to one more tonne.")
                else:
                    st.markdown("- No binding constraints — capacity headroom available.")
                st.markdown("**Top financial drivers**")
                drv = plan.alloc.groupby("pathway").agg(tonnes=("tonnes", "sum"), net_eur=("cost_eur", "sum"), co2_t=("co2_t", "sum")).reset_index()
                drv["pathway"] = drv["pathway"].map(lambda p: P[p]["label"])
                drv["net_eur"] = drv["net_eur"].round(0); drv["co2_t"] = drv["co2_t"].round(1); drv["tonnes"] = drv["tonnes"].round(1)
                drv = drv.reindex(drv["net_eur"].abs().sort_values(ascending=False).index)
                show_df(drv, height=210)
                if SS["overrides"]:
                    st.markdown("**Active overrides**")
                    for o in SS["overrides"]:
                        st.markdown(f"- {o['stream']} → {P[o['pathway']]['label']} · {esc(o['reason'])} · {o['actor']} @ {o['ts']}")
                with st.expander("🧮 Model formulation & factor provenance"):
                    st.code("min  Σ x[s,p] · (fee[p]·fees% + 0.12·dist·haul + residue[p]·landfill_fee\n"
                            "                 − revenue[s,p]·prices% + carbon€ · (CO2[s,p] + residue·CO2[s,landfill]))\n"
                            "s.t. Σ_p x[s,frac,p] = supply[s,frac]            (mass balance, clean/dirty split)\n"
                            "     Σ_s x[s,p] (+residues for landfill) ≤ cap[p]·capacity%\n"
                            "     Σ diverted·x ≥ target% · total input        (energy counted per toggle)\n"
                            "     Σ financial·x ≤ budget                      (if enabled)\n"
                            "     x ≥ 0 ; dirty fraction restricted to energy/landfill", language="text")
                    st.caption("Factors are synthetic, dated 2026-11 and versioned. Yields: recycling 90%, composting 85%, energy 75% (25% ash). "
                               "Replace with jurisdiction-approved references and practitioner review before production.")
                plans = load_plans()
                latest = plans.iloc[0] if len(plans) else None
                st.markdown("**Reviewer status:** " + (badge(latest.status, STATUS_KIND.get(latest.status, "sl")) + f" {latest.id} ({latest.created_at})" if latest is not None else badge("Not yet submitted", "sl")), unsafe_allow_html=True)
            with t4:
                st.caption("Deterministic ±20% contamination sensitivity — three re-solves, not a statistical confidence interval.")
                show_df(sensitivity_table(agg, sc, SS["overrides"]), height=160)
            with t5:
                excl = recs[~recs.status.isin(["Validated", "Approved"])]
                e1, e2 = st.columns(2)
                with e1:
                    st.markdown("**Excluded from planning**")
                    show_df(excl.groupby("status").agg(rows=("id", "count"), tonnes=("tonnes", "sum")).round(1).reset_index(), height=180)
                with e2:
                    st.markdown("**Flagged exceptions**")
                    for r in excl[excl.status == "Flagged"].itertuples(index=False):
                        st.markdown(f"- `{r.id}` — {esc(r.notes)}")
                st.caption("Full lineage (source file, row, checksum, schema version) is on each record. Measured seed tonnages vs estimated contamination are distinguished in the data dictionary.")
            st.markdown("---")
            a1, a2, a3 = st.columns([1.2, 1, 1])
            with a1:
                if btn("🛡️ Submit plan for review", key="plan_submit", type="primary",
                       disabled=not can("submit") or plan.status == "No data", help="Requires Analyst or Administrator"):
                    if guard("submit"):
                        plans = load_plans()
                        pid = f"PLAN-{len(plans) + 1:04d}"
                        params = {kk: vv for kk, vv in sc.items() if kk != "_v"}
                        result = {"kpis": plan.kpis, "capacity": plan.capacity.to_dict("records"),
                                  "shadows": plan.shadows, "flags": plan.flags, "solver": plan.solver}
                        with closing(db()) as c:
                            c.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (pid, now_iso(), PERIOD_LABEL, jdump({"params": params, "overrides": SS["overrides"]}),
                                       jdump(result), plan.alloc.to_json(orient="records"), "Pending", SS["role"], "", "", "",
                                       RULES_VERSION, FACTORS_VERSION))
                            c.commit()
                        audit("plan.submit", pid, f"cost {fmt_eur(plan.kpis['cost'])} · diversion {plan.kpis['diversion']:.1f}% · {plan.status}")
                        st.toast(f"{pid} submitted — switch to Reviewer to approve.", icon="🛡️"); st.rerun()
            with a2:
                dl("⬇️ Export plan (CSV)", plan.alloc.round(3).to_csv(index=False).encode(), "reflow_plan_allocations.csv", "text/csv", key="dl_plan")
            with a3:
                meta = {"id": "PLAN-DRAFT", "status": "DRAFT", "period": PERIOD_LABEL, "rules_version": RULES_VERSION,
                        "factors_version": FACTORS_VERSION, "submitted_by": SS["role"], "reviewed_by": "", "review_note": ""}
                result = {"kpis": plan.kpis, "capacity": plan.capacity.to_dict("records"), "shadows": plan.shadows,
                          "flags": plan.flags, "solver": plan.solver}
                md = build_report_md(meta, {kk: vv for kk, vv in sc.items() if kk != "_v"}, result, plan.alloc, SS["overrides"])
                dl("⬇️ Explanation (MD)", md.encode(), "reflow_plan_explanation.md", "text/markdown", key="dl_md")
        else:
            st.info("Validate or approve intake records to unlock the plan, Sankey and exports.")

# ----------------------------------------------------------------------------
# Page: Review & Audit
# ----------------------------------------------------------------------------
def page_review():
    sec("🛡️", "Review & audit", f"Current role: {SS['role']} · approvals enforced in handlers, not only the UI")
    with st.expander("🔐 Role & permission matrix"):
        matrix = pd.DataFrame([{"Capability": p, **{r: ("✓" if r in roles else "—") for r in ROLES}} for p, roles in PERMS.items()])
        show_df(matrix, height=300)
    recs = load_records()
    plans = load_plans()
    pending = plans[plans.status == "Pending"] if len(plans) else plans
    st.markdown("#### 🧾 Plans awaiting review")
    if not len(pending):
        st.info("No pending plans. Submit one from the Optimizer.")
    for row in pending.itertuples(index=False):
        with st.expander(f"📋 {row.id} · submitted {row.created_at} by {row.submitted_by}", expanded=True):
            pj = json.loads(row.params_json); rj = json.loads(row.result_json); k = rj["kpis"]
            st.markdown(chip(f"target {pj['params']['target']}%") + chip(f"carbon €{pj['params']['carbon']}/t")
                        + chip(f"capacity {pj['params']['capacity']}%") + chip(f"budget {'on' if pj['params']['budget_on'] else 'off'}")
                        + badge(row.status, "am"), unsafe_allow_html=True)
            st.markdown(f"**{fmt_eur(k['cost'])}/month** · diversion **{k['diversion']:.1f}%** · CO₂e **{k['co2']:,.0f} t** · recovered **{k['recovered_t']:,.0f} t**")
            for f in rj.get("flags", []):
                st.warning(f)
            note = st.text_input("Review note", key=f"pn_{row.id}", placeholder="conditions, corrections, rationale…")
            p1, p2, _ = st.columns([1, 1, 2])
            with p1:
                if btn("✅ Approve plan", key=f"pa_{row.id}", type="primary", disabled=not can("approve")):
                    if guard("approve"):
                        with closing(db()) as c:
                            c.execute("UPDATE plans SET status='Approved', reviewed_by=?, review_note=?, reviewed_at=? WHERE id=?",
                                      (SS["role"], note, now_iso(), row.id)); c.commit()
                        audit("plan.approve", row.id, note or "approved without note")
                        st.toast(f"{row.id} approved.", icon="✅"); st.rerun()
            with p2:
                if btn("⛔ Reject plan", key=f"pr_{row.id}", disabled=not can("approve") or not note.strip(), help="A note is required to reject"):
                    if guard("approve"):
                        with closing(db()) as c:
                            c.execute("UPDATE plans SET status='Rejected', reviewed_by=?, review_note=?, reviewed_at=? WHERE id=?",
                                      (SS["role"], note, now_iso(), row.id)); c.commit()
                        audit("plan.reject", row.id, note)
                        st.toast(f"{row.id} rejected.", icon="⛔"); st.rerun()

    st.markdown("#### 🚩 Flagged records — exception queue")
    flagged = recs[recs.status == "Flagged"]
    if not len(flagged):
        st.success("No flagged records. Missing, contradictory, late and out-of-range data lands here.")
    for r in flagged.itertuples(index=False):
        with st.expander(f"{r.id} · {STREAMS[r.stream_code]['name']} · {FAC_NAME.get(r.facility, r.facility)} · {r.tonnes:,.1f} t"):
            st.warning(r.notes)
            with st.form(key=f"fixform_{r.id}"):
                c1, c2, c3, c4 = st.columns(4)
                strm = c1.selectbox("Stream", STREAM_ORDER, index=STREAM_ORDER.index(r.stream_code), format_func=lambda s: STREAMS[s]["name"])
                date = c2.text_input("Date", value=r.date)
                tonnes = c3.number_input("Tonnes", 0.0, 2000.0, float(r.tonnes), 0.5)
                cont = c4.number_input("Cont %", 0.0, 60.0, min(float(r.contamination * 100), 60.0), 0.5)
                ok = stretch(st.form_submit_button, "💾 Save correction → Draft", disabled=not can("edit"))
            if ok and guard("edit"):
                errs, warns, norm = row_checks(date, r.facility, strm, tonnes, cont)
                if errs:
                    st.error(" · ".join(errs))
                else:
                    update_record(r.id, {**norm, "status": "Draft", "notes": "; ".join(warns)}, "record.exception.correct")
                    st.toast(f"{r.id} corrected → Draft. Re-validate below.", icon="🧪"); st.rerun()
            if btn("🧪 Re-validate now", key=f"rv_{r.id}", disabled=not can("edit")):
                if guard("edit"):
                    errs, warns, _ = row_checks(r.date, r.facility, r.stream_code, r.tonnes, r.contamination * 100)
                    if errs:
                        update_record(r.id, {"notes": "; ".join(errs)}, "record.validate.fail")
                        st.toast("Still failing: " + "; ".join(errs), icon="⛔")
                    else:
                        update_record(r.id, {"status": "Validated", "notes": "; ".join(warns)}, "record.validate")
                        st.toast(f"{r.id} validated.", icon="✅")
                    st.rerun()

    st.markdown("#### ✅ Validated records awaiting approval")
    val = recs[recs.status == "Validated"]
    picks = st.multiselect("Select records to approve", val.id.tolist(),
                           format_func=lambda i: f"{i} · {val.set_index('id').loc[i,'stream_code']} · {val.set_index('id').loc[i,'tonnes']:,.1f} t",
                           key="rev_picks")
    if btn(f"✅ Approve {len(picks)} record(s)", key="rev_bulk", type="primary", disabled=not can("approve") or not picks):
        if guard("approve"):
            for rid in picks:
                update_record(rid, {"status": "Approved"}, "record.approve.bulk")
            SS.pop("rev_picks", None)
            st.toast(f"{len(picks)} records approved.", icon="✅"); st.rerun()

    st.markdown("#### 🧾 Audit trail")
    adf = load_audit(1000)
    f1, f2 = st.columns([1, 2])
    actions = ["All"] + sorted(adf.action.unique().tolist())
    fa = f1.selectbox("Action type", actions, key="aud_act")
    fq = f2.text_input("Search details / entity", key="aud_q")
    if fa != "All":
        adf = adf[adf.action == fa]
    if fq:
        adf = adf[adf.apply(lambda r: fq.lower() in f"{r.entity} {r.details}".lower(), axis=1)]
    show_df(adf, height=320)
    dl("⬇️ Export audit trail (CSV)", adf.to_csv(index=False).encode(), "reflow_audit.csv", "text/csv", key="dl_aud")

# ----------------------------------------------------------------------------
# Page: Reports & Governance
# ----------------------------------------------------------------------------
DATA_DICT = [
    ("id", "TEXT PK", "—", "Stable record identifier", "WS-YYYY-NNNN", "system", "Internal", "unique, generated"),
    ("date", "DATE", "ISO day", "Activity / weighbridge date", "2020-01-01…2027-12-31", "import/manual", "Internal", "parseable; late arrivals flagged"),
    ("facility", "FK", "—", "Reporting site", "F1–F6 catalog", "import/manual", "Internal", "must exist in facility catalog"),
    ("stream_code", "FK", "—", "Classified material stream", "10-stream catalog (EWC-style)", "rules/manual", "Internal", "catalog membership; contradiction check vs description"),
    ("description", "TEXT", "—", "Free-text material description", "≤ 500 chars", "operator", "Internal", "optional; drives classifier cross-check"),
    ("tonnes", "REAL", "t (metric)", "Net mass received", "(0, 2000]", "weighbridge/estimate", "Internal", "positive, bounded; measured preferred"),
    ("contamination", "REAL", "fraction", "Non-target material share (stored 0–0.6; files use %)", "[0, 0.6]", "assay/estimate", "Internal", "0–60%; >60% flagged as exception"),
    ("status", "ENUM", "—", "Lifecycle state", "Draft/Validated/Approved/Flagged/Rejected/Archived", "workflow", "Internal", "transitions audited; Approved edits revert to Draft"),
    ("source", "TEXT", "—", "Producing system or entry mode", "free", "pipeline", "Internal", "provenance"),
    ("source_file", "TEXT", "—", "Archived original filename", "imports_archive/…", "pipeline", "Internal", "checksum in imports table"),
    ("source_row", "INT", "—", "Row in original file", "≥ 1", "pipeline", "Internal", "lineage"),
    ("schema_version", "INT", "—", "Record schema version", "1", "system", "Public", "migration marker"),
    ("created_at / updated_at", "TIMESTAMP", "local", "Creation / last mutation", "ISO", "system", "Internal", "set by DAL only"),
    ("notes", "TEXT", "—", "Exception / correction notes", "free", "workflow", "Internal", "shown in exception queue"),
]

def page_reports():
    sec("📚", "Reports & governance", "Exports · self-tests · data dictionary · runbook · acceptance evidence")
    plans = load_plans()
    alloc = None
    if len(plans):
        labels = plans.id.tolist()
        default = SS.get("rep_last", labels[0])
        idx = labels.index(default) if default in labels else 0
        pid = st.selectbox("Plan registry", labels, index=idx,
                           format_func=lambda i: f"{i} · {plans.set_index('id').loc[i,'status']} · {plans.set_index('id').loc[i,'created_at']}")
        SS["rep_last"] = pid
        row = plans[plans.id == pid].iloc[0]
        pj, rj = json.loads(row.params_json), json.loads(row.result_json)
        alloc = pd.read_json(io.StringIO(row.alloc_json))
        meta = {"id": row.id, "status": row.status, "period": row.period, "rules_version": row.rules_version,
                "factors_version": row.factors_version, "submitted_by": row.submitted_by,
                "reviewed_by": row.reviewed_by, "review_note": row.review_note}
        st.markdown(badge(row.status, STATUS_KIND.get(row.status, "sl")) + chip(row.period)
                    + chip(f"reviewer: {row.reviewed_by or '—'}"), unsafe_allow_html=True)
        md = build_report_md(meta, pj["params"], rj, alloc, pj.get("overrides", []))
    else:
        st.info("No submitted plans yet — generating a DRAFT report from the current scenario. Submit & approve a plan for the customer version.")
        recs = load_records()
        usable = recs[recs.status.isin(["Validated", "Approved"])].rename(
            columns={"stream_code": "stream", "contamination": "cont"})[["facility", "stream", "tonnes", "cont"]]
        sc = SS["scenario"]
        plan = solve_plan(aggregate_supply(usable, sc), sc, SS["overrides"])
        alloc = plan.alloc
        meta = {"id": "PLAN-DRAFT", "status": "DRAFT", "period": PERIOD_LABEL, "rules_version": RULES_VERSION,
                "factors_version": FACTORS_VERSION, "submitted_by": SS["role"], "reviewed_by": "", "review_note": ""}
        rj = {"kpis": plan.kpis, "capacity": plan.capacity.to_dict("records"), "shadows": plan.shadows, "flags": plan.flags, "solver": plan.solver}
        md = build_report_md(meta, {k: v for k, v in sc.items() if k != "_v"}, rj, alloc, SS["overrides"])
    preview = "\n".join(md.splitlines()[:42]) + "\n\n*…full report, sign-off block and limitations in the download.*"
    with st.container(border=True):
        st.markdown(preview)
    d1, d2 = st.columns(2)
    with d1:
        dl("⬇️ Full report (Markdown)", md.encode(), f"reflow_report_{meta['id']}.md", "text/markdown", key="dl_rep_md")
    with d2:
        dl("⬇️ Allocations (CSV)", alloc.round(3).to_csv(index=False).encode(), f"reflow_alloc_{meta['id']}.csv", "text/csv", key="dl_rep_csv")

    g1, g2, g3, g4, g5, g6 = st.tabs(["🧪 Self-tests", "📖 Data dictionary", "🗃 Catalogs & versions", "🛠 Runbook & backup", "✅ Acceptance", "⚖️ Licenses & limits"])
    with g1:
        st.caption("Deterministic in-app test suite: hand-checkable arithmetic, hard constraints, validation, reproducibility, exceptions, overrides, DB round-trip. Run it any time — results below are actual, not claimed.")
        if btn("▶ Run self-tests", key="run_tests", type="primary"):
            rows = run_self_tests()
            SS["last_tests"] = {"ts": now_iso(), "rows": rows}
            passed = sum(1 for r in rows if "PASS" in r["Result"])
            audit("qa.selftest", "suite", f"{passed}/{len(rows)} passed")
        if SS.get("last_tests"):
            rows = SS["last_tests"]["rows"]
            passed = sum(1 for r in rows if "PASS" in r["Result"])
            (st.success if passed == len(rows) else st.error)(f"{passed}/{len(rows)} tests passed · last run {SS['last_tests']['ts']}")
            show_df(pd.DataFrame(rows), height=430)
        else:
            st.info("Not run yet in this session.")
    with g2:
        show_df(pd.DataFrame(DATA_DICT, columns=["Field", "Type", "Unit", "Meaning", "Allowed values", "Source", "Sensitivity", "Validation rule"]), height=480)
        st.caption("Import files use contamination_pct (0–60); the database stores the fraction (0–0.6). Timestamps are local; jurisdiction fields belong to the facility catalog in the team edition.")
    with g3:
        st.markdown(chip(RULES_VERSION) + chip(FACTORS_VERSION) + chip(f"schema v{SCHEMA_VERSION}") + badge("SYNTHETIC FACTORS", "am"), unsafe_allow_html=True)
        srows = []
        for s in STREAM_ORDER:
            S = STREAMS[s]
            srows.append({"Code": s, "Stream": S["name"], "EWC-style": S["ewc"], "Base t/mo/unit": S["base"],
                          "Default cont %": S["cont"] * 100, "Recyclate €/t": S["price"],
                          "Allowed": ", ".join(p for p in PATH_ORDER if p == "landfill" or S.get(p)),
                          **{f"CO₂ {p}": S["co2"].get(p, "—") for p in PATH_ORDER}})
        show_df(pd.DataFrame(srows), height=390)
        show_df(pd.DataFrame([{"Pathway": P[p]["label"], "Gate fee €/t": P[p]["fee"], "Yield": P[p]["yield"],
                               "Residue → landfill": P[p]["residue"], "Capacity t/mo": P[p]["cap"]} for p in PATH_ORDER]))
        dl("⬇️ Catalogs (JSON)", jdump({"rules": RULES_VERSION, "factors": FACTORS_VERSION, "pathways": P, "streams": STREAMS}).encode(),
           "reflow_catalogs.json", "application/json", key="dl_cat")
        st.caption("Rollback: catalogs are versioned constants — restore a prior tagged version of this file and re-run self-tests before re-approving plans.")
    with g4:
        st.markdown("""
**Backup / restore (local MVP)** — the entire dataset is one SQLite file plus archived originals.
```powershell
# Backup: copy the database and import archive while the app is closed
Copy-Item waste_intelligence_demo.db backup_$(Get-Date -Format yyyyMMdd).db
Copy-Item imports_archive backup_imports -Recurse
# Restore: replace the file, then relaunch
Copy-Item backup_20261112.db waste_intelligence_demo.db -Force
streamlit run app.py
```
""")
        dl("⬇️ Hot backup (SQLite file)", DB_PATH.read_bytes(), f"reflow_backup_{dt.date.today().isoformat()}.db",
           "application/x-sqlite3", key="dl_backup")
    with g5:
        tests = {r["Test"]: r["Result"] for r in SS["last_tests"]["rows"]} if SS.get("last_tests") else {}

        def tstat(*names):
            if not tests:
                return "🕓 run self-tests"
            return "✅ PASS" if all("PASS" in tests.get(n, "") for n in names) else "❌ FAIL"
        n_approved = int((plans.status == "Approved").sum()) if len(plans) else 0
        rows = [
            {"Criterion": "Demo seeds realistic data on first launch", "Status": "✅ PASS" if len(load_records()) > 0 else "❌", "Evidence": f"{len(load_records())} records in SQLite"},
            {"Criterion": "Documented sample file imports; malformed rows rejected with actionable errors", "Status": tstat("Import rejects malformed rows"), "Evidence": "sample buttons + row-level error report"},
            {"Criterion": "Calculations reproducible & hand-checkable", "Status": tstat("Hand-checkable cost example (PET 100 t)", "Simulation reproducible (seeded)"), "Evidence": "seed 42 · deterministic LP"},
            {"Criterion": "Optimizer satisfies every hard constraint; infeasibility degrades safely", "Status": tstat("Hard capacity constraints respected", "Diversion target honored when feasible", "Infeasible scenario degrades safely"), "Evidence": "HiGHS + penalty-slack fallback"},
            {"Criterion": "Outputs traceable: inputs, versions, drivers, uncertainty", "Status": "✅ PASS", "Evidence": "Explanation & Sensitivity tabs, lineage per record"},
            {"Criterion": "Report export works", "Status": "✅ PASS", "Evidence": "Markdown + CSV downloads on this page"},
            {"Criterion": "Audit trail records material changes, approvals, overrides, exports", "Status": "✅ PASS" if len(load_audit(1)) else "🕓", "Evidence": f"{len(load_audit(1000))} events"},
            {"Criterion": "Authorized human approval gate exercised", "Status": "✅ PASS" if n_approved else "🕓 approve a plan", "Evidence": f"{n_approved} approved plan(s)"},
        ]
        show_df(pd.DataFrame(rows), height=330)
    with g6:
        show_df(pd.DataFrame([
            {"Dependency": "streamlit", "License": "Apache-2.0"}, {"Dependency": "pandas", "License": "BSD-3-Clause"},
            {"Dependency": "numpy", "License": "BSD-3-Clause"}, {"Dependency": "plotly", "License": "MIT"},
            {"Dependency": "scipy", "License": "BSD-3-Clause"}, {"Dependency": "openpyxl", "License": "MIT"},
            {"Dependency": "sqlite3 (stdlib)", "License": "PSF"},
        ]))


def render_sidebar():
    with st.sidebar:
        st.markdown('<div class="brand"><div class="logo">♻️</div><div><div class="name">ReFlow</div>'
                    '<div class="tag">WASTE STREAM INTELLIGENCE<br>CIRCULARITY OPTIMIZER · MVP 1.0</div></div></div>',
                    unsafe_allow_html=True)
        st.caption(APP_TAG)
        st.selectbox("Active role (demo personas)", ROLES, key="role", format_func=lambda r: ROLE_ICON[r])
        role = SS["role"]
        st.caption({"Administrator": "Full access · reset & backup", "Analyst": "Create · edit · import · submit plans",
                    "Reviewer": "Approve / reject · overrides", "Stakeholder": "Read-only · exports"}[role])
        st.divider()
        st.radio("Navigation", NAV, key="nav", label_visibility="collapsed")
        st.divider()
        sim = SS["sim"]
        held = sim["running"] and SS["nav"] not in LIVE_PAGES
        if sim["done"]:
            st.markdown('<span class="live paused"><span class="dot"></span>✔ COMPLETE</span>', unsafe_allow_html=True)
        elif sim["running"] and not held:
            st.markdown('<span class="live"><span class="dot"></span>SIMULATION LIVE</span>', unsafe_allow_html=True)
        elif held:
            st.markdown('<span class="live held"><span class="dot"></span>HELD ON THIS PAGE</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="live paused"><span class="dot"></span>PAUSED</span>', unsafe_allow_html=True)
        b1, b2, b3 = st.columns(3)
        with b1:
            label = "⏸" if sim["running"] else "▶"
            if btn(label, key="sim_toggle", disabled=sim["done"], help="Pause / resume the live simulation"):
                sim["running"] = not sim["running"]
                st.rerun()
        with b2:
            if btn("⏭", key="sim_step_btn", disabled=sim["done"], help="Advance one month"):
                sim_step()
                st.rerun()
        with b3:
            if btn("⟲", key="sim_replay_btn", help="Replay 12 months from the start"):
                sim_replay()
                st.rerun()
        st.select_slider("Tick pace", options=[0.5, 1.0, 2.0], key="speed", format_func=lambda v: f"{v:.1f}s / month")
        st.progress(sim["month"] / 12, text=f"{sim['month']}/12 months · seed {sim['seed']}")
        st.caption("Simulation advances on Overview & Optimizer. Pause anytime — arrivals are seeded and reproducible; plans follow your live scenario.")
        st.divider()
        n = len(load_records())
        size_kb = DB_PATH.stat().st_size / 1024 if DB_PATH.exists() else 0
        st.markdown(chip(f"🗂 {n} records") + chip(f"💾 {size_kb:,.0f} KB") + chip(PERIOD_LABEL), unsafe_allow_html=True)
        if role == "Administrator":
            with st.expander("🛠 Admin · danger zone"):
                confirm = st.checkbox("I understand this re-seeds all demo data", key="reset_confirm")
                if btn("🗑 Reset demo database", key="reset_btn", disabled=not confirm):
                    if guard("admin"):
                        reset_demo()
                        st.toast("Demo database re-seeded.", icon="🗑")
                        st.rerun()
        st.caption(f"{RULES_VERSION} · {FACTORS_VERSION}\nSynthetic demo · decision support only — human approval required.")


def main():
    st.set_page_config(page_title="ReFlow · Waste Stream Intelligence", page_icon="♻️",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    ensure_db()
    init_state()
    render_sidebar()
    page = SS["nav"]
    if page == NAV[0]:
        page_overview()
    elif page == NAV[1]:
        page_records()
    elif page == NAV[2]:
        page_optimizer()
    elif page == NAV[3]:
        page_review()
    else:
        page_reports()
    # Live simulation loop — one tail rerun per tick; editing pages hold the clock.
    if SS["sim"]["running"] and page in LIVE_PAGES and SS["sim"]["month"] < 12:
        time.sleep(float(SS["speed"]))
        sim_step()
        st.rerun()


if __name__ == "__main__":
    main()
