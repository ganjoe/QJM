import os
import argparse
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
import json
from datetime import datetime
from collections import defaultdict, Counter

DRY_RUN = False

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = "/home/daniel/QJM/llm-gateway/.env"
XML_FILE = os.path.join(SCRIPT_DIR, "trades.xml")
POSTGREST_URL = "http://127.0.0.1:3001"

def load_env(env_path):
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip()
    return env_vars

env = load_env(ENV_FILE)
SERVICE_ROLE_KEY = env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SERVICE_ROLE_KEY")

if not SERVICE_ROLE_KEY:
    print("Error: SERVICE_ROLE_KEY not found in .env file.")
    exit(1)

# Disable proxy auto-detection which can cause hangs on local addresses
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)
urllib.request.install_opener(opener)

def postgrest_request(path, method="GET", data=None):
    url = f"{POSTGREST_URL}{path}"
    headers = {
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    
    if method in ("POST", "PATCH"):
        headers["Prefer"] = "return=representation"
        
    req_data = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    print(f"-> Sending {method} request to {url}...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = response.read().decode("utf-8")
            if res_data:
                return json.loads(res_data)
            return None
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode('utf-8')}", flush=True)
        raise e
    except Exception as e:
        print(f"Request Error: {e}", flush=True)
        raise e


def get_existing_ids():
    try:
        # ALL event types, not just FILLs: the XML id is stored verbatim as trade_id for
        # cash transfers and dividends, so restricting this to FILLs made every import
        # insert each deposit/withdrawal/dividend again (observed: every cash row booked
        # exactly twice, which inflated the injected-cash figure by ~13,000 EUR).
        rows = postgrest_request("/pta_execution_log?select=trade_id")
        return {r["trade_id"] for r in rows if r.get("trade_id")}
    except Exception as e:
        print(f"Error fetching existing trade_ids: {e}")
        return set()

def fill_fingerprint(ticker, action, quantity, price, created_at):
    """Content fingerprint of a fill: ticker + side + qty + price + calendar day.

    Used to recognise a fill that is ALREADY in the database from the broker
    (sync-captured or one-time repair) so a later CSV import does not book the same
    execution a second time. Prices are rounded to 4 decimals and only the date is
    compared, because the broker's execution time and the imported time can differ.
    """
    day = (created_at or "")[:10]
    q = round(float(quantity), 4) if quantity is not None else None
    p = round(float(price), 4) if price is not None else None
    return (ticker, action, q, p, day)


def get_existing_fill_fingerprints(mode="live"):
    """Multiset of fill fingerprints already in the database.

    A Counter, not a set: two identical partial fills on the same day must be matched
    two-for-two. A set would let a second, genuinely new identical fill be skipped.
    """
    try:
        rows = postgrest_request(
            f"/pta_execution_log?select=ticker,action,quantity,price,created_at"
            f"&event_type=eq.FILL&mode=eq.{mode}")
        return Counter(fill_fingerprint(r["ticker"], r["action"], r.get("quantity"),
                                        r.get("price"), r.get("created_at")) for r in rows)
    except Exception as e:
        print(f"Error fetching existing fill fingerprints: {e}")
        return Counter()


def get_open_positions_by_ticker(mode="live"):
    """Currently open positions, oldest first, to seed the FIFO matcher.

    Without this seed the matcher only sees the current insert batch: a fill whose
    counterpart was imported in an EARLIER run can never match, so a closing SELL
    would be booked as a brand-new short instead of closing the existing long
    (and vice versa). Returns (longs, shorts) as {ticker: [queue entries]}.
    """
    longs, shorts = defaultdict(list), defaultdict(list)
    try:
        rows = postgrest_request(
            f"/pta_active_positions?select=trade_id,ticker,net_quantity,open_time"
            f"&mode=eq.{mode}&order=open_time.asc")
        for r in rows:
            qty = float(r["net_quantity"])
            entry = {"trade_id": r["trade_id"], "remaining": abs(qty), "fill": None}
            (longs if qty > 0 else shorts)[r["ticker"]].append(entry)
    except Exception as e:
        print(f"Error fetching open positions for FIFO seeding: {e}")
    return longs, shorts


def parse_number(val):
    if not val:
        return 0.0
    val = val.strip().replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0

def parse_datetime(date_str, time_str=None):
    if not time_str:
        time_str = "12:00:00"
    try:
        dt = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%d.%m.%Y %H:%M:%S")
        return dt.isoformat() + "Z"
    except ValueError:
        try:
            dt = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y-%m-%d %H:%M:%S")
            return dt.isoformat() + "Z"
        except ValueError:
            return None


def fifo_match_trades(events, seed_longs=None, seed_shorts=None):
    """
    FIFO-Matching: Assigns matching trade_ids so that BUY and SELL of the same
    ticker share a trade_id (= a complete round-trip trade).

    When a SELL spans multiple BUYs (scaling-in), ALL consumed BUY fills get
    reassigned to the first BUY's trade_id. This ensures the SQL view sees
    qty_bought == qty_sold for the complete round-trip.

    `seed_longs` / `seed_shorts` carry OPEN positions that are already in the
    database from earlier imports (see get_open_positions_by_ticker). They are put
    in front of the queues, so a fill that closes a position opened in an earlier
    batch matches it instead of creating an orphan opposite position. Seeded entries
    have no fill object: their trade_id is already correct in the database and is
    therefore never rewritten.
    """
    # Only match FILL events; pass through everything else unchanged
    fills = [e for e in events if e.get("event_type") == "FILL"]
    non_fills = [e for e in events if e.get("event_type") != "FILL"]

    # Sort fills chronologically
    fills.sort(key=lambda e: e.get("created_at") or "")

    # Group by ticker
    by_ticker = defaultdict(list)
    for f in fills:
        by_ticker[f["ticker"]].append(f)

    matched_fills = []
    stats = {"matched": 0, "unmatched": 0, "seeded": 0}

    for ticker, ticker_fills in by_ticker.items():
        # Queue entries: {"trade_id", "remaining", "fill"} — fill is None for seeded
        # (already persisted) positions, which must not be mutated.
        buy_queue = [dict(e) for e in (seed_longs or {}).get(ticker, [])]
        sell_queue = [dict(e) for e in (seed_shorts or {}).get(ticker, [])]

        def consume(queue, master_id, qty):
            """Consume qty FIFO from queue, unifying batch fills onto master_id.
            Returns the leftover quantity that found no counterpart."""
            remaining = qty
            while remaining > 0 and queue:
                head = queue[0]
                if head["fill"] is not None:
                    head["fill"]["trade_id"] = master_id  # unify trade_id
                else:
                    stats["seeded"] += 1
                if remaining >= head["remaining"]:
                    queue.pop(0)
                    remaining -= head["remaining"]
                else:
                    head["remaining"] -= remaining
                    remaining = 0
            return remaining

        for fill in ticker_fills:
            action = fill["action"]
            qty = fill["quantity"]

            if action == "BUY":
                if sell_queue:
                    # Short cover: match against oldest open short
                    master_id = sell_queue[0]["trade_id"]
                    fill["trade_id"] = master_id
                    stats["matched"] += 1
                    leftover = consume(sell_queue, master_id, qty)
                    if leftover > 0:
                        buy_queue.append({"trade_id": fill["trade_id"], "remaining": leftover, "fill": fill})
                else:
                    # New long position
                    buy_queue.append({"trade_id": fill["trade_id"], "remaining": qty, "fill": fill})
                    stats["unmatched"] += 1

            elif action == "SELL":
                if buy_queue:
                    # Close long: match against oldest open buy(s).
                    # The master trade_id is the first BUY's trade_id.
                    master_id = buy_queue[0]["trade_id"]
                    fill["trade_id"] = master_id
                    stats["matched"] += 1
                    leftover = consume(buy_queue, master_id, qty)
                    if leftover > 0:
                        sell_queue.append({"trade_id": fill["trade_id"], "remaining": leftover, "fill": fill})
                else:
                    # New short position
                    sell_queue.append({"trade_id": fill["trade_id"], "remaining": qty, "fill": fill})
                    stats["unmatched"] += 1

            matched_fills.append(fill)

    print(f"-> FIFO Matching: {stats['matched']} fills matched, {stats['unmatched']} new positions, "
          f"{stats['seeded']} matched against positions from earlier imports", flush=True)
    return matched_fills + non_fills


def main():
    if not os.path.exists(XML_FILE):
        print(f"Error: XML file not found at {XML_FILE}")
        return

    print("Fetching existing IDs from database...", flush=True)
    existing_ids = get_existing_ids()
    print(f"Found {len(existing_ids)} existing trade/event IDs in the database.", flush=True)

    # Cross-batch matching + protection against re-booking broker-sourced fills.
    seed_longs, seed_shorts = get_open_positions_by_ticker()
    existing_fps = get_existing_fill_fingerprints()
    print(f"Found {sum(len(v) for v in seed_longs.values())} open long / "
          f"{sum(len(v) for v in seed_shorts.values())} open short position(s) to seed FIFO matching, "
          f"and {sum(existing_fps.values())} fill(s) already recorded.", flush=True)

    print(f"Parsing XML file: {XML_FILE}", flush=True)
    tree = ET.parse(XML_FILE)
    root = tree.getroot()

    events_to_insert = []

    # 1. Parse Trades
    trades_node = root.find("Trades")
    if trades_node is not None:
        for trade in trades_node.findall("Trade"):
            trade_id = trade.get("id")
            if not trade_id:
                continue
            if trade_id in existing_ids:
                continue

            name = trade.get("name", "")
            isin = trade.get("isin", "")

            meta = trade.find("Meta")
            date_str = meta.find("Date").text if meta is not None and meta.find("Date") is not None else ""
            time_str = meta.find("Time").text if meta is not None and meta.find("Time") is not None else ""

            instrument = trade.find("Instrument")
            symbol = instrument.find("Symbol").text if instrument is not None and instrument.find("Symbol") is not None else ""
            currency = instrument.find("Currency").text if instrument is not None and instrument.find("Currency") is not None else "USD"

            execution = trade.find("Execution")
            qty_raw = execution.find("Quantity").text if execution is not None and execution.find("Quantity") is not None else "0"
            price_raw = execution.find("Price").text if execution is not None and execution.find("Price") is not None else "0"
            comm_raw = execution.find("Commission").text if execution is not None and execution.find("Commission") is not None else "0"

            qty_val = parse_number(qty_raw)
            action = "BUY" if qty_val > 0 else "SELL"
            quantity = abs(qty_val)
            price = parse_number(price_raw)
            commission = abs(parse_number(comm_raw))

            created_at = parse_datetime(date_str, time_str)

            notes = f"{name} (ISIN: {isin})" if name or isin else "Migrated trade"

            events_to_insert.append({
                "trade_id": trade_id,
                "ticker": symbol,
                "event_type": "FILL",
                "action": action,
                "quantity": quantity,
                "price": price,
                "commission": commission,
                "currency": currency,
                "notes": notes,
                "created_at": created_at
            })

    # 2. Parse Dividends
    divs_node = root.find("Dividends")
    if divs_node is not None:
        for div in divs_node.findall("Dividend"):
            div_id = div.get("id")
            if not div_id:
                continue
            if div_id in existing_ids:
                continue

            date_str = div.find("Date").text if div.find("Date") is not None else ""
            symbol = div.find("Symbol").text if div.find("Symbol") is not None else ""
            amount_raw = div.find("Amount").text if div.find("Amount") is not None else "0"
            currency = div.find("Currency").text if div.find("Currency") is not None else "USD"
            desc = div.find("Desc").text if div.find("Desc") is not None else "Dividend payment"

            amount = abs(parse_number(amount_raw))
            created_at = parse_datetime(date_str, "12:00:00")

            events_to_insert.append({
                "trade_id": div_id,
                "ticker": symbol,
                "event_type": "CASH_TRANSFER",
                "action": "DEPOSIT",
                "quantity": amount,
                "price": None,
                "commission": 0.0,
                "currency": currency,
                "notes": desc,
                "created_at": created_at
            })

    # 3. Parse Deposits/Withdrawals
    deps_node = root.find("DepositsWithdrawals")
    if deps_node is not None:
        for trans in deps_node.findall("Transaction"):
            trans_id = trans.get("id")
            if not trans_id:
                continue
            if trans_id in existing_ids:
                continue

            date_str = trans.find("Date").text if trans.find("Date") is not None else ""
            desc = trans.find("Desc").text if trans.find("Desc") is not None else "Cash Transfer"
            amount_raw = trans.find("Amount").text if trans.find("Amount") is not None else "0"
            currency = trans.find("Currency").text if trans.find("Currency") is not None else "EUR"

            amount_val = parse_number(amount_raw)
            action = "DEPOSIT" if amount_val > 0 else "WITHDRAW"
            amount = abs(amount_val)
            created_at = parse_datetime(date_str, "12:00:00")

            events_to_insert.append({
                "trade_id": trans_id,
                "ticker": "CASH",
                "event_type": "CASH_TRANSFER",
                "action": action,
                "quantity": amount,
                "price": None,
                "commission": 0.0,
                "currency": currency,
                "notes": desc,
                "created_at": created_at
            })

    # Do not book a fill that is already in the database. Count-based: an incoming fill
    # is skipped only while an unused matching row exists, so N identical partial fills
    # are matched N-for-N while genuinely new ones still get inserted.
    deduped, skipped = [], []
    remaining_fps = Counter(existing_fps)
    for e in events_to_insert:
        if e.get("event_type") == "FILL":
            fp = fill_fingerprint(e["ticker"], e["action"], e.get("quantity"), e.get("price"), e.get("created_at"))
            if remaining_fps[fp] > 0:
                remaining_fps[fp] -= 1
                skipped.append(e)
                continue
        deduped.append(e)
    for e in skipped:
        print(f"  SKIP already recorded: {e['ticker']} {e['action']} "
              f"{e.get('quantity')} @ {e.get('price')} on {(e.get('created_at') or '')[:10]}", flush=True)
    if skipped:
        print(f"-> {len(skipped)} fill(s) skipped (already in the database).", flush=True)
    events_to_insert = deduped

    total_events = len(events_to_insert)
    if total_events == 0:
        print("No new events to migrate (all exist already or file is empty).")
        return

    # FIFO-Match trade_ids before inserting (seeded with positions from earlier imports)
    events_to_insert = fifo_match_trades(events_to_insert, seed_longs, seed_shorts)

    if DRY_RUN:
        print(f"DRY RUN: nothing written. {total_events} event(s) would be inserted:", flush=True)
        for e in events_to_insert:
            print(f"  {e['event_type']:15} {e.get('ticker',''):6} {e.get('action',''):8} "
                  f"qty={e.get('quantity')} price={e.get('price')} "
                  f"trade_id={e['trade_id']}", flush=True)
        return

    print(f"Prepared {total_events} events for migration. Pushing to database...", flush=True)

    # PostgREST allows inserting multiple rows by POSTing a JSON list.
    # We will send them in chunks of 100 to avoid request size issues, although 1000s is usually fine.
    chunk_size = 100
    for i in range(0, total_events, chunk_size):
        chunk = events_to_insert[i:i+chunk_size]
        try:
            postgrest_request("/pta_execution_log", method="POST", data=chunk)
            print(f"Migrated {i + len(chunk)} / {total_events} events...", flush=True)
        except Exception as e:
            print(f"Error migrating chunk starting at index {i}: {e}", flush=True)
            return

    print("Migration completed successfully!", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push ibkr_csv_importer/trades.xml into Supabase/PostgreSQL")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be imported without writing anything to the database")
    _args = parser.parse_args()
    DRY_RUN = _args.dry_run
    main()
