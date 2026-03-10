#!/usr/bin/env python3
"""Descarga PDFs autenticados desde una URL de Moodle usando cookies Netscape."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REQUEST_TIMEOUT = (10, 90)
USER_AGENT = "moodle-pdf-downloader/2.0"
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
    parser = argparse.ArgumentParser(description="Descargar PDFs desde Moodle autenticado")
    parser.add_argument("--url", required=True, help="URL Moodle a analizar")
    parser.add_argument("--cookies", required=True, help="Archivo cookies Netscape")
    parser.add_argument("--out", required=True, help="Carpeta de salida")
    parser.add_argument("--verbose", action="store_true", help="Logs detallados")
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="[%(levelname)s] %(message)s")


def sanitize_name(value: str, fallback: str = "archivo") -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[\\/:*?\"<>|]", "_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    text = re.sub(r"_+", "_", text)
    return text[:150] if text else fallback


def ensure_dirs(out_dir: Path) -> Tuple[Path, Path]:
    pdf_root = out_dir / "pdfs"
    logs_root = out_dir / "logs"
    pdf_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    return pdf_root, logs_root


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
        raise ValueError("No se pudieron cargar cookies Netscape")
    return jar


def build_session(cookie_jar: requests.cookies.RequestsCookieJar) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.cookies.update(cookie_jar)
    return session


def request_url(session: requests.Session, url: str, stream: bool = False) -> requests.Response:
    return session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=stream)


def is_login_response(resp: requests.Response) -> bool:
    urls = [h.url for h in resp.history] + [resp.url]
    for candidate in urls:
        parsed = urlparse(candidate)
        lower = candidate.lower()
        if "login/index.php" in lower:
            return True
        if parsed.path.lower().endswith("/login/") or parsed.path.lower().startswith("/login"):
            return True
    ctype = resp.headers.get("Content-Type", "").lower()
    if "text/html" in ctype:
        html = resp.text[:12000].lower()
        if "login" in html and ("username" in html or "password" in html or "acceder" in html):
            return True
    return False


def extract_links(html: str, base_url: str) -> List[LinkItem]:
    soup = BeautifulSoup(html, "lxml")
    found: List[LinkItem] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        full_url = urljoin(base_url, href)
        lower = full_url.lower()
        if not full_url.startswith(("http://", "https://")):
            continue
        if not any(token in lower for token in MOODLE_PATTERNS) and not lower.endswith(".pdf"):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        text = a.get_text(" ", strip=True) or Path(urlparse(full_url).path).name or "recurso"
        found.append(LinkItem(url=full_url, text=sanitize_name(text, "recurso"), category=classify(full_url)))

    return found


def filename_from_response(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    filename = ""

    match = re.search(r"filename\*=UTF-8''([^;]+)", cd, flags=re.IGNORECASE)
    if match:
        filename = unquote(match.group(1).strip())
    if not filename:
        match = re.search(r'filename="?([^";]+)"?', cd, flags=re.IGNORECASE)
        if match:
            filename = unquote(match.group(1).strip())

    if not filename:
        filename = Path(urlparse(resp.url).path).name
    if not filename:
        filename = f"{sanitize_name(fallback, 'archivo')}.pdf"

    filename = sanitize_name(filename, "archivo.pdf")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return filename


def pdf_signature(head_bytes: bytes) -> bool:
    return head_bytes.startswith(b"%PDF-")


def is_pdf_response(resp: requests.Response, head_bytes: bytes) -> bool:
    ctype = resp.headers.get("Content-Type", "").lower()
    cdisp = resp.headers.get("Content-Disposition", "").lower()
    final_url = resp.url.lower()

    if "application/pdf" in ctype:
        return True
    if "filename=" in cdisp and ".pdf" in cdisp:
        return True
    if final_url.endswith(".pdf") or ".pdf?" in final_url:
        return True
    if pdf_signature(head_bytes):
        return True
    return False


def parse_folder_pdf_links(html: str, base_url: str) -> List[LinkItem]:
    soup = BeautifulSoup(html, "lxml")
    links: List[LinkItem] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        lower = full.lower()
        if "pluginfile.php" not in lower and not lower.endswith(".pdf"):
            continue
        if full in seen:
            continue
        seen.add(full)
        text = a.get_text(" ", strip=True) or Path(urlparse(full).path).name or "pdf"
        links.append(LinkItem(url=full, text=sanitize_name(text, "pdf"), category="pluginfile"))

    return links


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    idx = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def compute_hash(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def download_pdf_response(
    resp: requests.Response,
    target_dir: Path,
    fallback_name: str,
    seen_hashes: Set[str],
    seen_urls: Set[str],
    seen_names: Set[str],
) -> Tuple[str, str]:
    final_url = resp.url
    if final_url in seen_urls:
        return "", "duplicate"

    head_bytes = b""
    temp_path: Optional[Path] = None
    try:
        filename = filename_from_response(resp, fallback_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path = unique_path(target_dir / filename)

        with temp_path.open("wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                if not head_bytes:
                    head_bytes = chunk[:8]
                out.write(chunk)

        if not head_bytes:
            head_bytes = temp_path.read_bytes()[:8]

        if not is_pdf_response(resp, head_bytes):
            temp_path.unlink(missing_ok=True)
            return filename, "not_pdf"

        digest = compute_hash(temp_path)
        lname = filename.lower()
        if digest in seen_hashes or lname in seen_names:
            temp_path.unlink(missing_ok=True)
            return filename, "duplicate"

        seen_hashes.add(digest)
        seen_urls.add(final_url)
        seen_names.add(lname)
        return filename, "downloaded"
    finally:
        resp.close()


def write_logs(logs_root: Path, rows: List[Dict[str, str]], failed: List[Dict[str, str]], summary: Dict[str, int]) -> None:
    with (logs_root / "download_log.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "filename", "status", "http_status"])
        writer.writeheader()
        writer.writerows(rows)

    with (logs_root / "failed_urls.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "filename", "status", "http_status"])
        writer.writeheader()
        writer.writerows(failed)

    with (logs_root / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    out_dir = Path(args.out)
    cookies_file = Path(args.cookies)
    pdf_root, logs_root = ensure_dirs(out_dir)

    log_rows: List[Dict[str, str]] = []
    failed_rows: List[Dict[str, str]] = []
    summary = {
        "total_links": 0,
        "processed_links": 0,
        "downloaded": 0,
        "duplicates": 0,
        "failed": 0,
    }

    try:
        cookie_jar = load_cookies(cookies_file)
    except Exception as exc:
        logging.error("Error cargando cookies: %s", exc)
        failed_rows.append({"url": args.url, "filename": "", "status": "cookies_error", "http_status": ""})
        summary["failed"] += 1
        write_logs(logs_root, log_rows, failed_rows, summary)
        return 1

    session = build_session(cookie_jar)

    try:
        page = request_url(session, args.url, stream=False)
    except Exception as exc:
        logging.error("Error solicitando URL base: %s", exc)
        failed_rows.append({"url": args.url, "filename": "", "status": "request_error", "http_status": ""})
        summary["failed"] += 1
        write_logs(logs_root, log_rows, failed_rows, summary)
        return 1

    if page.status_code >= 400:
        failed_rows.append({"url": args.url, "filename": "", "status": "http_error", "http_status": str(page.status_code)})
        summary["failed"] += 1
        write_logs(logs_root, log_rows, failed_rows, summary)
        return 1

    if is_login_response(page):
        failed_rows.append({"url": args.url, "filename": "", "status": "login_redirect", "http_status": str(page.status_code)})
        summary["failed"] += 1
        write_logs(logs_root, log_rows, failed_rows, summary)
        return 1

    links = extract_links(page.text, page.url)
    summary["total_links"] = len(links)
    logging.info("Enlaces candidatos detectados: %d", len(links))

    seen_source_urls: Set[str] = set()
    seen_final_urls: Set[str] = set()
    seen_hashes: Set[str] = set()
    seen_names: Set[str] = set()

    for item in links:
        if item.url in seen_source_urls:
            log_rows.append({"url": item.url, "filename": "", "status": "duplicate", "http_status": ""})
            summary["duplicates"] += 1
            continue
        seen_source_urls.add(item.url)
        summary["processed_links"] += 1

        if item.category == "folder_view":
            folder_name = sanitize_name(item.text, "folder")
            folder_dir = pdf_root / folder_name
            try:
                folder_resp = request_url(session, item.url, stream=False)
            except Exception:
                log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": ""})
                failed_rows.append({"url": item.url, "filename": "", "status": "request_error", "http_status": ""})
                summary["failed"] += 1
                continue

            if folder_resp.status_code >= 400 or is_login_response(folder_resp):
                log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": str(folder_resp.status_code)})
                failed_rows.append({"url": item.url, "filename": "", "status": "login_or_http_error", "http_status": str(folder_resp.status_code)})
                summary["failed"] += 1
                continue

            nested_links = parse_folder_pdf_links(folder_resp.text, folder_resp.url)
            if not nested_links:
                log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": str(folder_resp.status_code)})
                failed_rows.append({"url": item.url, "filename": "", "status": "no_pdf_in_folder", "http_status": str(folder_resp.status_code)})
                summary["failed"] += 1
                continue

            for nested in nested_links:
                try:
                    pdf_resp = request_url(session, nested.url, stream=True)
                except Exception:
                    log_rows.append({"url": nested.url, "filename": "", "status": "failed", "http_status": ""})
                    failed_rows.append({"url": nested.url, "filename": "", "status": "request_error", "http_status": ""})
                    summary["failed"] += 1
                    continue

                if pdf_resp.status_code >= 400 or is_login_response(pdf_resp):
                    log_rows.append({"url": nested.url, "filename": "", "status": "failed", "http_status": str(pdf_resp.status_code)})
                    failed_rows.append({"url": nested.url, "filename": "", "status": "login_or_http_error", "http_status": str(pdf_resp.status_code)})
                    summary["failed"] += 1
                    pdf_resp.close()
                    continue

                filename, status = download_pdf_response(
                    pdf_resp,
                    folder_dir,
                    nested.text,
                    seen_hashes,
                    seen_final_urls,
                    seen_names,
                )
                log_rows.append({"url": nested.url, "filename": filename, "status": status, "http_status": str(pdf_resp.status_code)})
                if status == "downloaded":
                    summary["downloaded"] += 1
                elif status == "duplicate":
                    summary["duplicates"] += 1
                else:
                    failed_rows.append({"url": nested.url, "filename": filename, "status": status, "http_status": str(pdf_resp.status_code)})
                    summary["failed"] += 1
            continue

        try:
            resp = request_url(session, item.url, stream=True)
        except Exception:
            log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": ""})
            failed_rows.append({"url": item.url, "filename": "", "status": "request_error", "http_status": ""})
            summary["failed"] += 1
            continue

        if resp.status_code >= 400 or is_login_response(resp):
            log_rows.append({"url": item.url, "filename": "", "status": "failed", "http_status": str(resp.status_code)})
            failed_rows.append({"url": item.url, "filename": "", "status": "login_or_http_error", "http_status": str(resp.status_code)})
            summary["failed"] += 1
            resp.close()
            continue

        filename, status = download_pdf_response(resp, pdf_root, item.text, seen_hashes, seen_final_urls, seen_names)
        log_rows.append({"url": item.url, "filename": filename, "status": status, "http_status": str(resp.status_code)})
        if status == "downloaded":
            summary["downloaded"] += 1
        elif status == "duplicate":
            summary["duplicates"] += 1
        else:
            failed_rows.append({"url": item.url, "filename": filename, "status": status, "http_status": str(resp.status_code)})
            summary["failed"] += 1

    write_logs(logs_root, log_rows, failed_rows, summary)
    logging.info(
        "Procesados=%s Descargados=%s Duplicados=%s Fallidos=%s",
        summary["processed_links"],
        summary["downloaded"],
        summary["duplicates"],
        summary["failed"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
