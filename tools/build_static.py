#!/usr/bin/env python3
"""
Buduje lekkie pliki JSON z rozkładowego (statycznego) GTFS ZTP Kraków
dla aplikacji PRZEJAZDY:

  data/stops.json        – przystanki + linie (route_id -> numer, typ)
  data/trips.json        – trip_id -> [linia, kierunek, blok/brygada]  (do dekodowania GTFS-RT)
  data/dep-YYYYMMDD.json – odjazdy z każdego przystanku na dany dzień (tryb awaryjny bez GTFS-RT)
  data/index.json        – metadane

Uruchamiany codziennie przez GitHub Actions (.github/workflows/gtfs.yml).
Lokalnie:  python3 tools/build_static.py --out data  [--zip A=plik.zip --zip T=plik.zip]
"""
import argparse, csv, io, json, os, sys, urllib.request, zipfile, re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

FEEDS = {"A": "https://gtfs.ztp.krakow.pl/GTFS_KRK_A.zip",
         "T": "https://gtfs.ztp.krakow.pl/GTFS_KRK_T.zip",
         "M": "https://gtfs.ztp.krakow.pl/GTFS_KRK_M.zip"}
DAYS_AHEAD = 3
TZ = ZoneInfo("Europe/Warsaw")


def read_csv(z, name):
    try:
        raw = z.read(name)
    except KeyError:
        return []
    txt = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(txt)))


def hms_to_sec(s):
    p = (s or "").strip().split(":")
    if len(p) < 2:
        return None
    return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 and p[2] else 0)


def safe_name(route_key):
    return re.sub(r"[^A-Za-z0-9_-]", "_", route_key.replace(":", "_"))


def hms_to_min(s):
    p = (s or "").strip().split(":")
    if len(p) < 2:
        return None
    return int(p[0]) * 60 + int(p[1])


def active_services(z, d: date):
    """service_id aktywne w dniu d (calendar.txt + calendar_dates.txt)."""
    ds = d.strftime("%Y%m%d")
    wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][d.weekday()]
    act = set()
    for r in read_csv(z, "calendar.txt"):
        if r.get("start_date", "0") <= ds <= r.get("end_date", "99999999") and r.get(wd) == "1":
            act.add(r["service_id"])
    for r in read_csv(z, "calendar_dates.txt"):
        if r.get("date") == ds:
            if r.get("exception_type") == "1":
                act.add(r["service_id"])
            elif r.get("exception_type") == "2":
                act.discard(r["service_id"])
    return act


def block_from_trip_id(tid):
    m = re.search(r"block_(\d+)", tid or "")
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--zip", action="append", default=[], help="FEED=ścieżka.zip (zamiast pobierania)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (testy)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    sources = dict(FEEDS)
    local = dict(x.split("=", 1) for x in a.zip)
    today = date.fromisoformat(a.today) if a.today else datetime.now(TZ).date()
    days = [today + timedelta(days=i) for i in range(-1, DAYS_AHEAD)]  # -1: kursy po północy z dnia poprzedniego

    routes_out, stops_out = {}, []
    route_idx, head_idx = {}, {}
    route_list, head_list = [], []
    trips_out = {}
    dep = {d: {} for d in days[1:]}
    feeds_ok = []
    shapes_all = {}   # "feed:shape_id" -> [[lat, lon], ...]
    trip_shape = {}   # "feed:trip_id" -> "feed:shape_id"
    sched = {}   # route_key -> {"stops": [...], "si": {stop: idx}, "trips": {tid: {...}}, "act": {date: set(service)}}

    def ridx(rid):
        if rid not in route_idx:
            route_idx[rid] = len(route_list); route_list.append(rid)
        return route_idx[rid]

    def hidx(h):
        if h not in head_idx:
            head_idx[h] = len(head_list); head_list.append(h)
        return head_idx[h]

    for feed, url in sources.items():
        try:
            if feed in local:
                data = open(local[feed], "rb").read()
            elif local:
                continue
            else:
                print("pobieram", url, file=sys.stderr)
                data = urllib.request.urlopen(url, timeout=120).read()
            z = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:  # np. brak pliku M
            print("pomijam", feed, e, file=sys.stderr)
            continue
        feeds_ok.append(feed)

        for r in read_csv(z, "routes.txt"):
            rid = f"{feed}:{r['route_id']}"
            rt = int(r.get("route_type") or 3)
            routes_out[rid] = [r.get("route_short_name") or r.get("route_long_name") or r["route_id"], rt,
                               (r.get("route_color") or "").strip()]
        for r in read_csv(z, "stops.txt"):
            if r.get("location_type") not in (None, "", "0"):
                continue
            try:
                lat, lon = round(float(r["stop_lat"]), 6), round(float(r["stop_lon"]), 6)
            except (KeyError, ValueError):
                continue
            stops_out.append([f"{feed}:{r['stop_id']}", r.get("stop_name", "").strip(),
                              r.get("stop_code", "").strip(), lat, lon, feed])

        trip_info = {}
        for r in read_csv(z, "trips.txt"):
            tid = r["trip_id"]
            rid = f"{feed}:{r['route_id']}"
            block = (r.get("block_id") or "").strip() or block_from_trip_id(tid)
            hs = (r.get("trip_headsign") or "").strip()
            trip_info[tid] = (rid, hs, r.get("service_id"))
            if (r.get("shape_id") or "").strip():
                trip_shape[f"{feed}:{tid}"] = f"{feed}:{r['shape_id'].strip()}"
            trips_out[f"{feed}:{tid}"] = [ridx(rid), hidx(hs), block]

        act = {d: active_services(z, d) for d in days}
        any_act = set().union(*act.values()) if act else set()
        for r in read_csv(z, "stop_times.txt"):
            ti = trip_info.get(r["trip_id"])
            if not ti:
                continue
            # pełny rozkład kursu (do odchyleń od rozkładu) – tylko kursy aktywne w oknie dat
            if ti[2] in any_act:
                sc = sched.setdefault(ti[0], {"stops": [], "si": {}, "trips": {}, "act": {}})
                sk0 = f"{feed}:{r['stop_id']}"
                if sk0 not in sc["si"]:
                    sc["si"][sk0] = len(sc["stops"]); sc["stops"].append(sk0)
                tr = sc["trips"].setdefault(r["trip_id"], {"s": ti[2], "h": ti[1], "t": []})
                a_s = hms_to_sec(r.get("arrival_time") or r.get("departure_time"))
                d_s = hms_to_sec(r.get("departure_time") or r.get("arrival_time"))
                try:
                    seq = int(r.get("stop_sequence") or 0)
                except ValueError:
                    seq = 0
                if a_s is not None:
                    tr["t"].append([seq, sc["si"][sk0], a_s, d_s])
                for d in days:
                    if ti[2] in act[d]:
                        sc["act"].setdefault(d.strftime("%Y%m%d"), set()).add(ti[2])
            m = hms_to_min(r.get("departure_time") or r.get("arrival_time"))
            if m is None:
                continue
            rid, hs, sid = ti
            key = f"{ridx(rid)}|{hidx(hs)}"
            sk = f"{feed}:{r['stop_id']}"
            for i, d in enumerate(days):
                if sid not in act[d]:
                    continue
                if m >= 1440:            # 25:10 -> 01:10 dnia następnego
                    if i + 1 < len(days):
                        dep[days[i + 1]].setdefault(sk, {}).setdefault(key, []).append(m - 1440)
                elif d in dep:
                    dep[d].setdefault(sk, {}).setdefault(key, []).append(m)

        # geometria tras (shapes.txt) – tylko kształty używane przez kursy z okna dat
        need = {trip_shape.get(f"{feed}:{tid}") for sc in sched.values() for tid in sc["trips"]} - {None}
        pts = {}
        for r in read_csv(z, "shapes.txt"):
            k = f"{feed}:{r['shape_id']}"
            if k not in need:
                continue
            try:
                pts.setdefault(k, []).append((int(r.get("shape_pt_sequence") or 0), round(float(r["shape_pt_lat"]), 5), round(float(r["shape_pt_lon"]), 5)))
            except (KeyError, ValueError):
                pass
        for k, v in pts.items():
            v.sort()
            shapes_all[k] = [[x[1], x[2]] for x in v]

    if not feeds_ok:
        sys.exit("Nie udało się wczytać żadnego GTFS")

    gen = datetime.now(TZ).isoformat(timespec="seconds")
    w = lambda name, obj: json.dump(obj, open(os.path.join(a.out, name), "w", encoding="utf-8"),
                                    ensure_ascii=False, separators=(",", ":"))
    w("stops.json", {"generated": gen, "feeds": feeds_ok, "routes": routes_out, "stops": stops_out})
    w("trips.json", {"generated": gen, "routes": route_list, "heads": head_list, "trips": trips_out})
    # rozkład kursów per linia: data/sched/<linia>.json
    sdir = os.path.join(a.out, "sched"); os.makedirs(sdir, exist_ok=True)
    for f in os.listdir(sdir):
        os.remove(os.path.join(sdir, f))
    for rk, sc in sched.items():
        trips, shp = {}, {}
        feed = rk.split(":")[0]
        for tid, tr in sc["trips"].items():
            tr["t"].sort()
            sk = trip_shape.get(f"{feed}:{tid}")
            if sk in shapes_all:
                shp[sk] = shapes_all[sk]
            trips[tid] = [tr["s"], tr["h"], [[x[1], x[2], x[3]] for x in tr["t"]], sk if sk in shapes_all else ""]
        json.dump({"route": rk, "stops": sc["stops"], "act": {k: sorted(v) for k, v in sc["act"].items()}, "trips": trips, "shapes": shp},
                  open(os.path.join(sdir, safe_name(rk) + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    files = []
    for d, st in dep.items():
        for s in st.values():
            for k in s:
                s[k].sort()
        fn = f"dep-{d.strftime('%Y%m%d')}.json"
        w(fn, {"date": d.isoformat(), "routes": route_list, "heads": head_list, "stops": st})
        files.append(fn)
    # usuń stare pliki odjazdów
    for f in os.listdir(a.out):
        if f.startswith("dep-") and f not in files:
            os.remove(os.path.join(a.out, f))
    w("index.json", {"generated": gen, "feeds": feeds_ok, "dep": files})
    print("OK", feeds_ok, len(stops_out), "przystanków", len(trips_out), "kursów", file=sys.stderr)


if __name__ == "__main__":
    main()
