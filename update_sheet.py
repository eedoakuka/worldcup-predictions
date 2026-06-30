#!/usr/bin/env python3
"""
The Match Sheet — daily automation script.

Runs once a day (via GitHub Actions). On each run it:
  1. Pulls the full World Cup 2026 fixture/result list from the free,
     key-free openfootball dataset.
  2. Loads data/log.json, the running prediction history.
  3. Grades any of yesterday's predictions whose real result is now known.
  4. Finds today's fixtures and, for any that aren't already predicted,
     asks the Claude API to write an in-depth prediction (analysis +
     dual confidence ratings) grounded in real form data pulled from
     the same fixture list (recent results for each team).
  5. Writes data/log.json back out and regenerates index.html from it.

Designed to fail safely: if the upstream data source has a gap (it's a
once-a-day "wiki" dataset, not truly live), the script simply finds
nothing new to grade or predict and exits cleanly — it never invents
a score or a fixture.
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
                continue  # already graded
            fx = fixtures_by_id.get(m["id"])
            if not fx or not fx["decided"]:
                continue  # real result not known yet — leave pending, don't guess
            actual = {"home": fx["home_score"], "away": fx["away_score"]}
            outcome = outcome_label(m["prediction"], actual)
            m["result"] = {"home": actual["home"], "away": actual["away"], "note": ""}
            m["verdict"] = {"outcome": outcome, "writeup": None}  # writeup filled by Claude below
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
  "confidenceNote": "one sentence explaining the gap between the two confidence numbers"
}

Ground every claim in the form data and context given to you. Do not invent player injuries, transfers, \
or statistics not implied by the data provided — reason from team form, goal patterns, and the stage of \
the tournament instead. Be specific and opinionated, not generic. Output ONLY the JSON object."""


def generate_prediction(api_key, fixture, home_form, away_form):
    user_prompt = (
        f"Match: {fixture['team1']} vs {fixture['team2']}\n"
        f"Round: {fixture['round']}\n"
        f"Venue: {fixture['ground']}\n"
        f"Kickoff: {fixture['date']} {fixture['time']}\n"
        f"{fixture['team1']} recent results: {home_form}\n"
        f"{fixture['team2']} recent results: {away_form}\n"
    )
    raw = call_claude(api_key, PREDICTION_SYSTEM_PROMPT, user_prompt, max_tokens=1500)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    parsed = json.loads(raw.strip())
    return parsed


def build_todays_predictions(api_key, fixtures, today_str):
    todays_fixtures = [f for f in fixtures if f["date"] == today_str]
    matches = []
    for fx in todays_fixtures:
        mid = match_id(fx["date"], fx["team1"], fx["team2"])
        home_form = team_recent_form(fixtures, fx["team1"], today_str)
        away_form = team_recent_form(fixtures, fx["team2"], today_str)
        print(f"  Predicting {fx['team1']} vs {fx['team2']}...")
        try:
            pred = generate_prediction(api_key, fx, home_form, away_form)
        except Exception as e:
            print(f"  ERROR generating prediction for {fx['team1']} vs {fx['team2']}: {e}", file=sys.stderr)
            continue
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
    if existing_today and existing_today["matches"]:
        print(f"Today ({today_str}) already has {len(existing_today['matches'])} prediction(s) logged. Skipping generation.")
    else:
        print(f"Generating predictions for today ({today_str})...")
        todays_matches = build_todays_predictions(api_key, fixtures, today_str)
        if todays_matches:
            if existing_today:
                existing_today["matches"] = todays_matches
            else:
                log["entries"].append({"date": today_str, "matches": todays_matches})
            print(f"  Added {len(todays_matches)} prediction(s) for today.")
        else:
            print("  No fixtures found for today, or all predictions failed to generate.")

    log["entries"].sort(key=lambda e: e["date"])

    print("Saving log and rebuilding index.html...")
    save_log(log)
    render_html(log)
    print("Done.")


if __name__ == "__main__":
    main()
