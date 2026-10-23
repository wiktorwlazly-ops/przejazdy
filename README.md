# Przejazdy – dziennik przejazdów MPK Kraków (PWA na iPhone'a)

Aplikacja przeglądarkowa (instalowana na ekranie początkowym iOS), która zapisuje przejazdy do arkusza
**PRZEJAZDY** na Dysku Google, w układzie kolumn karty „ZIKiT Kraków”:

| A | B | C | D | E | F | G | H | I | J | K (nowa) |
|---|---|---|---|---|---|---|---|---|---|---|
| DATA | GODZINA ODJAZDU | PRZYSTANEK POCZĄTKOWY | LINIA | BRYGADA (TRAMWAJE) | NUMER TABOROWY | PRZYSTANEK KOŃCOWY | GODZINA PRZYJAZDU | CZAS PODRÓŻY | UWAGI | ID_APLIKACJI |

## Jak to działa

```
iPhone (Safari / ekran początkowy)
  index.html  ── GPS ──► przystanki w buforze 20 m (data/stops.json)
     │
     ├─► GTFS-RT ZTP (VehiclePositions / TripUpdates) ──► linia, kierunek, nr taborowy, brygada
     │      bezpośrednio z gtfs.ztp.krakow.pl, a jeśli przeglądarka zablokuje (CORS) – przez Apps Script
     │
     ├─► awaryjnie: rozkład statyczny (data/dep-RRRRMMDD.json) albo wpis ręczny
     │
     └─► Google Apps Script (Code.gs) ──► arkusz PRZEJAZDY (dopisywanie, edycja, usuwanie, odczyt historii)
GitHub Actions co noc: GTFS_KRK_A/T/M.zip ──► data/*.json
```

* **Start** – czas odjazdu zapisuje się w chwili dotknięcia (hh:mm:ss). Aplikacja ustala pozycję,
  podaje przystanki w buforze 20 m, a dla pojazdów ZTP w promieniu 60 m (albo stojących na tym przystanku)
  pokazuje podpowiedzi linii. Jedno dotknięcie podpowiedzi uzupełnia linię, brygadę i numer taborowy.
* **Koniec** – czas przyjazdu, przystanek końcowy z GPS (bufor 20 m) oraz dodatkowo przystanek,
  na którym według GTFS-RT stoi Twój pojazd.
* Jeśli GTFS-RT nie odpowiada, aplikacja bierze odjazdy z rozkładu (±10 min); każde pole można wpisać ręcznie.
* **Historia** – wszystkie przejazdy z arkusza (także te sprzed aplikacji), wyszukiwarka, edycja i usuwanie.
* **Statystyki** – liczba przejazdów i czas w drodze, podział tramwaj/autobus, linie, pojazdy, przystanki,
  relacje, miesiące, pory dnia i dni tygodnia; można wybrać okres.
* Bez zasięgu przejazd zapisuje się w telefonie i trafia do arkusza przy najbliższym połączeniu.

---

## Krok 0 – arkusz Google

Apps Script nie działa z plikiem .xlsx. Otwórz PRZEJAZDY.xlsx na Dysku → **Plik → Zapisz jako Arkusz Google**
i od teraz używaj tej kopii. Karta musi nazywać się `ZIKiT Kraków`; inną nazwę ustawisz w `Code.gs` (`SHEET_NAME`).

## Krok 1 – Apps Script (zapis do arkusza + pośrednik GTFS-RT)

1. W arkuszu wybierz **Rozszerzenia → Apps Script**.
2. Wklej zawartość `apps-script/Code.gs` (zastąp domyślny kod).
3. Zmień `APP_KEY` na długi losowy ciąg znaków, np. 30 znaków. Ten klucz chroni Twój arkusz.
4. (opcjonalnie) **Ustawienia projektu → Pokaż plik manifestu „appsscript.json”** i wklej `apps-script/appsscript.json`
   (strefa czasowa Europe/Warsaw i minimalne uprawnienia).
5. Wybierz funkcję `test` → **Uruchom** → zezwól na dostęp. W dzienniku powinny pojawić się wiersze z arkusza
   i rozmiar `VehiclePositions_T`.
6. **Wdróż → Nowe wdrożenie → Aplikacja internetowa**: *Wykonaj jako: Ja*, *Kto ma dostęp: Każdy*.
   Skopiuj adres kończący się na `/exec`.
   Po zmianie kodu: **Wdróż → Zarządzaj wdrożeniami → edytuj → Wersja: nowa**, a adres zostanie ten sam.

## Krok 2 – hosting aplikacji (GitHub Pages, za darmo)

1. Załóż **publiczne** repozytorium, np. `przejazdy`, i wgraj do niego całą zawartość tego folderu
   (razem z `.github/workflows/gtfs.yml`).
2. **Settings → Actions → General → Workflow permissions → Read and write permissions** i zapisz.
3. **Actions → „Aktualizacja GTFS (statyczny)” → Run workflow**. Po ok. 1–2 min w repo pojawi się folder `data/`.
   Dalej odświeża się co noc.
4. **Settings → Pages → Deploy from a branch → main / (root)**. Aplikacja będzie pod
   `https://<twoj-login>.github.io/przejazdy/`.

> GitHub Pages jest wymagane, bo iPhone udostępnia GPS tylko stronom po HTTPS. Działa też dowolny inny hosting HTTPS.

## Krok 3 – iPhone

1. Otwórz adres z kroku 2 w **Safari** → **Udostępnij → Do ekranu początkowego**.
   W tej formie iOS nie kasuje danych aplikacji.
2. Uruchom z ikony, przejdź do **Ustawienia**, wklej adres `/exec` i klucz, potem **Testuj połączenie**.
3. **Pobierz z arkusza** wczytuje całą dotychczasową historię (do statystyk i edycji).
4. Przy pierwszym starcie zezwól na lokalizację („Podczas używania aplikacji”, z włączoną dokładną lokalizacją).

## Krok 4 – sprawdzenie brygady i numeru taborowego (ważne)

Nie miałem możliwości podejrzeć bieżących plików GTFS-RT ZTP. Dekodowanie jest więc zgodne ze specyfikacją
GTFS-Realtime, ale **pochodzenie brygady i numeru taborowego trzeba potwierdzić na pierwszym przejeździe**:

* stojąc przy pojeździe, otwórz **Ustawienia → Diagnostyka GTFS-RT w pobliżu**. Zobaczysz surowe pola
  `vehicle.id / label / license_plate`, `trip_id`, `route_id`, numer bloku i wyliczoną brygadę;
* **Numer taborowy** – domyślnie `label`, potem `license_plate`, potem `id`. Jeśli numer boczny (np. HY864)
  jest w innym polu, wybierz je w ustawieniach;
* **Brygada** – domyślnie bierzemy numer bloku (`block_id` z trips.txt albo `block_NNN` z trip_id). Gdy zaczyna się
  od numeru linii, np. blok 506 na linii 50, wynikiem jest brygada 6. Jeśli ZTP numeruje inaczej, wybierz
  „Numer bloku bez zmian” albo podaj własne wyrażenie regularne na trip_id (grupa 1 = brygada).
  Domyślnie brygada zapisuje się tylko dla tramwajów, tak jak w nagłówku kolumny E (można to wyłączyć).

## Numer słupka (np. „Rondo Mogilskie 05”)

Każdy słupek ma w GTFS osobny rekord z własnymi współrzędnymi, więc przystanek zapisuje się jako *nazwa + numer słupka*.
Słupek ustalam na dwa sposoby:
1. **z GPS** – najbliższy słupek w buforze; obok każdej podpowiedzi widać odległość;
2. **z GTFS-RT** – gdy wybierzesz pojazd, który według ZTP stoi na przystanku, jego `stop_id` wskazuje
   dokładny słupek i ma pierwszeństwo przed GPS. Na końcu przejazdu działa to tak samo.

Numer słupka pochodzi domyślnie z pola `stop_code`. W **Diagnostyce** widać `stop_code` i `stop_id` najbliższych słupków
oraz to, co trafi do arkusza. Jeśli ZTP koduje słupek inaczej, wybierz w ustawieniach „ostatnie 2 cyfry stop_code”,
wyrażenie regularne na `stop_id` albo zapis bez słupka. Statystyki przystanków mogą łączyć słupki jednego przystanku
z dawnymi wpisami bez numeru (przełącznik w ustawieniach).

## Uwagi

* W kolumnie **K** (`ID_APLIKACJI`) aplikacja zapisuje identyfikator przejazdu, potrzebny do późniejszej edycji.
  Dawne wiersze nie mają ID, więc rozpoznaję je po numerze wiersza oraz kontroli daty i godziny. Przy pierwszej
  edycji takiego wiersza ID zostaje nadane.
* Nowe wiersze mają przystanek początkowy wpisany jako wartość, a nie formułę `=G(poprzedni)`.
  Czas podróży (I) to formuła, która uwzględnia przejazd przez północ. Godziny mają format `hh:mm:ss`.
* Przy usuwaniu wiersza formuła `=G…` w kolejnym wierszu zamienia się na wartość, żeby nie pojawił się `#REF!`.
* Bufor 20 m i promień szukania pojazdu zmienisz w ustawieniach. GPS iPhone'a w centrum miasta potrafi mieć
  błąd 10–30 m. Gdy w buforze nie ma przystanku, aplikacja pokaże najbliższe przystanki z oznaczeniem „poza buforem”
  oraz przystanek końcowy poprzedniego przejazdu.
* Kanał **M** ZTP jest domyślnie wyłączony; włączysz go w ustawieniach.

## Pliki

```
index.html              aplikacja (cały interfejs i logika)
sw.js, manifest.webmanifest, icons/   PWA (praca offline, ikona)
apps-script/Code.gs     backend do arkusza + proxy GTFS-RT
apps-script/appsscript.json
tools/build_static.py   budowa data/*.json ze statycznego GTFS
.github/workflows/gtfs.yml   nocna aktualizacja danych
```
