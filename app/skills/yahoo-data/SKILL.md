---
name: yahoo-data
description: General-purpose structured data source backed by Yahoo-supported market symbols and data interfaces. Use it to obtain reusable rows for global market data, supported financial time series, macro indicators, and event-window analysis before calculation, transformation, or chart rendering.
capability_scope: general_structured_data
market_scope: global_yahoo_supported_symbols
---

# Yahoo Data

Use `scripts/yahoo_data.py` as a general-purpose structured data source. It fetches data covered by Yahoo-supported symbols and interfaces, then writes reusable rows and a structured payload for downstream calculations, charts, tables, or transformations.

For daily US-market requests, the adapter has a bounded availability fallback when both Yahoo chart hosts return HTTP 429. Ordinary US-listed stocks and ETFs can use Nasdaq's daily historical interface; Nasdaq Composite uses Nasdaq's `COMP` history directly; S&P 500 and Dow direction use the explicitly disclosed `SPY` and `DIA` ETF proxies. The payload `source` and `method` fields identify every fallback and proxy mapping. The fallback supplies unadjusted daily OHLCV only, so `adj_close`, intraday intervals, unsupported non-US symbols, and Yahoo-specific fundamentals remain unavailable rather than being fabricated. Downstream answers must not describe proxy values as exact index levels.

`yahoo_data.py` is data-only. It never renders or displays an HTML chart. If the user explicitly requests a visualization, plan a separate downstream chart component whose schema matches the requested visual form. If the user only asks for Yahoo values or analysis, use the structured payload directly in the text answer and do not add a renderer.

## Capability Scope

- Select this Skill whenever the task needs general structured data and Yahoo covers the requested symbol and fields. It is not limited to a particular chart type or to US equities.
- Supported sources include global Yahoo-supported market symbols, market time series and derived returns, supported fundamentals time series, macro indicators exposed by this CLI, and event-window market data.
- Treat the output as source data. The requested presentation can be a chart, table, calculation, comparison, or another structured transformation.
- `general_structured_data` does not mean arbitrary web data. Do not use this Skill for unsupported fields, unstructured news or webpage facts, filing-only disclosures, or historical employee/headcount series. Use search or filings for those facts and merge them downstream when needed.
- Never invent a Yahoo field or silently substitute `close` for an unsupported metric. Unsupported symbols, metrics, or periods must fail explicitly or route to another source.

## Listing Region and Symbol Resolution

Before generating any Yahoo CLI, identify the exact security, listing country/region, exchange, and intended market/currency. A company's domicile is not its listing venue, and a bare ticker must never be assumed to be a US Yahoo symbol.

- US NASDAQ/NYSE/AMEX listings normally have no suffix.
- Canada: TSX `.TO`, TSXV `.V`.
- Australia: ASX `.AX`.
- United Kingdom: LSE `.L`.
- Hong Kong: `.HK`, with numeric codes normalized to four digits.
- Mainland China: Shanghai `.SS`, Shenzhen `.SZ`.
- Japan: Tokyo `.T`; South Korea: KOSPI `.KS`, KOSDAQ `.KQ`.
- Germany: Xetra `.DE`, Frankfurt `.F`; France `.PA`; Italy `.MI`; Netherlands `.AS`; Spain `.MC`; Switzerland `.SW`.
- Singapore `.SI`; Taiwan listed `.TW`, OTC `.TWO`.

For dual- or multi-listed companies, use the market requested by the user. If no market is specified, prefer the primary listing; for peer comparisons, prefer comparable listings in the same market or currency when available and state the selected listing in the step reason. If the company name, bare ticker, and listing venue are ambiguous, resolve the exact security first and do not guess.

In the planner JSON, write the resolved Yahoo symbol into `step.code`. Do not put `--symbol`, `--symbols`, or `--code` in `cli_args`, and do not leave an unresolved bare ticker in `step.code` while placing a different symbol in `cli_args`; the runtime appends `step.code` to the CLI.

Resolve the symbol before resolving the metric. Natural-language concepts are not automatically Yahoo metric names: timeline/price history maps to `close`, `adj_close`, `return_pct`, or `cumulative_return`; valuation must map only to explicitly supported fields such as `market_cap`. Each `yahoo_data.py` step accepts one `--metric`; split market cap and price history into separate steps rather than repeating the option. Do not pass `valuation` or `timeline` as `--metric`, and never fabricate PE/PB or other unsupported values.

## Scripts

- `yahoo_data.py`: main CLI.
- `yfinance_safe.py`: local helper used by Yahoo and business modules.

## Common Arguments

- `--symbols`: one or more comma-separated symbols.
- `--metric`: market metrics `close`, `adj_close`, `volume`, `return_pct`, `cumulative_return`, `market_cap`, `open`, `high`, `low`; financial metrics `financial_position`, `net_income`, `total_revenue`, `operating_income`, `gross_profit`; or supported event-window metrics. `financial_position` (also accepting `financials` and `balance_sheet`) returns total assets, total liabilities, stockholders' equity, cash and short-term investments, and total debt. Unsupported metrics fail explicitly and never fall back to `close`.
- `--macro-indicators`: supported macro series such as `gdp`, `population`, and `unemployment`.
- `--range`: Yahoo-style range.
- `--start-date`, `--end-date`: explicit date range when the task is period-bound.
- `--period-type`: `quarterly` or `annual` for financial metrics.
- `--range-length`: maximum number of financial periods, such as `8` for the latest eight quarters.
- `--interval`: Yahoo-style interval.
- `--chart-type`: backward-compatible payload-shape hint (`line`, `bar`, or `dynamic_line`); it does not trigger rendering.
- `--title`, `--payload-output`. `--output` is retained only as a deprecated JSON-output alias and never produces HTML.

## Routing Notes

Use Yahoo data as a general structured data source. If the user asks for grouped bars, grouped lines, tables, custom calculations, threshold filtering, or other derived views, fetch raw Yahoo rows first and pass the payload to `code_interpreter.py` or the appropriate generic renderer.
Do not select a chart component merely because Yahoo returned a time series. A renderer is allowed only when the user explicitly asks for a chart/visualization or another requested deliverable inherently requires one.
Do not pass generic chart script names as `--chart-type`; `--chart-type` is limited to the options supported by `yahoo_data.py`.
For historical employee/headcount series, do not use `--metric employees`: Yahoo's supported financial time-series mapping does not expose quarterly employee counts. Retrieve exact period-end employee counts from company filings or quarterly-results disclosures, then combine that evidence with Yahoo financial rows through `code_interpreter.py`.

For a generic `financial position` request, use `--metric financial_position --period-type annual`. Do not translate it into quarterly revenue, gross profit, operating income, and net income calls. If a single financial metric omits `--period-type` and Yahoo has no quarterly series, the CLI first tries the annual series and then, for supported fields such as latest revenue, the latest Yahoo financial snapshot/TTM value; every fallback is recorded in the payload. An explicitly requested quarterly period remains strict. When the user says latest/current without naming quarterly or annual, omit `--period-type` so this Yahoo-only fallback chain can run.
For a strict financial-period request, a missing Yahoo field is returned as a structured `yahoo-data-unavailable` source limitation rather than a module execution error. Do not retry the identical request, silently add another source, display a fallback card, or expose a TTM/latest snapshot as a replacement.

## Output Contract

The CLI writes a structured JSON payload to `--payload-output` and reports `rendered: false`. It does not create an HTML artifact, even when `--output` or `--chart-type` is present for backward compatibility.

For multiple symbols, `yahoo_data.py` emits tidy market rows such as
`{date, symbol, value, marketCap, returnPct, ...}`. This is raw source data, not a
`group_line.py`, `grouped_bar.py`, or `bar_line.py` payload.

When the requested renderer schema needs wide rows or explicit `series[]`, route
the Yahoo payload through `code_interpreter.py` after reading the target renderer
schema. The transform must pivot/map the tidy rows into that schema's exact
`payloadShape`; do not rely on the renderer to infer series from raw Yahoo rows.
