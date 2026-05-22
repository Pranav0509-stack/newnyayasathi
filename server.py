"""
LexSearch – Indian Court Judgments (High Courts + Supreme Court)
FastAPI backend: search API + PDF proxy from public AWS S3.
Run: uvicorn server:app --reload --port 8080
"""

# Load .env BEFORE any module that reads env vars at import-time.
# llm/router.py captures GEMINI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY
# in module globals when imported, so the .env file has to be parsed first
# or the LLM calls silently fall back to empty responses.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    # python-dotenv is optional; if it's not installed we rely on the
    # caller to have exported env vars themselves (e.g. via launch.json).
    pass

import io
import logging
import os
import re
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx
import ssl
import pandas as pd
import s3fs
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

# Phase 2 — auth + Brief assistant. Both modules degrade gracefully when
# optional deps (google-generativeai) aren't installed.
import legacy_auth as auth  # Phase 2: legacy single-file auth lives at legacy_auth.py
                            # so the new auth/ package can coexist.
from brief_service import answer_question, answer_conversational, _needs_case_retrieval, serialize_citations, generate_followups
from validators import input_guards
from validators.answer_gates import (
    BANNED_RE as _BANNED_PHRASE_RE,
    CASE_NAME_RE as _CASE_NAME_RE,
    SECTION_RE as _SECTION_REF_RE,
    _strip_markdown as _strip_md_for_validate,
)
import vault_service
import workflows
import web_signals
import doc_editor
from fastapi import File, UploadFile, Form

# ── Retrieval layer: FTS5 (primary) or BM25 (legacy fallback) ──────────
# The FTS5 adapter queries india_courts.db (22M+ records, <50ms) directly.
# Falls back to the legacy S3-based BM25 if FTS5 is not configured.
_RETRIEVAL_AVAILABLE = False
_FTS5_AVAILABLE = False
BM25Index = None  # type: ignore[assignment]
build_index = None  # type: ignore[assignment]
doc_to_retrieve_hit = None  # type: ignore[assignment]

_SERVER_DIR = Path(__file__).resolve().parent
# DB path resolution: env var > local symlink > sibling repo > /data (Docker)
_DB_CANDIDATES = [
    os.environ.get("INDIA_COURTS_DB", ""),
    str(_SERVER_DIR / "india_courts.db"),  # local symlink
    str(_SERVER_DIR.parent / "india-judgments-corpus" / "india_courts.db"),
    "/data/india_courts.db",  # Railway/Docker volume
]
INDIA_COURTS_DB = next((p for p in _DB_CANDIDATES if p and Path(p).exists()), _DB_CANDIDATES[2])

try:
    import sys as _sys
    _adapter_dir = str(_SERVER_DIR.parent / "india-judgments-corpus" / "scripts")
    if _adapter_dir not in _sys.path:
        _sys.path.insert(0, _adapter_dir)
    from sanhita_adapter import FTS5Index
    _FTS5_AVAILABLE = True
    _RETRIEVAL_AVAILABLE = True
except Exception as _fts5_err:
    FTS5Index = None  # type: ignore[assignment]

# Legacy BM25 fallback (only used if FTS5 adapter not found)
if not _FTS5_AVAILABLE:
    try:
        from retrieval import (
            BM25Index,
            build_index,
            doc_to_retrieve_hit,
        )
        _RETRIEVAL_AVAILABLE = True
    except Exception as _retrieval_err:  # pragma: no cover
        pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LexSearch")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "POST"],
    allow_headers=["*"],
)

# Contract Workbench routes (Sanhita Drafter)
try:
    from routes_contract import router as contract_router
    app.include_router(contract_router)
    logger.info("contract router mounted at /api/contract")
except Exception as _ce:
    logger.warning("contract router not loaded: %s", _ce)

# Smart Court Search (semantic + hybrid + in-app viewer)
try:
    from routes_search import router as search_router
    app.include_router(search_router)
    logger.info("search router mounted at /api/cases (smart-search, document, suggest)")
except Exception as _se:
    logger.warning("search router not loaded: %s", _se)

# Sanhita for Legal Aid (application intake)
try:
    from routes_legal_aid import router as legal_aid_router
    app.include_router(legal_aid_router)
    logger.info("legal-aid router mounted at /api/legal-aid")
except Exception as _le:
    logger.warning("legal-aid router not loaded: %s", _le)

# Phase 2 auth (signup / OTP / OAuth / sessions / claim). Gated by
# AUTH_V2_ENABLED so production can roll out behind a flag. See
# docs/AUTH_DESIGN.md §16.
try:
    from auth.deps import auth_v2_enabled
    if auth_v2_enabled():
        from auth.routes import router as auth_v2_router
        app.include_router(auth_v2_router)
        logger.info("auth_v2 router mounted at /auth")
    else:
        logger.info("auth_v2 router NOT mounted (AUTH_V2_ENABLED is false)")
except Exception as _ae:
    logger.warning("auth_v2 router not loaded: %s", _ae)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Sensible defaults: deny framing, strict referrer, no MIME sniffing.

    Exception: PDF proxy routes (/pdf/*, /sc-pdf/*, /doc-pdf/*) must allow
    same-origin framing so the in-app document viewer can embed them. They
    stream binary PDF only — no auth tokens, no scripts.
    """

    _IFRAME_OK = ("/pdf/", "/sc-pdf/", "/doc-pdf/")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        allow_frame = any(path.startswith(p) for p in self._IFRAME_OK)
        if allow_frame:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            # Some browsers prefer CSP frame-ancestors over the older header
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ---------------------------------------------------------------------------
# S3 config
# ---------------------------------------------------------------------------
HC_BUCKET = "indian-high-court-judgments"
SC_BUCKET = "indian-supreme-court-judgments"
HC_S3 = f"s3://{HC_BUCKET}"
SC_S3 = f"s3://{SC_BUCKET}"
HC_HTTP = f"https://{HC_BUCKET}.s3.amazonaws.com"
SC_HTTP = f"https://{SC_BUCKET}.s3.ap-south-1.amazonaws.com"

_fs: Optional[s3fs.S3FileSystem] = None


def get_fs() -> s3fs.S3FileSystem:
    global _fs
    if _fs is None:
        _fs = s3fs.S3FileSystem(anon=True)
    return _fs


# ---------------------------------------------------------------------------
# High Court mapping
# ---------------------------------------------------------------------------
HC_COURTS = [
    {"s3_code": "9_13", "name": "Allahabad High Court", "benches": [
        {"code": "cishclko", "name": "Lucknow Bench"},
        {"code": "cisdb_16012018", "name": "Allahabad"},
    ]},
    {"s3_code": "28_2", "name": "Andhra Pradesh High Court", "benches": [
        {"code": "aphc", "name": "Amaravati"},
    ]},
    {"s3_code": "27_1", "name": "Bombay High Court", "benches": [
        {"code": "newas", "name": "Appellate Side"},
        {"code": "newos", "name": "Original Side"},
        {"code": "newos_spl", "name": "Original Side (Special)"},
        {"code": "hcaurdb", "name": "Aurangabad Bench"},
        {"code": "kolhcdb", "name": "Nagpur Bench"},
        {"code": "hcbgoa", "name": "Goa Bench"},
    ]},
    {"s3_code": "19_16", "name": "Calcutta High Court", "benches": [
        {"code": "calcutta_appellate_side", "name": "Appellate Side"},
        {"code": "calcutta_original_side", "name": "Original Side"},
        {"code": "calcutta_circuit_bench_at_jalpaiguri", "name": "Jalpaiguri Circuit"},
        {"code": "calcutta_circuit_bench_at_port_blair", "name": "Port Blair Circuit"},
    ]},
    {"s3_code": "22_18", "name": "Chhattisgarh High Court", "benches": [
        {"code": "cghccisdb", "name": "Bilaspur"},
    ]},
    {"s3_code": "7_26", "name": "Delhi High Court", "benches": [
        {"code": "dhcdb", "name": "New Delhi"},
    ]},
    {"s3_code": "18_6", "name": "Gauhati High Court", "benches": [
        {"code": "asghccis", "name": "Guwahati"},
        {"code": "azghccis", "name": "Aizawl Bench"},
        {"code": "arghccis", "name": "Itanagar Bench"},
        {"code": "nlghccis", "name": "Kohima Bench"},
    ]},
    {"s3_code": "24_17", "name": "Gujarat High Court", "benches": [
        {"code": "gujarathc", "name": "Ahmedabad"},
    ]},
    {"s3_code": "2_5", "name": "Himachal Pradesh High Court", "benches": [
        {"code": "cmis", "name": "Shimla"},
    ]},
    {"s3_code": "1_12", "name": "Jammu & Kashmir High Court", "benches": [
        {"code": "jammuhc", "name": "Jammu"},
        {"code": "kashmirhc", "name": "Srinagar"},
    ]},
    {"s3_code": "20_7", "name": "Jharkhand High Court", "benches": [
        {"code": "jhar_pg", "name": "Ranchi"},
    ]},
    {"s3_code": "29_3", "name": "Karnataka High Court", "benches": [
        {"code": "karnataka_bng_old", "name": "Bengaluru"},
        {"code": "karhcdharwad", "name": "Dharwad Bench"},
        {"code": "karhckalaburagi", "name": "Kalaburagi Bench"},
    ]},
    {"s3_code": "32_4", "name": "Kerala High Court", "benches": [
        {"code": "highcourtofkerala", "name": "Ernakulam"},
    ]},
    {"s3_code": "23_23", "name": "Madhya Pradesh High Court", "benches": [
        {"code": "mphc_db_jbp", "name": "Jabalpur"},
        {"code": "mphc_db_gwl", "name": "Gwalior Bench"},
        {"code": "mphc_db_ind", "name": "Indore Bench"},
    ]},
    {"s3_code": "33_10", "name": "Madras High Court", "benches": [
        {"code": "hc_cis_mas", "name": "Chennai"},
        {"code": "mdubench", "name": "Madurai Bench"},
    ]},
    {"s3_code": "14_25", "name": "Manipur High Court", "benches": [
        {"code": "manipurhc_pg", "name": "Imphal"},
    ]},
    {"s3_code": "17_21", "name": "Meghalaya High Court", "benches": [
        {"code": "meghalaya", "name": "Shillong"},
    ]},
    {"s3_code": "21_11", "name": "Orissa High Court", "benches": [
        {"code": "cisnc", "name": "Cuttack"},
    ]},
    {"s3_code": "10_8", "name": "Patna High Court", "benches": [
        {"code": "patnahcucisdb94", "name": "Patna"},
    ]},
    {"s3_code": "3_22", "name": "Punjab & Haryana High Court", "benches": [
        {"code": "phhc", "name": "Chandigarh"},
    ]},
    {"s3_code": "8_9", "name": "Rajasthan High Court", "benches": [
        {"code": "rhcjodh240618", "name": "Jodhpur"},
        {"code": "jaipur", "name": "Jaipur Bench"},
    ]},
    {"s3_code": "11_24", "name": "Sikkim High Court", "benches": [
        {"code": "sikkimhc_pg", "name": "Gangtok"},
    ]},
    {"s3_code": "16_20", "name": "Telangana High Court", "benches": [
        {"code": "thcnc", "name": "Hyderabad"},
    ]},
    {"s3_code": "36_29", "name": "Andhra Pradesh High Court (Kurnool)", "benches": [
        {"code": "taphc", "name": "Kurnool"},
    ]},
    {"s3_code": "5_15", "name": "Uttarakhand High Court", "benches": [
        {"code": "ukhcucis_pg", "name": "Nainital"},
    ]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def _extract_pdf_filename(pdf_link: str) -> str:
    if not pdf_link:
        return ""
    return pdf_link.rstrip("/").split("/")[-1]


# ── High Court helpers ────────────────────────────────────────────────────

def _hc_parquet_path(year: int, court: str, bench: str) -> str:
    return f"{HC_S3}/metadata/parquet/year={year}/court={court}/bench={bench}/metadata.parquet"


def _hc_df_to_results(df: pd.DataFrame, court: str, bench: str, year: int) -> list[dict]:
    results = []
    for _, row in df.iterrows():
        pdf_link = _safe_str(row.get("pdf_link", ""))
        pdf_filename = _extract_pdf_filename(pdf_link)
        if not pdf_filename:
            continue
        s3_key = f"data/pdf/year={year}/court={court}/bench={bench}/{pdf_filename}"
        results.append({
            "type": "hc",
            "case_number": _safe_str(row.get("cnr", "")) or pdf_filename.replace(".pdf", ""),
            "title": _safe_str(row.get("title", "")) or pdf_filename.replace(".pdf", ""),
            "court_name": _safe_str(row.get("court", "")),
            "court": court,
            "bench": bench,
            "year": year,
            "judge": _safe_str(row.get("judge", "")),
            "date": _safe_str(row.get("decision_date", "")) or str(year),
            "disposal": _safe_str(row.get("disposal_nature", "")),
            "s3_key": urllib.parse.quote(s3_key, safe=""),
        })
    return results


# ── Supreme Court helpers ─────────────────────────────────────────────────

def _sc_parquet_path(year: int) -> str:
    return f"{SC_S3}/metadata/parquet/year={year}/metadata.parquet"


def _sc_df_to_results(df: pd.DataFrame, year: int) -> list[dict]:
    results = []
    for _, row in df.iterrows():
        path = _safe_str(row.get("path", ""))
        if not path:
            continue
        pdf_name = f"{path}_EN.pdf"
        title = _safe_str(row.get("title", ""))
        petitioner = _safe_str(row.get("petitioner", ""))
        respondent = _safe_str(row.get("respondent", ""))
        citation = _safe_str(row.get("citation", ""))
        case_id = _safe_str(row.get("case_id", ""))
        # Build a tar-based key: sc_tar/{year}/{pdf_name}
        tar_key = f"sc_tar/{year}/{pdf_name}"
        results.append({
            "type": "sc",
            "case_number": case_id or _safe_str(row.get("cnr", "")),
            "title": title or f"{petitioner} vs {respondent}",
            "petitioner": petitioner,
            "respondent": respondent,
            "citation": citation,
            "court_name": "Supreme Court of India",
            "court": "sc",
            "bench": "",
            "year": year,
            "judge": _safe_str(row.get("judge", "")),
            "author_judge": _safe_str(row.get("author_judge", "")),
            "date": _safe_str(row.get("decision_date", "")) or str(year),
            "disposal": _safe_str(row.get("disposal_nature", "")),
            "s3_key": urllib.parse.quote(tar_key, safe=""),
        })
    return results


def _apply_filters(df: pd.DataFrame, q: str, cnr: str, judge: str,
                   case_type: str, disposal: str, petitioner: str,
                   respondent: str, citation: str) -> pd.DataFrame:
    """Apply common filters to a DataFrame."""
    if cnr:
        col = "cnr" if "cnr" in df.columns else "case_id"
        if col in df.columns:
            df = df[df[col].astype(str).str.lower().str.contains(cnr.lower(), na=False)]
    if judge:
        df = df[df["judge"].astype(str).str.lower().str.contains(judge.lower(), na=False)]
    if case_type:
        df = df[df["title"].astype(str).str.upper().str.startswith(case_type.upper(), na=False)]
    if disposal:
        df = df[df["disposal_nature"].astype(str).str.lower().str.contains(disposal.lower(), na=False)]
    if petitioner and "petitioner" in df.columns:
        df = df[df["petitioner"].astype(str).str.lower().str.contains(petitioner.lower(), na=False)]
    if respondent and "respondent" in df.columns:
        df = df[df["respondent"].astype(str).str.lower().str.contains(respondent.lower(), na=False)]
    if citation and "citation" in df.columns:
        df = df[df["citation"].astype(str).str.lower().str.contains(citation.lower(), na=False)]
    if q:
        q_lower = q.lower()
        mask = pd.Series(False, index=df.index)
        for col_name in ["title", "description", "petitioner", "respondent"]:
            if col_name in df.columns:
                mask |= df[col_name].astype(str).str.lower().str.contains(q_lower, na=False)
        df = df[mask]
    return df


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/courts")
def list_courts():
    return JSONResponse(HC_COURTS)


@app.get("/search")
def search(
    mode: str = Query(default="hc", description="hc or sc"),
    court: str = Query(default=""),
    bench: str = Query(default=""),
    year: int = Query(default=0),
    q: str = Query(default=""),
    cnr: str = Query(default=""),
    judge: str = Query(default=""),
    case_type: str = Query(default=""),
    disposal: str = Query(default=""),
    petitioner: str = Query(default=""),
    respondent: str = Query(default=""),
    citation: str = Query(default=""),
    page: int = Query(default=1, ge=1),
):
    PAGE_SIZE = 50
    fs = get_fs()
    results: list[dict] = []

    years = [year] if year else list(range(2024, 2019, -1))

    if mode == "sc":
        # ── Supreme Court search ──
        has_filter = q or cnr or judge or case_type or disposal or petitioner or respondent or citation or year
        if not has_filter:
            raise HTTPException(400, "Provide at least one filter (year recommended).")

        for yr in years:
            path = _sc_parquet_path(yr)
            try:
                with fs.open(path, "rb") as f:
                    df = pd.read_parquet(f)
                logger.info(f"SC: Loaded {len(df)} rows from year={yr}")
            except Exception as e:
                logger.debug(f"SC: Skipping year={yr}: {e}")
                continue

            df = _apply_filters(df, q, cnr, judge, case_type, disposal, petitioner, respondent, citation)
            results.extend(_sc_df_to_results(df, yr))

            if len(results) >= PAGE_SIZE * page + PAGE_SIZE:
                break
    else:
        # ── High Court search ──
        has_filter = court or q or cnr or judge or case_type or year
        if not has_filter:
            raise HTTPException(400, "Provide at least one filter.")

        courts_to_scan: list[tuple[str, str]] = []
        if court and bench:
            courts_to_scan = [(court, bench)]
        elif court:
            for c in HC_COURTS:
                if c["s3_code"] == court:
                    courts_to_scan = [(court, b["code"]) for b in c["benches"]]
                    break
            if not courts_to_scan:
                courts_to_scan = [(court, court)]
        else:
            courts_to_scan = [
                ("7_26", "dhcdb"), ("27_1", "newas"),
                ("33_10", "hc_cis_mas"), ("19_16", "calcutta_appellate_side"),
                ("9_13", "cishclko"),
            ]

        for yr in years:
            for ct, bn in courts_to_scan:
                path = _hc_parquet_path(yr, ct, bn)
                try:
                    with fs.open(path, "rb") as f:
                        df = pd.read_parquet(f)
                    logger.info(f"HC: Loaded {len(df)} rows from year={yr}/court={ct}/bench={bn}")
                except Exception as e:
                    logger.debug(f"HC: Skipping {path}: {e}")
                    continue

                df = _apply_filters(df, q, cnr, judge, case_type, disposal, "", "", "")
                results.extend(_hc_df_to_results(df, ct, bn, yr))

                if len(results) >= PAGE_SIZE * page + PAGE_SIZE:
                    break
            if len(results) >= PAGE_SIZE * page + PAGE_SIZE:
                break

    start = (page - 1) * PAGE_SIZE
    return JSONResponse({
        "total": len(results),
        "page": page,
        "page_size": PAGE_SIZE,
        "results": results[start:start + PAGE_SIZE],
    })


# ---------------------------------------------------------------------------
# PDF endpoints
# ---------------------------------------------------------------------------

# SSL config for upstream PDF fetches.
# Some user machines run a TLS-intercepting proxy (corporate AV, dev tunnel,
# VPN) that breaks the default system trust store. We retry with verify=False
# in that single case — these are public PDFs, so MITM is not a privacy risk.
_HTTPX_VERIFY = os.environ.get("SANHITA_HTTPX_VERIFY", "true").lower() != "false"


def _new_pdf_client(*, timeout: int = 60) -> httpx.AsyncClient:
    """Create an AsyncClient with TLS settings tolerant of TLS-intercepting
    proxies. We default to verified TLS; fall through to unverified only on
    the documented SSL chain error."""
    return httpx.AsyncClient(timeout=timeout, verify=_HTTPX_VERIFY)


@app.get("/pdf/{s3_key:path}")
async def proxy_pdf(s3_key: str, download: bool = False):
    """Proxy HC PDF from S3.

    Opens the upstream stream and inspects status BEFORE returning a
    response — so a missing PDF turns into a JSON 404, not a half-streamed
    error the browser can't recover from.

    On SSL chain failure (corporate proxy intercept), falls back to a
    verify=False request — public PDFs, no privacy risk.
    """
    decoded = urllib.parse.unquote(s3_key)
    url = f"{HC_HTTP}/{decoded}"
    fname = decoded.split("/")[-1] or "judgment.pdf"
    disp = f'attachment; filename="{fname}"' if download else f'inline; filename="{fname}"'

    async def _try(verify: bool):
        cli = httpx.AsyncClient(timeout=60, verify=verify)
        req = cli.build_request("GET", url)
        resp = await cli.send(req, stream=True)
        return cli, resp

    client = None
    resp = None
    try:
        try:
            client, resp = await _try(verify=_HTTPX_VERIFY)
        except (httpx.ConnectError, ssl.SSLError) as ssl_exc:
            logger.warning("HC PDF SSL retry (verify=False) for %s: %s", decoded, ssl_exc)
            client, resp = await _try(verify=False)
    except httpx.RequestError as exc:
        if client is not None:
            await client.aclose()
        logger.warning("HC PDF upstream error for %s: %s", decoded, exc)
        raise HTTPException(502, "Court archive is unreachable. Try again in a minute.")

    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(404, "PDF not available in the public archive.")

    async def stream():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        media_type="application/pdf",
        headers={"Content-Disposition": disp},
    )


@app.head("/pdf/{s3_key:path}")
async def probe_pdf(s3_key: str):
    """Lightweight probe so the UI can detect a dead PDF link without
    downloading bytes. Returns 200 if the object exists, 404 otherwise."""
    decoded = urllib.parse.unquote(s3_key)
    url = f"{HC_HTTP}/{decoded}"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.head(url)
    except httpx.RequestError:
        raise HTTPException(502, "Court archive is unreachable.")
    if resp.status_code != 200:
        raise HTTPException(404, "PDF not available.")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# /doc-pdf/{doc_id} — generic remote PDF proxy for the documents table.
# Streams whatever pdf_url is stored against the row, with same-origin headers
# so the in-app iframe can render it (avoids X-Frame-Options blocks).
# ---------------------------------------------------------------------------

@app.get("/doc-pdf/{doc_id}")
async def proxy_doc_pdf(doc_id: str, download: bool = False):
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)
    row = idx.conn.execute(
        "SELECT pdf_url, title FROM documents WHERE doc_id = ? LIMIT 1",
        (doc_id,),
    ).fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "No PDF on file for this document.")
    upstream = row[0]
    fname = upstream.rstrip("/").split("/")[-1] or "document.pdf"
    disp = f'attachment; filename="{fname}"' if download else f'inline; filename="{fname}"'

    headers = {"User-Agent": "Mozilla/5.0 (compatible; SanhitaProxy/1.0)"}

    async def _try(verify: bool):
        cli = httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers=headers, verify=verify)
        req = cli.build_request("GET", upstream)
        resp = await cli.send(req, stream=True)
        return cli, resp

    client = None
    resp = None
    try:
        try:
            client, resp = await _try(verify=_HTTPX_VERIFY)
        except (httpx.ConnectError, ssl.SSLError) as ssl_exc:
            logger.warning("doc PDF SSL retry (verify=False) for %s: %s",
                           upstream, ssl_exc)
            client, resp = await _try(verify=False)
    except httpx.RequestError as exc:
        if client is not None:
            await client.aclose()
        logger.warning("doc PDF upstream error for %s: %s", upstream, exc)
        raise HTTPException(502, "Upstream is unreachable. Try again in a minute.")

    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(404, "PDF not available at the source URL.")

    async def stream():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        media_type="application/pdf",
        headers={"Content-Disposition": disp},
    )


# ---------------------------------------------------------------------------
# Retrieval layer — FTS5 (primary) or BM25 (legacy fallback)
# ---------------------------------------------------------------------------
BM25_PATH = Path(os.environ.get("LEXSEARCH_BM25_PATH", str(Path(__file__).parent / "bm25.pkl")))
BM25_ENABLED = os.environ.get("LEXSEARCH_BM25_ENABLED", "true").lower() == "true"
BM25_MAX_DOCS = int(os.environ.get("LEXSEARCH_BM25_MAX_DOCS", "0")) or None
BM25_LAZY = os.environ.get("LEXSEARCH_BM25_LAZY", "true").lower() == "true"

_bm25_lock = threading.Lock()
_bm25_index = None  # FTS5Index or BM25Index
_bm25_loading = False
_bm25_load_error: Optional[str] = None
_bm25_loaded_at: float = 0.0


def _load_bm25_blocking() -> None:
    """Load retrieval index: FTS5 (instant, preferred) or BM25 (legacy fallback)."""
    global _bm25_index, _bm25_loading, _bm25_load_error, _bm25_loaded_at

    # ── FTS5 path: instant startup, 22M+ docs, <50ms queries ──
    if _FTS5_AVAILABLE and Path(INDIA_COURTS_DB).exists():
        try:
            _bm25_index = FTS5Index(INDIA_COURTS_DB)
            _bm25_loaded_at = time.time()
            _bm25_load_error = None
            logger.info(
                "FTS5 ready: %d docs from %s (instant startup, multi-table search)",
                len(_bm25_index), INDIA_COURTS_DB,
            )
            return
        except Exception as e:
            logger.warning("FTS5 init failed, falling back to BM25: %s", e)

    # ── Legacy BM25 path: S3-based pickle ──
    if not _RETRIEVAL_AVAILABLE:
        _bm25_load_error = "retrieval module unavailable"
        return
    try:
        if BM25_PATH.exists():
            logger.info("Loading BM25 index from %s", BM25_PATH)
            _bm25_index = BM25Index.load(BM25_PATH)  # type: ignore[union-attr]
        else:
            logger.warning(
                "BM25 pickle not found at %s — building from S3 (this takes minutes). "
                "In production pre-build via `python ingest/rebuild_bm25.py`.",
                BM25_PATH,
            )
            _bm25_index = build_index(max_docs=BM25_MAX_DOCS)  # type: ignore[misc]
            try:
                _bm25_index.save(BM25_PATH)  # type: ignore[union-attr]
            except Exception as save_err:
                logger.warning("Could not persist BM25 index: %s", save_err)
        _bm25_loaded_at = time.time()
        _bm25_load_error = None
        if hasattr(_bm25_index, 'docs'):
            logger.info("BM25 ready: %d docs", len(_bm25_index.docs))
        else:
            logger.info("BM25 ready")
    except Exception as e:
        _bm25_load_error = f"{type(e).__name__}: {e}"
        logger.error("BM25 load failed: %s", _bm25_load_error)
    finally:
        _bm25_loading = False


def _ensure_bm25():
    """Return the loaded index, kicking off a background load on first call."""
    global _bm25_loading
    if not BM25_ENABLED or not _RETRIEVAL_AVAILABLE:
        return None
    if _bm25_index is not None:
        return _bm25_index
    with _bm25_lock:
        if _bm25_index is not None:
            return _bm25_index
        if not _bm25_loading:
            _bm25_loading = True
            if BM25_LAZY:
                t = threading.Thread(target=_load_bm25_blocking, daemon=True)
                t.start()
            else:
                _load_bm25_blocking()
    return _bm25_index


@app.on_event("startup")
def _startup_load_index() -> None:
    # Phase 2: ensure the SQLite schema exists before any request lands.
    try:
        auth.init_db()
        auth.seed_demo_code()
        logger.info("auth db ready at %s (demo code active)", auth.DB_PATH)
    except Exception as e:
        logger.error("auth.init_db failed: %s", e)

    if BM25_ENABLED and not BM25_LAZY:
        _ensure_bm25()
    elif BM25_ENABLED:
        # kick off background warm-up so the first /retrieve isn't cold
        _ensure_bm25()


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    k: int = Field(default=5, ge=1, le=20)
    tier: Optional[str] = Field(default=None, description="SC / HC / DC filter")


@app.post("/retrieve")
def retrieve(req: RetrieveRequest):
    """Dense retrieval endpoint consumed by NyayaSathi for grounding.

    Returns an ordered list of hits. Empty list with 200 means the index is
    loaded but no relevant docs were found — the caller should fall back to
    its local corpus. A 503 means the index isn't ready yet.
    """
    if not _RETRIEVAL_AVAILABLE:
        raise HTTPException(503, "retrieval module not installed on this deployment")
    idx = _ensure_bm25()
    if idx is None:
        raise HTTPException(503, f"BM25 index not ready ({_bm25_load_error or 'loading'})")

    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
        result_hits = idx.search(req.query, limit=req.k)
    else:
        raw = idx.query(req.query, k=req.k, tier=req.tier)
        result_hits = [doc_to_retrieve_hit(d, s, req.query) for d, s in raw]  # type: ignore[misc]
    return JSONResponse(
        {
            "query": req.query,
            "k": req.k,
            "tier": req.tier,
            "count": len(result_hits),
            "hits": result_hits,
        }
    )


@app.get("/judgment/{case_id}")
def get_judgment(case_id: str):
    """Return the indexed row for a single case_id (title, citation, body).

    NyayaSathi uses this when the caller asks a follow-up like "tell me more
    about that judgment". Text, not PDF — use /pdf/* or /sc-pdf/* for files.
    """
    idx = _ensure_bm25()
    if idx is None:
        raise HTTPException(503, "Index not ready")
    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
        case = idx.get(case_id)
        if case:
            return JSONResponse(case)
        raise HTTPException(404, f"case_id {case_id} not found")
    # Legacy BM25 path
    for d in idx.docs:
        if d.case_id == case_id:
            return JSONResponse(
                {
                    "case_id": d.case_id,
                    "court": d.court,
                    "bench": d.bench,
                    "year": d.year,
                    "date": d.date,
                    "title": d.title,
                    "citation": d.citation,
                    "tier": d.tier,
                    "text": {k: _safe_str(v) for k, v in d.source_row.items()
                             if k in ("title", "headnote", "description", "judge",
                                      "petitioner", "respondent", "citation",
                                      "disposal_nature")},
                }
            )
    raise HTTPException(404, f"case_id {case_id} not in index")


@app.post("/admin/reload")
def admin_reload(authorization: Optional[str] = Header(default=None)):
    """Hot-reload the BM25 pickle after `rebuild_bm25.py` writes a new one.
    Auth: Bearer token via LEXSEARCH_ADMIN_TOKEN env. If unset, endpoint is
    effectively disabled (403)."""
    global _bm25_index, _bm25_loading, _bm25_load_error
    expected = os.environ.get("LEXSEARCH_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(403, "admin reload disabled (set LEXSEARCH_ADMIN_TOKEN)")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(401, "bad token")
    with _bm25_lock:
        _bm25_index = None
        _bm25_loading = False
        _bm25_load_error = None
    _ensure_bm25()
    return {"status": "reloading"}


# ---------------------------------------------------------------------------
# Court Search + Analytics API (powered by india_courts.db FTS5)
# ---------------------------------------------------------------------------

# ── Document-type → optimized query rewrite map ────────────────────────────
# Each doc type gets a keyword prefix prepended to the user query so the
# FTS5 search returns the most relevant case law for that document type.
_DOC_TYPE_QUERY_PREFIX: dict[str, str] = {
    "bail_application":    "bail Section 437 439",
    "anticipatory_bail":   "anticipatory bail Section 438",
    "writ_petition":       "writ petition Article 226 mandamus certiorari",
    "legal_notice":        "notice Section 138 dishonour demand",
    "plaint":              "plaint civil suit decree",
    "written_statement":   "written statement reply defence",
    "consumer_complaint":  "consumer complaint deficiency service",
    "affidavit":           "affidavit sworn statement verification",
    "memo_of_appeal":      "appeal judgment set aside",
    "vakalatnama":         "",
}


@app.get("/api/cases/search")
def api_cases_search(
    q: str = "",
    court_code: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    verdict: Optional[str] = None,
    doc_type: Optional[str] = None,   # filter/boost by document type
    source: Optional[str] = None,     # "judgments" | "documents" | "all"
    k: int = 50,
    page: int = 1,                    # 1-indexed page number
    ls_session: Optional[str] = Cookie(default=None),
):
    """Full-text search across 83M+ Indian legal records.

    source="judgments" → only HC/SC judgments
    source="documents" → only statutes, legal docs, legal QA
    source="all" (default) → smart intent-based routing across all tables

    Pagination: pass `k` (page size, default 50, max 200) and `page`
    (1-indexed). The backend over-fetches to support deep paging and
    returns `total_pages` so the UI can render a page strip.
    """
    user = _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not q:
        return {"hits": [], "total": 0, "engine": "none"}

    # Clamp inputs
    k = max(1, min(int(k or 50), 200))
    page = max(1, int(page or 1))
    offset = (page - 1) * k

    # Boost query with doc-type prefix so relevant case law surfaces first
    search_q = q
    if doc_type and doc_type in _DOC_TYPE_QUERY_PREFIX:
        prefix = _DOC_TYPE_QUERY_PREFIX[doc_type]
        if prefix:
            search_q = f"{prefix} {q}"

    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
        # Over-fetch: ask for offset + k rows, then slice client-side.
        # We support pagination up to 2000 deep without changing the adapter.
        fetch_limit = min(offset + k, 2000)
        hits = idx.search(
            search_q, court_code=court_code,
            year_from=year_from, year_to=year_to,
            verdict=verdict, limit=fetch_limit,
            source=source or "all",
        )
        page_hits = hits[offset: offset + k]
        detected_intent = (page_hits[0].get("intent", "general")
                           if page_hits else "general")
        return {
            "hits":             page_hits,
            "page":             page,
            "page_size":        k,
            "page_count":       len(page_hits),
            "total_fetched":    len(hits),
            "has_more":         len(hits) > offset + k,
            "total":            len(idx),
            "engine":           "fts5",
            "effective_query":  search_q,
            "intent":           detected_intent,
        }
    return {"hits": [], "total": 0, "engine": "bm25_no_search"}


@app.get("/api/cases/latest")
def api_cases_latest(
    jurisdiction: str = "IN",
    k: int = 20,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Newest cases in the corpus."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None:
        return {"hits": [], "total": 0}
    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
        return {"hits": idx.latest(limit=k), "total": len(idx)}
    return {"hits": [], "total": 0}


@app.get("/api/cases/courts")
def api_cases_courts(ls_session: Optional[str] = Cookie(default=None)):
    """List all courts with case counts for filter dropdowns."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"courts": []}
    return {"courts": idx.courts()}


@app.get("/api/cases/verdicts")
def api_cases_verdicts(ls_session: Optional[str] = Cookie(default=None)):
    """List top verdicts with counts for filter dropdowns."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"verdicts": []}
    return {"verdicts": idx.verdicts()}


@app.get("/api/cases/related/{case_id}")
def api_related_cases(
    case_id: str,
    limit: int = 10,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Get cases related to a given case via citation graph (1-hop neighbors)."""
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"related": []}
    return {"related": idx.related_cases(case_id, limit=min(limit, 50))}


@app.get("/api/cases/doc-types")
def api_doc_types(ls_session: Optional[str] = Cookie(default=None)):
    """Document type distribution (from classifier)."""
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"doc_types": []}
    return {"doc_types": idx.doc_type_distribution()}


@app.get("/api/templates")
def api_templates_list(ls_session: Optional[str] = Cookie(default=None)):
    """List all draft templates from database."""
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"templates": []}
    return {"templates": idx.draft_templates()}


@app.get("/api/templates/{template_id}")
def api_template_detail(
    template_id: str,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Get a single template with full body text."""
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)
    tmpl = idx.draft_template(template_id)
    if not tmpl:
        raise HTTPException(404, "Template not found")
    return tmpl


@app.get("/api/cases/{case_id}")
def api_case_detail(
    case_id: str,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Single case detail. Handles prefixed IDs: 'doc_*', 'statute_*', 'qa_*'."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None:
        raise HTTPException(503)
    if not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)

    # 1. Try the primary judgments lookup first (idx.get is judgments-only)
    case = idx.get(case_id)
    if case:
        return case

    # 2. Strip prefix + try the appropriate corpus
    bare = case_id
    for prefix in ("doc_", "statute_", "qa_"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break

    conn = idx.conn

    # Try documents (gov circulars, India Code acts, etc.)
    if case_id.startswith("doc_") or not case_id.startswith(("statute_", "qa_")):
        row = conn.execute(
            "SELECT doc_id, title, source, doc_type, category, issued_date, "
            "issuer, pdf_url, landing_url, summary "
            "FROM documents WHERE doc_id = ? LIMIT 1",
            (bare,),
        ).fetchone()
        if row:
            return {
                "case_id":      f"doc_{row[0]}",
                "title":        row[1] or "",
                "court":        row[6] or row[2].upper(),
                "year":         int(row[5][:4]) if row[5] and row[5][:4].isdigit() else None,
                "citation":     row[1] or "",
                "verdict":      "",
                "judge":        "",
                "bench":        "",
                "date_decided": row[5] or "",
                "tier":         "DOCUMENT",
                "doc_type":     "DOCUMENT",
                "doc_subtype":  row[3] or "",
                "category":     row[4] or "",
                "issuer":       row[6] or "",
                "issued_date":  row[5] or "",
                "url":          row[8] or row[7] or "",
                "pdf_link":     "",          # populated by /text endpoint
                "pdf_available": bool(row[7]),
                "excerpt":      (row[9] or "")[:600],
                "explanation":  "",
                "description":  "",
                "source":       f"documents:{row[2]}",
                "jurisdiction": "IN",
            }

    # Try statutes
    if case_id.startswith("statute_"):
        row = conn.execute(
            "SELECT id, title, doc_id, full_text, entity, year, url "
            "FROM statutes WHERE id = ? OR doc_id = ? LIMIT 1",
            (bare, bare),
        ).fetchone()
        if row:
            return {
                "case_id":      f"statute_{row[0]}",
                "title":        row[1] or "",
                "court":        row[4] or "Parliament of India",
                "year":         row[5],
                "citation":     row[1] or "",
                "verdict":      "",
                "judge":        "",
                "tier":         "STATUTE",
                "doc_type":     "STATUTE",
                "url":          row[6] or "",
                "pdf_link":     "",
                "pdf_available": bool(row[6] and row[6].lower().endswith(".pdf")),
                "excerpt":      (row[3] or "")[:800],
                "full_text":    row[3] or "",
                "explanation":  "",
                "description":  "",
                "source":       "statutes",
                "jurisdiction": "IN",
            }

    # Try legal_qa
    if case_id.startswith("qa_"):
        row = conn.execute(
            "SELECT qa_id, context, question, answer, category "
            "FROM legal_qa WHERE qa_id = ? LIMIT 1",
            (bare,),
        ).fetchone()
        if row:
            return {
                "case_id":      f"qa_{row[0]}",
                "title":        (row[2] or "")[:150] or "Legal QA",
                "court":        "Legal Reference",
                "year":         None,
                "citation":     "",
                "verdict":      "",
                "judge":        "",
                "tier":         "QA",
                "doc_type":     "LEGAL_QA",
                "category":     row[4] or "",
                "url":          "",
                "pdf_link":     "",
                "pdf_available": False,
                "excerpt":      (row[3] or row[1] or "")[:800],
                "explanation":  "",
                "description":  "",
                "source":       "legal_qa",
                "jurisdiction": "IN",
            }

    # Try legal_docs (raw doc_id, no prefix)
    row = conn.execute(
        "SELECT doc_id, title, court, year, citation, summary, verdict, judge, "
        "       url, source, full_text, doc_type "
        "FROM legal_docs WHERE doc_id = ? LIMIT 1",
        (bare,),
    ).fetchone()
    if row:
        return {
            "case_id":      row[0],
            "title":        row[1] or "",
            "court":        row[2] or "",
            "year":         row[3],
            "citation":     row[4] or row[0],
            "excerpt":      (row[5] or "")[:600],
            "full_text":    row[10] or "",
            "verdict":      row[6] or "",
            "judge":        row[7] or "",
            "tier":         "SC" if (row[11] == "sc_judgment" or "supreme" in (row[2] or "").lower()) else "HC",
            "doc_type":     "LEGAL_DOC",
            "url":          row[8] or "",
            "pdf_link":     row[8] if (row[8] or "").lower().endswith(".pdf") else "",
            "pdf_available": bool(row[8] and row[8].lower().endswith(".pdf")),
            "source":       f"legal_docs:{row[9]}",
            "jurisdiction": "IN",
            "explanation":  "",
            "description":  "",
        }

    raise HTTPException(404, f"Case {case_id!r} not found in any corpus")


@app.get("/api/cases/{case_id}/text")
def api_case_text(
    case_id: str,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Return full text + PDF URL for a case — used by the in-app PDF/text reader.

    Response:
      { case_id, title, full_text, has_pdf, pdf_url, source }
    """
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)

    conn = idx.conn
    # Try judgments table first — fetch all path components needed for S3 key
    row = conn.execute(
        "SELECT cnr, title, full_text, pdf_link, pdf_available, court, year, "
        "       court_code, bench "
        "FROM judgments WHERE cnr = ? LIMIT 1",
        (case_id,),
    ).fetchone()
    if row:
        pdf_link = row[3] or ""
        court_code_raw = row[7] or ""   # e.g. "27~1"
        bench = row[8] or ""             # e.g. "newos"
        year = row[6] or ""
        s3_key = ""
        if pdf_link and court_code_raw and bench and year:
            # S3 layout: data/pdf/year={YYYY}/court={N_M}/bench={name}/{filename}.pdf
            # court_code is stored as "N~M" — convert to "N_M" partition format
            import re as _re
            fn_match = _re.search(r'([^/]+\.pdf)', pdf_link, _re.IGNORECASE)
            if fn_match:
                court_part = court_code_raw.replace("~", "_")
                s3_key = f"data/pdf/year={year}/court={court_part}/bench={bench}/{fn_match.group(1)}"
        return {
            "case_id": case_id,
            "title": row[1] or "",
            "full_text": row[2] or "",
            # All judgments rows have a real PDF on S3 — pdf_available column is unreliable.
            # Trust the constructed s3_key (presence implies a complete path).
            "has_pdf": bool(s3_key),
            "pdf_url": f"/pdf/{s3_key}" if s3_key else "",
            "source": "judgments",
        }

    # Try legal_docs table — 8M of these have direct .pdf URLs
    row = conn.execute(
        "SELECT doc_id, title, full_text, url, doc_type "
        "FROM legal_docs WHERE doc_id = ? LIMIT 1",
        (case_id,),
    ).fetchone()
    if row:
        url = (row[3] or "").strip()
        is_pdf = url.lower().endswith(".pdf")
        return {
            "case_id": case_id,
            "title": row[1] or "",
            "full_text": row[2] or "",
            "has_pdf": is_pdf,
            # Direct PDF URLs are external (e.g. indiacode.nic.in) — return as-is
            "pdf_url": url if is_pdf else "",
            "external_url": url if not is_pdf else "",
            "source": "legal_docs",
        }

    # Try statutes table — 100% of 2,333 rows have direct PDF URLs to indiacode.nic.in
    sid = case_id.replace("statute_", "") if case_id.startswith("statute_") else case_id
    row = conn.execute(
        "SELECT id, title, full_text, url FROM statutes WHERE id = ? OR doc_id = ? LIMIT 1",
        (sid, sid),
    ).fetchone()
    if row:
        url = (row[3] or "").strip()
        is_pdf = url.lower().endswith(".pdf")
        return {
            "case_id": case_id,
            "title": row[1] or "",
            "full_text": row[2] or "",
            "has_pdf": is_pdf,
            "pdf_url": url if is_pdf else "",
            "external_url": url if not is_pdf else "",
            "source": "statutes",
        }

    # Try documents table — ingested regulatory documents (SEBI, RBI, …)
    did = case_id.replace("doc_", "") if case_id.startswith("doc_") else case_id
    row = conn.execute(
        "SELECT doc_id, title, full_text, pdf_url, landing_url, summary, source, doc_type "
        "FROM documents WHERE doc_id = ? LIMIT 1",
        (did,),
    ).fetchone()
    if row:
        pdf_url = (row[3] or "").strip()
        return {
            "case_id": case_id,
            "title": row[1] or "",
            "full_text": row[2] or row[5] or "",
            "has_pdf": bool(pdf_url),
            # External PDFs go through our proxy so the iframe can load them
            # from same-origin (avoids X-Frame-Options blocks):
            "pdf_url": f"/doc-pdf/{did}" if pdf_url else "",
            "external_url": row[4] or "",
            "source": f"documents:{row[6]}",
        }

    raise HTTPException(404, "Case not found")


@app.post("/api/assistant/ask")
def api_assistant_ask(body: dict, ls_session: Optional[str] = Cookie(default=None)):
    """Smart legal reasoning assistant.

    Body:
      { question: "...", practice_area: "auto"|"corporate"|"tax"|... }

    Returns:
      { answer_short, answer_long, sub_answers, citations, evidence,
        confidence, caveats, sub_questions, warnings, elapsed_ms }

    Pipeline (see scripts/assistant/legal_reasoner.py):
      Question → Planner LLM → Multi-corpus retrieval → Synthesizer LLM
                → Validation gates → response.
    """
    _require_user(ls_session)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    practice_area = body.get("practice_area", "auto")

    # Lazy import — reasoner pulls in heavy modules
    import sys as _sys
    from pathlib import Path as _Path
    try:
        corpus_root = _Path(__file__).resolve().parent.parent / "india-judgments-corpus"
        if str(corpus_root) not in _sys.path:
            _sys.path.insert(0, str(corpus_root))
        from scripts.assistant.legal_reasoner import answer as reason_answer
    except Exception as exc:
        logger.exception("legal reasoner import failed")
        raise HTTPException(503, f"reasoner unavailable: {exc}")
    try:
        result = reason_answer(question, practice_area=practice_area)
        return result
    except Exception as exc:
        logger.exception("legal reasoner failed")
        raise HTTPException(500, f"reasoner error: {exc}")


@app.get("/api/cases/{case_id}/citations")
def api_case_citations(
    case_id: str,
    direction: str = "cited_by",   # 'cited_by' | 'cites'
    limit: int = 50,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Citator endpoint — returns the cited-by / cites list for a case.

    Response:
        {
          stats: { cited_by_count, cites_count, pagerank, distinguished, overruled, followed },
          edges: [ { case_id, citation_norm, para_no, context, edge_type, title } ]
        }
    """
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)

    # Resolve case_id prefix (strip 'doc_', 'statute_', 'qa_')
    bare = case_id
    for prefix in ("doc_", "statute_", "qa_"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break

    conn = idx.conn
    stats_row = conn.execute(
        "SELECT cited_by_count, cites_count, pagerank, distinguished_count, "
        "overruled_count, followed_count FROM citator_stats WHERE case_id = ?",
        (bare,),
    ).fetchone()
    stats = {
        "cited_by_count": stats_row[0] if stats_row else 0,
        "cites_count":    stats_row[1] if stats_row else 0,
        "pagerank":       stats_row[2] if stats_row else 0.0,
        "distinguished":  stats_row[3] if stats_row else 0,
        "overruled":      stats_row[4] if stats_row else 0,
        "followed":       stats_row[5] if stats_row else 0,
    }

    if direction == "cited_by":
        # Cases that cite THIS case
        rows = conn.execute(
            """SELECT from_case, citation_norm, para_no, context, edge_type
               FROM citation_edges
               WHERE COALESCE(to_case, citation_norm) = ?
               ORDER BY edge_id DESC
               LIMIT ?""",
            (bare, limit),
        ).fetchall()
    else:  # cites
        rows = conn.execute(
            """SELECT to_case, citation_norm, para_no, context, edge_type
               FROM citation_edges
               WHERE from_case = ?
               ORDER BY para_no NULLS LAST, edge_id
               LIMIT ?""",
            (bare, limit),
        ).fetchall()

    edges = []
    for r in rows:
        # Try to enrich the edge with the target/source case title
        target_id = r[0] or ""
        title = ""
        if target_id:
            for sql in (
                "SELECT title FROM judgments WHERE cnr = ? LIMIT 1",
                "SELECT title FROM legal_docs WHERE doc_id = ? LIMIT 1",
                "SELECT title FROM documents WHERE doc_id = ? LIMIT 1",
            ):
                tr = conn.execute(sql, (target_id,)).fetchone()
                if tr and tr[0]:
                    title = tr[0]
                    break
        edges.append({
            "case_id":       target_id,
            "title":         title or r[1] or "",   # citation_norm as fallback
            "citation":      r[1] or "",
            "para_no":       r[2],
            "context":       (r[3] or "")[:300],
            "edge_type":     r[4] or "cites",
        })

    return {"stats": stats, "direction": direction, "edges": edges}


# ── Citator status — "Is this still good law?" ────────────────────────────
# Pure-SQL verdict derived from citator_stats + recent citation_edges.
# Killer Manupatra differentiator: every Indian advocate's first question
# before relying on a case is "has it been overruled?". No free tool answers
# this. We do, in <50ms, across 16.9M judgments.
#
# Status rubric (docs/CORPUS_FIXES_AND_GROUNDING_PIPELINE.md flow #7):
#   OVERRULED  — overruled_count > 0                              (red)
#   QUESTIONED — distinguished_count >= 5 AND followed_count == 0 (amber)
#   GOOD LAW   — followed_count > 0 OR cited_by_count >= 3        (green)
#   UNTESTED   — cited_by_count == 0                              (grey)
#   CITED      — otherwise                                        (neutral)
@app.get("/api/cases/{case_id}/status")
def api_case_status(
    case_id: str,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Return 'Is this still good law?' verdict + supporting edges.

    Response:
      { status, color, label, summary, stats, treating_cases:[...] }
    """
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        raise HTTPException(503)

    bare = case_id
    for prefix in ("doc_", "statute_", "qa_"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break

    conn = idx.conn
    row = conn.execute(
        "SELECT cited_by_count, cites_count, pagerank, distinguished_count, "
        "overruled_count, followed_count, last_cited_year "
        "FROM citator_stats WHERE case_id = ?",
        (bare,),
    ).fetchone()
    if not row:
        cited_by = cites = pr = dist = ovr = fol = 0
        last_year = None
    else:
        cited_by, cites, pr, dist, ovr, fol, last_year = (
            row[0] or 0, row[1] or 0, row[2] or 0.0,
            row[3] or 0, row[4] or 0, row[5] or 0, row[6],
        )

    # Verdict
    if ovr > 0:
        status, color, label = "overruled", "red", "Overruled"
        summary = (
            f"This case has been overruled in {ovr} subsequent decision"
            f"{'s' if ovr != 1 else ''}. Do not rely on it without checking "
            f"the overruling case below."
        )
    elif dist >= 5 and fol == 0:
        status, color, label = "questioned", "amber", "Questioned"
        summary = (
            f"This case has been distinguished {dist} times and not yet "
            f"followed in any later judgment. Treat its ratio with caution."
        )
    elif fol > 0 or cited_by >= 3:
        status, color, label = "good_law", "green", "Good Law"
        summary = (
            f"Followed in {fol} later judgment{'s' if fol != 1 else ''} and "
            f"cited {cited_by} times overall."
            + (f" Last citation: {last_year}." if last_year else "")
        )
    elif cited_by == 0:
        status, color, label = "untested", "grey", "Untested"
        summary = "No subsequent judgment has cited this case in our corpus."
    else:
        status, color, label = "cited", "neutral", "Cited"
        summary = f"Cited {cited_by} times in later judgments."

    # Pull the strongest treating cases (priority: overruled > distinguished > followed)
    treating = []
    edge_order = (
        ("overruled", 5), ("set_aside", 5),
        ("distinguished", 5), ("followed", 5), ("applied", 3), ("cites", 0),
    )
    seen = set()
    for etype, lim in edge_order:
        if lim == 0:
            continue
        for r in conn.execute(
            """SELECT from_case, citation_norm, para_no, context, edge_type
               FROM citation_edges
               WHERE COALESCE(to_case, citation_norm) = ?
                 AND edge_type = ?
               ORDER BY edge_id DESC LIMIT ?""",
            (bare, etype, lim),
        ).fetchall():
            fc = r[0] or ""
            if fc in seen:
                continue
            seen.add(fc)
            # enrich with title
            title = ""
            for sql in (
                "SELECT title FROM judgments WHERE cnr = ? LIMIT 1",
                "SELECT title FROM legal_docs WHERE doc_id = ? LIMIT 1",
            ):
                tr = conn.execute(sql, (fc,)).fetchone()
                if tr and tr[0]:
                    title = tr[0]; break
            treating.append({
                "case_id": fc,
                "title": title or (r[1] or ""),
                "treatment": r[4] or "cites",
                "para_no": r[2],
                "context": (r[3] or "")[:280],
            })

    return {
        "case_id": bare,
        "status": status,
        "color": color,
        "label": label,
        "summary": summary,
        "stats": {
            "cited_by_count": cited_by,
            "cites_count": cites,
            "pagerank": pr,
            "distinguished_count": dist,
            "overruled_count": ovr,
            "followed_count": fol,
            "last_cited_year": last_year,
        },
        "treating_cases": treating,
    }


@app.get("/api/qa/search")
def api_qa_search(
    q: str = "",
    category: Optional[str] = None,
    k: int = 20,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Fast Legal QA search — queries legal_qa_fts directly (2ms vs 8.8s for full search).

    Bypasses the multi-table FTS5Index.search() and queries only legal_qa_fts
    for instant Q&A results. Returns questions, answers, category, acts cited.
    """
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not q:
        return {"hits": [], "total": 0, "engine": "none"}

    if not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"hits": [], "total": 0, "engine": "unavailable"}

    safe_q = re.sub(r"[^\w\s]", " ", q)
    safe_q = re.sub(r"\s+", " ", safe_q).strip()
    if not safe_q:
        return {"hits": [], "total": 0, "engine": "none"}

    try:
        params = [safe_q, k * 2]
        cat_clause = ""
        if category:
            cat_clause = "AND q.category = ?"
            params.insert(1, category)

        rows = idx.conn.execute(
            f"""SELECT q.qa_id, q.question, q.answer, q.category, q.acts_cited,
                       q.context, bm25(legal_qa_fts) AS score
               FROM legal_qa_fts f
               JOIN legal_qa q ON q.rowid = f.rowid
               WHERE legal_qa_fts MATCH ? {cat_clause}
               ORDER BY score
               LIMIT ?""",
            params,
        ).fetchall()

        hits = []
        for r in rows:
            hits.append({
                "qa_id": r[0] or "",
                "question": (r[1] or "")[:300],
                "answer": (r[2] or "")[:1200],
                "category": r[3] or "",
                "acts_cited": r[4] or "",
                "context": (r[5] or "")[:400],
                "score": abs(r[6]) if r[6] else 0,
                "source": "legal_qa_fts5",
            })

        # Get approximate total
        try:
            total = idx.conn.execute("SELECT MAX(rowid) FROM legal_qa").fetchone()[0] or 0
        except Exception:
            total = len(hits)

        return {"hits": hits[:k], "total": total, "engine": "legal_qa_fts5"}
    except Exception as e:
        logger.warning("QA search error: %s", e)
        return {"hits": [], "total": 0, "engine": "error", "error": str(e)}


@app.get("/api/analytics/judge-profile")
def api_judge_profile(
    judge: str = Query(..., min_length=2, description="Judge name (partial match OK)"),
    court_code: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    limit: int = 50,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Judge-level analytics: verdict breakdown, bail rates, case volume over time.

    Queries the 16M+ judgments table for cases decided by the given judge.
    Partial name matching — 'Chandrachud' matches 'Justice D.Y. Chandrachud'.
    """
    _require_user(ls_session)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"judge": judge, "cases": [], "verdict_breakdown": {}, "total": 0}

    try:
        # ── FTS5-driven prefix lookup (drops LOWER(judge) LIKE '%X%' → 3 min
        # full-table scan on 16.8M rows down to ~50 ms). The FTS index
        # already covers `judge`. We use column-scoped MATCH so unrelated
        # mentions in `description`/`full_text` don't pollute results.
        # Fallback to LIKE only if FTS expression fails to parse.
        import re as _re
        # Sanitize for FTS5: keep alphanumerics + space, drop punctuation
        safe_judge = _re.sub(r"[^A-Za-z0-9 ]+", " ", judge).strip()
        match_terms = [t for t in safe_judge.split() if len(t) >= 2]
        if match_terms:
            # column-scoped match: `judge:term1* judge:term2*`
            fts_expr = " ".join(f'judge:{t}*' for t in match_terms)
        else:
            fts_expr = ""

        params: list = []
        clauses: list[str] = []

        if fts_expr:
            clauses.append(
                "j.rowid IN (SELECT rowid FROM judgments_fts "
                "WHERE judgments_fts MATCH ?)"
            )
            params.append(fts_expr)
        else:
            # nothing to FTS — fall back to LIKE (rare)
            clauses.append("LOWER(j.judge) LIKE ?")
            params.append(f"%{judge.lower()}%")

        # We still post-filter by a substring check on `judge` to avoid the
        # FTS prefix match catching different judges that happen to share a
        # prefix token. This filter runs over a tiny FTS-pruned subset.
        clauses.append("LOWER(j.judge) LIKE ?")
        params.append(f"%{judge.lower()}%")

        if court_code:
            clauses.append("j.court_code = ?")
            params.append(court_code)
        if year_from:
            clauses.append("j.year >= ?")
            params.append(year_from)
        if year_to:
            clauses.append("j.year <= ?")
            params.append(year_to)

        where = " AND ".join(clauses)
        params.append(min(limit, 200))

        rows = idx.conn.execute(
            f"""SELECT j.cnr, j.title, j.court, j.year, j.verdict, j.judge,
                       j.date_decided, j.description
               FROM judgments j
               WHERE {where}
               ORDER BY j.year DESC, j.id DESC
               LIMIT ?""",
            params,
        ).fetchall()

        cases = []
        verdict_counts: dict[str, int] = {}
        court_counts: dict[str, int] = {}
        year_counts: dict[int, int] = {}

        for r in rows:
            verdict = (r[4] or "").strip()
            court = (r[2] or "")
            year = r[3]
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            court_counts[court] = court_counts.get(court, 0) + 1
            if year:
                year_counts[int(year)] = year_counts.get(int(year), 0) + 1
            cases.append({
                "cnr": r[0] or "",
                "title": r[1] or (r[7] or "")[:120] or r[0] or "",
                "court": court,
                "year": year,
                "verdict": verdict,
                "judge": r[5] or "",
                "date_decided": r[6] or "",
                "citation": r[0] or "",
            })

        # Count total via FTS-pruned subset (same pattern as the main query).
        count_params: list = []
        count_clauses: list[str] = []
        if fts_expr:
            count_clauses.append(
                "j.rowid IN (SELECT rowid FROM judgments_fts "
                "WHERE judgments_fts MATCH ?)"
            )
            count_params.append(fts_expr)
        count_clauses.append("LOWER(j.judge) LIKE ?")
        count_params.append(f"%{judge.lower()}%")
        if court_code:
            count_clauses.append("j.court_code = ?")
            count_params.append(court_code)
        where_count = " AND ".join(count_clauses)
        total_row = idx.conn.execute(
            f"SELECT COUNT(*) FROM judgments j WHERE {where_count}",
            count_params,
        ).fetchone()
        total = total_row[0] if total_row else len(cases)

        # Find the actual judge names matched (for display) — same FTS prune.
        name_clauses: list[str] = []
        name_params: list = []
        if fts_expr:
            name_clauses.append(
                "rowid IN (SELECT rowid FROM judgments_fts "
                "WHERE judgments_fts MATCH ?)"
            )
            name_params.append(fts_expr)
        name_clauses.append("LOWER(judge) LIKE ?")
        name_params.append(f"%{judge.lower()}%")
        name_where = " AND ".join(name_clauses)
        name_rows = idx.conn.execute(
            f"SELECT DISTINCT judge FROM judgments WHERE {name_where} LIMIT 10",
            name_params,
        ).fetchall()
        matched_names = [r[0] for r in name_rows if r[0]]

        # ── C2 extended analytics: lean_by_topic, reversal_rate, decision_speed,
        # bench_mates, outcome_breakdown. All best-effort — degrade gracefully
        # if the underlying enrichment tables are empty.
        ext: dict = {
            "outcome_breakdown": {},
            "lean_by_topic":     {},
            "reversal_rate":     None,
            "decision_speed":    None,
            "bench_mates":       [],
            "peer_percentile":   None,
        }
        try:
            judge_norm_like = f"%{judge.lower()}%"

            # Build a small CTE-friendly subquery that pre-prunes via FTS
            # (when fts_expr is non-empty) so each agg query is fast.
            if fts_expr:
                fts_prune_sql = (
                    "j.rowid IN (SELECT rowid FROM judgments_fts "
                    "WHERE judgments_fts MATCH ?) AND LOWER(j.judge) LIKE ?"
                )
                fts_prune_params = [fts_expr, judge_norm_like]
            else:
                fts_prune_sql = "LOWER(j.judge) LIKE ?"
                fts_prune_params = [judge_norm_like]

            # Outcome breakdown
            for outcome, n in idx.conn.execute(
                f"""SELECT j.outcome, COUNT(*)
                    FROM judgments j
                    WHERE {fts_prune_sql} AND j.outcome IS NOT NULL
                    GROUP BY j.outcome""",
                fts_prune_params,
            ):
                ext["outcome_breakdown"][outcome or "unknown"] = n

            # Lean by topic
            for topic, allowed_n, total_n in idx.conn.execute(
                f"""SELECT ct.topic,
                           SUM(CASE WHEN j.outcome='allowed' THEN 1 ELSE 0 END),
                           COUNT(*)
                    FROM judgments j
                    JOIN case_topics ct ON ct.case_id = j.cnr AND ct.corpus='judgments'
                    WHERE {fts_prune_sql} AND j.outcome IS NOT NULL
                    GROUP BY ct.topic
                    HAVING COUNT(*) >= 5""",
                fts_prune_params,
            ):
                ext["lean_by_topic"][topic] = {
                    "allowed_pct": round(allowed_n / total_n, 3) if total_n else None,
                    "total":       total_n,
                }

            # Reversal rate
            rev_row = idx.conn.execute(
                f"""SELECT
                       SUM(CASE WHEN r.parent_case_id IS NOT NULL THEN 1 ELSE 0 END),
                       COUNT(*)
                    FROM judgments j
                    LEFT JOIN reversals r ON r.parent_case_id = j.cnr
                    WHERE {fts_prune_sql}""",
                fts_prune_params,
            ).fetchone()
            if rev_row and rev_row[1]:
                ext["reversal_rate"] = round((rev_row[0] or 0) / rev_row[1], 4)

            # Decision speed (avg duration in days) — needs `j.` prefix in fts_prune
            speed_row = idx.conn.execute(
                f"""SELECT AVG(duration_days) FROM judgments j
                    WHERE {fts_prune_sql}
                      AND duration_days IS NOT NULL
                      AND duration_days > 0 AND duration_days < 10000""",
                fts_prune_params,
            ).fetchone()
            if speed_row and speed_row[0]:
                ext["decision_speed"] = round(speed_row[0], 1)

            # Bench mates: most-frequent co-judges
            for mate, n in idx.conn.execute(
                """SELECT mate.judge_name_norm, COUNT(*) AS n
                   FROM case_judges me
                   JOIN case_judges mate
                     ON mate.case_id = me.case_id
                    AND mate.corpus  = me.corpus
                    AND mate.judge_name_norm != me.judge_name_norm
                   WHERE me.judge_name_norm LIKE ?
                   GROUP BY mate.judge_name_norm
                   ORDER BY n DESC
                   LIMIT 10""",
                [judge_norm_like],
            ):
                ext["bench_mates"].append({"judge_name_norm": mate, "count": n})
        except Exception as exc:
            logger.debug("judge ext analytics partial failure: %s", exc)

        return {
            "query": judge,
            "matched_names": matched_names,
            "total": total,
            "cases": cases,
            "verdict_breakdown": verdict_counts,
            "court_breakdown": court_counts,
            "yearly_volume": dict(sorted(year_counts.items())),
            **ext,
        }
    except Exception as e:
        logger.warning("Judge profile error: %s", e)
        return {"judge": judge, "cases": [], "verdict_breakdown": {}, "total": 0, "error": str(e)}


@app.post("/api/analytics/predict-outcome")
def api_predict_outcome(body: dict, ls_session: Optional[str] = Cookie(default=None)):
    """Predictive: outcome (allowed / dismissed / partly_allowed) probability.

    Body:
      {
        court_code: "27~1",
        topic:      "criminal",
        year:       2024,
        bench_size: 2,
        petitioner_advocate_winrate: 0.62,
        respondent_advocate_winrate: 0.50,
        judge_lean_for_topic:        0.55,
        is_listed_company:           1
      }

    Returns:
      { p_allowed, p_dismissed, p_partly_allowed, predicted_class,
        confidence_band, valid_accuracy, model_version, fallback_used }

    Always wraps response with the disclaimer that predictions reflect
    historical patterns and are NOT legal advice.
    """
    _require_user(ls_session)
    import sys as _sys
    from pathlib import Path as _Path
    corpus_root = _Path(__file__).resolve().parent.parent / "india-judgments-corpus"
    if str(corpus_root) not in _sys.path:
        _sys.path.insert(0, str(corpus_root))
    try:
        from scripts.ml.inference import predict_outcome, model_status
    except Exception as exc:
        logger.exception("inference import failed")
        raise HTTPException(503, f"predictor unavailable: {exc}")

    result = predict_outcome(body or {})
    result["status"] = model_status()
    result["disclaimer"] = (
        "These probabilities reflect statistical patterns from public "
        "judgments. They are not legal advice and must not be relied on "
        "as a forecast of the outcome of any specific live case."
    )
    return result


@app.get("/api/analytics/predict-outcome/status")
def api_predict_outcome_status(ls_session: Optional[str] = Cookie(default=None)):
    """Lightweight check used by the UI to decide whether to render the
    predictive panel or show a 'model training' placeholder."""
    _require_user(ls_session)
    import sys as _sys
    from pathlib import Path as _Path
    corpus_root = _Path(__file__).resolve().parent.parent / "india-judgments-corpus"
    if str(corpus_root) not in _sys.path:
        _sys.path.insert(0, str(corpus_root))
    try:
        from scripts.ml.inference import model_status
    except Exception:
        return {"trained": False, "error": "module unavailable"}
    return model_status()


@app.get("/api/analytics/lawyer-profile")
def api_lawyer_profile(
    name: str = Query(..., min_length=3,
                      description="Advocate name (partial / fuzzy match OK)"),
    court: Optional[str] = None,
    topic: Optional[str] = None,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Lawyer (advocate) analytics dashboard.

    Backed by `case_advocates` (populated by scripts/enrich/advocate_ner.py).
    Returns: matched_names, win_rate, courts, judges_appeared,
             growth_trajectory, recent_cases.

    'name' is matched against `advocate_name_norm` with LIKE so
    'salve' matches 'harish salve' / 'h salve'.
    """
    _require_user(ls_session)
    # Lazy import — pulls in the corpus DB module only on first hit
    import sys as _sys
    from pathlib import Path as _Path
    corpus_root = _Path(__file__).resolve().parent.parent / "india-judgments-corpus"
    if str(corpus_root) not in _sys.path:
        _sys.path.insert(0, str(corpus_root))
    try:
        from scripts.lawyer_analytics import lawyer_profile, lawyer_win_rate
    except Exception as exc:
        logger.exception("lawyer_analytics import failed")
        raise HTTPException(503, f"lawyer analytics unavailable: {exc}")

    try:
        if court or topic:
            # Filtered view — return win-rate only with the filters applied
            wr = lawyer_win_rate(name, court=court, topic=topic)
            return {"query": name, "filtered": True, "win_rate": wr}
        return lawyer_profile(name)
    except Exception as exc:
        logger.exception("lawyer profile error: %s", exc)
        return {"query": name, "error": str(exc), "matched_names": [],
                "win_rate": {"total_cases": 0}, "courts": [],
                "judges_appeared": [], "growth_trajectory": [],
                "recent_cases": []}


@app.get("/api/analytics/corpus-stats")
def api_corpus_stats(ls_session: Optional[str] = Cookie(default=None)):
    """Corpus overview: total records, courts, year range."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None:
        return {}
    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
        return idx.stats()
    return {"total": len(idx.docs) if hasattr(idx, 'docs') else 0}


@app.get("/api/languages")
def api_languages():
    """Available languages for chat responses."""
    from brief_service import LANGUAGES
    return {"languages": [
        {"code": k, "label": v.split("(")[0].strip(),
         "native": v.split("(")[1].rstrip(")") if "(" in v else v}
        for k, v in LANGUAGES.items()
    ]}


# ─────────────────────────────────────────────────────────────────────────
# WEB SIGNALS — live legal news & developments
# ─────────────────────────────────────────────────────────────────────────

@app.get("/api/news")
def api_news():
    """Latest legal news from trusted Indian law sources."""
    signals = web_signals.fetch_legal_news(max_items=20)
    return {"signals": [s.to_dict() for s in signals], "sources": web_signals.available_sources()}


@app.get("/api/news/search")
def api_news_search(q: str = Query(default="", description="Search query")):
    """Search legal news for a specific topic."""
    if not q.strip():
        return api_news()
    signals = web_signals.search_web_signals(q.strip(), max_items=12)
    return {"query": q, "signals": [s.to_dict() for s in signals]}


@app.get("/api/news/sources")
def api_news_sources():
    """List available news signal sources."""
    return {"sources": web_signals.available_sources()}


# ─────────────────────────────────────────────────────────────────────────
# LEGAL DOCUMENT EDITOR  — Google-Docs-style drafting with AI + citations
# ─────────────────────────────────────────────────────────────────────────

class DocCreateBody(BaseModel):
    title: str = "Untitled Document"
    doc_type: str = "general"
    content: str = ""

class DocSaveBody(BaseModel):
    title: Optional[str] = None
    content: str
    citations: str = "[]"

class AiCompleteBody(BaseModel):
    content: str
    cursor_text: str = ""
    doc_type: str = ""

class AiImproveBody(BaseModel):
    selected_text: str
    doc_type: str = ""

class AiWriteSectionBody(BaseModel):
    instruction: str
    doc_type: str = ""
    context: str = ""
    prefer: Optional[str] = None   # "anthropic" / "gemini" / "groq" / "cloudflare"

class AiSuggestCasesBody(BaseModel):
    argument: str


@app.get("/api/editor/doc-types")
def api_editor_doc_types():
    """List all legal document types with templates."""
    return {"doc_types": doc_editor.list_doc_types()}


@app.get("/api/editor/template/{doc_type}")
def api_editor_template(doc_type: str):
    """Get starter template for a document type."""
    tmpl = doc_editor.get_template(doc_type)
    if not tmpl:
        raise HTTPException(404, f"Unknown doc_type: {doc_type}")
    return {"doc_type": doc_type, "template": tmpl}


@app.get("/api/editor/docs")
def api_editor_list(ls_session: Optional[str] = Cookie(default=None)):
    """List user's saved documents."""
    user = _require_user(ls_session)
    return {"documents": auth.doc_list(user["id"])}


@app.post("/api/editor/docs")
def api_editor_create(body: DocCreateBody, ls_session: Optional[str] = Cookie(default=None)):
    """Create a new document (optionally with template content)."""
    import time as _time
    user = _require_user(ls_session)
    content = body.content
    if not content and body.doc_type != "general":
        content = doc_editor.get_template(body.doc_type)
    doc_id = auth.doc_create(user["id"], body.title, body.doc_type, content)
    now = int(_time.time())
    return {"doc": {"id": doc_id, "title": body.title, "doc_type": body.doc_type,
                    "content": content, "word_count": len(content.split()), "updated_at": now}}


@app.get("/api/editor/docs/{doc_id}")
def api_editor_get(doc_id: int, ls_session: Optional[str] = Cookie(default=None)):
    """Get a document by ID."""
    user = _require_user(ls_session)
    doc = auth.doc_get(doc_id, user["id"])
    if not doc:
        raise HTTPException(404, "Document not found")
    return {"doc": doc}


@app.put("/api/editor/docs/{doc_id}")
def api_editor_save(doc_id: int, body: DocSaveBody, ls_session: Optional[str] = Cookie(default=None)):
    """Save/auto-save a document."""
    import time as _time
    user = _require_user(ls_session)
    # Fetch existing title if not provided
    title = body.title
    if title is None:
        existing = auth.doc_get(doc_id, user["id"])
        title = existing["title"] if existing else "Untitled Document"
    ok = auth.doc_save(doc_id, user["id"], title, body.content, body.citations)
    if not ok:
        raise HTTPException(404, "Document not found")
    wc = len(body.content.split())
    now = int(_time.time())
    return {"doc": {"id": doc_id, "title": title, "word_count": wc, "updated_at": now}}


@app.delete("/api/editor/docs/{doc_id}")
def api_editor_delete(doc_id: int, ls_session: Optional[str] = Cookie(default=None)):
    """Delete a document."""
    user = _require_user(ls_session)
    ok = auth.doc_delete(doc_id, user["id"])
    if not ok:
        raise HTTPException(404, "Document not found")
    return {"deleted": True}


@app.get("/api/editor/docs/{doc_id}/versions")
def api_editor_versions(doc_id: int, ls_session: Optional[str] = Cookie(default=None)):
    """Get version history for a document."""
    user = _require_user(ls_session)
    versions = auth.doc_versions(doc_id, user["id"])
    return {"versions": versions}


@app.post("/api/editor/docs/{doc_id}/restore/{version_id}")
def api_editor_restore(doc_id: int, version_id: int, ls_session: Optional[str] = Cookie(default=None)):
    """Restore a previous version."""
    user = _require_user(ls_session)
    content = auth.doc_restore_version(doc_id, version_id, user["id"])
    if content is None:
        raise HTTPException(404, "Version not found")
    return {"content": content}


@app.post("/api/editor/ai/complete")
def api_editor_ai_complete(body: AiCompleteBody, ls_session: Optional[str] = Cookie(default=None)):
    """AI: continue writing from cursor position."""
    _require_user(ls_session)
    completion = doc_editor.ai_complete(body.content, body.doc_type, body.cursor_text)
    return {"completion": completion}


@app.post("/api/editor/ai/improve")
def api_editor_ai_improve(body: AiImproveBody, ls_session: Optional[str] = Cookie(default=None)):
    """AI: improve selected text."""
    _require_user(ls_session)
    improved = doc_editor.ai_improve(body.selected_text, body.doc_type)
    return {"improved": improved}


@app.post("/api/editor/ai/write-section")
def api_editor_ai_write(body: AiWriteSectionBody, ls_session: Optional[str] = Cookie(default=None)):
    """AI: write a complete section based on instruction."""
    _require_user(ls_session)
    text = doc_editor.ai_write_section(body.instruction, body.doc_type,
                                        body.context, prefer=body.prefer)
    return {"text": text}


# ── Workflow output validator ──────────────────────────────────────────────
#
# Workflow nodes do extraction / classification / drafting — not legal
# research — so the full 6-gate research validator (which requires [n]
# citations) would false-flag every output. This endpoint runs the gates
# that DO apply to transformative tasks:
#
#   • banned_phrases — no "as an AI", "I think", hedging
#   • fabricated_cases — every case name in the output must appear in the
#     provided context (input doc / FIR / contract paste)
#   • statute_anchor — for compliance / claim-challenger style nodes,
#     every Section reference must be supported by the context or be a
#     real Indian statute we've indexed
#   • format_check — if the recipe declared an expected format (table /
#     json / bullets), verify the output matches it
#   • grounding_in_context — % of substantive sentences whose noun phrases
#     are traceable to the input context (heuristic, not strict)
#
# Returns a per-gate map plus a single boolean `passed`. Workflows show a
# ✓ or ⚠ badge per node; clicking the badge expands which gate failed.

class WorkflowValidateBody(BaseModel):
    output: str
    context: str = ""
    expected_format: str = "free"   # "free" | "table" | "json" | "bullets" | "ranked"
    node_kind: str = "ai"           # which node produced the output
    sources_used: int = 0           # number of [E*] grounding sources passed to the LLM


@app.post("/api/workflows/validate")
def api_workflow_validate(
    body: WorkflowValidateBody,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Run workflow-aware gates on an AI node's output. See module-docstring above."""
    _require_user(ls_session)
    out = body.output or ""
    ctx = (body.context or "").lower()
    gates: dict[str, bool] = {}
    reasons: list[str] = []

    # ── Gate 1: banned phrases (G3 from the research validator)
    bp = _BANNED_PHRASE_RE.search(out)
    gates["banned_phrases"] = bp is None
    if bp:
        reasons.append(f"banned phrase: '{bp.group(0)}'")

    # ── Gate 2: fabricated case names (G5 from the research validator)
    # Every case name in the output must appear in the input context.
    stripped = _strip_md_for_validate(out)
    fabricated: list[str] = []
    for cm in _CASE_NAME_RE.finditer(stripped):
        a = cm.group(1).lower()
        b = cm.group(2).lower()
        if a not in ctx or b not in ctx:
            # Allow if it matches a known landmark case we've seen many times.
            landmark = {
                "kesavananda bharati", "maneka gandhi", "vishaka", "puttaswamy",
                "shreya singhal", "joseph shine", "navtej johar", "sanjay chandra",
                "satender kumar antil", "niranjan shankar golikari",
                "hakam singh", "swastik gases", "balco employees union",
            }
            joined = f"{a} {b}".lower()
            if not any(L in joined for L in landmark):
                fabricated.append(f"{cm.group(1)} v. {cm.group(2)}")
    gates["no_fabricated_cases"] = not fabricated
    if fabricated:
        reasons.append(f"case names not in context: {fabricated[:3]}")

    # ── Gate 3: statute anchor presence (for compliance / defence outputs)
    # If the node label mentioned compliance / defence / risk / statute, we
    # expect at least one Section reference. Otherwise neutral.
    needs_anchor = body.node_kind in ("compliance",) or any(
        k in (out + body.expected_format).lower()
        for k in ("statutory", "anchor", "compliance", "defence", "violation")
    )
    has_section_ref = bool(_SECTION_REF_RE.search(out))
    if needs_anchor:
        gates["statute_anchor"] = has_section_ref
        if not has_section_ref:
            reasons.append("output discusses statutory issues but no Section reference present")
    else:
        gates["statute_anchor"] = True

    # ── Gate 4: format check (the LLM actually produced what was asked)
    expected = (body.expected_format or "free").lower()
    if expected == "table":
        # Markdown table = at least one line starting with | and a divider row
        has_table = bool(re.search(r"^\|.+\|\s*$\n^\|[\s\-:|]+\|\s*$", out, re.MULTILINE))
        gates["format_table"] = has_table
        if not has_table:
            reasons.append("expected a markdown table but none found")
    elif expected == "json":
        try:
            import json as _json
            blob = out.strip()
            # Try to extract from ```json fences
            m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", blob, re.DOTALL)
            if m: blob = m.group(1)
            _json.loads(blob)
            gates["format_json"] = True
        except Exception as e:
            gates["format_json"] = False
            reasons.append(f"expected JSON but parse failed: {str(e)[:80]}")
    elif expected == "bullets":
        bullet_lines = sum(1 for ln in out.splitlines() if re.match(r"^\s*[-*]\s+", ln))
        gates["format_bullets"] = bullet_lines >= 3
        if bullet_lines < 3:
            reasons.append(f"expected ≥3 bullets, got {bullet_lines}")
    elif expected == "ranked":
        num_lines = sum(1 for ln in out.splitlines() if re.match(r"^\s*\d+\.\s+", ln))
        gates["format_ranked"] = num_lines >= 3
        if num_lines < 3:
            reasons.append(f"expected ≥3 ranked items, got {num_lines}")
    # else format == "free" — no format gate

    # ── Gate: cite_resolves — if the LLM emitted [E1] [E2] markers, verify
    # each marker number is within the count of grounding sources we
    # actually passed to it. Catches the "[E7] used but only 3 sources
    # provided" hallucination.
    if body.sources_used > 0:
        cite_markers = [int(m.group(1)) for m in re.finditer(r"\[E(\d+)\]", out)]
        if cite_markers:
            bad_markers = [n for n in cite_markers if n < 1 or n > body.sources_used]
            gates["cite_resolves"] = not bad_markers
            if bad_markers:
                reasons.append(f"citation markers out of range (sources={body.sources_used}): {sorted(set(bad_markers))}")
        else:
            # Sources were provided but the LLM ignored them → ungrounded
            gates["cite_resolves"] = False
            reasons.append(f"{body.sources_used} grounding sources provided but no [E*] markers in output")

    # ── Gate 5: grounding in context (heuristic — at least one substantive
    # noun phrase from the output must appear in the input context).
    if ctx and len(out) > 200:
        # Pick capitalized multi-word phrases from output (proper nouns / acts).
        phrases = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\b", out)
        if phrases:
            cnt = sum(1 for p in phrases if p.lower() in ctx)
            ratio = cnt / max(len(phrases), 1)
            gates["grounding_in_context"] = ratio >= 0.30
            if ratio < 0.30:
                reasons.append(f"only {ratio:.0%} of proper-noun phrases appear in context")
        else:
            gates["grounding_in_context"] = True
    else:
        gates["grounding_in_context"] = True

    passed_count = sum(1 for v in gates.values() if v)
    total = len(gates)
    confidence = passed_count / total if total else 1.0
    # Hard-fail if banned_phrases or no_fabricated_cases fail (these matter
    # for legal correctness). Format gates are soft-fail.
    hard_pass = gates.get("banned_phrases", True) and gates.get("no_fabricated_cases", True)
    passed = hard_pass and passed_count == total

    return {
        "passed":      passed,
        "confidence":  round(confidence, 2),
        "gates":       gates,
        "reasons":     reasons,
        "passed_count": passed_count,
        "total":       total,
    }


@app.post("/api/editor/ai/suggest-cases")
def api_editor_suggest_cases(
    body: AiSuggestCasesBody,
    ls_session: Optional[str] = Cookie(default=None),
):
    """AI: suggest what case law to search for a given argument. Then search."""
    _require_user(ls_session)
    suggestion = doc_editor.ai_suggest_case_search(body.argument)
    # Actually run the search
    hits: list[dict] = []
    idx = _ensure_bm25()
    if idx is not None and suggestion.get("search_query"):
        try:
            if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
                hits = idx.search(suggestion["search_query"], limit=8)
        except Exception as e:
            logger.warning("editor case search failed: %s", e)
    return {
        "search_query": suggestion.get("search_query", ""),
        "explanation": suggestion.get("explanation", ""),
        "cases": hits[:8],
    }


@app.post("/api/editor/ai/insert-citation")
def api_editor_insert_citation(
    body: dict,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Format a case as a citation string for insertion."""
    _require_user(ls_session)
    citation_str = doc_editor.ai_insert_citation(body)
    return {"citation": citation_str}


@app.get("/api/editor/clauses")
def api_editor_clauses():
    """List all ready-made legal clauses for quick insertion."""
    return {"clauses": doc_editor.list_legal_clauses()}


@app.get("/api/editor/clauses/{clause_id}")
def api_editor_clause(clause_id: str):
    """Get a specific legal clause by ID."""
    clause = doc_editor.get_legal_clause(clause_id)
    if not clause:
        raise HTTPException(404, "Clause not found")
    return clause


@app.get("/api/analytics/court-efficiency")
def api_court_efficiency(ls_session: Optional[str] = Cookie(default=None)):
    """Court efficiency: avg days to dispose, disposal rate per court."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {"courts": []}
    return {"courts": idx.court_efficiency()}


@app.get("/api/analytics/bail-intelligence")
def api_bail_intelligence(
    court: Optional[str] = None,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Bail grant/rejection rates by court."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {}
    return idx.bail_intelligence(court_code=court)


@app.get("/api/analytics/verdict-patterns")
def api_verdict_patterns(
    court: Optional[str] = None,
    year_from: Optional[int] = None,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Verdict distribution patterns."""
    if not ls_session:
        raise HTTPException(401)
    if not auth.verify_session_token(ls_session):
        raise HTTPException(401)
    idx = _ensure_bm25()
    if idx is None or not (_FTS5_AVAILABLE and isinstance(idx, FTS5Index)):
        return {}
    return idx.verdict_patterns(court_code=court, year_from=year_from)


@app.get("/health")
def health():
    """Liveness + BM25 readiness. NyayaSathi pings this before enabling
    LexSearch-backed grounding on a call."""
    idx = _bm25_index
    return {
        "status": "ok",
        "retrieval_available": _RETRIEVAL_AVAILABLE,
        "bm25_enabled": BM25_ENABLED,
        "bm25_loaded": idx is not None,
        "bm25_loading": _bm25_loading,
        "bm25_doc_count": (len(idx) if idx is not None else 0),
        "bm25_loaded_at": _bm25_loaded_at,
        "bm25_load_error": _bm25_load_error,
    }


@app.get("/sc-pdf/{year}/{pdf_name}")
def serve_sc_pdf(year: int, pdf_name: str, download: bool = False):
    """Extract a single PDF from the Supreme Court tar archive on S3.

    Failure modes are normalised to JSON 404 so the UI can show a clean
    "PDF unavailable" state instead of a 500 spinner that never resolves.
    """
    fs = get_fs()
    tar_path = f"{SC_S3}/data/tar/year={year}/english/english.tar"

    try:
        with fs.open(tar_path, "rb") as f:
            with tarfile.open(fileobj=f, mode="r|") as tf:
                for member in tf:
                    if member.name == pdf_name or member.name.endswith(pdf_name):
                        extracted = tf.extractfile(member)
                        if extracted:
                            pdf_bytes = extracted.read()
                            disp = f'attachment; filename="{pdf_name}"' if download else f'inline; filename="{pdf_name}"'
                            return Response(
                                content=pdf_bytes,
                                media_type="application/pdf",
                                headers={"Content-Disposition": disp},
                            )
        raise HTTPException(404, "PDF not available in the archive.")
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(404, "Archive for that year is not available.")
    except Exception as e:
        logger.error("SC PDF extraction error for %s/%s: %s", year, pdf_name, e)
        raise HTTPException(404, "PDF could not be extracted.")


@app.head("/sc-pdf/{year}/{pdf_name}")
def probe_sc_pdf(year: int, pdf_name: str):
    """HEAD probe for SC PDFs. Confirms the tar archive for the year exists;
    we don't scan for the filename (would mean reading the whole tar) — the
    UI treats a 200 here as "try it, might still 404 on GET" but at least
    blocks links when the year's archive is missing entirely."""
    fs = get_fs()
    tar_path = f"{SC_S3}/data/tar/year={year}/english/english.tar"
    try:
        if fs.exists(tar_path):
            return Response(status_code=200)
    except Exception as e:
        logger.debug("SC archive probe failed for %s: %s", year, e)
    raise HTTPException(404, "SC archive not available for that year.")


# ---------------------------------------------------------------------------
# Phase 2 — access requests, login, Brief chat, admin
# ---------------------------------------------------------------------------

ADMIN_TOKEN = os.environ.get("LEXSEARCH_ADMIN_TOKEN", "")


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For if present (behind Render / Nginx), else peer IP.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _require_admin(authorization: Optional[str]) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(403, "admin disabled (set LEXSEARCH_ADMIN_TOKEN)")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(401, "bad token")


def _require_user(session: Optional[str]) -> dict:
    """Dependency-style helper. Returns the authenticated user row or 401.

    Phase 2 (docs/AUTH_DESIGN.md §15) — cookie-format coexistence:
      - Legacy HMAC-signed `uid.exp.sig` (dots present) → legacy_auth path
        with access_codes.id as user_id.
      - New opaque session id (no dots) → auth.session_store + auth.users
        with users.id as user_id.

    The unconditional demo-user fallback that previously fired on missing
    cookie is gone — it leaked demo identity to any unauthenticated caller.
    The replacement is an explicit AUTH_DEV_FALLBACK_EMAIL env var, gated to
    non-production environments.
    """
    if session:
        if "." in session:
            # Legacy HMAC-signed cookie. Verify against access_codes table.
            uid = auth.verify_session_token(session)
            if uid is not None:
                user = auth.get_user(uid)
                if user:
                    return user
        else:
            # New opaque session id. Phase 2 SessionStore lookup.
            try:
                from auth.deps import get_session_store
                from auth import users as _users
                store = get_session_store()
                record = store.get(session)
                if record is not None:
                    u = _users.get_by_id(record.user_id)
                    if u is not None and u.is_active:
                        # Adapt to the legacy "dict with id/email/name" shape so
                        # downstream callers (threads, vault, editor) keep working.
                        return {"id": u.id, "email": u.email, "name": u.display_name}
            except Exception as e:
                logger.warning("Phase 2 session lookup failed: %s", e)

    fallback_email = os.environ.get("AUTH_DEV_FALLBACK_EMAIL", "")
    if fallback_email and os.environ.get("APP_ENV", "development") != "production":
        with auth.db() as c:
            row = c.execute(
                "SELECT id, email, name FROM access_codes WHERE LOWER(email) = ? "
                "AND revoked_at IS NULL",
                (fallback_email.strip().lower(),),
            ).fetchone()
        if row:
            return dict(row)

    raise HTTPException(401, "not_signed_in")


# ── public: access request ─────────────────────────────────────────────────

class AccessRequestBody(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., min_length=5, max_length=160)
    role: str = Field(default="", max_length=80)
    firm: str = Field(default="", max_length=160)
    bar_no: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=1000)


@app.post("/api/access-request")
def api_access_request(body: AccessRequestBody, request: Request):
    ip = _client_ip(request)
    if not auth.rate_limit("access_request", ip, max_hits=5, window_s=3600):
        raise HTTPException(429, "Too many requests from this IP. Try later.")
    # Minimal email shape check — do not run a full RFC 5321.
    if "@" not in body.email or "." not in body.email.split("@")[-1]:
        raise HTTPException(400, "Please provide a valid email address.")
    rid = auth.create_access_request(
        body.name, body.email, body.role, body.firm, body.bar_no, body.note, ip
    )
    return {"ok": True, "request_id": rid}


# ── login / logout ────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    code: str = Field(..., min_length=6, max_length=32)


@app.post("/api/login")
def api_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    if not auth.rate_limit("login", ip, max_hits=10, window_s=600):
        raise HTTPException(429, "Too many login attempts. Slow down.")
    user = auth.validate_code(body.code)
    if not user:
        raise HTTPException(401, "Invalid or revoked access code.")
    token = auth.make_session_token(user["id"])
    resp = JSONResponse({"ok": True, "user": {"email": user["email"], "name": user["name"]}})
    is_https = request.url.scheme == "https"
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_S,
        httponly=True,
        samesite="none" if is_https else "lax",
        secure=is_https,
        path="/",
    )
    return resp


@app.get("/api/logout")
def api_logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def api_me(ls_session: Optional[str] = Cookie(default=None)):
    try:
        user = _require_user(ls_session)
    except HTTPException:
        return JSONResponse({"authenticated": False}, status_code=200)
    return {"authenticated": True, "user": {"email": user["email"], "name": user["name"]}}


# ── brief: threads + chat ─────────────────────────────────────────────────

class NewThreadBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)


@app.get("/api/brief/threads")
def api_list_threads(ls_session: Optional[str] = Cookie(default=None)):
    user = _require_user(ls_session)
    threads = auth.list_user_threads(user["id"], limit=30)
    return {
        "user": {"email": user["email"], "name": user["name"]},
        "threads": threads,
    }


@app.post("/api/brief/threads")
def api_create_thread(
    body: NewThreadBody = NewThreadBody(),
    ls_session: Optional[str] = Cookie(default=None),
):
    user = _require_user(ls_session)
    tid = auth.create_thread(user["id"], title=(body.title or "New matter"))
    return {
        "thread": {
            "id": tid,
            "title": body.title or "New matter",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    }


@app.get("/api/brief/threads/{thread_id}")
def api_get_thread(
    thread_id: int,
    ls_session: Optional[str] = Cookie(default=None),
):
    user = _require_user(ls_session)
    msgs = auth.get_thread_messages(thread_id, user["id"])
    if msgs is None:
        raise HTTPException(404, "Thread not found.")
    return {"thread_id": thread_id, "messages": msgs}


class ChatBody(BaseModel):
    thread_id: int = Field(..., ge=1)
    question: str = Field(..., min_length=2, max_length=4000)
    lang: str = Field(default="en", max_length=5)
    language: Optional[str] = Field(default=None, max_length=5)  # frontend sends this
    jurisdiction: Optional[str] = None
    sources: Optional[list[str]] = None
    prefer: Optional[str] = None


@app.post("/api/brief/chat")
def api_brief_chat(
    body: ChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("chat", ip, max_hits=30, window_s=60):
        raise HTTPException(429, "You're asking very quickly. Breathe.")

    # Ownership check + history pull in one shot
    history = auth.get_thread_messages(body.thread_id, user["id"])
    if history is None:
        raise HTTPException(404, "Thread not found.")

    # ── Input guardrails: length / injection / scope / PII redaction
    guard = input_guards.check(body.question, history_len=len(history or []))
    if not guard.allow:
        # Persist the user turn so the refusal is visible in the thread,
        # then return a structured refusal envelope (no LLM call, no retrieval).
        auth.append_message(body.thread_id, "user", body.question, None)
        auth.append_message(
            body.thread_id,
            "assistant",
            guard.refusal_message,
            serialize_citations([]),
        )
        return JSONResponse({
            "answer_markdown": guard.refusal_message,
            "citations": [],
            "llm": {"provider": "guard", "model": "input_guards", "latency_ms": 0, "fallback_chain": []},
            "validation": {"passed": False, "confidence": 0.0, "reasons": [guard.reason]},
            "refused": True,
            "guard": guard.to_dict(),
        })

    safe_question = guard.redacted_question or body.question
    lang = body.language or body.lang or "en"

    # ── Smart routing: only retrieve cases if the user is actually asking
    # for case search / legal research. Conversational queries (greetings,
    # general questions) get a direct LLM response without corpus search.
    if _needs_case_retrieval(safe_question):
        # Retrieve grounding hits — HybridSearchEngine spans all 6 corpora
        # (83M rows: judgments + legal_docs + pipeline_docs + statutes +
        # legal_qa + documents) with parallel BM25 fan-out + FAISS semantic
        # merge. Falls back to legacy FTS5Index.search() if the new engine
        # isn't importable.
        hits: list[dict] = []
        try:
            from routes_search import get_engine as _get_search_engine
            from scripts.search.engine import SearchFilters as _SearchFilters
            engine = _get_search_engine()
            raw = engine.search(safe_question, filters=_SearchFilters(),
                                 mode="hybrid", limit=12)
            for r in raw:
                title = (getattr(r, "title", "") or "").strip()
                pet = (getattr(r, "petitioner", "") or "").strip()
                res = (getattr(r, "respondent", "") or "").strip()
                if not title and (pet or res):
                    title = f"{pet} versus {res}".strip(" v.")
                hits.append({
                    "case_id":     getattr(r, "doc_id", "") or "",
                    "title":       title or "(untitled)",
                    "court":       getattr(r, "court", "") or "",
                    "year":        getattr(r, "year", None),
                    "citation":    getattr(r, "citation", None) or getattr(r, "doc_id", ""),
                    "excerpt":     (getattr(r, "summary", "") or "")[:600],
                    "verdict":     getattr(r, "verdict", "") or "",
                    "judge":       getattr(r, "judge", "") or "",
                    "source":      f"{getattr(r, 'source_table', 'unknown')}_engine",
                    "source_table":getattr(r, "source_table", ""),
                    "acts_cited":  getattr(r, "acts_cited", "") or "",
                    "score":       float(getattr(r, "score", 0.0)),
                    "doc_type":    getattr(r, "doc_type", "") or "",
                })
            logger.info("HybridSearchEngine retrieve for '%s': %d hits (across 6 corpora)",
                        safe_question[:50], len(hits))
        except Exception as e:
            logger.warning("HybridSearchEngine retrieval failed, falling back to legacy FTS5: %s", e)
            idx = _ensure_bm25()
            if idx is not None:
                try:
                    if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
                        hits = idx.search(safe_question, limit=12)
                    else:
                        results = idx.query(safe_question, k=10, tier=None)
                        hits = [doc_to_retrieve_hit(d, s, safe_question) for d, s in results]  # type: ignore[misc]
                except Exception as e2:
                    logger.warning("Legacy retrieval also failed: %s", e2)

        result = answer_question(safe_question, hits, history or [], lang=lang)

        # If retrieval returned zero hits, fall back to conversational mode
        # so the user at least gets a helpful response instead of "no results"
        if not hits:
            logger.info("No retrieval hits — falling back to conversational for: '%s'", safe_question[:80])
            result = answer_conversational(safe_question, history or [], lang=lang)
    else:
        # Conversational — no retrieval, direct LLM response
        logger.info("Conversational mode for: '%s'", safe_question[:80])
        result = answer_conversational(safe_question, history or [], lang=lang)

    if guard.notes:
        result["guard"] = guard.to_dict()

    # Persist user + assistant turns.
    auth.append_message(body.thread_id, "user", safe_question, None)
    auth.append_message(
        body.thread_id,
        "assistant",
        result["answer_markdown"],
        serialize_citations(result.get("citations") or []),
    )

    return JSONResponse(result)


@app.post("/api/brief/chat-v2")
def api_brief_chat_v2(
    body: ChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    """
    Same envelope as /api/brief/chat but routed through the full reasoner:
      planner LLM  →  multi-corpus retrieval  →  synthesiser  →  6 answer gates
    Returns the legacy `{answer_markdown, citations, validation, ...}` shape
    so the existing chat UI keeps rendering, while the answer is now
    grounded across 83M rows with sub-question decomposition + caveats.
    """
    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("chat", ip, max_hits=30, window_s=60):
        raise HTTPException(429, "You're asking very quickly. Breathe.")

    history = auth.get_thread_messages(body.thread_id, user["id"])
    if history is None:
        raise HTTPException(404, "Thread not found.")

    guard = input_guards.check(body.question, history_len=len(history or []))
    if not guard.allow:
        auth.append_message(body.thread_id, "user", body.question, None)
        auth.append_message(body.thread_id, "assistant",
                            guard.refusal_message, serialize_citations([]))
        return JSONResponse({
            "answer_markdown": guard.refusal_message,
            "citations": [],
            "llm": {"provider": "guard", "model": "input_guards",
                    "latency_ms": 0, "fallback_chain": []},
            "validation": {"passed": False, "confidence": 0.0,
                           "reasons": [guard.reason]},
            "refused": True,
            "guard": guard.to_dict(),
        })

    safe_question = guard.redacted_question or body.question

    # Conversational queries skip the heavy reasoner.
    if not _needs_case_retrieval(safe_question):
        result = answer_conversational(safe_question, history or [],
                                       lang=(body.language or body.lang or "en"))
        if guard.notes:
            result["guard"] = guard.to_dict()
        auth.append_message(body.thread_id, "user", safe_question, None)
        auth.append_message(body.thread_id, "assistant",
                            result["answer_markdown"],
                            serialize_citations(result.get("citations") or []))
        return JSONResponse(result)

    # Reasoner pipeline.
    import sys as _sys
    from pathlib import Path as _Path
    try:
        corpus_root = _Path(__file__).resolve().parent.parent / "india-judgments-corpus"
        if str(corpus_root) not in _sys.path:
            _sys.path.insert(0, str(corpus_root))
        from scripts.assistant.legal_reasoner import answer as reason_answer
    except Exception as exc:
        logger.exception("legal reasoner import failed; falling back to /chat")
        return api_brief_chat(body, request, ls_session)

    try:
        r = reason_answer(safe_question)
    except Exception as exc:
        logger.exception("legal reasoner runtime error; falling back to /chat")
        return api_brief_chat(body, request, ls_session)

    # Compose markdown body from the reasoner's structured fields.
    md_parts: list[str] = []
    short = (r.get("answer_short") or "").strip()
    long_ = (r.get("answer_long") or "").strip()
    if short:
        md_parts.append(short)
    if long_ and long_ != short:
        md_parts.append("\n\n" + long_)
    sub_answers = r.get("sub_answers") or []
    if sub_answers:
        md_parts.append("\n\n---\n\n### Sub-answers")
        for sa in sub_answers[:6]:
            q = (sa.get("sub_question") or "").strip()
            a = (sa.get("answer") or "").strip()
            if q:
                md_parts.append(f"\n\n**{q}**\n\n{a}")
    caveats = r.get("caveats") or []
    if caveats:
        md_parts.append("\n\n---\n\n### Caveats")
        for c in caveats[:4]:
            md_parts.append(f"\n- {c}")
    answer_md = "".join(md_parts).strip() or "No answer generated."

    # Map evidence → citations in the shape the rail consumes.
    citations = []
    for ev in (r.get("evidence") or [])[:12]:
        citations.append({
            "title":    ev.get("title") or ev.get("citation") or "Untitled",
            "source":   ev.get("source") or "corpus",
            "case_id":  ev.get("case_id") or "",
            "citation": ev.get("citation") or "",
            "excerpt":  (ev.get("excerpt") or "")[:600],
            "url":      ev.get("pdf_url") or "",
            "score":    float(ev.get("score") or 0.0),
        })

    # Server-side post-process: annotate every IPC / CrPC / IEA section
    # reference with its BNS / BNSS / BSA equivalent. Belt-and-braces with
    # the synthesizer's system-prompt rule — guarantees both numbers appear
    # even if the LLM forgets.
    try:
        from legal_code_mapping import annotate_text  # type: ignore
        answer_md = annotate_text(answer_md)
    except ImportError:
        pass
    response = {
        "answer_markdown": answer_md,
        "citations": citations,
        "llm": {"provider": "legal_reasoner",
                "model": "planner+synth+validator",
                "latency_ms": int(r.get("elapsed_ms") or 0),
                "fallback_chain": []},
        "validation": {
            "passed":     not r.get("warnings"),
            "confidence": r.get("confidence") or "medium",
            "reasons":    r.get("warnings") or [],
        },
        "sub_questions": r.get("sub_questions") or [],
        "practice_area": r.get("practice_area") or "auto",
    }
    if guard.notes:
        response["guard"] = guard.to_dict()

    auth.append_message(body.thread_id, "user", safe_question, None)
    auth.append_message(body.thread_id, "assistant",
                        response["answer_markdown"],
                        serialize_citations(response["citations"]))
    return JSONResponse(response)


# ── brief mode aliases ──────────────────────────────────────────────────
# The frontend routes to different endpoints based on the active mode
# (Search / Agent / Canvas). All currently share the same BM25 retrieval
# pipeline. When we add real web-search or agentic loops, these stubs
# become their own implementations.

@app.post("/api/brief/web")
def api_brief_web(
    body: ChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Web-search mode — fetches from DuckDuckGo + RSS + BM25 corpus."""
    import web_signals as ws

    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("chat", ip, max_hits=30, window_s=60):
        raise HTTPException(429, "You're asking very quickly. Breathe.")

    history = auth.get_thread_messages(body.thread_id, user["id"])
    if history is None:
        raise HTTPException(404, "Thread not found.")

    guard = input_guards.check(body.question, history_len=len(history or []))
    if not guard.allow:
        auth.append_message(body.thread_id, "user", body.question, None)
        auth.append_message(body.thread_id, "assistant", guard.refusal_message, serialize_citations([]))
        return JSONResponse({
            "answer_markdown": guard.refusal_message,
            "citations": [], "web_citations": [],
            "llm": {"provider": "guard", "model": "input_guards", "latency_ms": 0, "fallback_chain": []},
            "refused": True, "guard": guard.to_dict(),
        })

    safe_question = guard.redacted_question or body.question

    # 1. Web search: DuckDuckGo + RSS feeds
    web_results: list[dict] = []
    try:
        ddg = ws.search_duckduckgo(safe_question, max_results=6)
        rss = ws.search_web_signals(safe_question, max_items=4)
        all_web = ddg + rss
        # Deduplicate by URL
        seen_urls: set[str] = set()
        for s in all_web:
            if s.url not in seen_urls:
                seen_urls.add(s.url)
                web_results.append(s.to_dict())
        logger.info("web search: %d DDG + %d RSS = %d unique for '%s'",
                     len(ddg), len(rss), len(web_results), safe_question[:50])
    except Exception as e:
        logger.warning("web search failed: %s", e)

    # 2. BM25 corpus search (still ground with real cases)
    hits: list[dict] = []
    idx = _ensure_bm25()
    if idx is not None:
        try:
            if _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
                hits = idx.search(safe_question, limit=8)
            else:
                results = idx.query(safe_question, k=8, tier=None)
                hits = [doc_to_retrieve_hit(d, s, safe_question) for d, s in results]
        except Exception as e:
            logger.warning("BM25 retrieval failed in /api/brief/web: %s", e)

    # 3. Build combined answer
    lang = body.language or body.lang or "en"
    result = answer_question(safe_question, hits, history or [], lang=lang)

    # Add web citations to the result
    result["web_citations"] = web_results[:10]

    # Build a web-context summary for the no-LLM case
    if not result.get("answer_markdown") or "no LLM provider" in result.get("answer_markdown", "").lower():
        # Enhance no-LLM response with web results
        web_md_parts = []
        if web_results:
            web_md_parts.append("\n\n---\n\n## 🌐 Web Results\n")
            for i, wr in enumerate(web_results[:8], 1):
                web_md_parts.append(
                    f"**[{i}] [{wr['title']}]({wr['url']})**\n"
                    f"*{wr.get('source_name', 'Web')}*"
                    f"{(' · ' + wr['date']) if wr.get('date') else ''}\n\n"
                    f"{wr.get('excerpt', '')[:200]}\n"
                )
        if web_md_parts:
            result["answer_markdown"] = result.get("answer_markdown", "") + "".join(web_md_parts)

    if guard.notes:
        result["guard"] = guard.to_dict()

    auth.append_message(body.thread_id, "user", safe_question, None)
    auth.append_message(
        body.thread_id, "assistant",
        result["answer_markdown"],
        serialize_citations(result.get("citations") or []),
    )

    return JSONResponse(result)


@app.post("/api/brief/agent")
def api_brief_agent(
    body: ChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Agent mode — currently aliases to BM25 corpus search."""
    return api_brief_chat(body, request, ls_session)


@app.post("/api/brief/draft")
def api_brief_draft(
    body: ChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    """Canvas/draft mode — currently aliases to BM25 corpus search."""
    return api_brief_chat(body, request, ls_session)


# ─────────────────────────────────────────────────────────────────────────
# VAULT — per-user document upload + multi-doc Q&A
# ─────────────────────────────────────────────────────────────────────────

@app.get("/api/vault/docs")
def api_vault_list(ls_session: Optional[str] = Cookie(default=None)):
    user = _require_user(ls_session)
    return {"docs": auth.vault_list_docs(user["id"])}


@app.post("/api/vault/upload")
async def api_vault_upload(
    request: Request,
    file: UploadFile = File(...),
    ls_session: Optional[str] = Cookie(default=None),
):
    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("vault_upload", ip, max_hits=20, window_s=600):
        raise HTTPException(429, "Too many uploads. Try again in a few minutes.")

    # Size check
    blob = await file.read()
    if len(blob) > vault_service.UPLOAD_MAX_BYTES:
        raise HTTPException(413, f"File exceeds {vault_service.UPLOAD_MAX_BYTES // (1024*1024)} MB limit.")

    fname = (file.filename or "upload").lower()
    if not any(fname.endswith(ext) for ext in vault_service.ALLOWED_EXTS):
        raise HTTPException(415, f"Allowed types: {', '.join(sorted(vault_service.ALLOWED_EXTS))}")

    text = vault_service.extract_text(file.filename or fname, blob)
    if not text.strip():
        raise HTTPException(422, "Could not extract text from this file.")

    doc_id = auth.vault_create_doc(user["id"], file.filename or fname, file.content_type or "", len(blob))
    chunks = vault_service.chunk_document(doc_id, file.filename or fname, text)
    n = auth.vault_save_chunks(doc_id, user["id"], chunks)
    return {"ok": True, "doc_id": doc_id, "filename": file.filename, "chunks": n}


@app.delete("/api/vault/docs/{doc_id}")
def api_vault_delete(doc_id: int, ls_session: Optional[str] = Cookie(default=None)):
    user = _require_user(ls_session)
    ok = auth.vault_delete_doc(user["id"], doc_id)
    if not ok:
        raise HTTPException(404, "Document not found.")
    return {"ok": True, "doc_id": doc_id}


class VaultChatBody(BaseModel):
    question: str = Field(..., min_length=2, max_length=4000)
    doc_ids: Optional[list[int]] = None  # None = all docs


@app.post("/api/vault/chat")
def api_vault_chat(
    body: VaultChatBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("vault_chat", ip, max_hits=30, window_s=60):
        raise HTTPException(429, "You're asking very quickly. Breathe.")

    guard = input_guards.check(body.question, history_len=1)  # treat as follow-up (don't gate scope)
    if not guard.allow:
        return JSONResponse({"answer_markdown": guard.refusal_message, "refused": True,
                             "citations": [], "guard": guard.to_dict()})

    chunks = auth.vault_load_chunks(user["id"], body.doc_ids)
    if not chunks:
        return JSONResponse({
            "answer_markdown": "Your vault is empty. Upload a document first (PDF, DOCX, or TXT).",
            "citations": [], "refused": True,
        })

    hits = vault_service.rank_chunks(guard.redacted_question, chunks, k=8)
    result = vault_service.answer_over_vault(guard.redacted_question, hits, [])
    return JSONResponse(result)


class VaultAnalyseBody(BaseModel):
    doc_id: int
    language: Optional[str] = None


@app.post("/api/vault/analyse")
def api_vault_analyse(
    body: VaultAnalyseBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    """One-click structured document analysis: parties, dates, obligations, risks."""
    user = _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("vault_analyse", ip, max_hits=10, window_s=60):
        raise HTTPException(429, "Too many analysis requests.")

    chunks = auth.vault_load_chunks(user["id"], [body.doc_id])
    if not chunks:
        raise HTTPException(404, "Document not found or empty.")

    # Build full text from chunks
    full_text = "\n\n".join(c.get("text", "") for c in chunks[:20])[:8000]

    lang_name = ""
    if body.language and body.language != "en":
        from brief_service import LANGUAGES
        lang_name = LANGUAGES.get(body.language, "")

    system = """You are a legal document analyst. Analyze the document and provide a structured breakdown:

## Parties
- List all parties mentioned with their roles

## Key Dates & Timelines
- All important dates, deadlines, limitation periods

## Obligations & Commitments
- What each party is required to do

## Risks & Red Flags
- Potential legal risks, unfavorable clauses, missing provisions

## Governing Law & Jurisdiction
- Applicable law, jurisdiction, arbitration clause if any

## Recommended Next Steps
- What the advocate should do next

Be precise and practical. Use Indian legal terminology."""

    if lang_name:
        system += f"\n\nRespond in {lang_name}."

    try:
        from llm import router as llm_router
        resp = llm_router.generate(system, f"Analyze this document:\n\n{full_text}", temperature=0.2, max_tokens=1500)
        return JSONResponse({"analysis_markdown": resp.text})
    except Exception as e:
        logger.error("vault analyse failed: %s", e)
        raise HTTPException(500, f"Analysis failed: {str(e)[:100]}")


# ─────────────────────────────────────────────────────────────────────────
# DRAFT — 4 Indian-law templates
# ─────────────────────────────────────────────────────────────────────────

@app.get("/api/draft/templates")
def api_draft_templates(ls_session: Optional[str] = Cookie(default=None)):
    _require_user(ls_session)
    return {"templates": workflows.list_draft_templates()}


class DraftBody(BaseModel):
    template: str = Field(..., min_length=2, max_length=64)
    facts: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/draft")
def api_draft(
    body: DraftBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("draft", ip, max_hits=20, window_s=300):
        raise HTTPException(429, "Too many draft requests. Wait a few minutes.")
    try:
        # Try DB-backed template first
        db_template = None
        idx = _ensure_bm25()
        if idx and _FTS5_AVAILABLE and isinstance(idx, FTS5Index):
            db_template = idx.draft_template(body.template)
        return workflows.generate_draft(body.template, body.facts, db_template=db_template)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("draft failed: %s", e)
        raise HTTPException(500, "Draft generation failed.")


# ─────────────────────────────────────────────────────────────────────────
# REVIEW — contract clause review
# ─────────────────────────────────────────────────────────────────────────

class ReviewBody(BaseModel):
    clauses: list[str] = Field(..., min_length=1, max_length=40)


@app.post("/api/review")
def api_review(
    body: ReviewBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("review", ip, max_hits=20, window_s=300):
        raise HTTPException(429, "Too many review requests.")
    return workflows.review_contract(body.clauses)


# ─────────────────────────────────────────────────────────────────────────
# TRANSLATE — EN ↔ HI
# ─────────────────────────────────────────────────────────────────────────

class TranslateBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    direction: str = Field(default="en->hi")


@app.post("/api/translate")
def api_translate(
    body: TranslateBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("translate", ip, max_hits=30, window_s=300):
        raise HTTPException(429, "Too many translation requests.")
    try:
        return workflows.translate(body.text, direction=body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ─────────────────────────────────────────────────────────────────────────
# CITATOR — judicial history + related cases
# ─────────────────────────────────────────────────────────────────────────

class CitatorBody(BaseModel):
    case_title: str = Field(..., min_length=2, max_length=400)
    excerpt: str = Field(..., min_length=10, max_length=20000)
    holdings: str = Field(default="", max_length=8000)


@app.post("/api/citator")
def api_citator(
    body: CitatorBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("citator", ip, max_hits=30, window_s=300):
        raise HTTPException(429, "Too many citator requests.")
    return workflows.citator_summary(body.case_title, body.excerpt, body.holdings)


# ─────────────────────────────────────────────────────────────────────────
# REDLINE + GENERIC WORKFLOWS (Harvey-parity for India)
# ─────────────────────────────────────────────────────────────────────────

class RedlineBody(BaseModel):
    text: str = Field(..., min_length=50, max_length=40000)


@app.post("/api/redline")
def api_redline(
    body: RedlineBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("redline", ip, max_hits=15, window_s=300):
        raise HTTPException(429, "Too many redline requests.")
    return workflows.redline_contract(body.text)


class GenericBody(BaseModel):
    key: str = Field(..., min_length=2, max_length=64)
    text: str = Field(..., min_length=10, max_length=40000)


@app.get("/api/workflows")
def api_workflows_list():
    return {"workflows": workflows.list_generic_workflows()}


@app.post("/api/workflows/run")
def api_workflows_run(
    body: GenericBody,
    request: Request,
    ls_session: Optional[str] = Cookie(default=None),
):
    _require_user(ls_session)
    ip = _client_ip(request)
    if not auth.rate_limit("wf", ip, max_hits=30, window_s=300):
        raise HTTPException(429, "Too many workflow runs.")
    try:
        return workflows.run_generic(body.key, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── stub endpoints for frontend compatibility ────────────────────────────

@app.get("/api/connectors")
def api_connectors():
    """Return connector availability — India-only, always available."""
    return {"connectors": {"india_courts": True}}


@app.get("/api/clients")
def api_clients(
    status: Optional[str] = Query(default=None),
    ls_session: Optional[str] = Cookie(default=None),
):
    """NyayaSathi client queue — stub for now."""
    _require_user(ls_session)
    return {"clients": [], "total": 0}


@app.post("/api/google/docs/create")
def api_google_docs_create(
    ls_session: Optional[str] = Cookie(default=None),
):
    """Google Docs export — not yet implemented."""
    _require_user(ls_session)
    raise HTTPException(501, "Google Docs export coming soon.")


# ── admin endpoints (bearer-token auth) ───────────────────────────────────

@app.get("/api/admin/requests")
def api_admin_list_requests(
    status: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
):
    _require_admin(authorization)
    return {"requests": auth.list_access_requests(status=status)}


@app.post("/api/admin/requests/{request_id}/approve")
def api_admin_approve(
    request_id: int,
    authorization: Optional[str] = Header(default=None),
):
    _require_admin(authorization)
    approved = auth.approve_request(request_id)
    if not approved:
        raise HTTPException(404, "No pending request with that id.")
    # access_code is plaintext — returned ONCE.
    return approved


@app.post("/api/admin/requests/{request_id}/reject")
def api_admin_reject(
    request_id: int,
    authorization: Optional[str] = Header(default=None),
):
    _require_admin(authorization)
    ok = auth.reject_request(request_id)
    if not ok:
        raise HTTPException(404, "No pending request with that id.")
    return {"ok": True, "request_id": request_id, "status": "rejected"}


# ── Frontend handoff ──────────────────────────────────────────────────────
# Phase 2 cleanup: the legacy single-file HTML/JS frontend (login.html,
# brief.html, admin.html, app.js, etc.) is retired in favor of the Next.js
# app at web/. The backend now serves API + /auth/* only; every legacy page
# route 302s to the canonical frontend on FRONTEND_ORIGIN (default
# http://localhost:3000 in dev). Static asset and JS file routes return 404
# — nothing in the new frontend references them.

STATIC_DIR = Path(__file__).parent


def _frontend_origin() -> str:
    """Where to send users for the UI. In dev defaults to localhost:3000;
    in production set FRONTEND_ORIGIN to the canonical public URL."""
    origin = os.environ.get("FRONTEND_ORIGIN", "").strip().rstrip("/")
    if origin:
        return origin
    # Reasonable dev default — what the Next.js `npm run dev` binds to.
    return "http://localhost:3000"


def _redirect_to_frontend(path: str = "/") -> RedirectResponse:
    return RedirectResponse(url=f"{_frontend_origin()}{path}", status_code=302)


# Browser entry points → Next.js. The Next.js app then proxies /api/* and
# /auth/* back here for data; cookies remain first-party on the frontend
# origin throughout.
@app.get("/")
async def serve_index():
    return _redirect_to_frontend("/")


@app.get("/login")
async def serve_login():
    return _redirect_to_frontend("/login")


@app.get("/app")
async def serve_app():
    return _redirect_to_frontend("/app")


# Legacy pages were /brief, /admin, /viewer.html. /admin is still useful in
# theory (token-gated data fetches behind it) but the page itself is gone;
# point operators at the docs until the dashboard is rebuilt in Next.js.
@app.get("/brief")
async def serve_brief_legacy():
    return _redirect_to_frontend("/app")


@app.get("/admin")
async def serve_admin_legacy():
    return _redirect_to_frontend("/account/security")


@app.get("/viewer.html")
async def serve_viewer_legacy():
    return _redirect_to_frontend("/app")


# Asset routes that the legacy HTML referenced. The Next.js app does NOT
# reference these; returning 404 makes any stray legacy bookmark fail loud
# rather than silently serve stale JS.
_LEGACY_GONE_PATHS = ("/style.css", "/app.js", "/workspaces.js", "/app-shell.js", "/brief.js")


@app.get("/style.css")
@app.get("/app.js")
@app.get("/workspaces.js")
@app.get("/app-shell.js")
@app.get("/brief.js")
async def _legacy_assets_gone():
    raise HTTPException(404, "legacy frontend retired; the Next.js app at FRONTEND_ORIGIN owns the UI")


# /assets/* (firm logos, favicon, etc.) is kept ONLY because the PDF
# proxy templates may embed absolute /assets/ URLs in citation cards. The
# Next.js app does not depend on it. If you confirm nothing renders these
# anymore, remove the route entirely.
@app.get("/assets/{path:path}")
async def serve_asset(path: str):
    """Serve static assets (logo, firm logos, etc.) from ./assets/."""
    safe_path = (STATIC_DIR / "assets" / path).resolve()
    assets_root = (STATIC_DIR / "assets").resolve()
    if assets_root not in safe_path.parents and safe_path != assets_root:
        raise HTTPException(404, "Not found")
    if not safe_path.is_file():
        raise HTTPException(404, "Asset not found")
    ext = safe_path.suffix.lower()
    media = {
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    return FileResponse(safe_path, media_type=media)
