---
name: mfsm-ziwei
description: Use when fetching or refreshing mfsm.kvov.com Zi Wei Dou Shu pages, especially all hourly female or male charts for a Gregorian date. Keeps each independent analysis in its own mfsm-YYYY-MM-DD-gender-ziwei folder with raw HTML, chart HTML, JSON, Markdown, and CSV outputs.
---

# MFSM Zi Wei Collection

Use this skill to collect public Zi Wei Dou Shu chart pages from `mfsm.kvov.com`.

## Workflow

1. Identify the source date or URL, for example `1995-07-11` or `http://mfsm.kvov.com/fx/1995-07-11/`.
2. Run the bundled script from the repository root:

```powershell
python .agents/skills/mfsm-ziwei/scripts/scrape_mfsm_ziwei.py 1995-07-11 --gender female --output-root .
```

3. Keep each analysis self-contained in its generated folder:

```text
mfsm-YYYY-MM-DD-female-ziwei/
├── chart_html/
├── bazi_chart_html/
├── raw_html/
├── bazi_raw_html/
├── mfsm_YYYY-MM-DD_female_ziwei.json
├── mfsm_YYYY-MM-DD_female_ziwei.md
├── mfsm_YYYY-MM-DD_female_ziwei_analysis_rows.csv
├── mfsm_YYYY-MM-DD_female_bazi.json
├── mfsm_YYYY-MM-DD_female_bazi.md
└── mfsm_YYYY-MM-DD_female_bazi_summary.csv
```

Do not place one-off scraper scripts inside analysis folders. Put reusable scraping or parsing logic in this skill's `scripts/` directory.

## Fetch Method

- The date index page is `http://mfsm.kvov.com/fx/YYYY-MM-DD/`.
- The index page contains time-slot links like `mfsms-1.html`, `mfsms-3.html`, ..., `mfsms-24.html`.
- Zi Wei detail pages use `mfsmm-{time_code}-{gender_code}.html`.
- Ba Zi chart pages use `bzmp-{time_code}-{gender_code}.html`.
- Gender codes are `1` for male and `2` for female.
- The script preserves the source HTML before parsing, then extracts:
  - Zi Wei analysis rows and confidence percentages.
  - The twelve-palace chart table.
  - Ba Zi chart data: 坤/乾造, four pillars, 命宫, 胎元, 大运, 神煞, 旺衰, 纳音.
  - Chart metadata such as 五行局, 命主, 身主, 阳历, 农历, 时间, 性别.
  - Related links exposed by the page.

## Script Options

```powershell
python .agents/skills/mfsm-ziwei/scripts/scrape_mfsm_ziwei.py <YYYY-MM-DD-or-source-url> `
  --gender female `
  --output-root . `
  --delay 0.2
```

Use `--output-dir <path>` only when the default folder name is not appropriate. If the path is relative, it is resolved under `--output-root`.

## Notes

- The outputs are a local archive of public pages for personal research and review.
- The source site warns against commercial copying; keep that warning with the data when sharing internally.
- If the site layout changes, update only the script in this skill and regenerate the affected analysis folder.
