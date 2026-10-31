"""
Nightly player on/off ratings for Hoop Numbers.

For every finished NBA game it reads ESPN's play-by-play, splits the game into lineup "stints"
(the 10 players on the floor between substitutions), then fits a regularized regression
(RAPM) of points per 100 possessions on who was on the court. Each player gets an offense
and a defense rating. Ratings for a game day only use games played BEFORE that day.

Output: ratings_<season>.json for this season and last season, e.g. ratings_2027.json for 2026-27:
    {"version": 1, "season": 2027, "updated": "...", "dates": {"2026-11-02": {"<player id>": [off, def], ...}, ...}}
The page uses the latest date <= the game date.

Needs: Python 3.9+, numpy, scipy.  Run: python update_ratings.py
Testing with already-downloaded data: python update_ratings.py --offline path/to/hoop_data
"""
import argparse, datetime as dt, gzip, json, math, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import lsqr

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HEADERS = {"User-Agent": "Mozilla/5.0 (hoop-numbers ratings)", "Accept": "application/json"}
ET = ZoneInfo("America/New_York")
VERSION = 1
LAMBDA = 3000          # ridge strength, in possessions
PRIOR_KEEP = 0.5       # how much of last season's rating carries into the new season
LEAGUE_RTG = 114.5
DATA = "data"


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def season_of(d):
    return d.year + 1 if d.month >= 9 else d.year


def months_of(season, today):
    y, m = season - 1, 10
    while (y, m) <= (season, 6) and dt.date(y, m, 1) <= today:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


# ---------------- schedule: finished games with their (Eastern) date ----------------
def month_games(y, m, today, offline):
    if offline:
        p = os.path.join(offline, "scoreboards", f"{y}-{m:02d}.json")
        events = json.load(open(p))["events"] if os.path.exists(p) else []
    else:
        cache = os.path.join(DATA, "scoreboards", f"{y}-{m:02d}.json.gz")
        last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
        finished_month = dt.date(y, m, last) < today - dt.timedelta(days=3)
        if finished_month and os.path.exists(cache):
            return json.load(gzip.open(cache, "rt"))
        j = get(f"{SITE}/scoreboard?dates={y}{m:02d}01-{y}{m:02d}{last:02d}&limit=500")
        events = (j or {}).get("events") or []
        if not events:  # ESPN often refuses month ranges: ask day by day
            days = [f"{y}{m:02d}{d:02d}" for d in range(1, last + 1) if dt.date(y, m, d) <= today]
            with ThreadPoolExecutor(8) as ex:
                for res in ex.map(lambda d: get(f"{SITE}/scoreboard?dates={d}&limit=100"), days):
                    events += (res or {}).get("events") or []
    games, seen = [], set()
    for e in events:
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        st = ((e.get("status") or {}).get("type") or {})
        if not st.get("completed") or ((e.get("season") or {}).get("type") == 1):
            continue
        date = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET).date().isoformat()
        games.append({"id": str(e["id"]), "date": date})
    if not offline:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if finished_month:
            json.dump(games, gzip.open(cache, "wt"))
    return games


# ---------------- play-by-play -> lineup stints ----------------
def parse_stints(j):
    c = j["header"]["competitions"][0]
    if not ((c.get("status") or {}).get("type") or {}).get("completed"):
        return None
    teams = {x["homeAway"]: str(x["id"]) for x in c["competitors"]}
    H, A = teams["home"], teams["away"]
    starters, played = {H: set(), A: set()}, {H: set(), A: set()}
    for g in (j.get("boxscore") or {}).get("players") or []:
        tid = str(g["team"]["id"])
        for a in (g.get("statistics") or [{}])[0].get("athletes") or []:
            if not (a.get("athlete") or {}).get("id"):
                continue
            pid = str(a["athlete"]["id"])
            if a.get("starter"):
                starters[tid].add(pid)
            if not a.get("didNotPlay") and a.get("stats"):
                played[tid].add(pid)
    by_period = {}
    for p in j.get("plays") or []:
        by_period.setdefault((p.get("period") or {}).get("number", 1), []).append(p)
    stints = []
    for per in sorted(by_period):
        P = by_period[per]
        if per == 1:
            on = {t: set(starters[t]) for t in (H, A)}
        else:  # who was on at the start of the quarter: players seen before they were subbed in
            on, entered = {H: set(), A: set()}, set()
            for p in P:
                tid = str((p.get("team") or {}).get("id", ""))
                ids = [x["athlete"]["id"] for x in p.get("participants") or [] if (x.get("athlete") or {}).get("id")]
                if (p.get("type") or {}).get("text") == "Substitution" and len(ids) == 2:
                    if ids[1] not in entered and tid in on:
                        on[tid].add(ids[1])
                    entered.add(ids[0])
                    continue
                for pid in ids:
                    if pid in entered:
                        continue
                    side = H if pid in played[H] else A if pid in played[A] else None
                    if side:
                        on[side].add(pid)
            for t in (H, A):
                if len(on[t]) > 5:
                    on[t] = set(sorted(on[t])[:5])
        if any(len(on[t]) != 5 for t in (H, A)):
            continue
        cur = {t: set(on[t]) for t in (H, A)}
        pts, poss = {H: 0, A: 0}, {H: 0.0, A: 0.0}

        def flush():
            if poss[H] + poss[A] > 0 and len(cur[H]) == 5 and len(cur[A]) == 5:
                stints.append([sorted(cur[H]), sorted(cur[A]), pts[H], pts[A], round(poss[H], 2), round(poss[A], 2)])

        for p in P:
            t = (p.get("type") or {}).get("text", "")
            tid = str((p.get("team") or {}).get("id", ""))
            ids = [x["athlete"]["id"] for x in p.get("participants") or [] if (x.get("athlete") or {}).get("id")]
            if t == "Substitution" and len(ids) == 2 and tid in cur:
                flush()
                pts, poss = {H: 0, A: 0}, {H: 0.0, A: 0.0}
                cur[tid].discard(ids[1]); cur[tid].add(ids[0])
                continue
            if tid not in cur:
                continue
            if p.get("scoringPlay"):
                pts[tid] += int(p.get("scoreValue") or 0)
            txt = p.get("text", "")
            if p.get("shootingPlay") and "free throw" not in txt.lower():
                poss[tid] += 1
            elif "free throw" in t.lower() and re.search(r"1 of (2|3)", t):
                poss[tid] += 0.44
            elif "Turnover" in t or "turnover" in txt:
                poss[tid] += 1
            elif t == "Offensive Rebound":
                poss[tid] -= 1
        flush()
    return {"H": H, "A": A, "stints": stints}


def load_stints(season, games, offline):
    # one game per line, so each night only adds lines (keeps the GitHub history small)
    path = os.path.join(DATA, f"stints_{season}.jsonl")
    store = {}
    if os.path.exists(path):
        for line in open(path):
            if line.strip():
                g = json.loads(line); store[g.pop("id")] = g
    todo = [g for g in games if g["id"] not in store]
    if todo:
        print(f"  {season}: reading {len(todo)} new games", flush=True)

        def one(g):
            if offline:
                f = os.path.join(offline, "summaries", g["id"] + ".json")
                return g, (json.load(open(f)) if os.path.exists(f) else None)
            return g, get(f"{SITE}/summary?event={g['id']}")

        with ThreadPoolExecutor(1 if offline else 8) as ex:
            for g, j in ex.map(one, todo):
                if not j:
                    continue
                try:
                    s = parse_stints(j)
                except Exception:
                    s = None
                if s:
                    store[g["id"]] = {"date": g["date"], **s}
        os.makedirs(DATA, exist_ok=True)
        with open(path, "w") as f:
            for gid in sorted(store, key=lambda k: (store[k]["date"], k)):
                f.write(json.dumps({"id": gid, **store[gid]}, separators=(",", ":")) + "\n")
    return store


# ---------------- ratings ----------------
def fit(stints, idx, prior_vec, lam=LAMBDA):
    rows, cols, vals, y, w = [], [], [], [], []
    r, P = 0, len(idx)
    for hl, al, ph, pa, qh, qa in stints:
        for off, de, p_, q in ((hl, al, ph, qh), (al, hl, pa, qa)):
            if q < 1:
                continue
            for pid in off:
                rows.append(r); cols.append(idx[pid]); vals.append(1.0)
            for pid in de:
                rows.append(r); cols.append(P + idx[pid]); vals.append(1.0)
            y.append(100 * p_ / q - LEAGUE_RTG); w.append(q); r += 1
    if r == 0:
        return prior_vec.copy()
    X = sp.csr_matrix((vals, (rows, cols)), shape=(r, 2 * P))
    sw = np.sqrt(np.array(w))
    target = sw * (np.array(y) - X @ prior_vec)       # fit how far each player is from his prior
    return prior_vec + lsqr(sp.diags(sw) @ X, target, damp=math.sqrt(lam))[0]


def season_ratings(store, prior, with_snaps):
    games = sorted(store.values(), key=lambda g: g["date"])
    players = sorted({p for g in games for s in g["stints"] for p in s[0] + s[1]} | set(prior))
    idx = {p: i for i, p in enumerate(players)}
    P = len(players)
    pv = np.zeros(2 * P)
    for p, (o, d) in prior.items():
        if p in idx:
            pv[idx[p]], pv[P + idx[p]] = o * PRIOR_KEEP, d * PRIOR_KEEP
    snaps = {}
    dates = sorted({g["date"] for g in games})
    if with_snaps and dates:
        after_last = (dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=1)).isoformat()
        for d in dates + [after_last]:
            prev = [g for g in games if g["date"] < d]
            before = [s for g in prev for s in g["stints"]]
            coef = fit(before, idx, pv) if len(prev) > 30 or prior else pv.copy()
            snaps[d] = {p: [round(float(coef[i]), 3), round(float(coef[P + i]), 3)] for p, i in idx.items()
                        if abs(coef[i]) + abs(coef[P + i]) > 1e-4}
    final = fit([s for g in games for s in g["stints"]], idx, pv) if games else pv
    return snaps, {p: [float(final[i]), float(final[P + i])] for p, i in idx.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", help="folder from download_nba_data.py (testing)")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(ET).date()
    cur = season_of(today)
    seasons = [cur - 2, cur - 1, cur]          # the oldest one is only a starting point for the next
    prior = {}
    for s in seasons:
        games = [g for y, m in months_of(s, today) for g in month_games(y, m, today, a.offline)]
        print(f"{s - 1}-{str(s)[2:]}: {len(games)} finished games", flush=True)
        store = load_stints(s, games, a.offline)
        out = s >= cur - 1
        snaps, final = season_ratings(store, prior, with_snaps=out)
        if out and snaps:
            path = os.path.join(a.out, f"ratings_{s}.json")
            head = {"version": VERSION, "season": s, "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "games": len(store)}
            with open(path, "w") as f:  # one day per line, for small nightly diffs
                f.write(json.dumps(head, separators=(",", ":"))[:-1] + ',"dates":{\n')
                f.write(",\n".join(json.dumps(d) + ":" + json.dumps(snaps[d], separators=(",", ":")) for d in sorted(snaps)))
                f.write("\n}}\n")
            print(f"  wrote {path}: {len(snaps)} days, {os.path.getsize(path) / 1e6:.1f} MB", flush=True)
        prior = final if store else prior
    return 0


if __name__ == "__main__":
    sys.exit(main())
