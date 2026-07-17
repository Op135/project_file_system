# -*- coding: utf-8 -*-
"""Zemax 优化操作数文档的下载、解析与本地缓存。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


DEFAULT_SOURCE_URL = (
    "https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v252/zh-Hans/"
    "OpticStudio_User_Guide/OpticStudio_Help/topics/Optimization_Operands_by_Category.html"
)
ALLOWED_HOST = "ansyshelp.ansys.com"
INDEX_FILENAME = "Optimization_Operands_by_Category.html"
MAX_HTML_BYTES = 5 * 1024 * 1024
OPERAND_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9]{3}(?![A-Za-z0-9])")


class OperandDataError(RuntimeError):
    """操作数资料下载或解析失败。"""


def normalize_source_url(raw_url: str) -> str:
    """把 Ansys 登录跳转链接规范化为可直接读取的公共帮助页链接。"""

    candidate = (raw_url or "").strip()
    if not candidate:
        raise OperandDataError("请输入 Ansys 操作数分类页面网址")
    if "://" not in candidate:
        candidate = f"https://{candidate.lstrip('/')}"

    parsed = urlparse(candidate)
    if (parsed.hostname or "").lower() != ALLOWED_HOST:
        raise OperandDataError(f"仅支持 {ALLOWED_HOST} 官方帮助页面")

    query = parse_qs(parsed.query)
    return_url = query.get("returnurl") or query.get("returnUrl")
    if return_url:
        path = unquote(return_url[0])
    else:
        path = parsed.path

    if not path.startswith("/"):
        path = f"/{path}"
    if path.startswith("/Views/"):
        path = f"/public{path}"
    elif path.startswith("/public/public/"):
        path = path[len("/public") :]

    if not path.startswith("/public/Views/Secured/Zemax/"):
        raise OperandDataError("网址不是 Zemax OpticStudio 官方帮助内容页")
    if Path(path).name.lower() != INDEX_FILENAME.lower():
        raise OperandDataError(f"请输入以 {INDEX_FILENAME} 结尾的分类页面网址")

    return urlunparse(("https", ALLOWED_HOST, path, "", "", ""))


def _clean_text(parts: list[str]) -> str:
    text = "".join(parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    return text.strip(" \n")


class _AnsysPageParser(HTMLParser):
    """提取 Ansys DITA 页面中的标题、分类链接和操作数表格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.category_links: list[dict[str, str]] = []
        self.rows: list[dict[str, Any]] = []

        self._h1_depth = 0
        self._h1_parts: list[str] = []
        self._link: dict[str, Any] | None = None
        self._table_depth = 0
        self._row_cells: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._strong_depth = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attrs(attrs)
        if tag == "h1":
            self._h1_depth = 1
            self._h1_parts = []
        elif self._h1_depth:
            self._h1_depth += 1

        if tag == "a":
            classes = set(attributes.get("class", "").split())
            href = attributes.get("href", "")
            if "xref" in classes and href:
                self._link = {"href": href, "parts": []}

        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth:
            self._row_cells = []
        elif tag in {"td", "th"} and self._table_depth and self._row_cells is not None:
            self._cell = {"parts": [], "strong": []}
        elif tag == "strong" and self._cell is not None:
            self._strong_depth += 1
            self._cell.setdefault("strong_parts", []).append([])
        elif tag == "br":
            self._append_text("\n")
        elif tag == "img":
            alt = attributes.get("alt", "").strip()
            if alt and alt.lower() not in {"icon", "image"}:
                self._append_text(f" {alt} ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._h1_depth:
            self.title = _clean_text(self._h1_parts)
            self._h1_depth = 0
        elif self._h1_depth:
            self._h1_depth -= 1

        if tag == "a" and self._link is not None:
            name = _clean_text(self._link["parts"])
            href = str(self._link["href"])
            if name and href.lower().endswith(".html"):
                self.category_links.append({"name": name, "href": href})
            self._link = None

        if tag == "strong" and self._cell is not None and self._strong_depth:
            strong_parts = self._cell.get("strong_parts", [])
            if strong_parts:
                value = _clean_text(strong_parts[-1])
                if value:
                    self._cell["strong"].append(value)
            self._strong_depth -= 1
        elif tag in {"td", "th"} and self._cell is not None and self._row_cells is not None:
            self._cell["text"] = _clean_text(self._cell.pop("parts"))
            self._cell.pop("strong_parts", None)
            self._row_cells.append(self._cell)
            self._cell = None
            self._strong_depth = 0
        elif tag == "tr" and self._row_cells is not None:
            if self._row_cells:
                self.rows.append({"cells": self._row_cells})
            self._row_cells = None
            self._cell = None
            self._strong_depth = 0
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def _append_text(self, data: str) -> None:
        if self._h1_depth:
            self._h1_parts.append(data)
        if self._link is not None:
            self._link["parts"].append(data)
        if self._cell is not None:
            self._cell["parts"].append(data)
            if self._strong_depth:
                strong_parts = self._cell.get("strong_parts", [])
                if strong_parts:
                    strong_parts[-1].append(data)


def parse_category_index(html: str, source_url: str) -> tuple[str, list[dict[str, str]]]:
    parser = _AnsysPageParser()
    parser.feed(html)

    seen: set[str] = set()
    categories: list[dict[str, str]] = []
    source_dir = source_url.rsplit("/", 1)[0] + "/"
    for item in parser.category_links:
        absolute_url = urljoin(source_dir, item["href"])
        parsed = urlparse(absolute_url)
        if parsed.hostname != ALLOWED_HOST or "/topics/" not in parsed.path:
            continue
        if Path(parsed.path).name.lower() == INDEX_FILENAME.lower() or absolute_url in seen:
            continue
        seen.add(absolute_url)
        categories.append({"name": item["name"], "url": absolute_url})

    if len(categories) < 10:
        raise OperandDataError("未在分类页中找到完整的操作数分类，请检查网址或页面版本")
    return parser.title or "分类优化操作数", categories


def parse_operand_page(html: str, category_name: str, page_url: str) -> dict[str, Any]:
    parser = _AnsysPageParser()
    parser.feed(html)
    operands: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in parser.rows:
        cells = row.get("cells", [])
        if len(cells) < 2:
            continue
        code_cell = cells[0].get("text", "").strip()
        description_cell = cells[1]
        description = description_cell.get("text", "").strip()
        if not description or len(code_cell) > 80 or code_cell.casefold() in {"name", "名称"}:
            continue
        codes = OPERAND_CODE_RE.findall(code_cell)
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            parameters = [
                value.strip()
                for value in description_cell.get("strong", [])
                if value.strip() and value.strip() != code
            ]
            operands.append(
                {
                    "code": code,
                    "description": description,
                    "parameters": list(dict.fromkeys(parameters)),
                }
            )

    if not operands:
        raise OperandDataError(f"分类“{category_name}”没有解析到操作数说明")
    return {
        "name": parser.title or category_name,
        "index_name": category_name,
        "url": page_url,
        "operands": operands,
    }


def _download_html(url: str, timeout: float = 30.0, retries: int = 2) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RayfineOperandLookup/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                body = response.read(MAX_HTML_BYTES + 1)
                if len(body) > MAX_HTML_BYTES:
                    raise OperandDataError("帮助页面过大，已停止读取")
                if "/account/" in final_url or "text/html" not in content_type.lower():
                    raise OperandDataError("Ansys 页面要求登录，未获取到帮助正文")
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace")
        except OperandDataError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise OperandDataError(f"无法读取 Ansys 帮助页面：{last_error}")


def build_operand_database(
    source_url: str,
    progress_callback: Callable[[int, int, str], None] | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """下载分类入口及所有分类详情，返回完整可序列化资料库。"""

    normalized_url = normalize_source_url(source_url)
    index_html = _download_html(normalized_url)
    index_title, category_links = parse_category_index(index_html, normalized_url)
    total = len(category_links)
    completed = 0
    parsed_categories: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 6))) as executor:
        future_map = {
            executor.submit(_download_and_parse_category, category): category for category in category_links
        }
        for future in as_completed(future_map):
            category = future_map[future]
            try:
                parsed_categories[category["url"]] = future.result()
            except Exception as exc:  # noqa: BLE001 - 汇总所有分类错误后统一报告
                errors.append(f"{category['name']}: {exc}")
            completed += 1
            if progress_callback:
                progress_callback(completed, total, category["name"])

    if errors:
        preview = "；".join(errors[:3])
        suffix = f"；另有 {len(errors) - 3} 项失败" if len(errors) > 3 else ""
        raise OperandDataError(f"资料更新不完整：{preview}{suffix}")

    categories = [parsed_categories[item["url"]] for item in category_links]
    operand_count = sum(len(category["operands"]) for category in categories)
    if operand_count < 50:
        raise OperandDataError("解析到的操作数数量异常，未覆盖旧资料")

    path_parts = urlparse(normalized_url).path.split("/")
    version = next((part for part in path_parts if re.fullmatch(r"v\d+", part)), "")
    language = next((part for part in path_parts if part in {"zh-Hans", "en", "ja", "de", "fr"}), "")
    return {
        "schema_version": 1,
        "title": index_title,
        "source_url": normalized_url,
        "version": version,
        "language": language,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category_count": len(categories),
        "operand_count": operand_count,
        "categories": categories,
    }


def _download_and_parse_category(category: dict[str, str]) -> dict[str, Any]:
    html = _download_html(category["url"])
    return parse_operand_page(html, category["name"], category["url"])


def load_operand_database(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return empty_operand_database()
    try:
        with cache_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
            raise ValueError("invalid database shape")
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return empty_operand_database()


def save_operand_database(cache_path: Path, data: dict[str, Any]) -> None:
    """同目录临时文件 + os.replace，避免更新中断损坏已有资料。"""

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{cache_path.name}.", suffix=".tmp", dir=cache_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, cache_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def update_operand_database(source_url: str, cache_path: Path) -> dict[str, Any]:
    data = build_operand_database(source_url)
    save_operand_database(cache_path, data)
    return data


def empty_operand_database() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "title": "分类优化操作数",
        "source_url": DEFAULT_SOURCE_URL,
        "version": "",
        "language": "",
        "updated_at": "",
        "category_count": 0,
        "operand_count": 0,
        "categories": [],
    }
