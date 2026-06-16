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
