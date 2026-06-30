# The Match Sheet — automated daily World Cup predictions

This repo runs itself. Every morning, GitHub's servers (not your computer, not Claude)
wake up, pull the day's real World Cup fixtures and yesterday's real results, ask Claude
to write fresh in-depth predictions and grade what already happened, and push the
updated site live. You never have to open this repo again once it's set up.

## What's in here

- `index.html` — the live site (this is what GitHub Pages serves)
- `template.html` — the site's HTML/React shell; the script fills in the data
- `update_sheet.py` — the script that does the daily work
- `data/log.json` — the running history of every prediction ever made
- `.github/workflows/daily-update.yml` — tells GitHub *when* to run the script

## One-time setup (about 10 minutes)

### 1. Create the GitHub repository

1. Go to **github.com** and sign in (or create a free account)
2. Click **+** (top right) → **New repository**
3. Name it anything, e.g. `wc-predictions` — set it to **Public**
4. Click **Create repository**

### 2. Upload everything in this folder

1. On your new repo's page, click **uploading an existing file**
2. Drag in **every file and folder** from this package, keeping the same structure:
   - `index.html`
   - `template.html`
   - `update_sheet.py`
   - `data/log.json`
   - `.github/workflows/daily-update.yml`
3. Click **Commit changes**

   *(If GitHub's drag-and-drop won't preserve folders, see "Alternative: using git
   directly" at the bottom of this file.)*

### 3. Get a free Anthropic API key

1. Go to **console.anthropic.com**
2. Sign up or log in
3. Go to **Settings → API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-...`) — you won't be able to see it again, so
   keep it somewhere for a moment

This costs a small amount per use — generating a day's worth of predictions and
grading is typically a few cents. Running it daily for the rest of the tournament
should cost well under $5 total.

### 4. Add the API key as a GitHub secret

This lets the automation use your key without ever exposing it publicly.

1. In your repo, click **Settings** (top tab, not your account settings)
2. In the left sidebar: **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `ANTHROPIC_API_KEY`
5. Value: paste your key from step 3
6. Click **Add secret**

### 5. Turn on GitHub Pages

1. Still in **Settings**, click **Pages** in the left sidebar
2. Under "Source," select **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)` → **Save**
4. Wait about a minute, refresh the page — your live URL will appear, looking like:
   `https://yourusername.github.io/wc-predictions`

Bookmark that URL. That's your site, forever, on any device.

### 6. Run it once manually to confirm it works

1. Click the **Actions** tab at the top of your repo
2. Click **Update The Match Sheet** in the left sidebar
3. Click **Run workflow** (right side) → **Run workflow** again to confirm
4. Wait 1–2 minutes, then refresh — click into the run to see its log if you want
   to watch what it did
5. Visit your site URL — it should now reflect anything new

If it succeeds, you're done. It will now run automatically every morning at
**11:00 UTC** (roughly 6–7 AM US Eastern, depending on daylight saving) without you
doing anything.

## Changing the schedule

Open `.github/workflows/daily-update.yml` and edit this line:

```yaml
- cron: "0 11 * * *"
```

The format is `minute hour * * *` in **UTC time**, not your local time. For example,
`0 13 * * *` runs at 1:00 PM UTC. Save the change and commit it — the new schedule
takes effect immediately.

## How grading works

Each morning, before writing any new predictions, the script checks every match
in `data/log.json` that doesn't have a `result` yet. If the real fixture data now
shows a final score for that match, it fills it in and asks Claude to write an
honest writeup — including saying plainly when a prediction was wrong and why.
Matches with no reported score yet are left alone; the script never guesses or
invents a result.

## Where the fixture and score data comes from

The script pulls from a free, public-domain, no-signup-required World Cup dataset
(`openfootball/worldcup.json` on GitHub). It's maintained by hand roughly once a
day, so there can be a several-hour lag between a match ending and its score
appearing — that's why the schedule defaults to mid-morning UTC, to give the
previous day's late games time to be recorded.

## If something breaks

Check the **Actions** tab → click the failed run → it shows exactly which step
failed and why. Common issues:

- **401 / API key rejected** — the `ANTHROPIC_API_KEY` secret is missing or wrong;
  redo step 4 above.
- **No fixtures found for today** — this is normal on World Cup rest days between
  rounds; the script simply does nothing that day.
- **Nothing committed** — also normal if there was genuinely nothing new to grade
  or predict.

## Alternative: using git directly (if you're comfortable with a terminal)

```bash
git clone https://github.com/yourusername/wc-predictions.git
cd wc-predictions
# copy all the files from this package in here, preserving folder structure
git add .
git commit -m "Initial setup"
git push
```

Then continue from step 3 above.
