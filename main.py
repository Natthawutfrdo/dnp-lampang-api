"""
DNP GIS Case API — Production v5.0
ระบบ API สารบบคดีเชิงพื้นที่ สบอ.13 ลำปาง

🔧 v5.0 REFACTOR: รับไฟล์ Shapefile แยกส่วน (.shp .dbf .shx .prj ฯลฯ)
   แทนการส่งเป็น ZIP เดียว
   - endpoint /analyze-shapefile/ รับ: shp_file, dbf_file, shx_file, prj_file (optional)
   - endpoint /process-shapefile/ รับ: shp_file, dbf_file, shx_file, prj_file, pdf_file
   - ไม่มีการเปลี่ยน DB schema — backward compatible กับข้อมูลเดิมทั้งหมด
   - shapefile_url จะเป็น URL ของ .shp ไฟล์ (แทน .zip)
   - เพิ่ม endpoint /upload-shp-parts/ สำหรับ upload แยก แล้วคืน URLs ทุกไฟล์
"""

import os, shutil, tempfile, json, math, time, logging, glob, traceback, re, zipfile
from typing import Optional, List
from functools import lru_cache

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
import geopandas as gpd
from supabase import create_client, Client

# ─────────────────────────────────────────────────
# 1. LOGGING
# ─────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────
# 2. APP + MIDDLEWARE
# ─────────────────────────────────────────────────
app = FastAPI(
    title="DNP GIS Case API",
    description="ระบบบริการข้อมูลสารบบคดีและแผนที่เชิงพื้นที่ สบอ.13 ลำปาง",
    version="5.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

ALLOWED_ORIGINS: List[str] = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "https://natthawutfrdo.github.io,http://localhost:3000,http://127.0.0.1:5500"
    ).split(",")
    if o.strip()
]
log.info(f"✅ CORS origins: {ALLOWED_ORIGINS}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ─────────────────────────────────────────────────
# 3. SUPABASE CLIENT
# ─────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MAX_FILE_MB  = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
MAX_BYTES    = MAX_FILE_MB * 1024 * 1024

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("✅ Supabase connected")
    except Exception as e:
        log.error(f"❌ Supabase init error: {e}")
else:
    log.warning("⚠️  SUPABASE_URL / SUPABASE_KEY not set")

def get_db() -> Client:
    if not supabase:
        raise HTTPException(503, detail="ฐานข้อมูลยังไม่ได้เชื่อมต่อ")
    return supabase

# ─────────────────────────────────────────────────
# 4. SCHEMA CACHE
# ─────────────────────────────────────────────────
_schema_cache: dict[str, tuple[bool, float]] = {}
SCHEMA_TTL = 300

def check_columns(table: str, columns: list[str]) -> bool:
    key = f"{table}:{','.join(sorted(columns))}"
    now = time.time()
    if key in _schema_cache:
        val, ts = _schema_cache[key]
        if now - ts < SCHEMA_TTL:
            return val
    if not supabase:
        return False
    try:
        supabase.table(table).select(",".join(columns)).limit(0).execute()
        _schema_cache[key] = (True, now)
        return True
    except Exception:
        _schema_cache[key] = (False, now)
        return False

def clear_schema_cache():
    _schema_cache.clear()

# ─────────────────────────────────────────────────
# 5. TABLE MAP
# ─────────────────────────────────────────────────
TABLE_MAP = {
    "encroachment": "encroachment_cases",
    "timber":       "timber_cases",
    "wildlife":     "wildlife_cases",
}

def get_table(case_type: str) -> str:
    if case_type not in TABLE_MAP:
        raise HTTPException(400, f"case_type ต้องเป็น: {', '.join(TABLE_MAP.keys())}")
    return TABLE_MAP[case_type]

# ─────────────────────────────────────────────────
# 6. FILE VALIDATION
# ─────────────────────────────────────────────────
async def validate_file(
    file: UploadFile,
    allowed_ext: str,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    ext = allowed_ext if allowed_ext.startswith(".") else f".{allowed_ext}"
    if not file.filename.lower().endswith(ext):
        raise HTTPException(400, f"ไฟล์ต้องเป็นนามสกุล {ext}")
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(413, f"ไฟล์ใหญ่เกิน {max_bytes // 1024 // 1024} MB")
    await file.seek(0)
    return content

async def validate_shp_parts(
    shp_file: UploadFile,
    dbf_file: UploadFile,
    shx_file: UploadFile,
) -> dict[str, bytes]:
    """
    ตรวจสอบและอ่าน bytes ของไฟล์ Shapefile แยกส่วน
    บังคับ: .shp .dbf .shx  |  optional: .prj .cpg
    """
    errors = []
    if not shp_file or not shp_file.filename.lower().endswith(".shp"):
        errors.append("ต้องแนบไฟล์ .shp")
    if not dbf_file or not dbf_file.filename.lower().endswith(".dbf"):
        errors.append("ต้องแนบไฟล์ .dbf")
    if not shx_file or not shx_file.filename.lower().endswith(".shx"):
        errors.append("ต้องแนบไฟล์ .shx")
    if errors:
        raise HTTPException(400, " | ".join(errors))

    result = {}
    result["shp"] = await shp_file.read()
    result["dbf"] = await dbf_file.read()
    result["shx"] = await shx_file.read()
    return result

# ─────────────────────────────────────────────────
# 7. SHAPEFILE ASSEMBLY — เขียนลง tmpdir แล้วคืน path
# ─────────────────────────────────────────────────
async def assemble_shapefile(
    tmpdir: str,
    shp_file: UploadFile,
    dbf_file: UploadFile,
    shx_file: UploadFile,
    prj_file: Optional[UploadFile] = None,
    cpg_file: Optional[UploadFile] = None,
) -> str:
    """
    เขียนไฟล์ Shapefile ที่แยกมาทั้งหมดลงใน tmpdir/input.*
    คืน path ของ .shp หลัก
    """
    base = os.path.join(tmpdir, "input")

    async def write_part(upload: UploadFile, ext: str):
        content = await upload.read()
        with open(f"{base}{ext}", "wb") as f:
            f.write(content)

    await write_part(shp_file, ".shp")
    await write_part(dbf_file, ".dbf")
    await write_part(shx_file, ".shx")

    if prj_file and prj_file.filename:
        await write_part(prj_file, ".prj")
    if cpg_file and cpg_file.filename:
        await write_part(cpg_file, ".cpg")

    shp_path = f"{base}.shp"
    log.info(f"[ASSEMBLE] shapefile ready: {shp_path}")
    return shp_path

# ─────────────────────────────────────────────────
# 8. UTM / LAT-LON HELPERS
# ─────────────────────────────────────────────────
def utm_to_latlon(zone: int, easting: float, northing: float, is_north: bool = True) -> dict:
    k0 = 0.9996; a = 6378137.0; e = 0.0818191908426215
    e2 = e*e; e4 = e2*e2; e6 = e4*e2; e1sq = e2/(1-e2)
    x = easting - 500000.0
    y = northing if is_north else northing - 10000000.0
    lon0 = (zone-1)*6 - 180 + 3
    M = y/k0
    mu = M/(a*(1 - e2/4 - 3*e4/64 - 5*e6/256))
    phi1 = (mu
            + (3/2*e2 + 27/32*e4 + 55/512*e6)*math.sin(2*mu)
            + (21/16*e4 + 55/32*e6)*math.sin(4*mu)
            + (151/96*e6)*math.sin(6*mu))
    N1 = a/math.sqrt(1 - e2*math.sin(phi1)**2)
    T1 = math.tan(phi1)**2
    C1 = e1sq*math.cos(phi1)**2
    R1 = a*(1-e2)/(1 - e2*math.sin(phi1)**2)**1.5
    D  = x/(N1*k0)
    lat = phi1 - (N1*math.tan(phi1)/R1)*(
        D**2/2 - (5+3*T1+10*C1-4*C1**2-9*e1sq)*D**4/24
        + (61+90*T1+298*C1+45*T1**2-252*e1sq-3*C1**2)*D**6/720)
    lon = (D - (1+2*T1+C1)*D**3/6
           + (5-2*C1+28*T1-3*C1**2+8*e1sq+24*T1**2)*D**5/120)/math.cos(phi1)
    return {"lat": math.degrees(lat), "lon": lon0 + math.degrees(lon)}

def latlon_to_utm(lat: float, lon: float) -> dict:
    try:
        zone = int((lon + 180)/6) + 1
        k0=0.9996; a=6378137.0; e=0.0818191908426215
        e2=e*e; e4=e2*e2; e6=e4*e2; e1sq=e2/(1-e2)
        lr = math.radians(lat); lr2 = math.radians(lon)
        lo = math.radians((zone-1)*6 - 180 + 3)
        N = a/math.sqrt(1-e2*math.sin(lr)**2)
        T = math.tan(lr)**2; C = e1sq*math.cos(lr)**2
        A = math.cos(lr)*(lr2-lo)
        M = a*((1-e2/4-3*e4/64-5*e6/256)*lr
                -(3*e2/8+3*e4/32+45*e6/1024)*math.sin(2*lr)
                +(15*e4/256+45*e6/1024)*math.sin(4*lr)
                -(35*e6/3072)*math.sin(6*lr))
        east = (k0*N*(A+(1-T+C)*A**3/6
                +(5-18*T+T**2+72*C-58*e1sq)*A**5/120) + 500000.0)
        north = k0*(M+N*math.tan(lr)*(A**2/2
                +(5-T+9*C+4*C**2)*A**4/24
                +(61-58*T+T**2+600*C-330*e1sq)*A**6/720))
        if lat < 0:
            north += 10000000.0
        return {"utm_zone": zone, "utm_easting": int(round(east)), "utm_northing": int(round(north))}
    except Exception as err:
        log.warning(f"latlon→UTM error: {err}")
        return {"utm_zone": 47, "utm_easting": 0, "utm_northing": 0}

# ─────────────────────────────────────────────────
# 9. GIS EXTRACTION — รับ shp_path โดยตรง (ไม่ต้อง unzip)
# ─────────────────────────────────────────────────
def _read_shp_via_fiona(shp_path: str):
    import fiona
    import geopandas as gpd
    from shapely.geometry import shape
    from shapely.validation import make_valid
    from pyproj import CRS as ProjCRS

    _ENCODINGS = ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]
    src = None

    for enc in _ENCODINGS:
        try:
            with fiona.open(shp_path, encoding=enc) as s:
                _ = list(s)
            src = fiona.open(shp_path, encoding=enc)
            log.info(f"[GIS][fiona] encoding ok: {enc}")
            break
        except (UnicodeDecodeError, UnicodeError):
            log.warning(f"[GIS][fiona] encoding {enc} failed")
        except Exception as e:
            log.warning(f"[GIS][fiona] open error ({enc}): {e}")
            break

    if src is None:
        raise HTTPException(400, "อ่าน .dbf ไม่ได้ — ลอง encoding ทุกแบบแล้ว")

    rows = []
    crs_wkt = None

    with src:
        crs_wkt = src.crs_wkt
        for feat in src:
            raw = None
            try:
                raw = feat["geometry"]
            except (KeyError, TypeError):
                raw = getattr(feat, "geometry", None)

            geom = None
            if raw is not None:
                try:
                    if isinstance(raw, bytes):
                        from shapely import wkb
                        geom = wkb.loads(raw)
                    elif isinstance(raw, str):
                        from shapely import wkt as swkt
                        geom = swkt.loads(raw)
                    elif isinstance(raw, dict):
                        geom = shape(raw)
                    elif hasattr(raw, "__geo_interface__"):
                        geom = shape(raw.__geo_interface__)
                    else:
                        geom = shape(dict(raw))
                except Exception as eg:
                    log.warning(f"[GIS][fiona] geom parse error: {eg}")

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

    valid_geom_count = sum(1 for r in rows if r.get("geometry") is not None)
    log.info(f"[GIS][fiona] valid geometry: {valid_geom_count}/{len(rows)}")

    if valid_geom_count == 0:
        log.warning("[GIS][fiona] geometry ว่างทั้งหมด → ลอง gpd.read_file ตรง")
        for enc in ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]:
            try:
                _tmp = gpd.read_file(shp_path, encoding=enc)
                if _tmp is not None and len(_tmp) > 0 and not _tmp.geometry.isna().all():
                    return _tmp
            except Exception as eg:
                log.warning(f"[GIS][fiona→gpd] enc={enc} failed: {eg}")
        raise HTTPException(400, "Shapefile อ่าน geometry ไม่ได้")

    rows = [r for r in rows if r.get("geometry") is not None]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry")

    # CRS fallback chain
    crs_set = False
    if crs_wkt:
        for method, fn in [
            ("from_wkt(str)", lambda: ProjCRS.from_wkt(crs_wkt)),
            ("from_wkt(bytes)", lambda: ProjCRS.from_wkt(crs_wkt.encode("utf-8") if isinstance(crs_wkt, str) else crs_wkt)),
            ("from_user_input", lambda: ProjCRS.from_user_input(crs_wkt)),
        ]:
            if crs_set:
                break
            try:
                gdf = gdf.set_crs(fn(), allow_override=True)
                crs_set = True
                log.info(f"[GIS] CRS: {method} ok")
            except Exception as e:
                log.warning(f"[GIS] {method} failed: {e}")

        if not crs_set:
            m = re.search(r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"(\d{4,6})"', crs_wkt, re.IGNORECASE)
            if not m:
                m = re.search(r'EPSG["\s:,]+(\d{4,6})', crs_wkt, re.IGNORECASE)
            if m:
                gdf = gdf.set_crs(epsg=int(m.group(1)), allow_override=True)
                crs_set = True
                log.info(f"[GIS] CRS: parsed EPSG:{m.group(1)}")

    if not crs_set:
        log.warning("[GIS] CRS ไม่สามารถตั้งได้ — ใช้ EPSG:32647")
        gdf = gdf.set_crs(epsg=32647, allow_override=True)

    return gdf


def extract_gis_from_shp(shp_path: str) -> dict:
    """
    คำนวณพิกัดกลาง + พื้นที่จาก .shp path โดยตรง
    (ไม่ต้อง unzip — ไฟล์ถูก assemble ไว้แล้วใน tmpdir)
    """
    from shapely.validation import make_valid

    log.info(f"[GIS] reading shp: {shp_path}")
    gdf = None
    last_err = None

    for enc in ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]:
        try:
            _tmp = gpd.read_file(shp_path, encoding=enc)
            if (
                _tmp is not None
                and len(_tmp) > 0
                and not _tmp.geometry.isna().all()
            ):
                gdf = _tmp
                log.info(f"[GIS] gpd.read_file ok: encoding={enc}, rows={len(gdf)}")
                break
            else:
                log.warning(f"[GIS] gpd.read_file({enc}): geometry all-null or empty")
        except UnicodeDecodeError as e:
            last_err = e
        except Exception as e:
            last_err = e
            log.warning(f"[GIS] gpd.read_file({enc}) error: {e}")
            gdf = None

    if gdf is None or gdf.empty or gdf.geometry.isna().all():
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

    if gdf.crs is None:
        log.warning("[GIS] CRS ไม่พบ — ใช้ EPSG:32647")
        gdf = gdf.set_crs(epsg=32647, allow_override=True)

    try:
        gdf84 = gdf.to_crs(epsg=4326)
        c = gdf84.geometry.centroid.iloc[0]
        lat, lon = float(c.y), float(c.x)
    except Exception as e:
        log.warning(f"[GIS] to_crs error: {e} — ใช้พิกัด default")
        lat, lon = 18.29, 99.50
        gdf84 = gdf

    try:
        area_sqm = max(float(gdf.to_crs(epsg=32647).geometry.area.sum()), 0.0)
    except Exception:
        area_sqm = 0.0

    total_wa = area_sqm / 4.0
    rai   = int(total_wa // 400)
    ngarn = int((total_wa % 400) // 100)
    wa    = int(round(total_wa % 100))

    try:
        gdf84["geometry"] = gdf84["geometry"].simplify(tolerance=0.0001, preserve_topology=True)
    except Exception:
        pass

    utm = latlon_to_utm(lat, lon)
    return {
        "gdf84": gdf84,
        "lat": lat, "lon": lon,
        "rai": rai, "ngarn": ngarn, "wa": wa,
        **utm,
    }

# ─────────────────────────────────────────────────
# 10. STORAGE UPLOAD HELPER
# ─────────────────────────────────────────────────
def upload_to_storage(
    db: Client,
    bucket: str,
    path: str,
    file_path: str,
    content_type: str = "application/octet-stream",
) -> str:
    with open(file_path, "rb") as f:
        db.storage.from_(bucket).upload(
            path=path,
            file=f,
            file_options={
                "cache-control": "3600",
                "upsert": True,
                "content-type": content_type,
            },
        )
    return db.storage.from_(bucket).get_public_url(path)

def upload_bytes_to_storage(
    db: Client,
    bucket: str,
    path: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload bytes โดยตรง (ไม่ต้องเขียน temp file)"""
    db.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={
            "cache-control": "3600",
            "upsert": True,
            "content-type": content_type,
        },
    )
    return db.storage.from_(bucket).get_public_url(path)

# content-type map สำหรับ shapefile parts
SHP_CONTENT_TYPES = {
    ".shp": "application/octet-stream",
    ".dbf": "application/dbase",
    ".shx": "application/octet-stream",
    ".prj": "text/plain",
    ".cpg": "text/plain",
    ".sbn": "application/octet-stream",
    ".sbx": "application/octet-stream",
}

# ─────────────────────────────────────────────────
# 11. ENDPOINTS
# ─────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def root():
    return {
        "message": "DNP GIS Case API v5.0 is running!",
        "status": "ok",
        "version": "5.0",
        "upload_mode": "separate_files (.shp .dbf .shx .prj)",
    }

@app.get("/health", tags=["Health"])
def health():
    return {
        "api": "ok",
        "db": "connected" if supabase else "disconnected",
        "cors_origins": ALLOWED_ORIGINS,
        "version": "5.0",
    }

# ── วิเคราะห์ Shapefile (แยกไฟล์ — ไม่บันทึก DB) ──────────────────────────
@app.post("/analyze-shapefile/", tags=["GIS"])
async def analyze_shapefile(
    shp_file: UploadFile = File(..., description="ไฟล์ .shp"),
    dbf_file: UploadFile = File(..., description="ไฟล์ .dbf"),
    shx_file: UploadFile = File(..., description="ไฟล์ .shx"),
    prj_file: Optional[UploadFile] = File(None, description="ไฟล์ .prj (แนะนำ)"),
    cpg_file: Optional[UploadFile] = File(None, description="ไฟล์ .cpg (optional)"),
):
    """
    รับไฟล์ Shapefile แยกส่วน → วิเคราะห์พิกัด + พื้นที่
    ไม่บันทึกลง DB ใช้สำหรับ preview ก่อนบันทึก
    """
    with tempfile.TemporaryDirectory() as tmp:
        shp_path = await assemble_shapefile(
            tmp, shp_file, dbf_file, shx_file, prj_file, cpg_file
        )
        gis = extract_gis_from_shp(shp_path)

    return {
        "success":      True,
        "lat":          gis["lat"],
        "lon":          gis["lon"],
        "rai":          gis["rai"],
        "ngarn":        gis["ngarn"],
        "wa":           gis["wa"],
        "utm_zone":     gis["utm_zone"],
        "utm_easting":  gis["utm_easting"],
        "utm_northing": gis["utm_northing"],
    }

# ── อัปโหลด Shapefile parts ขึ้น Storage (แยก endpoint) ───────────────────
@app.post("/upload-shp-parts/", tags=["GIS"])
async def upload_shp_parts(
    shp_file: UploadFile = File(...),
    dbf_file: UploadFile = File(...),
    shx_file: UploadFile = File(...),
    prj_file: Optional[UploadFile] = File(None),
    cpg_file: Optional[UploadFile] = File(None),
    prefix:   str = Form("case"),
    db: Client = Depends(get_db),
):
    """
    อัปโหลด Shapefile ทุกไฟล์ขึ้น Supabase Storage
    คืน URLs ของทุกส่วน + geojson_url
    """
    safe_prefix = prefix.replace("/", "_").replace(" ", "_")

    with tempfile.TemporaryDirectory() as tmp:
        shp_path = await assemble_shapefile(
            tmp, shp_file, dbf_file, shx_file, prj_file, cpg_file
        )

        # วิเคราะห์ GIS ก่อน upload
        gis = extract_gis_from_shp(shp_path)

        urls = {}
        parts = {
            ".shp": shp_file,
            ".dbf": dbf_file,
            ".shx": shx_file,
        }
        if prj_file and prj_file.filename:
            parts[".prj"] = prj_file
        if cpg_file and cpg_file.filename:
            parts[".cpg"] = cpg_file

        # Upload แต่ละส่วน
        for ext, upload in parts.items():
            storage_path = f"{safe_prefix}{ext}"
            local_path   = os.path.join(tmp, f"input{ext}")
            ct = SHP_CONTENT_TYPES.get(ext, "application/octet-stream")
            try:
                url = upload_to_storage(db, "dnp-shapefiles", storage_path, local_path, ct)
                urls[ext.lstrip(".")] = url
            except Exception as e:
                log.warning(f"Upload {ext} failed: {e}")

        # Upload GeoJSON
        geojson_path = os.path.join(tmp, f"{safe_prefix}_map.json")
        with open(geojson_path, "w", encoding="utf-8") as jf:
            jf.write(gis["gdf84"].to_json())
        try:
            geojson_url = upload_to_storage(
                db, "dnp-shapefiles", f"{safe_prefix}_map.json", geojson_path, "application/json"
            )
            urls["geojson"] = geojson_url
        except Exception as e:
            log.warning(f"GeoJSON upload failed: {e}")
            geojson_url = ""

    return {
        "success":      True,
        "lat":          gis["lat"],
        "lon":          gis["lon"],
        "rai":          gis["rai"],
        "ngarn":        gis["ngarn"],
        "wa":           gis["wa"],
        "utm_zone":     gis["utm_zone"],
        "utm_easting":  gis["utm_easting"],
        "utm_northing": gis["utm_northing"],
        "urls":         urls,
        "geojson_url":  geojson_url,
        "shp_url":      urls.get("shp", ""),
    }

# ── บันทึกคดีบุกรุก / ไม้ (แยกไฟล์) ──────────────────────────────────────
@app.post("/process-shapefile/", tags=["Cases"])
async def process_shapefile(
    shp_file:       UploadFile = File(..., description="ไฟล์ .shp"),
    dbf_file:       UploadFile = File(..., description="ไฟล์ .dbf"),
    shx_file:       UploadFile = File(..., description="ไฟล์ .shx"),
    prj_file:       Optional[UploadFile] = File(None),
    cpg_file:       Optional[UploadFile] = File(None),
    pdf_file:       Optional[UploadFile] = File(None),
    case_type:      str   = Form(...),
    complaint_no:   str   = Form(""),
    criminal_no:    str   = Form(""),
    seizure_no:     str   = Form(""),
    case_date:      str   = Form(...),
    location:       str   = Form(...),
    status:         str   = Form(...),
    case_status:    str   = Form(...),
    agency:         str   = Form(...),
    suspects_count: int   = Form(0),
    rai:            float = Form(0.0),
    ngarn:          float = Form(0.0),
    wa:             float = Form(0.0),
    timber_type:    str   = Form(""),
    width:          str   = Form("0"),
    length:         str   = Form("0"),
    size:           str   = Form("0"),
    vol1:           float = Form(0.0),
    vol2:           float = Form(0.0),
    utm_zone:       int   = Form(47),
    utm_easting:    int   = Form(0),
    utm_northing:   int   = Form(0),
    db: Client = Depends(get_db),
):
    get_table(case_type)

    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, "pdf", max_bytes=20 * 1024 * 1024)

    primary_key = complaint_no or criminal_no or "unknown"
    safe_key    = primary_key.replace("/", "_").replace(" ", "_")

    new_cols = (case_type == "encroachment") and check_columns(
        "encroachment_cases", ["complaint_no", "criminal_no", "seizure_no"]
    )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            # ── Assemble shapefile ────────────────────────────────────────────
            shp_path = await assemble_shapefile(
                tmp, shp_file, dbf_file, shx_file, prj_file, cpg_file
            )

            # ── วิเคราะห์ GIS ─────────────────────────────────────────────────
            gis = extract_gis_from_shp(shp_path)

            f_zone     = utm_zone     if utm_easting != 0 else gis["utm_zone"]
            f_easting  = utm_easting  if utm_easting != 0 else gis["utm_easting"]
            f_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]
            f_rai      = int(rai)       if rai   > 0 else gis["rai"]
            f_ngarn    = int(ngarn)     if ngarn > 0 else gis["ngarn"]
            f_wa       = int(round(wa)) if wa    > 0 else gis["wa"]
            coords     = [gis["lat"], gis["lon"]]

            # ── Upload Shapefile parts ────────────────────────────────────────
            shp_url = ""
            part_files = {
                ".shp": os.path.join(tmp, "input.shp"),
                ".dbf": os.path.join(tmp, "input.dbf"),
                ".shx": os.path.join(tmp, "input.shx"),
            }
            if os.path.exists(os.path.join(tmp, "input.prj")):
                part_files[".prj"] = os.path.join(tmp, "input.prj")
            if os.path.exists(os.path.join(tmp, "input.cpg")):
                part_files[".cpg"] = os.path.join(tmp, "input.cpg")

            for ext, local_path in part_files.items():
                storage_path = f"{safe_key}{ext}"
                ct = SHP_CONTENT_TYPES.get(ext, "application/octet-stream")
                try:
                    url = upload_to_storage(db, "dnp-shapefiles", storage_path, local_path, ct)
                    if ext == ".shp":
                        shp_url = url   # ใช้ .shp URL เป็น shapefile_url หลัก
                except Exception as e:
                    log.warning(f"Upload {ext} failed: {e}")
                    if ext == ".shp":
                        return JSONResponse({"success": False, "error": f"อัปโหลด .shp ล้มเหลว: {e}"})

            # ── Upload PDF ────────────────────────────────────────────────────
            pdf_url = ""
            if pdf_file and pdf_file.filename:
                pdf_fn  = f"{safe_key}_{pdf_file.filename.replace(' ', '_')}"
                pdf_tmp = os.path.join(tmp, pdf_fn)
                with open(pdf_tmp, "wb") as buf:
                    shutil.copyfileobj(pdf_file.file, buf)
                try:
                    pdf_url = upload_to_storage(db, "dnp-pdfs", pdf_fn, pdf_tmp, "application/pdf")
                except Exception as e:
                    log.warning(f"PDF upload failed: {e}")

            # ── สร้าง + Upload GeoJSON ─────────────────────────────────────────
            geojson_fn   = f"{safe_key}_map.json"
            geojson_path = os.path.join(tmp, geojson_fn)
            with open(geojson_path, "w", encoding="utf-8") as jf:
                jf.write(gis["gdf84"].to_json())
            try:
                geojson_url = upload_to_storage(
                    db, "dnp-shapefiles", geojson_fn, geojson_path, "application/json"
                )
            except Exception as e:
                return JSONResponse({"success": False, "error": f"สร้าง GeoJSON ล้มเหลว: {e}"})

            is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")

            # ── Insert DB ─────────────────────────────────────────────────────
            try:
                if case_type == "encroachment":
                    row: dict = {
                        "case_no":        complaint_no or criminal_no,
                        "case_date":      case_date,
                        "location":       location,
                        "rai":            f_rai,
                        "ngarn":          f_ngarn,
                        "wa":             f_wa,
                        "is_finished":    is_finished,
                        "case_status":    case_status,
                        "coords":         coords,
                        "agency":         agency,
                        "suspects_count": suspects_count,
                        "shapefile_url":  shp_url,   # URL ของ .shp
                        "pdf_url":        pdf_url,
                        "geojson_data":   geojson_url,
                        "utm_zone":       f_zone,
                        "utm_easting":    f_easting,
                        "utm_northing":   f_northing,
                    }
                    if new_cols:
                        row["complaint_no"] = complaint_no
                        row["criminal_no"]  = criminal_no
                        row["seizure_no"]   = seizure_no
                    else:
                        extra = ""
                        if complaint_no: extra += f"\n[แจ้ง: {complaint_no}]"
                        if criminal_no:  extra += f"\n[อาญา: {criminal_no}]"
                        if seizure_no:   extra += f"\n[ยึดทรัพย์: {seizure_no}]"
                        if extra:
                            row["case_status"] = case_status + extra
                    db.table("encroachment_cases").insert(row).execute()

                elif case_type == "timber":
                    db.table("timber_cases").insert({
                        "case_no":        complaint_no or criminal_no,
                        "case_date":      case_date,
                        "location":       location,
                        "timber_type":    timber_type,
                        "width":          width,
                        "length":         length,
                        "size":           size,
                        "vol_logs":       vol1,
                        "vol_processed":  vol2,
                        "is_finished":    is_finished,
                        "case_status":    case_status,
                        "coords":         coords,
                        "agency":         agency,
                        "suspects_count": suspects_count,
                        "shapefile_url":  shp_url,
                        "pdf_url":        pdf_url,
                        "geojson_data":   geojson_url,
                        "utm_zone":       f_zone,
                        "utm_easting":    f_easting,
                        "utm_northing":   f_northing,
                    }).execute()

            except Exception as e:
                return JSONResponse({"success": False, "error": f"บันทึก DB ล้มเหลว: {e}"})

            return {
                "success":         True,
                "message":         "บันทึกข้อมูลสำเร็จ",
                "coords":          coords,
                "geojson_url":     geojson_url,
                "shapefile_url":   shp_url,
                "pdf_url":         pdf_url,
                "utm_zone":        f_zone,
                "utm_easting":     f_easting,
                "utm_northing":    f_northing,
                "schema_upgraded": new_cols,
            }

    except HTTPException:
        raise
    except Exception as e:
        log.exception("process_shapefile error")
        return JSONResponse({"success": False, "error": str(e)})

# ── บันทึกคดีสัตว์ป่า (ไม่เปลี่ยน — ไม่มี shapefile) ──────────────────────
@app.post("/process-wildlife/", tags=["Cases"])
async def process_wildlife(
    pdf_file:       UploadFile = File(None),
    case_no:        str   = Form(...),
    case_date:      str   = Form(""),
    location:       str   = Form(...),
    status:         str   = Form(...),
    case_status:    str   = Form(...),
    agency:         str   = Form(...),
    suspects_count: int   = Form(0),
    wildlife_type:  str   = Form(""),
    equipment:      str   = Form(""),
    coords_lat:     float = Form(0.0),
    coords_lon:     float = Form(0.0),
    utm_zone:       int   = Form(47),
    utm_easting:    int   = Form(0),
    utm_northing:   int   = Form(0),
    db: Client = Depends(get_db),
):
    pdf_url = ""
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, "pdf", max_bytes=20 * 1024 * 1024)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf_fn   = f"{case_no.replace('/', '_')}_{pdf_file.filename.replace(' ', '_')}"
                pdf_path = os.path.join(tmp, pdf_fn)
                with open(pdf_path, "wb") as buf:
                    shutil.copyfileobj(pdf_file.file, buf)
                pdf_url = upload_to_storage(db, "dnp-pdfs", pdf_fn, pdf_path, "application/pdf")
        except Exception as e:
            log.warning(f"Wildlife PDF upload failed: {e}")

    is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")
    coords      = [coords_lat, coords_lon] if (coords_lat or coords_lon) else [18.29, 99.50]

    f_zone, f_east, f_north = utm_zone, utm_easting, utm_northing
    if utm_easting == 0 and coords_lat:
        u = latlon_to_utm(coords[0], coords[1])
        f_zone, f_east, f_north = u["utm_zone"], u["utm_easting"], u["utm_northing"]
    if coords_lat == 0.0 and utm_easting:
        try:
            ll = utm_to_latlon(utm_zone, utm_easting, utm_northing)
            coords = [ll["lat"], ll["lon"]]
        except Exception:
            coords = [18.29, 99.50]

    try:
        db.table("wildlife_cases").insert({
            "case_no":        case_no,
            "case_date":      case_date,
            "location":       location,
            "wildlife_type":  wildlife_type,
            "equipment":      equipment,
            "is_finished":    is_finished,
            "case_status":    case_status,
            "coords":         coords,
            "agency":         agency,
            "suspects_count": suspects_count,
            "pdf_url":        pdf_url,
            "utm_zone":       f_zone,
            "utm_easting":    f_east,
            "utm_northing":   f_north,
        }).execute()
        return {
            "success":      True,
            "message":      "บันทึกคดีสัตว์ป่าสำเร็จ",
            "coords":       coords,
            "utm_zone":     f_zone,
            "utm_easting":  f_east,
            "utm_northing": f_north,
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# ── ดึงรายการคดี ──────────────────────────────────────────────────────────
@app.get("/get-cases/{case_type}", tags=["Cases"])
async def get_cases(
    case_type: str,
    page:      int  = Query(1,   ge=1),
    limit:     int  = Query(50,  ge=1, le=200),
    search:    str  = Query(""),
    finished:  Optional[bool] = Query(None),
    db: Client = Depends(get_db),
):
    table = get_table(case_type)
    try:
        offset = (page - 1) * limit
        q = db.table(table).select("*", count="exact")
        if finished is not None:
            q = q.eq("is_finished", finished)
        if search:
            q = q.ilike("location", f"%{search}%")
        res = q.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        return {
            "data":  res.data  if res.data  else [],
            "total": res.count if res.count else 0,
            "page":  page,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(500, str(e))

# ── ลบคดี ─────────────────────────────────────────────────────────────────
@app.delete("/delete-case/{case_type}/{case_no}", tags=["Cases"])
async def delete_case(
    case_type: str,
    case_no:   str,
    db: Client = Depends(get_db),
):
    table = get_table(case_type)
    try:
        db.table(table).delete().eq("case_no", case_no).execute()
        return {"success": True, "message": f"ลบคดี {case_no} เรียบร้อย"}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── UTM Converter endpoint ────────────────────────────────────────────────
@app.get("/convert-utm", tags=["Utils"])
def convert_utm_api(
    zone:     int   = Query(47),
    easting:  float = Query(...),
    northing: float = Query(...),
    is_north: bool  = Query(True),
):
    try:
        result = utm_to_latlon(zone, easting, northing, is_north)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(400, f"แปลงพิกัดไม่ได้: {e}")

# ── Reload schema cache ───────────────────────────────────────────────────
@app.post("/reload-schema/", tags=["Admin"])
async def reload_schema(db: Client = Depends(get_db)):
    clear_schema_cache()
    try:
        db.rpc("pg_notify", {"channel": "pgrst", "payload": "reload schema"}).execute()
        return {"success": True, "message": "Reload schema สำเร็จ"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ── Debug endpoint ────────────────────────────────────────────────────────
@app.post("/debug-shp/", tags=["Debug"])
async def debug_shp(
    shp_file: UploadFile = File(...),
    dbf_file: UploadFile = File(...),
    shx_file: UploadFile = File(...),
    prj_file: Optional[UploadFile] = File(None),
    cpg_file: Optional[UploadFile] = File(None),
):
    """ตรวจสอบ Shapefile แยกไฟล์แบบละเอียด"""
    result: dict = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shp_path = await assemble_shapefile(
                tmp, shp_file, dbf_file, shx_file, prj_file, cpg_file
            )
            result["shp_path"]    = shp_path
            result["files_found"] = os.listdir(tmp)

            try:
                gdf = gpd.read_file(shp_path)
                result["gdf_len"]         = len(gdf)
                result["gdf_crs"]         = str(gdf.crs)
                result["gdf_columns"]     = list(gdf.columns)
                result["geom_null_count"] = int(gdf.geometry.isna().sum())
                if len(gdf) and not gdf.geometry.isna().all():
                    gdf84 = gdf.to_crs(epsg=4326)
                    c = gdf84.geometry.centroid.iloc[0]
                    result["centroid_lat"] = round(float(c.y), 6)
                    result["centroid_lon"] = round(float(c.x), 6)
            except Exception as eg:
                result["gpd_error"] = str(eg)

            try:
                pipeline = extract_gis_from_shp(shp_path)
                result["pipeline"] = {
                    "lat":          pipeline["lat"],
                    "lon":          pipeline["lon"],
                    "rai":          pipeline["rai"],
                    "ngarn":        pipeline["ngarn"],
                    "wa":           pipeline["wa"],
                    "utm_zone":     pipeline["utm_zone"],
                    "utm_easting":  pipeline["utm_easting"],
                    "utm_northing": pipeline["utm_northing"],
                    "gdf_len":      len(pipeline["gdf84"]),
                }
            except Exception as ep:
                result["pipeline_error"]     = str(ep)
                result["pipeline_traceback"] = traceback.format_exc()

        result["success"] = True
    except Exception as e:
        result["success"]   = False
        result["error"]     = str(e)
        result["traceback"] = traceback.format_exc()
    return result

# ─────────────────────────────────────────────────
# 12. GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    log.exception(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error"},
    )
