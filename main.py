# -*- coding: utf-8 -*-
# ไฟล์: main.py (สำหรับเซฟลงโฟลเดอร์ /api เพื่อ Deploy ขึ้น Render.com)
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import geopandas as gpd
import pandas as pd
import pandas.api.types as ptypes
import json
import zipfile
import os
import shutil
from supabase import create_client, Client

app = FastAPI(title="DNP สบอ.13 ลำปาง - Supabase API")

# เปิดสิทธิ์ CORS ให้ GitHub Pages หรือระบบภายนอกสามารถส่งข้อมูลเข้ามาได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ⚠️ ข้อควรระวัง: นำ URL และ Service Key จากหน้าแดชบอร์ด Supabase (Settings > API) มาใส่แทนค่าด้านล่างนี้
SUPABASE_URL = "https://pihngogrcxxeqyvulnwl.supabase.co"
SUPABASE_KEY = "sb_publishable_VcEhhWluqdphyd9jnZBY1g_qaJiFn39"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/process-shapefile/")
async def process_shapefile(
    case_type: str = Form(...),
    case_no: str = Form(...),
    case_date: str = Form(...),
    location: str = Form(...),
    status: str = Form(...),
    case_status: str = Form(...),
    agency: str = Form(...),
    suspects_count: int = Form(0),
    timber_type: str = Form(None),   
    width: str = Form(None),         
    length: str = Form(None),        
    size: str = Form(None),          
    vol1: float = Form(0.0),          
    vol2: float = Form(0.0),          
    file: UploadFile = File(...),
    pdf_file: UploadFile = File(None) # รองรับการรับไฟล์ PDF จากหน้าเว็บ (ไม่บังคับใส่)
):
    # ป้องกันอาการหน่วยความจำเต็ม (แรมเกิน) เมื่อเจอไฟล์ Shapefile ขนาดใหญ่ เช่น 21 MB
    MAX_FILE_SIZE = 100 * 1024 * 1024  
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0) 
    
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="ไฟล์ .zip มีขนาดใหญ่เกินไป (ระบบรองรับสูงสุด 100 MB)")

    # สร้างพื้นที่และโฟลเดอร์จัดเก็บชั่วคราวบนเซิร์ฟเวอร์
    temp_dir = f"temp_{case_no.replace('/', '_')}"
    os.makedirs(temp_dir, exist_ok=True)
    zip_path = os.path.join(temp_dir, file.filename)
    
    # บันทึกไฟล์ Zip แบบ Chunk-by-Chunk (ช่วยประหยัดแรมเซิร์ฟเวอร์)
    with open(zip_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024): 
            buffer.write(chunk)
        
    try:
        # แตกไฟล์ Zip เพื่อตรวจค้นหาไฟล์หลัก .shp
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        shp_file = None
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.endswith('.shp'):
                    shp_file = os.path.join(root, f)
                    break
        
        if not shp_file:
            raise HTTPException(status_code=400, detail="ไม่พบไฟล์หลัก .shp ภายในไฟล์ Zip ที่ส่งมา")
            
        # อ่านข้อมูลพิกัดแปลงด้วย GeoPandas พร้อมถอดรหัสภาษาไทย (cp874)
        gdf = gpd.read_file(shp_file, encoding='cp874')
        
        # แปลงระบบพิกัดดั้งเดิม (เช่น Indian 1975 หรือ UTM) ให้กลายเป็น WGS84 (Lat/Lon)
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
            
        # จัดการแปลงคอลัมน์ประเภท วันที่/เวลา ทั้งหมดให้เป็น Text ป้องกันระบบแปลง JSON พัง
        for col in gdf.columns:
            if gdf[col].dtype.name in ['datetime64[ns]', 'datetime64', 'timestamp'] or ptypes.is_datetime64_any_dtype(gdf[col]):
                gdf[col] = gdf[col].dt.strftime('%Y-%m-%d %H:%M:%S').fillna('')
            elif gdf[col].dtype == 'object':
                gdf[col] = gdf[col].apply(lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if hasattr(x, 'strftime') else x)
        
        # คำนวณหาพิกัดแกนกลาง (Centroid) ของพื้นที่แปลงที่ดิน
        centroid = gdf.geometry.centroid.iloc[0]
        calculated_coords = f"{centroid.y:.6f}, {centroid.x:.6f}"
        
        # คำนวณเนื้อที่ระบบไทย (ไร่-งาน-วา) จากค่าพื้นที่จริงเมตร (สำหรับคดีบุกรุกป่า)
        rai, ngarn, wa = 0, 0, 0
        if case_type == 'encroachment':
            gdf_utm = gdf.to_crs(epsg=32647) 
            area_sqm = gdf_utm.geometry.area.sum()
            rai = int(area_sqm // 1600)
            rem = area_sqm % 1600
            ngarn = int(rem // 400)
            wa = round((rem % 400) / 4)

      gdf['geometry'] = gdf['geometry'].simplify(tolerance=0.0001, preserve_topology=True)

        # --- อัปโหลดไฟล์ Shapefile (.zip) ขึ้น Supabase Storage (เวอร์ชันแก้บั๊กชื่อไฟล์มีเว้นวรรค) ---
        safe_filename = file.filename.replace(" ", "_").replace("/", "_").replace("\\", "_")
        clean_filename = f"{case_no.replace('/', '_')}_{safe_filename}"
        with open(zip_path, "rb") as f_data:
            supabase.storage.from_("dnp-shapefiles").upload(
                path=clean_filename,
                file=f_data,
                file_options={"cache-control": "3600", "upsert": "true"}
            )
        shapefile_public_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_filename)
        
        # หากพบว่าขนาดตัวอักษร GeoJSON ยังใหญ่เกิน 5MB ให้ทำการดึงเฉพาะกล่องขอบเขต (Bounding Box) 
        # หรือสั่งย่อพิกัดเพิ่มอีกชั้น เพื่อป้องกันฐานข้อมูลพัง (Statement Timeout)
        if len(geojson_string.encode('utf-8')) > 5 * 1024 * 1024:
            # เพิ่มความเข้มข้นในการย่อยพิกัดหากไฟล์ยังคงใหญ่เกินไป
            gdf['geometry'] = gdf['geometry'].simplify(tolerance=0.0005, preserve_topology=True)
            geojson_data = json.loads(gdf.to_json())
        # --- อัปโหลดไฟล์ Shapefile (.zip) ขึ้น Supabase Storage ---
        clean_filename = f"{case_no.replace('/', '_')}_{file.filename}"
        with open(zip_path, "rb") as f_data:
            supabase.storage.from_("dnp-shapefiles").upload(
                path=clean_filename,
                file=f_data,
                file_options={"cache-control": "3600", "upsert": "true"}
            )
        shapefile_public_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_filename)

        # --- อัปโหลดไฟล์เอกสารสแกน (.pdf) ขึ้น Supabase Storage (ถ้ามี) ---
        pdf_public_url = ""
        if pdf_file:
            pdf_filename = f"{case_no.replace('/', '_')}_{pdf_file.filename}"
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

        # --- บันทึกข้อมูลคดีเชิงพื้นที่ลงในตาราง PostgreSQL ของ Supabase ---
        is_finished_bool = True if status == 'คดีสิ้นสุด' else False
        
        if case_type == 'encroachment':
            db_data = {
                "case_no": case_no, "case_date": case_date, "location": location,
                "rai": rai, "ngarn": ngarn, "wa": wa, "is_finished": is_finished_bool,
                "case_status": case_status, "coords": calculated_coords, "agency": agency,
                "suspects_count": suspects_count, "shapefile_url": shapefile_public_url,
                "pdf_url": pdf_public_url, "geojson_data": geojson_data
            }
            supabase.table("encroachment_cases").insert(db_data).execute()
        else:
            db_data = {
                "case_no": case_no, "case_date": case_date, "location": location,
                "timber_type": timber_type, "width": width, "length": length, "size": size,
                "vol_logs": vol1, "vol_processed": vol2, "is_finished": is_finished_bool,
                "case_status": case_status, "coords": calculated_coords, "agency": agency,
                "suspects_count": suspects_count, "shapefile_url": shapefile_public_url,
                "pdf_url": pdf_public_url, "geojson_data": geojson_data
            }
            supabase.table("timber_cases").insert(db_data).execute()

        return {
            "success": True,
            "message": "ประมวลผล Shapefile บีบอัดพิกัด และอัปโหลดไฟล์ขึ้น Supabase สำเร็จเรียบร้อย",
            "coords": calculated_coords,
            "geojson": geojson_data
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # ล้างไฟล์ขยะชั่วคราวทั้งหมดออกจากเซิร์ฟเวอร์ทันทีเพื่อความปลอดภัย
        shutil.rmtree(temp_dir, ignore_errors=True)

# Endpoint สำหรับดึงประวัติข้อมูลคดีไปแสดงบนตารางหน้าจอเว็บ
@app.get("/get-cases/{case_type}")
async def get_cases(case_type: str):
    table_name = "encroachment_cases" if case_type == "encroachment" else "timber_cases"
    response = supabase.table(table_name).select("*").order("id", desc=True).execute()
    return response.data

# Endpoint สำหรับลบข้อมูลคดี
@app.delete("/delete-case/{case_type}/{case_no}")
async def delete_case(case_type: str, case_no: str):
    table_name = "encroachment_cases" if case_type == "encroachment" else "timber_cases"
    supabase.table(table_name).delete().eq("case_no", case_no).execute()
    return {"success": True, "message": f"ลบข้อมูลคดีเลขที่ {case_no} เรียบร้อยแล้ว"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True)
