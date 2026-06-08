import os
import shutil
import tempfile
import zipfile
import json
import math
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import geopandas as gpd
from supabase import create_client, Client

app = FastAPI(title="DNP GIS Case API Systems")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase: Optional[Client] = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as init_err:
        print(f"❌ ไม่สามารถเริ่มต้น Supabase Client: {init_err}")
        supabase = None
else:
    supabase = None


# =============================================================
# ✅ ฟังก์ชัน WGS84 (Lat/Lon) → UTM (Pure Python ไม่ต้องลง lib เพิ่ม)
# =============================================================
def latlon_to_utm(lat: float, lon: float) -> dict:
    """
    แปลงพิกัด WGS84 (Latitude, Longitude) → UTM (Zone, Easting, Northing)
    ใช้สูตร Transverse Mercator มาตรฐาน WGS84 Ellipsoid
    """
    try:
        # คำนวณ Zone อัตโนมัติจากลองจิจูด
        zone = int((lon + 180) / 6) + 1

        k0    = 0.9996
        a     = 6378137.0          # WGS84 semi-major axis (เมตร)
        e     = 0.0818191908426215  # eccentricity
        e2    = e * e
        e4    = e2 * e2
        e6    = e4 * e2
        e1sq  = e2 / (1 - e2)      # e'^2

        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        lon_origin_rad = math.radians((zone - 1) * 6 - 180 + 3)

        N = a / math.sqrt(1 - e2 * math.sin(lat_rad) ** 2)
        T = math.tan(lat_rad) ** 2
        C = e1sq * math.cos(lat_rad) ** 2
        A = math.cos(lat_rad) * (lon_rad - lon_origin_rad)

        M = a * (
            (1 - e2/4 - 3*e4/64 - 5*e6/256)       * lat_rad
          - (3*e2/8 + 3*e4/32 + 45*e6/1024)        * math.sin(2 * lat_rad)
          + (15*e4/256 + 45*e6/1024)                * math.sin(4 * lat_rad)
          - (35*e6/3072)                             * math.sin(6 * lat_rad)
        )

        easting = k0 * N * (
            A
          + (1 - T + C) * A**3 / 6
          + (5 - 18*T + T**2 + 72*C - 58*e1sq) * A**5 / 120
        ) + 500000.0

        northing = k0 * (
            M + N * math.tan(lat_rad) * (
                A**2 / 2
              + (5 - T + 9*C + 4*C**2)                      * A**4 / 24
              + (61 - 58*T + T**2 + 600*C - 330*e1sq)        * A**6 / 720
            )
        )
        # ซีกโลกใต้ให้บวก 10,000,000 เมตร (ไทยอยู่ซีกเหนือ ไม่ต้องบวก)
        if lat < 0:
            northing += 10000000.0

        return {
            "utm_zone":     zone,
            "utm_easting":  int(round(easting)),
            "utm_northing": int(round(northing))
        }
    except Exception as e:
        print(f"⚠️ แปลง lat/lon → UTM ผิดพลาด: {e}")
        return {"utm_zone": 47, "utm_easting": 0, "utm_northing": 0}


# =============================================================
# ✅ ฟังก์ชันหลัก: แตก Shapefile → คำนวณพิกัด + พื้นที่ + UTM
# =============================================================
def extract_gis_and_calculate(zip_path: str, extract_dir: str):
    """
    ถอดรหัส Shapefile จาก .zip แล้ว:
      1. หาจุดกึ่งกลาง (Centroid) → Lat/Lon WGS84
      2. คำนวณพื้นที่ → ไร่-งาน-ตารางวา
      3. แปลง Centroid → UTM (Zone, Easting, Northing)
    คืนค่า: dict ที่มีทุก field
    """
    try:
        # แตกไฟล์ zip
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        # หาไฟล์ .shp (รองรับ subfolder)
        shp_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.endswith('.shp'):
                    shp_files.append(os.path.join(root, f))

        if not shp_files:
            raise HTTPException(
                status_code=400,
                detail="ไม่พบไฟล์ .shp ภายในไฟล์ Zip ที่ส่งมา"
            )

        shp_path = shp_files[0]

        # อ่าน shapefile
        try:
            gdf = gpd.read_file(shp_path)
        except Exception as read_err:
            raise HTTPException(
                status_code=400,
                detail=f"ไม่สามารถเปิดอ่านไฟล์ Shapefile: {read_err}"
            )

        if gdf.empty:
            raise HTTPException(
                status_code=400,
                detail="ไฟล์ Shapefile ไม่มีข้อมูลเชิงพื้นที่"
            )

        # กำหนด CRS ถ้าไม่มี (สมมติ UTM Zone 47N สำหรับลำปาง)
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=32647)

        # ----- [1] หา Centroid ใน WGS84 (EPSG:4326) -----
        try:
            gdf_wgs84    = gdf.to_crs(epsg=4326)
            centroid     = gdf_wgs84.geometry.centroid.iloc[0]
            lat_val      = float(centroid.y)
            lon_val      = float(centroid.x)
            calculated_coords = [lat_val, lon_val]
        except Exception:
            calculated_coords = [18.29, 99.50]
            gdf_wgs84 = gdf

        # ----- [2] คำนวณพื้นที่ใน UTM (EPSG:32647) -----
        try:
            gdf_utm  = gdf.to_crs(epsg=32647)
            area_sqm = float(gdf_utm.geometry.area.sum())
            area_sqm = max(area_sqm, 0.0)
        except Exception:
            area_sqm = 0.0

        total_wa = area_sqm / 4.0
        rai      = int(total_wa // 400)
        ngarn    = int((total_wa % 400) // 100)
        wa       = int(round(total_wa % 100))

        # ----- [3] แปลง Centroid → UTM -----
        utm_result = latlon_to_utm(calculated_coords[0], calculated_coords[1])

        return {
            "gdf_wgs84":    gdf_wgs84,
            "coords":       calculated_coords,
            "rai":          rai,
            "ngarn":        ngarn,
            "wa":           wa,
            # ✅ เพิ่ม UTM
            "utm_zone":     utm_result["utm_zone"],
            "utm_easting":  utm_result["utm_easting"],
            "utm_northing": utm_result["utm_northing"],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"ระบบคำนวณ GIS ขัดข้อง: {e}"
        )


# =============================================================
# Endpoint: GET /
# =============================================================
@app.get("/")
def read_root():
    return {"message": "DNP GIS Case API is running!"}


# =============================================================
# Endpoint: POST /analyze-shapefile/
# (วิเคราะห์ไฟล์ Shapefile เบื้องต้น ก่อนบันทึกจริง)
# =============================================================
@app.post("/analyze-shapefile/")
async def analyze_shapefile(file: UploadFile = File(...)):
    if not file.filename.endswith('.zip'):
        raise HTTPException(
            status_code=400,
            detail="กรุณาอัปโหลดไฟล์ .zip เท่านั้น"
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, file.filename)
        with open(zip_path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        extract_dir = os.path.join(temp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        gis = extract_gis_and_calculate(zip_path, extract_dir)

        # ✅ คืนค่า UTM กลับไปให้ Frontend ด้วย
        return {
            "success":      True,
            "lat":          gis["coords"][0],
            "lon":          gis["coords"][1],
            "rai":          gis["rai"],
            "ngarn":        gis["ngarn"],
            "wa":           gis["wa"],
            "utm_zone":     gis["utm_zone"],
            "utm_easting":  gis["utm_easting"],
            "utm_northing": gis["utm_northing"],
        }


# =============================================================
# Endpoint: POST /process-shapefile/
# (บันทึกคดีบุกรุก / คดีไม้ พร้อม Shapefile)
# =============================================================
@app.post("/process-shapefile/")
async def process_shapefile(
    file:           UploadFile = File(...),
    pdf_file:       UploadFile = File(None),
    case_type:      str   = Form(...),
    case_no:        str   = Form(...),
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
    # ✅ รับ UTM จาก Frontend (ที่ผู้ใช้กรอกมือ หรือได้จาก analyze-shapefile)
    utm_zone:       int   = Form(47),
    utm_easting:    int   = Form(0),
    utm_northing:   int   = Form(0),
):
    if not supabase:
        raise HTTPException(status_code=500, detail="ยังไม่ได้เชื่อมต่อ Supabase")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:

            # --- บันทึกไฟล์ zip ---
            zip_path = os.path.join(temp_dir, file.filename)
            with open(zip_path, "wb") as buf:
                shutil.copyfileobj(file.file, buf)

            extract_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)

            # --- ประมวลผล GIS ---
            gis = extract_gis_and_calculate(zip_path, extract_dir)
            gdf_wgs84         = gis["gdf_wgs84"]
            calculated_coords = gis["coords"]

            # ✅ ถ้าผู้ใช้ไม่ได้กรอก UTM เอง ให้ใช้ค่าที่คำนวณจาก Shapefile
            final_utm_zone     = utm_zone     if utm_easting != 0 else gis["utm_zone"]
            final_utm_easting  = utm_easting  if utm_easting != 0 else gis["utm_easting"]
            final_utm_northing = utm_northing if utm_northing != 0 else gis["utm_northing"]

            # ✅ ถ้าช่องพื้นที่ยังเป็น 0 ให้ใช้ค่าจาก Shapefile
            final_rai   = int(rai)   if rai   > 0 else gis["rai"]
            final_ngarn = int(ngarn) if ngarn > 0 else gis["ngarn"]
            final_wa    = int(round(wa)) if wa > 0 else gis["wa"]

            # --- Simplify geometry ---
            try:
                gdf_wgs84['geometry'] = gdf_wgs84['geometry'].simplify(
                    tolerance=0.0001, preserve_topology=True
                )
            except Exception:
                pass

            # --- อัปโหลด Shapefile zip ต้นฉบับ ---
            clean_fn = f"{case_no.replace('/', '_')}_{file.filename.replace(' ', '_')}"
            try:
                with open(zip_path, "rb") as f_data:
                    supabase.storage.from_("dnp-shapefiles").upload(
                        path=clean_fn,
                        file=f_data,
                        file_options={"cache-control": "3600", "upsert": "true"}
                    )
                shapefile_url = supabase.storage.from_("dnp-shapefiles").get_public_url(clean_fn)
            except Exception as e:
                return {"success": False, "error": f"อัปโหลด Shapefile ผิดพลาด: {e}"}

            # --- อัปโหลด PDF (ถ้ามี) ---
            pdf_url = ""
            if pdf_file:
                try:
                    pdf_fn       = f"{case_no.replace('/', '_')}_{pdf_file.filename.replace(' ', '_')}"
                    pdf_tmp_path = os.path.join(temp_dir, pdf_fn)
                    with open(pdf_tmp_path, "wb") as buf:
                        shutil.copyfileobj(pdf_file.file, buf)
                    with open(pdf_tmp_path, "rb") as pd:
                        supabase.storage.from_("dnp-pdfs").upload(
                            path=pdf_fn, file=pd,
                            file_options={"cache-control": "3600", "upsert": "true"}
                        )
                    pdf_url = supabase.storage.from_("dnp-pdfs").get_public_url(pdf_fn)
                except Exception as e:
                    print(f"⚠️ อัปโหลด PDF ล้มเหลว: {e}")

            # --- สร้างและอัปโหลด GeoJSON ---
            try:
                geojson_fn   = f"{case_no.replace('/', '_')}_map.json"
                geojson_str  = gdf_wgs84.to_json()
                geojson_path = os.path.join(temp_dir, geojson_fn)
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

            # --- บันทึกลง Supabase Database ---
            is_finished = (status == "คดีสิ้นสุด")

            try:
                if case_type == "encroachment":
                    db_data = {
                        "case_no":        case_no,
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
                        # ✅ เพิ่ม UTM
                        "utm_zone":       final_utm_zone,
                        "utm_easting":    final_utm_easting,
                        "utm_northing":   final_utm_northing,
                    }
                    supabase.table("encroachment_cases").insert(db_data).execute()

                elif case_type == "timber":
                    db_data = {
                        "case_no":        case_no,
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
                        # ✅ เพิ่ม UTM
                        "utm_zone":       final_utm_zone,
                        "utm_easting":    final_utm_easting,
                        "utm_northing":   final_utm_northing,
                    }
                    supabase.table("timber_cases").insert(db_data).execute()

            except Exception as e:
                return {"success": False, "error": f"บันทึก Database ผิดพลาด: {e}"}

            return {
                "success":      True,
                "message":      "บันทึกข้อมูลสำเร็จ",
                "coords":       calculated_coords,
                "geojson_url":  geojson_url,
                "utm_zone":     final_utm_zone,
                "utm_easting":  final_utm_easting,
                "utm_northing": final_utm_northing,
            }

    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================
# Endpoint: POST /process-wildlife/
# (บันทึกคดีสัตว์ป่า ใช้พิกัดจุด ไม่มี Shapefile)
# =============================================================
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
    # ✅ รับ UTM จาก Frontend
    utm_zone:       int   = Form(47),
    utm_easting:    int   = Form(0),
    utm_northing:   int   = Form(0),
):
    if not supabase:
        raise HTTPException(status_code=500, detail="ยังไม่ได้เชื่อมต่อ Supabase")

    try:
        # --- อัปโหลด PDF ---
        pdf_url = ""
        if pdf_file:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_fn   = f"{case_no.replace('/', '_')}_{pdf_file.filename.replace(' ', '_')}"
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
                print(f"⚠️ อัปโหลด PDF ล้มเหลว: {e}")

        is_finished = (status == "คดีสิ้นสุด")
        coords = (
            [coords_lat, coords_lon]
            if (coords_lat != 0.0 or coords_lon != 0.0)
            else [18.29, 99.50]
        )

        # ✅ ถ้าผู้ใช้ไม่ได้กรอก UTM แต่มี Lat/Lon → คำนวณ UTM อัตโนมัติ
        final_utm_zone     = utm_zone
        final_utm_easting  = utm_easting
        final_utm_northing = utm_northing

        if utm_easting == 0 and utm_northing == 0 and coords_lat != 0.0:
            utm_auto = latlon_to_utm(coords[0], coords[1])
            final_utm_zone     = utm_auto["utm_zone"]
            final_utm_easting  = utm_auto["utm_easting"]
            final_utm_northing = utm_auto["utm_northing"]

        # ✅ ถ้าผู้ใช้กรอก UTM แต่ไม่มี Lat/Lon → แปลง UTM → Lat/Lon
        if coords_lat == 0.0 and utm_easting != 0:
            try:
                ll = utm_to_latlon_python(utm_zone, utm_easting, utm_northing)
                coords = [ll["lat"], ll["lon"]]
            except Exception:
                coords = [18.29, 99.50]

        db_data = {
            "case_no":        case_no,
            "case_date":      case_date,
            "location":       location,
            "wildlife_type":  wildlife_type,
            "equipment":      equipment,
            "is_finished":    is_finished,
            "case_status":    case_status,
            "coords":         coords,
            "agency":         agency,
            "suspects_count": int(suspects_count),
            "pdf_url":        pdf_url,
            # ✅ เพิ่ม UTM
            "utm_zone":       final_utm_zone,
            "utm_easting":    final_utm_easting,
            "utm_northing":   final_utm_northing,
        }
        supabase.table("wildlife_cases").insert(db_data).execute()

        return {
            "success":      True,
            "message":      "บันทึกข้อมูลคดีสัตว์ป่าสำเร็จ",
            "coords":       coords,
            "utm_zone":     final_utm_zone,
            "utm_easting":  final_utm_easting,
            "utm_northing": final_utm_northing,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Helper: UTM → Lat/Lon (ฝั่ง Python สำหรับกรณีกรอก UTM แต่ไม่มี Lat/Lon)
def utm_to_latlon_python(zone: int, easting: float, northing: float,
                          is_north: bool = True) -> dict:
    k0   = 0.9996
    a    = 6378137.0
    e    = 0.0818191908426215
    e2   = e * e
    e4   = e2 * e2
    e6   = e4 * e2
    e1sq = e2 / (1 - e2)

    x = easting - 500000.0
    y = northing if is_north else northing - 10000000.0

    lon_origin = (zone - 1) * 6 - 180 + 3
    M   = y / k0
    mu  = M / (a * (1 - e2/4 - 3*e4/64 - 5*e6/256))

    phi1 = (mu
        + (3/2*e2 + 27/32*e4 + 55/512*e6) * math.sin(2*mu)
        + (21/16*e4 + 55/32*e6)            * math.sin(4*mu)
        + (151/96*e6)                        * math.sin(6*mu))

    N1 = a / math.sqrt(1 - e2 * math.sin(phi1)**2)
    T1 = math.tan(phi1)**2
    C1 = e1sq * math.cos(phi1)**2
    R1 = a * (1 - e2) / (1 - e2 * math.sin(phi1)**2)**1.5
    D  = x / (N1 * k0)

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D**2/2
        - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*e1sq)              * D**4/24
        + (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*e1sq - 3*C1**2) * D**6/720
    )
    lon = (
        D
        - (1 + 2*T1 + C1) * D**3/6
        + (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*e1sq + 24*T1**2) * D**5/120
    ) / math.cos(phi1)

    return {
        "lat": math.degrees(lat),
        "lon": lon_origin + math.degrees(lon)
    }


# =============================================================
# Endpoint: GET /get-cases/{case_type}
# =============================================================
@app.get("/get-cases/{case_type}")
async def get_cases(case_type: str):
    if not supabase:
        raise HTTPException(status_code=500, detail="ยังไม่ได้เชื่อมต่อ Supabase")

    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(status_code=400, detail="ประเภทคดีไม่ถูกต้อง")

    try:
        res = supabase.table(table_map[case_type]).select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================
# Endpoint: DELETE /delete-case/{case_type}/{case_no}
# =============================================================
@app.delete("/delete-case/{case_type}/{case_no}")
async def delete_case(case_type: str, case_no: str):
    if not supabase:
        raise HTTPException(status_code=500, detail="ยังไม่ได้เชื่อมต่อ Supabase")

    table_map = {
        "encroachment": "encroachment_cases",
        "timber":       "timber_cases",
        "wildlife":     "wildlife_cases",
    }
    if case_type not in table_map:
        raise HTTPException(status_code=400, detail="ประเภทคดีไม่ถูกต้อง")

    try:
        supabase.table(table_map[case_type]).delete().eq("case_no", case_no).execute()
        return {"message": f"ลบข้อมูลคดี {case_no} สำเร็จ"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
