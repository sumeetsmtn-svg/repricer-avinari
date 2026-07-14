#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════╗
║        repricer.py — Repricer Automático                     ║
║        Bsale (fuente de verdad) ↔ Mercado Libre (Catálogo)   ║
║        v4.3 DEFINITIVA — Conexión con Streamlit App          ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import requests
import time
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

from ml_sku_resolver import MLSkuResolver, obtener_contexto_buy_box

load_dotenv()

# ══════════════════════════════════════════════════════════════
#  ZONA DE CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════

MODO_PRUEBA = True

BSALE_TOKEN         = os.environ["BSALE_TOKEN"]
BSALE_PRICE_LIST_ID = 7

ML_APP_ID     = os.environ["ML_APP_ID"]
ML_SECRET_KEY = os.environ["ML_SECRET_KEY"]

MARGEN_MINIMO_PCT  = 0.00 
DIFERENCIAL_PRECIO = 100   

PAUSA_BSALE     = 0.35 
PAUSA_ML        = 0.50 

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("repricer")

@dataclass
class Producto:
    variant_id: int
    detail_id: int
    sku: str
    nombre: str
    precio_actual: float
    costo: float
    precio_minimo: float = field(init=False)

    def __post_init__(self):
        self.precio_minimo = round(self.costo * (1 + MARGEN_MINIMO_PCT), 2)

BSALE_BASE = "https://api.bsale.io/v1"
BSALE_HDRS = {"access_token": BSALE_TOKEN, "Content-Type": "application/json"}

def _bsale_get(endpoint: str, params: dict = None) -> dict:
    r = requests.get(f"{BSALE_BASE}{endpoint}", headers=BSALE_HDRS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

# 👇 AQUÍ ESTÁ LA MAGIA QUE FALTABA
def bsale_cargar_productos(limite_escaneo: int = 10) -> list[Producto]:
    log.info(f"Bsale → Descargando catálogo (Límite: {limite_escaneo})...")
    stocks = _bsale_get("/stocks.json", {"state": 1, "limit": limite_escaneo})
    productos = []
    
    for s in stocks.get("items", []):
        if float(s.get("quantity") or 0) <= 0: continue
        href = (s.get("variant") or {}).get("href", "")
        if not href: continue
        vid = int(href.rstrip("/").replace(".json", "").split("/")[-1])
        
        data = _bsale_get(f"/variants/{vid}.json")
        sku = (data.get("code") or "").strip()
        if not sku: continue
        
        nombre = (data.get("description") or "").strip()
        if nombre == "." or nombre == "":
            p_id = data.get("product", {}).get("id")
            if p_id: nombre = (_bsale_get(f"/products/{p_id}.json").get("name") or "").strip()
            
        cost_data = _bsale_get(f"/variants/{vid}/costs.json")
        costo = float(cost_data.get("averageCost") or cost_data.get("totalCost") or 1.0) if cost_data else 1.0
        
        pl_data = _bsale_get(f"/price_lists/{BSALE_PRICE_LIST_ID}/details.json", {"variantid": vid})
        if not pl_data.get("items"): continue
        det = pl_data["items"][0]
        precio_actual_bsale = float(det.get("variantValue") or 0)
        
        meta_bruta = (costo * 1.07) * 1.19
        p_min = (meta_bruta + 790) / 0.86
        if p_min >= 9980: p_min = (meta_bruta + 1000) / 0.86
        if p_min >= 19990: p_min = (meta_bruta + 3100) / 0.86
        
        productos.append(Producto(vid, int(det["id"]), sku, nombre, precio_actual_bsale, p_min))
        
    return productos

def ml_actualizar_precio_item(token: str, item_id: str, nuevo_precio: float) -> bool:
    url = f"https://api.mercadolibre.com/items/{item_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {"price": nuevo_precio}
    
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"  ✗ Error al actualizar precio en ML para {item_id}: {e}")
        return False

def ejecutar() -> None:
    pass

if __name__ == "__main__":
    ejecutar()