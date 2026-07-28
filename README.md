# mapfusion — OruxMaps Karten-Fusion


`mapfusion.py` verschmilzt beliebig viele OruxMaps-Offline-Karten (`OruxMapsImages.db` + `.otrk2.xml`) zu einer einzigen kombinierten Karte. Alle Kacheln (Map-Tiles) der einzelnen Quell-Karten werden in einer gemeinsamen Datenbank zusammengeführt — mit korrekter geografischer Positionierung und passender Kalibrierungsdatei.

Das Programm unterstützt **zwei Ausgabeformate**:

| Format | Dateien | Ziel-Anwendung |
| ------ | ------- | -------------- |
| **OruxMaps** | `OruxMapsImages.db` + `<Name>.otrk2.xml` | OruxMaps (Android) |
| **MBTiles** | `<Name>.mbtiles` + `<Name>.vrt` | QMapShack (Windows/Linux/OSX), QGIS, etc. |

---

## Voraussetzung: 

**Pillow** muss installiert sein (für die Aufspaltung von 512px-Tiles):

pip install Pillow


---

## Bedienung (GUI)

| Feld / Button              | Funktion                                                      |
| -------------------------- | ------------------------------------------------------------- |
| **Eingabeverzeichnis**     | Verzeichnis, in dem rekursiv nach OruxMaps-Karten gesucht wird. Beim Auswählen wird sofort gescannt und die gefundenen Karten in der Tabelle angezeigt. |
| **Ausgabeverzeichnis**     | Zielordner für die fusionierte Karte. Wird das Feld leer gelassen, wird standardmäßig ein Unterordner `<Kartenname>` im Eingabeverzeichnis erstellt. |
| **Kartenname**             | Name der fusionierten Karte. Wird beim Start der Fusion abgefragt, falls leer. Bestimmt den Dateinamen (`<Name>.otrk2.xml` bzw. `<Name>.mbtiles`). |
| **Ausgabeformat**          | Wahl zwischen **OruxMaps** (`.otrk2.xml` + `OruxMapsImages.db`) und **QMapShack / MBTiles** (`.mbtiles`). |
| **Gefundene Karten**       | Tabelle mit allen erkannten Quell-Karten: Name, Zoom-Level-Bereich, Tile-Anzahl. Eintrag selektieren (Klick; mit *Strg*/*Umschalt* für Mehrfachauswahl). Wenn **keine** Karte ausgewählt ist, werden alle gelisteten Karten fusioniert. |
| **Karten fusionieren**     | Startet die Fusion nach Sicherheitsabfrage.                   |
| **Fortschrittsbalken**     | Zeigt den prozentualen Fortschritt der Tile-Verarbeitung.     |
| **Protokoll**              | Farblich markiertes Log mit allen Meldungen und Fehlern.      |

---

## Ablauf der Fusion

```
┌──────────────────────┐
│ Eingabeverzeichnis   │
│                      │
│  Karte A/            │
│    OruxMapsImages.db │     ┌─────────────────────────┐
│    Karte A.otrk2.xml │     │   mapfusion.py          │
│                      │     │                         │
│  Karte B/            │ ──► │  1. Karten suchen       │
│    OruxMapsImages.db │     │  2. XML parsen          │
│    Karte B.otrk2.xml │     │  3. Tiles umrechnen     │
│  …                   │     │  4. Tiles zusammenführen│
│                      │     │  5. XML generieren      │
└──────────────────────┘     └───────────┬─────────────┘
                                         │
                                         ▼
                             ┌─────────────────────────┐
                             │ Ausgabeverzeichnis/     │
                             │   OruxMapsImages.db    │
                             │   <Name>.otrk2.xml     │
                             └─────────────────────────┘
```

### Verarbeitete Schritte

1. **Suchen:** Rekursives Durchsuchen des Eingabeverzeichnisses nach `OruxMapsImages.db`-Dateien. Das Ausgabeverzeichnis wird dabei automatisch ausgeschlossen, damit die gerade entstehende Karte nicht selbst als Quelle erkannt wird.

2. **XML parsen:** Pro gefundener Karte wird die `.otrk2.xml` eingelesen. Für jeden Zoom-Level (`layerLevel > 0`) werden extrahiert:
   - `MapBounds` (minLat / maxLat / minLon / maxLon)
   - `img_width` / `img_height` (Tile-Größe: 256 oder 512 px)

3. **Koordinaten-Transformation:** Jede Quell-Karte verwendet **lokale** Tile-Koordinaten ab (0, 0). Aus den `MapBounds` wird berechnet, bei welchem **globalen OSM-256-Tile** die lokale (0, 0) liegt:

   ```
   gx = global_x_offset + lx
   gy = global_y_offset + ly
   ```

   Dadurch landen alle Karten im einheitlichen globalen Web-Mercator-Raster und überlappen nur dort, wo sie geografisch deckungsgleich sind.

4. **512-Tile-Aufspaltung:** Quell-Karten mit 512px-Tiles werden automatisch mit Pillow in jeweils 4× 256px-Sub-Tiles zerlegt, damit alle Tiles das einheitliche OruxMaps-256er-Raster verwenden.

5. **Tiles schreiben:** Alle Tiles werden mit globalen Koordinaten in die Ziel-DB eingetragen (`INSERT OR REPLACE`). Bei geografischen Überlappungen gewinnt die zuletzt geschriebene Karte. Je nach gewähltem Ausgabeformat:
   - **OruxMaps:** `tiles`-Tabelle mit Spalten `(x, y, z, image)`, y wächst nach unten (OSM-Konvention).
   - **MBTiles:** `tiles`-Tabelle mit Spalten `(zoom_level, tile_column, tile_row, tile_data)`, y-Achse wird invertiert (`tms_y = 2^z − 1 − y`, TMS-Konvention für QMapShack).

6. **Kalibrierung/Metadaten generieren:**
   - **OruxMaps:** Die Ausgabe-`.otrk2.xml` erhält einen äußeren Container (`layers="true" layerLevel="0"`) mit dem Kartenname, sowie pro Zoom-Level einen `<MapCalibration>`-Block mit der **vereinigten Bounding-Box**.
   - **MBTiles:** Die `metadata`-Tabelle wird mit `name`, `format` (png), `bounds`, `minzoom`/`maxzoom`, `center`, `type=baselayer` und `version=1.1` gefüllt (MBTiles-Spezifikation 1.3).

---

## Ausgabe-Format

### OruxMapsImages.db

SQLite-Datenbank mit dem OruxMaps-Standard-Schema:

```sql
CREATE TABLE tiles (
  x     int,
  y     int,
  z     int,          -- Zoom-Level
  image blob,         -- PNG- oder JPEG-Bilddaten
  PRIMARY KEY (x, y, z)
);
CREATE INDEX IND ON tiles (x, y, z);

CREATE TABLE android_metadata (locale TEXT);   -- 'de_DE'
```

> Wichtig: Die gespeicherten `(x, y, z)` sind **lokale** Koordinaten — pro Zoom-Level auf `(0,0)` normalisiert, damit OruxMaps die Tiles entsprechend der XML `xMax`/`yMax` findet.

### `<Name>.otrk2.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<OruxTracker xmlns="http://oruxtracker.com/app/res/calibration" versionCode="3.0">
<MapCalibration layers="true" layerLevel="0">
  <MapName><![CDATA[MeineFusion]]></MapName>

  <!-- Pro Zoom-Level ein Block: -->
  <OruxTracker xmlns="..." versionCode="2.1">
  <MapCalibration layers="false" layerLevel="12">
    <MapName><![CDATA[MeineFusion 12]]></MapName>
    <MapChunks xMax="119" yMax="178" datum="WGS84" projection="Mercator"
               img_height="256" img_width="256" file_name="MeineFusion 12" />
    <MapDimensions height="45568" width="30464" />
    <MapBounds minLat="40.979898069620" maxLat="69.6716..."
               minLon="-9.14..." maxLon="15.46..." />
    <CalibrationPoints>
      <CalibrationPoint corner="TL" lon="..." lat="..." />
      <CalibrationPoint corner="BR" lon="..." lat="..." />
      <CalibrationPoint corner="TR" lon="..." lat="..." />
      <CalibrationPoint corner="BL" lon="..." lat="..." />
    </CalibrationPoints>
  </MapCalibration>
  </OruxTracker>

  … (weitere Zoom-Level) …

</MapCalibration>
</OruxTracker>
```

---

## Ausgabe-Format: MBTiles (QMapShack)

### `<Name>.mbtiles`

SQLite-Datenbank nach MBTiles-Spezifikation (1.3). QMapShack, QGIS und weitere Werkzeuge unterstützen dieses Format direkt.

```sql
CREATE TABLE tiles (
  zoom_level  integer,
  tile_column integer,    -- OSM-x (global)
  tile_row    integer,    -- TMS-y (invertiert: tms_y = 2^z − 1 − osm_y)
  tile_data   blob,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
);

CREATE TABLE metadata (
  name  text,
  value text
);
```

Metadaten-Schlüssel: `name`, `format` (png), `type` (baselayer), `version`, `bounds` (minLon,minLat,maxLon,maxLat), `minzoom`, `maxzoom`, `center`, `description`.

> Die MBTiles-Koordinaten sind **global** (TMS mit y-Flip) — dies ist der Standard für MBTiles/QMapShack.

### `<Name>.vrt`

Eine GDAL/QMapShack Virtual-Raster-Datei, die automatisch neben der `.mbtiles` erzeugt wird. Sie erlaubt QMapShack und GDAL, die MBTiles als georeferenziertes Raster mit Zoom-Stufen (Overviews) zu laden.

Aufbau:

| Element | Bedeutung |
| ------- | --------- |
| `rasterXSize` / `rasterYSize` | Pixelabmessungen bei `maxZoom` (volle Auflösung) |
| `SRS` | EPSG:3857 (WGS 84 / Pseudo-Mercator) |
| `GeoTransform` | Top-Left-Ecke `(minLon→m, maxLat→m)`, Pixelgröße = Erdumfang / (256 · 2^maxZoom) |
| `VRTRasterBand` ×4 | R/G/B/Alpha-Bänder, die auf die `.mbtiles` verweisen |
| `OverviewList` | `2, 4, 8, …, 2^(maxZoom−minZoom)` — Zoom-Stufen für QMapShack |

Beispiel:

```xml
<VRTDataset rasterXSize="2097152" rasterYSize="2097152">
  <SRS dataAxisToSRSAxisMapping="1,2">PROJCS["WGS 84 / Pseudo-Mercator", …, AUTHORITY["EPSG","3857"]]</SRS>
  <GeoTransform>  0.0,  4.777e+00,  0.0,  1.002e+07,  0.0, -4.777e+00</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1">
    <ColorInterp>Red</ColorInterp>
    <ComplexSource>
      <SourceFilename relativeToVRT="1">MeineKarte.mbtiles</SourceFilename>
      …
    </ComplexSource>
  </VRTRasterBand>
  …
  <OverviewList resampling="nearest">2 4 8 16 32 64 128 256 512 1024 2048 4096 8192</OverviewList>
</VRTDataset>
```

---

## Übernahme in Anwendungen

### OruxMaps (Android)

1. Den gesamten Ausgabeordner (mit `OruxMapsImages.db` und `<Name>.otrk2.xml`) auf das Android-Gerät kopieren — z. B. nach `OruxMaps/mapfiles/<Name>/`.
2. In OruxMaps: **Manager → Offline-Karten → Neue Karte hinzufügen → OruxMaps**, dann die `.otrk2.xml` auswählen.
3. Die fusionierte Karte erscheint mit dem vergebenen Kartennamen und allen Zoom-Leveln.

### QMapShack (Windows/Linux/OSX)

1. Den Ausgabeordner (mit `<Name>.mbtiles` und `<Name>.vrt`) auf den PC kopieren.
2. In QMapShack: **Karte → Kartenliste → GDAL/Kachel-Datei hinzufügen**, dann die `.vrt`-Datei auswählen.
3. QMapShack lädt die MBTiles über die VRT-Referenz und bietet alle Zoom-Stufen als Overviews an.

---

## Technische Details

### Web-Mercator-Transformationen

| Funktion         | Formel                                               |
| ---------------- | ---------------------------------------------------- |
| `lon_to_tile_x`  | $(lon + 180°) / 360° \times 2^z$                     |
| `lat_to_tile_y`  | $\frac{1 - \mathrm{asinh}(\tan(lat_{rad})) / \pi}{2} \times 2^z$ |
| `tile_x_to_lon`  | $x / 2^z \times 360° - 180°$                        |
| `tile_y_to_lat`  | $\mathrm{deg}(\mathrm{atan}(\mathrm{sinh}(\pi - 2\pi y / 2^z)))$ |

Dabei ist $z$ das Zoom-Level und die Tile-Koordinaten sind im OSM/Google-Raster (y wächst nach unten).

### Modul-Funktionen (für Programmierer)

| Funktion                  | Beschreibung                                           |
| ------------------------- | ------------------------------------------------------ |
| `parse_otrk2_xml(path)`   | Liest `.otrk2.xml`, gibt Liste der Zoom-Level-Dicts zurück. |
| `discover_maps(dir, log)` | Sucht rekursiv nach OruxMaps-Karten, gibt `MapSource`-Objekte zurück. |
| `merge_tiles_to_db(...)`  | Hauptfunktion: liest Quell-Tiles, rechnet um, schreibt Ziel-DB. |
| `write_merged_otrk2_xml(...)` | Generiert die kombinierte `.otrk2.xml`.           |
| `MapSource`               | Datenklasse für eine einzelne Quell-Karte.             |

---

## Fehlerbehebung

| Problem                                  | Lösung                                                        |
| ---------------------------------------- | ------------------------------------------------------------- |
| **"Pillow wird benötigt"**               | `pip install Pillow` ausführen.                               |
| **Keine Karten gefunden**                | Prüfen, ob der Ordner `OruxMapsImages.db` **und** `.otrk2.xml` enthält. |
| **"database is locked"**                 | Tritt nicht mehr auf — das Ausgabeverzeichnis wird automatisch vom Scan ausgeschlossen. |
| **512-Tiles erscheinen verzerrt**        | Pillow muss installiert sein, sonst können 512px-Karten nicht verarbeitet werden. |
| **Geografische Überlappung**             | Bei überlappenden Karten gewinnt die zuletzt verarbeitete Quelle. Karten in der gewünschten Priorität-Reihenfolge ins Eingabeverzeichnis geben oder nachträglich sortieren. |
