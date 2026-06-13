"""
DNP GIS Case API Systems — Production-Ready Version
ระบบ API สำหรับบันทึกสารบบคดีเชิงพื้นที่ ศูนย์สารสนเทศ DNP สบอ.13 ลำปาง
ปรับปรุง: ระบบ Cache ตรวจสอบ Schema, Input Validation, Pagination, บีบอัดข้อมูล GZip,
        จำกัดสิทธิ์ CORS เฉพาะโดเมนที่กำหนด และปรับปรุงการ Upsert ไฟล์ขึ้น Storage
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
# 1. การตั้งค่าแอปพลิเคชัน (App Setup & CORS)
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="DNP GIS Case API Systems", 
    description="ระบบบริการข้อมูลสารบบคดีและแผนที่เชิงพื้นที่ สบอ.13 ลำปาง",
    version="3.2"
)

# ดึงค่าโดเมนที่อนุญาตให้เข้าถึง API จาก Environment Variables (ค่าเริ่มต้นคือ Localhost และ GitHub Pages)
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://natthawutfrdo.github.io,http://localhost:3000,http://127.0.0.1:5500"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # จำกัดการเข้าถึงตาม Origin เพื่อความปลอดภัย
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# เปิดใช้งาน GZip เพื่อบีบอัดข้อมูล Response ที่มีขนาดเกิน 1,000 ไบต์ ช่วยให้โหลดแผนที่ GeoJSON เร็วขึ้น
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ─────────────────────────────────────────────────────────────
# 2. การเชื่อมต่อฐานข้อมูล Supabase Client
# ─────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ เชื่อมต่อ Supabase สำเร็จ")
    except Exception as e:
        print(f"❌ Supabase initialization error: {e}")
else:
    print("⚠️ แจ้งเตือน: ยังไม่ได้ระบุค่า SUPABASE_URL หรือ SUPABASE_KEY ใน Environment Variables")

# ─────────────────────────────────────────────────────────────
# 3. ระบบจัดการ Schema Cache (ตรวจสอบคอลัมน์เพื่อความเข้ากันได้)
# ─────────────────────────────────────────────────────────────
_schema_cache: dict[str, tuple[bool, float]] = {}
SCHEMA_CACHE_TTL = 300  # เก็บค่านาน 5 นาที

def check_columns_exist_cached(table: str, columns: list[str]) -> bool:
    """ตรวจสอบว่าตารางในฐานข้อมูลมีคอลัมน์ใหม่ที่ระบุหรือไม่ โดยใช้ระบบ Cache เพื่อลดภาระของเซิร์ฟเวอร์"""
    cache_key = f"{table}:{','.join(sorted(columns))}"
    now = time.time()
    
    if cache_key in _schema_cache:
        result, ts = _schema_cache[cache_key]
        if now - ts < SCHEMA_CACHE_TTL:
            return result
            
    if not supabase:
        return False
        
    try:
        # ใช้การดึงข้อมูลจำกัดแค่ 0 แถวเพื่อทดสอบว่าโครงสร้างคอลัมน์มีอยู่จริงหรือไม่
        supabase.table(table).select(",".join(columns)).limit(0).execute()
        _schema_cache[cache_key] = (True, now)
        return True
    except Exception:
        _schema_cache[cache_key] = (False, now)
        return False

def invalidate_schema_cache():
    """ล้างข้อมูล Schema Cache ทั้งหมด"""
    _schema_cache.clear()

# ─────────────────────────────────────────────────────────────
# 4. ฟังก์ชันตรวจสอบไฟล์ (File Validation Helper)
# ─────────────────────────────────────────────────────────────
MAX_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

async def validate_file(file: UploadFile, allowed_ext: str, max_bytes: int = MAX_BYTES):
    """ตรวจสอบความถูกต้องของนามสกุลไฟล์และจำกัดขนาดไฟล์ไม่ให้เกินที่กำหนด"""
    if not file.filename.lower().endswith(allowed_ext):
        raise HTTPException(400, f"รูปแบบไฟล์ไม่ถูกต้อง ต้องเป็นไฟล์ .{allowed_ext} เท่านั้น")
        
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(413, f"ขนาดไฟล์ใหญ่เกินกำหนด (จำกัดไม่เกิน {MAX_FILE_SIZE_MB} MB)")
        
    await file.seek(0)  # เลื่อนพอยน์เตอร์กลับไปจุดเริ่มต้นเพื่อให้นำไฟล์ไปประมวลผลต่อได้
    return content

# ─────────────────────────────────────────────────────────────
# 5. ฟังก์ชันคำนวณและแปลงพิกัด (UTM & Lat-Lon Helpers)
# ─────────────────────────────────────────────────────────────
def utm_to_latlon_python(zone: int, easting: float, northing: float, is_north: bool = True) -> dict:
    """แปลงพิกัดจากระบบ UTM (WGS84) มาเป็นระบบพิกัดภูมิศาสตร์ (Latitude / Longitude) ด้วยสูตรคณิตศาสตร์แบบดั้งเดิม"""
    k0 = 0.9996
    a = 6378137.0
    e = 0.0818191908426215
    e2 = e * e
    e4 = e2 * e2
    e6 = e4 * e2
    e1sq = e2 / (1 - e2)
    
    x = easting - 500000.0
    y = northing if is_north else northing - 10000000.0
    lon_origin = (zone - 1) * 6 - 180 + 3
    
    M = y / k0
    mu = M / (a * (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256))
    
    phi1 = (mu
            + (3 / 2 * e2 + 27 / 32 * e4 + 55 / 512 * e6) * math.sin(2 * mu)
            + (21 / 16 * e4 + 55 / 32 * e6) * math.sin(4 * mu)
            + (151 / 96 * e6) * math.sin(6 * mu))
            
    N1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    T1 = math.tan(phi1) ** 2
    C1 = e1sq * math.cos(phi1) ** 2
    R1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    D = x / (N1 * k0)
    
    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D ** 2 / 2 - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2 - 9 * e1sq) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 ** 2 - 252 * e1sq - 3 * C1 ** 2) * D ** 6 / 720)
        
    lon = (D - (1 + 2 * T1 + C1) * D ** 3 / 6
           + (5 - 2 * C1 + 28 * T1 - 3 * C1 ** 2 + 8 * e1sq + 24 * T1 ** 2) * D ** 5 / 120) / math.cos(phi1)
           
    return {"lat": math.degrees(lat), "lon": lon_origin + math.degrees(lon)}

def latlon_to_utm(lat: float, lon: float) -> dict:
    """แปลงพิกัดจากระบบพิกัดภูมิศาสตร์ (Latitude / Longitude) ไปเป็นระบบพิกัด UTM (WGS84)"""
    try:
        zone = int((lon + 180) / 6) + 1
        k0 = 0.9996
        a = 6378137.0
        e = 0.0818191908426215
        e2 = e * e
        e4 = e2 * e2
        e6 = e4 * e2
        e1sq = e2 / (1 - e2)
        
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        lon_origin_rad = math.radians((zone - 1) * 6 - 180 + 3)
        
        N = a / math.sqrt(1 - e2 * math.sin(lat_rad) ** 2)
        T = math.tan(lat_rad) ** 2
        C = e1sq * math.cos(lat_rad) ** 2
        A = math.cos(lat_rad) * (lon_rad - lon_origin_rad)
        
        M = a * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat_rad
                 - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat_rad)
                 + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat_rad)
                 - (35 * e6 / 3072) * math.sin(6 * lat_rad))
                 
        easting = k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T ** 2 + 72 * C - 58 * e1sq) * A ** 5 / 120) + 500000.0
        northing = k0 * (M + N * math.tan(lat_rad) * (A ** 2 / 2 + (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24
                                                      + (61 - 58 * T + T ** 2 + 600 * C - 330 * e1sq) * A ** 6 / 720))
        if lat < 0:
            northing += 10000000.0
            
        return {"utm_zone": zone, "utm_easting": int(round(easting)), "utm_northing": int(round(northing))}
    except Exception as err:
        print(f"⚠️ latlon→UTM error: {err}")
        return {"utm_zone": 47, "utm_easting": 0, "utm_northing": 0}

# ─────────────────────────────────────────────────────────────
# 6. ฟังก์ชันจัดการไฟล์ GIS และคำนวณพื้นที่ (GIS Extraction Helper)
# ─────────────────────────────────────────────────────────────
def extract_gis_and_calculate(zip_path: str, extract_dir: str):
    """แตกไฟล์สำเนา Shapefile ในไฟล์ Zip เพื่อหาพิกัดศูนย์กลางแปลงและคำนวณสัดส่วนพื้นที่ (ไร่-งาน-ตร.ว.)"""
    try:
        with zipfile.ZipFile(zip_path, 'r') as zr:
            zr.extractall(extract_dir)
            
        shp_files = [
            os.path.join(root, f)
            for root, _, files in os.walk(extract_dir)
            for f in files if f.endswith('.shp')
        ]
        
        if not shp_files:
            raise HTTPException(400, "ไม่พบไฟล์นามสกุล .shp ภายในไฟล์คอมเพรส Zip")
            
        try:
            gdf = gpd.read_file(shp_files[0])
        except Exception as e:
            raise HTTPException(400, f"ไม่สามารถเปิดอ่านไฟล์ Shapefile ได้: {e}")
            
        if gdf.empty:
            raise HTTPException(400, "ไฟล์ Shapefile ไม่มีข้อมูลเชิงพื้นที่ภายในระบบ")
            
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=32647)  # ตั้งค่า CRS เริ่มต้นเป็น UTM Zone 47N หาดไม่มีการกำหนดไว้
            
        try:
            gdf_wgs84 = gdf.to_crs(epsg=4326)
            c = gdf_wgs84.geometry.centroid.iloc[0]
            lat_val, lon_val = float(c.y), float(c.x)
        except Exception:
            lat_val, lon_val = 18.29, 99.50  # พิกัดสํารองกรณีแปลงค่าไม่ได้
            gdf_wgs84 = gdf
            
        try:
            # คำนวณพื้นที่เป็นตารางเมตรจากค่าพิกัด UTM
            area_sqm = float(gdf.to_crs(epsg=32647).geometry.area.sum())
            area_sqm = max(area_sqm, 0.0)
        except Exception:
            area_sqm = 0.0
            
        # แปลงพื้นที่ตารางเมตรให้เป็น หน่วยไทย (1 ไร่ = 400 ตร.ว. / 1 งาน = 100 ตร.ว. / 1 ตร.ว. = 4 ตร.ม.)
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
        raise HTTPException(500, f"เกิดข้อผิดพลาดในระบบวิเคราะห์ข้อมูล GIS: {e}")

# ─────────────────────────────────────────────────────────────
# 7. ปลายทางเครือข่ายให้บริการ (API Endpoints)
# ─────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "DNP GIS Case API v3.2 is running perfectly!", "status": "ok"}

@app.get("/health")
def health():
    return {"db": "ok" if supabase else "disconnected"}

# ── Endpoint: สำหรับวิเคราะห์ข้อมูลดิบจาก Shapefile เท่านั้น (ไม่บันทึกลงฐานข้อมูล) ──
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

# ── Endpoint: สำหรับบันทึกหรือส่งประมวลผลข้อมูลคดีประเภท บุกรุกพื้นที่ป่า และ คดีไม้ผิดกฎหมาย ──
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
        raise HTTPException(500, "ยังไม่ได้กำหนดหรือทำการเชื่อมต่อกับฐานข้อมูล Supabase")
    if case_type not in ("encroachment", "timber"):
        raise HTTPException(400, "พารามิเตอร์ case_type ต้องกำหนดเป็น encroachment หรือ timber เท่านั้น")

    # ตรวจเช็คความถูกต้องและขนาดไฟล์
    await validate_file(file, ".zip")
    if pdf_file and pdf_file.filename:
        await validate_file(pdf_file, ".pdf", max_bytes=20 * 1024 * 1024)

    primary_key = complaint_no or criminal_no or "unknown"
    safe_key = primary_key.replace('/', '_').replace(' ', '_')

    # ตรวจสอบโครงสร้างตารางผ่านระบบ Cache เพื่อดูแนวโน้มโครงสร้างคอลัมน์ใหม่
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

            # ตรวจสอบลำดับการใช้ข้อมูลพิกัดและขนาดพื้นที่ (หาก Form ส่งค่ามาจะใช้ค่าจาก Form เป็นหลัก)
            final_utm_zone     = utm_zone     if utm_easting  != 0 else gis["utm_zone"]
            final_utm_easting  = utm_easting  if utm_easting  != 0 else gis["utm_easting"]
            final_utm_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]
            final_rai          = int(rai)       if rai   > 0 else gis["rai"]
            final_ngarn        = int(ngarn)     if ngarn > 0 else gis["ngarn"]
            final_wa           = int(round(wa)) if wa    > 0 else gis["wa"]

            # ปรับปรุงลดขนาดและความซับซ้อนของเส้น Polygon ของแผนที่เพื่อเพิ่มสปีดการเรนเดอร์บนหน้าเว็บ
            try:
                gdf_wgs84['geometry'] = gdf_wgs84['geometry'].simplify(
                    tolerance=0.0001, preserve_topology=True
                )
            except Exception:
                pass

            clean_fn = f"{safe_key}_{file.filename.replace(' ', '_')}"

            # อัปโหลดชุดไฟล์ Shapefile (.zip) ไปยัง Supabase Storage Bucket
            try:
                with open(zip_path, "rb") as fd:
                    supabase.storage.from_("dnp-shapefiles").upload(
                        path=clean_fn, file=fd,
                        file_options={"cache-control": "3600", "upsert": True}  # ปรับแก้เป็น Boolean True
                    )
                shapefile_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_fn)
            except Exception as e:
                return {"success": False, "error": f"อัปโหลดไฟล์สํารอง Shapefile ผิดพลาด: {e}"}

            # อัปโหลดไฟล์เอกสารประจำคดีความ PDF (ถ้ามีไฟล์แนบส่งมา)
            pdf_url = ""
            if pdf_file and pdf_file.filename:
                try:
                    pdf_fn = f"{safe_key}_{pdf_file.filename.replace(' ', '_')}"
                    pdf_tmp = os.path.join(tmp, pdf_fn)
                    with open(pdf_tmp, "wb") as buf:
                        shutil.copyfileobj(pdf_file.file, buf)
                    with open(pdf_tmp, "rb") as pd_file_data:
                        supabase.storage.from_("dnp-pdfs").upload(
                            path=pdf_fn, file=pd_file_data,
                            file_options={"cache-control": "3600", "upsert": True}  # ปรับแก้เป็น Boolean True
                        )
                    pdf_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_fn)
                except Exception as e:
                    print(f"⚠️ PDF upload failed: {e}")

            # สร้างไฟล์แผนที่ GeoJSON (.json) และอัปโหลดไปยังคลาวด์จัดเก็บข้อมูล
            try:
                geojson_fn   = f"{safe_key}_map.json"
                geojson_str  = gdf_wgs84.to_json()
                geojson_path = os.path.join(tmp, geojson_fn)
                with open(geojson_path, "w", encoding="utf-8") as jf:
                    jf.write(geojson_str)
                with open(geojson_path, "rb") as jd:
                    supabase.storage.from_("dnp-shapefiles").upload(
                        path=geojson_fn, file=jd,
                        file_options={"cache-control": "3600", "upsert": True}  # ปรับแก้เป็น Boolean True
                    )
                geojson_url = supabase.storage.from_("dnp-shapefiles").get_public_url(geojson_fn)
            except Exception as e:
                return {"success": False, "error": f"การสร้างแปลงโครงข่ายพิกัด GeoJSON ล้มเหลว: {e}"}

            is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")

            # จัดโครงสร้างคอลัมน์เพื่อทำการบันทึกข้อมูลแบบแยกลงตารางที่เกี่ยวข้อง
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
                    # บันทึกข้อมูลลงตารางย่อยหากฐานข้อมูลรองรับคอลัมน์คดีใหม่ครบถ้วน
                    if new_cols_exist:
                        db_data["complaint_no"] = complaint_no
                        db_data["criminal_no"]  = criminal_no
                        db_data["seizure_no"]   = seizure_no
                    else:
                        # ระบบสำรองกรณีฐานข้อมูลยังไม่ได้อัปเกรด จะยุบรวมเลขลงไปเป็นข้อความในคอลัมน์ประวัติความคืบหน้าแทน
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
                return {"success": False, "error": f"การส่งข้อมูลเพื่อเพิ่มลงฐานข้อมูลล้มเหลว: {e}"}

            return {
                "success":         True,
                "message":         "บันทึกข้อมูลสารระบบคดีพร้อมแผนที่เสร็จสิ้นเรียบร้อย",
                "coords":          calculated_coords,
                "geojson_url":     geojson_url,
                "utm_zone":        final_utm_zone,
                "utm_easting":     final_utm_easting,
                "utm_northing":    final_utm_northing,
                "schema_upgraded": new_cols_exist,
            }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Endpoint: สำหรับประมวลผลและจัดเก็บคดีที่เกี่ยวข้องกับสัตว์ป่าผิดกฎหมาย ──
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
        await validate_file(pdf_file, ".pdf", max_bytes=20 * 1024 * 1024)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf_fn = f"{case_no.replace('/', '_')}_{pdf_file.filename.replace(' ', '_')}"
                pdf_path = os.path.join(tmp, pdf_fn)
                with open(pdf_path, "wb") as buf:
                    shutil.copyfileobj(pdf_file.file, buf)
                with open(pdf_path, "rb") as pd_file_data:
                    supabase.storage.from_("dnp-pdfs").upload(
                        path=pdf_fn, file=pd_file_data,
                        file_options={"cache-control": "3600", "upsert": True}  # ปรับแก้เป็น Boolean True
                    )
                pdf_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_fn)
        except Exception as e:
            print(f"⚠️ PDF upload failed: {e}")

    is_finished = status in ("คดีสิ้นสุด", "finished", "done", "true", "1")
    coords = [coords_lat, coords_lon] if (coords_lat or coords_lon) else [18.29, 99.50]

    # ตรวจสอบและแปลงค่าไขว้ไปมาระหว่าง Lat-Lon และพิกัดระบุ UTM กรณีที่ข้อมูลใดข้อมูลหนึ่งขาดหายไป
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
            "success": True, "message": "บันทึกข้อมูลคดีสัตว์ป่าเสร็จสิ้นเรียบร้อย",
            "coords": coords, "utm_zone": final_utm_zone,
            "utm_easting": final_utm_easting, "utm_northing": final_utm_northing,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Endpoint: เรียกดูรายการคดีแบบแยกประเภทคดี พร้อมระบบแบ่งหน้า (Pagination) และการค้นหา ──
@app.get("/get-cases/{case_type}")
async def get_cases(
    case_type: str,
    page:     int = Query(1, ge=1),
    limit:    int = Query(50, ge=1, le=200),
    search:   str = Query(""),
    finished: Optional[bool] = Query(None),
):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อระบบฐานข้อมูล Supabase คลาวด์")
        
    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(400, "ระบุพารามิเตอร์ประเภทคดีความไม่ถูกต้อง")
        
    try:
        offset = (page - 1) * limit
        q = supabase.table(table_map[case_type]).select("*", count="exact")
        
        # ฟิลเตอร์กรองสถานะสิ้นสุดคดี
        if finished is not None:
            q = q.eq("is_finished", finished)
            
        # ค้นหาด้วยตัวอักษรจากสถานที่เกิดเหตุแบบ Case-insensitive (ilike)
        if search:
            q = q.ilike("location", f"%{search}%")
            
        # เรียงลำดับจากเวลาอัปเดตล่าสุด และแบ่งช่วงแถวข้อมูลสำหรับการดึง (Range Pagination)
        res = q.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        return {
            "data":  res.data,
            "total": res.count,
            "page":  page,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Endpoint: สำหรับใช้ในการลบระเบียนคดีความออกจากระบบฐานข้อมูล ──
@app.delete("/delete-case/{case_type}/{case_no}")
async def delete_case(case_type: str, case_no: str):
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้เชื่อมต่อคลาวด์ฐานข้อมูล")
        
    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(400, "ประเภทคดีไม่ถูกต้องเพื่อเริ่มกระบวนการลบ")
        
    try:
        supabase.table(table_map[case_type]).delete().eq("case_no", case_no).execute()
        return {"message": f"ทำการลบข้อมูลหมายเลขคดี {case_no} ออกจากเซิร์ฟเวอร์เรียบร้อยแล้ว"}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Endpoint: สั่งการให้ระบบรีเฟรชโครงสร้างและทำลาย Cache ตัวตรวจคอลัมน์คดี ──
@app.post("/reload-schema/")
async def reload_schema():
    invalidate_schema_cache()
    if not supabase:
        raise HTTPException(500, "ยังไม่ได้ทำการเชื่อมต่อโครงข่ายคลาวด์ Supabase")
    try:
        # ส่งคลื่นสัญญาณ Notify ไปบอก PostgREST ของ Supabase เพื่อบังคับโหลด Schema ใหม่
        supabase.rpc("pg_notify", {"channel": "pgrst", "payload": "reload schema"}).execute()
        return {"success": True, "message": "บังคับทำลายแคชและ Reload Schema ของเซิร์ฟเวอร์เสร็จสิ้น"}
    except Exception as e:
        return {"success": False, "message": str(e)}
