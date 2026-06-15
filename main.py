"""
DNP GIS Case API — Production v4.0
ระบบ API สารบบคดีเชิงพื้นที่ สบอ.13 ลำปาง
แก้ไข: CORS, response format, pagination, schema cache, error handling
"""

import os, shutil, tempfile, zipfile, json, math, time, logging
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
    version="4.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────
# VALUE ใน Render ต้องเป็น: https://natthawutfrdo.github.io,http://localhost:3000
# ไม่ต้องใส่ ALLOWED_ORIGINS= นำหน้า
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

# ── GZip ─────────────────────────────────────────
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
    """Dependency: ตรวจสอบ Supabase ก่อนใช้งาน"""
    if not supabase:
        raise HTTPException(503, detail="ฐานข้อมูลยังไม่ได้เชื่อมต่อ")
    return supabase

# ─────────────────────────────────────────────────
# 4. SCHEMA CACHE (ตรวจคอลัมน์ใหม่)
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
# ─────────────────────────────────────────────────
async def validate_file(
    file: UploadFile,
    allowed_ext: str,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(400, f"ไฟล์ต้องเป็นนามสกุล .{allowed_ext}")
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

    shp_files = [
        os.path.join(root, f)
        for root, _, files in os.walk(extract_dir)
        for f in files if f.endswith(".shp")
    ]
    if not shp_files:
        raise HTTPException(400, "ไม่พบไฟล์ .shp ในไฟล์ ZIP")

    try:
        gdf = gpd.read_file(shp_files[0])
    except Exception as e:
        raise HTTPException(400, f"เปิด Shapefile ไม่ได้: {e}")

    if gdf.empty:
        raise HTTPException(400, "Shapefile ไม่มีข้อมูลเชิงพื้นที่")

    # ตั้ง CRS ถ้าไม่มี
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=32647)

    # แปลงเป็น WGS84
    try:
        gdf84 = gdf.to_crs(epsg=4326)
        c = gdf84.geometry.centroid.iloc[0]
        lat, lon = float(c.y), float(c.x)
    except Exception:
        lat, lon = 18.29, 99.50
        gdf84 = gdf

    # คำนวณพื้นที่ (ตร.ม. → ไร่-งาน-ตร.ว.)
    try:
        area_sqm = max(float(gdf.to_crs(epsg=32647).geometry.area.sum()), 0.0)
    except Exception:
        area_sqm = 0.0

    total_wa = area_sqm / 4.0
    rai   = int(total_wa // 400)
    ngarn = int((total_wa % 400) // 100)
    wa    = int(round(total_wa % 100))

    # ลด complexity ของ geometry
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
def upload_to_storage(db: Client, bucket: str, path: str, file_path: str, content_type: str = "application/octet-stream") -> str:
    """อัปโหลดไฟล์ขึ้น Supabase Storage แล้วคืน public URL"""
    with open(file_path, "rb") as f:
        db.storage.from_(bucket).upload(
            path=path, file=f,
            file_options={"cache-control": "3600", "upsert": True, "content-type": content_type},
        )
    return db.storage.from_(bucket).get_public_url(path)

# ─────────────────────────────────────────────────
# 10. ENDPOINTS
# ─────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def root():
    return {"message": "DNP GIS Case API v4.0 is running!", "status": "ok", "version": "4.0"}

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
    await validate_file(file, ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "upload.zip")
        with open(zip_path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        gis = extract_gis(zip_path, os.path.join(tmp, "ex"))
    return {
        "success": True,
        "lat": gis["lat"], "lon": gis["lon"],
        "rai": gis["rai"], "ngarn": gis["ngarn"], "wa": gis["wa"],
        "utm_zone": gis["utm_zone"],
        "utm_easting": gis["utm_easting"],
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
    # ── Validate ──
    get_table(case_type)  # ตรวจ case_type
    await validate_file(file, ".zip")
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, ".pdf", max_bytes=20 * 1024 * 1024)

    primary_key = complaint_no or criminal_no or "unknown"
    safe_key = primary_key.replace("/", "_").replace(" ", "_")

    # ── ตรวจ schema คอลัมน์ใหม่ ──
    new_cols = (case_type == "encroachment") and check_columns(
        "encroachment_cases", ["complaint_no", "criminal_no", "seizure_no"]
    )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            # ── 1. Extract GIS ──
            zip_path = os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as buf:
                shutil.copyfileobj(file.file, buf)
            gis = extract_gis(zip_path, os.path.join(tmp, "ex"))

            # ── 2. ลำดับความสำคัญของค่า (Form > GIS) ──
            f_zone     = utm_zone     if utm_easting != 0 else gis["utm_zone"]
            f_easting  = utm_easting  if utm_easting != 0 else gis["utm_easting"]
            f_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]
            f_rai   = int(rai)       if rai   > 0 else gis["rai"]
            f_ngarn = int(ngarn)     if ngarn > 0 else gis["ngarn"]
            f_wa    = int(round(wa)) if wa    > 0 else gis["wa"]
            coords  = [gis["lat"], gis["lon"]]

            # ── 3. อัปโหลด Shapefile ZIP ──
            clean_shp = f"{safe_key}_{file.filename.replace(' ', '_')}"
            try:
                shp_url = upload_to_storage(db, "dnp-shapefiles", clean_shp, zip_path)
            except Exception as e:
                return JSONResponse({"success": False, "error": f"อัปโหลด Shapefile ล้มเหลว: {e}"})

            # ── 4. อัปโหลด PDF ──
            pdf_url = ""
            if pdf_file and pdf_file.filename:
                pdf_fn = f"{safe_key}_{pdf_file.filename.replace(' ', '_')}"
                pdf_tmp = os.path.join(tmp, pdf_fn)
                with open(pdf_tmp, "wb") as buf:
                    shutil.copyfileobj(pdf_file.file, buf)
                try:
                    pdf_url = upload_to_storage(db, "dnp-pdfs", pdf_fn, pdf_tmp, "application/pdf")
                except Exception as e:
                    log.warning(f"PDF upload failed: {e}")

            # ── 5. สร้างและอัปโหลด GeoJSON ──
            geojson_fn   = f"{safe_key}_map.json"
            geojson_path = os.path.join(tmp, geojson_fn)
            with open(geojson_path, "w", encoding="utf-8") as jf:
                jf.write(gis["gdf84"].to_json())
            try:
                geojson_url = upload_to_storage(db, "dnp-shapefiles", geojson_fn, geojson_path, "application/json")
            except Exception as e:
                return JSONResponse({"success": False, "error": f"สร้าง GeoJSON ล้มเหลว: {e}"})

            is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")

            # ── 6. บันทึก DB ──
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
                        # Fallback: ยุบเลขคดีไว้ใน case_status
                        extra = ""
                        if complaint_no: extra += f"\n[แจ้ง: {complaint_no}]"
                        if criminal_no:  extra += f"\n[อาญา: {criminal_no}]"
                        if seizure_no:   extra += f"\n[ยึดทรัพย์: {seizure_no}]"
                        if extra: row["case_status"] = case_status + extra
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
    # ── PDF ──
    pdf_url = ""
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, ".pdf", max_bytes=20 * 1024 * 1024)
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
    coords = [coords_lat, coords_lon] if (coords_lat or coords_lon) else [18.29, 99.50]

    # ── แปลงพิกัดไขว้ ──
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
            "success": True,
            "message": "บันทึกคดีสัตว์ป่าสำเร็จ",
            "coords":  coords,
            "utm_zone": f_zone, "utm_easting": f_east, "utm_northing": f_north,
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})

# ── ดึงรายการคดี (แก้ response format) ──────────
@app.get("/get-cases/{case_type}", tags=["Cases"])
async def get_cases(
    case_type: str,
    page:     int   = Query(1,   ge=1),
    limit:    int   = Query(50,  ge=1, le=200),
    search:   str   = Query(""),
    finished: Optional[bool] = Query(None),
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

        # ── ✅ FIX: คืน {data, total, page, limit} เสมอ ──
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
# 11. GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    log.exception(f"Unhandled error: {exc}")
    return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})
