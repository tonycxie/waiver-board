# Waiver board

Type a Sleeper username and see this week's consensus waiver pickups, with FAAB bid estimates, checked against your own Sleeper leagues. Superflex and 1-QB leagues each get their own QB bids and QB order.

**Site:** https://tonycxie.github.io/waiver-board/ (add `?u=<username>` to link straight to someone's leagues)

## How it works
- `data/board.json` holds the week's consensus pickups and FAAB estimates. `data/players.json` holds rest-of-season ranks, byes and dynasty values by Sleeper player id. Both are refreshed each week by Tony's waiver run (`export_site.py` in the Fantasy Football Waiver Manager project).
- Everything league-specific is pulled live from Sleeper's public API in the browser: the user's 2026 leagues, rosters, FAAB balances and completed waiver claims.
- `logic.js` holds the league math (availability, FAAB vs. rivals, lineup check, winning-bid history). It's a port of the league half of `build.py`.

No logins and no stored data. The page remembers the last username in your own browser only.

## Changing the site code
Phones cache files for a while. When you change `app.js`, `logic.js` or `style.css`, bump the `?v=` number on all three references (two in `index.html`, one import in `app.js`) so browsers fetch the new files.
