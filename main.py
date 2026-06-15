"""
DNP GIS Case API — Production v4.1
ระบบ API สารบบคดีเชิงพื้นที่ สบอ.13 ลำปาง
แก้ไข: CORS, response format, pagination, schema cache, error handling
🔧 v4.1: แก้ validate_file นามสกุลไฟล์ (เพิ่มจุดนำหน้า ext)
🔧 v4.1: เพิ่ม /debug-multipolygon/ endpoint สำหรับ debug MultiPolygon
"""
 
import os, shutil, tempfile, zipfile, json, math, time, logging, glob, traceback
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
    version="4.1",
    docs_url="/docs",
    redoc_url="/redoc",
)
 
# ── CORS ──────────────────────────────────────────
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
SCHEMA_TTL = 300  # 5 นาที
 
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
# 🔧 FIX: normalize ext ให้มีจุดเสมอ ไม่ว่าจะส่งมาว่า "zip" หรือ ".zip"
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
 
# ─────────────────────────────────────────────────
# 7. UTM / LAT-LON HELPERS
# ─────────────────────────────────────────────────
def utm_to_latlon(zone: int, easting: float, northing: float, is_north: bool = True) -> dict:
    """แปลง UTM (WGS84) → Lat/Lon"""
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
    """แปลง Lat/Lon → UTM (WGS84)"""
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
# 8. GIS EXTRACTION
# ─────────────────────────────────────────────────
def extract_gis(zip_path: str, extract_dir: str) -> dict:
    """แตก Shapefile จาก ZIP แล้วคำนวณพิกัดกลาง + พื้นที่"""
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)
    except Exception:
        raise HTTPException(400, "ไฟล์ ZIP ไม่สมบูรณ์หรือเสียหาย")
 
    shp_files = glob.glob(os.path.join(extract_dir, "**", "*.shp"), recursive=True)
    if not shp_files:
        shp_files = glob.glob(os.path.join(extract_dir, "**", "*.SHP"), recursive=True)
    if not shp_files:
        raise HTTPException(400, "ไม่พบไฟล์ .shp ในไฟล์ ZIP")
 
    # copy ไฟล์ทั้งหมดไปไว้ใน safe_dir ที่ชื่อ ASCII ล้วน
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
 
    # ── อ่านด้วย fiona โดยตรง (รองรับ fiona 1.9+) ────────────────
    try:
        import fiona
        from shapely.geometry import shape
        from shapely.validation import make_valid
 
        with fiona.open(shp_path, encoding="utf-8") as src:
            crs_wkt = src.crs_wkt
            features = []
            for feat in src:
                # fiona 1.9+ ใช้ feat["geometry"] แทน feat.geometry
                raw_geom = feat["geometry"] if "geometry" in feat else getattr(feat, "geometry", None)
                if raw_geom is not None:
                    try:
                        geom = shape(raw_geom)
                        # ซ่อม geometry ที่ invalid (เช่น self-intersection)
                        if not geom.is_valid:
                            geom = make_valid(geom)
                    except Exception as eg:
                        log.warning(f"[GIS] shape() error: {eg}")
                        geom = None
                else:
                    geom = None
 
                props = dict(feat.properties) if hasattr(feat, "properties") else {}
                features.append({"geometry": geom, **props})
 
            log.info(f"[GIS] fiona ok: {len(features)} features, crs={src.crs}")
 
        if not features:
            raise HTTPException(400, "Shapefile ไม่มีข้อมูล (0 features)")
 
        gdf = gpd.GeoDataFrame(features, geometry="geometry")
        if crs_wkt:
            gdf = gdf.set_crs(crs_wkt, allow_override=True)
        elif gdf.crs is None:
            gdf = gdf.set_crs(epsg=32647)
            log.info("[GIS] CRS ไม่พบ — ใช้ EPSG:32647")
 
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[GIS] fiona failed: {e}")
        raise HTTPException(400, f"เปิด Shapefile ไม่ได้: {e}")
 
    if gdf.geometry.isna().all():
        raise HTTPException(400, "Shapefile มีข้อมูลแต่ geometry ว่างทั้งหมด")
 
    # ── แปลงพิกัด → WGS84 ────────────────────────
    try:
        gdf84 = gdf.to_crs(epsg=4326)
        c = gdf84.geometry.centroid.iloc[0]
        lat, lon = float(c.y), float(c.x)
    except Exception:
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
 
    # ── Simplify สำหรับ GeoJSON ────────────────────
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
# 9. STORAGE UPLOAD HELPER
# ─────────────────────────────────────────────────
def upload_to_storage(
    db: Client,
    bucket: str,
    path: str,
    file_path: str,
    content_type: str = "application/octet-stream",
) -> str:
    """อัปโหลดไฟล์ขึ้น Supabase Storage แล้วคืน public URL"""
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
 
# ─────────────────────────────────────────────────
# 10. ENDPOINTS
# ─────────────────────────────────────────────────
 
@app.get("/", tags=["Health"])
def root():
    return {
        "message": "DNP GIS Case API v4.1 is running!",
        "status": "ok",
        "version": "4.1",
    }
 
@app.get("/health", tags=["Health"])
def health():
    return {
        "api": "ok",
        "db": "connected" if supabase else "disconnected",
        "cors_origins": ALLOWED_ORIGINS,
    }
 
# ── วิเคราะห์ Shapefile (ไม่บันทึก DB) ──────────
@app.post("/analyze-shapefile/", tags=["GIS"])
async def analyze_shapefile(file: UploadFile = File(...)):
    content = await validate_file(file, "zip")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "upload.zip")
        with open(zip_path, "wb") as buf:
            buf.write(content)
        gis = extract_gis(zip_path, os.path.join(tmp, "ex"))
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
 
# ── บันทึกคดีบุกรุก / ไม้ ───────────────────────
@app.post("/process-shapefile/", tags=["Cases"])
async def process_shapefile(
    file:           UploadFile = File(...),
    pdf_file:       UploadFile = File(None),
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
    zip_content = await validate_file(file, "zip")
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, "pdf", max_bytes=20 * 1024 * 1024)
 
    primary_key = complaint_no or criminal_no or "unknown"
    safe_key    = primary_key.replace("/", "_").replace(" ", "_")
 
    new_cols = (case_type == "encroachment") and check_columns(
        "encroachment_cases", ["complaint_no", "criminal_no", "seizure_no"]
    )
 
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as buf:
                buf.write(zip_content)
            gis = extract_gis(zip_path, os.path.join(tmp, "ex"))
 
            f_zone     = utm_zone     if utm_easting != 0 else gis["utm_zone"]
            f_easting  = utm_easting  if utm_easting != 0 else gis["utm_easting"]
            f_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]
            f_rai      = int(rai)       if rai   > 0 else gis["rai"]
            f_ngarn    = int(ngarn)     if ngarn > 0 else gis["ngarn"]
            f_wa       = int(round(wa)) if wa    > 0 else gis["wa"]
            coords     = [gis["lat"], gis["lon"]]
 
            clean_shp = f"{safe_key}_{file.filename.replace(' ', '_')}"
            try:
                shp_url = upload_to_storage(db, "dnp-shapefiles", clean_shp, zip_path)
            except Exception as e:
                return JSONResponse({"success": False, "error": f"อัปโหลด Shapefile ล้มเหลว: {e}"})
 
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
                        "shapefile_url":  shp_url,
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
 
# ── บันทึกคดีสัตว์ป่า ───────────────────────────
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
 
# ── ดึงรายการคดี ─────────────────────────────────
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
 
# ── ลบคดี ────────────────────────────────────────
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
 
# ── UTM Converter endpoint ────────────────────────
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
 
# ── Reload schema cache ───────────────────────────
@app.post("/reload-schema/", tags=["Admin"])
async def reload_schema(db: Client = Depends(get_db)):
    clear_schema_cache()
    try:
        db.rpc("pg_notify", {"channel": "pgrst", "payload": "reload schema"}).execute()
        return {"success": True, "message": "Reload schema สำเร็จ"}
    except Exception as e:
        return {"success": False, "message": str(e)}
 
# ─────────────────────────────────────────────────
# 11. DEBUG ENDPOINTS
# ─────────────────────────────────────────────────
 
@app.post("/debug-zip/", tags=["Debug"])
async def debug_zip(file: UploadFile = File(...)):
    """ตรวจสอบโครงสร้างไฟล์ ZIP + Shapefile โดยละเอียด"""
    result = {}
    try:
        content = await file.read()
        result["file_size_kb"] = round(len(content) / 1024, 1)
        result["filename"]     = file.filename
 
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as f:
                f.write(content)
 
            with zipfile.ZipFile(zip_path) as z:
                result["zip_contents"] = z.namelist()
 
            ex_dir = os.path.join(tmp, "ex")
            os.makedirs(ex_dir)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(ex_dir)
 
            shp_files  = glob.glob(os.path.join(ex_dir, "**", "*.shp"), recursive=True)
            shp_files += glob.glob(os.path.join(ex_dir, "**", "*.SHP"), recursive=True)
            result["shp_found"] = shp_files
 
            if shp_files:
                gdf = gpd.read_file(shp_files[0])
                result["gdf_columns"]      = list(gdf.columns)
                result["gdf_dtypes"]       = {c: str(gdf[c].dtype) for c in gdf.columns}
                result["gdf_crs"]          = str(gdf.crs)
                result["gdf_empty"]        = gdf.empty
                result["gdf_len"]          = len(gdf)
                result["geometry_col"]     = gdf.geometry.name if not gdf.empty else "N/A"
                if not gdf.empty:
                    result["first_geom_type"]  = str(type(gdf.iloc[0].geometry).__name__) if gdf.iloc[0].geometry else "None"
                    result["geom_null_count"]  = int(gdf.geometry.isna().sum())
 
        result["success"] = True
    except Exception as e:
        result["success"]   = False
        result["error"]     = str(e)
        result["traceback"] = traceback.format_exc()
    return result
 
 
@app.post("/debug-multipolygon/", tags=["Debug"])
async def debug_multipolygon(file: UploadFile = File(...)):
    """
    Debug MultiPolygon แบบละเอียดทีละขั้น
    ตรวจสอบว่า fiona อ่าน geometry ได้ถูกต้องหรือไม่
    เปรียบเทียบ feat.geometry vs feat["geometry"] (fiona 1.9+)
    """
    import fiona
    from shapely.geometry import shape
    from shapely.validation import make_valid
 
    result: dict = {}
    try:
        content = await file.read()
        result["file_size_kb"] = round(len(content) / 1024, 1)
        result["filename"]     = file.filename
 
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "up.zip")
            with open(zip_path, "wb") as f:
                f.write(content)
 
            # ── ตรวจ ZIP ──────────────────────────────────────
            with zipfile.ZipFile(zip_path) as z:
                result["zip_contents"] = z.namelist()
 
            ex = os.path.join(tmp, "ex")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(ex)
 
            shps  = glob.glob(os.path.join(ex, "**", "*.shp"), recursive=True)
            shps += glob.glob(os.path.join(ex, "**", "*.SHP"), recursive=True)
            result["shp_found"] = shps
 
            if not shps:
                result["success"] = False
                result["error"]   = "ไม่พบไฟล์ .shp ใน ZIP"
                return result
 
            shp = shps[0]
            result["shp_path"] = shp
 
            # ── ทดสอบ fiona โดยตรง ────────────────────────────
            fiona_result = {}
            try:
                with fiona.open(shp, encoding="utf-8") as src:
                    fiona_result["driver"]      = src.driver
                    fiona_result["crs"]         = str(src.crs)
                    fiona_result["crs_wkt"]     = src.crs_wkt[:120] if src.crs_wkt else None
                    fiona_result["schema"]      = str(src.schema)
                    fiona_result["feature_count"] = len(src)
                    fiona_result["fiona_version"] = fiona.__version__
 
                    if len(src) == 0:
                        fiona_result["warning"] = "ไม่มี feature ในไฟล์"
                    else:
                        feat = next(iter(src))
 
                        # ── keys ที่มีใน feature object ──────────
                        fiona_result["feat_keys"] = list(feat.keys())
 
                        # ── เปรียบเทียบ 2 วิธีเข้าถึง geometry ──
                        # วิธี 1: feat["geometry"]  (fiona 1.9+ preferred)
                        geom_bracket = feat["geometry"] if "geometry" in feat else "KEY_NOT_FOUND"
                        # วิธี 2: feat.geometry  (fiona < 1.9 / legacy)
                        geom_attr    = getattr(feat, "geometry", "ATTR_NOT_FOUND")
 
                        fiona_result["geom_via_bracket_type"]   = type(geom_bracket).__name__
                        fiona_result["geom_via_bracket_is_none"] = geom_bracket is None
                        fiona_result["geom_via_attr_type"]      = type(geom_attr).__name__
                        fiona_result["geom_via_attr_is_none"]   = geom_attr is None
 
                        # preview ค่า geometry dict
                        if geom_bracket and hasattr(geom_bracket, "get"):
                            fiona_result["geom_type_field"]      = geom_bracket.get("type")
                            coord_sample = geom_bracket.get("coordinates")
                            if coord_sample:
                                # แสดงแค่ 1 จุดแรกเพื่อไม่ให้ response ใหญ่เกิน
                                fiona_result["geom_coord_sample"] = str(coord_sample)[:300]
                        elif isinstance(geom_bracket, str):
                            fiona_result["geom_bracket_str"] = geom_bracket
 
                        # ── ทดสอบ shapely shape() ─────────────────
                        shapely_result = {}
                        raw = geom_bracket if geom_bracket is not None else geom_attr
                        if raw is not None:
                            try:
                                shp_geom = shape(raw)
                                shapely_result["geom_type"]   = shp_geom.geom_type
                                shapely_result["is_valid"]    = shp_geom.is_valid
                                shapely_result["is_empty"]    = shp_geom.is_empty
                                shapely_result["area_deg2"]   = shp_geom.area   # ยังไม่แปลงหน่วย
                                if not shp_geom.is_valid:
                                    shapely_result["validity_reason"] = str(shp_geom.is_valid)
                                    fixed = make_valid(shp_geom)
                                    shapely_result["make_valid_type"]  = fixed.geom_type
                                    shapely_result["make_valid_valid"] = fixed.is_valid
                            except Exception as e_shp:
                                shapely_result["error"] = str(e_shp)
                                shapely_result["traceback"] = traceback.format_exc()
                        else:
                            shapely_result["error"] = "ไม่ได้ geometry จากทั้ง 2 วิธี"
 
                        fiona_result["shapely_test"] = shapely_result
 
            except Exception as e_fiona:
                fiona_result["error"]     = str(e_fiona)
                fiona_result["traceback"] = traceback.format_exc()
 
            result["fiona"] = fiona_result
 
            # ── ทดสอบ geopandas อ่านตรง ──────────────────────
            gpd_result = {}
            try:
                gdf = gpd.read_file(shp)
                gpd_result["len"]             = len(gdf)
                gpd_result["columns"]         = list(gdf.columns)
                gpd_result["geometry_col"]    = gdf.geometry.name
                gpd_result["crs"]             = str(gdf.crs)
                gpd_result["geom_null_count"] = int(gdf.geometry.isna().sum())
                if len(gdf):
                    g0 = gdf.iloc[0].geometry
                    gpd_result["first_geom_type"]  = str(type(g0).__name__) if g0 else "None"
                    gpd_result["first_geom_valid"] = bool(g0.is_valid) if g0 else False
                    gpd_result["first_geom_empty"] = bool(g0.is_empty) if g0 else True
                    # ทดสอบ to_crs
                    try:
                        gdf84 = gdf.to_crs(epsg=4326)
                        c = gdf84.geometry.centroid.iloc[0]
                        gpd_result["centroid_lat"] = round(float(c.y), 6)
                        gpd_result["centroid_lon"] = round(float(c.x), 6)
                    except Exception as e_crs:
                        gpd_result["to_crs_error"] = str(e_crs)
            except Exception as e_gpd:
                gpd_result["error"]     = str(e_gpd)
                gpd_result["traceback"] = traceback.format_exc()
 
            result["geopandas"] = gpd_result
 
            # ── ทดสอบ extract_gis (pipeline จริง) ────────────
            pipeline_result = {}
            try:
                gis = extract_gis(zip_path, os.path.join(tmp, "pipeline_ex"))
                pipeline_result["success"]      = True
                pipeline_result["lat"]          = gis["lat"]
                pipeline_result["lon"]          = gis["lon"]
                pipeline_result["rai"]          = gis["rai"]
                pipeline_result["ngarn"]        = gis["ngarn"]
                pipeline_result["wa"]           = gis["wa"]
                pipeline_result["utm_zone"]     = gis["utm_zone"]
                pipeline_result["utm_easting"]  = gis["utm_easting"]
                pipeline_result["utm_northing"] = gis["utm_northing"]
                pipeline_result["gdf_len"]      = len(gis["gdf84"])
            except HTTPException as e_http:
                pipeline_result["success"] = False
                pipeline_result["error"]   = e_http.detail
            except Exception as e_pipe:
                pipeline_result["success"]   = False
                pipeline_result["error"]     = str(e_pipe)
                pipeline_result["traceback"] = traceback.format_exc()
 
            result["pipeline"] = pipeline_result
 
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
