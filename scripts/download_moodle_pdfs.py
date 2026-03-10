#!/usr/bin/env python3
"""Descarga PDFs desde Moodle usando cookies Netscape, pensado para ejecución local."""

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
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REQUEST_TIMEOUT = (10, 90)
USER_AGENT = "moodle-pdf-downloader-local/3.0"
PDF_CANDIDATE_PATTERNS = (
    "mod/resource/view.php",
    "mod/folder/view.php",
    "mod/url/view.php",
    "pluginfile.php",
)
INTENSIVOS_TABS = [
    "Matemática I",
    "Matemática II",
    "Matemática III",
    "Matemática IV",
    "Física 100",
    "Física 110",
    "Física 120",
    "Física 130",
    "Química",
    "Programación",
    "CIAC",
]
HISTORICAL_YEARS = {"2020", "2021", "2022", "2023", "2024"}


@dataclass
class LinkItem:
    url: str
    text: str
    link_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Descargar PDFs desde Moodle autenticado")
    parser.add_argument("--url", required=True, help="URL del curso o sección de Moodle")
    parser.add_argument("--cookies", required=True, help="Ruta a cookies Netscape exportadas")
    parser.add_argument("--out", required=True, help="Carpeta de salida (crea pdfs/ y logs/)")
    parser.add_argument(
        "--intensivos-ciac",
        action="store_true",
        help="Recorre sub-solapas de INTENSIVOS CIAC y prioriza 'Descargar carpeta' en carpetas históricas 2020-2024",
    )
    parser.add_argument("--verbose", action="store_true", help="Activa logs detallados")
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
    zip_root = out_dir / "zips"
    logs_root = out_dir / "logs"
    pdf_root.mkdir(parents=True, exist_ok=True)
    zip_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    return pdf_root, zip_root, logs_root


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def load_cookies_netscape(cookies_file: Path) -> requests.cookies.RequestsCookieJar:
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
        domain = domain.lstrip(".")
        jar.set(name, value, domain=domain, path=path, secure=(secure.upper() == "TRUE"))

    if not jar:
        raise ValueError("Archivo de cookies vacío o no compatible con formato Netscape")
    return jar


def build_session(cookie_jar: requests.cookies.RequestsCookieJar) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.cookies.update(cookie_jar)
    return session


def classify_url(url: str) -> str:
    low = url.lower()
    if "mod/resource/view.php" in low:
        return "mod/resource/view.php"
    if "mod/folder/view.php" in low:
        return "mod/folder/view.php"
    if "mod/url/view.php" in low:
        return "mod/url/view.php"
    if "pluginfile.php" in low:
        return "pluginfile.php"
    if low.endswith(".pdf") or ".pdf?" in low:
        return "pdf_direct"
    return "other"


def detect_login_issue(resp: requests.Response) -> Optional[str]:
    redirect_chain = [h.url for h in resp.history] + [resp.url]
    lowered = [u.lower() for u in redirect_chain]

    if any("/login/index.php" in u for u in lowered):
        return "redirect_login_index"
    if any("/auth/oauth2/login.php" in u for u in lowered):
        return "redirect_oauth2_login"

    ctype = resp.headers.get("Content-Type", "").lower()
    if "text/html" in ctype:
        html = resp.text[:20000].lower()
        login_tokens = ["login", "acceder", "iniciar sesi", "username", "password", "oauth2"]
        if sum(token in html for token in login_tokens) >= 2:
            return "login_html_detected"

    return None


def request_get(session: requests.Session, url: str, stream: bool = False) -> requests.Response:
    return session.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT, stream=stream)


def extract_candidate_links(html: str, base_url: str) -> List[LinkItem]:
    soup = BeautifulSoup(html, "lxml")
    links: List[LinkItem] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        low = full_url.lower()
        if not full_url.startswith(("http://", "https://")):
            continue
        if not any(p in low for p in PDF_CANDIDATE_PATTERNS) and not low.endswith(".pdf") and ".pdf?" not in low:
            continue

        seen.add(full_url)
        text = a.get_text(" ", strip=True) or Path(urlparse(full_url).path).name or "recurso"
        links.append(LinkItem(url=full_url, text=sanitize_name(text, "recurso"), link_type=classify_url(full_url)))

    return links


def extract_tab_links(html: str, base_url: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    wanted = {normalize_text(name): name for name in INTENSIVOS_TABS}
    found: Dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        key = normalize_text(label)
        if key in wanted and wanted[key] not in found:
            found[wanted[key]] = urljoin(base_url, a["href"].strip())

    return found


def extract_historical_folder_links(html: str, base_url: str) -> List[LinkItem]:
    soup = BeautifulSoup(html, "lxml")
    links: List[LinkItem] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin(base_url, href)
        if "mod/folder/view.php" not in full_url.lower() or full_url in seen:
            continue

        text = a.get_text(" ", strip=True)
        haystack = f"{text} {full_url}"
        if not any(year in haystack for year in HISTORICAL_YEARS):
            continue

        seen.add(full_url)
        links.append(LinkItem(url=full_url, text=sanitize_name(text, "carpeta"), link_type="mod/folder/view.php"))

    return links


def find_download_folder_url(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        full_url = urljoin(base_url, href)
        if "download_folder.php" in full_url.lower() or "descargar carpeta" in text:
            return full_url
    return None


def extract_pdf_links_from_html(html: str, base_url: str) -> List[LinkItem]:
    soup = BeautifulSoup(html, "lxml")
    out: List[LinkItem] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a.get("href", "").strip())
        low = full_url.lower()
        if full_url in seen:
            continue
        if "pluginfile.php" in low or low.endswith(".pdf") or ".pdf?" in low:
            seen.add(full_url)
            text = a.get_text(" ", strip=True) or Path(urlparse(full_url).path).name or "pdf"
            out.append(LinkItem(url=full_url, text=sanitize_name(text, "pdf"), link_type=classify_url(full_url)))

    return out


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
        filename = Path(urlparse(resp.url).path).name or f"{sanitize_name(fallback, 'archivo')}.pdf"

    filename = sanitize_name(filename, "archivo.pdf")
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def is_pdf_response(resp: requests.Response, first_chunk: bytes) -> bool:
    ctype = resp.headers.get("Content-Type", "").lower()
    disp = resp.headers.get("Content-Disposition", "").lower()
    low_url = resp.url.lower()

    return (
        "application/pdf" in ctype
        or ".pdf" in disp
        or low_url.endswith(".pdf")
        or ".pdf?" in low_url
        or first_chunk.startswith(b"%PDF-")
    )


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    idx = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def console_report(url: str, link_type: str, status: str, final_url: str, content_type: str, reason: str) -> None:
    print(
        f"url_original={url} | tipo={link_type} | http_status={status} | final_url={final_url} | "
        f"content_type={content_type or '-'} | reason={reason}"
    )


def download_pdf(
    resp: requests.Response,
    target_dir: Path,
    fallback_name: str,
    known_hashes: Set[str],
) -> Tuple[str, str]:
    filename = filename_from_response(resp, fallback_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_path(target_dir / filename)

    first_chunk = b""
    with out_path.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk[:8]
            fh.write(chunk)

    if not first_chunk:
        first_chunk = out_path.read_bytes()[:8]

    if not is_pdf_response(resp, first_chunk):
        out_path.unlink(missing_ok=True)
        return filename, "not_pdf_response"

    digest = compute_sha256(out_path)
    if digest in known_hashes:
        out_path.unlink(missing_ok=True)
        return filename, "duplicate_sha256"

    known_hashes.add(digest)
    return out_path.name, "downloaded"


def download_zip(
    resp: requests.Response,
    target_dir: Path,
    fallback_name: str,
    known_hashes: Set[str],
) -> Tuple[str, str]:
    filename = filename_from_response(resp, fallback_name)
    if filename.lower().endswith(".pdf"):
        filename = f"{Path(filename).stem}.zip"
    if not filename.lower().endswith(".zip"):
        filename = f"{filename}.zip"

    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_path(target_dir / filename)
    with out_path.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                fh.write(chunk)

    ctype = resp.headers.get("Content-Type", "").lower()
    if "zip" not in ctype and out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        return filename, "not_zip_response"

    digest = compute_sha256(out_path)
    if digest in known_hashes:
        out_path.unlink(missing_ok=True)
        return filename, "duplicate_sha256"

    known_hashes.add(digest)
    return out_path.name, "downloaded_zip"


def write_logs(
    logs_dir: Path,
    download_rows: List[Dict[str, str]],
    failed_rows: List[Dict[str, str]],
    summary: Dict[str, int],
) -> None:
    download_log = logs_dir / "download_log.csv"
    failed_log = logs_dir / "failed_urls.csv"
    summary_file = logs_dir / "summary.json"

    headers = ["url_original", "tipo", "http_status", "final_url", "content_type", "reason", "filename"]

    with download_log.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(download_rows)

    with failed_log.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(failed_rows)

    with summary_file.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)


def iter_links_with_nested(base_links: Iterable[LinkItem], nested_links: Dict[str, List[LinkItem]]) -> Iterable[LinkItem]:
    for item in base_links:
        yield item
        for child in nested_links.get(item.url, []):
            yield child


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    out_dir = Path(args.out)
    cookies_path = Path(args.cookies)
    pdf_dir, zip_dir, logs_dir = ensure_dirs(out_dir)

    download_rows: List[Dict[str, str]] = []
    failed_rows: List[Dict[str, str]] = []
    summary = {
        "total_detected_links": 0,
        "processed_links": 0,
        "downloaded": 0,
        "downloaded_zips": 0,
        "duplicates": 0,
        "failed": 0,
    }

    try:
        cookie_jar = load_cookies_netscape(cookies_path)
    except Exception as exc:
        logging.error("No se pudieron cargar cookies: %s", exc)
        failed_rows.append(
            {
                "url_original": args.url,
                "tipo": "course_url",
                "http_status": "-",
                "final_url": "-",
                "content_type": "-",
                "reason": f"cookies_error:{exc}",
                "filename": "",
            }
        )
        summary["failed"] += 1
        write_logs(logs_dir, download_rows, failed_rows, summary)
        return 1

    session = build_session(cookie_jar)

    try:
        course_resp = request_get(session, args.url, stream=False)
    except Exception as exc:
        logging.error("No se pudo abrir URL base: %s", exc)
        failed_rows.append(
            {
                "url_original": args.url,
                "tipo": "course_url",
                "http_status": "-",
                "final_url": "-",
                "content_type": "-",
                "reason": f"request_error:{exc}",
                "filename": "",
            }
        )
        summary["failed"] += 1
        write_logs(logs_dir, download_rows, failed_rows, summary)
        return 1

    login_issue = detect_login_issue(course_resp)
    if login_issue:
        reason = f"auth_failed:{login_issue}"
        console_report(args.url, "course_url", str(course_resp.status_code), course_resp.url, course_resp.headers.get("Content-Type", ""), reason)
        failed_rows.append(
            {
                "url_original": args.url,
                "tipo": "course_url",
                "http_status": str(course_resp.status_code),
                "final_url": course_resp.url,
                "content_type": course_resp.headers.get("Content-Type", ""),
                "reason": reason,
                "filename": "",
            }
        )
        summary["failed"] += 1
        write_logs(logs_dir, download_rows, failed_rows, summary)
        return 1

    if args.intensivos_ciac:
        tab_links = extract_tab_links(course_resp.text, course_resp.url)
        logging.info("Sub-solapas detectadas INTENSIVOS CIAC: %s", sorted(tab_links.keys()))
        course_links: List[LinkItem] = []
        for tab_name in INTENSIVOS_TABS:
            if tab_name not in tab_links:
                logging.warning("No se encontró sub-solapa: %s", tab_name)
                continue
            tab_url = tab_links[tab_name]
            try:
                tab_resp = request_get(session, tab_url, stream=False)
            except Exception as exc:
                logging.error("No se pudo abrir sub-solapa %s: %s", tab_name, exc)
                continue
            historical = extract_historical_folder_links(tab_resp.text, tab_resp.url)
            for item in historical:
                item.text = sanitize_name(f"{tab_name}_{item.text}", "carpeta")
            course_links.extend(historical)
    else:
        course_links = extract_candidate_links(course_resp.text, course_resp.url)

    summary["total_detected_links"] = len(course_links)
    logging.info("Links detectados en curso: %s", len(course_links))

    known_hashes: Set[str] = set()
    seen_urls: Set[str] = set()

    for link in course_links:
        summary["processed_links"] += 1
        if link.url in seen_urls:
            summary["duplicates"] += 1
            reason = "duplicate_input_url"
            console_report(link.url, link.link_type, "-", "-", "-", reason)
            row = {
                "url_original": link.url,
                "tipo": link.link_type,
                "http_status": "-",
                "final_url": "-",
                "content_type": "-",
                "reason": reason,
                "filename": "",
            }
            download_rows.append(row)
            continue

        seen_urls.add(link.url)

        try:
            resp = request_get(session, link.url, stream=True)
        except Exception as exc:
            summary["failed"] += 1
            reason = f"request_error:{exc}"
            console_report(link.url, link.link_type, "-", "-", "-", reason)
            fail = {
                "url_original": link.url,
                "tipo": link.link_type,
                "http_status": "-",
                "final_url": "-",
                "content_type": "-",
                "reason": reason,
                "filename": "",
            }
            download_rows.append(fail)
            failed_rows.append(fail)
            continue

        status = str(resp.status_code)
        final_url = resp.url
        ctype = resp.headers.get("Content-Type", "")

        login_issue = detect_login_issue(resp)
        if login_issue:
            summary["failed"] += 1
            reason = f"auth_failed:{login_issue}"
            console_report(link.url, link.link_type, status, final_url, ctype, reason)
            fail = {
                "url_original": link.url,
                "tipo": link.link_type,
                "http_status": status,
                "final_url": final_url,
                "content_type": ctype,
                "reason": reason,
                "filename": "",
            }
            download_rows.append(fail)
            failed_rows.append(fail)
            resp.close()
            continue

        if resp.status_code >= 400:
            summary["failed"] += 1
            reason = "http_error"
            console_report(link.url, link.link_type, status, final_url, ctype, reason)
            fail = {
                "url_original": link.url,
                "tipo": link.link_type,
                "http_status": status,
                "final_url": final_url,
                "content_type": ctype,
                "reason": reason,
                "filename": "",
            }
            download_rows.append(fail)
            failed_rows.append(fail)
            resp.close()
            continue

        should_parse_html = "text/html" in ctype.lower() or link.link_type in {
            "mod/folder/view.php",
            "mod/resource/view.php",
            "mod/url/view.php",
        }

        if should_parse_html:
            html_text = resp.text
            resp.close()
            if args.intensivos_ciac and link.link_type == "mod/folder/view.php":
                download_folder_url = find_download_folder_url(html_text, final_url)
                if download_folder_url and download_folder_url not in seen_urls:
                    seen_urls.add(download_folder_url)
                    summary["processed_links"] += 1
                    try:
                        zip_resp = request_get(session, download_folder_url, stream=True)
                    except Exception as exc:
                        summary["failed"] += 1
                        reason = f"request_error:{exc}"
                        console_report(download_folder_url, "download_folder", "-", "-", "-", reason)
                        fail = {
                            "url_original": download_folder_url,
                            "tipo": "download_folder",
                            "http_status": "-",
                            "final_url": "-",
                            "content_type": "-",
                            "reason": reason,
                            "filename": "",
                        }
                        download_rows.append(fail)
                        failed_rows.append(fail)
                        continue

                    zip_status = str(zip_resp.status_code)
                    zip_final = zip_resp.url
                    zip_ctype = zip_resp.headers.get("Content-Type", "")
                    if zip_resp.status_code >= 400:
                        summary["failed"] += 1
                        reason = "http_error"
                        console_report(download_folder_url, "download_folder", zip_status, zip_final, zip_ctype, reason)
                        fail = {
                            "url_original": download_folder_url,
                            "tipo": "download_folder",
                            "http_status": zip_status,
                            "final_url": zip_final,
                            "content_type": zip_ctype,
                            "reason": reason,
                            "filename": "",
                        }
                        download_rows.append(fail)
                        failed_rows.append(fail)
                        zip_resp.close()
                        continue

                    filename, dl_status = download_zip(zip_resp, zip_dir, link.text, known_hashes)
                    zip_resp.close()
                    if dl_status == "downloaded_zip":
                        summary["downloaded_zips"] += 1
                    elif dl_status.startswith("duplicate"):
                        summary["duplicates"] += 1
                    else:
                        summary["failed"] += 1

                    console_report(download_folder_url, "download_folder", zip_status, zip_final, zip_ctype, dl_status)
                    row = {
                        "url_original": download_folder_url,
                        "tipo": "download_folder",
                        "http_status": zip_status,
                        "final_url": zip_final,
                        "content_type": zip_ctype,
                        "reason": dl_status,
                        "filename": filename,
                    }
                    download_rows.append(row)
                    if dl_status in {"downloaded_zip", "duplicate_sha256"}:
                        continue

            nested_pdf_links = extract_pdf_links_from_html(html_text, final_url)
            if not nested_pdf_links:
                summary["failed"] += 1
                reason = "html_without_pdf_links"
                console_report(link.url, link.link_type, status, final_url, ctype, reason)
                fail = {
                    "url_original": link.url,
                    "tipo": link.link_type,
                    "http_status": status,
                    "final_url": final_url,
                    "content_type": ctype,
                    "reason": reason,
                    "filename": "",
                }
                download_rows.append(fail)
                failed_rows.append(fail)
                continue

            reason = f"html_with_{len(nested_pdf_links)}_pdf_links"
            console_report(link.url, link.link_type, status, final_url, ctype, reason)
            download_rows.append(
                {
                    "url_original": link.url,
                    "tipo": link.link_type,
                    "http_status": status,
                    "final_url": final_url,
                    "content_type": ctype,
                    "reason": reason,
                    "filename": "",
                }
            )

            for nested in nested_pdf_links:
                summary["processed_links"] += 1
                if nested.url in seen_urls:
                    summary["duplicates"] += 1
                    reason = "duplicate_nested_url"
                    console_report(nested.url, nested.link_type, "-", "-", "-", reason)
                    download_rows.append(
                        {
                            "url_original": nested.url,
                            "tipo": nested.link_type,
                            "http_status": "-",
                            "final_url": "-",
                            "content_type": "-",
                            "reason": reason,
                            "filename": "",
                        }
                    )
                    continue

                seen_urls.add(nested.url)
                try:
                    nested_resp = request_get(session, nested.url, stream=True)
                except Exception as exc:
                    summary["failed"] += 1
                    reason = f"request_error:{exc}"
                    console_report(nested.url, nested.link_type, "-", "-", "-", reason)
                    fail = {
                        "url_original": nested.url,
                        "tipo": nested.link_type,
                        "http_status": "-",
                        "final_url": "-",
                        "content_type": "-",
                        "reason": reason,
                        "filename": "",
                    }
                    download_rows.append(fail)
                    failed_rows.append(fail)
                    continue

                nested_login = detect_login_issue(nested_resp)
                nested_status = str(nested_resp.status_code)
                nested_final = nested_resp.url
                nested_ctype = nested_resp.headers.get("Content-Type", "")
                if nested_login or nested_resp.status_code >= 400:
                    summary["failed"] += 1
                    reason = f"auth_failed:{nested_login}" if nested_login else "http_error"
                    console_report(nested.url, nested.link_type, nested_status, nested_final, nested_ctype, reason)
                    fail = {
                        "url_original": nested.url,
                        "tipo": nested.link_type,
                        "http_status": nested_status,
                        "final_url": nested_final,
                        "content_type": nested_ctype,
                        "reason": reason,
                        "filename": "",
                    }
                    download_rows.append(fail)
                    failed_rows.append(fail)
                    nested_resp.close()
                    continue

                filename, dl_status = download_pdf(nested_resp, pdf_dir, nested.text, known_hashes)
                nested_resp.close()
                if dl_status == "downloaded":
                    summary["downloaded"] += 1
                elif dl_status.startswith("duplicate"):
                    summary["duplicates"] += 1
                else:
                    summary["failed"] += 1

                console_report(nested.url, nested.link_type, nested_status, nested_final, nested_ctype, dl_status)
                row = {
                    "url_original": nested.url,
                    "tipo": nested.link_type,
                    "http_status": nested_status,
                    "final_url": nested_final,
                    "content_type": nested_ctype,
                    "reason": dl_status,
                    "filename": filename,
                }
                download_rows.append(row)
                if dl_status != "downloaded" and not dl_status.startswith("duplicate"):
                    failed_rows.append(row)
            continue

        filename, dl_status = download_pdf(resp, pdf_dir, link.text, known_hashes)
        resp.close()

        if dl_status == "downloaded":
            summary["downloaded"] += 1
        elif dl_status.startswith("duplicate"):
            summary["duplicates"] += 1
        else:
            summary["failed"] += 1

        console_report(link.url, link.link_type, status, final_url, ctype, dl_status)
        row = {
            "url_original": link.url,
            "tipo": link.link_type,
            "http_status": status,
            "final_url": final_url,
            "content_type": ctype,
            "reason": dl_status,
            "filename": filename,
        }
        download_rows.append(row)
        if dl_status != "downloaded" and not dl_status.startswith("duplicate"):
            failed_rows.append(row)

    write_logs(logs_dir, download_rows, failed_rows, summary)
    logging.info("Resumen: %s", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
