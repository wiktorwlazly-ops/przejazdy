#!/usr/bin/env python3
"""
Archiwum przebiegów tras (bez rozkładów) – do rysowania dawnych przejazdów tak, jak linia jechała w danym dniu.

  data/archive/<SYSTEM>_<LINIA>.json  – warianty przebiegu linii z okresami obowiązywania
  data/archive/index.json             – co i z jakich wersji zbudowano

Tryby:
  backfill  – jednorazowo: pobiera dawne wersje GTFS z Transitland (klucz w zmiennej TRANSITLAND_API_KEY)
              python3 tools/archive.py backfill --since 2024-07-01 --step-days 7
  update    – co noc: dopisuje przebiegi z bieżącego GTFS ZTP (pliki pobrane wcześniej przez workflow)
              python3 tools/archive.py update --zip A=/tmp/A.zip --zip T=/tmp/T.zip --zip M=/tmp/M.zip
  local     – test: python3 tools/archive.py local --zip T=plik.zip@2024-09-02 ...

Wariant przebiegu = kolejność przystanków (nazwa, słupek, współrzędne) + geometria (shapes.txt, jeśli jest).
Okres obowiązywania = daty wersji GTFS, w których wariant występował (ciągłe próbki łączone w zakresy).
"""
import argparse, csv, io, json, os, re, sys, time, zipfile, hashlib, urllib.request, urllib.error
from datetime import date, datetime, timedelta

OUT = "data/archive"
TL = "https://transit.land/api/v2/rest"
TL_FEEDS = {"T": "f-ztp~krakow~tram", "A": "f-ztp~krakow~bus"}   # Mobilis (M) – tylko od bieżącego GTFS (update)
SYS = {"T": "ZTP-T", "A": "ZTP-A", "M": "ZTP-M"}
SUMMARY = []


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, file=sys.stderr)
    SUMMARY.append(msg)


def write_summary():
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("```\n" + "\n".join(SUMMARY[-400:]) + "\n```\n")


# ---------------------------------------------------------------- GTFS → przebiegi
def read_csv(z, name):
    try:
        raw = z.read(name)
    except KeyError:
        return []
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))


def post_of(code):
    c = (code or "").strip()
    m = re.search(r"(\d{2})$", c)
    return m.group(1) if m else c


def enc_poly(pts):
    out, pl, pg = [], 0, 0
    def enc(v):
        v = ~(v << 1) if v < 0 else (v << 1)
        s = ""
        while v >= 0x20:
            s += chr((0x20 | (v & 0x1f)) + 63); v >>= 5
        return s + chr(v + 63)
    for la, lo in pts:
        a, b = round(la * 1e5), round(lo * 1e5)
        out.append(enc(a - pl) + enc(b - pg)); pl, pg = a, b
    return "".join(out)


def active_services(z, d_from, d_to):
    """service_id kursujące choć raz w dniach d_from..d_to (maks. 14 dni). None = brak kalendarza (bierzemy wszystko)."""
    cal, cd = read_csv(z, "calendar.txt"), read_csv(z, "calendar_dates.txt")
    if not cal and not cd:
        return None
    a = date.fromisoformat(d_from); b = min(date.fromisoformat(d_to), a + timedelta(days=13))
    wds = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    act = set(); d = a
    while d <= b:
        ds = d.strftime("%Y%m%d"); day = set()
        for r in cal:
            if r.get("start_date", "0") <= ds <= r.get("end_date", "99999999") and r.get(wds[d.weekday()]) == "1":
                day.add(r["service_id"])
        for r in cd:
            if r.get("date") == ds:
                (day.add if r.get("exception_type") == "1" else day.discard)(r["service_id"])
        act |= day; d += timedelta(days=1)
    return act


def extract_patterns(zbytes, feed, d_from=None, d_to=None):
    """Zwraca {linia: {klucz_wariantu: {"stops": [...], "shape": str}}} dla jednej wersji GTFS,
    tylko z kursów kursujących w dniach d_from..d_to (bez wariantów planowanych na przyszłość)."""
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    act = active_services(z, d_from, d_to or d_from) if d_from else None
    stops = {}
    for r in read_csv(z, "stops.txt"):
        try:
            stops[r["stop_id"]] = [r.get("stop_name", "").strip(), post_of(r.get("stop_code")),
                                   round(float(r["stop_lat"]), 5), round(float(r["stop_lon"]), 5)]
        except (KeyError, ValueError):
            pass
    routes = {r["route_id"]: (r.get("route_short_name") or r.get("route_long_name") or r["route_id"]).strip()
              for r in read_csv(z, "routes.txt")}
    trips = {}
    for r in read_csv(z, "trips.txt"):
        if act is not None and act and r.get("service_id") not in act:
            continue
        trips[r["trip_id"]] = (routes.get(r["route_id"], r["route_id"]), (r.get("shape_id") or "").strip())
    seqs = {}
    for r in read_csv(z, "stop_times.txt"):
        t = r["trip_id"]
        if t not in trips:
            continue
        try:
            seq = int(r.get("stop_sequence") or 0)
        except ValueError:
            seq = 0
        seqs.setdefault(t, []).append((seq, r["stop_id"]))
    # unikalne (linia, sekwencja przystanków, shape)
    uniq = {}
    for t, lst in seqs.items():
        lst.sort()
        line, shp = trips[t]
        sids = tuple(s for _, s in lst if s in stops)
        if len(sids) < 2:
            continue
        uniq.setdefault((line, sids, shp), True)
    need_shapes = {k[2] for k in uniq if k[2]}
    shapes = {}
    if need_shapes:
        tmp = {}
        for r in read_csv(z, "shapes.txt"):
            sid = r.get("shape_id")
            if sid in need_shapes:
                try:
                    tmp.setdefault(sid, []).append((int(r.get("shape_pt_sequence") or 0), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
                except (KeyError, ValueError):
                    pass
        for sid, pts in tmp.items():
            pts.sort()
            shapes[sid] = enc_poly([(p[1], p[2]) for p in pts])
    out = {}
    for (line, sids, shp) in uniq:
        st = [stops[s] for s in sids]
        sh = shapes.get(shp, "")
        key = hashlib.sha1(json.dumps([st, sh], ensure_ascii=False).encode()).hexdigest()[:16]
        out.setdefault(line, {})[key] = {"stops": st, "shape": sh}
    return out


# ---------------------------------------------------------------- archiwum na dysku
def safe(s):
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def load_archive():
    arc = {}
    if os.path.isdir(OUT):
        for f in os.listdir(OUT):
            if f.endswith(".json") and f != "index.json":
                j = json.load(open(os.path.join(OUT, f), encoding="utf-8"))
                arc[(j["sys"], j["line"])] = {p["key"]: p for p in j["patterns"]}
    return arc


def merge(arc, sysname, pats, d_from, d_to):
    """Dopisuje warianty z jednej wersji GTFS ważnej (w przybliżeniu) od d_from do d_to."""
    n_new = 0
    for line, variants in pats.items():
        bucket = arc.setdefault((sysname, line), {})
        for key, v in variants.items():
            p = bucket.get(key)
            if not p:
                p = bucket[key] = {"key": key, "stops": v["stops"], "shape": v["shape"], "ranges": []}
                n_new += 1
            rs = p["ranges"]
            # łączenie zakresów: nachodzące lub stykające się (≤ 1 dzień przerwy)
            rs.append([d_from, d_to])
            rs.sort()
            merged = []
            for a, b in rs:
                if merged and date.fromisoformat(a) <= date.fromisoformat(merged[-1][1]) + timedelta(days=1):
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            p["ranges"] = merged
    return n_new


def save_archive(arc, meta):
    os.makedirs(OUT, exist_ok=True)
    keep = set()
    lines = {}
    for (sysname, line), bucket in arc.items():
        fn = f"{safe(sysname)}_{safe(line)}.json"
        keep.add(fn)
        pats = sorted(bucket.values(), key=lambda p: p["ranges"][0][0] if p["ranges"] else "")
        json.dump({"sys": sysname, "line": line, "patterns": pats}, open(os.path.join(OUT, fn), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        lines.setdefault(sysname, []).append(line)
    old = {}
    ip = os.path.join(OUT, "index.json")
    if os.path.exists(ip):
        old = json.load(open(ip, encoding="utf-8"))
    idx = {"generated": datetime.now().isoformat(timespec="seconds"),
           "lines": {k: sorted(v, key=lambda x: (len(x), x)) for k, v in lines.items()},
           "sources": (old.get("sources") or []) + meta}
    idx["sources"] = idx["sources"][-2000:]
    json.dump(idx, open(ip, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


# ---------------------------------------------------------------- Transitland
def tl_get(url, key, binary=False, tries=3):
    for i in range(tries):
        req = urllib.request.Request(url, headers={"apikey": key, "User-Agent": "przejazdy-archive"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
                return data if binary else json.loads(data)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(10 * (i + 1)); continue
            raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]} :: {body}")
        except urllib.error.URLError as e:
            if i < tries - 1:
                time.sleep(5); continue
            raise RuntimeError(f"{e} {url.split('?')[0]}")


# ---- GraphQL: zawartość wersji bez pobierania pliku (gdy licencja nie pozwala na /download) ----
TL_GQL = "https://transit.land/api/v2/query"
GQL_ROUTES = """
query($sha1: String!, $after: Int, $d1: Date, $d2: Date) {
  routes(after: $after, limit: 25, where: {feed_version_sha1: $sha1}) {
    id
    route_short_name
    route_long_name
    a: trips(limit: 2000, where: {service_date: $d1}) { stop_pattern_id }
    b: trips(limit: 2000, where: {service_date: $d2}) { stop_pattern_id }
    patterns {
      stop_pattern_id
      trips(limit: 1) {
        shape { geometry }
        stop_times { stop_sequence stop { stop_name stop_code geometry } }
      }
    }
  }
}"""
GQL_INTROSPECT = """
query { __schema { types { name fields { name args { name } } inputFields { name } } } }"""


def tl_gql(query, variables, key, tries=3):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for i in range(tries):
        req = urllib.request.Request(TL_GQL, data=body, headers={"apikey": key, "Content-Type": "application/json", "User-Agent": "przejazdy-archive"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                j = json.loads(r.read())
            if j.get("errors"):
                raise RuntimeError("GraphQL: " + "; ".join(e.get("message", "") for e in j["errors"])[:600])
            return j["data"]
        except urllib.error.HTTPError as e:
            txt = e.read()[:400].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise RuntimeError(f"HTTP {e.code} GraphQL :: {txt}")
        except urllib.error.URLError as e:
            if i < tries - 1:
                time.sleep(5); continue
            raise


def log_schema(key):
    """Przy błędzie zapytania – wypisz pola typów, żeby można było poprawić zapytanie."""
    try:
        d = tl_gql(GQL_INTROSPECT, {}, key)
        want = {"Route", "RouteStopPattern", "Trip", "StopTime", "Stop", "Shape", "TripFilter", "RouteFilter", "Query"}
        for t in d["__schema"]["types"]:
            if t["name"] in want:
                f = [x["name"] + ("(" + ",".join(a["name"] for a in x["args"]) + ")" if x.get("args") else "") for x in (t.get("fields") or [])]
                inf = [x["name"] for x in (t.get("inputFields") or [])]
                log(f"  [schemat] {t['name']}: {', '.join((f or inf)[:60])}")
    except Exception as e:
        log("  [schemat] nie udało się odczytać:", e)


def lonlat(g):
    c = (g or {}).get("coordinates") or []
    return (round(c[1], 5), round(c[0], 5)) if len(c) >= 2 else None


def patterns_via_gql(sha, d_from, key):
    """Ten sam wynik co extract_patterns(), ale z API zapytań Transitland."""
    d1 = date.fromisoformat(d_from)
    d2 = d1 + timedelta(days=(5 - d1.weekday()) % 7 or 7)      # najbliższa sobota → warianty weekendowe
    out, after = {}, None
    while True:
        data = tl_gql(GQL_ROUTES, {"sha1": sha, "after": after, "d1": d1.isoformat(), "d2": d2.isoformat()}, key)
        routes = data.get("routes") or []
        for r in routes:
            line = (r.get("route_short_name") or r.get("route_long_name") or str(r["id"])).strip()
            active = {t["stop_pattern_id"] for t in (r.get("a") or []) + (r.get("b") or [])}
            for p in (r.get("patterns") or []):
                if active and p["stop_pattern_id"] not in active:
                    continue
                if not active:
                    continue                                   # linia nie kursowała w próbkowanych dniach
                tr = (p.get("trips") or [None])[0]
                if not tr:
                    continue
                sts = sorted(tr.get("stop_times") or [], key=lambda x: x.get("stop_sequence") or 0)
                st = []
                for x in sts:
                    s0 = x.get("stop") or {}; ll = lonlat(s0.get("geometry"))
                    if ll:
                        st.append([(s0.get("stop_name") or "").strip(), post_of(s0.get("stop_code")), ll[0], ll[1]])
                if len(st) < 2:
                    continue
                g = ((tr.get("shape") or {}).get("geometry") or {}).get("coordinates") or []
                sh = enc_poly([(c[1], c[0]) for c in g if len(c) >= 2]) if g else ""
                k = hashlib.sha1(json.dumps([st, sh], ensure_ascii=False).encode()).hexdigest()[:16]
                out.setdefault(line, {})[k] = {"stops": st, "shape": sh}
        if len(routes) < 25:
            break
        after = routes[-1]["id"]
        time.sleep(0.5)
    return out


def tl_versions(feed, key):
    out, url = [], f"{TL}/feed_versions?feed_onestop_id={feed}&limit=100"
    while url:
        j = tl_get(url, key)
        out += j.get("feed_versions", [])
        nxt = (j.get("meta") or {}).get("next")
        url = nxt if nxt and nxt != url else None
        time.sleep(0.5)
    return out


def backfill(a):
    key = os.environ.get("TRANSITLAND_API_KEY", "").strip()
    if not key:
        log("BRAK klucza: ustaw sekret TRANSITLAND_API_KEY w repozytorium"); write_summary(); sys.exit(1)
    arc = load_archive(); meta = []
    since = date.fromisoformat(a.since); until = date.fromisoformat(a.until) if a.until else date.today()
    for feed, onestop in TL_FEEDS.items():
        log(f"\n=== {feed} ({onestop}) ===")
        try:
            vers = tl_versions(onestop, key)
        except Exception as e:
            log("  nie udało się pobrać listy wersji:", e); continue
        vs = []
        for v in vers:
            f = (v.get("fetched_at") or "")[:10]
            if f:
                vs.append((f, v))
        vs.sort(key=lambda x: x[0])
        if not vs:
            log("  brak wersji"); continue
        log(f"  wersji w Transitland: {len(vs)}; najstarsza pobrana {vs[0][0]}, najnowsza {vs[-1][0]}")
        log(f"  zakres kalendarza najstarszej: {vs[0][1].get('earliest_calendar_date')} – {vs[0][1].get('latest_calendar_date')}")
        # próbkowanie: pierwsza wersja w każdym oknie step-days, w zakresie since..until
        picked, next_from = [], since
        for f, v in vs:
            d = date.fromisoformat(f)
            if d < since - timedelta(days=a.step_days) or d > until:
                continue
            if d >= next_from or not picked:
                picked.append((f, v)); next_from = d + timedelta(days=a.step_days)
        # wersja obowiązująca na początku okresu (ostatnia przed since)
        before = [x for x in vs if x[0] < since.isoformat()]
        if before and (not picked or picked[0][0] > since.isoformat()):
            picked.insert(0, before[-1])
        log(f"  do pobrania (co ~{a.step_days} dni): {len(picked)} wersji")
        ok = 0; use_gql = a.gql
        for k, (f, v) in enumerate(picked):
            d_from = max(f, since.isoformat()) if k == 0 else f
            d_to = (date.fromisoformat(picked[k + 1][0]) - timedelta(days=1)).isoformat() if k + 1 < len(picked) else until.isoformat()
            sha = v.get("sha1")
            try:
                how = ""
                if not use_gql:
                    try:
                        zb = tl_get(f"{TL}/feed_versions/{sha}/download", key, binary=True)
                        if zb[:2] != b"PK":
                            raise RuntimeError("odpowiedź nie jest plikiem ZIP")
                        pats = extract_patterns(zb, feed, d_from, d_to); how = f"{len(zb)//1024} kB"
                    except RuntimeError as e:
                        if "401" in str(e) or "403" in str(e):
                            log("  pobieranie plików niedozwolone – przechodzę na API zapytań (GraphQL)")
                            use_gql = True
                        else:
                            raise
                if use_gql:
                    pats = patterns_via_gql(sha, d_from, key); how = "GraphQL"
                n = merge(arc, SYS[feed], pats, d_from, d_to)
                ok += 1
                log(f"  {f} {sha[:10]}  {how}  linii {len(pats)}  nowych wariantów {n}  ważność {d_from}…{d_to}")
                if ok % 10 == 0:
                    save_archive(arc, [])
                meta.append({"feed": feed, "sha1": sha, "fetched": f, "from": d_from, "to": d_to, "ok": True})
            except Exception as e:
                log(f"  {f} {sha[:10] if sha else '?'}  BŁĄD: {e}")
                meta.append({"feed": feed, "sha1": sha, "fetched": f, "ok": False, "error": str(e)[:200]})
                if ok == 0 and k == 0 and "GraphQL" in str(e):
                    log_schema(key)
                if ok == 0 and k >= 2:
                    log("  przerywam ten kanał – pierwsze pobrania się nie udają (prawdopodobnie brak zgody na pobieranie lub zły klucz)")
                    break
            time.sleep(1)
        if ok:
            save_archive(arc, [m for m in meta if m["feed"] == feed]); meta = [m for m in meta if m["feed"] != feed]
    save_archive(arc, meta)
    log("\nGotowe. Linie w archiwum:", sum(1 for _ in arc), "plików.")
    write_summary()


def update(a):
    arc = load_archive(); meta = []
    today = date.today().isoformat()
    for spec in a.zip:
        feed, path = spec.split("=", 1)
        try:
            zb = open(path, "rb").read()
            pats = extract_patterns(zb, feed, today, today)
        except Exception as e:
            log(f"{feed}: pomijam ({e})"); continue
        n = merge(arc, SYS.get(feed, feed), pats, today, today)
        log(f"{feed}: linii {len(pats)}, nowych wariantów {n}")
        meta.append({"feed": feed, "fetched": today, "from": today, "to": today, "ok": True, "src": "ztp"})
    save_archive(arc, meta)
    write_summary()


def local(a):
    arc = load_archive(); meta = []
    for spec in a.zip:
        feed, rest = spec.split("=", 1)
        path, d = rest.split("@")
        d_to = a.to or d
        pats = extract_patterns(open(path, "rb").read(), feed, d, d_to)
        n = merge(arc, SYS[feed], pats, d, d_to)
        log(f"{feed} {d}: linii {len(pats)}, nowych wariantów {n}")
        meta.append({"feed": feed, "fetched": d, "ok": True, "src": "local"})
    save_archive(arc, meta)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("backfill"); b.add_argument("--since", default="2024-07-01"); b.add_argument("--until", default="")
    b.add_argument("--step-days", type=int, default=7)
    b.add_argument("--gql", action="store_true", help="od razu przez API zapytań (bez prób pobierania plików)")
    u = sp.add_parser("update"); u.add_argument("--zip", action="append", default=[])
    l = sp.add_parser("local"); l.add_argument("--zip", action="append", default=[]); l.add_argument("--to", default="")
    a = ap.parse_args()
    {"backfill": backfill, "update": update, "local": local}[a.cmd](a)
