"""
FPL Weekly Recap - GitHub Actions data export
-----------------------------------------------
Same data pulled by weekly_recap.py, but written as ONE combined JSON file
instead of five CSVs, meant to be run by a GitHub Actions workflow and
committed into the repo so a static site (e.g. GitHub Pages) can fetch it
same-origin - no CORS issue, no manual re-upload each week.

If --end_gw is left at 0 (the default), it auto-detects which gameweek to
pull: if one is currently IN PROGRESS (deadline passed, matches not all
done), it includes that one and marks it "live_gw" in the output JSON (so
viewers know bonus points etc. may still shift for that week). Otherwise
it falls back to the latest fully FINISHED gameweek. Either way, no
gameweek number needs to be tracked by hand.

The output also carries "generated_at" - a UTC timestamp of when this
export ran - so the page can show "Last updated: ..." instead of
claiming to be live (it isn't - it only refreshes when someone clicks
the workflow button).

Usage (matches what the GitHub Actions workflow calls):
    python3 export_fpl_data.py --league_id=14514 --end_gw=0
    python3 export_fpl_data.py --league_id=14514 --end_gw=3   # force a specific gw

Output: data/fpl-data.json (path can be overridden with --output_path)
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

FPL_URL = "https://fantasy.premierleague.com/api/"
BOOTSTRAP_URL = FPL_URL + "bootstrap-static/"
LEAGUE_CLASSIC_URL = FPL_URL + "leagues-classic/"
LIVE_URL = FPL_URL + "event/{gw}/live/"
PICKS_URL = FPL_URL + "entry/{entry_id}/event/{gw}/picks/"


def get_bootstrap(session):
    return session.get(BOOTSTRAP_URL).json()


def get_player_info(bootstrap):
    position_names = {pt["id"]: pt["singular_name_short"] for pt in bootstrap["element_types"]}
    team_names = {team["id"]: team["short_name"] for team in bootstrap["teams"]}
    return {
        el["id"]: {
            "name": el["web_name"],
            "position": position_names.get(el["element_type"], "?"),
            "team": team_names.get(el["team"], "?"),
        }
        for el in bootstrap["elements"]
    }


def get_latest_finished_gw(bootstrap):
    """Highest gameweek number that FPL has marked as finished."""
    finished = [e["id"] for e in bootstrap["events"] if e.get("finished")]
    if not finished:
        raise RuntimeError("No finished gameweeks yet this season.")
    return max(finished)


def resolve_end_gw(bootstrap):
    """Decide which gameweek to pull up to when --end_gw=0 (auto).

    Returns (end_gw, is_live):
      - If a gameweek is currently in progress (deadline passed, matches
        not all finished yet), include it and report is_live=True - its
        numbers (especially bonus points) can still shift until FPL marks
        it finished.
      - Otherwise, fall back to the latest fully-finished gameweek,
        is_live=False.
    """
    events = bootstrap["events"]
    finished = [e["id"] for e in events if e.get("finished")]
    latest_finished = max(finished) if finished else 0

    current = next((e for e in events if e.get("is_current")), None)
    if current and not current.get("finished"):
        return current["id"], True

    if not latest_finished:
        raise RuntimeError("No finished or in-progress gameweeks yet this season.")
    return latest_finished, False


def get_league_entries(session, league_id):
    entries = []
    page = 1
    while True:
        url = (
            LEAGUE_CLASSIC_URL
            + str(league_id)
            + "/standings/?page_new_entries=1&page_standings="
            + str(page)
            + "&phase=1"
        )
        data = session.get(url).json()
        results = data["standings"]["results"]
        if not results:
            break
        for r in results:
            entries.append(
                {"entry_id": r["entry"], "manager_name": r["player_name"], "team_name": r["entry_name"]}
            )
        if not data["standings"]["has_next"]:
            break
        page += 1
    return entries


def get_gw_live_points(session, gw):
    data = session.get(LIVE_URL.format(gw=gw)).json()
    return {el["id"]: el["stats"]["total_points"] for el in data["elements"]}


def get_entry_gw_picks(session, entry_id, gw):
    return session.get(PICKS_URL.format(entry_id=entry_id, gw=gw)).json()


FPL_ENTRY_URL = "https://fantasy.premierleague.com/entry/{entry_id}/event/{gw}"


def build_manager_gameweek_rows(session, entries, start_gw, end_gw, player_info):
    rows, popularity_rows = [], []
    prev_squads = {}
    season_transfers = {}  # entry_id -> cumulative transfers made so far this season

    for gw in range(start_gw, end_gw + 1):
        gw_player_points = get_gw_live_points(session, gw)
        squads_this_gw = {}
        captain_counts = Counter()
        owned_counts = Counter()
        num_managers_this_gw = 0

        for entry in entries:
            data = get_entry_gw_picks(session, entry["entry_id"], gw)
            hist = data.get("entry_history")
            if not hist:
                continue
            num_managers_this_gw += 1

            picks = data.get("picks", [])
            squad_ids = {p["element"] for p in picks}
            captain_pick = next((p for p in picks if p["is_captain"]), None)
            captain_id = captain_pick["element"] if captain_pick else None
            captain_multiplier = captain_pick["multiplier"] if captain_pick else 0
            captain_raw_points = gw_player_points.get(captain_id, 0) if captain_id else 0

            # FPL's own "points" field on entry_history (picks endpoint) can lag
            # behind the live per-player stats endpoint - especially right after
            # a match finishes, while bonus points are still being finalized.
            # Rather than trust that separately-cached total, compute it
            # ourselves from the same live per-player data used for the squad
            # breakdown below, so the two always agree and both stay fresh.
            gw_points_computed = sum(
                gw_player_points.get(p["element"], 0) * p["multiplier"] for p in picks
            )
            bench_points_computed = sum(
                gw_player_points.get(p["element"], 0) for p in picks if p["multiplier"] == 0
            )
            points_delta = gw_points_computed - hist["points"]

            season_transfers[entry["entry_id"]] = (
                season_transfers.get(entry["entry_id"], 0) + hist["event_transfers"]
            )

            for pid in squad_ids:
                owned_counts[pid] += 1
            if captain_id:
                captain_counts[captain_id] += 1

            prev_squad = prev_squads.get(entry["entry_id"])
            if prev_squad is not None:
                transferred_in = squad_ids - prev_squad
                transferred_out = prev_squad - squad_ids
                points_in = sum(gw_player_points.get(pid, 0) for pid in transferred_in)
                points_out = sum(gw_player_points.get(pid, 0) for pid in transferred_out)
                net_transfer_impact = points_in - points_out - hist["event_transfers_cost"]
                transferred_in_names = ", ".join(
                    player_info.get(pid, {}).get("name", "Unknown") for pid in transferred_in
                )
                transferred_out_names = ", ".join(
                    player_info.get(pid, {}).get("name", "Unknown") for pid in transferred_out
                )
            else:
                points_in = None
                points_out = None
                net_transfer_impact = None
                transferred_in_names = ""
                transferred_out_names = ""

            rows.append(
                {
                    "gameweek": gw,
                    "entry_id": entry["entry_id"],
                    "manager_name": entry["manager_name"],
                    "team_name": entry["team_name"],
                    "gw_points": gw_points_computed,
                    "cumulative_points": hist["total_points"] + points_delta,
                    "bench_points": bench_points_computed,
                    "team_value": hist["value"] / 10,
                    "bank": hist["bank"] / 10,
                    "overall_rank": hist["overall_rank"],
                    "num_transfers": hist["event_transfers"],
                    "transfer_cost": hist["event_transfers_cost"],
                    "net_transfer_impact": net_transfer_impact,
                    "transferred_in": transferred_in_names,
                    "transferred_out": transferred_out_names,
                    "transfer_points_in": points_in,
                    "transfer_points_out": points_out,
                    "season_transfers_to_date": season_transfers[entry["entry_id"]],
                    "chip_played": data.get("active_chip"),
                    "captain_id": captain_id,
                    "captain_name": player_info.get(captain_id, {}).get("name", "Unknown"),
                    "captain_multiplier": captain_multiplier,
                    "captain_contribution": captain_raw_points * captain_multiplier,
                    # Full per-player squad detail used to be included here as its
                    # own dataset (every player, every manager, every gameweek) -
                    # that's the bulk of the file's size and it only grows every
                    # week. A link to the manager's own team page on FPL's site
                    # covers the same "who did they play" need on demand, without
                    # carrying the data ourselves.
                    "team_link": FPL_ENTRY_URL.format(entry_id=entry["entry_id"], gw=gw),
                }
            )

            squads_this_gw[entry["entry_id"]] = squad_ids

        prev_squads = squads_this_gw

        for pid, owned_count in owned_counts.items():
            popularity_rows.append(
                {
                    "gameweek": gw,
                    "player_id": pid,
                    "player_name": player_info.get(pid, {}).get("name", "Unknown"),
                    "times_owned": owned_count,
                    "pct_owned": round(100 * owned_count / num_managers_this_gw, 1)
                    if num_managers_this_gw
                    else 0,
                    "times_captained": captain_counts.get(pid, 0),
                    "pct_captained": round(100 * captain_counts.get(pid, 0) / num_managers_this_gw, 1)
                    if num_managers_this_gw
                    else 0,
                }
            )

    return rows, popularity_rows


def add_league_rank_and_movement(df):
    df = df.copy()
    df["league_rank"] = df.groupby("gameweek")["cumulative_points"].rank(
        ascending=False, method="min"
    ).astype(int)
    df = df.sort_values(["entry_id", "gameweek"])
    df["prev_rank"] = df.groupby("entry_id")["league_rank"].shift(1)
    df["rank_change"] = df["prev_rank"] - df["league_rank"]
    df = df.drop(columns=["prev_rank"])
    return df.sort_values(["gameweek", "league_rank"]).reset_index(drop=True)


def build_gameweek_highlights(df, popularity_df):
    first_gw = df["gameweek"].min()
    highlight_rows = []
    for gw, gw_df in df.groupby("gameweek"):
        if gw == first_gw:
            # Everyone's squad is brand new on the very first tracked
            # gameweek - nobody's had a chance to be a "ghost" yet, so
            # nobody is excluded here.
            active_df = gw_df
        else:
            # A "ghost" - a manager who has made zero transfers all season -
            # is still running whatever squad they set on gw1. That can go
            # stale in ways that quietly "win" awards they shouldn't: e.g. a
            # captain who's since left the Premier League entirely and now
            # scores 0 forever, making them a lock for "worst captain" every
            # week. Exclude ghosts from every comparison below, not just the
            # transfer ones.
            active_df = gw_df[gw_df["season_transfers_to_date"] > 0]
            if active_df.empty:  # everyone happens to be a ghost - don't blank the gw
                active_df = gw_df

        top = active_df.loc[active_df["gw_points"].idxmax()]
        bottom = active_df.loc[active_df["gw_points"].idxmin()]

        movers = active_df.dropna(subset=["rank_change"])
        riser = movers.loc[movers["rank_change"].idxmax()] if not movers.empty else None
        faller = movers.loc[movers["rank_change"].idxmin()] if not movers.empty else None

        best_cap = active_df.loc[active_df["captain_contribution"].idxmax()]
        worst_cap = active_df.loc[active_df["captain_contribution"].idxmin()]
        most_wasted = active_df.loc[active_df["bench_points"].idxmax()]

        value_king = active_df.loc[active_df["team_value"].idxmax()]
        value_laggard = active_df.loc[active_df["team_value"].idxmin()]

        chip_rows = active_df[active_df["chip_played"].notna()]
        chip_master = chip_rows.loc[chip_rows["gw_points"].idxmax()] if not chip_rows.empty else None
        no_chip_rows = active_df[active_df["chip_played"].isna()]
        no_chip_warrior = (
            no_chip_rows.loc[no_chip_rows["gw_points"].idxmax()] if not no_chip_rows.empty else None
        )

        # On top of the season-long ghost exclusion above, only managers who
        # actually made a transfer THIS gameweek are eligible for the
        # transfer awards - an otherwise-active manager who simply didn't
        # transfer this particular week still nets exactly 0, which isn't a
        # "transfer" to award.
        transfer_rows = active_df.dropna(subset=["net_transfer_impact"])
        transfer_rows = transfer_rows[transfer_rows["num_transfers"] > 0]
        if not transfer_rows.empty:
            sharpest_trader = transfer_rows.loc[transfer_rows["net_transfer_impact"].idxmax()]
            transfer_tangle = transfer_rows.loc[transfer_rows["net_transfer_impact"].idxmin()]
        else:
            sharpest_trader = transfer_tangle = None

        one_move_rows = transfer_rows[transfer_rows["num_transfers"] == 1]
        one_move_rows = one_move_rows[one_move_rows["net_transfer_impact"] > 0]
        one_move_master = (
            one_move_rows.loc[one_move_rows["net_transfer_impact"].idxmax()]
            if not one_move_rows.empty
            else None
        )

        chips_this_gw = gw_df[gw_df["chip_played"].notna()][["manager_name", "chip_played"]].to_dict(
            "records"
        )

        pop_gw = popularity_df[popularity_df["gameweek"] == gw]
        most_captained = pop_gw.loc[pop_gw["pct_captained"].idxmax()] if not pop_gw.empty else None
        most_owned = pop_gw.loc[pop_gw["pct_owned"].idxmax()] if not pop_gw.empty else None

        def name_or_blank(row, col="manager_name"):
            return row[col] if row is not None else ""

        highlight_rows.append(
            {
                "gameweek": gw,
                "top_scorer": top["manager_name"],
                "top_score": top["gw_points"],
                "bottom_scorer": bottom["manager_name"],
                "bottom_score": bottom["gw_points"],
                "biggest_riser": name_or_blank(riser),
                "riser_places_gained": riser["rank_change"] if riser is not None else None,
                "biggest_faller": name_or_blank(faller),
                "faller_places_lost": abs(faller["rank_change"]) if faller is not None else None,
                "best_captain_manager": best_cap["manager_name"],
                "best_captain_player": best_cap["captain_name"],
                "best_captain_points": best_cap["captain_contribution"],
                "worst_captain_manager": worst_cap["manager_name"],
                "worst_captain_player": worst_cap["captain_name"],
                "worst_captain_points": worst_cap["captain_contribution"],
                "most_wasted_bench_manager": most_wasted["manager_name"],
                "wasted_bench_points": most_wasted["bench_points"],
                "value_king_manager": value_king["manager_name"],
                "value_king_value": value_king["team_value"],
                "value_laggard_manager": value_laggard["manager_name"],
                "value_laggard_value": value_laggard["team_value"],
                "chip_master_manager": name_or_blank(chip_master),
                "chip_master_chip": chip_master["chip_played"] if chip_master is not None else "",
                "chip_master_score": chip_master["gw_points"] if chip_master is not None else None,
                "no_chip_warrior_manager": name_or_blank(no_chip_warrior),
                "no_chip_warrior_score": no_chip_warrior["gw_points"]
                if no_chip_warrior is not None
                else None,
                # "Total best transfer" - best net impact regardless of how many
                # moves it took; players_in/out list everyone who moved that gw.
                "sharpest_trader_manager": name_or_blank(sharpest_trader),
                "sharpest_trader_net_impact": sharpest_trader["net_transfer_impact"]
                if sharpest_trader is not None
                else None,
                "sharpest_trader_players_in": sharpest_trader["transferred_in"]
                if sharpest_trader is not None
                else "",
                "sharpest_trader_players_out": sharpest_trader["transferred_out"]
                if sharpest_trader is not None
                else "",
                "sharpest_trader_points_in": sharpest_trader["transfer_points_in"]
                if sharpest_trader is not None
                else None,
                "sharpest_trader_points_out": sharpest_trader["transfer_points_out"]
                if sharpest_trader is not None
                else None,
                "transfer_tangle_manager": name_or_blank(transfer_tangle),
                "transfer_tangle_net_impact": transfer_tangle["net_transfer_impact"]
                if transfer_tangle is not None
                else None,
                "transfer_tangle_players_in": transfer_tangle["transferred_in"]
                if transfer_tangle is not None
                else "",
                "transfer_tangle_players_out": transfer_tangle["transferred_out"]
                if transfer_tangle is not None
                else "",
                "transfer_tangle_points_in": transfer_tangle["transfer_points_in"]
                if transfer_tangle is not None
                else None,
                "transfer_tangle_points_out": transfer_tangle["transfer_points_out"]
                if transfer_tangle is not None
                else None,
                # "Best single transfer" - exactly one player out, one player in.
                "one_move_master_manager": name_or_blank(one_move_master),
                "one_move_master_net_impact": one_move_master["net_transfer_impact"]
                if one_move_master is not None
                else None,
                "one_move_master_player_in": one_move_master["transferred_in"]
                if one_move_master is not None
                else "",
                "one_move_master_player_out": one_move_master["transferred_out"]
                if one_move_master is not None
                else "",
                "one_move_master_points_in": one_move_master["transfer_points_in"]
                if one_move_master is not None
                else None,
                "one_move_master_points_out": one_move_master["transfer_points_out"]
                if one_move_master is not None
                else None,
                "most_captained_player": most_captained["player_name"] if most_captained is not None else "",
                "most_captained_pct": most_captained["pct_captained"] if most_captained is not None else None,
                "most_owned_player": most_owned["player_name"] if most_owned is not None else "",
                "most_owned_pct": most_owned["pct_owned"] if most_owned is not None else None,
                "chips_played": "; ".join(
                    f"{c['manager_name']} ({c['chip_played']})" for c in chips_this_gw
                ),
            }
        )
    return pd.DataFrame(highlight_rows)


def build_season_summary(df):
    latest_gw = df["gameweek"].max()
    latest = df[df["gameweek"] == latest_gw].set_index("entry_id")

    summary = df.groupby(["entry_id", "manager_name", "team_name"]).agg(
        total_transfers=("num_transfers", "sum"),
        total_transfer_cost=("transfer_cost", "sum"),
        best_overall_rank=("overall_rank", "min"),
        gameweeks_tracked=("gameweek", "count"),
    ).reset_index()

    summary["current_team_value"] = summary["entry_id"].map(latest["team_value"])
    summary["current_cumulative_points"] = summary["entry_id"].map(latest["cumulative_points"])
    summary["current_league_rank"] = summary["entry_id"].map(latest["league_rank"])
    return summary.sort_values("current_league_rank").reset_index(drop=True)


def df_records(df):
    """Convert a DataFrame to plain JSON-safe records (NaN -> null)."""
    return json.loads(df.to_json(orient="records"))


def main(league_id=14514, start_gw=1, end_gw=0, output_path="data/fpl-data.json"):
    session = requests.session()

    print(f"Fetching player info and league '{league_id}' entries...")
    bootstrap = get_bootstrap(session)
    player_info = get_player_info(bootstrap)
    entries = get_league_entries(session, league_id)
    print(f"Found {len(entries)} managers in the league.")

    is_live = False
    if not end_gw:
        end_gw, is_live = resolve_end_gw(bootstrap)
        status = "IN PROGRESS - numbers may still shift" if is_live else "finished"
        print(f"Auto-detected gameweek {end_gw} ({status})")

    print(f"Pulling gameweeks {start_gw}-{end_gw} (this can take a minute)...")
    rows, popularity_rows = build_manager_gameweek_rows(
        session, entries, start_gw, end_gw, player_info
    )
    df = pd.DataFrame(rows)
    df = add_league_rank_and_movement(df)
    popularity_df = pd.DataFrame(popularity_rows)
    highlights_df = build_gameweek_highlights(df, popularity_df)
    season_df = build_season_summary(df)

    combined = {
        "league_id": league_id,
        "start_gw": start_gw,
        "end_gw": end_gw,
        "live_gw": end_gw if is_live else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manager_gameweek_stats": df_records(df),
        "player_gameweek_popularity": df_records(popularity_df),
        "gameweek_highlights": df_records(highlights_df),
        "season_summary": df_records(season_df),
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
