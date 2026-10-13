# Yahoo Market Data

Aime Chart 的独立 Yahoo 结构化取数模块。提供行情、收益率、支持的财务序列、宏观序列和事件窗口数据，输出 JSON，保留来源、回退与代理标记。

## 使用

建议 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app/skills/yahoo-data/scripts/yahoo_data.py --help
python app/skills/yahoo-data/scripts/yahoo_data.py --symbols AAPL,MSFT --metric close --range 1mo --interval 1d --payload-output outputs/prices.json
```

运行真实取数需要网络和数据源可用。Yahoo 限流时的 Nasdaq 回退仅覆盖受支持的美国日线行情；来源、指数代理和未复权限制会在 payload 中明确记录。严格财务周期缺少数据时返回来源限制，不能当作真实数值。

`--chart-type` 只是数据形状提示。`--output` 是 JSON 输出的兼容别名，此 CLI 不生成图表。完整参数和数据口径见 `app/skills/yahoo-data/SKILL.md`。

## 文件

- `app/skills/yahoo-data/scripts/yahoo_data.py`：取数 CLI。
- `app/skills/yahoo-data/scripts/yfinance_safe.py`：缓存、重试和来源回退辅助模块。
- `tests/`：不访问外部服务的回退、缓存及 JSON 输出验证。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```
