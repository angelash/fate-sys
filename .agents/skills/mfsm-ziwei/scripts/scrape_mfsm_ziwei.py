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
        slots_by_code.setdefault(
            code,
            TimeSlot(
                code=code,
                label=label,
                entry_url=entry_url,
                ziwei_url=ziwei_url,
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


def md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


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


def scrape(config: ScrapeConfig) -> dict[str, Any]:
    raw_dir = config.out_dir / "raw_html"
    chart_dir = config.out_dir / "chart_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    chart_dir.mkdir(parents=True, exist_ok=True)

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
        items.append(item)
        chart_name = f"{slot.code:02d}-{safe_label(slot.label)}.html"
        write_chart_html(chart_dir / chart_name, item, config, chart_html)
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
    json_path = config.out_dir / f"{prefix}.json"
    md_path = config.out_dir / f"{prefix}.md"
    csv_path = config.out_dir / f"{prefix}_analysis_rows.csv"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, payload)
    write_analysis_csv(csv_path, payload)

    return {
        "output_dir": str(config.out_dir),
        "time_slots": len(items),
        "analysis_rows": sum(len(item["analysis_rows"]) for item in items),
        "palaces": sum(len(item["chart"]["palaces"]) for item in items),
        "files": {
            "json": str(json_path),
            "markdown": str(md_path),
            "csv": str(csv_path),
            "raw_html_dir": str(raw_dir),
            "chart_html_dir": str(chart_dir),
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
