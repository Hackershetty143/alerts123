# Navigator scanner → Telegram

Scans your pairs every 15 minutes with the Navigator v2.2 logic. It messages you on Telegram when a setup grades **A+ or A** (configurable) **and** a 15m candle has closed beyond 50% of the 15m range. For a BUY that means closing at or below the 50% level; for a SELL, at or above it. Each message has a link that opens the pair on TradingView.

The 4H bias and range, the 15m range, the FVG and the sweep checks, and the grade follow the indicator's defaults. It uses OANDA demo data and runs free on GitHub Actions.

## Setup (one time, about 10 minutes)

1. **Create the repo.** On GitHub, create a new **public** repository, e.g. `navigator-scanner`.
2. **Upload the files.** Choose *Add file → Upload files* and drag in everything from this folder, including the `.github` folder:
   - `navigator_scan.py`
   - `requirements.txt`
   - `state.json`
   - `README.md`
   - `.github/workflows/scan.yml`

   If the `.github` folder doesn't upload, choose *Add file → Create new file* instead. Name it `.github/workflows/scan.yml` and paste in that file's contents.
3. **Add the secrets.** Go to *Settings → Secrets and variables → Actions → New repository secret* and add three secrets:
   - `OANDA_TOKEN`: your OANDA demo API token
   - `TELEGRAM_BOT_TOKEN`: the token of your existing Telegram bot
   - `TELEGRAM_CHAT_ID`: `8804659567`
4. **Allow the scanner to save its state.** Go to *Settings → Actions → General → Workflow permissions*, choose **Read and write permissions**, then click Save.
5. **Start your bot in Telegram.** Open your bot and press **Start**. A bot can't message you until you've started it.
6. **Test it.** Go to *Actions → Navigator scan → Run workflow*, tick **Send a test message**, then click Run. Within about a minute you should get a "scanner is connected" message. The run log shows the current grade for every pair.

After that it runs on its own, every 15 minutes, Sunday evening to Friday night.

## Choosing which grades alert you (no code editing)

Go to *Settings → Secrets and variables → Actions → **Variables** tab → New repository variable*:

| Variable | Example | What it does |
|---|---|---|
| `ALERT_GRADES` | `A+,A,B` | Grades that send a message. If you don't set it, the default is `A+,A`. |
| `REQUIRE_50` | `0` | `0` alerts as soon as the setup grades, without waiting for the close past 50%. If you don't set it, the 50% rule is on. |

Changes apply from the next run.

## Other settings

Edit the top of `navigator_scan.py` on GitHub:

| Setting | Default | What it does |
|---|---|---|
| `PAIRS` | 21 FX pairs + gold | OANDA names, e.g. `GBP_JPY` |
| `SWING_MTF`, `SWING_LTF` | 5, 5 | 4H and 15m swing length |
| `LIQ_L`, `LIQ_R` | 5, 5 | 15m liquidity sensitivity |
| `SWEEP_LB` | 10 | Sweep window before the range extreme |
| `FIB_LEVEL` | 0.71 | Fib level, used only with `FVG_RULE_FIB` |
| `FVG_RULE_FIB` | `False` | Set to `True` to require the FVG to contain the Fib level |
| `PRICE` | `"M"` | Price used: `"M"` mid, `"B"` bid, `"A"` ask |

## Notes

- **One message per setup.** The same range, side and grade is never sent twice. If an A later becomes an A+, you get a new message.
- **First run.** On the first run you may get messages for setups that are already formed.
- **Small differences from TradingView.** Candles come from OANDA, so a grade can occasionally differ from the TradingView chart. Always confirm on the chart before trading.
- **Timing.** GitHub's schedule can run a few minutes late at busy times.
- **Keep the repo active.** GitHub pauses scheduled workflows in a repo with no activity for 60 days. The scanner commits its state file, which normally counts as activity. If the scanner ever stops, open *Actions* and re-enable the workflow.
