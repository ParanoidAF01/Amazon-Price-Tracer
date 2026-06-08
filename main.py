"""
FastAPI backend
---------------
Serves:
  GET  /              → public dashboard (dark UI)
  GET  /admin         → admin panel (password protected)
  POST /admin/login   → session login
  GET  /api/products  → JSON list of products with price data
  POST /api/products  → add product
  PUT  /api/products/{id} → edit product
  DELETE /api/products/{id} → remove product
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from supabase import create_client, Client

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "changeme123")
SESSION_SECRET  = os.environ.get("SESSION_SECRET", "supersecretkey")
PORT            = int(os.environ.get("PORT", 8000))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = App = FastAPI(title="Amazon Price Tracker")
templates = Jinja2Templates(directory="templates")

# ── Auth helper ───────────────────────────────────────────────────────────────
def is_authenticated(request: Request) -> bool:
    return request.cookies.get("session") == SESSION_SECRET

def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")

# ── ASIN extractor ────────────────────────────────────────────────────────────
def extract_asin(url: str) -> Optional[str]:
    for pat in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

# ── Data helpers ──────────────────────────────────────────────────────────────
def get_products_with_prices():
    products = supabase.table("products").select("*").eq("active", True).execute().data or []
    result = []
    for p in products:
        history = (
            supabase.table("price_history")
            .select("price,checked_at")
            .eq("product_id", p["id"])
            .order("checked_at", desc=True)
            .limit(48)   # last 24 hrs at 30-min interval
            .execute()
            .data or []
        )
        current_price = history[0]["price"] if history else None
        lowest_price  = min((h["price"] for h in history), default=None)
        result.append({
            **p,
            "current_price": current_price,
            "lowest_price":  lowest_price,
            "history":       list(reversed(history)),
        })
    return result

def get_all_products():
    return supabase.table("products").select("*").order("created_at", desc=True).execute().data or []

# ── Public dashboard ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    products = get_products_with_prices()
    return templates.TemplateResponse("dashboard.html", {
        "request":  request,
        "products": products,
        "now":      datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    })

# ── Admin login ───────────────────────────────────────────────────────────────
@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/admin", status_code=302)
    return templates.TemplateResponse("admin.html", {"request": request, "view": "login", "error": None})

@app.post("/admin/login")
async def login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse("/admin", status_code=302)
        resp.set_cookie("session", SESSION_SECRET, httponly=True, samesite="lax")
        return resp
    return templates.TemplateResponse("admin.html", {
        "request": request, "view": "login", "error": "Wrong password"
    }, status_code=401)

@app.get("/admin/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=302)
    resp.delete_cookie("session")
    return resp

# ── Admin panel ───────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=302)
    products = get_all_products()
    return templates.TemplateResponse("admin.html", {
        "request":  request,
        "view":     "panel",
        "products": products,
    })

# ── REST API ──────────────────────────────────────────────────────────────────
class ProductIn(BaseModel):
    url:            str
    name:           Optional[str] = None
    baseline_price: Optional[float] = None

@app.get("/api/products")
async def api_get_products(request: Request):
    require_auth(request)
    return get_all_products()

@app.post("/api/products", status_code=201)
async def api_add_product(body: ProductIn, request: Request):
    require_auth(request)
    if not body.url.startswith("http"):
        raise HTTPException(400, "Invalid URL")
    asin = extract_asin(body.url)
    data = {
        "url":            body.url,
        "asin":           asin,
        "name":           body.name or asin or "New product",
        "baseline_price": body.baseline_price,
        "active":         True,
    }
    res = supabase.table("products").insert(data).execute()
    return res.data[0]

@app.put("/api/products/{product_id}")
async def api_update_product(product_id: str, body: ProductIn, request: Request):
    require_auth(request)
    data = {k: v for k, v in {
        "url":            body.url,
        "name":           body.name,
        "baseline_price": body.baseline_price,
    }.items() if v is not None}
    res = supabase.table("products").update(data).eq("id", product_id).execute()
    if not res.data:
        raise HTTPException(404, "Product not found")
    return res.data[0]

@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: str, request: Request):
    require_auth(request)
    supabase.table("products").delete().eq("id", product_id).execute()
    return {"deleted": True}

@app.put("/api/products/{product_id}/toggle")
async def api_toggle_product(product_id: str, request: Request):
    require_auth(request)
    current = supabase.table("products").select("active").eq("id", product_id).execute().data
    if not current:
        raise HTTPException(404, "Product not found")
    new_state = not current[0]["active"]
    res = supabase.table("products").update({"active": new_state}).eq("id", product_id).execute()
    return res.data[0]

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
