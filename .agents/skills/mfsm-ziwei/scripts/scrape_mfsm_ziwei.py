from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag


DEFAULT_SOURCE_TEMPLATE = "http://mfsm.kvov.com/fx/{date}/"
GENDERS = {
    "male": {"code": 1, "label": "男命"},
    "female": {"code": 2, "label": "女命"},
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class ScrapeConfig:
    date: str
    gender: str
    gender_code: int
    gender_label: str
    source_url: str
    out_dir: Path
    delay: float


@dataclass(frozen=True)
class TimeSlot:
    code: int
    label: str
    entry_url: str
    ziwei_url: str
    bazi_url: str


def normalize_date_or_url(value: str) -> tuple[str, str]:
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
    if not date_match:
        raise ValueError(f"Cannot find YYYY-MM-DD in {value!r}")

    date = date_match.group(1)
    if re.match(r"https?://", value):
        source_url = value.rstrip("/") + "/"
    else:
        source_url = DEFAULT_SOURCE_TEMPLATE.format(date=date)
    return date, source_url


def build_config(args: argparse.Namespace) -> ScrapeConfig:
    date, source_url = normalize_date_or_url(args.date_or_url)
    gender_info = GENDERS[args.gender]
    output_root = Path(args.output_root).resolve()

    if args.output_dir:
        candidate = Path(args.output_dir)
        out_dir = candidate if candidate.is_absolute() else output_root / candidate
    else:
        out_dir = output_root / f"mfsm-{date}-{args.gender}-ziwei"

    return ScrapeConfig(
        date=date,
        gender=args.gender,
        gender_code=gender_info["code"],
        gender_label=gender_info["label"],
        source_url=source_url,
        out_dir=out_dir,
        delay=args.delay,
    )


def fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def clean_text(node: Tag | None, sep: str = "\n") -> str:
    if node is None:
        return ""
    text = node.get_text(sep, strip=True)
    text = re.sub(r"[\ue000-\uf8ff]", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_label(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(". ") or "slot"


def parse_time_slots(index_html: str, config: ScrapeConfig) -> list[TimeSlot]:
    soup = BeautifulSoup(index_html, "lxml")
    slots_by_code: dict[int, TimeSlot] = {}

    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        match = re.search(r"/fx/\d{4}-\d{2}-\d{2}/mfsms-(\d+)\.html", href)
        if not match:
            continue

        code = int(match.group(1))
        label = clean_text(link, " ")
        entry_url = urljoin(config.source_url, href)
        ziwei_url = urljoin(config.source_url, f"mfsmm-{code}-{config.gender_code}.html")
        bazi_url = urljoin(config.source_url, f"bzmp-{code}-{config.gender_code}.html")
        slots_by_code.setdefault(
            code,
            TimeSlot(
                code=code,
                label=label,
                entry_url=entry_url,
                ziwei_url=ziwei_url,
                bazi_url=bazi_url,
            ),
        )

    return [slots_by_code[key] for key in sorted(slots_by_code)]


def parse_related_links(soup: BeautifulSoup, source_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        button = anchor.find("button")
        if not button:
            continue
        text = clean_text(button, " ")
        if text not in {"紫微斗数", "生辰八字", "八字命盘", "一生富贵", "一生姻缘", "大运分析", "流年分析"}:
            continue
        url = urljoin(source_url, str(anchor["href"]))
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        links.append({"label": text, "url": url})

    return links


def parse_analysis_table(soup: BeautifulSoup) -> tuple[dict[str, Any], list[dict[str, str]]]:
    table = None
    for candidate in soup.find_all("table"):
        if "紫微斗数算命一生运势分析" in clean_text(candidate, " "):
            table = candidate
            break

    if table is None:
        return {"title": "", "advertised_count": None, "visible_count": 0}, []

    th = table.find("th")
    title = clean_text(th, " ")
    advertised_match = re.search(r"\((\d+)条\)", title)
    advertised_count = int(advertised_match.group(1)) if advertised_match else None

    rows: list[dict[str, str]] = []
    for index, tr in enumerate(table.find_all("tr")[1:], start=1):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        rows.append(
            {
                "index": str(index),
                "analysis": clean_text(cells[0]),
                "confidence": clean_text(cells[1], " "),
                "basis": clean_text(cells[2]),
            }
        )

    return (
        {
            "title": title,
            "advertised_count": advertised_count,
            "visible_count": len(rows),
            "login_more_text_present": "登录显示更多" in title,
        },
        rows,
    )


def star_name(div: Tag) -> str:
    parts: list[str] = []
    for child in div.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name == "br":
            parts.append(" ")
    return "".join(parts).strip()


def parse_star(div: Tag) -> dict[str, str]:
    classes = set(div.get("class", []))
    if "zw1" in classes:
        level = "主星"
    elif "zw2" in classes:
        level = "辅星"
    else:
        level = "杂曜"

    brightness_tag = div.find("span", class_="zw9")
    transform_tag = div.find("span", class_="zw6")

    return {
        "name": star_name(div),
        "level": level,
        "brightness": clean_text(brightness_tag, " "),
        "transform": clean_text(transform_tag, " "),
        "transform_title": str(transform_tag.get("title", "")).strip() if transform_tag else "",
        "raw_text": clean_text(div, " "),
    }


def parse_chart_table(soup: BeautifulSoup) -> tuple[dict[str, Any], str]:
    chart = soup.select_one("table.zwds")
    if chart is None:
        return {"meta": {}, "palaces": []}, ""

    meta: dict[str, str] = {}
    center = chart.find("td", attrs={"colspan": "2", "rowspan": "2"})
    if center:
        center_copy = BeautifulSoup(str(center), "lxml")
        for select in center_copy.find_all("select"):
            select.decompose()
        for line in clean_text(center_copy, "\n").splitlines():
            line = line.strip()
            if not line or line in {"类似命盘讨论分析", "星尘算命-免费在线算命"}:
                continue
            if "：" in line:
                key, value = line.split("：", 1)
                meta[key.strip()] = value.strip()
            elif line.endswith("局"):
                meta["五行局"] = line

    palaces: list[dict[str, Any]] = []
    for td in chart.find_all("td"):
        if td.get("colspan") or td.get("rowspan"):
            continue
        right = td.find("div", class_="right-zw")
        left = td.find("div", class_="left-zw")
        if right is None or left is None:
            continue

        palace_tag = right.select_one(".right-t .zw10, .right-t .zw5")
        palace_name = clean_text(palace_tag, " ")
        if not palace_name:
            continue

        right_t_values = [
            clean_text(div, " ")
            for div in right.select(".right-t > div")
            if clean_text(div, " ") and div is not palace_tag
        ]
        age_range = right_t_values[0] if len(right_t_values) >= 1 else ""
        phase = right_t_values[1] if len(right_t_values) >= 2 else ""
        stem_branch = right_t_values[2] if len(right_t_values) >= 3 else ""

        stars = [
            parse_star(div)
            for div in left.find_all("div", recursive=False)
            if {"zw1", "zw2", "zw3"} & set(div.get("class", []))
        ]
        right_x = right.find("div", class_="right-x")
        aux = [
            clean_text(div, " ")
            for div in (right_x.find_all("div", class_="zw4") if right_x else [])
            if clean_text(div, " ")
        ]
        badges = [
            clean_text(badge, " ")
            for badge in td.find_all(class_=lambda value: value and "layui-badge" in value.split())
            if clean_text(badge, " ") not in {"大运", "流年"}
        ]

        palaces.append(
            {
                "name": palace_name,
                "age_range": age_range,
                "phase": phase,
                "stem_branch": stem_branch,
                "is_life_palace": palace_name == "命宫",
                "is_body_palace": "身宫" in badges,
                "badges": badges,
                "stars": stars,
                "auxiliary_notes": aux,
                "raw_text": clean_text(td),
            }
        )

    return {"meta": meta, "palaces": palaces}, str(chart)


def parse_notes(soup: BeautifulSoup) -> list[str]:
    notes: list[str] = []
    seen: set[str] = set()

    for blockquote in soup.find_all("blockquote"):
        text = clean_text(blockquote)
        if not text:
            continue
        if any(skip in text for skip in ["必须登录", "严禁复制用于商业用途", "收藏本网页"]):
            continue
        if text in seen:
            continue
        seen.add(text)
        notes.append(text)

    return notes


def parse_page(slot: TimeSlot, html_text: str, config: ScrapeConfig) -> tuple[dict[str, Any], str]:
    soup = BeautifulSoup(html_text, "lxml")
    analysis_info, analysis_rows = parse_analysis_table(soup)
    chart, chart_html = parse_chart_table(soup)

    title = clean_text(soup.title, " ")
    h1 = clean_text(soup.find("h1"), " ")
    breadcrumb = clean_text(soup.find(class_="crumb"), " > ")

    item = {
        "time_code": slot.code,
        "time_label": slot.label,
        "entry_url": slot.entry_url,
        "url": slot.ziwei_url,
        "title": title,
        "h1": h1,
        "breadcrumb": breadcrumb,
        "related_links": parse_related_links(soup, config.source_url),
        "analysis_info": analysis_info,
        "analysis_rows": analysis_rows,
        "chart": chart,
        "notes": parse_notes(soup),
    }
    return item, chart_html


def text_lines(node: Tag | None) -> list[str]:
    text = clean_text(node, "\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def cell_lines_preserve_breaks(cell: Tag | None) -> list[str]:
    if cell is None:
        return []

    lines: list[str] = []
    current: list[str] = []
    for child in cell.descendants:
        if isinstance(child, NavigableString):
            current.append(str(child))
        elif isinstance(child, Tag) and child.name == "br":
            lines.append(re.sub(r"\s+", " ", "".join(current)).strip())
            current = []
    if current or not lines:
        lines.append(re.sub(r"\s+", " ", "".join(current)).strip())
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_bazi_basic_info(cell: Tag | None) -> dict[str, Any]:
    if cell is None:
        return {}

    text = clean_text(cell, "\n")
    compact = re.sub(r"\s+", " ", text)

    info: dict[str, Any] = {
        "raw_text": text,
        "solar_terms": [],
        "adjusted_birth_month_options": [],
    }

    patterns = {
        "gregorian": r"公历：\s*(\d{4}年\d{2}月\d{2}日)",
        "lunar": r"农历：\s*([^\n]+?)(?:\n|时间：)",
        "time": r"时间：\s*([0-9-]+点)",
        "leap_year_text": r"公历：\s*(\d{4}年[^\s]+闰年)",
        "luck_start": r"交运：命主于公历\s*(\d{4}-\d{2}-\d{2})\s*交运",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            info[key] = re.sub(r"\s+", " ", match.group(1)).strip()

    info["lunar_month_type"] = "闰月" if "闰月" in compact and "非闰月" not in compact else "非闰月" if "非闰月" in compact else ""

    for name, moment in re.findall(r"([\u4e00-\u9fa5]{2,3})：公历\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", compact):
        info["solar_terms"].append({"name": name, "datetime": moment})

    for anchor in cell.find_all("a", href=True):
        label = clean_text(anchor, " ")
        href = str(anchor["href"])
        if "szmxg" in href or "默认" in label or "上月" in label or "下月" in label:
            info["adjusted_birth_month_options"].append({"label": label, "url": href})

    return info


def parse_bazi_cell(cell: Tag, slot_name: str) -> dict[str, Any]:
    strings = [value.strip() for value in cell.stripped_strings if value.strip()]
    stem_branch_tag = cell.find("div", class_="zw3x")
    stem_branch = [value.strip() for value in stem_branch_tag.stripped_strings] if stem_branch_tag else []

    title_parts: list[str] = []
    hidden_stems: list[str] = []
    if stem_branch:
        try:
            first_stem_index = strings.index(stem_branch[0])
        except ValueError:
            first_stem_index = 1
        title_parts = strings[:first_stem_index]
        hidden_stems = strings[first_stem_index + len(stem_branch) :]
    elif strings:
        title_parts = [strings[0]]
        hidden_stems = strings[1:]

    title = re.sub(r"\s+", "", "".join(title_parts))
    label = slot_name
    ten_god = title
    paren_match = re.match(r"(.+?)\((.+)\)", title)
    if paren_match:
        label = paren_match.group(1)
        ten_god = paren_match.group(2)

    return {
        "slot": slot_name,
        "label": label,
        "ten_god": ten_god,
        "stem": stem_branch[0] if len(stem_branch) >= 1 else "",
        "branch": stem_branch[1] if len(stem_branch) >= 2 else "",
        "hidden_stems": hidden_stems,
        "raw_text": clean_text(cell),
    }


def parse_dayun_cell(cell: Tag | None) -> list[dict[str, str]]:
    if cell is None:
        return []

    lines = text_lines(cell)
    items: list[dict[str, str]] = []
    stem_branch_re = re.compile(r"^[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]$")
    for index, line in enumerate(lines):
        if not stem_branch_re.match(line):
            continue
        details = lines[index + 1] if index + 1 < len(lines) else ""
        match = re.match(r"\[([^\]]*)\]\s*\[([^\]]*)\]\s*(.*)", details)
        if match:
            nayin, strength, shensha = match.groups()
            items.append(
                {
                    "pillar": line,
                    "nayin": re.sub(r"\s+", " ", nayin).strip(),
                    "strength": re.sub(r"\s+", " ", strength).strip(),
                    "shensha": re.sub(r"\s+", " ", shensha).strip(),
                }
            )
        else:
            items.append({"pillar": line, "nayin": "", "strength": "", "shensha": details})
    return items


def parse_voids_and_patterns(cell: Tag | None) -> dict[str, Any]:
    text = clean_text(cell, "\n")
    result: dict[str, Any] = {"raw_text": text, "patterns": []}
    for key, pattern in {
        "year_pillar_void": r"年柱：([^\n]+)",
        "day_pillar_void": r"日柱：([^\n]+)",
        "pattern_text": r"格局：([^\n]+)",
    }.items():
        match = re.search(pattern, text)
        if match:
            result[key] = re.sub(r"\s+", " ", match.group(1)).strip()
    if "pattern_text" in result:
        result["patterns"] = [part for part in re.split(r"\s+", result["pattern_text"]) if part and part != "格局说明"]
    return result


def parse_bazi_main_chart(table: Tag | None) -> dict[str, Any]:
    if table is None:
        return {}

    nested = table.find("table")
    if nested is None:
        return {}

    rows = nested.find_all("tr", recursive=False)
    if not rows:
        return {}

    first_cells = rows[0].find_all("td", recursive=False)
    chart_type = clean_text(first_cells[0], " ").replace(" ", "") if first_cells else ""
    names = ["年柱", "月柱", "日柱", "时柱"]
    positions = [1, 2, 3, 4]
    pillars = {
        name: parse_bazi_cell(first_cells[position], name)
        for name, position in zip(names, positions)
        if position < len(first_cells)
    }

    extra_positions = {"命宫": 6, "胎元": 7}
    extras = {
        name: parse_bazi_cell(first_cells[position], name)
        for name, position in extra_positions.items()
        if position < len(first_cells)
    }
    dayun_cell = first_cells[-1] if first_cells else None

    voids_and_patterns = {}
    if len(rows) > 1:
        cells = rows[1].find_all("td", recursive=False)
        if len(cells) > 1:
            voids_and_patterns = parse_voids_and_patterns(cells[1])

    return {
        "chart_type": chart_type,
        "pillars": pillars,
        "extras": extras,
        "dayun": parse_dayun_cell(dayun_cell),
        "voids_and_patterns": voids_and_patterns,
        "raw_text": clean_text(nested),
    }


def parse_bazi_wangshuai(table: Tag | None) -> dict[str, Any]:
    if table is None:
        return {}

    rows = table.find_all("tr", recursive=False)
    if len(rows) < 3:
        return {}

    cells = rows[2].find_all("td", recursive=False)
    if not cells:
        return {}

    labels = text_lines(cells[0])
    column_map = {
        "年柱": 1,
        "月柱": 2,
        "日柱": 3,
        "时柱": 4,
        "命宫": 6,
        "胎元": 7,
    }
    result: dict[str, dict[str, str]] = {}
    for name, position in column_map.items():
        if position >= len(cells):
            continue
        values = cell_lines_preserve_breaks(cells[position])
        result[name] = {label: values[index] if index < len(values) else "" for index, label in enumerate(labels)}
    return result


def parse_bazi_shensha_grid(table: Tag | None) -> list[dict[str, Any]]:
    if table is None:
        return []

    column_map = {
        "年柱": 1,
        "月柱": 2,
        "日柱": 3,
        "时柱": 4,
        "命宫": 6,
        "胎元": 7,
    }
    rows: list[dict[str, Any]] = []
    for tr in table.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        row_name = clean_text(cells[0], " ")
        if not any(token in row_name for token in ["神煞", "空亡"]):
            continue
        values = {
            column: text_lines(cells[position]) if position < len(cells) else []
            for column, position in column_map.items()
        }
        rows.append({"name": row_name, "values": values})
    return rows


def parse_luck_cycles(table: Tag | None) -> list[dict[str, Any]]:
    if table is None:
        return []

    nested_tables = table.find_all("table")
    if len(nested_tables) < 2:
        return []

    rows = nested_tables[1].find_all("tr")
    if not rows:
        return []
    cells = rows[0].find_all("td", recursive=False)
    cycles: list[dict[str, Any]] = []
    for cell in cells[1:]:
        lines = text_lines(cell)
        if len(lines) < 3:
            continue
        cycles.append(
            {
                "dayun": lines[0],
                "start_year": lines[1],
                "annual_pillars": lines[2:-1],
                "end_year": lines[-1],
            }
        )
    return cycles


def parse_generic_table(table: Tag) -> dict[str, Any]:
    headers = [clean_text(th, " ") for th in table.find_all("th")]
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        rows.append([clean_text(cell, "\n") for cell in cells])
    return {
        "title": headers[0] if len(headers) == 1 else "",
        "headers": headers,
        "rows": rows,
        "raw_text": clean_text(table),
    }


def parse_bazi_page(slot: TimeSlot, html_text: str, config: ScrapeConfig) -> tuple[dict[str, Any], str]:
    soup = BeautifulSoup(html_text, "lxml")
    chart_table = soup.select_one("table.zwsz")
    basic_cell = chart_table.find("tr").find("td") if chart_table and chart_table.find("tr") else None

    item = {
        "time_code": slot.code,
        "time_label": slot.label,
        "url": slot.bazi_url,
        "title": clean_text(soup.title, " "),
        "h1": clean_text(soup.find("h1"), " "),
        "breadcrumb": clean_text(soup.find(class_="crumb"), " > "),
        "basic_info": parse_bazi_basic_info(basic_cell),
        "main_chart": parse_bazi_main_chart(chart_table),
        "wangshuai_nayin": parse_bazi_wangshuai(chart_table),
        "shensha_grid": parse_bazi_shensha_grid(chart_table),
        "luck_cycles": parse_luck_cycles(chart_table),
        "analysis_tables": [parse_generic_table(table) for table in soup.select("table.layui-table")],
        "notes": parse_notes(soup),
    }
    return item, str(chart_table) if chart_table else ""


def write_chart_html(path: Path, item: dict[str, Any], config: ScrapeConfig, chart_html: str) -> None:
    title = f"{config.date} {item['time_label']} {config.gender_label}紫微命盘"
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="http://pub5.kvov.com/layui/css/layui.css">
  <link href="http://pub.kvov.com/css/zw.css?cc=t" rel="stylesheet" type="text/css">
  <style>
    body {{ margin: 24px; font-family: Arial, "Microsoft YaHei", sans-serif; }}
    .source {{ margin: 0 0 16px; color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p class="source">来源：<a href="{html.escape(item['url'])}">{html.escape(item['url'])}</a></p>
  {chart_html}
</body>
</html>
"""
    path.write_text(content, encoding="utf-8")


def write_bazi_chart_html(path: Path, item: dict[str, Any], config: ScrapeConfig, chart_html: str) -> None:
    title = f"{config.date} {item['time_label']} {config.gender_label}八字命盘"
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="http://pub5.kvov.com/layui/css/layui.css">
  <link href="http://pub.kvov.com/css/zw.css" rel="stylesheet" type="text/css">
  <style>
    body {{ margin: 24px; font-family: Arial, "Microsoft YaHei", sans-serif; }}
    .source {{ margin: 0 0 16px; color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p class="source">来源：<a href="{html.escape(item['url'])}">{html.escape(item['url'])}</a></p>
  {chart_html}
</body>
</html>
"""
    path.write_text(content, encoding="utf-8")


def md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def bazi_pillar_text(value: dict[str, Any]) -> str:
    if not value:
        return ""
    stem_branch = f"{value.get('stem', '')}{value.get('branch', '')}"
    hidden = "；".join(value.get("hidden_stems", []))
    parts = [value.get("ten_god", ""), stem_branch, hidden]
    return " ".join(part for part in parts if part)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# {payload['date']} {payload['gender_label']}紫微斗数命盘与分析")
    lines.append("")
    lines.append(f"- 来源入口：{payload['source_url']}")
    lines.append(f"- 抓取时间：{payload['fetched_at']}")
    lines.append(f"- 时辰数：{len(payload['items'])}")
    lines.append("- 说明：仅整理公开页面中可直接访问的紫微斗数页；留言区未纳入。")
    lines.append("")

    for item in payload["items"]:
        lines.append(f"## {item['time_label']}（code={item['time_code']}）")
        lines.append("")
        lines.append(f"- 页面：{item['url']}")
        lines.append(f"- 标题：{item['h1'] or item['title']}")
        if item["chart"]["meta"]:
            meta = "；".join(f"{key}：{value}" for key, value in item["chart"]["meta"].items())
            lines.append(f"- 命盘信息：{meta}")
        lines.append(
            f"- 紫微口诀：{item['analysis_info']['visible_count']} 条"
            + (
                f"（页面标注 {item['analysis_info']['advertised_count']} 条）"
                if item["analysis_info"].get("advertised_count")
                else ""
            )
        )
        lines.append("")

        lines.append("### 命盘十二宫")
        lines.append("")
        lines.append("| 宫位 | 大限 | 状态 | 干支 | 星曜 | 辅助信息 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for palace in item["chart"]["palaces"]:
            star_text = "；".join(
                " ".join(
                    part
                    for part in [
                        star["name"],
                        star["brightness"],
                        star["transform"],
                        f"({star['level']})",
                    ]
                    if part
                )
                for star in palace["stars"]
            )
            aux = "；".join(palace["badges"] + palace["auxiliary_notes"])
            lines.append(
                "| "
                + " | ".join(
                    md_escape(str(value))
                    for value in [
                        palace["name"],
                        palace["age_range"],
                        palace["phase"],
                        palace["stem_branch"],
                        star_text,
                        aux,
                    ]
                )
                + " |"
            )
        lines.append("")

        lines.append("### 紫微口诀")
        lines.append("")
        lines.append("| # | 可信度 | 推断依据 | 分析 |")
        lines.append("| --- | --- | --- | --- |")
        for row in item["analysis_rows"]:
            lines.append(
                "| "
                + " | ".join(
                    md_escape(str(value))
                    for value in [
                        row["index"],
                        row["confidence"],
                        row["basis"],
                        row["analysis"],
                    ]
                )
                + " |"
            )
        lines.append("")

        if item["notes"]:
            lines.append("### 页面说明")
            lines.append("")
            for note in item["notes"]:
                lines.append(f"- {note.replace(chr(10), ' ')}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_analysis_csv(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "date",
                "gender",
                "gender_label",
                "time_code",
                "time_label",
                "url",
                "row_index",
                "confidence",
                "basis",
                "analysis",
            ],
        )
        writer.writeheader()
        for item in payload["items"]:
            for row in item["analysis_rows"]:
                writer.writerow(
                    {
                        "date": payload["date"],
                        "gender": payload["gender"],
                        "gender_label": payload["gender_label"],
                        "time_code": item["time_code"],
                        "time_label": item["time_label"],
                        "url": item["url"],
                        "row_index": row["index"],
                        "confidence": row["confidence"],
                        "basis": row["basis"],
                        "analysis": row["analysis"],
                    }
                )


def write_bazi_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# {payload['date']} {payload['gender_label']}八字命盘排盘数据")
    lines.append("")
    lines.append(f"- 来源入口：{payload['source_url']}")
    lines.append(f"- 抓取时间：{payload['fetched_at']}")
    lines.append(f"- 时辰数：{len(payload['items'])}")
    lines.append("- 说明：整理 `bzmp-{时辰}-{性别}.html` 的公开八字命盘页，保留原始 HTML 和排盘 HTML。")
    lines.append("")

    for item in payload["items"]:
        bazi = item.get("bazi", {})
        main_chart = bazi.get("main_chart", {})
        pillars = main_chart.get("pillars", {})
        extras = main_chart.get("extras", {})
        voids = main_chart.get("voids_and_patterns", {})
        basic = bazi.get("basic_info", {})

        lines.append(f"## {item['time_label']}（code={item['time_code']}）")
        lines.append("")
        lines.append(f"- 页面：{bazi.get('url', '')}")
        lines.append(f"- 标题：{bazi.get('h1') or bazi.get('title', '')}")
        lines.append(f"- 公历：{basic.get('gregorian', '')}")
        lines.append(f"- 农历：{basic.get('lunar', '')} {basic.get('lunar_month_type', '')}".rstrip())
        lines.append(f"- 交运：{basic.get('luck_start', '')}")
        if basic.get("solar_terms"):
            terms = "；".join(f"{term['name']}：{term['datetime']}" for term in basic["solar_terms"])
            lines.append(f"- 节气：{terms}")
        if voids:
            lines.append(
                "- 空亡/格局："
                + "；".join(
                    part
                    for part in [
                        f"年柱 {voids.get('year_pillar_void', '')}" if voids.get("year_pillar_void") else "",
                        f"日柱 {voids.get('day_pillar_void', '')}" if voids.get("day_pillar_void") else "",
                        f"格局 {voids.get('pattern_text', '')}" if voids.get("pattern_text") else "",
                    ]
                    if part
                )
            )
        lines.append("")

        lines.append("### 四柱排盘")
        lines.append("")
        lines.append("| 项 | 年柱 | 月柱 | 日柱 | 时柱 | 命宫 | 胎元 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        lines.append(
            "| 十神/干支/藏干 | "
            + " | ".join(
                md_escape(
                    bazi_pillar_text(
                        pillars.get(name, extras.get(name, {}))
                    )
                )
                for name in ["年柱", "月柱", "日柱", "时柱", "命宫", "胎元"]
            )
            + " |"
        )
        wangshuai = bazi.get("wangshuai_nayin", {})
        for label in ["日主旺衰", "天干旺衰", "纳音"]:
            lines.append(
                f"| {label} | "
                + " | ".join(md_escape(wangshuai.get(name, {}).get(label, "")) for name in ["年柱", "月柱", "日柱", "时柱", "命宫", "胎元"])
                + " |"
            )
        lines.append("")

        if main_chart.get("dayun"):
            lines.append("### 大运")
            lines.append("")
            lines.append("| 大运 | 纳音 | 旺衰 | 神煞 |")
            lines.append("| --- | --- | --- | --- |")
            for row in main_chart["dayun"]:
                lines.append(
                    "| "
                    + " | ".join(md_escape(row.get(key, "")) for key in ["pillar", "nayin", "strength", "shensha"])
                    + " |"
                )
            lines.append("")

        if bazi.get("shensha_grid"):
            lines.append("### 神煞")
            lines.append("")
            lines.append("| 项 | 年柱 | 月柱 | 日柱 | 时柱 | 命宫 | 胎元 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for row in bazi["shensha_grid"]:
                values = row.get("values", {})
                lines.append(
                    f"| {md_escape(row.get('name', ''))} | "
                    + " | ".join(md_escape("；".join(values.get(name, []))) for name in ["年柱", "月柱", "日柱", "时柱", "命宫", "胎元"])
                    + " |"
                )
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_bazi_summary_csv(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "date",
                "gender",
                "time_code",
                "time_label",
                "url",
                "year_pillar",
                "month_pillar",
                "day_pillar",
                "hour_pillar",
                "minggong",
                "taiyuan",
                "year_void",
                "day_void",
                "patterns",
                "luck_start",
            ],
        )
        writer.writeheader()
        for item in payload["items"]:
            bazi = item.get("bazi", {})
            main_chart = bazi.get("main_chart", {})
            pillars = main_chart.get("pillars", {})
            extras = main_chart.get("extras", {})
            voids = main_chart.get("voids_and_patterns", {})
            basic = bazi.get("basic_info", {})
            writer.writerow(
                {
                    "date": payload["date"],
                    "gender": payload["gender"],
                    "time_code": item["time_code"],
                    "time_label": item["time_label"],
                    "url": bazi.get("url", ""),
                    "year_pillar": bazi_pillar_text(pillars.get("年柱", {})),
                    "month_pillar": bazi_pillar_text(pillars.get("月柱", {})),
                    "day_pillar": bazi_pillar_text(pillars.get("日柱", {})),
                    "hour_pillar": bazi_pillar_text(pillars.get("时柱", {})),
                    "minggong": bazi_pillar_text(extras.get("命宫", {})),
                    "taiyuan": bazi_pillar_text(extras.get("胎元", {})),
                    "year_void": voids.get("year_pillar_void", ""),
                    "day_void": voids.get("day_pillar_void", ""),
                    "patterns": " ".join(voids.get("patterns", [])),
                    "luck_start": basic.get("luck_start", ""),
                }
            )


def scrape(config: ScrapeConfig) -> dict[str, Any]:
    raw_dir = config.out_dir / "raw_html"
    chart_dir = config.out_dir / "chart_html"
    bazi_raw_dir = config.out_dir / "bazi_raw_html"
    bazi_chart_dir = config.out_dir / "bazi_chart_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    chart_dir.mkdir(parents=True, exist_ok=True)
    bazi_raw_dir.mkdir(parents=True, exist_ok=True)
    bazi_chart_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    index_html = fetch(session, config.source_url)
    (raw_dir / "index.html").write_text(index_html, encoding="utf-8")

    slots = parse_time_slots(index_html, config)
    if not slots:
        raise RuntimeError("No time-slot links were found on the source page.")

    items: list[dict[str, Any]] = []
    for slot in slots:
        print(f"Fetching {slot.code} {slot.label}: {slot.ziwei_url}")
        page_html = fetch(session, slot.ziwei_url)
        (raw_dir / f"mfsmm-{slot.code}-{config.gender_code}.html").write_text(page_html, encoding="utf-8")
        item, chart_html = parse_page(slot, page_html, config)
        chart_name = f"{slot.code:02d}-{safe_label(slot.label)}.html"
        write_chart_html(chart_dir / chart_name, item, config, chart_html)

        print(f"Fetching bazi {slot.code} {slot.label}: {slot.bazi_url}")
        bazi_html = fetch(session, slot.bazi_url)
        (bazi_raw_dir / f"bzmp-{slot.code}-{config.gender_code}.html").write_text(bazi_html, encoding="utf-8")
        bazi_item, bazi_chart_html = parse_bazi_page(slot, bazi_html, config)
        bazi_chart_name = f"{slot.code:02d}-{safe_label(slot.label)}.html"
        write_bazi_chart_html(bazi_chart_dir / bazi_chart_name, bazi_item, config, bazi_chart_html)
        item["bazi"] = bazi_item

        items.append(item)
        if config.delay > 0:
            time.sleep(config.delay)

    payload = {
        "date": config.date,
        "gender": config.gender,
        "gender_code": config.gender_code,
        "gender_label": config.gender_label,
        "source_url": config.source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    prefix = f"mfsm_{config.date}_{config.gender}_ziwei"
    bazi_prefix = f"mfsm_{config.date}_{config.gender}_bazi"
    json_path = config.out_dir / f"{prefix}.json"
    md_path = config.out_dir / f"{prefix}.md"
    csv_path = config.out_dir / f"{prefix}_analysis_rows.csv"
    bazi_json_path = config.out_dir / f"{bazi_prefix}.json"
    bazi_md_path = config.out_dir / f"{bazi_prefix}.md"
    bazi_csv_path = config.out_dir / f"{bazi_prefix}_summary.csv"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, payload)
    write_analysis_csv(csv_path, payload)

    bazi_payload = {
        key: payload[key]
        for key in ["date", "gender", "gender_code", "gender_label", "source_url", "fetched_at"]
    }
    bazi_payload["items"] = [
        {
            "time_code": item["time_code"],
            "time_label": item["time_label"],
            "entry_url": item["entry_url"],
            "bazi": item.get("bazi", {}),
        }
        for item in items
    ]
    bazi_json_path.write_text(json.dumps(bazi_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_bazi_markdown(bazi_md_path, bazi_payload)
    write_bazi_summary_csv(bazi_csv_path, bazi_payload)

    return {
        "output_dir": str(config.out_dir),
        "time_slots": len(items),
        "analysis_rows": sum(len(item["analysis_rows"]) for item in items),
        "palaces": sum(len(item["chart"]["palaces"]) for item in items),
        "bazi_pages": sum(1 for item in items if item.get("bazi")),
        "files": {
            "json": str(json_path),
            "markdown": str(md_path),
            "csv": str(csv_path),
            "bazi_json": str(bazi_json_path),
            "bazi_markdown": str(bazi_md_path),
            "bazi_csv": str(bazi_csv_path),
            "raw_html_dir": str(raw_dir),
            "chart_html_dir": str(chart_dir),
            "bazi_raw_html_dir": str(bazi_raw_dir),
            "bazi_chart_html_dir": str(bazi_chart_dir),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch all hourly Zi Wei Dou Shu pages from mfsm.kvov.com for a Gregorian date."
    )
    parser.add_argument("date_or_url", help="Gregorian date YYYY-MM-DD or a source URL like http://mfsm.kvov.com/fx/YYYY-MM-DD/")
    parser.add_argument("--gender", choices=sorted(GENDERS), default="female", help="Chart gender to fetch. Default: female.")
    parser.add_argument("--output-root", default=".", help="Root directory for generated analysis folders. Default: current directory.")
    parser.add_argument("--output-dir", help="Optional output folder. Relative paths are resolved under --output-root.")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between detail-page requests in seconds. Default: 0.2.")
    return parser.parse_args()


def main() -> None:
    config = build_config(parse_args())
    summary = scrape(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
