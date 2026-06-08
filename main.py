import os
import zipfile
import shutil
import tempfile
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import geopandas as gpd

app = FastAPI(title="DNP Lampang GIS API")

# เปิด CORS เพื่อให้ Frontend (ที่อาจจะรันจากเครื่องอื่น) สามารถดึงข้อมูลได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/analyze-shapefile/")
async def analyze_shapefile(file: UploadFile = File(...)):
    """
    เส้น API สำหรับแงะไฟล์ Shapefile (.zip) เพื่อดึงพิกัดศูนย์กลาง 
    และคำนวณพื้นที่เป็น ไร่-งาน-ตร.ว. ส่งกลับไปพรีวิวบนหน้าเว็บอัตโนมัติ
    """
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="กรุณาอัปโหลดไฟล์บีบอัดในรูปแบบ .zip เท่านั้น")

    # 1. สร้างโฟลเดอร์ชั่วคราวเพื่อทำงาน
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, file.filename)

    try:
        # 2. บันทึกไฟล์ .zip ลง temporary directory
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 3. แตกไฟล์ .zip ออกมา
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        # 4. ค้นหาไฟล์ .shp ภายในโฟลเดอร์ (รวมถึงในโฟลเดอร์ย่อยถ้ามี)
        shp_file = None
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.endswith('.shp'):
                    shp_file = os.path.join(root, f)
                    break
            if shp_file:
                break

        if not shp_file:
            raise HTTPException(status_code=400, detail="ไม่พบไฟล์ .shp ภายในไฟล์ .zip ที่ส่งมา")

        # 5. อ่านไฟล์ Shapefile ด้วย GeoPandas
        gdf = gpd.read_file(shp_file)
        if gdf.empty:
            raise HTTPException(status_code=400, detail="ไฟล์ Shapefile ไม่มีข้อมูล Vector ด้านใน")

        # --- ส่วนที่ 1: หาพิกัดศูนย์กลาง (Centroid) สำหรับซูมแผนที่ ---
        # แปลงเป็น WGS84 (EPSG:4326) เสมอเพื่อให้อ่านค่า Lat/Lon ได้ถูกต้อง
        gdf_wgs84 = gdf.to_crs(epsg=4326)
        centroid = gdf_wgs84.geometry.unary_union.centroid
        lat = centroid.y
        lon = centroid.x

        # --- ส่วนที่ 2: คำนวณพื้นที่แปลงขยายผลเป็น ไร่-งาน-ตร.ว. ---
        # แปลงโครงสร้างพื้นผิวเป็น UTM Zone 47N (EPSG:32647) ซึ่งครอบคลุมพื้นที่ลำปาง เพื่อคิดพื้นที่เป็นตารางเมตรจริง
        gdf_utm = gdf.to_crs(epsg=32647)
        area_sqm = gdf_utm.geometry.area.sum()

        # สูตรแปลงค่า: 1 ไร่ = 1,600 ตร.ม. / 1 งาน = 400 ตร.ม. / 1 ตร.ว. = 4 ตร.ม.
        total_wa = area_sqm / 4
        rai = int(total_wa // 400)
        ngarn = int((total_wa % 400) // 100)
        wa = round(total_wa % 100, 1)

        return {
            "success": True,
            "lat": lat,
            "lon": lon,
            "rai": rai,
            "ngarn": ngarn,
            "wa": wa
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาดในการประมวลผล GIS: {str(e)}")
    
    finally:
        # 6. ลบไฟล์ขยะและโฟลเดอร์ชั่วคราวออกทั้งหมด ป้องกันเซิร์ฟเวอร์เต็ม
        shutil.rmtree(temp_dir, ignore_errors=True)

# โค้ดเดิมสำหรับเซฟและลบข้อมูลคดี (คงไว้เหมือนเดิม)
# @app.post("/process-shapefile/") ...
# @app.get("/get-cases/{case_type}") ...
# @app.delete("/delete-case/{case_type}/{case_no}") ...
