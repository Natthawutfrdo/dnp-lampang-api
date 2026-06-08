import os
import shutil
import tempfile
import zipfile
import json
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping
from supabase import create_client, Client, ClientOptions
from httpx import Timeout as HttpxTimeout

app = FastAPI(title="DNP GIS Case API Systems")

# เปิดสิทธิ์ CORS ให้หน้าเว็บ Frontend (เช่น GitHub Pages) สามารถเชื่อมต่อได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 💡 ดึงค่าคอนฟิกเชื่อมต่อฐานข้อมูลจาก Environment Variables ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# แก้ไขจุดนี้: เปลี่ยนจากโครงสร้างเดิมที่สั่งแครชระบบ (raise RuntimeWarning) 
# เป็นการพิมพ์ Log เตือนแทน เพื่อให้เซิร์ฟเวอร์เปิดบริการได้แม้ช่วงแรกยังไม่ได้ใส่คีย์
if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ [WARNING] ไม่พบค่า SUPABASE_URL หรือ SUPABASE_KEY ในระบบ")
    print("กรุณาตรวจสอบการกรอกค่าเหล่านี้ในหน้าแดชบอร์ดเมนู Environment บน Render.com เพื่อให้ระบบบันทึกคดีได้")

# กำหนดเวลาให้ระบบยอมรอการอ่าน/เขียนข้อมูลสูงสุด 300 วินาที (5 นาที) สำหรับแปลงพิกัดขนาดใหญ่
custom_timeout = HttpxTimeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# ทำการเชื่อมต่อ Supabase อย่างปลอดภัย
if SUPABASE_URL and SUPABASE_KEY:
    supabase: Optional[Client] = create_client(
        SUPABASE_URL, 
        SUPABASE_KEY,
        options=ClientOptions(
            postgrest_client_timeout=custom_timeout,
            storage_client_timeout=custom_timeout
        )
    )
else:
    supabase = None

def extract_gis_and_calculate(zip_path: str, extract_dir: str):
    """ฟังก์ชันส่วนกลางสำหรับถอดรหัสพิกัด หาจุดกึ่งกลาง และคำนวณพื้นที่ ไร่-งาน-วา"""
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)
        
    shp_files = [f for f in os.listdir(extract_dir) if f.endswith('.shp')]
    if not shp_files:
        for root, dirs, files in os.walk(extract_dir):
            shp_files = [f for f in files if f.endswith('.shp')]
            if shp_files:
                extract_dir = root
                break
                
    if not shp_files:
        raise HTTPException(status_code=400, detail="ไม่พบไฟล์ .shp ภายในไฟล์ Zip ที่ส่งมา")
        
    shp_path = os.path.join(extract_dir, shp_files[0])
    gdf = gpd.read_file(shp_path)
    
    if gdf.empty:
        raise HTTPException(status_code=400, detail="ไฟล์ Shapefile ไม่มีข้อมูลเชิงพื้นที่")
        
    if gdf.crs is None:
        gdf.set_crs(epsg=32647, inplace=True) # กำหนดค่าเริ่มต้นเป็น UTM Zone 47N (จ.ลำปาง)
        
    # แปลงเข้าสู่ระบบ WGS84 (EPSG:4326) สำหรับคำนวณพิกัดภูมิศาสตร์แสดงบนแผนที่เว็บ Leaflet
    gdf_wgs84 = gdf.to_crs(epsg=4326)
    centroid = gdf_wgs84.geometry.centroid.iloc[0]
    calculated_coords = [centroid.y, centroid.x]
    
    # คำนวณพื้นที่จริง (แปลงเป็นระบบโครงพิกัด UTM Zone 47N เพื่อความแม่นยำสูงสุดในหน่วยตารางเมตร)
    gdf_utm = gdf.to_crs(epsg=32647)
    area_sqm = gdf_utm.geometry.area.sum()
    
    total_wa = area_sqm / 4
    rai = int(total_wa // 400)
    ngarn = int((total_wa % 400) // 100)
    wa = round(total_wa % 100, 1)
    
    return {
        "gdf_wgs84": gdf_wgs84,
        "coords": calculated_coords,
        "rai": rai,
        "ngarn": ngarn,
        "wa": wa
    }

@app.get("/")
def read_root():
    return {"message": "DNP GIS Case API is running successfully!"}

@app.post("/analyze-shapefile/")
async def analyze_shapefile(file: UploadFile = File(...)):
    """เส้น API สำหรับดึงค่าพิกัดและคำนวณพื้นที่แสดงพรีวิวบนหน้าจอก่อนที่ผู้ใช้จะกดบันทึกจริง"""
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="กรุณาอัปโหลดไฟล์บีบอัดประเภท .zip")
        
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, file.filename)
            with open(zip_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
                
            extract_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            
            gis_result = extract_gis_and_calculate(zip_path, extract_dir)
            
            return {
                "success": True,
                "lat": gis_result["coords"][0],
                "lon": gis_result["coords"][1],
                "rai": gis_result["rai"],
                "ngarn": gis_result["ngarn"],
                "wa": gis_result["wa"]
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process-shapefile/")
async def process_shapefile(
    file: UploadFile = File(...),
    pdf_file: UploadFile = File(None),
    case_type: str = Form(...),
    case_no: str = Form(...),
    case_date: str = Form(...),
    location: str = Form(...),
    status: str = Form(...),
    case_status: str = Form(...),
    agency: str = Form(...),
    suspects_count: int = Form(0),
    # ฟิลด์เฉพาะคดีบุกรุก
    rai: float = Form(0.0),
    ngarn: float = Form(0.0),
    wa: float = Form(0.0),
    # ฟิลด์เฉพาะคดีไม้
    timber_type: str = Form(""),
    width: float = Form(0.0),
    length: float = Form(0.0),
    size: float = Form(0.0),
    vol1: float = Form(0.0),
    vol2: float = Form(0.0)
):
    """เส้น API หลักสำหรับบันทึกข้อมูลและอัปโหลดไฟล์ขนาดใหญ่ด้วยระบบ Hybrid Storage"""
    if not supabase:
        raise HTTPException(status_code=500, detail="ระบบยังไม่ได้เชื่อมต่อฐานข้อมูล Supabase (กรุณากรอก Environment Variables บน Render)")
        
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, file.filename)
            with open(zip_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            extract_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            
            # ดึงโครงพิกัดผังแปลงและการคำนวณ
            gis_result = extract_gis_and_calculate(zip_path, extract_dir)
            gdf_wgs84 = gis_result["gdf_wgs84"]
            calculated_coords = gis_result["coords"]
            
            # ลดทอนจุดพิกัดที่ซ้ำซ้อนเพื่อย่อยขนาดไฟล์แผนที่ให้เปิดหน้าจอได้เร็วและลื่นไหลขึ้น
            gdf_wgs84['geometry'] = gdf_wgs84['geometry'].simplify(tolerance=0.0001, preserve_topology=True)

            # --- 1. อัปโหลดไฟล์ต้นฉบับ Shapefile (.zip) ขึ้นคลาวด์ Storage ---
            safe_filename = file.filename.replace(" ", "_").replace("/", "_").replace("\\", "_")
            clean_filename = f"{case_no.replace('/', '_')}_{safe_filename}"
            with open(zip_path, "rb") as f_data:
                supabase.storage.from_("dnp-shapefiles").upload(
                    path=clean_filename,
                    file=f_data,
                    file_options={"cache-control": "3600", "upsert": "true"}
                )
            shapefile_public_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_filename)

            # --- 2. อัปโหลดไฟล์เอกสารสแกนสำนวนคดี (.pdf) ขึ้นคลาวด์ Storage ---
            pdf_public_url = ""
            if pdf_file:
                safe_pdf_filename = pdf_file.filename.replace(" ", "_").replace("/", "_").replace("\\", "_")
                pdf_filename = f"{case_no.replace('/', '_')}_{safe_pdf_filename}"
                pdf_temp_path = os.path.join(temp_dir, pdf_file.filename)
                with open(pdf_temp_path, "wb") as pdf_buffer:
                    shutil.copyfileobj(pdf_file.file, pdf_buffer)
                
                with open(pdf_temp_path, "rb") as pdf_data:
                    supabase.storage.from_("dnp-pdfs").upload(
                        path=pdf_filename,
                        file=pdf_data,
                        file_options={"cache-control": "3600", "upsert": "true"}
                    )
                pdf_public_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_filename)

            # --- 3. ระบบ Hybrid Storage: เขียนแปลงพิกัดออกเป็นไฟล์ JSON ย่อย แยกไปฝากถังเก็บเพื่อป้องกันปัญหา Timeout ---
            geojson_data = json.loads(gdf_wgs84.to_json())
            geojson_string = json.dumps(geojson_data, ensure_ascii=False)
            
            geojson_filename = f"{case_no.replace('/', '_')}_map.json"
            geojson_temp_path = os.path.join(temp_dir, geojson_filename)
            
            with open(geojson_temp_path, "w", encoding="utf-8") as json_file:
                json_file.write(geojson_string)
                
            with open(geojson_temp_path, "rb") as json_data:
                supabase.storage.from_("dnp-shapefiles").upload(
                    path=geojson_filename,
                    file=json_data,
                    file_options={"cache-control": "3600", "upsert": "true"}
                )
            geojson_public_url = supabase.storage.from_("dnp-shapefiles").get_public_url(geojson_filename)

            # --- 4. จัดเตรียมชุดคีย์ข้อมูลและเขียนคำสั่งบันทึกลง Table ข้อมูลสารบบ ---
            is_finished_bool = True if status == 'คดีสิ้นสุด' else False
            
            if case_type == 'encroachment':
                db_data = {
                    "case_no": case_no, "case_date": case_date, "location": location,
                    "rai": rai, "ngarn": ngarn, "wa": wa, "is_finished": is_finished_bool,
                    "case_status": case_status, "coords": calculated_coords, "agency": agency,
                    "suspects_count": suspects_count, "shapefile_url": shapefile_public_url,
                    "pdf_url": pdf_public_url, 
                    "geojson_data": geojson_public_url
                }
                supabase.table("encroachment_cases").insert(db_data).execute()
            else:
                db_data = {
                    "case_no": case_no, "case_date": case_date, "location": location,
                    "timber_type": timber_type, "width": width, "length": length, "size": size,
                    "vol_logs": vol1, "vol_processed": vol2, "is_finished": is_finished_bool,
                    "case_status": case_status, "coords": calculated_coords, "agency": agency,
                    "suspects_count": suspects_count, "shapefile_url": shapefile_public_url,
                    "pdf_url": pdf_public_url, 
                    "geojson_data": geojson_public_url
                }
                supabase.table("timber_cases").insert(db_data).execute()

            return {
                "success": True,
                "message": "ประมวลผลและจัดเก็บพิกัดแผนที่สารบบคดีเสร็จสิ้น",
                "coords": calculated_coords,
                "geojson_url": geojson_public_url
            }

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/get-cases/{case_type}")
async def get_cases(case_type: str):
    if not supabase: 
        raise HTTPException(status_code=500, detail="ฐานข้อมูลยังไม่ได้ตั้งค่าเชื่อมต่อ")
    try:
        table_name = "encroachment_cases" if case_type == "encroachment" else "timber_cases"
        res = supabase.table(table_name).select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete-case/{case_type}/{case_no}")
async def delete_case(case_type: str, case_no: str):
    if not supabase: 
        raise HTTPException(status_code=500, detail="ฐานข้อมูลยังไม่ได้ตั้งค่าเชื่อมต่อ")
    try:
        table_name = "encroachment_cases" if case_type == "encroachment" else "timber_cases"
        supabase.table(table_name).delete().eq("case_no", case_no).execute()
        return {"message": "ลบข้อมูลออกจากสารบบคดีสำเร็จ"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
