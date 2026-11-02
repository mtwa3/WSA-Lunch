# WSA Lower School Lunch → Google Calendar (auto-synced)

Keeps a read-only Google Calendar showing the **whole current month + the whole
next month** of Lower School lunch. FLIK's menus are re-read once a day by GitHub Actions, which
commits a calendar file that GitHub Pages serves; Google Calendar subscribes to
that URL. No manual importing ever again — and because each day keeps one
permanent event ID, a menu edit **updates the existing entry instead of creating
a duplicate**.

| | |
|---|---|
| Feed URL | `https://YOU.github.io/REPO/wsa-lunch.ics` |
| Freshness | rebuilt daily; window = current month + next month; Google refetches every ~8–24 h |
| Unpublished days | shown as “Menu not yet published” (`--include-placeholders`); remove that flag to hide them |
| Freshness label | every entry ends with “Last checked <date time>” so you can see how recent the data is; set `DISPLAY_TZ` at the top of the script (e.g. `"America/Chicago"`) to show your own timezone |
| Scope | lunch only · Mon–Fri · all-day · **no reminders** |
| Secrets | none required |

## Files

| File | Purpose |
|---|---|
| `wsa_lunch_sync.py` | fetches FLIK menus, writes the feed, remembers what changed |
| `.github/workflows/wsa-lunch-sync.yml` | runs it daily and commits the result |
| `wsa-lunch.ics` | **generated** — the calendar Google subscribes to |
| `.wsa-state.json` | **generated** — per-day content hash + SEQUENCE, so only real changes are rewritten |

## First-time setup

1. Create a **public** repo containing `wsa_lunch_sync.py`, `README.md`,
   `.gitignore`, and this workflow at the exact path
   `.github/workflows/wsa-lunch-sync.yml`.
2. **Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)` → Save.**
3. **Actions → wsa-lunch-sync → Run workflow** once, so it publishes right away.
4. Subscribe in Google Calendar (desktop browser): **Other calendars → + → From
   URL**, paste the feed URL, then **Add calendar**.
5. Give the calendar a colour. From here on it maintains itself.

## Day to day

Nothing to do. If you're curious, check the Actions tab: a commit named
`menu sync <date>` means FLIK changed a menu; "No menu changes - nothing to
commit" means they did not. Edits show up in Google within about a day.

## Tuning the window

```bash
python wsa_lunch_sync.py --months 2                        # whole current month + next month (what the workflow uses)
python wsa_lunch_sync.py --days 7  --back-days 45         # rolling: 1 week ahead, 6 weeks of history
python wsa_lunch_sync.py --days 10 --back-days 0          # 2 weeks ahead, no history
python wsa_lunch_sync.py --start 2026-11-02 --end 2027-05-26   # whole term (see caveat)
python wsa_lunch_sync.py --dry-run                        # preview, write nothing
```

The GitHub workflow runs `--months 2 --include-placeholders`: everything from the
1st of this month through the last day of next month. The window is recomputed
every day, so on the 1st it automatically slides forward to cover the next pair
of months. Days FLIK has not published yet appear as “Menu not yet published -
check back closer to the date”; drop `--include-placeholders` from the workflow
if you'd rather they be absent until real menus exist.

A subscription **replaces** the calendar on every refetch: any day that is not in
the feed disappears from Google. That is what keeps stale menus from lingering,
but it also means `--back-days` is how much history stays visible.

Other flags: `--include-placeholders` (days FLIK has not published yet),
`--print-feed`, `--out`, `--state`, `--ca-bundle`, `--fixture DIR` (offline test),
`--self-test`. School and meal are constants at the top of the script
(`SCHOOL = "lower-school"`, `MEAL = "lunch"`) — Middle or Upper School is a
one-word change.

## Troubleshooting

- **Calendar looks empty?** Open the feed URL in a browser. If it is stale, check
  the last Actions run (red means it failed). Google can also lag up to a day;
  removing and re-adding the calendar forces an immediate fetch.
- **Duplicate entries?** That comes from *importing* a file rather than
  subscribing. Delete the imported events, then use From URL (step 4).
- **`CERTIFICATE_VERIFY_FAILED` when running this locally on macOS:** that Python
  build ships no CA bundle. Use `/usr/bin/python3`, or run
  `"/Applications/Python 3.x/Install Certificates.command"`, or pass
  `--ca-bundle /etc/ssl/cert.pem`. GitHub runners are unaffected.
- **Nothing runs for ~2 months (summer break)?** GitHub switches off schedules on
  inactive repos. Any commit, or one manual Run workflow, turns it back on.
- **Events are read-only:** expected for subscriptions. For editable events written
  directly into your calendar, use the `--gcal` mode in the script instead.
