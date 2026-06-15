"""
════════════════════════════════════════════════════════
  FIX: extract_gis() — "expected bytes, str found"
  แทน function extract_gis() เดิมทั้งหมดใน main.py
════════════════════════════════════════════════════════
สาเหตุ: fiona >= 1.9 ส่ง geometry กลับมาเป็น dict โดยตรง
        แต่โค้ดเดิมบางที่ยังเรียก shape() กับ object ผิดประเภท
        หรือ pyproj.CRS.from_wkt() รับ str ไม่ได้ในบางเวอร์ชั่น

วิธีใช้:
  แทน def extract_gis(...) เดิมใน main.py ด้วยโค้ดนี้ทั้งหมด
"""

def extract_gis(zip_path: str, extract_dir: str) -> dict:
    """แตก Shapefile จาก ZIP แล้วคำนวณพิกัดกลาง + พื้นที่"""
    import os, glob, shutil, zipfile
    import geopandas as gpd
    import fiona
    from shapely.geometry import shape
    from shapely.validation import make_valid

    # ── แตก ZIP ──────────────────────────────────
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)
    except Exception:
        raise HTTPException(400, "ไฟล์ ZIP ไม่สมบูรณ์หรือเสียหาย")

    # ── หา .shp ───────────────────────────────────
    shp_files = (
        glob.glob(os.path.join(extract_dir, "**", "*.shp"), recursive=True) +
        glob.glob(os.path.join(extract_dir, "**", "*.SHP"), recursive=True)
    )
    # กรอง __MACOSX ออก (Mac zip artifact)
    shp_files = [f for f in shp_files if "__MACOSX" not in f]
    if not shp_files:
        raise HTTPException(400, "ไม่พบไฟล์ .shp ใน ZIP")

    # ── copy ไปที่ safe_dir (ชื่อ ASCII ล้วน) ────
    original_shp = shp_files[0]
    shp_dir = os.path.dirname(original_shp)
    safe_dir = os.path.join(extract_dir, "safe")
    os.makedirs(safe_dir, exist_ok=True)
    for f in glob.glob(os.path.join(shp_dir, "*")):
        ext = os.path.splitext(f)[1].lower()
        if ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx"):
            try:
                shutil.copy2(f, os.path.join(safe_dir, f"input{ext}"))
            except Exception:
                pass
    shp_path = os.path.join(safe_dir, "input.shp")
    if not os.path.exists(shp_path):
        shp_path = original_shp

    log.info(f"[GIS] reading: {shp_path}")

    # ════════════════════════════════════════════
    # อ่านด้วย geopandas โดยตรง (วิธีที่เสถียรที่สุด)
    # ลอง encoding หลายแบบ เพราะไฟล์ .dbf ไทยมักเป็น TIS-620
    # ════════════════════════════════════════════
    gdf = None
    last_err = None
    for enc in ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]:
        try:
            gdf = gpd.read_file(shp_path, encoding=enc)
            if gdf is not None and len(gdf) > 0:
                log.info(f"[GIS] gpd.read_file ok: encoding={enc}, rows={len(gdf)}")
                break
        except UnicodeDecodeError as e:
            last_err = e
            log.warning(f"[GIS] encoding {enc} failed: {e}")
            gdf = None
        except Exception as e:
            last_err = e
            log.warning(f"[GIS] gpd.read_file({enc}) error: {e}")
            gdf = None

    # ── ถ้า gpd ล้มเหลวทุก encoding → ใช้ fiona fallback ──
    if gdf is None or gdf.empty:
        log.warning("[GIS] gpd fallback → fiona manual read")
        try:
            gdf = _read_shp_via_fiona(shp_path)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"เปิด Shapefile ไม่ได้: {last_err or e}")

    if gdf is None or gdf.empty:
        raise HTTPException(400, "Shapefile ไม่มีข้อมูล (0 features)")

    if gdf.geometry.isna().all():
        raise HTTPException(400, "Shapefile มีข้อมูลแต่ geometry ว่างทั้งหมด")

    # ── ตั้ง CRS (ถ้ายังไม่มี default เป็น UTM47) ──
    if gdf.crs is None:
        log.warning("[GIS] CRS ไม่พบ — ใช้ EPSG:32647")
        gdf = gdf.set_crs(epsg=32647, allow_override=True)

    # ── แปลงเป็น WGS84 ───────────────────────────
    try:
        gdf84 = gdf.to_crs(epsg=4326)
        c = gdf84.geometry.centroid.iloc[0]
        lat, lon = float(c.y), float(c.x)
    except Exception as e:
        log.warning(f"[GIS] to_crs error: {e} — ใช้พิกัด default")
        lat, lon = 18.29, 99.50
        gdf84 = gdf

    # ── คำนวณพื้นที่ ──────────────────────────────
    try:
        area_sqm = max(float(gdf.to_crs(epsg=32647).geometry.area.sum()), 0.0)
    except Exception:
        area_sqm = 0.0

    total_wa = area_sqm / 4.0
    rai   = int(total_wa // 400)
    ngarn = int((total_wa % 400) // 100)
    wa    = int(round(total_wa % 100))

    # ── Simplify ──────────────────────────────────
    try:
        gdf84["geometry"] = gdf84["geometry"].simplify(
            tolerance=0.0001, preserve_topology=True
        )
    except Exception:
        pass

    utm = latlon_to_utm(lat, lon)
    return {
        "gdf84": gdf84,
        "lat": lat, "lon": lon,
        "rai": rai, "ngarn": ngarn, "wa": wa,
        **utm,
    }


def _read_shp_via_fiona(shp_path: str):
    """
    Fallback: อ่าน Shapefile ด้วย fiona โดยตรง
    แก้ปัญหา 'expected bytes, str found' ของ fiona >= 1.9
    geometry อาจเป็น dict, MappingProxy, bytes, str ขึ้นกับเวอร์ชั่น
    """
    import fiona
    import geopandas as gpd
    from shapely.geometry import shape
    from shapely.validation import make_valid

    _ENCODINGS = ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]
    src = None
    for enc in _ENCODINGS:
        try:
            with fiona.open(shp_path, encoding=enc) as s:
                _ = list(s)   # ทดสอบอ่านจริง
            src = fiona.open(shp_path, encoding=enc)
            log.info(f"[GIS][fiona] encoding ok: {enc}")
            break
        except (UnicodeDecodeError, UnicodeError):
            log.warning(f"[GIS][fiona] encoding {enc} failed")
        except Exception as e:
            log.warning(f"[GIS][fiona] open error: {e}")
            break

    if src is None:
        raise HTTPException(400, "อ่าน .dbf ไม่ได้ — ลอง encoding ทุกแบบแล้ว")

    rows = []
    crs_wkt = None
    with src:
        crs_wkt = src.crs_wkt
        for feat in src:
            # ── อ่าน geometry ให้รองรับทุกเวอร์ชั่น fiona ──
            raw = None
            try:
                raw = feat["geometry"]          # fiona 1.9+ (dict/MappingProxy)
            except (KeyError, TypeError):
                raw = getattr(feat, "geometry", None)   # fiona legacy

            geom = None
            if raw is not None:
                try:
                    if isinstance(raw, bytes):
                        from shapely import wkb
                        geom = wkb.loads(raw)
                    elif isinstance(raw, str):
                        # fiona บางตัวส่ง WKT string
                        from shapely import wkt as swkt
                        geom = swkt.loads(raw)
                    elif isinstance(raw, dict):
                        geom = shape(raw)
                    elif hasattr(raw, "__geo_interface__"):
                        geom = shape(raw.__geo_interface__)
                    else:
                        # ลอง shape() ตรงๆ — fiona MappingProxy
                        geom = shape(dict(raw))
                except Exception as eg:
                    log.warning(f"[GIS][fiona] geom parse error: {eg} (type={type(raw).__name__})")

            if geom is not None and not geom.is_valid:
                geom = make_valid(geom)

            props = {}
            try:
                props = dict(feat.properties)
            except Exception:
                pass

            rows.append({"geometry": geom, **props})

    if not rows:
        raise HTTPException(400, "Shapefile ไม่มีข้อมูล (0 features)")

    gdf = gpd.GeoDataFrame(rows, geometry="geometry")

    # ── ตั้ง CRS ──────────────────────────────────
    if crs_wkt:
        try:
            from pyproj import CRS as ProjCRS
            gdf = gdf.set_crs(ProjCRS.from_wkt(crs_wkt), allow_override=True)
        except Exception as ce:
            log.warning(f"[GIS] from_wkt failed ({ce}) → EPSG:32647")
            gdf = gdf.set_crs(epsg=32647, allow_override=True)
    else:
        gdf = gdf.set_crs(epsg=32647, allow_override=True)

    return gdf
