import os
import shutil
import tempfile
import zipfile
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping
from supabase import create_client, Client, ClientOptions
from httpx import Timeout as HttpxTimeout

app = FastAPI(title="DNP GIS Case API")

# เปิดสิทธิ์ CORS ให้หน้าเว็บ Frontend (เช่น GitHub Pages) สามารถเชื่อมต่อได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 💡 ตั้งค่าเชื่อมต่อ Supabase พร้อมเพิ่มเวลา Timeout สำหรับไฟล์ใหญ่ ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeWarning("กรุณาตั้งค่า SUPABASE_URL และ SUPABASE_KEY ใน Environment Variables")

# กำหนดเวลาให้ระบบยอมรอการอ่าน/เขียนข้อมูลสูงสุด 300 วินาที (5 นาที) สำหรับไฟล์ 21 MB
custom_timeout = HttpxTimeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

supabase: Client = create_client(
    SUPABASE_URL, 
    SUPABASE_KEY,
    options=ClientOptions(
        postgrest_client_timeout=custom_timeout,
        storage_client_timeout=custom_timeout
    )
)

@app.get("/")
def read_root():
    return {"message": "DNP GIS Case API is running successfully!"}

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
    try:
        # สร้างโฟลเดอร์ชั่วคราวสำหรับประมวลผลไฟล์
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, file.filename)
            with open(zip_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            # แตกไฟล์ .zip ของ Shapefile ออกมา
            extract_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            # ค้นหาไฟล์ .shp ภายในโฟลเดอร์ที่แตกออกมา
            shp_files = [f for f in os.listdir(extract_dir) if f.endswith('.shp')]
            if not shp_files:
                # กรณีไฟล์ซ้อนอยู่ในโฟลเดอร์ย่อยอีกชั้น
                for root, dirs, files in os.walk(extract_dir):
                    shp_files = [f for f in files if f.endswith('.shp')]
                    if shp_files:
                        extract_dir = root
                        break
            
            if not shp_files:
                raise HTTPException(status_code=400, detail="ไม่พบไฟล์ .shp ภายในไฟล์ Zip ที่ส่งมา")
            
            shp_path = os.path.join(extract_dir, shp_files[0])
            
            # อ่านไฟล์ด้วย GeoPandas และแปลงพิกัดเข้าสู่ระบบ WGS84 (EPSG:4326) สำหรับแสดงผลบนแผนที่เว็บ
            gdf = gpd.read_file(shp_path)
            if gdf.crs is None:
                gdf.set_crs(epsg=32647, inplace=True) # กำหนดค่าเริ่มต้นเป็น UTM Zone 47N ถ้าไม่ได้ระบุมา
            gdf = gdf.to_crs(epsg=4326)
            
            # คำนวณหาจุดกึ่งกลาง (Centroid) เพื่อส่งกลับไปปักหมุดแผนที่ในหน้าเว็บแรกเริ่ม
            centroid = gdf.geometry.centroid.iloc[0]
            calculated_coords = [centroid.y, centroid.x]
            
            # 💡 สั่งลดทอนจุดพิกัดที่ละเอียดเกินจำเป็นออก เพื่อย่อยขนาดโครงสร้างลงมหาศาล
            gdf['geometry'] = gdf['geometry'].simplify(tolerance=0.0001, preserve_topology=True)

            # --- 1. อัปโหลดไฟล์ Shapefile (.zip) ขึ้น Supabase Storage (เวอร์ชันแก้บั๊กช่องว่างในชื่อไฟล์) ---
            safe_filename = file.filename.replace(" ", "_").replace("/", "_").replace("\\", "_")
            clean_filename = f"{case_no.replace('/', '_')}_{safe_filename}"
            with open(zip_path, "rb") as f_data:
                supabase.storage.from_("dnp-shapefiles").upload(
                    path=clean_filename,
                    file=f_data,
                    file_options={"cache-control": "3600", "upsert": "true"}
                )
            shapefile_public_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_filename)

            # --- 2. อัปโหลดไฟล์เอกสารสแกน (.pdf) ขึ้น Supabase Storage ---
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

            # --- 3. ⚡ ระบบ Hybrid Storage: แยกแปลง GeoJSON ขนาดยักษ์ เซิฟลงถังเก็บไฟล์เพื่อแก้บั๊ก Timeout ---
            geojson_data = json.loads(gdf.to_json())
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

            # --- 4. จัดเตรียมแพ็กเก็ตข้อมูลและเซฟลงตารางฐานข้อมูล (ความเร็วสูง ไม่ติดล็อก 15 วินาที) ---
            is_finished_bool = True if status == 'คดีสิ้นสุด' else False
            
            if case_type == 'encroachment':
                db_data = {
                    "case_no": case_no, "case_date": case_date, "location": location,
                    "rai": rai, "ngarn": ngarn, "wa": wa, "is_finished": is_finished_bool,
                    "case_status": case_status, "coords": calculated_coords, "agency": agency,
                    "suspects_count": suspects_count, "shapefile_url": shapefile_public_url,
                    "pdf_url": pdf_public_url, 
                    "geojson_data": geojson_public_url  # เก็บเป็นข้อความลิงก์ URL ชี้ไปยังไฟล์แทนก้อน Object ยักษ์
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
                    "geojson_data": geojson_public_url  # เก็บเป็นข้อความลิงก์ URL ชี้ไปยังไฟล์แทนก้อน Object ยักษ์
                }
                supabase.table("timber_cases").insert(db_data).execute()

            return {
                "success": True,
                "message": "ประมวลผลสำเร็จและจัดเก็บพิกัดแผนที่แบบความเร็วสูงเสร็จสิ้น",
                "coords": calculated_coords,
                "geojson_url": geojson_public_url
            }

    except Exception as e:
        return {"success": False, "error": str(e)}
