# Run the scan every weekday (Mac)

The forward scoreboard is the only honest test, and it needs reads on many days. Let the computer do it.

`jevbot scan` asks Jev about your watchlist once, prints a table, and saves every read to
`runs/predictions.jsonl`. Re-running the same day adds nothing (one read per ticker, day and horizon).

## 1. Try it by hand first
```
cd ~/jevtrade && . .venv/bin/activate
export TYPESAFE_API_KEY="your-key"
jevbot scan --symbols AAPL,MSFT,NVDA --h 5
```

## 2. Schedule it with launchd (runs 22:30 local time, Mon to Fri)
Create `~/Library/LaunchAgents/com.jevbot.scan.plist` (replace YOURNAME and the key):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jevbot.scan</string>
  <key>WorkingDirectory</key><string>/Users/YOURNAME/jevtrade</string>
  <key>EnvironmentVariables</key><dict>
    <key>TYPESAFE_API_KEY</key><string>PASTE-KEY-HERE</string>
  </dict>
  <key>ProgramArguments</key><array>
    <string>/Users/YOURNAME/jevtrade/.venv/bin/jevbot</string><string>scan</string>
  </array>
  <key>StartCalendarInterval</key><array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/YOURNAME/jevtrade/runs/scan.log</string>
  <key>StandardErrorPath</key><string>/Users/YOURNAME/jevtrade/runs/scan.log</string>
</dict></plist>
```
Then: `chmod 600 ~/Library/LaunchAgents/com.jevbot.scan.plist && launchctl load ~/Library/LaunchAgents/com.jevbot.scan.plist`

The key sits in that file, so keep it private and use a key with a low spend limit. A sleeping Mac does not run it;
launchd runs missed jobs when the Mac wakes.

## 3. Read the results
Open `jevbot web`, look at "Forward scoreboard", or download the CSV. Give it at least 4 weeks and 10 tickers
before believing anything. The page refuses to call a verdict with fewer than 10 independent signals.
