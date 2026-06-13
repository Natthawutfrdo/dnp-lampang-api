"""
DNP GIS Case API — improved version
ปรับปรุง: cache schema check, input validation, pagination, CORS จำกัด origin,
file size limit, async upload, better error handling
"""
import os
import shutil
import tempfile
import zipfile
import json
import math
import time
from typing import Optional
from functools import lru_cache

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import geopandas as gpd
from supabase import create_client, Client

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="DNP GIS Case API Systems", version="3.2")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://your-frontend-domain.com,http://localhost:3000,http://127.0.0.1:5500"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # จำกัด origin แทน "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)  # บีบอัด response อัตโนมัติ

# ─────────────────────────────────────────────────────────────
# Supabase client
# ─────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"❌ Supabase init error: {e}")

# ─────────────────────────────────────────────────────────────
# Schema cache — ตรวจสอบครั้งเดียว แคชผล
# ─────────────────────────────────────────────────────────────
_schema_cache: dict[str, tuple[bool, float]] = {}
SCHEMA_CACHE_TTL = 300  # 5 นาที

def check_columns_exist_cached(table: str, columns: list[str]) -> bool:
    cache_key = f"{table}:{','.join(sorted(columns))}"
    now = time.time()
    if cache_key in _schema_cache:
        result, ts = _schema_cache[cache_key]
        if now - ts < SCHEMA_CACHE_TTL:
            return result
    if not supabase:
        return False
    try:
        supabase.table(table).select(",".join(columns)).limit(0).execute()
        _schema_cache[cache_key] = (True, now)
        return True
    except Exception:
        _schema_cache[cache_key] = (False, now)
        return False

def invalidate_schema_cache():
    _schema_cache.clear()

# ─────────────────────────────────────────────────────────────
# File validation
# ─────────────────────────────────────────────────────────────
MAX_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

async def validate_file(file: UploadFile, allowed_ext: str, max_bytes: int = MAX_BYTES):
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(400, f"ต้องเป็นไฟล์ .{allowed_ext} เท่านั้น")
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(413, f"ไฟล์ใหญ่เกิน {MAX_FILE_SIZE_MB} MB")
    await file.seek(0)
    return content

# ─────────────────────────────────────────────────────────────
# UTM helpers (ไม่เปลี่ยนจาก original)
# ─────────────────────────────────────────────────────────────
def utm_to_latlon_python(zone: int, easting: float, northing: float,
                          is_north: bool = True) -> dict:
    k0 = 0.9996; a = 6378137.0; e = 0.0818191908426215
    e2=e*e; e4=e2*e2; e6=e4*e2; e1sq=e2/(1-e2)
    x = easting - 500000.0
    y = northing if is_north else northing - 10000000.0
    lon_origin = (zone-1)*6 - 180 + 3
    M = y/k0
    mu = M/(a*(1-e2/4-3*e4/64-5*e6/256))
    phi1 = (mu
        + (3/2*e2+27/32*e4+55/512*e6)*math.sin(2*mu)
        + (21/16*e4+55/32*e6)*math.sin(4*mu)
        + (151/96*e6)*math.sin(6*mu))
    N1=a/math.sqrt(1-e2*math.sin(phi1)**2)
    T1=math.tan(phi1)**2; C1=e1sq*math.cos(phi1)**2
    R1=a*(1-e2)/(1-e2*math.sin(phi1)**2)**1.5
    D=x/(N1*k0)
    lat=phi1-(N1*math.tan(phi1)/R1)*(
        D**2/2-(5+3*T1+10*C1-4*C1**2-9*e1sq)*D**4/24
        +(61+90*T1+298*C1+45*T1**2-252*e1sq-3*C1**2)*D**6/720)
    lon=(D-(1+2*T1+C1)*D**3/6
        +(5-2*C1+28*T1-3*C1**2+8*e1sq+24*T1**2)*D**5/120)/math.cos(phi1)
    return {"lat": math.degrees(lat), "lon": lon_origin+math.degrees(lon)}

def latlon_to_utm(lat: float, lon: float) -> dict:
    try:
        zone=int((lon+180)/6)+1
        k0=0.9996; a=6378137.0; e=0.0818191908426215
        e2=e*e; e4=e2*e2; e6=e4*e2; e1sq=e2/(1-e2)
        lat_rad=math.radians(lat); lon_rad=math.radians(lon)
        lon_origin_rad=math.radians((zone-1)*6-180+3)
        N=a/math.sqrt(1-e2*math.sin(lat_rad)**2)
        T=math.tan(lat_rad)**2; C=e1sq*math.cos(lat_rad)**2
        A=math.cos(lat_rad)*(lon_rad-lon_origin_rad)
        M=a*((1-e2/4-3*e4/64-5*e6/256)*lat_rad
            -(3*e2/8+3*e4/32+45*e6/1024)*math.sin(2*lat_rad)
            +(15*e4/256+45*e6/1024)*math.sin(4*lat_rad)
            -(35*e6/3072)*math.sin(6*lat_rad))
        easting=k0*N*(A+(1-T+C)*A**3/6+(5-18*T+T**2+72*C-58*e1sq)*A**5/120)+500000.0
        northing=k0*(M+N*math.tan(lat_rad)*(A**2/2+(5-T+9*C+4*C**2)*A**4/24
            +(61-58*T+T**2+600*C-330*e1sq)*A**6/720))
        if lat < 0: northing += 10000000.0
        return {"utm_zone": zone, "utm_easting": int(round(easting)), "utm_northing": int(round(northing))}
    except Exception as err:
        print(f"⚠️ latlon→UTM error: {err}")
        return {"utm_zone": 47, "utm_easting": 0, "utm_northing": 0}

# ─────────────────────────────────────────────────────────────
# GIS extraction helper
# ─────────────────────────────────────────────────────────────
def extract_gis_and_calculate(zip_path: str, extract_dir: str):
    try:
        with zipfile.ZipFile(zip_path, 'r') as zr:
            zr.extractall(extract_dir)
        shp_files = [
            os.path.join(root, f)
            for root, _, files in os.walk(extract_dir)
            for f in files if f.endswith('.shp')
        ]
        if not shp_files:
            raise HTTPException(400, "ไม่พบไฟล์ .shp ในไฟล์ Zip")
        try:
            gdf = gpd.read_file(shp_files[0])
        except Exception as e:
            raise HTTPException(400, f"เปิด Shapefile ไม่ได้: {e}")
        if gdf.empty:
            raise HTTPException(400, "Shapefile ไม่มีข้อมูลเชิงพื้นที่")
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=32647)
        try:
            gdf_wgs84 = gdf.to_crs(epsg=4326)
            c = gdf_wgs84.geometry.centroid.iloc[0]
            lat_val, lon_val = float(c.y), float(c.x)
        except Exception:
            lat_val, lon_val = 18.29, 99.50
            gdf_wgs84 = gdf
        try:
            area_sqm = float(gdf.to_crs(epsg=32647).geometry.area.sum())
            area_sqm = max(area_sqm, 0.0)
        except Exception:
            area_sqm = 0.0
        total_wa = area_sqm / 4.0
        rai = int(total_wa // 400)
        ngarn = int((total_wa % 400) // 100)
        wa = int(round(total_wa % 100))
        utm = latlon_to_utm(lat_val, lon_val)
        return {
            "gdf_wgs84": gdf_wgs84,
            "coords": [lat_val, lon_val],
            "rai": rai, "ngarn": ngarn, "wa": wa,
            **utm
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"ระบบ GIS ขัดข้อง: {e}")

# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────
@app.get("/")
def read_root():
    return {"message": "DNP GIS Case API v3.2 is running!", "status": "ok"}

@app.get("/health")
def health():
    return {"db": "ok" if supabase else "disconnected"}

# ── Analyze shapefile ──
@app.post("/analyze-shapefile/")
async def analyze_shapefile(file: UploadFile = File(...)):
    await validate_file(file, ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "upload.zip")
        with open(zip_path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        extract_dir = os.path.join(tmp, "ex")
        os.makedirs(extract_dir, exist_ok=True)
        gis = extract_gis_and_calculate(zip_path, extract_dir)
        return {
            "success": True,
            "lat": gis["coords"][0], "lon": gis["coords"][1],
            "rai": gis["rai"], "ngarn": gis["ngarn"], "wa": gis["wa"],
            "utm_zone": gis["utm_zone"],
            "utm_easting": gis["utm_easting"],
            "utm_northing": gis["utm_northing"],
        }

# ── Process shapefile (บุกรุก / ไม้) ──
@app.post("/process-shapefile/")
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
    width:          float = Form(0.0),
    length:         float = Form(0.0),
    size:           float = Form(0.0),
    vol1:           float = Form(0.0),
    vol2:           float = Form(0.0),
    utm_zone:       int   = Form(47),
    utm_easting:    int   = Form(0),
    utm_northing:   int   = Form(0),
):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อ Supabase")
    if case_type not in ("encroachment", "timber"):
        raise HTTPException(400, "case_type ต้องเป็น encroachment หรือ timber")

    # Validate files
    await validate_file(file, ".zip")
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, ".pdf", max_bytes=20*1024*1024)

    primary_key = complaint_no or criminal_no or "unknown"
    safe_key = primary_key.replace('/', '_').replace(' ', '_')

    # Check schema (cached)
    new_cols_exist = False
    if case_type == "encroachment":
        new_cols_exist = check_columns_exist_cached(
            "encroachment_cases", ["complaint_no", "criminal_no", "seizure_no"]
        )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as buf:
                shutil.copyfileobj(file.file, buf)
            extract_dir = os.path.join(tmp, "ex")
            os.makedirs(extract_dir, exist_ok=True)

            gis = extract_gis_and_calculate(zip_path, extract_dir)
            gdf_wgs84 = gis["gdf_wgs84"]
            calculated_coords = gis["coords"]

            # ใช้ค่าจาก form ถ้ามี ไม่งั้นใช้จาก shapefile
            final_utm_zone     = utm_zone     if utm_easting  != 0 else gis["utm_zone"]
            final_utm_easting  = utm_easting  if utm_easting  != 0 else gis["utm_easting"]
            final_utm_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]
            final_rai          = int(rai)       if rai   > 0 else gis["rai"]
            final_ngarn        = int(ngarn)     if ngarn > 0 else gis["ngarn"]
            final_wa           = int(round(wa)) if wa    > 0 else gis["wa"]

            # Simplify geometry ลดขนาด GeoJSON
            try:
                gdf_wgs84['geometry'] = gdf_wgs84['geometry'].simplify(
                    tolerance=0.0001, preserve_topology=True
                )
            except Exception:
                pass

            clean_fn = f"{safe_key}_{file.filename.replace(' ', '_')}"

            # Upload shapefile zip
            try:
                with open(zip_path, "rb") as fd:
                    supabase.storage.from_("dnp-shapefiles").upload(
                        path=clean_fn, file=fd,
                        file_options={"cache-control": "3600", "upsert": "true"}
                    )
                shapefile_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_fn)
            except Exception as e:
                return {"success": False, "error": f"อัปโหลด Shapefile ผิดพลาด: {e}"}

            # Upload PDF (optional)
            pdf_url = ""
            if pdf_file and pdf_file.filename:
                try:
                    pdf_fn = f"{safe_key}_{pdf_file.filename.replace(' ', '_')}"
                    pdf_tmp = os.path.join(tmp, pdf_fn)
                    with open(pdf_tmp, "wb") as buf:
                        shutil.copyfileobj(pdf_file.file, buf)
                    with open(pdf_tmp, "rb") as pd:
                        supabase.storage.from_("dnp-pdfs").upload(
                            path=pdf_fn, file=pd,
                            file_options={"cache-control": "3600", "upsert": "true"}
                        )
                    pdf_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_fn)
                except Exception as e:
                    print(f"⚠️ PDF upload failed: {e}")

            # Create & upload GeoJSON
            try:
                geojson_fn   = f"{safe_key}_map.json"
                geojson_str  = gdf_wgs84.to_json()
                geojson_path = os.path.join(tmp, geojson_fn)
                with open(geojson_path, "w", encoding="utf-8") as jf:
                    jf.write(geojson_str)
                with open(geojson_path, "rb") as jd:
                    supabase.storage.from_("dnp-shapefiles").upload(
                        path=geojson_fn, file=jd,
                        file_options={"cache-control": "3600", "upsert": "true"}
                    )
                geojson_url = supabase.storage.from_("dnp-shapefiles").get_public_url(geojson_fn)
            except Exception as e:
                return {"success": False, "error": f"สร้าง GeoJSON ผิดพลาด: {e}"}

            is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")

            # Insert database
            try:
                if case_type == "encroachment":
                    db_data = {
                        "case_no":        complaint_no or criminal_no,
                        "case_date":      case_date,
                        "location":       location,
                        "rai":            final_rai,
                        "ngarn":          final_ngarn,
                        "wa":             final_wa,
                        "is_finished":    is_finished,
                        "case_status":    case_status,
                        "coords":         calculated_coords,
                        "agency":         agency,
                        "suspects_count": int(suspects_count),
                        "shapefile_url":  shapefile_url,
                        "pdf_url":        pdf_url,
                        "geojson_data":   geojson_url,
                        "utm_zone":       final_utm_zone,
                        "utm_easting":    final_utm_easting,
                        "utm_northing":   final_utm_northing,
                    }
                    if new_cols_exist:
                        db_data["complaint_no"] = complaint_no
                        db_data["criminal_no"]  = criminal_no
                        db_data["seizure_no"]   = seizure_no
                    else:
                        extra = ""
                        if complaint_no: extra += f"\n[แจ้ง: {complaint_no}]"
                        if criminal_no:  extra += f"\n[อาญา: {criminal_no}]"
                        if seizure_no:   extra += f"\n[ยึดทรัพย์: {seizure_no}]"
                        if extra: db_data["case_status"] = case_status + extra
                    supabase.table("encroachment_cases").insert(db_data).execute()

                elif case_type == "timber":
                    supabase.table("timber_cases").insert({
                        "case_no":        complaint_no or criminal_no,
                        "case_date":      case_date,
                        "location":       location,
                        "timber_type":    timber_type,
                        "width":          float(width),
                        "length":         float(length),
                        "size":           float(size),
                        "vol_logs":       float(vol1),
                        "vol_processed":  float(vol2),
                        "is_finished":    is_finished,
                        "case_status":    case_status,
                        "coords":         calculated_coords,
                        "agency":         agency,
                        "suspects_count": int(suspects_count),
                        "shapefile_url":  shapefile_url,
                        "pdf_url":        pdf_url,
                        "geojson_data":   geojson_url,
                        "utm_zone":       final_utm_zone,
                        "utm_easting":    final_utm_easting,
                        "utm_northing":   final_utm_northing,
                    }).execute()

            except Exception as e:
                return {"success": False, "error": f"บันทึก DB ผิดพลาด: {e}"}

            return {
                "success":         True,
                "message":         "บันทึกข้อมูลสำเร็จ",
                "coords":          calculated_coords,
                "geojson_url":     geojson_url,
                "utm_zone":        final_utm_zone,
                "utm_easting":     final_utm_easting,
                "utm_northing":    final_utm_northing,
                "schema_upgraded": new_cols_exist,
            }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Process wildlife ──
@app.post("/process-wildlife/")
async def process_wildlife(
    pdf_file:       UploadFile = File(None),
    case_no:        str   = Form(...),
    case_date:      str   = Form(...),
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
):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อ Supabase")

    pdf_url = ""
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, ".pdf", max_bytes=20*1024*1024)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf_fn = f"{case_no.replace('/', '_')}_{pdf_file.filename.replace(' ', '_')}"
                pdf_path = os.path.join(tmp, pdf_fn)
                with open(pdf_path, "wb") as buf:
                    shutil.copyfileobj(pdf_file.file, buf)
                with open(pdf_path, "rb") as pd:
                    supabase.storage.from_("dnp-pdfs").upload(
                        path=pdf_fn, file=pd,
                        file_options={"cache-control": "3600", "upsert": "true"}
                    )
                pdf_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_fn)
        except Exception as e:
            print(f"⚠️ PDF upload failed: {e}")

    is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")
    coords = [coords_lat, coords_lon] if (coords_lat or coords_lon) else [18.29, 99.50]

    final_utm_zone, final_utm_easting, final_utm_northing = utm_zone, utm_easting, utm_northing
    if utm_easting == 0 and coords_lat:
        u = latlon_to_utm(coords[0], coords[1])
        final_utm_zone, final_utm_easting, final_utm_northing = u["utm_zone"], u["utm_easting"], u["utm_northing"]
    if coords_lat == 0.0 and utm_easting:
        try:
            ll = utm_to_latlon_python(utm_zone, utm_easting, utm_northing)
            coords = [ll["lat"], ll["lon"]]
        except Exception:
            coords = [18.29, 99.50]

    try:
        supabase.table("wildlife_cases").insert({
            "case_no": case_no, "case_date": case_date, "location": location,
            "wildlife_type": wildlife_type, "equipment": equipment,
            "is_finished": is_finished, "case_status": case_status,
            "coords": coords, "agency": agency,
            "suspects_count": int(suspects_count), "pdf_url": pdf_url,
            "utm_zone": final_utm_zone, "utm_easting": final_utm_easting,
            "utm_northing": final_utm_northing,
        }).execute()
        return {
            "success": True, "message": "บันทึกคดีสัตว์ป่าสำเร็จ",
            "coords": coords, "utm_zone": final_utm_zone,
            "utm_easting": final_utm_easting, "utm_northing": final_utm_northing,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Get cases — มี pagination ──
@app.get("/get-cases/{case_type}")
async def get_cases(
    case_type: str,
    page:     int = Query(1, ge=1),
    limit:    int = Query(50, ge=1, le=200),
    search:   str = Query(""),
    finished: Optional[bool] = Query(None),
):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อ Supabase")
    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(400, "ประเภทคดีไม่ถูกต้อง")
    try:
        offset = (page - 1) * limit
        q = supabase.table(table_map[case_type]).select("*", count="exact")
        if finished is not None:
            q = q.eq("is_finished", finished)
        if search:
            q = q.ilike("location", f"%{search}%")
        res = q.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        return {
            "data":  res.data,
            "total": res.count,
            "page":  page,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Delete case ──
@app.delete("/delete-case/{case_type}/{case_no}")
async def delete_case(case_type: str, case_no: str):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อ Supabase")
    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(400, "ประเภทคดีไม่ถูกต้อง")
    try:
        supabase.table(table_map[case_type]).delete().eq("case_no", case_no).execute()
        return {"message": f"ลบคดี {case_no} สำเร็จ"}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Reload schema cache ──
@app.post("/reload-schema/")
async def reload_schema():
    invalidate_schema_cache()
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อ Supabase")
    try:
        supabase.rpc("pg_notify", {"channel": "pgrst", "payload": "reload schema"}).execute()
        return {"success": True, "message": "reload schema แล้ว cache ถูก invalidate"}
    except Exception as e:
        return {"success": False, "message": str(e)}
