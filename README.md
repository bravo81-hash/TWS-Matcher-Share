# TWS Matcher

A small local app for Windows that checks your options positions in **Interactive Brokers (TWS)** against **OptionNet Explorer (ONE)** and **OptionStrat**. IBKR is treated as the source of truth, and the dashboard shows any leg, quantity or price that doesn't match.

It runs only on your own PC, and the dashboard is only reachable from that PC. **It never places, changes or cancels orders.** It only reads from TWS.

## What you get

- **Reconciliation:** MATCH or a precise difference for each account, including legs missing from either side, quantity mismatches, price drift, and fills IBKR cancelled after the fact.
- **Risk & Greeks:** delta, gamma, theta and vega by ticker and expiry, IV rank, expiry and early-assignment checks, margin headroom, and daily P&L by ticker.
- **OptionStrat:** generated links that mirror your positions, plus a check against OptionStrat's export.
- **ONE import:** a file of the day's fills in the format ONE imports, to cut down manual re-entry.

## What you need

- Windows 10 or 11
- TWS, logged in to your account
- OptionNet Explorer, if you want the ONE reconciliation
- An OptionStrat account (optional)
- Option market data on your IBKR account, for the Greeks. Positions and fills work without it.

## Step-by-step setup

### 1. Install Python

1. Download Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/).
2. In the installer, tick **Add python.exe to PATH**, then click **Install Now**.

### 2. Download the app

1. On this GitHub page, click the green **Code** button, then **Download ZIP**.
2. Unzip it somewhere permanent, such as `Documents\TWS-Matcher`.

### 3. Install what it depends on

Open the folder in File Explorer, click the address bar, type `cmd` and press Enter. Then run:

```bash
pip install -r requirements.txt
```

### 4. Allow API connections in TWS

In TWS, open **Global Configuration > API > Settings** and set:

1. Tick **Enable ActiveX and Socket Clients**.
2. Set **Socket port** to `7496` (live) or `7497` (paper).
3. Add `127.0.0.1` to **Trusted IPs**.
4. Click **OK**.

The app expects port `7496`. To use paper trading, change the port near the top of `canonical_engine.py` to `7497`.

### 5. Create your settings file

1. Make a copy of `config.example.json` and name it `config.json`.
2. Open `config.json` in Notepad and fill in:

| Setting | What to enter |
|---|---|
| `account_map` | Each ONE account name and its IBKR account ID, e.g. `"My ONE Account": "U1234567"`. Several ONE accounts can point at one IBKR account. |
| `ignore_one_accounts` | ONE accounts to skip, such as test or backtest accounts. |
| `account_codes` | A short code for each IBKR account, e.g. `"U1234567": "MAIN"`. |
| `flex_timezone` | The time zone your PC runs in, e.g. `America/New_York`. |
| `one_export_dirs` | Folders the app searches for ONE and OptionStrat exports. |
| `naming` | Optional: your own account, strategy and structure codes, for the Naming convention tab. |
| `email` | Optional: for emailed reports. Leave `enabled` as `false` to skip. Gmail needs an [App Password](https://support.google.com/accounts/answer/185833), not your normal password. |

`config.json` stays on your PC. If you ever put this folder on GitHub, the included `.gitignore` keeps it out.

### 6. Export your ONE positions

1. In ONE, export the **Summary Report** as CSV to your Downloads or Documents folder.
2. Do this again whenever your ONE positions change. The app always reads the newest export, so the dashboard is only as current as your last export.

For the OptionStrat check, also download OptionStrat's `all-active` report (XLSX) to the same folder.

### 7. Start the app

1. Make sure TWS is open and logged in.
2. Double-click `launch_dashboard.bat`.
3. Your browser opens <http://127.0.0.1:8787/>. Give it a minute for the first refresh. Greeks take a few minutes longer.

To stop the app, close its command window.

### 8. Optional: start automatically with TWS

Run this once from the app folder:

```bash
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

TWS Matcher will then start about 90 seconds after TWS opens. The script prints how to remove this again.

## Troubleshooting

| What you see | What to try |
|---|---|
| Dashboard says it can't connect to TWS | Check TWS is logged in, then recheck step 4, especially the port. |
| Port 8787 already in use | The app is probably already running. Close the old command window first. |
| Every leg shows as different | Check that the ONE account names in `account_map` exactly match ONE. |
| ONE data looks out of date | Export the Summary Report from ONE again. |
| Greeks are missing | Check your option market data subscriptions. Stock options only quote during US market hours, so outside them the app shows labelled estimates from the prior close. |
| `pip` or `python` is not recognised | Reinstall Python with **Add python.exe to PATH** ticked. |

## Running the tests

```bash
pip install pytest
python -m pytest -q
```

## Disclaimer

This is a personal tool, shared as is with no warranty. Greeks, P&L and reconciliation results can be wrong or out of date, so always check against TWS before acting. Nothing here is financial advice.
