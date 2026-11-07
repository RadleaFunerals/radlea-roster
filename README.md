# Radlea Roster (v0.2)

Staff roster for Townsville Funerals, Cremations & Crematorium and Burdekin Funerals (Ayr). It covers:

- **Normal hours**: shifts per person per day, each person's usual shifts, paid hours per week.
- **After hours**: a two-person transfer team per location for every night (weeknights) or day (weekends and public holidays).
- **Gentle Way attendances**: a main process or short term system check, given to any staff member marked Gentle Way trained.
- **Staff requests**: staff see published rosters on their phones, mark themselves unavailable and ask to swap. Managers approve or decline.

It runs on its own for now. It is built as two Django apps (`org` and `roster`) so it can move into the FMS repository later without a rewrite (see "Joining the FMS" below).

Third Party Sensitive: the app holds staff names and mobile numbers. No pay rates are stored.

---

## 1. Put it live on DigitalOcean (no command line needed)

Once it's live, staff use it on their phones at an address like `https://radlea-roster-xxxxx.ondigitalocean.app`.

**A. Code on GitHub.** The code goes in a private repository, `RadleaFunerals/radlea-roster`.

**B. Create the app.** In DigitalOcean, go to **Create > App Platform** and choose GitHub, then `RadleaFunerals/radlea-roster`, branch `main`. Tick **Autodeploy**.

- Region: **Sydney**. Size: the smallest web service.
- **Run command**: copy the line from `Procfile` after `web: `.
- HTTP port: 8080 (the default).
- **Add resource > Database > Dev Database** (PostgreSQL), named `db`.

**C. Environment variables** (app level, scope "Run and build time"; tick Encrypt for the secret ones):

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | 50+ random characters (encrypt) |
| `RADLEA_DIRECTOR_PASSWORD` | your first password, 10+ characters (encrypt). Used once, to create your `robin.drury` login |
| `SITE_URL` | `${APP_URL}` |
| `DATABASE_URL` | `${db.DATABASE_URL}` (only if it isn't already listed) |

**D. Create the app.** The first build takes a few minutes. On every start the app sets up the database by itself, loads the staff and public holidays (adding only what's missing, never overwriting your changes), and creates your director login the first time.

**E. Log in** at the app's address as `robin.drury`. Then:

1. **Logins**: press **Create login** for each person. The temporary password shows once; pass it on.
2. **Setup > Staff members**: add mobile numbers (for tap-to-call) and email addresses (for roster emails).
3. **Setup > Public holidays**: add the 2027 Townsville and Burdekin show holidays once gazetted.

**Roster emails (optional, add any time).** Add these variables and the app redeploys:

| Variable | Value |
|---|---|
| `EMAIL_HOST` | `smtp.gmail.com` |
| `EMAIL_HOST_USER` | the sending address, e.g. `info@townsvillefunerals.com.au` |
| `EMAIL_HOST_PASSWORD` | a Google **app password** for that account (needs 2-Step Verification on the account; encrypt) |

Until these are set, publishing still works but no emails go out (they're written to the app log).

## 2. Run it on your own computer (optional, for trying things)

```bash
cd radlea_roster
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DJANGO_DEBUG=1                # Windows: set DJANGO_DEBUG=1
python manage.py migrate
python manage.py seed_radlea --with-logins
python manage.py runserver
```

Open http://127.0.0.1:8000 and log in with a username and temporary password printed by `seed_radlea`.

Tests: `python manage.py test` (42 tests).

### What is loaded automatically

| What | Source |
|---|---|
| 13 staff + Robin and Leanne, with role, Award grade, employment type and home site | Staff Pay Master Register v0.2 (02/11/2026) and payrun 02/11/2026 |
| Usual Mon–Fri shifts for full-time staff (30 min unpaid break, 7.6 paid hours a day) | Register "Usual shift" column |
| Chloe and Graham able to work at both sites | Register |
| Gentle Way trained: Shane and Robin | Robin, 08/11/2026 |
| Natalie edits the Townsville roster, Amie edits Ayr; Robin and Leanne edit both | [ASSUMPTION: confirm] |
| After hours: weeknights 17:00–08:00, weekend and public holiday days 08:00–08:00 | Confirmed by Robin, 08/11/2026 |
| Queensland public holidays, 24/12/2026 to 28/12/2027 | qld.gov.au, checked 08/11/2026 |

## 3. Building a week (Natalie, Amie, directors)

1. Open **Townsville** or **Ayr** and go to the week.
2. **Fill usual shifts** adds everyone's normal Mon–Fri shifts. It skips public holidays, approved leave and days that already have a shift.
3. Add services, casual shifts and changes with **+ add** in any cell. Click a shift to change or remove it.
4. Click each day in the **After hours** row to set the pair. **Copy last week's after hours** fills empty nights from the week before.
5. Add **Gentle Way attendances** in their row (Townsville only).
6. Read **Roster check** below the grid. Red items are errors and amber items are warnings.
7. **Publish to staff**. Staff can't see a week until it is published. Everyone rostered that week who has an email address is emailed their own shifts, after-hours nights (with their partner's name) and Gentle Way attendances. Republishing after changes sends an "UPDATED" email. Anything changed after publishing is also marked UPDATED on staff screens.
8. **Print** gives an A4 landscape copy for the noticeboard.

### Rules the roster enforces

Errors stop the change being saved:

- A person rostered at a site they're not set up for (e.g. Danny at Townsville).
- Overlapping shifts, including shifts that run past midnight.
- Rostering someone during approved unavailability.
- The same person entered twice in an after-hours pair, or put on both sites' teams on the same night.
- Anyone not marked "can do transfers" on the after-hours team.
- Anyone not marked Gentle Way trained on a Gentle Way attendance.

Warnings are shown but allowed:

- An after-hours night with one person or no one.
- A casual shift under 4 paid hours (the timesheet tops it up to 4).
- Full-time paid hours above 38 in the week (check overtime).
- More than 7 after-hours nights in a row [ASSUMPTION: confirm limit].
- An unassigned Gentle Way attendance, or a pending unavailability request.

Publishing is blocked while there are errors, unless "Publish anyway" is ticked.

Every change is written to a change log (Setup > Change logs) that can't be edited.

## 4. Staff (phone)

- **My roster**: your next 4 weeks, including who you're paired with after hours.
- **On call now**: who is on after hours at each site right now and next, with tap-to-call.
- **I'm not available**: send leave or unavailability to your manager. Paid leave is still applied for in Xero as now. This only stops you being rostered.
- **Swap**: ask to hand a shift, after-hours night or Gentle Way attendance to someone else. You can name who has agreed.

Requests go to the editor for the person's site. Approving a swap re-checks every rule against the replacement first.

## 5. Joining the FMS later

- `org.Location` becomes the FMS foundations Location. TVL and BUR codes match D-018 case numbering.
- `org.StaffMember` becomes the FMS staff record (staff module, brief section 6.10).
- `roster` moves across unchanged. Calendar events (services, transfers) can later create Shifts automatically, and "On call now" feeds the first call screen (R-003).
- Timesheets stay in the Netlify app for now. A later step can pre-fill timesheets from the published roster.

## 6. Not in this version

- Two-factor login. The brief requires MFA for the FMS: add it before real go-live.
- SMS. Email goes out on publish. SMS needs an SMS provider account and can be added to `roster/notify.py`.
- Cost estimates and pay calculation. Pay stays with the timesheet app, the Pay Master Register and Xero.
- Qualification and licence expiry tracking.
