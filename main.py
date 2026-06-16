"""
DNP GIS Case API — Production v5.0
ระบบ API สารบบคดีเชิงพื้นที่ สบอ.13 ลำปาง
(แก้ไขฟังก์ชันการอ่าน Shapefile ให้มีความเสถียร)
"""

import os, shutil, tempfile, json, math, time, logging, traceback, re, zipfile
from typing import Optional, List
from functools import lru_cache

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
import geopandas as gpd
from supabase import create_client, Client
import fiona
from shapely.geometry import shape
from shapely.validation import make_valid

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
    version="5.0.1",
)

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://natthawutfrdo.github.io,http://localhost:3000,http://127.0.0.1:5500").split(",") if o.strip()
]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ─────────────────────────────────────────────────
# 3. SUPABASE CLIENT
# ─────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

def get_db():
    if not supabase: raise HTTPException(503, "Database not connected")
    return supabase

# ─────────────────────────────────────────────────
# 4. FIXED GIS CORE
# ─────────────────────────────────────────────────
def _read_shp_via_fiona(shp_path: str):
    """
    ฟังก์ชันแก้ไขการอ่าน Shapefile ที่เสถียรขึ้น:
    1. วนลูปหา Encoding ที่อ่านได้
    2. ใช้ fiona.crs จาก source ตรงๆ เพื่อตั้งค่าพิกัด
    3. สร้าง GeoDataFrame ใหม่จากข้อมูลที่อ่านได้โดยตรง
    """
    _ENCODINGS = ["utf-8", "tis-620", "cp874", "cp1252", "latin-1"]
    rows = []
    fiona_crs = None
    success = False

    # พยายามอ่านไฟล์
    for enc in _ENCODINGS:
        try:
            with fiona.open(shp_path, encoding=enc) as src:
                fiona_crs = src.crs  # ดึง CRS object ตรงๆ
                for feat in src:
                    geom = None
                    raw = feat.get("geometry")
                    if raw:
                        geom = shape(raw)
                        if not geom.is_valid: geom = make_valid(geom)
                    
                    props = dict(feat.get("properties", {}))
                    rows.append({"geometry": geom, **props})
            
            if rows:
                success = True
                log.info(f"[GIS] Read success with encoding: {enc}")
                break
        except Exception as e:
            log.warning(f"[GIS] Read failed with encoding {enc}: {e}")

    if not success or not rows:
        raise HTTPException(400, "ไฟล์ Shapefile ไม่มีข้อมูล หรือไฟล์ชุด .shp/.shx ไม่ถูกต้อง")

    # สร้าง GeoDataFrame
    gdf = gpd.GeoDataFrame(rows, geometry="geometry")

    # การตั้งค่า CRS ที่เสถียรที่สุด
    if fiona_crs:
        try:
            gdf.set_crs(fiona_crs, allow_override=True, inplace=True)
            log.info("[GIS] CRS set successfully via fiona_crs")
        except Exception as e:
            log.warning(f"[GIS] set_crs failed: {e}. Falling back to 32647")
            gdf.set_crs(epsg=32647, allow_override=True, inplace=True)
    else:
        log.warning("[GIS] No CRS found. Falling back to 32647")
        gdf.set_crs(epsg=32647, allow_override=True, inplace=True)

    return gdf

def extract_gis_from_shp(shp_path: str) -> dict:
    gdf = _read_shp_via_fiona(shp_path)
    
    try:
        gdf84 = gdf.to_crs(epsg=4326)
        c = gdf84.geometry.centroid.iloc[0]
        lat, lon = float(c.y), float(c.x)
    except:
        lat, lon = 18.29, 99.50
        gdf84 = gdf

    try:
        area_sqm = float(gdf.to_crs(epsg=32647).geometry.area.sum())
    except:
        area_sqm = 0.0

    total_wa = area_sqm / 4.0
    return {
        "gdf84": gdf84,
        "lat": lat, "lon": lon,
        "rai": int(total_wa // 400),
        "ngarn": int((total_wa % 400) // 100),
        "wa": int(round(total_wa % 100)),
        "utm_zone": 47,
        "utm_easting": 0,
        "utm_northing": 0,
    }

# ─────────────────────────────────────────────────
# 5. ASSEMBLE HELPER
# ─────────────────────────────────────────────────
async def assemble_shapefile(tmpdir, shp, dbf, shx, prj, cpg) -> str:
    base = os.path.join(tmpdir, "input")
    for f, ext in [(shp, ".shp"), (dbf, ".dbf"), (shx, ".shx")]:
        if f:
            with open(f"{base}{ext}", "wb") as wf: wf.write(await f.read())
    if prj and prj.filename:
        with open(f"{base}.prj", "wb") as wf: wf.write(await prj.read())
    return f"{base}.shp"

# ─────────────────────────────────────────────────
# 6. ENDPOINTS (เหลือส่วนที่เหลือให้ครบ)
# ─────────────────────────────────────────────────
@app.post("/debug-shp/")
async def debug_shp(
    shp_file: UploadFile = File(...),
    dbf_file: UploadFile = File(...),
    shx_file: UploadFile = File(...),
    prj_file: Optional[UploadFile] = File(None),
):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            shp_path = await assemble_shapefile(tmp, shp_file, dbf_file, shx_file, prj_file, None)
            gdf = _read_shp_via_fiona(shp_path)
            return {"success": True, "rows": len(gdf), "crs": str(gdf.crs)}
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}

@app.post("/analyze-shapefile/")
async def analyze_shapefile(
    shp_file: UploadFile = File(...),
    dbf_file: UploadFile = File(...),
    shx_file: UploadFile = File(...),
    prj_file: Optional[UploadFile] = File(None),
):
    with tempfile.TemporaryDirectory() as tmp:
        shp_path = await assemble_shapefile(tmp, shp_file, dbf_file, shx_file, prj_file, None)
        gis = extract_gis_from_shp(shp_path)
        return {
            "success": True, "lat": gis["lat"], "lon": gis["lon"],
            "rai": gis["rai"], "ngarn": gis["ngarn"], "wa": gis["wa"]
        }

# (เพิ่ม Endpoint process-shapefile และส่วนอื่นๆ ของคุณต่อท้ายตรงนี้ได้เลยครับ)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
