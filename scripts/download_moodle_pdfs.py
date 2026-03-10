#!/usr/bin/env python3
"""Descarga PDFs de enlaces Moodle detectados en un HTML exportado."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REQUEST_TIMEOUT = (10, 60)
USER_AGENT = "moodle-pdf-downloader/1.0"
MOODLE_PATTERNS = (
    "mod/resource/view.php",
    "mod/folder/view.php",
    "mod/url/view.php",
    "pluginfile.php",
)


@dataclass
class LinkItem:
    url: str
    text: str
    category: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descargar PDFs desde HTML de Moodle")
    parser.add_argument("--html", required=True, help="Ruta al HTML exportado")
    parser.add_argument("--cookies", required=True, help="Archivo cookies.txt (Netscape)")
    parser.add_argument("--out", required=True, help="Carpeta de salida")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay entre requests")
    parser.add_argument("--verbose", action="store_true", help="Activar logs detallados")
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")


def sanitize_name(text: str, fallback: str = "archivo") -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[\\/:*?\"<>|]", "_", value).strip().strip(".")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"_+", "_", value)
    return value[:150] if value else fallback


def ensure_dirs(out_dir: Path) -> Tuple[Path, Path]:
    pdf_root = out_dir / "pdfs"
    logs_root = out_dir / "logs"
    pdf_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    return pdf_root, logs_root


def detect_base_url(soup: BeautifulSoup, explicit_base: Optional[str] = None) -> Optional[str]:
    if explicit_base:
        return explicit_base
    base_tag = soup.find("base", href=True)
    if base_tag:
        return str(base_tag["href"]).strip()
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v, href=True)
    if canonical:
        return str(canonical["href"]).strip()
    return None


def classify(url: str) -> str:
    lower = url.lower()
    if "mod/folder/view.php" in lower:
        return "folder_view"
    if "mod/resource/view.php" in lower:
        return "resource_view"
    if "mod/url/view.php" in lower:
        return "url_view"
    if "pluginfile.php" in lower:
        return "pluginfile"
    if lower.endswith(".pdf"):
        return "pdf_direct"
    return "other"


def extract_links(soup: BeautifulSoup, base_url: Optional[str]) -> List[LinkItem]:
    links: List[LinkItem] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        full_url = urljoin(base_url, href) if base_url else href
        if not full_url.startswith(("http://", "https://")):
            continue

        lower = full_url.lower()
        if not any(token in lower for token in MOODLE_PATTERNS) and not lower.endswith(".pdf"):
            continue
        if full_url in seen:
            continue

        seen.add(full_url)
        text = a.get_text(" ", strip=True) or Path(urlparse(full_url).path).name or "sin_nombre"
        links.append(LinkItem(url=full_url, text=sanitize_name(text, "sin_nombre"), category=classify(full_url)))
    return links


def load_cookies(cookies_file: Path) -> requests.cookies.RequestsCookieJar:
    if not cookies_file.exists():
        raise FileNotFoundError(f"No existe archivo de cookies: {cookies_file}")

    jar = requests.cookies.RequestsCookieJar()
    for raw in cookies_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, _expiry, name, value = parts[:7]
        jar.set(name, value, domain=domain, path=path, secure=(secure.upper() == "TRUE"))

    if not jar:
        raise ValueError("No se pudieron cargar cookies en formato Netscape")
    return jar


def build_session(cookie_jar: requests.cookies.RequestsCookieJar) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.cookies.update(cookie_jar)
    return session


def is_pdf_response(resp: requests.Response) -> bool:
    ctype = resp.headers.get("Content-Type", "").lower()
    return "application/pdf" in ctype or resp.url.lower().endswith(".pdf")


def filename_from_response(resp: requests.Response, fallback_text: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    filename = ""
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd)
    if match:
        filename = unquote(match.group(1).strip().strip('"'))
    if not filename:
        filename = Path(urlparse(resp.url).path).name
    if not filename:
        filename = f"{sanitize_name(fallback_text, 'archivo')}.pdf"

    filename = sanitize_name(filename, "archivo.pdf")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return filename


def parse_folder_pdf_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        lower = href.lower()
        if "pluginfile.php" in lower or lower.endswith(".pdf"):
            urls.add(href)
    return list(urls)


def request_url(session: requests.Session, url: str, delay: float) -> requests.Response:
    resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
    time.sleep(delay)
    return resp


def compute_hash(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def download_pdf(
    resp: requests.Response,
    pdf_root: Path,
    seen_hashes: Set[str],
    seen_names: Set[str],
    fallback_text: str,
) -> Tuple[str, str]:
    filename = filename_from_response(resp, fallback_text)
    target = unique_path(pdf_root / filename)

    with target.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)

    digest = compute_hash(target)
    lname = filename.lower()
    if digest in seen_hashes or lname in seen_names:
        target.unlink(missing_ok=True)
        return filename, "duplicate"

    seen_hashes.add(digest)
    seen_names.add(lname)
    return filename, "downloaded"


def write_logs(logs_root: Path, log_rows: List[Dict[str, str]], failed_rows: List[Dict[str, str]], summary: Dict[str, int]) -> None:
    with (logs_root / "download_log.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "filename", "status", "http_status"])
        writer.writeheader()
        writer.writerows(log_rows)

    with (logs_root / "failed_urls.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "status", "http_status", "error"])
        writer.writeheader()
        writer.writerows(failed_rows)

    with (logs_root / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    html_path = Path(args.html)
    cookies_path = Path(args.cookies)
    out_dir = Path(args.out)

    if not html_path.exists():
        logging.error("No existe HTML: %s", html_path)
        return 1

    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "lxml")
    base_url = detect_base_url(soup)

    links = extract_links(soup, base_url)
    logging.info("Enlaces Moodle detectados: %d", len(links))

    pdf_root, logs_root = ensure_dirs(out_dir)
    cookie_jar = load_cookies(cookies_path)
    session = build_session(cookie_jar)

    seen_input_urls: Set[str] = set()
    seen_final_urls: Set[str] = set()
    seen_hashes: Set[str] = set()
    seen_names: Set[str] = set()

    log_rows: List[Dict[str, str]] = []
    failed_rows: List[Dict[str, str]] = []

    summary = {
        "total_links": len(links),
        "downloaded": 0,
        "duplicates": 0,
        "failed": 0,
    }

    for item in links:
        if item.url in seen_input_urls:
            log_rows.append({"url": item.url, "filename": "", "status": "duplicate", "http_status": ""})
            summary["duplicates"] += 1
            continue
        seen_input_urls.add(item.url)

        try:
            resp = request_url(session, item.url, args.delay)
        except Exception as exc:
            log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": ""})
            failed_rows.append({"url": item.url, "status": "failed", "http_status": "", "error": str(exc)})
            summary["failed"] += 1
            continue

        if resp.status_code >= 400:
            status = str(resp.status_code)
            log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": status})
            failed_rows.append({"url": item.url, "status": "failed", "http_status": status, "error": f"HTTP {status}"})
            summary["failed"] += 1
            continue

        candidates: List[requests.Response] = []
        if is_pdf_response(resp):
            candidates.append(resp)
        elif item.category == "folder_view":
            for url in parse_folder_pdf_links(resp.text, resp.url):
                try:
                    nested = request_url(session, url, args.delay)
                except Exception:
                    continue
                if nested.status_code < 400 and is_pdf_response(nested):
                    candidates.append(nested)

        if not candidates:
            log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": str(resp.status_code)})
            failed_rows.append(
                {
                    "url": item.url,
                    "status": "failed",
                    "http_status": str(resp.status_code),
                    "error": "No se encontró PDF en el recurso",
                }
            )
            summary["failed"] += 1
            continue

        for pdf_resp in candidates:
            final_url = pdf_resp.url
            if final_url in seen_final_urls:
                log_rows.append({"url": final_url, "filename": "", "status": "duplicate", "http_status": str(pdf_resp.status_code)})
                summary["duplicates"] += 1
                continue
            seen_final_urls.add(final_url)

            try:
                filename, status = download_pdf(pdf_resp, pdf_root, seen_hashes, seen_names, item.text)
            except Exception as exc:
                log_rows.append({"url": final_url, "filename": "", "status": "failed", "http_status": str(pdf_resp.status_code)})
                failed_rows.append(
                    {
                        "url": final_url,
                        "status": "failed",
                        "http_status": str(pdf_resp.status_code),
                        "error": str(exc),
                    }
                )
                summary["failed"] += 1
                continue

            log_rows.append(
                {
                    "url": final_url,
                    "filename": filename,
                    "status": status,
                    "http_status": str(pdf_resp.status_code),
                }
            )
            if status == "downloaded":
                summary["downloaded"] += 1
            else:
                summary["duplicates"] += 1

    write_logs(logs_root, log_rows, failed_rows, summary)
    logging.info("Descargados=%s Duplicados=%s Fallidos=%s", summary["downloaded"], summary["duplicates"], summary["failed"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
