# MediaIndexer

Ein selbstheilendes **Telegram Media Wiki System**. Es läuft als **Userbot**
über die Telegram Client API (MTProto), liest deine eigenen privaten Threads als
Event‑Stream und baut daraus ein persistentes Wiki für **Filme, Serien und
Anime** – inklusive Episoden, Releases und Updates über die Zeit.

> Telegram ist nur die Oberfläche. Die **Datenbank ist die einzige Wahrheit**.
> Inhalte werden ausschließlich inkrementell aktualisiert, niemals komplett neu
> geschrieben.

---

## Wichtige Hinweise vorab

- **Userbot / ToS:** Das System steuert einen echten Telegram‑Account über die
  Client API. Automatisierung eines persönlichen Accounts bewegt sich in einer
  Grauzone der Telegram‑Nutzungsbedingungen. Nutze es bewusst und für deine
  eigenen, privaten Daten. Die `TG_SESSION` ist ein vollwertiges Login –
  behandle sie wie ein Passwort.
- **Datenbank‑Treiber:** `motor` ist seit Mitte 2026 abgekündigt. Dieses Projekt
  nutzt deshalb die offizielle **async API von PyMongo** (`AsyncMongoClient`).
- **Erkennungsgenauigkeit:** Titel‑/Episodenerkennung und das Matching gegen die
  externen Datenbanken sind heuristisch. Sie funktionieren ohne Tuning gut für
  saubere Dateinamen, sollten aber an deinen realen Thread‑Daten nachjustiert
  werden (siehe *Tuning*). Es findet **kein implizites Lernen** statt – alles
  läuft über explizite Tabellen und Confidence‑Werte.

---

## Voraussetzungen

- Docker und Docker Compose
- Telegram **API ID** und **API HASH** von <https://my.telegram.org> →
  *API development tools*
- Optional, aber empfohlen: ein **TMDb API Key** (<https://www.themoviedb.org>)
  und ein **OMDb API Key** (<https://www.omdbapi.com>). Anime nutzt standardmäßig
  Jikan (MyAnimeList) ohne Key.

---

## Schnellstart

```bash
# 1. Repository entpacken und hineinwechseln
cd mediaindexer

# 2. Session-String einmalig erzeugen (interaktiv, Login-Code nötig)
#    Lokal mit installierten Abhängigkeiten:
pip install -r requirements.txt
python scripts/generate_session.py
#    -> den ausgegebenen String als TG_SESSION notieren

# 3. Konfiguration anlegen
cp .env.example .env
#    .env öffnen und ausfüllen (TG_API_ID, TG_API_HASH, TG_SESSION,
#    OWNER_ID, SOURCE_CHAT_IDS, TARGET_CHAT_ID, API-Keys ...)

# 4. Starten
docker compose up -d --build

# 5. Logs ansehen
docker compose logs -f bot
```

Die numerischen Chat‑IDs (z. B. `-1001234567890`) bekommst du am einfachsten,
indem du die Gruppe kurz in einem Telegram‑Client mit aktivierter Entwickler‑/
Debug‑Ansicht öffnest, oder über einen ID‑Bot. `OWNER_ID` ist die User‑ID deines
eigenen Accounts.

---

## Befehle

Befehle werden von deinem eigenen Account (der `OWNER_ID`) mit dem Präfix `.`
geschrieben – auch im Quell‑Chat oder in den *Gespeicherten Nachrichten*.

| Befehl | Wirkung |
| --- | --- |
| `.import <chat> [from_msg_id]` | Liest einen Quell‑Chat nachträglich ein. Mit `from_msg_id` nur Nachrichten **nach** dieser ID. Live‑Status, hängende Einträge werden automatisch übersprungen und der Import läuft weiter. |
| `.status` | Zeigt Zähler (Medien, Episoden, Pending, Posts) und Queue‑Größen. |
| `.repair` | Startet sofort einen Self‑Healing‑Durchlauf. |
| `.rebuild <titel>` | Erzwingt Neu‑Auflösung der Metadaten und Neu‑Rendern einer Media (Suche per Titel). |
| `.help` | Hilfe anzeigen. |

`<chat>` kann eine numerische ID, ein `@username` oder `here` (aktueller Chat)
sein.

---

## Wie es funktioniert

Jede Nachricht ist ein Event und durchläuft strikt eine Pipeline über drei
Queues – direkte Verarbeitung gibt es nicht:

```
Telegram → ingest_queue → processing_queue → update_queue → Telegram‑Posts
              (persist)      (erkennen,          (Karte rendern,
                              Kontext, Auflösen)   Posts syncen)
```

**Erkennung (deterministisch, max. 3 Zeilen pro Nachricht):**

- `Dateiname` ist **ausschließlich** `document/video.file_name` aus der Telegram
  API – niemals aus Text, Caption oder OCR abgeleitet.
- Titel‑Priorität: `file_name` > `caption` > `message_text` > Thread‑Kontext.
- `#hashtags` sind immer Tags/Metadaten, niemals Titel.
- **Thread‑Kontext:** Sobald ein Titel erkannt wurde, ist er der aktive Kontext.
  Folgen danach reine Episoden („Episode 1“, „E02“ …), werden sie zwingend an
  das zuletzt erkannte Medium gebunden. Ein Medienwechsel im Thread wird erkannt
  und sauber getrennt.

**Externe Quellen (priorisiert):**

- Anime: MyAnimeList (Jikan) → AniList → Kitsu → TMDb → OMDb
- Film/Serie: TMDb → OMDb

**Persistenz & Dedup:** Aktive Threads, aktuelle Medien, Episoden‑Kontexte, die
letzten Events und alle UI‑Posts liegen in MongoDB. Gleiche `media_id` wird
gemerged, gleiche Episode aktualisiert, gleiche Release zusammengeführt.

**UI – Media Cards:** Jede Karte folgt exakt einem festen Template (Titel, Jahr,
Beschreibung als Quote, Genres, Bewertung, Erstveröffentlichung, Laufzeit,
Episoden, Releases, Quellen). Episoden skalieren automatisch: bis 20 vollständig,
bis 100 in Blöcken, bis 1000 gruppiert, darüber nur Übersicht. Pro Media gibt es
genau **einen Root‑Post**; wird es zu groß, entstehen verknüpfte Overflow‑Posts,
die **immer den Titel** tragen. Eine Post‑State‑Machine verwaltet
`CREATED / UPDATED / SPLIT / MERGED / ARCHIVED`; unveränderte Posts werden per
Content‑Hash übersprungen.

**Self‑Healing:** Periodisch (und per `.repair`) werden Pending‑Events erneut
durch die Pipeline geschickt (mit aktualisiertem Kontext), Medien ohne
Metadaten erneut aufgelöst und „dirty“ Karten neu gerendert. Events, die zu oft
scheitern, werden nach `PENDING_MAX_ATTEMPTS` verworfen.

---

## Projektstruktur

```
mediaindexer/
├── docker-compose.yml      Bot- + MongoDB-Service
├── Dockerfile
├── requirements.txt
├── .env.example
├── scripts/
│   └── generate_session.py StringSession erzeugen
└── app/
    ├── main.py             Entrypoint: Config, DB, Client, Worker, Healer
    ├── config.py           Konfiguration aus Umgebungsvariablen
    ├── util.py             Hash, Slug, 3-Zeilen-Regel, IDs
    ├── telegram/           Client, Parser, Handler, Befehle
    ├── pipeline/           Queues + Worker (Ingest/Processing/Update)
    ├── detection/          Patterns, Extractor, Episoden, Classifier, Kontext
    ├── providers/          TMDb, OMDb, Jikan, AniList, Kitsu, Registry
    ├── storage/            MongoDB-Anbindung, Modelle, Repositories
    ├── ui/                 Templates, Card-Builder, Post-Manager
    └── healing/            Self-Healing
```

---

## Tuning

Die „Intelligenz“ ist bewusst explizit und damit anpassbar:

- **Pattern‑Tabellen:** `app/detection/patterns.py` (Release‑Tokens, Episoden‑
  Regex, Anime‑Marker).
- **Schwellenwerte:** in der `.env` (`TITLE_MATCH_THRESHOLD`,
  `PROVIDER_MATCH_THRESHOLD`, `CLASSIFY_MIN_CONFIDENCE`).
- **Alias‑Tabelle:** die `patterns`‑Collection in MongoDB (`type: alias`) bildet
  beliebige Schreibweisen auf eine `media_id` ab – ideal, um Fehlzuordnungen
  dauerhaft zu korrigieren.

Nach Änderungen ggf. `.repair` ausführen, damit Pending‑Events neu bewertet
werden.

---

## Betrieb

```bash
docker compose up -d --build     # starten / aktualisieren
docker compose logs -f bot       # Logs
docker compose restart bot       # neu starten (State bleibt in MongoDB)
docker compose down              # stoppen (Daten bleiben im Volume mongo_data)
```

Der gesamte Zustand liegt im benannten Volume `mongo_data`; ein Neustart des
Bots verliert nichts und nimmt unterbrochene Arbeit automatisch wieder auf.

## Hinweise & Troubleshooting (Stand dieser Version)

**Telethon ≥ 1.43 ist Pflicht.** Zuklappbare (expandable) Blockquotes — für die
Beschreibung und die Episodenlisten — funktionieren erst ab Telethon 1.43.0;
ältere Versionen ignorieren das `expandable`-Attribut und zeigen ein normales,
nicht zugeklapptes Zitat. Nach einem Update unbedingt das Image neu bauen:
`docker compose build --no-cache && docker compose up -d`.

**Flood-Schutz (`FloodWaitError`).** Ein Userbot, der im Schwung viele Posts in
ein Thema schreibt, löst Telegrams Spam-Schutz aus (Wartezeiten von teils
>1000 s). Gegenmaßnahmen sind eingebaut: alle sendenden/edierenden Aufrufe sind
global gedrosselt (`SEND_MIN_INTERVAL`, Standard 3 s) und kurze Floods verschluckt
Telethon automatisch (`FLOOD_SLEEP_THRESHOLD`); größere werden einmal ausgesessen
und wiederholt (gedeckelt durch `FLOOD_WAIT_MAX`). Wer schneller posten will,
senkt `SEND_MIN_INTERVAL` — auf eigenes Risiko.

**Quell- und Provider-Verlinkung.** Jede Episode/jedes Release wird per
`t.me/c/…`-Deeplink auf den Quellpost verlinkt; die Datenquelle (TMDb/IMDb/MAL/
AniList/Kitsu) wird im Footer verlinkt. Deeplinks funktionieren nur in
Supergruppen/Kanälen (ID mit `-100`-Präfix), nicht in einfachen Gruppen.

**Große Serien.** Bis `EPISODES_LINK_LIMIT` (Standard 600) Episoden ist jede
Folge einzeln anklickbar (in zuklappbaren Staffel-Blöcken; sehr große Staffeln
werden auf mehrere Blöcke aufgeteilt). Darüber wird pro Staffel eine Zeile mit
verlinkter erster Folge gezeigt.

## Titelerkennung bei dateilosen Ankündigungen (Serien)

In vielen Gruppen wird zuerst ein Bild-Post mit Titel veröffentlicht und erst
danach die Episoden, deren Dateinamen oft nur noch `S01E01-finale` o. ä.
enthalten. Damit dabei genau **ein** korrekter Eintrag entsteht:

- Der Serientitel wird aus dem Dateinamen ausschließlich aus dem Text **vor** dem
  Episodenmarker gelesen; alles dahinter (`finale`, `Ozymandias`, `the end`) ist
  der Episodentitel und wird für die Serienidentifikation ignoriert. `S01E01-finale`
  ist damit „nur Episode" und bindet an den Thread-Kontext statt einen Junk-Eintrag
  anzulegen.
- Findet die Provider-Suche zum Dateititel nichts, wird **als letzter Ausweg** der
  Titel des vorausgehenden Bild-Posts herangezogen (maximal die ersten 2 Zeilen,
  jede Zeile als eigener Kandidat — nie konkateniert). Schlägt auch das fehl, wird
  der Eintrag aus den Detection-Daten erstellt.
- Sobald ein Treffer erzielt wurde, werden alle folgenden Episoden ohne eigenen
  Treffer dieser gefundenen Serie zugeordnet.
- Mit `POST_ONLY_IF_RESOLVED=true` wird nichts in den Zielthread gepostet, solange
  kein Provider-Treffer vorliegt; der Eintrag erscheint automatisch, sobald der
  Healer die Metadaten auflöst.

### Erkannte Episoden-Formate

Damit Episoden ohne Serientitel nicht als eigenständige Einträge landen, werden
u. a. folgende Marker erkannt (Text davor = Serientitel, Text danach =
Episodentitel und wird ignoriert):

`S01E01`, `S1.E1`, `S09 E01`, `S1-05`, `1x05`, `09x01`, `9X01`,
`Season 1 Episode 5`, `Staffel 1 Folge 5`, `E16`, `E016`, `Ep16`, `EP16`,
`Episode 16`, `Folge 16`, `Capitulo 16`, `#16`, Anime `- 05`, sowie reine
führende Episodennummern im Listenformat `16 - Titel` / `16. Titel`.

Steht keiner dieser Marker im Namen und auch kein Serientitel, bleibt die Datei
„unaufgelöst" (pending) statt einen Fehleintrag zu erzeugen. Fehlt ein Format,
das bei dir vorkommt, lässt es sich in `app/detection/patterns.py` ergänzen.

## Post-Text-gestützte Erkennung & Fehlervermeidung

Der Telegram-Post-Text, mit dem ein Video geteilt wird, fließt jetzt in die
Erkennung ein – nicht nur der Dateiname:

- **Trailer:** Enthält der Post-Text das Wort „trailer", wird das Video komplett
  ignoriert (kein Eintrag, kein Post, keine Quelle).
- **Episodenmarker im Post-Text:** Heißt die Datei nur wie der Episodentitel
  (`Endlich Frieden`) und der Post-Text liefert den Marker (`E19 - Endlich
  Frieden`), wird das als Episode erkannt und an die Serie des vorausgehenden
  Bild-Posts gebunden – statt einen Einzeleintrag zu erzeugen. Der Serientitel
  ist immer der Text **vor** dem Marker der markertragenden Quelle.
- **Jahr aus dem Post-Text:** Fehlt im Dateinamen das Jahr (`chinatown`), wird es
  aus dem Post-Text (`chinatown (1974)`) übernommen.
- **Junk-Präfixe:** Downloader-/Seiten-Präfixe wie `Y2Mate`, `vıvo Watch` werden
  vom Titel entfernt (echte Titel wie „Watch Dogs"/„The Ting" bleiben erhalten).
- **Teil-Marker:** `TitelT01`, `t02`, `PeleT03` werden als Episoden/Teile erkannt.
- **Jahr ≠ Episode:** Eine 4-stellige Jahreszahl (z. B. `- 1992`) wird nicht mehr
  als Episodennummer (`S1E1992`) interpretiert; Filme bleiben Filme.

Mehrere Episoden derselben Serie (`Spartacus Blood and Sand S02E03`,
`Britannia - S02E09 - …`) landen dadurch in **einem** Eintrag statt je Episode in
einem eigenen.

## Darstellung: zugeklappte Blöcke & korrektes Zeichenlimit

- **Alle Episoden** stehen jetzt in zugeklappten (expandable) Staffel-Blöcken,
  egal wie viele – die Staffelüberschrift steht **im Block** über den Folgen, und
  ein ganzer Block landet nie aufgeteilt auf zwei Posts.
- **Unaufgelöste Einträge** (ohne Provider-Treffer) zeigen ihre Film-/Episoden-
  Links ebenfalls in zugeklappten Zitaten.
- **Zeichenlimit korrekt gezählt:** Telegram rechnet nur den *sichtbaren* Text auf
  sein 4096-Limit an – die langen `t.me`-Deeplink-URLs zählen nicht mit. Vorher
  wurde die rohe HTML-Länge gezählt, daher der vorzeitige Umbruch bei ~48
  Episoden. Jetzt wird die sichtbare Länge gemessen, sodass deutlich mehr Folgen
  pro Post passen (z. B. 100 Folgen in einem Block).

## Anime-Quellthread

`ANIME_SOURCE_THREAD_IDS` (kommagetrennte Topic-IDs): Für Dateien aus diesen
Threads versucht der Resolver zuerst die Anime-Provider (Jikan/AniList/Kitsu) und
erst danach TMDb/OMDb – unabhängig davon, wie der Dateiname aussieht.

## Episoden kompakt, Versionen & unaufgelöste Einträge

- **Kompakt:** Episoden stehen nebeneinander als `E01 E02 E03 …` (umbrechend) im
  zugeklappten Staffel-Block, nicht mehr eine pro Zeile – das spart Posts.
- **Mehrere Versionen:** Wird eine Episode später in besserer Qualität gepostet,
  verlinkt `E01` auf die beste Version und jede weitere erscheint als eigener
  Tag dahinter, z. B. `E01[720p]` – beide Quellposts bleiben erreichbar.
  (Identische Dateien werden nicht doppelt geführt; nur echte andere Versionen.)
- **Filme:** Die Zeile `📦 Releases: N` steht jetzt mitsamt den verlinkten
  Filmversionen INNERHALB des zugeklappten Zitats.
- **Ohne Provider-Treffer:** Die Zusammenfassungszeile „🎞️ Episoden: N in M
  Staffel(n)" wird bei unaufgelösten Einträgen weggelassen (die Staffel-Blöcke
  bleiben).

## Sehr große Serien: Telegram-Entity-Limit

Telegram erlaubt nur ~100 Formatierungs-**Entities** (jeder Link, jedes Fett,
jedes Blockquote = 1 Entity) pro Nachricht und stellt alles darüber als
**Klartext** dar – bei sehr großen Serien kippten so ab einem Punkt die
Episoden-Links und der Footer (Quellen/TMDb) in unformatierten Text.

Der Renderer begrenzt jetzt die Entity-Zahl **pro Staffel-Block** und **pro Post**
(`TG_MAX_ENTITIES`, Standard 90). Ein Post nimmt nur so viele Staffel-Blöcke auf,
wie unter dem Limit bleiben; der Rest wandert in den nächsten Zweigpost. Dadurch
bleibt das gewünschte Format (zugeklappte Zitate, verlinkte Episoden, verlinkter
Footer) auch bei 200+ Episoden vollständig erhalten – es entstehen lediglich ein
paar Posts mehr. Mehrfachversions-Tags stehen mit Leerzeichen (`E01 [720p]`),
damit sie getrennt antippbar sind.

## Weitere Robustheit & Befehle (dieser Stand)

**Episoden-Links (Anime/Serien):** Episoden bekommen jetzt auch dann einen
anklickbaren Link, wenn die Quelldatei **ohne Dateinamen** gepostet wurde (häufig
bei Anime-Videos). Vorher entstand mangels Dateiname kein Release und die Folge
wurde als reiner, nicht klickbarer Text dargestellt.

**Footer:** Bei genau einer Quelle steht unten „🔗 Quelle" (Singular) mit dem
Link direkt im Wort statt „Quellen: 1".

**Führende Episodennummern:** zusätzlich erkannt werden `04.01` (= Staffel 4,
Folge 1) und nullgepolsterte Nummern wie `044` (= Folge 44). Reine Filmtitel wie
`300` oder `1917` bleiben unberührt.

**Sequenz-Gruppierung:** Aufeinanderfolgende **unaufgelöste** Einträge, deren
Titel nach Entfernen einer führenden/abschließenden Nummer denselben Stamm haben
(`ida rogalski 1` … `ida rogalski 10`), werden als fortlaufende Episoden EINES
Eintrags gebunden statt einzeln gepostet. Aufgelöste Reihen (echte Sequels) bleiben
getrennt.

**Anime-Titelsuche:** strengere Schwelle (`ANIME_MATCH_THRESHOLD`, Standard 88).
Schwache Anime-Teiltreffer werden **verworfen** (kein falscher Titel/Poster) — der
Eintrag bleibt lieber unaufgelöst, als falsch zu sein.

**Loop-/Hänger-Fallback:** Wird dieselbe Media in kurzer Zeit zu oft neu
verarbeitet (`UPDATE_LOOP_MAX`) oder schlägt zu oft fehl (`UPDATE_FAIL_MAX`),
wandert sie in Quarantäne und wird übersprungen; jeder Sync hat ein hartes
Timeout (`UPDATE_SYNC_TIMEOUT`). Das bricht den in der Praxis beobachteten
Post→Lösch→Post-Loop.

### Neue Owner-Befehle
- `.reindex` — rendert **alle** Einträge mit den aktuellen Anzeige-Regeln neu
  (Entity-Limits, Footer, Links). Arbeitet auf den gespeicherten Daten; führt
  keine erneute Erkennung der Altdateien durch. Für erneutes Auflösen
  Unaufgelöster zusätzlich `.repair`.
- `.prune` — prüft pro Eintrag die Quell-Links gegen Telegram und entfernt tote
  Releases/Quellen; Einträge ohne überlebende Quelle werden samt Zielpost
  gelöscht. Sicher: was nicht eindeutig als gelöscht bestätigt ist (Fehler/keine
  ID), bleibt erhalten.

## Hörbücher (deutscher Fokus, TMDb-artiger Buch-Indexer)

MediaIndexer erkennt und katalogisiert jetzt auch **Hörbücher** — nahtlos in
derselben Datenbank-/Provider-Struktur wie Filme/Serien/Anime.

**Erkennung:** automatisch über Audio-Endungen (`.mp3`, `.m4a`, `.m4b`, `.flac`,
…) oder deutsche Schlüsselwörter (Hörbuch, Hörspiel, ungekürzt, „gelesen von",
…) — im Dateinamen, mit Rückfall auf den Telegram-Post-Text wie bei Filmen.
Mehrteilige Hörbücher (Teil 1, Teil 2, CD1 …) werden als Dateien EINES Eintrags
zusammengefasst (wie Film-Releases), nicht als TV-Episoden.

**Provider-Kette (für Hörbücher):** Audnexus (primär — sucht jetzt auch per
**Titel/Dateiname** über den regionalen Audible-Katalog, nicht nur per ASIN:
Katalog-Suche → ASIN → reiche Audnexus-Metadaten, mit Fallback auf die
Katalogdaten) → Google Books → Deutsche Nationalbibliothek (DNB, SRU/Dublin Core,
deutscher Fokus) → Open Library. Da Hörbuch-Dateinamen oft „Autor - Titel"
enthalten, gleicht die Auflösung zusätzlich gegen „Autor + Titel" ab. Kein
API-Key nötig (Google Books optional über `GOOGLE_BOOKS_API_KEY`). Schwache
Treffer unter `AUDIOBOOK_MATCH_THRESHOLD` bleiben unaufgelöst statt falsch.
Metadaten werden lokal gecacht; Duplikate über denselben canonical-key-Mechanismus
vermieden. Hörbücher werden **wie Filme** dargestellt: Cover als Foto, Beschreibung
im aufklappbaren Zitat, Datei-Links im Zitat, plus Autor/Sprecher und ein
Provider-Link (Audible/DNB/…).

### Eigene Threads steuern (ENV)
- `AUDIOBOOK_SOURCE_THREAD_IDS` — Dateien aus diesen Topics werden als Hörbücher
  behandelt (eigene Provider-Suche), analog zu `ANIME_SOURCE_THREAD_IDS`.
- `IGNORE_THREAD_IDS` — Topics, die **komplett ignoriert** werden: daraus
  entstehen keine Einträge.

### Nachträgliche Korrektur
- `.repair` löst unaufgelöste Hörbücher gegen die Buch-Provider neu auf (nutzt
  den gespeicherten Medientyp; Autor/Sprecher werden ergänzt).
- `.reindex` rendert alle Einträge mit aktuellen Regeln neu **und entfernt
  Einträge, deren Quellen inzwischen alle in einem ignorierten Thread liegen**.
- `.prune` entfernt tote Quell-Links auch bei Hörbüchern.

## Titel aus Post-Text, Archive & Trailer-Synonyme (dieser Stand)

**Post-Text als Titelquelle:** Dateiname und der Telegram-Post-Text, mit dem die
Datei gepostet wurde, sind nicht immer gleich. Der Indexer sammelt jetzt **alle**
Titel-Kandidaten (Dateiname, Caption, Post-Text) und probiert sie der Reihe nach
bei den Providern — ein kryptischer Dateiname wie `tmsf-eternalyou (2024)` wird
so über den echten Titel im Post-Text (`eternal you – vom ende der endlichkeit
2024`) aufgelöst. Bleibt ein Eintrag unaufgelöst, wird der aussagekräftigste
Titel angezeigt (mehrwortiger Post-Text-Titel statt Dateiname-Kürzel). Alle
Kandidaten werden als `search_aliases` gespeichert, damit **`.repair`** sie
nachträglich erneut probieren kann (auch aus dem Post-Text, nicht nur dem
gespeicherten Titel).

**Archive ignorieren:** `IGNORE_ARCHIVE_FILES=true` (Standard) überspringt
Archiv-Uploads (`.rar`, `.zip`, `.7z`, mehrteilige `.r00`/`.7z.001`/`.001` …), die
sonst Müll-Einträge erzeugen. `.reindex` entfernt zusätzlich bestehende Einträge,
deren Dateien ausschließlich Archive sind.

**Trailer/Preview erkennen:** Neben „trailer" werden jetzt auch Teaser, Preview,
Vorschau, Promo, Sample, Snippet und Ausschnitt im Post-Text als Trailer erkannt
und ignoriert. Über `TRAILER_KEYWORDS` (komma-separiert) lassen sich weitere
Begriffe ergänzen.

## Stilisierte Posts, weitere Episodenformate & bare Zahlen (dieser Stand)

**Unicode-Normalisierung (NFKC):** Stilisierte Telegram-Posts mit mathematischer
Fettschrift/Kursive und Modifier-Buchstaben (z. B. `𝗦𝗲𝗮𝘀𝗼𝗻 𝟮 ᴴᴰ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲 𝟱`)
werden vor der Erkennung auf normales ASCII gefaltet (`Season 2 HD Episode 5`).
Dadurch werden Marker **und** Serientitel erkannt — das Peaky-Blinders-Beispiel
ergibt jetzt korrekt `Peaky Blinders` S02E05/E06 und beide Folgen landen in
**einem** Eintrag statt je einem eigenen. Überflüssige Trennzeichen am Titelrand
(`Peaky Blinders |`) werden entfernt.

**Mehr Episodenformate erkannt:** `1a/1b/2a` (Buchstaben-Teil), `10.1/01.2`
(Episode mit Unter-Teil, eine Stelle nach dem Punkt — abgegrenzt von der
`S04E01`-Form mit zwei Stellen), `S1F1/S01F05` (deutsche Staffel/Folge-Kurzform),
`bd1/ed2/op1/sp3/ova2` (Disc/Opening/Ending/Special), `01_Titel`, `1. Titel`. Alle
binden als Episode an die Serie im Thread, statt einzelne Einträge zu erzeugen.

**Bare Zahlen als Episoden:** Dateinamen, die nur aus einer Zahl bestehen
(`01`, `100`), werden als **Episodennummer** behandelt statt als Titel gesucht
(eine bloße Zahl liefert sonst falsche Provider-Treffer und je einen Müll-Eintrag).
Vierstellige Jahreszahlen (`1917`, `2024`) bleiben Titel.

**Hörbücher:** Die Zeile „Erstveröffentlichung" wird bei Hörbüchern nicht mehr
angezeigt.

### Nachträgliche Korrektur
- `.reindex` rendert alle Einträge neu — entfernt damit u. a. die
  „Erstveröffentlichung"-Zeile bei bestehenden Hörbüchern.
- **`.tidy`** (Alias `.dropbad`) löscht unaufgelöste Einträge, deren „Titel" in
  Wahrheit nur ein Episoden-Marker ist (`1a`, `100`, `S1F1`, `bd2`, `10.1` …) und
  die höchstens wenige Releases haben — die Altlasten falscher Einzel-Einträge.
  Echte Serien bleiben unangetastet.
