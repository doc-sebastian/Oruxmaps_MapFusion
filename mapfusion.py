#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mapfusion.py - Verschmilzt mehrere OruxMaps Offline-Karten (OruxMapsImages.db +
.otrk2.xml) zu einer einzigen kombinierten Karte.

Funktionsweise:
  - Rekursives Suchen nach OruxMapsImages.db + zugehöriger .otrk2.xml
  - Tiles werden aus lokalen DB-Koordinaten in globale Web-Mercator-Koordinaten
    (OSM-Tile-Schema, TMS-y-Flip) umgerechnet, damit Karten mit unterschiedlichem
    geografischem Ursprung korrekt fusioniert werden.
  - Die kombinierte DB enthält alle Tiles in globalen Koordinaten.
  - Die .otrk2.xml wird neu generiert: pro Zoom-Level ein <MapCalibration>-Block
    über die vereinigte Ausdehnung aller Quell-Karten.

Autor: Sebastian Fischer
Datum: 2026-07-27
"""

import os
import io
import math
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# --------------------------------------------------------------------------------------
#  Globale Konstanten
# --------------------------------------------------------------------------------------
TILE_SIZE = 256  # OruxMaps arbeitet intern mit 256x256 px Tiles

# Ausgabeformate
FORMAT_ORUX = "orux"      # OruxMaps (OruxMapsImages.db + .otrk2.xml)
FORMAT_MBTILES = "mbtiles"  # QMapShack-kompatibel (.mbtiles + .vrt)

# Nachrichtentypen für Logging
LOG_INFO = "INFO"
LOG_WARN = "WARNUNG"
LOG_ERROR = "FEHLER"
LOG_OK = "OK"


# --------------------------------------------------------------------------------------
#  Math: Web-Mercator Umrechnungen (OSM / Google Tiling Scheme)
# --------------------------------------------------------------------------------------
def lon_to_tile_x(lon, zoom):
    """Longitude zu globaler OSM-Tile-X-Koordinate (float)."""
    return (lon + 180.0) / 360.0 * (2 ** zoom)


def lat_to_tile_y(lat, zoom):
    """Latitude zu globaler OSM-Tile-Y-Koordinate (float, y wächst nach unten)."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    return (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (2 ** zoom)


def tile_x_to_lon(x, zoom):
    """OSM-Tile-X-Koordinate zu Longitude (linke Kante)."""
    return x / (2 ** zoom) * 360.0 - 180.0


def tile_y_to_lat(y, zoom):
    """OSM-Tile-Y-Koordinate zu Latitude (obere Kante)."""
    n = math.pi - 2.0 * math.pi * y / (2 ** zoom)
    return math.degrees(math.atan(math.sinh(n)))


# --------------------------------------------------------------------------------------
#  Hilfsfunktion: DB-Schema sicherstellen
# --------------------------------------------------------------------------------------
def ensure_tiles_schema(conn):
    """Stellt sicher, dass die DB das korrekte OruxMaps-Tiles-Schema hat."""
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS tiles (
        x int, y int, z int, image blob,
        PRIMARY KEY (x, y, z)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS android_metadata (
        locale TEXT DEFAULT 'de_DE'
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS IND ON tiles (x, y, z)")
    # android_metadata befüllen falls leer
    cur.execute("SELECT COUNT(*) FROM android_metadata")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO android_metadata (locale) VALUES ('de_DE')")
    conn.commit()


def ensure_mbtiles_schema(conn):
    """Stellt das MBTiles-Tiles-Schema für QMapShack her."""
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS tiles (
        zoom_level  integer,
        tile_column integer,
        tile_row    integer,
        tile_data   blob,
        PRIMARY KEY (zoom_level, tile_column, tile_row)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS metadata (
        name  text,
        value text
    )""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
                "ON tiles (zoom_level, tile_column, tile_row)")
    conn.commit()


def write_mbtiles_metadata(conn, map_name, zoom_bounds):
    """
    Schreibt die MBTiles-Metadaten-Tabelle.
    zoom_bounds: Liste von Dicts mit 'zoom', 'minLat', 'maxLat', 'minLon', 'maxLon'
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM metadata")
    meta = []
    meta.append(("name", map_name))
    meta.append(("format", "png"))
    meta.append(("type", "baselayer"))
    meta.append(("version", "1.1"))
    meta.append(("description", "Erzeugt mit mapfusion.py"))
    min_z = min(zl['zoom'] for zl in zoom_bounds)
    max_z = max(zl['zoom'] for zl in zoom_bounds)
    meta.append(("minzoom", str(min_z)))
    meta.append(("maxzoom", str(max_z)))
    # vereinigte Bounding-Box über alle Zoom-Level
    min_lat = min(zl['minLat'] for zl in zoom_bounds)
    max_lat = max(zl['maxLat'] for zl in zoom_bounds)
    min_lon = min(zl['minLon'] for zl in zoom_bounds)
    max_lon = max(zl['maxLon'] for zl in zoom_bounds)
    meta.append(("bounds", "%.6f,%.6f,%.6f,%.6f" % (min_lon, min_lat, max_lon, max_lat)))
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0
    meta.append(("center", "%.6f,%.6f,%d" % (center_lon, center_lat, min_z)))
    cur.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", meta)
    conn.commit()


def write_qmapshack_vrt(vrt_path, mbtiles_filename, map_name, zoom_bounds):
    """
    Schreibt eine QMapShack-kompatible .vrt-Datei für eine MBTiles-Karte.

    Die VRT beschreibt das Raster bei maximaler Auflösung (maxZoom) und
    enthält <OverviewList> für die kleineren Zoom-Stufen, die QMapShack/GDAL
    aus der MBTiles nutzen.

    vrt_path:        Vollständiger Pfad der zu schreibenden .vrt-Datei.
    mbtiles_filename: Dateiname (ohne Pfad) der .mbtiles im gleichen Verzeichnis.
    map_name:        Kartenname.
    zoom_bounds:     Liste von Dicts mit 'zoom','minLat','maxLat','minLon','maxLon'.
    """
    R = 6378137.0  # WGS84-Äquatorradius (Web Mercator)
    WORLD_M = R * math.pi * 2  # Erdumfang in Metern

    min_z = min(zl['zoom'] for zl in zoom_bounds)
    max_z = max(zl['zoom'] for zl in zoom_bounds)

    min_lat = min(zl['minLat'] for zl in zoom_bounds)
    max_lat = max(zl['maxLat'] for zl in zoom_bounds)
    min_lon = min(zl['minLon'] for zl in zoom_bounds)
    max_lon = max(zl['maxLon'] for zl in zoom_bounds)

    # Web-Mercator-Meterkoordinaten der Bounding-Box
    def lon_to_mx(lon):
        return R * lon * math.pi / 180.0

    def lat_to_my(lat):
        lat = max(min(lat, 85.05112878), -85.05112878)
        return R * math.asinh(math.tan(math.radians(lat)))

    x_min = lon_to_mx(min_lon)
    x_max = lon_to_mx(max_lon)
    y_min = lat_to_my(min_lat)  # südlich → kleinerer y-Wert
    y_max = lat_to_my(max_lat)  # nördlich → größerer y-Wert

    # Pixelgröße bei maxZoom (Meter pro Pixel)
    m_per_px = WORLD_M / (TILE_SIZE * (2 ** max_z))

    # Rasterabmessungen bei maxZoom (volle Pixelauflösung)
    raster_x = int(round((x_max - x_min) / m_per_px))
    raster_y = int(round((y_max - y_min) / m_per_px))

    # GeoTransform: top-left = (x_min, y_max), Pixel = ±m_per_px (eine Zeile)
    gt_x = x_min
    gt_y = y_max
    gt = "  %.16e,  %.16e,  %.16e,  %.16e,  %.16e,  %.16e" % (
        gt_x, m_per_px, 0.0, gt_y, 0.0, -m_per_px)

    # OverviewList: 2, 4, 8, ... 2^(maxZoom-minZoom)
    n_overviews = max_z - min_z
    overviews = " ".join(str(2 ** i) for i in range(1, n_overviews + 1))

    # SRS: EPSG:3857 (WGS 84 / Pseudo-Mercator)
    srs = ('PROJCS["WGS 84 / Pseudo-Mercator",'
           'GEOGCS["WGS 84",'
           'DATUM["WGS_1984",'
           'SPHEROID["WGS 84",6378137,298.257223563,'
           'AUTHORITY["EPSG","7030"]],'
           'AUTHORITY["EPSG","6326"]],'
           'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
           'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
           'AUTHORITY["EPSG","4326"]],'
           'PROJECTION["Mercator_1SP"],'
           'PARAMETER["central_meridian",0],'
           'PARAMETER["scale_factor",1],'
           'PARAMETER["false_easting",0],'
           'PARAMETER["false_northing",0],'
           'UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
           'AXIS["Easting",EAST],'
           'AXIS["Northing",NORTH],'
           'EXTENSION["PROJ4",'
           '"+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 '
           '+x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs"],'
           'AUTHORITY["EPSG","3857"]]')

    bands = ['Red', 'Green', 'Blue', 'Alpha']
    xml = []
    xml.append('<VRTDataset rasterXSize="%d" rasterYSize="%d">' % (raster_x, raster_y))
    xml.append('  <SRS dataAxisToSRSAxisMapping="1,2">%s</SRS>' % srs)
    xml.append('  <GeoTransform>%s</GeoTransform>' % gt)
    for i, color in enumerate(bands, start=1):
        xml.append('  <VRTRasterBand dataType="Byte" band="%d">' % i)
        xml.append('    <ColorInterp>%s</ColorInterp>' % color)
        xml.append('    <ComplexSource>')
        xml.append('      <SourceFilename relativeToVRT="1">%s</SourceFilename>' % mbtiles_filename)
        xml.append('      <SourceBand>%d</SourceBand>' % i)
        xml.append('      <SourceProperties RasterXSize="%d" RasterYSize="%d" DataType="Byte" '
                   'BlockXSize="%d" BlockYSize="%d" />'
                   % (raster_x, raster_y, TILE_SIZE, TILE_SIZE))
        xml.append('      <SrcRect xOff="0" yOff="0" xSize="%d" ySize="%d" />'
                   % (raster_x, raster_y))
        xml.append('      <DstRect xOff="0" yOff="0" xSize="%d" ySize="%d" />'
                   % (raster_x, raster_y))
        xml.append('      <UseMaskBand>true</UseMaskBand>')
        xml.append('    </ComplexSource>')
        xml.append('  </VRTRasterBand>')
    xml.append('  <OverviewList resampling="nearest">%s</OverviewList>' % overviews)
    xml.append('</VRTDataset>')

    with open(vrt_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(xml) + '\n')


# --------------------------------------------------------------------------------------
#  XML-Parser: .otrk2.xml einlesen
# --------------------------------------------------------------------------------------
def parse_otrk2_xml(xml_path):
    """
    Liest eine OruxMaps .otrk2.xml Datei und extrahiert pro Zoom-Level:
      - MapBounds (minLat, maxLat, minLon, maxLon)
      - Tile-Größe (img_height / img_width)
      - Layer-Level (layerLevel)

    Gibt eine Liste von Dicts zurück:
      [{'name': str, 'zoom': int, 'minLat': float, 'maxLat': float,
        'minLon': float, 'maxLon': float, 'tile_size': int}]
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(xml_path)
    root = tree.getroot()

    results = []

    def _local(tag):
        """Entfernt XML-Namespace-Prefix."""
        return tag.split('}')[-1] if '}' in tag else tag

    # Alle MapCalibration-Elemente finden (rekursiv)
    for mc in root.iter():
        if _local(mc.tag) != 'MapCalibration':
            continue
        layer_level_str = mc.get('layerLevel', '0')
        try:
            layer_level = int(layer_level_str)
        except ValueError:
            continue
        if layer_level == 0:
            continue  # Äußerer Container, kein echtes Zoom-Level

        entry = {'zoom': layer_level, 'tile_size': 256}

        for child in mc:
            cl = _local(child.tag)
            if cl == 'MapName':
                entry['name'] = child.text or ''
            elif cl == 'MapChunks':
                iw = child.get('img_width', '256')
                entry['tile_size'] = int(iw)
            elif cl == 'MapBounds':
                entry['minLat'] = float(child.get('minLat'))
                entry['maxLat'] = float(child.get('maxLat'))
                entry['minLon'] = float(child.get('minLon'))
                entry['maxLon'] = float(child.get('maxLon'))

        if 'minLat' in entry:
            results.append(entry)

    return results


# --------------------------------------------------------------------------------------
#  XML-Generator: kombinierte .otrk2.xml schreiben
# --------------------------------------------------------------------------------------
def write_merged_otrk2_xml(xml_path, map_name, zoom_levels):
    """
    Schreibt eine neue .otrk2.xml mit der äußeren Container-Struktur und einem
    <MapCalibration>-Block pro Zoom-Level.

    zoom_levels: sortierte Liste von Dicts:
      {'zoom': int, 'minLat': float, 'maxLat': float,
       'minLon': float, 'maxLon': float}
    """
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<OruxTracker xmlns="http://oruxtracker.com/app/res/calibration"')
    lines.append(' versionCode="3.0">')
    lines.append('<MapCalibration layers="true" layerLevel="0">')
    lines.append('<MapName><![CDATA[%s]]></MapName>' % map_name)

    for zl in sorted(zoom_levels, key=lambda d: d['zoom']):
        z = zl['zoom']
        min_lat = zl['minLat']
        max_lat = zl['maxLat']
        min_lon = zl['minLon']
        max_lon = zl['maxLon']

        # Berechne globale Tile-Positionen
        x_min_f = lon_to_tile_x(min_lon, z)
        x_max_f = lon_to_tile_x(max_lon, z)
        y_min_f = lat_to_tile_y(max_lat, z)  # oben → kleines y
        y_max_f = lat_to_tile_y(min_lat, z)  # unten → großes y

        x_tiles = max(1, int(math.ceil(x_max_f)) - int(math.floor(x_min_f)))
        y_tiles = max(1, int(math.ceil(y_max_f)) - int(math.floor(y_min_f)))

        width_px = x_tiles * TILE_SIZE
        height_px = y_tiles * TILE_SIZE

        layer_name = "%s %02d" % (map_name, z)

        lines.append('<OruxTracker xmlns="http://oruxtracker.com/app/res/calibration"')
        lines.append(' versionCode="2.1">')
        lines.append('<MapCalibration layers="false" layerLevel="%d">' % z)
        lines.append('<MapName><![CDATA[%s]]></MapName>' % layer_name)
        lines.append('<MapChunks xMax="%d" yMax="%d" datum="WGS84" projection="Mercator" '
                     'img_height="%d" img_width="%d" file_name="%s" />'
                     % (x_tiles, y_tiles, TILE_SIZE, TILE_SIZE, layer_name))
        lines.append('<MapDimensions height="%d" width="%d" />' % (height_px, width_px))
        lines.append('<MapBounds minLat="%.12f" maxLat="%.12f" '
                     'minLon="%.12f" maxLon="%.12f" />'
                     % (min_lat, max_lat, min_lon, max_lon))
        lines.append('<CalibrationPoints>')
        lines.append('<CalibrationPoint corner="TL" lon="%.6f" lat="%.6f" />'
                     % (min_lon, max_lat))
        lines.append('<CalibrationPoint corner="BR" lon="%.6f" lat="%.6f" />'
                     % (max_lon, min_lat))
        lines.append('<CalibrationPoint corner="TR" lon="%.6f" lat="%.6f" />'
                     % (max_lon, max_lat))
        lines.append('<CalibrationPoint corner="BL" lon="%.6f" lat="%.6f" />'
                     % (min_lon, min_lat))
        lines.append('</CalibrationPoints>')
        lines.append('</MapCalibration>')
        lines.append('</OruxTracker>')

    lines.append('</MapCalibration>')
    lines.append('</OruxTracker>')

    with open(xml_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


# --------------------------------------------------------------------------------------
#  Daten-Klassen
# --------------------------------------------------------------------------------------
class MapSource:
    """Repräsentiert eine einzelne Quell-Karte (DB + XML-Metadaten)."""
    def __init__(self, db_path, xml_data, name):
        self.db_path = db_path
        self.xml_data = xml_data
        self.name = name
        # Lookup: zoom → Level-Dict
        self.levels = {lv['zoom']: lv for lv in xml_data}

    def __repr__(self):
        return "MapSource('%s', %d levels)" % (self.name, len(self.levels))


# --------------------------------------------------------------------------------------
#  Karten-Suche
# --------------------------------------------------------------------------------------
def discover_maps(root_dir, log_fn, exclude_dirs=None):
    """
    Sucht rekursiv nach OruxMapsImages.db-Dateien und versucht, die zugehörige
    .otrk2.xml zu finden (im selben Verzeichnis).
    Gibt eine Liste von MapSource-Objekten zurück.

    exclude_dirs: Liste von Verzeichnispfaden, die vom Scan ausgeschlossen
    werden sollen (z.B. das Ausgabeverzeichnis, falls es im Eingabeverzeichnis
    liegt). Kostenlos: mit os.path.realpath normalisiert.
    """
    if exclude_dirs is None:
        exclude_dirs = []
    exclude_real = set()
    for d in exclude_dirs:
        try:
            exclude_real.add(os.path.realpath(d).lower())
            exclude_real.add(os.path.normpath(d).lower())
        except Exception:
            pass

    sources = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # exclude_dirs: nicht in diese Ordner hineinrekurrieren
        # (dirnames in-place modifizieren, damit os.walk dort nicht hinabsteigt)
        pruned = []
        for dn in list(dirnames):
            full = os.path.join(dirpath, dn)
            try:
                fl = os.path.realpath(full).lower()
            except Exception:
                fl = full.lower()
            if fl in exclude_real or full.lower() in exclude_real:
                pruned.append(dn)
                dirnames.remove(dn)
        for dn in pruned:
            log_fn(LOG_INFO, "  Überspringe (Ausgabe): %s" % os.path.join(dirpath, dn))

        if 'OruxMapsImages.db' in filenames:
            db_path = os.path.join(dirpath, 'OruxMapsImages.db')
            xml_files = [f for f in filenames if f.endswith('.otrk2.xml')]
            if not xml_files:
                log_fn(LOG_WARN, "Keine .otrk2.xml gefunden für %s – übersprungen." % db_path)
                continue
            xml_path = os.path.join(dirpath, xml_files[0])
            try:
                xml_data = parse_otrk2_xml(xml_path)
                if not xml_data:
                    log_fn(LOG_WARN, "Keine Zoom-Level in %s – übersprungen." % xml_files[0])
                    continue
                name = xml_files[0].replace('.otrk2.xml', '')
                src = MapSource(db_path, xml_data, name)
                sources.append(src)
                log_fn(LOG_INFO, "  Gefunden: %s (%d Zoom-Level)" % (name, len(xml_data)))
            except Exception as e:
                log_fn(LOG_ERROR, "Fehler beim Parsen von %s: %s" % (xml_path, e))
    return sources


# --------------------------------------------------------------------------------------
#  KERN-LOGIK: Tiles zusammenführen
# --------------------------------------------------------------------------------------
def _split_tile_512_to_256(image_blob):
    """
    Zerlegt ein 512x512 px Bild-Blob in 4 Sub-Tiles à 256x256 px.
    Gibt eine Liste von (sub_x, sub_y, blob) zurück (sub_x/sub_y ∈ {0,1}).
    Gibt None zurück, falls das Bild nicht 512x512 ist oder ein Fehler auftritt.
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow (PIL) wird für 512-Tile-Aufspaltung benötigt: "
                           "pip install Pillow")
    try:
        img = Image.open(io.BytesIO(image_blob))
    except Exception:
        return None
    w, h = img.size
    if w != 512 or h != 512:
        return None
    mode = img.mode
    fmt = 'PNG' if mode in ('RGBA', 'LA', 'P', '1') else 'JPEG'
    sub_tiles = []
    for sx in range(2):
        for sy in range(2):
            box = (sx * 256, sy * 256, sx * 256 + 256, sy * 256 + 256)
            sub = img.crop(box)
            buf = io.BytesIO()
            sub.save(buf, format=fmt)
            sub_tiles.append((sx, sy, buf.getvalue()))
    return sub_tiles


def _write_tile_orux(out_cur, counter, gx, gy, z, image_blob):
    """Schreibt ein Tile in die OruxMaps-Schema-DB. Gibt (inserted, dup) zurück."""
    try:
        out_cur.execute(
            "INSERT OR REPLACE INTO tiles (x, y, z, image) VALUES (?,?,?,?)",
            (gx, gy, z, image_blob))
        return (counter[0] + 1, counter[1])
    except sqlite3.IntegrityError:
        return (counter[0], counter[1] + 1)


def _write_tile_mbtiles(out_cur, counter, gx, gy, z, image_blob):
    """
    Schreibt ein Tile in die MBTiles-Schema-DB.
    MBTiles verwendet TMS-Konvention: y-Achse ist invertiert gegenüber OSM.
        tms_y = (2^z - 1) - osm_y
    """
    tms_y = (2 ** z - 1) - gy
    try:
        out_cur.execute(
            "INSERT OR REPLACE INTO tiles "
            "(zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)",
            (z, gx, tms_y, image_blob))
        return (counter[0] + 1, counter[1])
    except sqlite3.IntegrityError:
        return (counter[0], counter[1] + 1)


def merge_tiles_to_db(sources, out_dir, map_name, log_fn, progress_fn=None,
                      split_512=True, db_format=FORMAT_ORUX):
    """
    Hauptfunktion:
    1. Liest alle Tiles aus allen Quell-DBs und rechnet lokale Tile-Koordinaten
       in globale Web-Mercator-Koordinaten um.
    2. Schreibt sie in die Ziel-DB:
       - FORMAT_ORUX:    OruxMapsImages.db — Tiles werden auf LOKALE
                         Koordinaten (0-basiert pro Zoom-Level) normalisiert,
                         weil OruxMaps die Tiles ab (0,0) entsprechend der
                         XML xMax/yMax erwartet.
       - FORMAT_MBTILES: <map_name>.mbtiles (QMapShack, TMS-y-Flip, globale
                         Koordinaten wie in der MBTiles-Spezifikation).
    3. Sammelt die Bounding-Box pro Zoom-Level für die XML-Erzeugung.

    Gibt eine Liste von Zoom-Level-Dicts zurück.
    """
    os.makedirs(out_dir, exist_ok=True)

    # --- Zieldateiname & Schema je nach Format ---
    if db_format == FORMAT_MBTILES:
        out_db_full = os.path.join(out_dir, '%s.mbtiles' % map_name)
        write_fn = _write_tile_mbtiles
        use_global_coords = True
    else:
        out_db_full = os.path.join(out_dir, 'OruxMapsImages.db')
        write_fn = None  # wird nach Phase 1 gesetzt (interne Liste)
        use_global_coords = False

    # Alte Zieldateien entfernen
    if os.path.exists(out_db_full):
        os.remove(out_db_full)
    for ext in ['-journal', '-wal', '-shm']:
        p = out_db_full + ext
        if os.path.exists(p):
            os.remove(p)

    # --- Bounding-Boxen pro Zoom-Level sammeln (Tile-Size wird immer 256) ---
    zoom_bounds = {}

    for src in sources:
        for z, lv in src.levels.items():
            if z not in zoom_bounds:
                zoom_bounds[z] = {
                    'minLat': lv['minLat'], 'maxLat': lv['maxLat'],
                    'minLon': lv['minLon'], 'maxLon': lv['maxLon'],
                }
            else:
                b = zoom_bounds[z]
                b['minLat'] = min(b['minLat'], lv['minLat'])
                b['maxLat'] = max(b['maxLat'], lv['maxLat'])
                b['minLon'] = min(b['minLon'], lv['minLon'])
                b['maxLon'] = max(b['maxLon'], lv['maxLon'])

    log_fn(LOG_INFO, "Zoom-Level in Fusion: %s" % sorted(zoom_bounds.keys()))
    if split_512:
        log_fn(LOG_INFO, "512px-Tiles werden zu 4×256 aufgespalten (Pillow).")
    log_fn(LOG_INFO, "Zielformat: %s" % ("MBTiles (QMapShack)" if db_format == FORMAT_MBTILES
                                          else "OruxMaps (.otrk2)"))

    # --- Gesamtanzahl Tiles zählen (für Fortschritt) ---
    total_tiles = 0
    for src in sources:
        conn = sqlite3.connect(src.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tiles")
        total_tiles += c.fetchone()[0]
        conn.close()

    log_fn(LOG_INFO, "Gesamt: %d Quell-Tiles von %d Karten." % (total_tiles, len(sources)))

    # --- PHASE 1: Alle Tiles (global) einsammeln ---
    # Für OruxMaps: dict pro zoom → {(gx, gy): blob} (später normalisiert)
    # Für MBTiles:  direkt schreiben (globale Koordinaten = korrekt)
    collected = {}  # z → {(gx, gy): blob}  — nur für OruxMaps

    processed = 0
    counter = (0, 0)  # (inserted, skipped_dup) — nur für MBTiles

    if not use_global_coords:
        out_conn = None  # wird erst in Phase 2 erstellt
    else:
        out_conn = sqlite3.connect(out_db_full)
        if db_format == FORMAT_MBTILES:
            ensure_mbtiles_schema(out_conn)
        else:
            ensure_tiles_schema(out_conn)
        out_cur = out_conn.cursor()

    for src in sources:
        log_fn(LOG_INFO, "Verarbeite: %s" % src.name)
        src_conn = sqlite3.connect(src.db_path)
        src_cur = src_conn.cursor()

        for z, lv in src.levels.items():
            min_lat = lv['minLat']
            max_lat = lv['maxLat']
            min_lon = lv['minLon']
            max_lon = lv['maxLon']
            tile_size_src = lv.get('tile_size', 256)

            # Globale Start-Tile-Koordinaten (im 256er OSM-Raster) für die
            # obere linke Ecke dieser Karte:
            gx_min_f = lon_to_tile_x(min_lon, z)
            gy_min_f = lat_to_tile_y(max_lat, z)  # obere Kante

            gx_offset = int(math.floor(gx_min_f))
            gy_offset = int(math.floor(gy_min_f))

            src_cur.execute("SELECT x, y, image FROM tiles WHERE z=?", (z,))
            rows = src_cur.fetchall()

            for lx, ly, image_blob in rows:
                if tile_size_src == 512 and split_512:
                    subs = _split_tile_512_to_256(image_blob)
                    if subs:
                        base_gx = gx_offset + 2 * lx
                        base_gy = gy_offset + 2 * ly
                        for sx, sy, sub_blob in subs:
                            gx = base_gx + sx
                            gy = base_gy + sy
                            if use_global_coords:
                                counter = _write_tile_mbtiles(
                                    out_cur, counter, gx, gy, z, sub_blob)
                            else:
                                collected.setdefault(z, {})[(gx, gy)] = sub_blob
                    else:
                        gx = gx_offset + lx
                        gy = gy_offset + ly
                        if use_global_coords:
                            counter = _write_tile_mbtiles(
                                out_cur, counter, gx, gy, z, image_blob)
                        else:
                            collected.setdefault(z, {})[(gx, gy)] = image_blob
                else:
                    gx = gx_offset + lx
                    gy = gy_offset + ly
                    if use_global_coords:
                        counter = _write_tile_mbtiles(
                            out_cur, counter, gx, gy, z, image_blob)
                    else:
                        collected.setdefault(z, {})[(gx, gy)] = image_blob

                processed += 1
                if progress_fn and total_tiles > 0:
                    progress_fn(processed / total_tiles * 100)

        src_conn.close()
        if out_conn:
            out_conn.commit()

    # --- PHASE 2 (nur OruxMaps): Tiles in DB schreiben, normalisiert auf (0,0) ---
    if not use_global_coords:
        out_conn = sqlite3.connect(out_db_full)
        ensure_tiles_schema(out_conn)
        out_cur = out_conn.cursor()

        inserted_orux = 0
        for z in sorted(collected.keys()):
            tiles_z = collected[z]
            if not tiles_z:
                continue
            # Per-zoom: kleinstes (gx, gy) finden → Offset für Normalisierung
            min_gx = min(k[0] for k in tiles_z)
            min_gy = min(k[1] for k in tiles_z)
            for (gx, gy), blob in tiles_z.items():
                lx = gx - min_gx
                ly = gy - min_gy
                try:
                    out_cur.execute(
                        "INSERT OR REPLACE INTO tiles "
                        "(x, y, z, image) VALUES (?,?,?,?)",
                        (lx, ly, z, blob))
                    inserted_orux += 1
                except sqlite3.IntegrityError:
                    pass  # Sollte nie passieren nach Normalisierung
            out_conn.commit()
            log_fn(LOG_INFO, "  z=%2d: %d Tiles (lokale Koordinaten ab 0,0)" % (z, len(tiles_z)))

        out_conn.close()
        log_fn(LOG_OK, "OruxMaps-DB geschrieben: %d Tiles." % inserted_orux)
        counter = (inserted_orux, 0)

    # Bei MBTiles: Metadaten schreiben und schließen
    if db_format == FORMAT_MBTILES:
        zoom_list = [{'zoom': z, **zoom_bounds[z]} for z in sorted(zoom_bounds.keys())]
        write_mbtiles_metadata(out_conn, map_name, zoom_list)
        out_conn.close()

    inserted, skipped_dup = counter
    log_fn(LOG_OK, "Fertig: %d Tiles eingefügt, %d Duplikate übersprungen."
          % (inserted, skipped_dup))

    return [{'zoom': z, **zoom_bounds[z]} for z in sorted(zoom_bounds.keys())]


# ======================================================================================
#  GUI
# ======================================================================================
class MapFusionGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("OruxMaps Karten-Fusion")
        self.root.geometry("750x620")
        self.root.minsize(600, 500)

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.map_name = tk.StringVar()
        self.out_format = tk.StringVar(value=FORMAT_ORUX)
        self.input_dir.trace_add('write', self._on_input_changed)

        # Gefundene Quell-Karten und Mapping für Tree-Auswahl
        self._sources = []
        self._source_by_item = {}

        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        # --- Eingabeverzeichnis ---
        frame_in = ttk.LabelFrame(self.root, text="Eingabeverzeichnis (OruxMaps-Karten)")
        frame_in.pack(fill='x', padx=10, pady=(10, 5))

        ttk.Entry(frame_in, textvariable=self.input_dir, width=70).pack(
            side='left', expand=True, fill='x', padx=(10, 5), pady=8)
        ttk.Button(frame_in, text="Durchsuchen…",
                   command=self._browse_input).pack(side='left', padx=(0, 10), pady=8)

        # --- Ausgabeverzeichnis ---
        frame_out = ttk.LabelFrame(self.root, text="Ausgabeverzeichnis")
        frame_out.pack(fill='x', padx=10, pady=5)

        ttk.Entry(frame_out, textvariable=self.output_dir, width=70).pack(
            side='left', expand=True, fill='x', padx=(10, 5), pady=8)
        ttk.Button(frame_out, text="Durchsuchen…",
                   command=self._browse_output).pack(side='left', padx=(0, 10), pady=8)

        # --- Kartenname & Ausgabeformat ---
        frame_name = ttk.LabelFrame(self.root, text="Name der fusionierten Karte")
        frame_name.pack(fill='x', padx=10, pady=5)

        inner = ttk.Frame(frame_name)
        inner.pack(fill='x', padx=10, pady=8)
        ttk.Label(inner, text="Kartenname:").pack(side='left')
        ttk.Entry(inner, textvariable=self.map_name, width=50).pack(
            side='left', expand=True, fill='x', padx=(10, 0))

        # --- Ausgabeformat ---
        frame_fmt = ttk.LabelFrame(self.root,
                                   text="Ausgabeformat für die fusionierte Karte")
        frame_fmt.pack(fill='x', padx=10, pady=5)

        inner_fmt = ttk.Frame(frame_fmt)
        inner_fmt.pack(fill='x', padx=10, pady=8)
        ttk.Radiobutton(inner_fmt, text="OruxMaps  (.otrk2.xml + OruxMapsImages.db)",
                        variable=self.out_format, value=FORMAT_ORUX).pack(
                            side='left', padx=(0, 20))
        ttk.Radiobutton(inner_fmt, text="QMapShack / MBTiles  (.vrt + .mbtiles)",
                        variable=self.out_format, value=FORMAT_MBTILES).pack(
                            side='left')

        # --- Gefundene Karten ---
        frame_maps = ttk.LabelFrame(self.root, text="Gefundene Karten")
        frame_maps.pack(fill='both', expand=True, padx=10, pady=5)

        self.tree = ttk.Treeview(frame_maps, columns=('path', 'zoom', 'tiles'),
                                 show='headings', height=8, selectmode='extended')
        self.tree.heading('path', text='Karte')
        self.tree.heading('zoom', text='Zoom-Level')
        self.tree.heading('tiles', text='Tiles')
        self.tree.column('path', width=350)
        self.tree.column('zoom', width=120)
        self.tree.column('tiles', width=80)
        self.tree.pack(side='left', expand=True, fill='both', padx=(10, 5), pady=8)

        scrollbar = ttk.Scrollbar(frame_maps, orient='vertical',
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y', pady=8, padx=(0, 10))

        # --- Buttons ---
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill='x', padx=10, pady=5)

        self.btn_merge = ttk.Button(frame_btn, text="Karten fusionieren",
                                    command=self._start_merge)
        self.btn_merge.pack(side='right', padx=(5, 0))
        ttk.Button(frame_btn, text="Schließen",
                   command=self.root.quit).pack(side='right')

        # --- Fortschrittsbalken ---
        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.pack(fill='x', padx=10, pady=(5, 2))

        # --- Log-Ausgabe ---
        frame_log = ttk.LabelFrame(self.root, text="Protokoll")
        frame_log.pack(fill='both', expand=True, padx=10, pady=(5, 10))

        self.log_text = tk.Text(frame_log, height=10, state='disabled',
                                wrap='word', font=('Consolas', 9))
        log_scroll = ttk.Scrollbar(frame_log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side='left', expand=True, fill='both', padx=(10, 5), pady=8)
        log_scroll.pack(side='right', fill='y', pady=8, padx=(0, 10))

        # Status-Bar
        self.status = ttk.Label(self.root, text="Bereit.", relief='sunken', anchor='w')
        self.status.pack(side='bottom', fill='x')

        # Farben für Log
        self.log_text.tag_config(LOG_INFO + '_tag', foreground='gray20')
        self.log_text.tag_config(LOG_WARN + '_tag', foreground='darkorange')
        self.log_text.tag_config(LOG_ERROR + '_tag', foreground='red')
        self.log_text.tag_config(LOG_OK + '_tag', foreground='green')

    # --- Event Handler ---
    def _on_input_changed(self, *args):
        """Wenn das Eingabeverzeichnis geändert wird, Karten suchen."""
        d = self.input_dir.get().strip()
        if d and os.path.isdir(d):
            self._scan_maps(d)

    def _browse_input(self):
        d = filedialog.askdirectory(
            title="Eingabeverzeichnis wählen",
            initialdir=self.input_dir.get() or os.path.expanduser("~"))
        if d:
            self.input_dir.set(d)

    def _browse_output(self):
        d = filedialog.askdirectory(
            title="Ausgabeverzeichnis wählen",
            initialdir=self.output_dir.get() or self.input_dir.get()
                       or os.path.expanduser("~"))
        if d:
            self.output_dir.set(d)

    def _log(self, level, msg):
        """Thread-safe Logging ins Text-Widget."""
        def _do():
            self.log_text.configure(state='normal')
            prefix = {'INFO': '[i]', 'WARNUNG': '[!]',
                      'FEHLER': '[X]', 'OK': '[OK]'}.get(level, '[?]')
            self.log_text.insert('end', "%s %s\n" % (prefix, msg),
                                 (level + '_tag',))
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.root.after(0, _do)

    def _scan_maps(self, directory):
        """Durchsucht das Verzeichnis und füllt die Treeview."""
        self.tree.delete(*self.tree.get_children())
        self._log(LOG_INFO, "Durchsuche: %s" % directory)

        def _do_scan():
            sources = discover_maps(directory, self._log)
            self.root.after(0, lambda: self._populate_tree(sources))

        threading.Thread(target=_do_scan, daemon=True).start()

    def _populate_tree(self, sources):
        # Quellen speichern (für spätere Auswahl-Auswertung)
        self._sources = list(sources)
        # Mapping tree-item-id → MapSource
        self._source_by_item = {}
        for src in sources:
            zooms = sorted(src.levels.keys())
            zoom_str = "%d-%d" % (min(zooms), max(zooms)) if zooms else "?"
            # Tile-Anzahl
            try:
                conn = sqlite3.connect(src.db_path)
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM tiles")
                cnt = c.fetchone()[0]
                conn.close()
            except Exception:
                cnt = '?'
            item = self.tree.insert('', 'end', values=(src.name, zoom_str, cnt))
            self._source_by_item[item] = src

        self._log(LOG_OK, "%d Karten gefunden." % len(sources))
        self.status.config(text="%d Karten gefunden." % len(sources))

    def _get_selected_sources(self):
        """
        Gibt die laut Tree-Auswahl zu fusionierenden Quell-Karten zurück.
        Wenn nichts ausgewählt ist, werden alle gelisteten Karten verwendet.
        """
        sel_items = self.tree.selection()
        if sel_items:
            sources = [self._source_by_item[item]
                       for item in sel_items if item in self._source_by_item]
        else:
            sources = list(self._sources)
        return sources

    def _set_progress(self, pct):
        def _do():
            self.progress['value'] = pct
            self.status.config(text="Verarbeitung: %.0f%%" % pct)
        self.root.after(0, _do)

    def _start_merge(self):
        in_dir = self.input_dir.get().strip()
        out_dir = self.output_dir.get().strip()
        name = self.map_name.get().strip()

        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showerror("Fehler", "Bitte gültiges Eingabeverzeichnis wählen.")
            return
        if not name:
            # Nach Kartennamen fragen
            name = simpledialog.askstring(
                "Kartenname",
                "Bitte Namen für die fusionierte Karte eingeben:",
                parent=self.root)
            if not name:
                return
            self.map_name.set(name)

        if not out_dir:
            # Default: Unterordner im Eingabeverzeichnis
            out_dir = os.path.join(in_dir, name)
            self.output_dir.set(out_dir)

        # Bestätigung
        selected = self._get_selected_sources()
        n_sel = len(selected)
        sel_hint = ("%d ausgewählte Karte(n)" % n_sel if n_sel
                    else "alle %d Karten" % len(self._sources))
        if not messagebox.askyesno(
                "Bestätigung",
                "Karten fusionieren?\n\n"
                "Eingabe:  %s\n"
                "Ausgabe:  %s\n"
                "Name:     %s\n"
                "Fusion:   %s" % (in_dir, out_dir, name, sel_hint)):
            return

        self.btn_merge.config(state='disabled')
        self.progress['value'] = 0

        def _worker():
            try:
                self._log(LOG_INFO, "Starte Fusion … (%d Quell-Karten)" % len(selected))
                if not selected:
                    self._log(LOG_ERROR, "Keine Karten gefunden!")
                    return

                zoom_levels = merge_tiles_to_db(
                    selected, out_dir, name,
                    self._log, self._set_progress,
                    db_format=self.out_format.get())

                # Bei OruxMaps: XML schreiben; bei MBTiles: VRT schreiben.
                if self.out_format.get() == FORMAT_ORUX:
                    xml_path = os.path.join(out_dir, "%s.otrk2.xml" % name)
                    self._log(LOG_INFO, "Schreibe %s …" % xml_path)
                    write_merged_otrk2_xml(xml_path, name, zoom_levels)
                    self._log(LOG_OK, "XML geschrieben: %s" % xml_path)
                else:
                    self._log(LOG_INFO, "MBTiles-Metadaten geschrieben.")
                    # QMapShack-VRT-Datei erzeugen
                    vrt_path = os.path.join(out_dir, "%s.vrt" % name)
                    mbtiles_name = "%s.mbtiles" % name
                    self._log(LOG_INFO, "Schreibe %s …" % vrt_path)
                    write_qmapshack_vrt(vrt_path, mbtiles_name, name, zoom_levels)
                    self._log(LOG_OK, "VRT geschrieben: %s" % vrt_path)

                self._log(LOG_OK, "Fusion abgeschlossen!")
                self._set_progress(100)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Fertig",
                    "Karte '%s' erfolgreich erstellt!\n\n%s" % (name, out_dir)))

            except Exception as e:
                self._log(LOG_ERROR, "Fehler: %s" % e)
                import traceback
                self._log(LOG_ERROR, traceback.format_exc())
                self.root.after(0, lambda: messagebox.showerror("Fehler", str(e)))
            finally:
                self.root.after(0, lambda: self.btn_merge.config(state='normal'))

        threading.Thread(target=_worker, daemon=True).start()


# --------------------------------------------------------------------------------------
#  Main
# --------------------------------------------------------------------------------------
def main():
    root = tk.Tk()
    app = MapFusionGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
