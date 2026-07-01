#!/usr/bin/env python3
"""
The Match Sheet — daily automation script.

Runs once a day (via GitHub Actions). On each run it:
  1. Pulls the full World Cup 2026 fixture/result list from the free,
     key-free openfootball dataset.
  2. Loads data/log.json, the running prediction history.
  3. Grades any of yesterday's predictions whose real result is now known.
  4. Finds today's fixtures and, for any that aren't already predicted,
     fetches betting odds from API-Football, then asks the Claude API to
     write an in-depth prediction (analysis, dual confidence ratings,
     two alternate scenarios, and a "going against the market" note when
     the pick differs from the implied market favourite).
  5. Computes a status (upcoming / final) and tournament context
     (round label, day-of-tournament counter) for every match.
  6. Writes data/log.json back out and regenerates index.html from it.

Designed to fail safely: odds fetching is best-effort — if API-Football
is unavailable the script continues without odds rather than failing
entirely. Matches are either "upcoming" or "final"; true in-progress
live scoring is handled client-side via KickoffAPI.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

FIXTURES_URL = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# API-Football (api-sports.io) — for pre-match betting odds.
# Free tier: 100 requests/day. Sign up at https://dashboard.api-football.com/
# Set the key as a GitHub secret named API_FOOTBALL_KEY.
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_WC_LEAGUE = 1       # FIFA World Cup
API_FOOTBALL_WC_SEASON = 2026

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(REPO_ROOT, "data", "log.json")
HTML_PATH = os.path.join(REPO_ROOT, "index.html")
TEMPLATE_PATH = os.path.join(REPO_ROOT, "template.html")

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


# ───────────────────────── networking helpers ─────────────────────────

def fetch_json(url, retries=MAX_RETRIES):
    """GET a URL and parse JSON, with basic retry on transient failures."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "match-sheet-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            last_err = e
            print(f"  fetch attempt {attempt}/{retries} failed: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(f"Could not fetch {url} after {retries} attempts: {last_err}")


def call_claude(api_key, system_prompt, user_prompt, max_tokens=2000, retries=MAX_RETRIES):
    """Call the Anthropic Messages API and return the text content."""
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                ANTHROPIC_URL,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                raise RuntimeError(f"No text block in Claude response: {data}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code}: {err_body}"
            print(f"  Claude call attempt {attempt}/{retries} failed: {last_err}", file=sys.stderr)
            if e.code == 401:
                raise RuntimeError("Anthropic API key was rejected (401). Check the ANTHROPIC_API_KEY secret.")
            if attempt < retries:
                time.sleep(RETRY_DELAY_SECONDS)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            last_err = str(e)
            print(f"  Claude call attempt {attempt}/{retries} failed: {last_err}", file=sys.stderr)
            if attempt < retries:
                time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(f"Claude API call failed after {retries} attempts: {last_err}")


# ───────────────────────── betting odds helpers ─────────────────────────

def fetch_api_football(api_key, endpoint, params=None):
    """Call the API-Football v3 API. Returns the parsed JSON or None on failure.
    Fails silently so the rest of the script can continue without odds."""
    if not api_key:
        return None
    url = f"{API_FOOTBALL_URL}/{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    try:
        req = urllib.request.Request(url, headers={
            "x-apisports-key": api_key,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  API-Football call failed for {endpoint}: {e}", file=sys.stderr)
        return None


def get_fixture_id(api_football_key, team1, team2, date_str):
    """Find the API-Football fixture ID for a given match by team names and date."""
    data = fetch_api_football(api_football_key, "fixtures", {
        "league": API_FOOTBALL_WC_LEAGUE,
        "season": API_FOOTBALL_WC_SEASON,
        "date": date_str,
    })
    if not data:
        return None
    for fx in data.get("response", []):
        home = fx.get("teams", {}).get("home", {}).get("name", "").lower()
        away = fx.get("teams", {}).get("away", {}).get("name", "").lower()
        t1, t2 = team1.lower(), team2.lower()
        # fuzzy match: check if either team name is contained within the other
        if (t1 in home or home in t1) and (t2 in away or away in t2):
            return fx["fixture"]["id"]
        if (t2 in home or home in t2) and (t1 in away or away in t1):
            return fx["fixture"]["id"]
    return None


def fetch_match_odds(api_football_key, team1, team2, date_str):
    """Fetch 1X2 pre-match odds for a fixture. Returns a dict with keys
    home_odds, draw_odds, away_odds, bookmaker, or None if unavailable."""
    fixture_id = get_fixture_id(api_football_key, team1, team2, date_str)
    if not fixture_id:
        print(f"  No API-Football fixture ID found for {team1} vs {team2}", file=sys.stderr)
        return None

    data = fetch_api_football(api_football_key, "odds", {
        "fixture": fixture_id,
        "bet": 1,   # bet ID 1 = Match Winner (1X2) across all bookmakers
    })
    if not data:
        return None

    # Walk through bookmakers to find the first clean 1X2 result
    for bookie_entry in data.get("response", []):
        bookmaker = bookie_entry.get("bookmakers", [])
        for bk in bookmaker:
            for bet in bk.get("bets", []):
                if bet.get("name") == "Match Winner":
                    values = {v["value"]: float(v["odd"]) for v in bet.get("values", [])}
                    if "Home" in values and "Draw" in values and "Away" in values:
                        return {
                            "home": values["Home"],
                            "draw": values["Draw"],
                            "away": values["Away"],
                            "bookmaker": bk.get("name", "Odds"),
                            "fixture_id": fixture_id,
                        }
    print(f"  No 1X2 odds found for fixture {fixture_id}", file=sys.stderr)
    return None


def odds_to_implied_prob(decimal_odds):
    """Convert decimal odds to implied probability percentage."""
    if not decimal_odds or decimal_odds <= 0:
        return 0
    return round((1 / decimal_odds) * 100)


def market_favourite(odds):
    """Return 'home', 'draw', or 'away' — whichever the market favours."""
    if not odds:
        return None
    return min(["home", "draw", "away"], key=lambda k: odds[k])


def prediction_vs_market(prediction, odds, home_name, away_name):
    """Compare a predicted scoreline to the market favourite.
    Returns a dict with: going_against (bool), market_fav (str),
    market_fav_name (str), and a summary string."""
    if not odds:
        return None
    pred_diff = prediction["home"] - prediction["away"]
    pred_outcome = "home" if pred_diff > 0 else "away" if pred_diff < 0 else "draw"
    fav = market_favourite(odds)
    going_against = pred_outcome != fav
    fav_name = home_name if fav == "home" else away_name if fav == "away" else "a draw"
    return {
        "going_against": going_against,
        "predicted_outcome": pred_outcome,
        "market_favourite": fav,
        "market_favourite_name": fav_name,
        "home_implied": odds_to_implied_prob(odds["home"]),
        "draw_implied": odds_to_implied_prob(odds["draw"]),
        "away_implied": odds_to_implied_prob(odds["away"]),
    }


# ───────────────────────── fixture data helpers ─────────────────────────

def normalize_fixtures(raw):
    """Turn the openfootball schema into a flat list of match dicts keyed by date."""
    out = []
    for m in raw.get("matches", []):
        score = m.get("score") or {}
        ft = score.get("ft")
        out.append({
            "date": m.get("date"),
            "time": m.get("time", ""),
            "round": m.get("round", ""),
            "group": m.get("group", ""),
            "ground": m.get("ground", ""),
            "team1": m.get("team1"),
            "team2": m.get("team2"),
            "home_score": ft[0] if ft else None,
            "away_score": ft[1] if ft else None,
            "decided": ft is not None,
        })
    return out


def team_recent_form(fixtures, team, before_date, n=3):
    """Build a short human-readable recent-form string for a team."""
    played = [
        f for f in fixtures
        if f["decided"] and f["date"] and f["date"] < before_date
        and (f["team1"] == team or f["team2"] == team)
    ]
    played.sort(key=lambda f: f["date"])
    recent = played[-n:]
    if not recent:
        return f"No completed matches on record yet for {team}."
    parts = []
    for f in recent:
        is_home = f["team1"] == team
        gf = f["home_score"] if is_home else f["away_score"]
        ga = f["away_score"] if is_home else f["home_score"]
        opp = f["team2"] if is_home else f["team1"]
        if gf > ga:
            res = "W"
        elif gf < ga:
            res = "L"
        else:
            res = "D"
        parts.append(f"{res} {gf}-{ga} vs {opp}")
    return "; ".join(parts)


def match_id(date, team1, team2):
    slug = lambda s: "".join(c.lower() if c.isalnum() else "" for c in s)[:4]
    return f"{slug(team1)}-{slug(team2)}-{date}"


def tournament_context(fixtures, for_date):
    """Compute a 'Day N of the tournament' style context string for a given date,
    derived from the actual span of dates in the fixture list (no hardcoded dates)."""
    all_dates = sorted(set(f["date"] for f in fixtures if f["date"]))
    if not all_dates or for_date not in all_dates:
        # for_date might fall on a rest day between rounds — find its position
        # relative to the full span instead of requiring an exact match.
        if not all_dates:
            return None
        start = datetime.strptime(all_dates[0], "%Y-%m-%d")
        end = datetime.strptime(all_dates[-1], "%Y-%m-%d")
        cur = datetime.strptime(for_date, "%Y-%m-%d")
        if cur < start or cur > end:
            return None
        day_num = (cur - start).days + 1
        total_days = (end - start).days + 1
        return {"day": day_num, "totalDays": total_days}
    start = datetime.strptime(all_dates[0], "%Y-%m-%d")
    end = datetime.strptime(all_dates[-1], "%Y-%m-%d")
    cur = datetime.strptime(for_date, "%Y-%m-%d")
    day_num = (cur - start).days + 1
    total_days = (end - start).days + 1
    return {"day": day_num, "totalDays": total_days}


def match_status(fixture_decided):
    """Matches are only ever 'upcoming' or 'final' — true live/in-progress
    tracking is intentionally out of scope (see module docstring)."""
    return "final" if fixture_decided else "upcoming"


# ───────────────────────── log (prediction history) ─────────────────────────

def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entries": []}


def save_log(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def find_entry(log, date):
    for entry in log["entries"]:
        if entry["date"] == date:
            return entry
    return None


def find_match_in_log(log, mid):
    for entry in log["entries"]:
        for m in entry["matches"]:
            if m["id"] == mid:
                return m
    return None


# ───────────────────────── grading ─────────────────────────

def outcome_label(predicted, actual):
    """Classify a graded prediction as correct / close / wrong."""
    p_diff = predicted["home"] - predicted["away"]
    a_diff = actual["home"] - actual["away"]
    p_dir = "home" if p_diff > 0 else "away" if p_diff < 0 else "draw"
    a_dir = "home" if a_diff > 0 else "away" if a_diff < 0 else "draw"

    exact = predicted["home"] == actual["home"] and predicted["away"] == actual["away"]
    if exact:
        return "correct"
    if p_dir == a_dir:
        # Right winner/draw call. "Close" if goal difference is within 1 and
        # total goals are within 2, otherwise still a make on direction but
        # call it close by default since the hardest part (who wins) was right.
        return "close"
    return "wrong"


def grade_predictions(log, fixtures_by_id):
    """Walk the log; for any ungraded match whose real result is now known,
    fill in result + verdict. Returns the list of newly graded matches."""
    newly_graded = []
    for entry in log["entries"]:
        for m in entry["matches"]:
            if m.get("result"):
                m["status"] = "final"  # belt-and-suspenders for older entries
                continue  # already graded
            fx = fixtures_by_id.get(m["id"])
            if not fx or not fx["decided"]:
                m.setdefault("status", "upcoming")  # backfill for pre-status log entries
                continue  # real result not known yet — leave pending, don't guess
            actual = {"home": fx["home_score"], "away": fx["away_score"]}
            outcome = outcome_label(m["prediction"], actual)
            m["result"] = {"home": actual["home"], "away": actual["away"], "note": ""}
            m["verdict"] = {"outcome": outcome, "writeup": None}  # writeup filled by Claude below
            m["status"] = "final"
            newly_graded.append(m)
    return newly_graded


def write_grading_writeups(api_key, newly_graded):
    """Ask Claude for a short, honest writeup for each newly graded match."""
    for m in newly_graded:
        outcome = m["verdict"]["outcome"]
        prompt = (
            f"You predicted {m['home']['name']} {m['prediction']['home']}-{m['prediction']['away']} "
            f"{m['away']['name']}. Your stated winner confidence was {m['winnerConfidence']}% and "
            f"score confidence was {m['scoreConfidence']}%. Your pre-match reasoning was:\n\n"
            + "\n".join(f"- {a['heading']}: {a['body']}" for a in m["analysis"])
            + f"\n\nThe actual final score was {m['home']['name']} {m['result']['home']}-"
            f"{m['result']['away']} {m['away']['name']}. Your grade for this prediction is '{outcome}'.\n\n"
            "Write a short (2-4 sentence), honest, specific grading writeup: what you got right or "
            "wrong, and WHY — reference the actual reasoning above and what actually happened, not "
            "generic commentary. If you were wrong, say plainly what assumption broke. If correct, "
            "say what signal proved out. Plain prose, no headers, first person ('I predicted...')."
        )
        try:
            writeup = call_claude(
                api_key,
                system_prompt="You are the analyst behind 'The Match Sheet', a World Cup prediction column. You grade your own past predictions honestly, including admitting clearly when you were wrong.",
                user_prompt=prompt,
                max_tokens=400,
            )
            m["verdict"]["writeup"] = writeup.strip()
        except Exception as e:
            print(f"  Warning: could not generate writeup for {m['id']}: {e}", file=sys.stderr)
            m["verdict"]["writeup"] = (
                f"Predicted {m['prediction']['home']}-{m['prediction']['away']}, "
                f"actual was {m['result']['home']}-{m['result']['away']}."
            )


# ───────────────────────── prediction generation ─────────────────────────

PREDICTION_SYSTEM_PROMPT = """You are the analyst behind "The Match Sheet", a World Cup 2026 daily \
prediction column. For the match described, write an in-depth, opinionated prediction in STRICT JSON \
matching this schema and nothing else (no markdown fences, no preamble):

{
  "analysis": [
    {"heading": "short heading", "body": "3-5 sentence paragraph of real tactical/form reasoning"},
    {"heading": "short heading", "body": "..."},
    {"heading": "short heading", "body": "..."}
  ],
  "prediction": {"home": <int>, "away": <int>},
  "winnerConfidence": <int 0-100, how sure you are of the WINNER/DRAW outcome>,
  "scoreConfidence": <int 0-100, how sure you are of the EXACT scoreline — always much lower than winnerConfidence>,
  "confidenceNote": "one sentence explaining the gap between the two confidence numbers",
  "goingAgainstMarket": <true if your predicted outcome differs from the market favourite, false if it agrees or no odds available>,
  "againstMarketReason": "if goingAgainstMarket is true: 2-3 sentences explaining specifically why you think the market is wrong. If false: empty string.",
  "alternates": [
    {
      "prediction": {"home": <int>, "away": <int>},
      "likelihood": "<short 2-4 word label, e.g. 'Live underdog', 'Tighter margin', 'Bigger margin', 'Different winner', 'Draw scenario'>",
      "reasoning": "1-2 sentences on the specific condition that would produce THIS outcome instead of your main pick"
    },
    {
      "prediction": {"home": <int>, "away": <int>},
      "likelihood": "<a different short label than the first alternate>",
      "reasoning": "1-2 sentences on the specific condition that would produce THIS outcome instead"
    }
  ]
}

The two alternates must be genuinely different from your main prediction and from each other. \
If betting odds are provided, compare your pick against the market and be explicit about whether \
you agree or disagree with where the money is. When going against the market, explain exactly why — \
reference specific form data, tactical mismatches, or historical patterns the market may be \
underweighting. Do not invent player injuries or statistics not in the data provided. \
Be specific and opinionated. Output ONLY the JSON object."""


def validate_prediction_schema(parsed):
    """Raise a clear error if the parsed prediction is missing required fields."""
    required_top = ["analysis", "prediction", "winnerConfidence", "scoreConfidence",
                    "confidenceNote", "alternates", "goingAgainstMarket", "againstMarketReason"]
    missing = [k for k in required_top if k not in parsed]
    if missing:
        raise ValueError(f"Prediction response missing required field(s): {missing}")
    if not isinstance(parsed["alternates"], list) or len(parsed["alternates"]) < 2:
        raise ValueError(f"Expected at least 2 alternates, got: {parsed.get('alternates')}")
    for alt in parsed["alternates"]:
        for k in ["prediction", "likelihood", "reasoning"]:
            if k not in alt:
                raise ValueError(f"Alternate missing required field '{k}': {alt}")


def generate_prediction(api_key, fixture, home_form, away_form, odds=None, max_attempts=2):
    odds_line = ""
    if odds:
        odds_line = (
            f"\nBetting market odds (1X2, decimal): "
            f"{fixture['team1']} {odds['home']} | Draw {odds['draw']} | {fixture['team2']} {odds['away']}"
            f"\nImplied probabilities: {fixture['team1']} {odds_to_implied_prob(odds['home'])}% | "
            f"Draw {odds_to_implied_prob(odds['draw'])}% | {fixture['team2']} {odds_to_implied_prob(odds['away'])}%"
        )
    user_prompt = (
        f"Match: {fixture['team1']} vs {fixture['team2']}\n"
        f"Round: {fixture['round']}\n"
        f"Venue: {fixture['ground']}\n"
        f"Kickoff: {fixture['date']} {fixture['time']}\n"
        f"{fixture['team1']} recent results: {home_form}\n"
        f"{fixture['team2']} recent results: {away_form}"
        + odds_line
    )
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            raw = call_claude(api_key, PREDICTION_SYSTEM_PROMPT, user_prompt, max_tokens=1800)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw.strip())
            validate_prediction_schema(parsed)
            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            print(f"  Prediction attempt {attempt}/{max_attempts} produced invalid schema: {e}", file=sys.stderr)
    raise RuntimeError(f"Could not get a valid prediction after {max_attempts} attempts: {last_err}")


def build_todays_predictions(api_key, fixtures, today_str, only_ids=None, api_football_key=None):
    """Generate predictions for today's fixtures. If only_ids is given, only
    fixtures whose match_id is in that set are processed."""
    todays_fixtures = [f for f in fixtures if f["date"] == today_str]
    if only_ids is not None:
        todays_fixtures = [
            f for f in todays_fixtures
            if match_id(f["date"], f["team1"], f["team2"]) in only_ids
        ]
    matches = []
    for fx in todays_fixtures:
        mid = match_id(fx["date"], fx["team1"], fx["team2"])
        home_form = team_recent_form(fixtures, fx["team1"], today_str)
        away_form = team_recent_form(fixtures, fx["team2"], today_str)

        # Fetch betting odds — best-effort, None if unavailable
        odds = None
        if api_football_key:
            print(f"  Fetching odds for {fx['team1']} vs {fx['team2']}...")
            odds = fetch_match_odds(api_football_key, fx["team1"], fx["team2"], today_str)
            if odds:
                print(f"    Odds: {fx['team1']} {odds['home']} | Draw {odds['draw']} | {fx['team2']} {odds['away']}")

        print(f"  Predicting {fx['team1']} vs {fx['team2']}...")
        try:
            pred = generate_prediction(api_key, fx, home_form, away_form, odds=odds)
        except Exception as e:
            print(f"  ERROR generating prediction for {fx['team1']} vs {fx['team2']}: {e}", file=sys.stderr)
            continue

        # Build the market comparison object stored with the match
        market = None
        if odds:
            market = prediction_vs_market(pred["prediction"], odds, fx["team1"], fx["team2"])
            market["home_odds"] = odds["home"]
            market["draw_odds"] = odds["draw"]
            market["away_odds"] = odds["away"]
            market["bookmaker"] = odds.get("bookmaker", "")

        matches.append({
            "id": mid,
            "kickoff": fx["time"] or "Time TBD",
            "venue": fx["ground"] or "Venue TBD",
            "round": fx["round"] or "World Cup 2026",
            "home": {"name": fx["team1"], "code": fx["team1"][:3].upper()},
            "away": {"name": fx["team2"], "code": fx["team2"][:3].upper()},
            "homeForm": home_form,
            "awayForm": away_form,
            "analysis": pred["analysis"],
            "prediction": pred["prediction"],
            "winnerConfidence": pred["winnerConfidence"],
            "scoreConfidence": pred["scoreConfidence"],
            "confidenceNote": pred["confidenceNote"],
            "goingAgainstMarket": pred.get("goingAgainstMarket", False),
            "againstMarketReason": pred.get("againstMarketReason", ""),
            "market": market,
            "alternates": pred.get("alternates", []),
            "status": match_status(fx["decided"]),
            "result": None,
            "verdict": None,
        })
    return matches



# ───────────────────────── HTML rendering ─────────────────────────

def render_html(log):
    """Inject the log data into the template's placeholder."""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()
    log_json = json.dumps(log, ensure_ascii=False)
    out = template.replace("__MATCH_SHEET_LOG_JSON__", log_json)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(out)


# ───────────────────────── main ─────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # API-Football key is optional — odds fetching is best-effort
    api_football_key = os.environ.get("API_FOOTBALL_KEY")
    if api_football_key:
        print("API-Football key found — betting odds will be fetched.")
    else:
        print("No API_FOOTBALL_KEY found — predictions will be written without betting odds.")

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    print(f"Run started at {now.isoformat()} UTC. Today (UTC) = {today_str}")

    print("Fetching fixture/result data...")
    raw = fetch_json(FIXTURES_URL)
    fixtures = normalize_fixtures(raw)
    fixtures_by_id = {match_id(f["date"], f["team1"], f["team2"]): f for f in fixtures}
    print(f"  Loaded {len(fixtures)} total tournament fixtures.")

    log = load_log()

    print("Grading any predictions whose real result is now known...")
    newly_graded = grade_predictions(log, fixtures_by_id)
    if newly_graded:
        print(f"  {len(newly_graded)} prediction(s) newly graded. Writing grading commentary...")
        write_grading_writeups(api_key, newly_graded)
    else:
        print("  Nothing new to grade.")

    existing_today = find_entry(log, today_str)
    already_predicted_ids = set()
    if existing_today:
        already_predicted_ids = {m["id"] for m in existing_today["matches"]}

    all_todays_fixture_ids = {
        match_id(f["date"], f["team1"], f["team2"])
        for f in fixtures if f["date"] == today_str
    }
    missing_ids = all_todays_fixture_ids - already_predicted_ids

    if not all_todays_fixture_ids:
        print(f"No fixtures found in the data source for today ({today_str}).")
    elif not missing_ids:
        print(f"Today ({today_str}) already has all {len(all_todays_fixture_ids)} known fixture(s) predicted. Nothing new to generate.")
    else:
        print(f"Today ({today_str}) has {len(missing_ids)} fixture(s) not yet predicted (out of {len(all_todays_fixture_ids)} total). Generating...")
        new_matches = build_todays_predictions(api_key, fixtures, today_str, only_ids=missing_ids, api_football_key=api_football_key)
        if new_matches:
            if existing_today:
                existing_today["matches"].extend(new_matches)
            else:
                log["entries"].append({"date": today_str, "matches": new_matches})
            print(f"  Added {len(new_matches)} new prediction(s) for today.")
        else:
            print("  All missing fixtures failed to generate a valid prediction this run — will retry on the next run.")

    log["entries"].sort(key=lambda e: e["date"])

    # Backfill tournament context (day N of total) on every entry, including
    # older ones — cheap to recompute and keeps it correct if the underlying
    # fixture list's span ever shifts (e.g. a postponed match).
    for entry in log["entries"]:
        ctx = tournament_context(fixtures, entry["date"])
        if ctx:
            entry["tournamentContext"] = ctx

    print("Saving log and rebuilding index.html...")
    save_log(log)
    render_html(log)
    print("Done.")


if __name__ == "__main__":
    main()
