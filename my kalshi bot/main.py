import os
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import json
import time
import uuid
import calendar
import logging
import requests
from typing import Dict, Any, List, Optional, Tuple
from kalshi_python import Configuration, KalshiClient, MarketsApi, PortfolioApi
from kalshi_python.models import CreateOrderRequest

# =====================================================================
# LOGGING SETUP
# =====================================================================
logging.basicConfig(
    filename='kalshi_execution.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)

# =====================================================================
# SYSTEM & RISK CONFIGURATION
# =====================================================================
KALSHI_KEY_ID = "6f26446f-ff72-4026-9ce7-a9f78fe0a058"
KALSHI_KEY_PATH = "private_key.pem"
ODDS_API_KEY = "ffd0c19dddd4ca4535ee3b44ecbb1a2d"

MAX_RISK_PER_TRADE = 0.02          # Hard cap: 2% risk per trade
MAX_TOTAL_EXPOSURE = 0.20          # Hard cap: 20% max portfolio exposure
MIN_EV_THRESHOLD = 0.04            # Minimum +4.0% Expected Value required
HARD_STOP_LOSS_PROB = 0.32         # Absolute floor: liquidate if win prob < 32%
STOP_LOSS_EDGE_DROP = 0.15         # Relative floor: liquidate if prob falls 15pp below entry prob
MAX_SLIPPAGE_PCT = 0.30            # Slippage floor: reject bids below 70% of fair value
MAX_SPREAD_CENTS = 8               # Reject markets if Bid-Ask spread > 8c
MAX_ODDS_AGE_SECONDS = 60          # Reject odds data older than 60 seconds
PRE_MATCH_LOCKOUT_SECONDS = 300    # No new entries within 5 mins of kickoff
BLIND_CYCLES_BEFORE_FORCE_EXIT = 3 # Force-liquidate after N consecutive blind stop-loss cycles
MAX_MARKET_PAGES = 10              # Pagination safety cap
POSITIONS_FILE = "active_positions.json"
FALLBACK_BANKROLL = 500.0          # Used only if live balance fetch fails

# =====================================================================
# CANONICAL TEAM ALIAS MAP (Pinnacle Institutional Name -> Kalshi Aliases)
# =====================================================================
EPL_TEAM_MAP: Dict[str, List[str]] = {
    "Arsenal": ["arsenal"],
    "Aston Villa": ["aston villa", "villa"],
    "Bournemouth": ["bournemouth", "afc bournemouth"],
    "Brentford": ["brentford"],
    "Brighton and Hove Albion": ["brighton", "brighton & hove albion"],
    "Chelsea": ["chelsea"],
    "Crystal Palace": ["crystal palace", "palace"],
    "Everton": ["everton"],
    "Fulham": ["fulham"],
    "Ipswich Town": ["ipswich", "ipswich town"],
    "Leicester City": ["leicester", "leicester city"],
    "Liverpool": ["liverpool"],
    "Manchester City": ["manchester city", "man city", "mancity"],
    "Manchester United": ["manchester united", "man united", "man utd", "manutd"],
    "Newcastle United": ["newcastle", "newcastle united"],
    "Nottingham Forest": ["nottingham forest", "nottm forest", "forest"],
    "Southampton": ["southampton"],
    "Tottenham Hotspur": ["tottenham", "tottenham hotspur", "spurs"],
    "West Ham United": ["west ham", "west ham united"],
    "Wolverhampton Wanderers": ["wolverhampton", "wolves"]
}

# Initialize Kalshi SDK client
kalshi_config = Configuration(host="https://demo-api.kalshi.co/trade-api/v2")
kalshi_client = KalshiClient(kalshi_config)
kalshi_client.set_kalshi_auth(KALSHI_KEY_ID, KALSHI_KEY_PATH)
portfolio_api = PortfolioApi(api_client=kalshi_client)
markets_api = MarketsApi(api_client=kalshi_client)

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
# ACCOUNT & DYNAMIC EXPOSURE CALCULATIONS
# =====================================================================

def get_live_bankroll() -> float:
    try:
        balance = portfolio_api.get_balance()
        cents = getattr(balance, "balance", None)
        if cents is None:
            raise ValueError("No 'balance' field in response")
        return cents / 100.0
    except Exception as e:
        logging.error(f"Failed to fetch live balance, using fallback bankroll: {e}")
        return FALLBACK_BANKROLL

def current_total_exposure() -> float:
    """
    Calculates total exposure by summing filled positions from disk state
    AND querying the exchange dynamically for resting/unfilled orders.
    Filters out already-tracked orders to prevent double-counting.
    """
    total = sum((pos["buy_price"] / 100.0) * pos.get("count", 0) for pos in active_positions.values())

    # Collect all pending order IDs already tracked in active_positions
    tracked_order_ids = {
        pos.get("pending_order_id") for pos in active_positions.values()
        if pos.get("pending_order_id")
    }

    try:
        resp = portfolio_api.get_orders(status="resting")
        orders = getattr(resp, "orders", []) or []
        for ord_data in orders:
            order_dict = ord_data.to_dict() if hasattr(ord_data, "to_dict") else ord_data
            order_id = order_dict.get("order_id") or order_dict.get("client_order_id")
            if order_id in tracked_order_ids:
                continue
            remaining_count = order_dict.get("count", 0) - order_dict.get("filled_count", 0)
            price_cents = order_dict.get("yes_price", 0)
            if remaining_count > 0 and price_cents > 0:
                total += (price_cents / 100.0) * remaining_count
    except Exception as e:
        logging.warning(f"Failed to fetch resting orders for exposure check: {e}")

    return total

# =====================================================================
# TEAM NAME NORMALIZATION
# =====================================================================

def normalize_team_name(raw_name: str) -> Optional[str]:
    """Map Odds API team name to canonical EPL_TEAM_MAP key."""
    raw_lower = raw_name.lower().strip()
    for canonical, aliases in EPL_TEAM_MAP.items():
        if raw_lower == canonical.lower() or raw_lower in aliases:
            return canonical
    return None

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
    Kalshi fee per contract (in dollars).
    fee = max($0.02, 0.07 * min(price, 1 - price))
    """
    p = price_cents / 100.0
    return max(0.02, 0.07 * min(p, 1.0 - p))

def get_order_status(order_id: str) -> Tuple[Optional[str], int]:
    try:
        order_resp = portfolio_api.get_order(order_id)
        order = getattr(order_resp, "order", None)
        if order is None:
            return None, 0
        status = getattr(order, "status", None)
        filled_count = getattr(order, "filled_count", 0)
        return status, filled_count
    except Exception as e:
        logging.error(f"API communication failure checking {order_id}: {e}")
        return None, 0

def new_client_order_id() -> str:
    return f"ord_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

# =====================================================================
# EXCHANGE INTERFACE & STRICT MARKET MATCHING
# =====================================================================

def fetch_all_kalshi_markets() -> List[Dict[str, Any]]:
    all_markets: List[Dict[str, Any]] = []
    cursor = None
    seen_cursors: set = set()
    page_count = 0
    while page_count < MAX_MARKET_PAGES:
        page_count += 1
        try:
            params: Dict[str, Any] = {"limit": 100, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            resp = markets_api.get_markets(**params)
            markets = getattr(resp, "markets", []) or []
            for m in markets:
                all_markets.append(m.to_dict() if hasattr(m, "to_dict") else m)

            cursor = getattr(resp, "cursor", None)
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        except Exception as e:
            logging.error(f"Pagination break while fetching Kalshi markets: {e}")
            break
    if page_count >= MAX_MARKET_PAGES:
        logging.warning(f"Market pagination hit safety cap ({MAX_MARKET_PAGES} pages)")
    return all_markets

def find_matching_ticker(team_name: str, kalshi_markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Strict Match Winner filtering using explicit team aliases.
    Prevents cross-team title collisions (e.g., buying Arsenal YES when searching Chelsea).
    """
    aliases = EPL_TEAM_MAP.get(team_name, [team_name.lower()])
    valid_markets = []
    derivative_keywords = {'goals', 'score', 'half', 'over', 'under', 'spread', 'margin', 'both teams', 'corner', 'draw'}

    for m in kalshi_markets:
        title = m.get('title', '').lower()
        subtitle = m.get('subtitle', '').lower()
        ticker = m.get('ticker', '').lower()

        # Reject derivatives
        if any(kw in title for kw in derivative_keywords):
            continue

        # Check if the market is an explicit Match Winner contract
        is_winner_market = "winner" in title or "win" in title
        if not is_winner_market:
            continue

        # Match alias against subtitle or target ticker suffix first to isolate the YES team
        team_matched = False
        for alias in aliases:
            alias_clean = alias.lower()
            if alias_clean == subtitle or alias_clean in ticker:
                team_matched = True
                break
            if alias_clean in title and m.get('yes_sub_title', '').lower() == alias_clean:
                team_matched = True
                break

        if team_matched:
            valid_markets.append(m)

    return valid_markets

def parse_orderbook(ob_response: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Parse Kalshi orderbook response into (best_yes_ask, best_yes_bid).
    Kalshi returns 'yes' and 'no' arrays of [price, quantity].
    YES best ask = lowest yes-side price.
    YES best bid = 100 - lowest no-side price.
    """
    if hasattr(ob_response, "to_dict"):
        ob_response = ob_response.to_dict()

    orderbook = ob_response.get('orderbook', {}) if isinstance(ob_response, dict) else {}
    yes_side = orderbook.get('yes', [])
    no_side = orderbook.get('no', [])

    best_ask = None
    if yes_side:
        best_ask = min(yes_side, key=lambda x: x[0])[0]

    best_bid = None
    if no_side:
        best_bid = 100 - min(no_side, key=lambda x: x[0])[0]

    return best_ask, best_bid

def resolve_pending_order(ticker: str) -> None:
    pos = active_positions.get(ticker)
    if not pos or not pos.get("pending_order_id"):
        return

    status, filled = get_order_status(pos["pending_order_id"])
    pending_side = pos.get("pending_side", "buy")

    if filled:
        if pending_side == "buy":
            # Buy fills only increase count
            new_count = max(pos.get("count", 0), filled)
            if new_count != pos.get("count", 0):
                logging.info(f"Buy fill update on {ticker}: {pos.get('count', 0)} -> {new_count}")
                pos["count"] = new_count
        elif pending_side == "sell":
            # Sell fills reduce count
            sold = filled
            old_count = pos.get("count", 0)
            new_count = max(0, old_count - sold)
            if new_count != old_count:
                logging.info(f"Sell fill update on {ticker}: {old_count} -> {new_count} (sold {sold})")
                pos["count"] = new_count

    if status in ("filled", "canceled", "rejected", "expired"):
        pos["pending_order_id"] = None
        pos.pop("pending_side", None)
        if pos.get("count", 0) == 0:
            logging.info(f"Purging fully exited position for {ticker}")
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
            if pos.get("count", 0) == 0:
                del active_positions[ticker]

    save_positions(active_positions)

# =====================================================================
# MAIN EXECUTION ENGINE
# =====================================================================

def run_trading_engine() -> None:
    global active_positions

    bankroll = get_live_bankroll()
    committed_this_cycle = 0.0

    # 1. Fetch Sharp Odds via The-Odds-API (API key in params, not URL)
    try:
        res = requests.get(
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "bookmakers": "pinnacle"
            },
            timeout=10
        )
        res.raise_for_status()
        events = res.json()
    except Exception as e:
        logging.error(f"Sharp odds API unreachable: {e}")
        return

    # 2. Synchronize Kalshi Market State
    kalshi_markets = fetch_all_kalshi_markets()
    if not kalshi_markets:
        return

    # 3. Reconcile resting/pending orders
    for ticker in list(active_positions.keys()):
        resolve_pending_order(ticker)

    sharp_probabilities: Dict[str, float] = {}

    # --- STEP 1: GLOBAL DATA INGESTION (DECOUPLED FROM ENTRY LOCKOUTS) ---
    for event in events:
        try:
            pinnacle_outcomes = event['bookmakers'][0]['markets'][0]['outcomes']
            devigged_probs = get_devigged_probs(pinnacle_outcomes)
            for team_name, prob in devigged_probs.items():
                # Normalize to canonical name for consistent lookup
                canonical = normalize_team_name(team_name)
                if canonical:
                    sharp_probabilities[canonical] = prob
                sharp_probabilities[team_name] = prob
        except (IndexError, KeyError):
            continue

    # --- STEP 2: ALPHA ENTRY ENGINE ---
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

            # Normalize team name to canonical for alias matching
            canonical_name = normalize_team_name(team_name)
            search_name = canonical_name if canonical_name else team_name

            matches = find_matching_ticker(search_name, kalshi_markets)
            for m in matches:
                ticker = m.get('ticker', '')
                if ticker in active_positions:
                    continue

                try:
                    ob = markets_api.get_market_orderbook(ticker=ticker)
                    best_ask, best_bid = parse_orderbook(ob)
                except Exception as e:
                    logging.warning(f"Orderbook read failed for {ticker}: {e}")
                    continue

                if not best_ask or not best_bid:
                    continue

                if (best_ask - best_bid) > MAX_SPREAD_CENTS:
                    continue

                kalshi_prob = best_ask / 100.0
                fee = calculate_kalshi_fee(best_ask)
                net_cost = kalshi_prob + fee
                ev = true_prob - net_cost

                if ev < MIN_EV_THRESHOLD:
                    continue

                effective_bankroll = bankroll - committed_this_cycle
                contracts = calculate_kelly_size(true_prob, net_cost, effective_bankroll)
                if contracts <= 0:
                    continue

                # Exposure gate: use actual trade cost, not fixed percentage
                actual_trade_cost = contracts * net_cost
                if current_total_exposure() + actual_trade_cost > bankroll * MAX_TOTAL_EXPOSURE:
                    logging.info(
                        f"EXPOSURE GATE: {ticker} blocked. "
                        f"Would add ${actual_trade_cost:.2f} to ${current_total_exposure():.2f} "
                        f"(cap: ${bankroll * MAX_TOTAL_EXPOSURE:.2f})"
                    )
                    continue

                logging.info(
                    f"ALPHA ENTRY: {ticker} | Price: {best_ask}c | "
                    f"EV: {ev*100:.2f}% | Size: {contracts} | "
                    f"TrueProb: {true_prob*100:.1f}% | NetCost: {net_cost*100:.1f}c"
                )
                order_id = new_client_order_id()

                try:
                    portfolio_api.create_order(
                        create_order_request=CreateOrderRequest(
                            ticker=ticker,
                            action="buy",
                            type="limit",
                            side="yes",
                            count=contracts,
                            yes_price=best_ask,
                            client_order_id=order_id
                        )
                    )
                except Exception as e:
                    # Order was NEVER accepted by exchange — do NOT create a position entry
                    logging.critical(f"create_order call failed for {ticker} (order_id={order_id}): {e}")
                    continue

                time.sleep(1200)
                status, filled = get_order_status(order_id)

                if filled > 0:
                    active_positions[ticker] = {
                        "buy_price": best_ask,
                        "count": filled,
                        "team": search_name,
                        "entry_true_prob": true_prob,
                        "pending_order_id": order_id if filled < contracts else None,
                        "pending_side": "buy" if filled < contracts else None
                    }
                    committed_this_cycle += filled * net_cost
                    save_positions(active_positions)
                elif status not in ("canceled", "rejected", "expired"):
                    active_positions[ticker] = {
                        "buy_price": best_ask,
                        "count": 0,
                        "team": search_name,
                        "entry_true_prob": true_prob,
                        "pending_order_id": order_id,
                        "pending_side": "buy"
                    }
                    save_positions(active_positions)

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
        current_prob = sharp_probabilities.get(team_name)

        if current_prob is None:
            # Track consecutive blind cycles and force-exit after threshold
            blind_count = pos_data.get("_blind_cycles", 0) + 1
            pos_data["_blind_cycles"] = blind_count
            logging.warning(
                f"STOP-LOSS BLIND: No sharp prob for '{team_name}' on {ticker}. "
                f"Blind cycle {blind_count}/{BLIND_CYCLES_BEFORE_FORCE_EXIT}. "
                f"Position has {pos_data.get('count', 0)} contracts."
            )
            if blind_count >= BLIND_CYCLES_BEFORE_FORCE_EXIT:
                logging.critical(
                    f"FORCED LIQUIDATION: {ticker} — {blind_count} consecutive blind cycles. "
                    f"Attempting market exit."
                )
                # Fall through to liquidation with a synthetic low probability
                current_prob = 0.0
                pos_data["_blind_cycles"] = 0
            else:
                save_positions(active_positions)
                continue
        else:
            # Reset blind cycle counter when data is available
            pos_data.pop("_blind_cycles", None)

        entry_prob = pos_data.get("entry_true_prob")
        edge_collapsed = entry_prob is not None and (entry_prob - current_prob) >= STOP_LOSS_EDGE_DROP
        absolute_floor_breached = current_prob < HARD_STOP_LOSS_PROB

        if not (absolute_floor_breached or edge_collapsed):
            continue

        reason = "ABSOLUTE FLOOR" if absolute_floor_breached else "EDGE COLLAPSE"
        if current_prob == 0.0 and pos_data.get("_blind_cycles", 0) == 0:
            reason = "FORCED (BLIND DATA)"
        logging.warning(
            f"STOP-LOSS TRIGGERED ({reason}): {ticker} | Entry Prob: "
            f"{(entry_prob or 0)*100:.1f}% | Current Sharp Prob: {current_prob*100:.1f}%"
        )

        try:
            ob = markets_api.get_market_orderbook(ticker=ticker)
            _, best_bid = parse_orderbook(ob)
        except Exception:
            continue

        if not best_bid:
            continue

        # Percentage-based slippage floor: reject bids below 70% of fair value
        if current_prob > 0:
            min_acceptable_bid = max(1, int(current_prob * 100 * (1.0 - MAX_SLIPPAGE_PCT)))
        else:
            min_acceptable_bid = 1  # Forced liquidation accepts any price

        if best_bid < min_acceptable_bid:
            logging.warning(
                f"LIQUIDATION ABORTED: {ticker} Bid ({best_bid}c) below floor ({min_acceptable_bid}c)."
            )
            continue

        cnt = pos_data["count"]
        order_id = new_client_order_id()

        try:
            portfolio_api.create_order(
                create_order_request=CreateOrderRequest(
                    ticker=ticker,
                    action="sell",
                    type="limit",
                    side="yes",
                    count=cnt,
                    yes_price=best_bid,
                    client_order_id=order_id
                )
            )
        except Exception as e:
            logging.critical(f"create_order (sell) failed for {ticker} (order_id={order_id}): {e}")
            # Don't record a pending order for a failed API call
            continue

        time.sleep(1200)
        status, filled = get_order_status(order_id)

        if filled >= cnt:
            del active_positions[ticker]
            save_positions(active_positions)
            logging.info(f"COMPLETE LIQUIDATION: {ticker} closed at {best_bid}c ({cnt} contracts).")
        elif filled > 0:
            pos_data["count"] -= filled
            pos_data["pending_order_id"] = order_id
            pos_data["pending_side"] = "sell"
            save_positions(active_positions)
            logging.info(
                f"PARTIAL LIQUIDATION: {ticker} sold {filled}/{cnt}. "
                f"Remaining: {pos_data['count']} contracts."
            )
        else:
            logging.warning(f"LIQUIDATION UNFILLED: {ticker} sell order got 0 fills at {best_bid}c.")

if __name__ == "__main__":
    logging.info("Execution Engine Process Started.")
    while True:
        try:
            run_trading_engine()
        except Exception as crash:
            logging.critical(f"FATAL EXCEPTION IN LOOP: {crash}", exc_info=True)
        time.sleep(1200)