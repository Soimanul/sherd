# 60-second demo recording

Use only the synthetic demo database. Capture a fresh terminal and Orca's in-app browser; show the demo banner whenever the browser is visible. Edit the recording to these durations.

| Time | Action and exact command | On screen | Caption |
| --- | --- | --- | --- |
| 0–7 s | Terminal: `sherd demo` | Demo database table counts | “Start with made-up data.” |
| 7–14 s | Terminal: `sherd show --list --demo` | Available insight IDs | “22 built-in digs; no model needed.” |
| 14–21 s | Terminal: `sherd show messages.activity_heatmap --demo` | Hour and weekday insight | “A pattern from local rows.” |
| 21–30 s | Terminal: `sherd web --demo --no-open` | Loopback address; switch to Orca's in-app browser at that address | “A dashboard on this machine.” |
| 30–41 s | Browser: dashboard, then Messages | Headline charts, then messages charts | “Browse by source.” |
| 41–50 s | Browser: select Wrapped in navigation | Six cards, privacy toggles off | “A year in six cards.” |
| 50–60 s | Terminal: `sherd wrapped --demo --out wrapped`; browser returns to `/wrapped` | Six PNG paths, then the Wrapped deck | “Initials only. No amounts. Review before posting.” |

Trim command execution pauses in editing; do not substitute personal exports or enable the Names/Amounts toggles.
