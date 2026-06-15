"""
CSE (Colombo Stock Exchange) Annual & Quarterly Report Downloader
=================================================================
Downloads past 10 years of Annual and Quarterly (Interim) reports for:
  - Ambeon Holdings PLC           (GREG.N0000)

Output folder: E:\\AMBEON\\script\\backend\\Demo_Data

Usage:
    pip install requests beautifulsoup4 lxml tqdm
    python backend/scripts/demo_reports_download_amb.py

Notes:
  - The CSE website is a JavaScript SPA; this script uses the undocumented
    REST API that the browser uses (https://www.cse.lk/api/).
  - If an API endpoint changes or rate-limits you, the script retries and
    skips gracefully, logging all failures to  failed_downloads.log.
  - For quarterly reports CSE calls them "Interim Financial Statements"
    (published 4 times a year: Q1/Q2/Q3/Q4 of each financial year).
"""

import os
import re
import time
import json
import logging
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

# ── optional: pretty progress bars ──────────────────────────────────────────
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_OUTPUT_DIR = Path(r"E:\AMBEON\script\backend\Demo_Data")
YEAR_FROM       = datetime.now().year - 10   # e.g. 2015 if run in 2025
YEAR_TO         = datetime.now().year
REQUEST_DELAY   = 1.5          # seconds between HTTP requests (be polite)
MAX_RETRIES     = 3
TIMEOUT         = 30

# Companies: display name  →  CSE ticker symbol
COMPANIES = {
    "Ambeon Holdings PLC": "GREG.N0000",
}

CSE_API_BASE   = "https://www.cse.lk/api/"
CSE_CDN_BASE   = "https://cdn.cse.lk/"

# Report-type codes used by the CSE API
REPORT_TYPE_ANNUAL    = "AR"    # Annual Report
REPORT_TYPE_QUARTERLY = "IFS"   # Interim Financial Statements (quarterly)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cse_download.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

failed_log = logging.getLogger("failed")
failed_handler = logging.FileHandler("failed_downloads.log", encoding="utf-8")
failed_log.addHandler(failed_handler)
failed_log.setLevel(logging.WARNING)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cse.lk/",
    "Origin":  "https://www.cse.lk",
})


def _get(url: str, **kwargs) -> requests.Response | None:
    """GET with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=TIMEOUT, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            log.warning("GET %s  attempt %d/%d  → %s", url, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)
    return None


def _post(url: str, data: dict, **kwargs) -> requests.Response | None:
    """POST with retries."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.post(url, data=data, timeout=TIMEOUT, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            log.warning("POST %s  attempt %d/%d  → %s", url, attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)
    return None


# ── CSE API helpers ───────────────────────────────────────────────────────────
def get_company_id(symbol: str) -> str | None:
    """
    Resolve the internal numeric company ID from a ticker symbol.
    The CSE API returns it inside 'reqSymbolInfo' or 'companyId'.
    """
    r = _post(CSE_API_BASE + "companyInfoSummery", data={"symbol": symbol})
    if not r:
        return None
    try:
        payload = r.json()
        # Try common key paths
        for path in [
            ["reqSymbolInfo", "companyId"],
            ["reqSymbolInfo", "id"],
            ["companyId"],
        ]:
            node = payload
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if node:
                return str(node)
    except Exception:
        pass
    # Fallback: scrape from the company page HTML
    return _scrape_company_id(symbol)


def _scrape_company_id(symbol: str) -> str | None:
    """
    Fallback: hit the CSE listed-company page and scrape the numeric id
    from the network calls embedded in the page source / meta tags.
    """
    url = f"https://www.cse.lk/pages/listed-company/listed-company.component.html?symbol={symbol}"
    r = _get(url)
    if not r:
        return None
    # The page embeds JSON with 'companyId' or the id appears in API call URLs
    m = re.search(r'"companyId"\s*:\s*(\d+)', r.text)
    if m:
        return m.group(1)
    m = re.search(r'/companyInfo/(\d+)', r.text)
    if m:
        return m.group(1)
    return None


def _epoch_to_date_str(value) -> str:
    """Convert CSE manualDate/uploadedDate (epoch ms) to YYYY-MM-DD."""
    if value is None or value == "":
        return ""
    try:
        ts = int(value)
        if ts > 1_000_000_000_000:
            ts //= 1000
        return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(value)


def _normalize_financial_item(item: dict) -> dict | None:
    """Map CSE financials API record to downloader fields."""
    path = item.get("path") or item.get("reportFile") or item.get("filePath") or ""
    if not path or not str(path).lower().endswith(".pdf"):
        return None
    manual = item.get("manualDate") or item.get("uploadedDate") or item.get("reportDate") or ""
    return {
        "title": item.get("fileText") or item.get("title") or item.get("reportName") or "",
        "reportDate": _epoch_to_date_str(manual),
        "reportFile": path,
    }


def fetch_reports(symbol: str, report_type: str) -> list[dict]:
    """
    Query CSE API for all financial reports of a given type for a company.

    API endpoint (CSE 2025 site):
      POST https://www.cse.lk/api/financials
      Body: symbol=GREG.N0000
      Response keys: infoAnnualData, infoQuarterlyData
    """
    r = _post(CSE_API_BASE + "financials", data={"symbol": symbol})
    if r:
        try:
            data = r.json()
            list_key = (
                "infoAnnualData"
                if report_type == REPORT_TYPE_ANNUAL
                else "infoQuarterlyData"
            )
            raw = data.get(list_key, [])
            reports = []
            for item in raw:
                normalized = _normalize_financial_item(item)
                if normalized:
                    reports.append(normalized)
            if reports:
                return reports
        except Exception:
            pass

    return _scrape_reports_from_page(symbol, report_type)


def _scrape_reports_from_page(symbol: str, report_type: str) -> list[dict]:
    """
    Scrape report links directly from the CSE company page as a fallback.
    Returns minimal dicts with keys: title, reportDate, reportFile.
    """
    from bs4 import BeautifulSoup
    type_param = "annual" if report_type == REPORT_TYPE_ANNUAL else "interim"
    url = f"https://www.cse.lk/company-profile?symbol={symbol}"
    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    reports = []
    for a in soup.find_all("a", href=re.compile(r"cdn\.cse\.lk.*\.pdf", re.I)):
        href = a["href"]
        title = a.get_text(strip=True) or href
        # Try to parse a date from surrounding text
        reports.append({
            "title":      title,
            "reportDate": "",
            "reportFile": href.replace(CSE_CDN_BASE, "").lstrip("/"),
        })
    return reports


def _extract_year_quarter(title: str, date_str: str) -> tuple[int | None, int | None]:
    """
    From a report title / date string derive (year, quarter).
    Quarter is None for annual reports.
    """
    year = None
    quarter = None

    # Try date field first (ISO date or epoch milliseconds)
    if date_str:
        if str(date_str).isdigit() and len(str(date_str)) >= 10:
            try:
                ts = int(date_str)
                if ts > 1_000_000_000_000:
                    ts //= 1000
                dt = datetime.utcfromtimestamp(ts)
                year = dt.year
                quarter = (dt.month - 1) // 3 + 1
            except (ValueError, OSError):
                pass
        if year is None:
            m = re.search(r"(\d{4})", date_str)
            if m:
                year = int(m.group(1))
        if quarter is None:
            m2 = re.search(r"-(\d{2})-", date_str)
            if m2:
                month = int(m2.group(1))
                quarter = (month - 1) // 3 + 1

    # Fall back to title
    if not year:
        m = re.search(r"20(\d{2})", title)
        if m:
            year = int("20" + m.group(1))

    if not quarter:
        m = re.search(r"Q(\d)", title, re.I)
        if m:
            quarter = int(m.group(1))
        elif re.search(r"(January|February|March)", title, re.I):
            quarter = 1
        elif re.search(r"(April|May|June)", title, re.I):
            quarter = 2
        elif re.search(r"(July|August|September)", title, re.I):
            quarter = 3
        elif re.search(r"(October|November|December)", title, re.I):
            quarter = 4

    return year, quarter


# ── Folder creation ───────────────────────────────────────────────────────────
def make_dirs(company_name: str, report_type: str, year: int, quarter: int | None) -> Path:
    """
    Build and create the directory path matching the required structure:
      Demo_Data / <Company> / Annual|Quarterly / <label>
    """
    sub = "Annual" if report_type == REPORT_TYPE_ANNUAL else "Quarterly"
    if report_type == REPORT_TYPE_ANNUAL:
        label = f"Annual Report {year}"
    else:
        label = f"Quarterly Report {year} Q{quarter}"

    folder = BASE_OUTPUT_DIR / company_name / sub / label
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── Download ──────────────────────────────────────────────────────────────────
def download_pdf(report_file_path: str, dest_folder: Path, filename: str) -> bool:
    """
    Download a PDF from cdn.cse.lk and save to dest_folder/filename.pdf.
    Skips if the file already exists (resume-friendly).
    """
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    dest = dest_folder / filename

    if dest.exists() and dest.stat().st_size > 1024:
        log.info("  ↳ already exists, skipping: %s", dest.name)
        return True

    path = report_file_path.lstrip("/")
    if path.startswith("upload_report_file/") and not path.startswith("cmt/"):
        path = "cmt/" + path
    url = urljoin(CSE_CDN_BASE, path)
    log.info("  ↳ downloading %s", url)
    time.sleep(REQUEST_DELAY)
    r = _get(url, stream=True)
    if not r:
        failed_log.warning("FAILED  %s  →  %s", url, dest)
        return False

    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)

    size_kb = dest.stat().st_size // 1024
    log.info("     saved %s  (%d KB)", dest.name, size_kb)
    return True


# ── Main orchestrator ─────────────────────────────────────────────────────────
def process_company(company_name: str, symbol: str):
    log.info("=" * 60)
    log.info("Company : %s  (%s)", company_name, symbol)
    log.info("=" * 60)

    for report_type, label in [
        (REPORT_TYPE_ANNUAL,    "Annual Reports"),
        (REPORT_TYPE_QUARTERLY, "Quarterly Reports"),
    ]:
        log.info("  Fetching %s list …", label)
        reports = fetch_reports(symbol, report_type)
        log.info("  → %d reports found (all years)", len(reports))

        if not reports:
            log.warning("  No %s found for %s — skipping.", label, symbol)
            continue

        filtered = []
        for rep in reports:
            title    = rep.get("title", "") or rep.get("reportName", "") or rep.get("fileText", "") or ""
            date_str = rep.get("reportDate", "") or rep.get("submittedDate", "") or ""
            rf       = rep.get("reportFile", "") or rep.get("filePath", "") or rep.get("path", "") or ""

            year, quarter = _extract_year_quarter(title, date_str)

            if year is None:
                log.debug("  Cannot determine year for: %s — skipping", title)
                continue
            if not (YEAR_FROM <= year <= YEAR_TO):
                continue
            if report_type == REPORT_TYPE_QUARTERLY and quarter is None:
                log.debug("  Cannot determine quarter for: %s — skipping", title)
                continue
            if not rf:
                log.debug("  No file path for: %s — skipping", title)
                continue

            filtered.append((year, quarter, title, rf))

        log.info("  → %d reports in range %d–%d", len(filtered), YEAR_FROM, YEAR_TO)

        iterator = tqdm(filtered, desc=f"  {company_name[:25]} {label[:15]}") if HAS_TQDM else filtered

        for year, quarter, title, rf in iterator:
            folder = make_dirs(company_name, report_type, year, quarter)

            # Build a clean filename from the title
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()
            safe_title = safe_title[:120]          # truncate if very long
            if not safe_title:
                safe_title = f"{symbol}_{year}"
                if quarter:
                    safe_title += f"_Q{quarter}"

            download_pdf(rf, folder, safe_title)


def main():
    log.info("CSE Report Downloader  |  Years: %d – %d", YEAR_FROM, YEAR_TO)
    log.info("Output dir: %s", BASE_OUTPUT_DIR)
    BASE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for company_name, symbol in COMPANIES.items():
        try:
            process_company(company_name, symbol)
        except Exception as exc:
            log.error("Unhandled error for %s: %s", company_name, exc, exc_info=True)
            failed_log.error("COMPANY ERROR  %s  %s", company_name, exc)

    log.info("")
    log.info("All done.  Check failed_downloads.log for any skipped files.")


if __name__ == "__main__":
    main()