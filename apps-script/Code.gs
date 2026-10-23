/**
 * PRZEJAZDY – backend Google Apps Script
 * ---------------------------------------
 * 1) zapis / odczyt / edycja przejazdów w arkuszu Google (PRZEJAZDY)
 * 2) pośrednik (proxy) do plików GTFS-RT ZTP Kraków – przeglądarka na iPhonie
 *    nie zawsze może pobrać je bezpośrednio (CORS), a Apps Script może.
 *
 * Wdrożenie: patrz README.md, sekcja „Krok 1”.
 */

// ====== KONFIGURACJA ======================================================
const SHEET_NAME = 'ZIKiT Kraków';          // nazwa karty z przejazdami
const APP_KEY    = 'ZMIEN-MNIE-na-dlugi-losowy-klucz'; // ten sam klucz wpiszesz w aplikacji
const RT_BASE    = 'https://gtfs.ztp.krakow.pl/';
const ALLOWED_FEEDS = [
  'VehiclePositions_A', 'VehiclePositions_T', 'VehiclePositions_M',
  'TripUpdates_A', 'TripUpdates_T', 'TripUpdates_M',
  'ServiceAlerts_A', 'ServiceAlerts_T', 'ServiceAlerts_M'
];
// Kolumny arkusza (1 = A). K jest nowa – trzyma identyfikator przejazdu z aplikacji.
const COL = { date: 1, dep: 2, from: 3, line: 4, brigade: 5, vehicle: 6, to: 7, arr: 8, dur: 9, notes: 10, id: 11 };
const NCOLS = 11;
// ==========================================================================

function doGet(e) {
  const p = (e && e.parameter) || {};
  try {
    if (!authOk_(p.key)) return out_({ ok: false, error: 'auth' });
    switch (p.action) {
      case 'ping': return out_({ ok: true, sheet: SHEET_NAME, now: new Date().toISOString() });
      case 'rt':   return out_(rt_(p.feeds));
      case 'list': return out_(list_());
      default:     return out_({ ok: false, error: 'unknown action' });
    }
  } catch (err) {
    return out_({ ok: false, error: String(err && err.message || err) });
  }
}

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || '{}');
    if (!authOk_(body.key)) return out_({ ok: false, error: 'auth' });
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      switch (body.action) {
        case 'add':    return out_(add_(body.ride));
        case 'update': return out_(update_(body.ride));
        case 'delete': return out_(delete_(body.ride));
        default:       return out_({ ok: false, error: 'unknown action' });
      }
    } finally { lock.releaseLock(); }
  } catch (err) {
    return out_({ ok: false, error: String(err && err.message || err) });
  }
}

// ---------- GTFS-RT proxy ----------
function rt_(feedsParam) {
  const feeds = String(feedsParam || '').split(',').filter(f => ALLOWED_FEEDS.indexOf(f) >= 0);
  if (!feeds.length) return { ok: false, error: 'no feeds' };
  const reqs = feeds.map(f => ({ url: RT_BASE + f + '.pb', muteHttpExceptions: true }));
  const res = UrlFetchApp.fetchAll(reqs);
  const data = {};
  res.forEach((r, i) => {
    data[feeds[i]] = r.getResponseCode() === 200 ? Utilities.base64Encode(r.getContent()) : null;
  });
  return { ok: true, fetched: Date.now(), feeds: data };
}

// ---------- arkusz ----------
function sheet_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sh) throw new Error('Brak karty: ' + SHEET_NAME);
  if (!sh.getRange(1, COL.id).getValue()) sh.getRange(1, COL.id).setValue('ID_APLIKACJI');
  return sh;
}

/** Ostatni wiersz z wpisaną datą w kolumnie A (kolumny C/I mają formuły aż do końca). */
function lastDataRow_(sh) {
  const n = sh.getMaxRows();
  const vals = sh.getRange(1, COL.date, n, 1).getValues();
  for (let i = n - 1; i >= 1; i--) if (vals[i][0] !== '' && vals[i][0] !== null) return i + 1;
  return 1;
}

function list_() {
  const sh = sheet_();
  const tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone();
  const last = lastDataRow_(sh);
  if (last < 2) return { ok: true, rides: [] };
  const rng = sh.getRange(2, 1, last - 1, NCOLS);
  const v = rng.getValues();
  const d = rng.getDisplayValues();   // czasy bierzemy jako tekst (unikamy przesunięć strefy dla 1899 r.)
  const rides = [];
  for (let i = 0; i < v.length; i++) {
    const r = v[i];
    if (r[COL.date - 1] === '' || r[COL.date - 1] === null) continue;
    const dt = r[COL.date - 1] instanceof Date
      ? Utilities.formatDate(r[COL.date - 1], tz, 'yyyy-MM-dd')
      : String(d[i][COL.date - 1]);
    rides.push({
      row: i + 2,
      id: String(r[COL.id - 1] || ''),
      date: dt,
      dep: normTime_(d[i][COL.dep - 1]),
      from: String(d[i][COL.from - 1] || ''),
      line: String(d[i][COL.line - 1] || ''),
      brigade: String(d[i][COL.brigade - 1] || ''),
      vehicle: String(d[i][COL.vehicle - 1] || ''),
      to: String(d[i][COL.to - 1] || ''),
      arr: normTime_(d[i][COL.arr - 1]),
      notes: String(d[i][COL.notes - 1] || '')
    });
  }
  return { ok: true, rides: rides };
}

function normTime_(s) {
  const m = String(s || '').match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return '';
  return ('0' + m[1]).slice(-2) + ':' + m[2] + ':' + (m[3] || '00');
}
function timeToSerial_(t) {
  const m = String(t || '').match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return '';
  return (Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3] || 0)) / 86400;
}
function dateToSerial_(s) {
  const m = String(s || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return '';
  return (Date.UTC(+m[1], +m[2] - 1, +m[3]) - Date.UTC(1899, 11, 30)) / 86400000;
}
function numOrText_(x) {
  const s = String(x == null ? '' : x).trim();
  return /^\d+$/.test(s) ? Number(s) : s;
}

function writeRow_(sh, row, ride) {
  const vals = [[
    dateToSerial_(ride.date), timeToSerial_(ride.dep), ride.from || '', numOrText_(ride.line),
    numOrText_(ride.brigade), ride.vehicle || '', ride.to || '', timeToSerial_(ride.arr)
  ]];
  sh.getRange(row, 1, 1, 8).setValues(vals);
  sh.getRange(row, COL.dur).setFormula('=IF(OR(B' + row + '="",H' + row + '=""),"",IF(H' + row + '<B' + row + ',H' + row + '+1-B' + row + ',H' + row + '-B' + row + '))');
  sh.getRange(row, COL.notes).setValue(ride.notes || '');
  sh.getRange(row, COL.id).setValue(ride.id || '');
  sh.getRange(row, COL.date).setNumberFormat('yyyy-mm-dd');
  sh.getRange(row, COL.dep).setNumberFormat('hh:mm:ss');
  sh.getRange(row, COL.arr).setNumberFormat('hh:mm:ss');
  sh.getRange(row, COL.dur).setNumberFormat('[h]:mm:ss');
}

function findRowById_(sh, id) {
  if (!id) return 0;
  const last = lastDataRow_(sh);
  if (last < 2) return 0;
  const ids = sh.getRange(2, COL.id, last - 1, 1).getValues();
  for (let i = 0; i < ids.length; i++) if (String(ids[i][0]) === String(id)) return i + 2;
  return 0;
}

function add_(ride) {
  const sh = sheet_();
  const existing = findRowById_(sh, ride.id);       // ponowna wysyłka tego samego przejazdu = aktualizacja
  const row = existing || lastDataRow_(sh) + 1;
  if (row > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), 50);
  writeRow_(sh, row, ride);
  return { ok: true, row: row, id: ride.id };
}

/** Przejazdy historyczne (bez ID) identyfikujemy numerem wiersza + kontrolą daty i godziny. */
function locate_(sh, ride) {
  let row = findRowById_(sh, ride.id);
  if (row) return row;
  if (ride.row && ride.orig) {
    const tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone();
    const r = sh.getRange(ride.row, 1, 1, 2);
    const v = r.getValues()[0], d = r.getDisplayValues()[0];
    const dt = v[0] instanceof Date ? Utilities.formatDate(v[0], tz, 'yyyy-MM-dd') : String(d[0]);
    if (dt === ride.orig.date && normTime_(d[1]) === ride.orig.dep) return ride.row;
  }
  return 0;
}

function update_(ride) {
  const sh = sheet_();
  const row = locate_(sh, ride);
  if (!row) return { ok: false, error: 'not_found' };
  writeRow_(sh, row, ride);
  return { ok: true, row: row, id: ride.id };
}

function delete_(ride) {
  const sh = sheet_();
  const row = locate_(sh, ride);
  if (!row) return { ok: false, error: 'not_found' };
  // następny wiersz może mieć w C formułę =G<usuwany> – zamieniamy ją na wartość, by nie powstał #REF!
  const nextC = sh.getRange(row + 1, COL.from);
  if (nextC.getFormula()) nextC.setValue(nextC.getValue());
  sh.deleteRow(row);
  return { ok: true, row: row };
}

function authOk_(key) {
  const k = PropertiesService.getScriptProperties().getProperty('APP_KEY') || APP_KEY;
  return key && String(key) === k;
}
function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Uruchom raz ręcznie w edytorze, aby nadać uprawnienia i sprawdzić dostęp do arkusza oraz ZTP. */
function test() {
  Logger.log(JSON.stringify(list_()).slice(0, 500));
  const r = rt_('VehiclePositions_T');
  Logger.log('VehiclePositions_T: ' + (r.feeds.VehiclePositions_T ? r.feeds.VehiclePositions_T.length + ' znaków base64' : 'BŁĄD'));
}
