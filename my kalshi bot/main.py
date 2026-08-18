import os
import re
import sys
import json
import time
import uuid
import signal
import calendar
import logging
import unicodedata
import requests
from typing import Dict, Any, List, Optional, Tuple
from kalshi_python.api_client import KalshiAuth

# All state lives next to this script, not in the current working directory,
# so running the bot from another folder cannot orphan its position file.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =====================================================================
# LOGGING SETUP (file + console, so a live run is actually observable)
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, 'kalshi_execution.log')),
        logging.StreamHandler(sys.stdout),
    ],
)

# =====================================================================
# SYSTEM & RISK CONFIGURATION
# =====================================================================
# KALSHI_ENV=prod trades real money. Default is the demo exchange.
KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo").lower()
# DRY_RUN=1 evaluates and logs every decision but never sends an order.
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# Demo and production are separate Kalshi accounts with separate API keys.
# The right credential set is picked by KALSHI_ENV; explicit env vars still win.
_DEMO_KEY_ID = "a2f667f6-7a9a-48f1-b138-ccff1d3e177f"
_DEMO_KEY_PATH = os.path.join(BASE_DIR, "private_key.pem")
_PROD_KEY_ID = "7fa83fcd-4cfb-445f-a33f-d37e79d54b90"
_PROD_KEY_PATH = os.path.join(BASE_DIR, "private_key_prod.pem")

# .strip() everywhere here: a trailing newline from copy-pasting a secret
# into GitHub's UI (or a shell) is invisible but breaks the KEY_ID the
# moment it's used as an HTTP header value ("Invalid ... return character(s)
# in header value"), and silently mismatches the pinned key path otherwise.
KALSHI_KEY_ID = os.environ.get(
    "KALSHI_KEY_ID", _PROD_KEY_ID if KALSHI_ENV == "prod" else _DEMO_KEY_ID
).strip()
KALSHI_KEY_PATH = os.environ.get(
    "KALSHI_KEY_PATH", _PROD_KEY_PATH if KALSHI_ENV == "prod" else _DEMO_KEY_PATH
).strip()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "d71d5fe01a2b95e83abf43c1137ac323").strip()

MAX_RISK_PER_TRADE = 0.02          # Hard cap: 2% risk per trade
MAX_TOTAL_EXPOSURE = 0.20          # Hard cap: 20% max portfolio exposure
MIN_EV_THRESHOLD = 0.005           # Minimum +0.5% EV. Lowered from 4% purely to
                                    # accumulate paper-trade data faster in DRY_RUN —
                                    # a 4% edge basically never appears (see EV history
                                    # in the log), so at 4% the paper ledger would stay
                                    # empty for weeks. This threshold is noisier and
                                    # should NOT be used with DRY_RUN off.
# preflight() refuses to start live (non-DRY_RUN) trading below this floor —
# the comment above was a paper-only intent, not an enforced one, so a
# forgotten env var could otherwise put real money on 0.5%-edge noise.
LIVE_MIN_EV_FLOOR = 0.02
HARD_STOP_LOSS_PROB = 0.32         # Absolute floor: liquidate if win prob < 32%
STOP_LOSS_EDGE_DROP = 0.15         # Relative floor: liquidate if prob falls 15pp below entry prob
MAX_SLIPPAGE_PCT = 0.30            # Slippage floor: reject bids below 70% of fair value
MAX_SPREAD_CENTS = 8               # Reject markets if Bid-Ask spread > 8c
MAX_ODDS_AGE_SECONDS = 900         # Reject odds data older than 15 minutes
PRE_MATCH_LOCKOUT_SECONDS = 300    # No new entries within 5 mins of kickoff
MAX_MARKET_PAGES = 20              # Pagination safety cap
# Demo and prod are separate accounts with separate positions. A shared
# state file lets one environment's reconciliation purge the other's
# legitimate positions as "settled" the moment it sees them missing.
POSITIONS_FILE = os.path.join(BASE_DIR, f"active_positions_{KALSHI_ENV}.json")
FALLBACK_BANKROLL = 500.0          # Used only if live balance fetch fails
MAX_ORDER_AGE_SECONDS = 900        # Cancel a resting entry order after 15 minutes
# DRY_RUN never places an order, so it never learns whether an entry it
# would have taken was actually right. This is a separate paper-trading
# ledger: every "[DRY RUN] Would buy" gets recorded here, then followed
# up once its market settles, so a real win-rate/P&L can accumulate
# without ever risking money.
PAPER_LEDGER_FILE = os.path.join(BASE_DIR, f"paper_trades_{KALSHI_ENV}.json")

API_PATH_PREFIX = "/trade-api/v2"
API_BASE = (
    "https://api.elections.kalshi.com" + API_PATH_PREFIX
    if KALSHI_ENV == "prod"
    else "https://demo-api.kalshi.co" + API_PATH_PREFIX
)
# The odds API free tier allows ~500 requests/month, and each league costs one
# request per cycle. Scanning every soccer league the-odds-api offers (45+)
# would burn the monthly quota in hours, so this is a deliberately curated
# set of the highest-profile leagues — the ones most likely to have both
# a liquid Kalshi match-winner market AND a Pinnacle line to compare it to.
# odds-api sport key -> Kalshi match-winner series ticker.
LEAGUE_MAP: Dict[str, str] = {
    "soccer_epl": "KXEPLGAME",
    "soccer_spain_la_liga": "KXLALIGAGAME",
    "soccer_italy_serie_a": "KXSERIEAGAME",
    "soccer_germany_bundesliga": "KXBUNDESLIGAGAME",
    "soccer_france_ligue_one": "KXLIGUE1GAME",
    "soccer_uefa_champs_league": "KXUCLGAME",
    "soccer_uefa_europa_league": "KXUELGAME",
    "soccer_usa_mls": "KXMLSGAME",
    "soccer_netherlands_eredivisie": "KXEREDIVISIEGAME",
    "soccer_portugal_primeira_liga": "KXLIGAPORTUGALGAME",
    "soccer_spl": "KXSCOTTISHPREMGAME",
    "soccer_brazil_campeonato": "KXBRASILEIROGAME",
}
SERIES_TO_SPORT: Dict[str, str] = {v: k for k, v in LEAGUE_MAP.items()}
# 12 leagues x 1 credit/cycle. At 500 credits/month this is the slowest
# cadence that still finishes every league before quota resets are likely,
# with headroom for preflight checks and manual runs. Override via env for
# faster iteration (e.g. CYCLE_SLEEP_SECONDS=1800 for a single-league test).
CYCLE_SLEEP_SECONDS = int(os.environ.get("CYCLE_SLEEP_SECONDS", str(6 * 3600)))
MIN_ODDS_QUOTA = 10                # Stop pulling more leagues once remaining quota drops below this

# =====================================================================
# GENERIC CROSS-LEAGUE TEAM MATCHING
# =====================================================================
# Odds-api and Kalshi routinely name the same club differently across the
# 12 leagues above (12 x hand-built alias tables is not maintainable), so
# matching is done structurally instead: normalize to a token set and
# require one side's tokens to be a SUBSET of the other's. Subset, not
# "any shared word" — a plain-intersection check false-matches "Real Madrid"
# with "Real Sociedad" and "Inter Milan" with "AC Milan" on their shared
# city/prefix word. Short entity prefixes (AC, RC, AS...) are deliberately
# NOT stripped as noise, because for these city-sharing pairs they're
# exactly the token that keeps the two clubs from looking identical.
# A short list of genuinely unrelated abbreviations (PSG has zero letters
# in common with "Paris Saint Germain") still needs an explicit alias.
_CLUB_NOISE_WORDS = {"fc", "cf", "afc", "club", "clube", "futebol", "futbol", "calcio"}
# Applied per-token, not per-phrase: a generic abbreviation pattern ("St."
# vs "Saint") that recurs across many club names, unlike the phrase-level
# aliases below which are each specific to one club.
_TOKEN_SYNONYMS = {"st": "saint"}
_KNOWN_CLUB_ALIASES = {
    "psg": "paris saint germain",
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "inter": "inter milan",
    "atleti": "atletico madrid",
    "barca": "barcelona",
    "ga eagles": "go ahead eagles",
    "new york rb": "new york red bulls",
    "los angeles g": "la galaxy",
}

def _normalize_tokens(raw_name: str) -> set:
    name = unicodedata.normalize("NFKD", raw_name or "").encode("ascii", "ignore").decode("ascii")
    name = name.lower().strip()
    name = _KNOWN_CLUB_ALIASES.get(name, name)
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    tokens = set()
    for t in name.split():
        if t in _CLUB_NOISE_WORDS or len(t) < 2:
            continue
        tokens.add(_TOKEN_SYNONYMS.get(t, t))
    return tokens

def _teams_match(name_a: str, name_b: str) -> bool:
    """True if two team-name strings plausibly refer to the same club."""
    tokens_a, tokens_b = _normalize_tokens(name_a), _normalize_tokens(name_b)
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b or tokens_a <= tokens_b or tokens_b <= tokens_a:
        return True
    # One bare-city-name side (e.g. "Rennes") vs a regional/inflected variant
    # ("Stade Rennais") that shares no exact token: compare word stems.
    # Gated to single-token sides only, so a shared connector word between
    # two *different* multi-word clubs (the Real Madrid / Inter Milan cases
    # above) can never trigger it.
    if len(tokens_a) == 1 or len(tokens_b) == 1:
        smaller, larger = (tokens_a, tokens_b) if len(tokens_a) == 1 else (tokens_b, tokens_a)
        (tok,) = tuple(smaller)
        if len(tok) >= 4:
            for t in larger:
                if len(t) >= 4 and t[:4] == tok[:4]:
                    return True
    return False

# The generated SDK models silently drop fields this bot depends on
# (yes_sub_title, orderbook_fp, balance_dollars), so all calls go over raw REST.
_signer = KalshiAuth(KALSHI_KEY_ID, KALSHI_KEY_PATH)

def kalshi_get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Signed GET against the Kalshi REST API, returning the raw JSON body."""
    headers = _signer.create_auth_headers("GET", API_PATH_PREFIX + endpoint)
    resp = requests.get(API_BASE + endpoint, headers=headers, params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def kalshi_post(endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Signed POST against the Kalshi REST API, returning the raw JSON body."""
    headers = _signer.create_auth_headers("POST", API_PATH_PREFIX + endpoint)
    headers["Content-Type"] = "application/json"
    resp = requests.post(API_BASE + endpoint, headers=headers, json=body, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()

# =====================================================================
# STATE & PERSISTENCE LAYER
# =====================================================================

def load_positions() -> Dict[str, Any]:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.critical(f"State corruption detected in {POSITIONS_FILE}: {e}")
    return {}

def save_positions(positions: Dict[str, Any]) -> None:
    try:
        with open(POSITIONS_FILE + ".tmp", "w") as f:
            json.dump(positions, f, indent=4)
        os.replace(POSITIONS_FILE + ".tmp", POSITIONS_FILE)
    except Exception as e:
        logging.error(f"Failed atomic disk save: {e}")

active_positions = load_positions()

# =====================================================================
# PAPER TRADING LEDGER (DRY_RUN outcome tracking)
# =====================================================================

def load_paper_ledger() -> Dict[str, Any]:
    if os.path.exists(PAPER_LEDGER_FILE):
        try:
            with open(PAPER_LEDGER_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.critical(f"State corruption detected in {PAPER_LEDGER_FILE}: {e}")
    return {"open": {}, "closed": [], "wins": 0, "losses": 0, "total_pnl": 0.0}

def save_paper_ledger(ledger: Dict[str, Any]) -> None:
    try:
        with open(PAPER_LEDGER_FILE + ".tmp", "w") as f:
            json.dump(ledger, f, indent=4)
        os.replace(PAPER_LEDGER_FILE + ".tmp", PAPER_LEDGER_FILE)
    except Exception as e:
        logging.error(f"Failed atomic disk save: {e}")

paper_ledger = load_paper_ledger()

def record_paper_entry(
    ticker: str, team: str, opponent: str, sport_key: str,
    price_cents: int, net_cost: float, true_prob: float, contracts: int,
) -> None:
    """Log a DRY_RUN entry that would have been taken, for later settlement."""
    paper_ledger["open"][ticker] = {
        "team": team,
        "opponent": opponent,
        "league": sport_key,
        "price_cents": price_cents,
        "net_cost": net_cost,
        "true_prob": true_prob,
        "contracts": contracts,
        "opened_ts": time.time(),
    }
    save_paper_ledger(paper_ledger)

def resolve_paper_trades() -> None:
    """
    Check every open paper trade's market for settlement. A market that has
    settled always buys YES in this bot's model, so result=="yes" is a win
    and result=="no" is a total loss of the paper stake — never a partial.
    """
    if not paper_ledger["open"]:
        return

    for ticker in list(paper_ledger["open"].keys()):
        entry = paper_ledger["open"][ticker]
        try:
            market = kalshi_get(f"/markets/{ticker}").get("market", {})
        except Exception as e:
            logging.warning(f"Paper trade settlement check failed for {ticker}: {e}")
            continue

        status = market.get("status")
        result = market.get("result")
        if status not in ("finalized", "settled") or result not in ("yes", "no"):
            continue

        contracts = entry["contracts"]
        net_cost = entry["net_cost"]
        won = result == "yes"
        pnl = contracts * (1.0 - net_cost) if won else -contracts * net_cost

        entry["result"] = result
        entry["won"] = won
        entry["pnl"] = pnl
        entry["closed_ts"] = time.time()
        paper_ledger["closed"].append(entry)
        paper_ledger["wins"] += int(won)
        paper_ledger["losses"] += int(not won)
        paper_ledger["total_pnl"] += pnl
        del paper_ledger["open"][ticker]

        logging.info(
            f"PAPER SETTLED: {entry['team']} vs {entry['opponent']} ({ticker}) — "
            f"{'WIN' if won else 'LOSS'} | P&L: ${pnl:+.2f}"
        )

    save_paper_ledger(paper_ledger)

    total_closed = paper_ledger["wins"] + paper_ledger["losses"]
    if total_closed:
        win_rate = 100.0 * paper_ledger["wins"] / total_closed
        logging.info(
            f"PAPER LEDGER: {len(paper_ledger['open'])} open, {total_closed} settled | "
            f"{paper_ledger['wins']}W-{paper_ledger['losses']}L ({win_rate:.1f}%) | "
            f"Total hypothetical P&L: ${paper_ledger['total_pnl']:+.2f}"
        )
    elif paper_ledger["open"]:
        logging.info(f"PAPER LEDGER: {len(paper_ledger['open'])} open, none settled yet")

# =====================================================================
# ACCOUNT & DYNAMIC EXPOSURE CALCULATIONS
# =====================================================================

def get_live_bankroll() -> float:
    try:
        data = kalshi_get("/portfolio/balance")
        if "balance_dollars" in data:
            return float(data["balance_dollars"])
        if "balance" in data:
            return float(data["balance"]) / 100.0
        raise ValueError("No balance field in response")
    except Exception as e:
        logging.error(f"Failed to fetch live balance, using fallback bankroll: {e}")
        return FALLBACK_BANKROLL

def current_total_exposure() -> float:
    """
    Calculates total exposure by summing filled positions from disk state
    AND querying the exchange dynamically for resting/unfilled orders.
    Filters out already-tracked orders to prevent double-counting.

    In DRY_RUN, there are no real positions or resting orders, so exposure
    is computed against the paper ledger's open trades instead — otherwise
    every paper entry gets sized as if it were the only position ever
    taken, and MAX_TOTAL_EXPOSURE never actually gates anything.
    """
    if DRY_RUN:
        return sum(
            entry["contracts"] * entry["net_cost"]
            for entry in paper_ledger["open"].values()
        )

    # Positions adopted from the exchange have no local buy_price; they carry a
    # cost_basis taken from the exchange instead. Never assume either exists.
    total = 0.0
    for pos in active_positions.values():
        count = pos.get("count", 0) or 0
        if not count:
            continue
        buy_price = pos.get("buy_price")
        if buy_price is not None:
            total += (buy_price / 100.0) * count
        else:
            total += float(pos.get("cost_basis") or 0.0)

    # Collect all pending order IDs already tracked in active_positions
    tracked_order_ids = {
        pos.get("pending_order_id") for pos in active_positions.values()
        if pos.get("pending_order_id")
    }

    try:
        data = kalshi_get("/portfolio/orders", {"status": "resting", "limit": 200})
        for order in data.get("orders", []) or []:
            if order.get("client_order_id") in tracked_order_ids:
                continue
            if order.get("order_id") in tracked_order_ids:
                continue
            remaining = order.get("remaining_count") or 0
            price_cents = order.get("yes_price", 0)
            if remaining > 0 and price_cents > 0:
                total += (price_cents / 100.0) * remaining
    except Exception as e:
        logging.warning(f"Failed to fetch resting orders for exposure check: {e}")

    return total

# =====================================================================
# MATHEMATICAL & VALIDATION ENGINE
# =====================================================================

def get_devigged_probs(outcomes: List[Dict[str, Any]]) -> Dict[str, float]:
    raw = {}
    for o in outcomes:
        price = o.get('price', 0)
        name = o.get('name')
        if not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        raw[name] = 1.0 / price
    total_margin = sum(raw.values())
    if total_margin <= 0:
        return {}
    return {name: val / total_margin for name, val in raw.items()}

def calculate_kelly_size(true_prob: float, fee_adjusted_cost: float, bankroll: float) -> int:
    c = fee_adjusted_cost
    if c >= 1.0 or c <= 0.0 or true_prob <= c:
        return 0

    b = (1.0 - c) / c
    q = 1.0 - true_prob
    f_kelly = ((b * true_prob) - q) / b

    if f_kelly <= 0.0:
        return 0

    adj_fraction = min(f_kelly * 0.25, MAX_RISK_PER_TRADE)
    max_capital = bankroll * adj_fraction
    contracts = int(max_capital / c)
    return max(0, contracts)

def calculate_kalshi_fee(price_cents: int) -> float:
    """
    Kalshi quadratic trading fee per contract, in dollars: 0.07 * P * (1 - P).
    """
    p = price_cents / 100.0
    return 0.07 * p * (1.0 - p)

def _fp(n: float) -> str:
    """Format a contract count as a Kalshi fixed-point string."""
    return f"{n:.2f}"

def _cents_to_dollars(cents: int) -> str:
    return f"{cents / 100.0:.2f}"

def _fp_to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0

def place_order(ticker: str, book_side: str, count: int, price_cents: int, client_id: str) -> Dict[str, Any]:
    """
    Create-order V2. book_side: 'bid' (buy YES) or 'ask' (sell YES) — see
    https://docs.kalshi.com/getting_started/order_direction. The legacy
    /portfolio/orders POST (action/side/yes_price, integer count) returns
    HTTP 410 as of this API version; only /portfolio/events/orders accepts
    new orders now.
    """
    return kalshi_post("/portfolio/events/orders", {
        "ticker": ticker,
        "client_order_id": client_id,
        "side": book_side,
        "count": _fp(count),
        "price": _cents_to_dollars(price_cents),
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    })

def get_order_status(order_id: str) -> Tuple[Optional[str], int]:
    """
    Return (status, cumulative_fill_count). status is one of the current
    OrderStatus values: resting, canceled, executed.
    """
    try:
        order = kalshi_get(f"/portfolio/orders/{order_id}").get("order")
        if not order:
            return None, 0
        return order.get("status"), _fp_to_int(order.get("fill_count_fp", 0))
    except Exception as e:
        logging.error(f"API communication failure checking {order_id}: {e}")
        return None, 0

def new_client_order_id() -> str:
    return f"ord_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

# =====================================================================
# EXCHANGE INTERFACE & STRICT MARKET MATCHING
# =====================================================================

def fetch_league_markets(series_ticker: str) -> List[Dict[str, Any]]:
    """
    Fetch open markets for one league's match-winner series.
    Scanning every open market is useless here: the exchange lists thousands
    and any one series' contracts never appear inside a sane page budget.
    """
    all_markets: List[Dict[str, Any]] = []
    cursor = None
    seen_cursors: set = set()

    for _ in range(MAX_MARKET_PAGES):
        try:
            params: Dict[str, Any] = {
                "limit": 100,
                "status": "open",
                "series_ticker": series_ticker,
            }
            if cursor:
                params["cursor"] = cursor
            data = kalshi_get("/markets", params)
            all_markets.extend(data.get("markets", []) or [])

            cursor = data.get("cursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        except Exception as e:
            logging.error(f"Pagination break while fetching {series_ticker} markets: {e}")
            break

    return all_markets

def fetch_all_league_markets() -> Dict[str, List[Dict[str, Any]]]:
    """Fetch open match-winner markets for every configured league."""
    markets_by_league: Dict[str, List[Dict[str, Any]]] = {}
    for sport_key, series_ticker in LEAGUE_MAP.items():
        markets = fetch_league_markets(series_ticker)
        markets_by_league[sport_key] = markets
        logging.info(f"Fetched {len(markets)} open {series_ticker} markets ({sport_key})")
    return markets_by_league

def find_matching_ticker(
    team_name: str,
    opponent_name: str,
    kalshi_markets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Locate the YES-side match-winner contract for team_name in its fixture
    against opponent_name. Both teams must independently match so a club
    playing several fixtures in the same window cannot be matched to the
    wrong one.

    The opponent is confirmed against a SIBLING market's yes_sub_title
    (same event_ticker), not the title string: Kalshi's title text uses its
    own short display name for both clubs ("Arsenal vs Coventry Winner?"),
    which can be a strict subset mismatch against a fuller odds-api name
    like "Coventry City" (extra "City" token breaks a title-string subset
    check even though the club is right) — comparing against the sibling
    market's own yes_sub_title uses the same clean single-token comparison
    that already works for the primary team, instead of noisy title prose.
    """
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for m in kalshi_markets:
        by_event.setdefault(m.get("event_ticker"), []).append(m)

    matches = []

    for m in kalshi_markets:
        yes_sub = m.get("yes_sub_title") or ""

        if not yes_sub or yes_sub.strip().lower() == "tie":
            continue
        if not _teams_match(team_name, yes_sub):
            continue

        siblings = by_event.get(m.get("event_ticker"), [])
        opponent_confirmed = any(
            _teams_match(opponent_name, s.get("yes_sub_title") or "")
            for s in siblings
            if s is not m and (s.get("yes_sub_title") or "").strip().lower() != "tie"
        )
        if not opponent_confirmed:
            continue

        matches.append(m)

    return matches

def _price_to_cents(value: Any) -> Optional[int]:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None

def parse_orderbook(ob_response: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Parse a Kalshi orderbook into (best_yes_ask, best_yes_bid) in cents.
    The API returns orderbook_fp with 'yes_dollars'/'no_dollars' as
    [price, size] string pairs, each side listing resting *bids*.
    Best YES bid  = highest yes-side price.
    Best YES ask  = 100 - highest no-side price.
    """
    if not isinstance(ob_response, dict):
        return None, None

    book = ob_response.get("orderbook_fp") or ob_response.get("orderbook") or {}
    yes_side = book.get("yes_dollars") or book.get("yes") or []
    no_side = book.get("no_dollars") or book.get("no") or []

    best_bid = None
    if yes_side:
        prices = [_price_to_cents(lvl[0]) for lvl in yes_side if _price_to_cents(lvl[0])]
        if prices:
            best_bid = max(prices)

    best_ask = None
    if no_side:
        prices = [_price_to_cents(lvl[0]) for lvl in no_side if _price_to_cents(lvl[0])]
        if prices:
            best_ask = 100 - max(prices)

    return best_ask, best_bid

def fetch_exchange_positions() -> Dict[str, Dict[str, Any]]:
    """
    Return {ticker: {"count": int, "cost_basis": float}} for every open
    position the exchange reports.
    """
    result: Dict[str, Dict[str, Any]] = {}
    try:
        data = kalshi_get("/portfolio/positions", {"limit": 200})
        for mp in data.get("market_positions", []) or []:
            ticker = mp.get("ticker")
            try:
                qty = int(float(mp.get("position_fp", mp.get("position", 0))))
            except (TypeError, ValueError):
                qty = 0
            try:
                basis = float(mp.get("market_exposure_dollars") or 0.0)
            except (TypeError, ValueError):
                basis = 0.0
            if ticker and qty != 0:
                result[ticker] = {"count": qty, "cost_basis": basis}
    except Exception as e:
        logging.error(f"Failed to fetch exchange positions: {e}")
    return result


def reconcile_with_exchange() -> None:
    """
    Make local state agree with the exchange. The exchange is the source of
    truth: a crash between placing an order and saving state would otherwise
    leave a real position that no stop-loss ever looks at.
    """
    exchange = fetch_exchange_positions()
    if not exchange and not active_positions:
        return

    for ticker, info in exchange.items():
        qty = info["count"]
        pos = active_positions.get(ticker)
        if pos is None:
            logging.warning(
                f"ORPHAN ADOPTED: {ticker} holds {qty} contracts (${info['cost_basis']:.2f}) "
                f"on the exchange but was missing from local state. Now tracked."
            )
            active_positions[ticker] = {
                "buy_price": None,
                "cost_basis": info["cost_basis"],
                "count": qty,
                "team": team_from_ticker(ticker),
                "league": league_from_ticker(ticker),
                "entry_true_prob": None,
                "pending_order_id": None,
                "pending_side": None,
            }
        else:
            pos.setdefault("cost_basis", info["cost_basis"])
            if pos.get("count", 0) != qty:
                logging.warning(
                    f"COUNT DRIFT: {ticker} local={pos.get('count')} exchange={qty}. "
                    f"Trusting the exchange."
                )
                pos["count"] = qty

    # Anything we think we hold but the exchange does not: it settled or closed.
    for ticker in list(active_positions.keys()):
        if ticker in exchange:
            continue
        pos = active_positions[ticker]
        if pos.get("pending_order_id"):
            continue  # a resting order legitimately has no position yet
        logging.info(f"POSITION CLOSED/SETTLED on exchange, purging local state: {ticker}")
        del active_positions[ticker]

    save_positions(active_positions)


def league_from_ticker(ticker: str) -> Optional[str]:
    """Recover the odds-api sport key for an orphaned position from its ticker prefix."""
    for series_ticker, sport_key in SERIES_TO_SPORT.items():
        if ticker.startswith(series_ticker + "-"):
            return sport_key
    return None

def team_from_ticker(ticker: str) -> Optional[str]:
    """
    Best-effort team name for a position adopted from the exchange. There's
    no odds-api fixture context for an orphan, so this is Kalshi's own display
    name verbatim — it won't necessarily key-match sharp_probabilities, in
    which case the stop-loss engine correctly falls back to holding to
    settlement rather than trading blind.
    """
    try:
        market = kalshi_get(f"/markets/{ticker}").get("market", {})
        yes_sub = (market.get("yes_sub_title") or "").strip()
        return yes_sub or None
    except Exception:
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel-order V2. The legacy DELETE /portfolio/orders/{id} also 410s now."""
    try:
        path = f"{API_PATH_PREFIX}/portfolio/events/orders/{order_id}"
        headers = _signer.create_auth_headers("DELETE", path)
        resp = requests.delete(f"{API_BASE}/portfolio/events/orders/{order_id}", headers=headers, timeout=15)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        return True
    except Exception as e:
        logging.error(f"Failed to cancel order {order_id}: {e}")
        return False


def resolve_pending_order(ticker: str) -> None:
    pos = active_positions.get(ticker)
    if not pos or not pos.get("pending_order_id"):
        return

    status, cumulative_filled = get_order_status(pos["pending_order_id"])
    pending_side = pos.get("pending_side", "buy")

    # fill_count_fp is the ORDER's cumulative fill since it was placed, not a
    # per-check delta. The initial partial fill (from the create response) is
    # already folded into pos["count"], so only the fill count *since then*
    # should move it again — otherwise a second partial fill double-applies.
    already_applied = pos.get("_pending_filled_applied", 0)
    delta = max(0, cumulative_filled - already_applied)

    if delta:
        if pending_side == "buy":
            pos["count"] = pos.get("count", 0) + delta
            logging.info(f"Buy fill update on {ticker}: +{delta} -> {pos['count']}")
        elif pending_side == "sell":
            pos["count"] = max(0, pos.get("count", 0) - delta)
            logging.info(f"Sell fill update on {ticker}: -{delta} -> {pos['count']}")
        pos["_pending_filled_applied"] = cumulative_filled

    if status in ("executed", "canceled"):
        pos["pending_order_id"] = None
        pos.pop("pending_side", None)
        pos.pop("_order_placed_ts", None)
        pos.pop("_pending_filled_applied", None)
        if pos.get("count", 0) == 0:
            logging.info(f"Purging fully exited position for {ticker}")
            del active_positions[ticker]
    elif status == "resting":
        # A limit order left resting at a stale price keeps reserving capital
        # while the market moves away from it. Pull it and re-evaluate later.
        placed_ts = pos.get("_order_placed_ts")
        if placed_ts and (time.time() - placed_ts) > MAX_ORDER_AGE_SECONDS:
            age = int(time.time() - placed_ts)
            logging.info(f"Cancelling stale resting order on {ticker} (age {age}s)")
            if cancel_order(pos["pending_order_id"]):
                pos["pending_order_id"] = None
                pos.pop("pending_side", None)
                pos.pop("_order_placed_ts", None)
                if pos.get("count", 0) == 0:
                    del active_positions[ticker]
    elif status is None:
        # API returned error — order may not exist on exchange (ghost)
        ghost_checks = pos.get("_ghost_checks", 0) + 1
        pos["_ghost_checks"] = ghost_checks
        if ghost_checks >= 3:
            logging.warning(f"Purging ghost pending order for {ticker} after {ghost_checks} failed lookups")
            pos["pending_order_id"] = None
            pos.pop("pending_side", None)
            pos.pop("_ghost_checks", None)
            pos.pop("_pending_filled_applied", None)
            if pos.get("count", 0) == 0:
                del active_positions[ticker]

    save_positions(active_positions)

# =====================================================================
# MAIN EXECUTION ENGINE
# =====================================================================

def fetch_league_odds(sport_key: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[int]]:
    """Fetch Pinnacle odds for one league. Returns (events, requests_remaining)."""
    res = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
        params={"apiKey": ODDS_API_KEY, "regions": "eu", "bookmakers": "pinnacle"},
        timeout=10,
    )
    res.raise_for_status()
    remaining = res.headers.get("x-requests-remaining")
    return res.json(), (int(float(remaining)) if remaining is not None else None)


def run_trading_engine() -> None:
    global active_positions

    if DRY_RUN:
        # Paper trades are hypothetical, so they shouldn't be sized off
        # whatever the real account happens to hold right now (e.g. $0.99
        # after an unrelated manual trade) — that would just make every
        # Kelly size round down to 0 and the paper ledger would never
        # collect a single entry. Size against a fixed notional bankroll
        # instead, as if the account were actually funded.
        bankroll = FALLBACK_BANKROLL
    else:
        bankroll = get_live_bankroll()
    committed_this_cycle = 0.0

    # 1. Fetch Sharp Odds for every configured league, stopping early if quota runs low.
    events_by_league: Dict[str, List[Dict[str, Any]]] = {}
    for sport_key in LEAGUE_MAP:
        try:
            events, remaining = fetch_league_odds(sport_key)
            events_by_league[sport_key] = events or []
            if remaining is not None:
                if remaining < MIN_ODDS_QUOTA:
                    logging.critical(
                        f"ODDS QUOTA EXHAUSTED: {remaining} requests left after {sport_key}. "
                        f"Skipping remaining leagues this cycle."
                    )
                    break
                if remaining < MIN_ODDS_QUOTA * 5:
                    logging.warning(f"Odds quota low: {remaining} requests remaining.")
        except Exception as e:
            logging.error(f"Sharp odds API unreachable for {sport_key}: {e}")
            continue

    if not events_by_league:
        logging.error("No odds data fetched for any league this cycle.")
        return

    # 2. Synchronize Kalshi Market State, one series per league
    markets_by_league = fetch_all_league_markets()
    if not any(markets_by_league.values()):
        logging.error("No open markets found in any configured league.")
        return

    # 3. Reconcile resting/pending orders, then sync against the exchange
    for ticker in list(active_positions.keys()):
        resolve_pending_order(ticker)
    reconcile_with_exchange()
    resolve_paper_trades()

    # Keyed by (sport_key, team_name), not team_name alone: a flat team-name
    # key would silently collide if two tracked leagues ever share a club
    # name, and the stop-loss engine would then read the wrong league's
    # probability for a position without any error or warning.
    sharp_probabilities: Dict[Tuple[str, str], float] = {}

    # --- STEP 1: GLOBAL DATA INGESTION (DECOUPLED FROM ENTRY LOCKOUTS) ---
    for sport_key, events in events_by_league.items():
        for event in events:
            try:
                pinnacle_outcomes = event['bookmakers'][0]['markets'][0]['outcomes']
                devigged_probs = get_devigged_probs(pinnacle_outcomes)
                for team_name, prob in devigged_probs.items():
                    sharp_probabilities[(sport_key, team_name)] = prob
            except (IndexError, KeyError):
                continue

    # --- STEP 2: ALPHA ENTRY ENGINE ---
    for sport_key, events in events_by_league.items():
        kalshi_markets = markets_by_league.get(sport_key, [])
        if not kalshi_markets:
            continue

        for event in events:
            commence_time = event.get("commence_time")
            now_ts = time.time()

            match_ts = None
            if commence_time:
                try:
                    match_ts = calendar.timegm(time.strptime(commence_time, "%Y-%m-%dT%H:%M:%SZ"))
                except Exception:
                    continue

            # Circuit Breaker: Block entries if match is live or within lockout window
            if match_ts and (match_ts - now_ts) <= PRE_MATCH_LOCKOUT_SECONDS:
                continue

            try:
                last_update = event['bookmakers'][0].get('last_update')
                if last_update:
                    update_ts = calendar.timegm(time.strptime(last_update, "%Y-%m-%dT%H:%M:%SZ"))
                    if (now_ts - update_ts) > MAX_ODDS_AGE_SECONDS:
                        continue

                pinnacle_outcomes = event['bookmakers'][0]['markets'][0]['outcomes']
                devigged_probs = get_devigged_probs(pinnacle_outcomes)
            except (IndexError, KeyError):
                continue

            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")

            for team_name, true_prob in devigged_probs.items():
                if team_name not in (home_team, away_team):
                    continue

                search_name = team_name
                opponent = away_team if team_name == home_team else home_team

                matches = find_matching_ticker(search_name, opponent, kalshi_markets)
                if not matches:
                    logging.info(f"No Kalshi market matched {search_name} vs {opponent} ({sport_key})")
                for m in matches:
                    ticker = m.get('ticker', '')
                    if ticker in active_positions:
                        continue
                    if DRY_RUN and ticker in paper_ledger["open"]:
                        continue

                    try:
                        ob = kalshi_get(f"/markets/{ticker}/orderbook")
                        best_ask, best_bid = parse_orderbook(ob)
                    except Exception as e:
                        logging.warning(f"Orderbook read failed for {ticker}: {e}")
                        continue

                    if not best_ask or not best_bid:
                        logging.info(f"SKIP {ticker}: empty book (ask={best_ask}, bid={best_bid})")
                        continue

                    if (best_ask - best_bid) > MAX_SPREAD_CENTS:
                        logging.info(
                            f"SKIP {ticker}: spread {best_ask - best_bid}c > {MAX_SPREAD_CENTS}c"
                        )
                        continue

                    kalshi_prob = best_ask / 100.0
                    fee = calculate_kalshi_fee(best_ask)
                    net_cost = kalshi_prob + fee
                    ev = true_prob - net_cost

                    if ev < MIN_EV_THRESHOLD:
                        logging.info(
                            f"SKIP {ticker}: EV {ev*100:.2f}% < {MIN_EV_THRESHOLD*100:.2f}% "
                            f"(ask {best_ask}c, sharp {true_prob*100:.1f}%)"
                        )
                        continue

                    effective_bankroll = bankroll - committed_this_cycle
                    contracts = calculate_kelly_size(true_prob, net_cost, effective_bankroll)
                    if contracts <= 0:
                        logging.info(f"SKIP {ticker}: Kelly size resolved to 0 contracts")
                        continue

                    # Exposure gate: use actual trade cost, not fixed percentage
                    actual_trade_cost = contracts * net_cost
                    exposure = current_total_exposure()
                    if exposure + actual_trade_cost > bankroll * MAX_TOTAL_EXPOSURE:
                        logging.info(
                            f"EXPOSURE GATE: {ticker} blocked. "
                            f"Would add ${actual_trade_cost:.2f} to ${exposure:.2f} "
                            f"(cap: ${bankroll * MAX_TOTAL_EXPOSURE:.2f})"
                        )
                        continue

                    logging.info(
                        f"ALPHA ENTRY: {ticker} | Price: {best_ask}c | "
                        f"EV: {ev*100:.2f}% | Size: {contracts} | "
                        f"TrueProb: {true_prob*100:.1f}% | NetCost: {net_cost*100:.1f}c"
                    )
                    if DRY_RUN:
                        logging.info(f"[DRY RUN] Would buy {contracts} {ticker} @ {best_ask}c")
                        # The pre-loop skip above guarantees this ticker has no
                        # open paper trade yet, so this is always a genuinely
                        # new entry — commit its cost so the NEXT candidate's
                        # Kelly sizing sees a smaller effective bankroll,
                        # instead of every trade this cycle being sized as if
                        # it were the only one taken.
                        record_paper_entry(
                            ticker, search_name, opponent, sport_key,
                            best_ask, net_cost, true_prob, contracts,
                        )
                        committed_this_cycle += contracts * net_cost
                        continue

                    client_id = new_client_order_id()

                    try:
                        resp = place_order(ticker, "bid", contracts, best_ask, client_id)
                        order_id = resp.get("order_id")
                        if not order_id:
                            raise ValueError(f"No order_id in response: {resp}")
                    except Exception as e:
                        # Order was NEVER accepted by exchange — do NOT create a position entry
                        logging.critical(f"create_order call failed for {ticker} (client_id={client_id}): {e}")
                        continue

                    # The create-order response already reports the immediate fill,
                    # so there's no need to sleep and poll separately for this part.
                    filled = _fp_to_int(resp.get("fill_count", 0))
                    order_remaining = _fp_to_int(resp.get("remaining_count", 0))
                    placed_ts = time.time()
                    logging.info(
                        f"ORDER ACCEPTED: {ticker} order_id={order_id} "
                        f"filled={filled} remaining={order_remaining}"
                    )

                    if filled > 0:
                        partial = order_remaining > 0
                        active_positions[ticker] = {
                            "buy_price": best_ask,
                            "count": filled,
                            "team": search_name,
                            "league": sport_key,
                            "entry_true_prob": true_prob,
                            "pending_order_id": order_id if partial else None,
                            "pending_side": "buy" if partial else None,
                            "_order_placed_ts": placed_ts if partial else None,
                            "_pending_filled_applied": filled if partial else None,
                        }
                        committed_this_cycle += filled * net_cost
                        logging.info(f"FILLED: {ticker} {filled}/{contracts} @ {best_ask}c")
                        save_positions(active_positions)
                    elif order_remaining > 0:
                        active_positions[ticker] = {
                            "buy_price": best_ask,
                            "count": 0,
                            "team": search_name,
                            "league": sport_key,
                            "entry_true_prob": true_prob,
                            "pending_order_id": order_id,
                            "pending_side": "buy",
                            "_order_placed_ts": placed_ts,
                            "_pending_filled_applied": 0,
                        }
                        logging.info(f"RESTING: {ticker} order {order_id} awaiting fill")
                        save_positions(active_positions)
                    else:
                        logging.warning(f"ORDER DEAD: {ticker} order {order_id} — 0 filled, 0 resting.")

    # --- STEP 3: DEFENSIVE LIQUIDATION & STOP-LOSS ENGINE ---
    for ticker in list(active_positions.keys()):
        pos_data = active_positions[ticker]
        if pos_data.get("count", 0) == 0:
            continue

        # Guard: don't re-trigger stop-loss if a sell order is already pending
        if pos_data.get("pending_order_id") and pos_data.get("pending_side") == "sell":
            logging.info(f"Stop-loss sell already pending for {ticker}, skipping re-trigger")
            continue

        team_name = pos_data.get("team")
        current_prob = sharp_probabilities.get((pos_data.get("league"), team_name))

        if current_prob is None:
            # A pre-match odds feed drops fixtures once they kick off, so missing
            # data is the NORMAL end-state of every position, not an emergency.
            # Dumping into an unknown book on no information is strictly worse
            # than holding a binary contract to its automatic settlement.
            logging.info(
                f"HOLD TO SETTLEMENT: {ticker} — no pre-match odds for '{team_name}' "
                f"(match likely in play). Holding {pos_data.get('count', 0)} contracts."
            )
            continue

        entry_prob = pos_data.get("entry_true_prob")

        # The absolute floor is only meaningful relative to why we entered.
        # A flat 32% floor would instantly stop out any deliberate underdog
        # position that was bought precisely because it was cheap.
        edge_collapsed = (
            entry_prob is not None and (entry_prob - current_prob) >= STOP_LOSS_EDGE_DROP
        )
        absolute_floor_breached = (
            current_prob < HARD_STOP_LOSS_PROB
            and (entry_prob is None or entry_prob >= HARD_STOP_LOSS_PROB)
        )

        if not (absolute_floor_breached or edge_collapsed):
            continue

        reason = "ABSOLUTE FLOOR" if absolute_floor_breached else "EDGE COLLAPSE"
        logging.warning(
            f"STOP-LOSS TRIGGERED ({reason}): {ticker} | Entry Prob: "
            f"{(entry_prob or 0)*100:.1f}% | Current Sharp Prob: {current_prob*100:.1f}%"
        )

        try:
            ob = kalshi_get(f"/markets/{ticker}/orderbook")
            _, best_bid = parse_orderbook(ob)
        except Exception as e:
            logging.warning(f"Orderbook read failed during liquidation of {ticker}: {e}")
            continue

        if not best_bid:
            logging.warning(f"LIQUIDATION ABORTED: {ticker} has no bid to sell into.")
            continue

        # Percentage-based slippage floor: reject bids below 70% of fair value.
        # There is no longer a "sell at any price" path — holding a binary
        # contract to settlement is always available as the fallback.
        min_acceptable_bid = max(1, int(current_prob * 100 * (1.0 - MAX_SLIPPAGE_PCT)))

        if best_bid < min_acceptable_bid:
            logging.warning(
                f"LIQUIDATION ABORTED: {ticker} Bid ({best_bid}c) below floor ({min_acceptable_bid}c)."
            )
            continue

        cnt = pos_data["count"]

        if DRY_RUN:
            logging.info(f"[DRY RUN] Would sell {cnt} {ticker} @ {best_bid}c")
            continue

        client_id = new_client_order_id()

        try:
            resp = place_order(ticker, "ask", cnt, best_bid, client_id)
            order_id = resp.get("order_id")
            if not order_id:
                raise ValueError(f"No order_id in response: {resp}")
        except Exception as e:
            logging.critical(f"create_order (sell) failed for {ticker} (client_id={client_id}): {e}")
            # Don't record a pending order for a failed API call
            continue

        filled = _fp_to_int(resp.get("fill_count", 0))
        remaining = _fp_to_int(resp.get("remaining_count", 0))

        if filled >= cnt:
            del active_positions[ticker]
            save_positions(active_positions)
            logging.info(f"COMPLETE LIQUIDATION: {ticker} closed at {best_bid}c ({cnt} contracts).")
        elif filled > 0:
            pos_data["count"] -= filled
            pos_data["pending_order_id"] = order_id
            pos_data["pending_side"] = "sell"
            pos_data["_order_placed_ts"] = time.time()
            pos_data["_pending_filled_applied"] = filled
            save_positions(active_positions)
            logging.info(
                f"PARTIAL LIQUIDATION: {ticker} sold {filled}/{cnt}. "
                f"Remaining: {pos_data['count']} contracts."
            )
        elif remaining > 0:
            pos_data["pending_order_id"] = order_id
            pos_data["pending_side"] = "sell"
            pos_data["_order_placed_ts"] = time.time()
            pos_data["_pending_filled_applied"] = 0
            save_positions(active_positions)
            logging.info(f"SELL RESTING: {ticker} order {order_id} awaiting fill")
        else:
            logging.warning(f"LIQUIDATION UNFILLED: {ticker} sell order got 0 fills at {best_bid}c.")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    logging.warning(f"Signal {signum} received — finishing cycle, then exiting.")


def preflight() -> bool:
    """Fail fast and loudly on a misconfigured start, rather than mid-trade."""
    ok = True

    if not DRY_RUN and MIN_EV_THRESHOLD < LIVE_MIN_EV_FLOOR:
        logging.critical(
            f"MIN_EV_THRESHOLD ({MIN_EV_THRESHOLD*100:.2f}%) is below the "
            f"{LIVE_MIN_EV_FLOOR*100:.0f}% floor for live trading with DRY_RUN off. "
            f"This threshold was lowered for paper-trading data collection only — "
            f"raise it (or the floor, deliberately) before trading real money on it."
        )
        return False

    if not os.path.exists(KALSHI_KEY_PATH):
        logging.critical(f"Private key not found at {KALSHI_KEY_PATH}")
        return False

    try:
        bankroll = get_live_bankroll()
        if bankroll == FALLBACK_BANKROLL:
            logging.critical("Balance fetch failed — check API key and system clock.")
            ok = False
        else:
            logging.info(f"Authenticated. Balance: ${bankroll:.2f}")
    except Exception as e:
        logging.critical(f"Balance check failed: {e}")
        ok = False

    try:
        # /v4/sports doesn't consume quota (unlike /v4/sports/{sport}/odds),
        # so startup checks the key without spending real cycle budget.
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={"apiKey": ODDS_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining")
        logging.info(f"Odds API key OK. {remaining} requests remaining this period.")
        if remaining is not None and int(float(remaining)) < MIN_ODDS_QUOTA:
            logging.critical(f"Odds quota already below the {MIN_ODDS_QUOTA} floor — cycles will be skipped.")
            ok = False
        est_per_cycle = len(LEAGUE_MAP)
        cycles_per_day = 86400 / CYCLE_SLEEP_SECONDS
        logging.info(
            f"{len(LEAGUE_MAP)} leagues configured -> ~{est_per_cycle} odds requests/cycle, "
            f"~{est_per_cycle * cycles_per_day:.0f}/day at a {CYCLE_SLEEP_SECONDS}s cadence."
        )
    except Exception as e:
        logging.critical(f"Odds API check failed: {e}")
        ok = False

    total_markets = 0
    total_tradable = 0
    for sport_key, series_ticker in LEAGUE_MAP.items():
        markets = fetch_league_markets(series_ticker)
        tradable = 0
        for m in markets:
            try:
                ask, bid = parse_orderbook(kalshi_get(f"/markets/{m['ticker']}/orderbook"))
                if ask and bid:
                    tradable += 1
            except Exception:
                pass
        total_markets += len(markets)
        total_tradable += tradable
        logging.info(f"{series_ticker} ({sport_key}): {len(markets)} open, {tradable} with a two-sided book.")

    if total_markets == 0:
        logging.critical("No open markets found in any configured league.")
        ok = False
    elif total_tradable == 0:
        logging.warning(
            "No configured league currently has liquidity — the bot will find nothing "
            "to trade until that changes. This is an exchange condition, not a bug."
        )

    return ok


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logging.info("=" * 70)
    logging.info(
        f"Kalshi soccer bot starting | {len(LEAGUE_MAP)} leagues | env={KALSHI_ENV.upper()} "
        f"| dry_run={DRY_RUN} | base={API_BASE}"
    )
    if KALSHI_ENV == "prod" and not DRY_RUN:
        logging.warning("PRODUCTION MODE: orders placed here use REAL money.")
    logging.info("=" * 70)

    if not preflight():
        logging.critical("Preflight failed. Not starting.")
        sys.exit(1)

    logging.info("Preflight passed. Entering main loop (Ctrl-C to stop).")
    while not _shutdown:
        try:
            run_trading_engine()
        except Exception as crash:
            logging.critical(f"FATAL EXCEPTION IN LOOP: {crash}", exc_info=True)
        if _shutdown:
            break
        logging.info(f"Cycle complete. Sleeping {CYCLE_SLEEP_SECONDS}s.")
        for _ in range(CYCLE_SLEEP_SECONDS):
            if _shutdown:
                break
            time.sleep(1)

    logging.info("Shutdown complete. Open positions remain on the exchange.")