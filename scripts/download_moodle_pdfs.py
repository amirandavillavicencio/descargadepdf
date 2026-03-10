#!/usr/bin/env python3
"""Descarga PDFs desde un HTML exportado de Moodle/Aula USM."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REQUEST_TIMEOUT = (10, 60)
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "moodle-pdf-backup/1.0"


@dataclass
class LinkItem:
    source_page: str
    url: str
    link_text: str
    category: str
    section: str


@dataclass
class DownloadRecord:
    index: int
    source_page: str
    link_text: str
    original_url: str
    resolved_url: str
    final_url: str
    content_type: str
    filename: str
    output_path: str
    status: str
    http_status: str
    message: str


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")


def load_html(html_path: Path) -> Tuple[str, BeautifulSoup]:
    parser = "lxml"
    try:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html_text, parser)
    except Exception:
        parser = "html.parser"
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html_text, parser)
    logging.info("HTML cargado (%s) con parser=%s", html_path, parser)
    return html_text, soup


def detect_base_url(soup: BeautifulSoup, explicit_base: Optional[str]) -> Optional[str]:
    if explicit_base:
        return explicit_base.strip()

    base_tag = soup.find("base", href=True)
    if base_tag:
        return str(base_tag["href"]).strip()

    canonical = soup.find("link", rel=lambda v: v and "canonical" in v, href=True)
    if canonical:
        return str(canonical["href"]).strip()

    for meta_name in ["og:url", "twitter:url"]:
        tag = soup.find("meta", attrs={"property": meta_name}) or soup.find(
            "meta", attrs={"name": meta_name}
        )
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def sanitize_name(value: str, fallback: str = "archivo") -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.strip().replace("\n", " ")
    value = re.sub(r"[\\/:*?\"<>|]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = re.sub(r"_+", "_", value)
    return value[:150] if value else fallback


def infer_link_text(anchor) -> str:
    text = anchor.get_text(" ", strip=True)
    if text:
        return text
    for attr in ["title", "aria-label", "data-original-title"]:
        v = anchor.get(attr)
        if v:
            return str(v).strip()
    href = anchor.get("href", "")
    name = Path(urlparse(href).path).name
    return unquote(name) or "sin_nombre"


def classify_link(url: str) -> str:
    p = urlparse(url)
    path = p.path.lower()
    if path.endswith(".pdf"):
        return "pdf_directo"
    if "pluginfile.php" in path:
        return "pluginfile"
    if "draftfile.php" in path:
        return "draftfile"
    if "mod/resource/view.php" in path:
        return "resource_view"
    if "mod/folder/view.php" in path:
        return "folder_view"
    if "mod/url/view.php" in path:
        return "url_view"
    return "otros"


def infer_section(anchor) -> str:
    section_container = anchor.find_parent(
        ["li", "section", "div"],
        class_=re.compile(r"section|topic|content|activity|modtype", re.I),
    )
    if not section_container:
        return "recursos"

    heading = section_container.find(["h2", "h3", "h4", "strong"])
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return sanitize_name(text, "recursos")
    return "recursos"


def extract_links(soup: BeautifulSoup, base_url: Optional[str]) -> List[LinkItem]:
    links: List[LinkItem] = []
    ignored_prefixes = ("javascript:", "mailto:", "#")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.lower().startswith(ignored_prefixes):
            continue
        if "logout" in href.lower():
            continue

        absolute = urljoin(base_url, href) if base_url else href
        if not absolute.startswith(("http://", "https://")):
            continue

        link_text = sanitize_name(infer_link_text(a), "sin_nombre")
        section = infer_section(a)
        category = classify_link(absolute)
        links.append(
            LinkItem(
                source_page=base_url or "html_input",
                url=absolute,
                link_text=link_text,
                category=category,
                section=section,
            )
        )

    return links


def parse_cookies(raw: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    netscape_like = any("\t" in ln for ln in lines if not ln.startswith("#"))
    if netscape_like:
        for line in lines:
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5].strip()
                value = parts[6].strip()
                if name:
                    cookies[name] = value
    else:
        flat = raw.replace("\n", ";")
        for token in flat.split(";"):
            token = token.strip()
            if not token or "=" not in token:
                continue
            name, value = token.split("=", 1)
            name = name.strip()
            if name:
                cookies[name] = value.strip()

    if not cookies:
        raise ValueError("No se pudieron parsear cookies desde MOODLE_COOKIES")

    return cookies


def build_session(cookies: Dict[str, str]) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.2,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    session.cookies.update(cookies)
    return session


def detect_pdf_candidates(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    candidates: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        lower = href.lower()
        text = a.get_text(" ", strip=True).lower()
        if ".pdf" in lower or "pluginfile.php" in lower or "download" in text:
            candidates.add(href)
    return list(candidates)


def parse_folder_page(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: Set[str] = set()
    for a in soup.select(".fp-filename-icon a[href], .foldertree a[href], a[href]"):
        href = urljoin(base_url, a.get("href", ""))
        if ".pdf" in href.lower() or "pluginfile.php" in href.lower():
            urls.add(href)
    return list(urls)


def compute_hash(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    sha = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def pick_output_folder(base_out: Path, item: LinkItem) -> Path:
    category_map = {
        "resource_view": "recursos",
        "folder_view": "carpetas",
        "url_view": "enlaces",
        "pdf_directo": "pdf_directos",
        "pluginfile": "recursos",
        "draftfile": "recursos",
        "otros": "recursos",
    }
    top = category_map.get(item.category, "recursos")
    section = sanitize_name(item.section, "recursos")
    return base_out / "pdfs" / top / section


def pick_filename(response: requests.Response, fallback_url: str, link_text: str) -> str:
    cd = response.headers.get("Content-Disposition", "")
    filename = ""
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd)
    if match:
        filename = unquote(match.group(1).strip().strip('"'))

    if not filename:
        filename = Path(urlparse(response.url or fallback_url).path).name

    if not filename:
        filename = f"{sanitize_name(link_text, 'archivo')}.pdf"

    filename = sanitize_name(filename, "archivo.pdf")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return filename


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def resolve_resource(
    item: LinkItem,
    session: requests.Session,
    only_pdf: bool,
    delay: float,
) -> Tuple[List[Tuple[requests.Response, str]], List[str]]:
    resolved: List[Tuple[requests.Response, str]] = []
    errors: List[str] = []

    try:
        response = session.get(item.url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        time.sleep(delay)
    except Exception as exc:
        errors.append(f"Error de red: {exc}")
        return resolved, errors

    ctype = response.headers.get("Content-Type", "").lower()
    final_url = response.url

    if response.status_code >= 400:
        errors.append(f"HTTP {response.status_code}")
        return resolved, errors

    if "application/pdf" in ctype or final_url.lower().endswith(".pdf"):
        resolved.append((response, final_url))
        return resolved, errors

    if only_pdf:
        return resolved, errors

    if "text/html" in ctype or "application/xhtml+xml" in ctype or not ctype:
        html = response.text
        candidate_urls: List[str] = []
        if item.category == "folder_view":
            candidate_urls.extend(parse_folder_page(html, final_url))
        candidate_urls.extend(detect_pdf_candidates(html, final_url))

        seen = set()
        for candidate in candidate_urls:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                candidate_resp = session.get(candidate, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                time.sleep(delay)
            except Exception as exc:
                errors.append(f"Fallo candidato {candidate}: {exc}")
                continue
            c_c = candidate_resp.headers.get("Content-Type", "").lower()
            if candidate_resp.status_code < 400 and (
                "application/pdf" in c_c or candidate_resp.url.lower().endswith(".pdf")
            ):
                resolved.append((candidate_resp, candidate))

    return resolved, errors


def ensure_dirs(out_dir: Path) -> Tuple[Path, Path]:
    pdf_root = out_dir / "pdfs"
    logs_root = out_dir / "logs"
    pdf_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    return pdf_root, logs_root


def write_logs(logs_root: Path, records: List[DownloadRecord], failed: List[Dict[str, str]]) -> None:
    log_csv = logs_root / "download_log.csv"
    failed_csv = logs_root / "failed_urls.csv"

    with log_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "index",
                "source_page",
                "link_text",
                "original_url",
                "resolved_url",
                "final_url",
                "content_type",
                "filename",
                "output_path",
                "status",
                "http_status",
                "message",
            ],
        )
        writer.writeheader()
        for r in records:
            writer.writerow(r.__dict__)

    with failed_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["original_url", "resolved_url", "status", "http_status", "error"],
        )
        writer.writeheader()
        for row in failed:
            writer.writerow(row)


def summarize_results(logs_root: Path, summary: Dict[str, object]) -> None:
    summary_path = logs_root / "download_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def process_link(
    index: int,
    item: LinkItem,
    session: requests.Session,
    out_dir: Path,
    only_pdf: bool,
    delay: float,
    state: Dict[str, object],
    lock: threading.Lock,
) -> Tuple[List[DownloadRecord], List[Dict[str, str]], Dict[str, int]]:
    records: List[DownloadRecord] = []
    failed_rows: List[Dict[str, str]] = []
    counters = {"downloaded": 0, "duplicates": 0, "failures": 0}

    with lock:
        processed_urls: Set[str] = state["processed_urls"]  # type: ignore[assignment]
        if item.url in processed_urls:
            records.append(
                DownloadRecord(
                    index=index,
                    source_page=item.source_page,
                    link_text=item.link_text,
                    original_url=item.url,
                    resolved_url=item.url,
                    final_url=item.url,
                    content_type="",
                    filename="",
                    output_path="",
                    status="duplicate_skipped",
                    http_status="",
                    message="URL original ya procesada",
                )
            )
            counters["duplicates"] += 1
            return records, failed_rows, counters
        processed_urls.add(item.url)

    responses, errors = resolve_resource(item, session, only_pdf, delay)

    if not responses:
        msg = "; ".join(errors) if errors else "No se detectó PDF en recurso"
        records.append(
            DownloadRecord(
                index=index,
                source_page=item.source_page,
                link_text=item.link_text,
                original_url=item.url,
                resolved_url=item.url,
                final_url="",
                content_type="",
                filename="",
                output_path="",
                status="failed",
                http_status="",
                message=msg,
            )
        )
        failed_rows.append(
            {
                "original_url": item.url,
                "resolved_url": item.url,
                "status": "failed",
                "http_status": "",
                "error": msg,
            }
        )
        counters["failures"] += 1
        return records, failed_rows, counters

    for response, resolved_url in responses:
        final_url = response.url
        content_type = response.headers.get("Content-Type", "")
        http_status = str(response.status_code)
        out_folder = pick_output_folder(out_dir, item)
        out_folder.mkdir(parents=True, exist_ok=True)
        filename = pick_filename(response, final_url, item.link_text)
        output_path = unique_path(out_folder / filename)

        with lock:
            final_urls: Set[str] = state["final_urls"]  # type: ignore[assignment]
            hashes: Set[str] = state["hashes"]  # type: ignore[assignment]
            filename_size: Set[Tuple[str, int]] = state["filename_size"]  # type: ignore[assignment]
            if final_url in final_urls:
                records.append(
                    DownloadRecord(
                        index=index,
                        source_page=item.source_page,
                        link_text=item.link_text,
                        original_url=item.url,
                        resolved_url=resolved_url,
                        final_url=final_url,
                        content_type=content_type,
                        filename=filename,
                        output_path=str(output_path),
                        status="duplicate_skipped",
                        http_status=http_status,
                        message="URL final duplicada",
                    )
                )
                counters["duplicates"] += 1
                continue
            final_urls.add(final_url)

        try:
            with output_path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        except Exception as exc:
            counters["failures"] += 1
            failed_rows.append(
                {
                    "original_url": item.url,
                    "resolved_url": resolved_url,
                    "status": "failed",
                    "http_status": http_status,
                    "error": str(exc),
                }
            )
            records.append(
                DownloadRecord(
                    index=index,
                    source_page=item.source_page,
                    link_text=item.link_text,
                    original_url=item.url,
                    resolved_url=resolved_url,
                    final_url=final_url,
                    content_type=content_type,
                    filename=filename,
                    output_path=str(output_path),
                    status="failed",
                    http_status=http_status,
                    message=f"Error al guardar: {exc}",
                )
            )
            continue

        digest = compute_hash(output_path)
        file_size = output_path.stat().st_size
        key = (filename.lower(), file_size)
        with lock:
            hashes = state["hashes"]  # type: ignore[assignment]
            filename_size = state["filename_size"]  # type: ignore[assignment]
            if digest in hashes or key in filename_size:
                output_path.unlink(missing_ok=True)
                records.append(
                    DownloadRecord(
                        index=index,
                        source_page=item.source_page,
                        link_text=item.link_text,
                        original_url=item.url,
                        resolved_url=resolved_url,
                        final_url=final_url,
                        content_type=content_type,
                        filename=filename,
                        output_path=str(output_path),
                        status="duplicate_skipped",
                        http_status=http_status,
                        message="Hash o nombre+tamaño duplicado",
                    )
                )
                counters["duplicates"] += 1
                continue
            hashes.add(digest)
            filename_size.add(key)

        counters["downloaded"] += 1
        records.append(
            DownloadRecord(
                index=index,
                source_page=item.source_page,
                link_text=item.link_text,
                original_url=item.url,
                resolved_url=resolved_url,
                final_url=final_url,
                content_type=content_type,
                filename=filename,
                output_path=str(output_path),
                status="downloaded",
                http_status=http_status,
                message="OK",
            )
        )

    return records, failed_rows, counters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descarga PDFs desde HTML exportado de Moodle")
    parser.add_argument("--html", required=True, help="Ruta del HTML exportado")
    parser.add_argument("--out", required=True, help="Carpeta de salida")
    parser.add_argument("--max-workers", type=int, default=4, help="Número de workers")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay entre requests")
    parser.add_argument("--only-pdf", action="store_true", help="Solo intentar PDFs directos")
    parser.add_argument("--base-url", default=None, help="URL base opcional")
    parser.add_argument("--verbose", action="store_true", help="Logs detallados")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    html_path = Path(args.html)
    out_dir = Path(args.out)

    if not html_path.exists():
        logging.error("No existe HTML de entrada: %s", html_path)
        return 1

    raw_cookies = os.getenv("MOODLE_COOKIES", "").strip()
    if not raw_cookies:
        logging.error("Falta variable de entorno MOODLE_COOKIES")
        return 1

    try:
        cookies = parse_cookies(raw_cookies)
    except Exception as exc:
        logging.error("Error parseando MOODLE_COOKIES: %s", exc)
        return 1

    html_text, soup = load_html(html_path)
    base_url = detect_base_url(soup, args.base_url)
    logging.info("Base URL detectada: %s", base_url or "(no detectada)")

    links = extract_links(soup, base_url)
    total_links_found = len(links)
    if total_links_found == 0:
        logging.warning("No se encontraron enlaces procesables en el HTML")

    ensure_dirs(out_dir)
    session = build_session(cookies)

    start_ts = datetime.now(timezone.utc)
    start_time = time.time()
    all_records: List[DownloadRecord] = []
    failed_rows: List[Dict[str, str]] = []

    state: Dict[str, object] = {
        "processed_urls": set(),
        "final_urls": set(),
        "hashes": set(),
        "filename_size": set(),
    }
    lock = threading.Lock()

    total_processed = 0
    total_downloaded = 0
    total_duplicates = 0
    total_failures = 0

    workers = max(1, int(args.max_workers))
    logging.info("Procesando %s enlaces con %s worker(s)", total_links_found, workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_link,
                i,
                link,
                session,
                out_dir,
                args.only_pdf,
                args.delay,
                state,
                lock,
            ): i
            for i, link in enumerate(links, start=1)
        }
        for future in as_completed(futures):
            recs, fails, counters = future.result()
            all_records.extend(recs)
            failed_rows.extend(fails)
            total_processed += 1
            total_downloaded += counters["downloaded"]
            total_duplicates += counters["duplicates"]
            total_failures += counters["failures"]
            logging.info(
                "Progreso: %s/%s | descargados=%s duplicados=%s fallos=%s",
                total_processed,
                total_links_found,
                total_downloaded,
                total_duplicates,
                total_failures,
            )

    logs_root = out_dir / "logs"
    write_logs(logs_root, sorted(all_records, key=lambda r: r.index), failed_rows)

    end_ts = datetime.now(timezone.utc)
    elapsed = round(time.time() - start_time, 2)
    summary = {
        "total_links_found": total_links_found,
        "total_links_processed": total_processed,
        "total_pdfs_downloaded": total_downloaded,
        "total_duplicates_skipped": total_duplicates,
        "total_failures": total_failures,
        "start_time": start_ts.isoformat(),
        "end_time": end_ts.isoformat(),
        "elapsed_seconds": elapsed,
    }
    summarize_results(logs_root, summary)

    logging.info("Finalizado. Descargados=%s | Duplicados=%s | Fallos=%s", total_downloaded, total_duplicates, total_failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
