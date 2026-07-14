"""
ml_sku_resolver.py
==================
Resuelve el mapeo SKU-de-Bsale → item_id de Mercado Libre Chile y evalúa la Buy Box.
"""

from __future__ import annotations
import logging
import time
from typing import Optional
import requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

BASE_URL = "https://api.mercadolibre.com"
BATCH_SIZE = 20
PAGE_SIZE = 100
SCROLL_DELAY_SEC = 0.3
BATCH_DELAY_SEC = 0.2
EAN_ATTRIBUTE_IDS = {"EAN", "GTIN", "UPC", "ISBN", "MPN"}

class MLApiError(Exception):
    def __init__(self, status_code: int, message: str, url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} en {url}: {message}")

class MLSkuResolver:
    def __init__(self, access_token: str, site_id: str = "MLC"):
        if not access_token:
            raise ValueError("access_token no puede estar vacío.")
        self._token = access_token
        self._site_id = site_id
        self._seller_id: Optional[int] = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def resolver_sku(self, sku_raw: str) -> Optional[str]:
        variantes = self._normalizar_sku(sku_raw)
        log.info("Resolviendo SKU '%s' → variantes: %s", sku_raw, variantes)
        seller_id = self._obtener_seller_id()

        for variante in variantes:
            item_id = self._buscar_por_seller_sku(seller_id, variante)
            if item_id:
                log.info("Match rápido via seller_sku: %s → %s", variante, item_id)
                return item_id

        log.info("seller_sku vacío o no mapeado para '%s'. Iniciando fallback por EAN...", sku_raw)
        item_id = self._buscar_por_ean_scan(seller_id, variantes)
        if item_id:
            log.info("Match por EAN attribute: %s → %s", sku_raw, item_id)
        else:
            log.warning("No se encontró item_id para SKU '%s'.", sku_raw)
        return item_id

    def _obtener_seller_id(self) -> int:
        if self._seller_id is not None:
            return self._seller_id
        url = f"{BASE_URL}/users/me"
        resp = self._get(url)
        self._seller_id = int(resp["id"])
        log.info("seller_id obtenido: %d (nickname: %s)", self._seller_id, resp.get("nickname", "?"))
        return self._seller_id

    def _buscar_por_seller_sku(self, seller_id: int, sku: str) -> Optional[str]:
        url = f"{BASE_URL}/users/{seller_id}/items/search"
        params = {"seller_sku": sku, "status": "active", "limit": 1}
        try:
            resp = self._get(url, params=params)
            results = resp.get("results", [])
            return results[0] if results else None
        except MLApiError as e:
            log.debug("seller_sku query falló para '%s': %s", sku, e)
            return None

    def _buscar_por_ean_scan(self, seller_id: int, variantes_sku: list[str]) -> Optional[str]:
        variantes_set = set(variantes_sku)
        offset = 0
        while offset < 1000:
            item_ids = self._obtener_pagina_items(seller_id, offset)
            if not item_ids:
                break
            for i in range(0, len(item_ids), BATCH_SIZE):
                batch = item_ids[i : i + BATCH_SIZE]
                items_detalle = self._obtener_detalle_batch(batch)
                for item in items_detalle:
                    ean_values = self._extraer_eans(item)
                    if ean_values & variantes_set:
                        return item["id"]
                time.sleep(BATCH_DELAY_SEC)
            if len(item_ids) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(SCROLL_DELAY_SEC)
        return None

    def _obtener_pagina_items(self, seller_id: int, offset: int) -> list[str]:
        url = f"{BASE_URL}/users/{seller_id}/items/search"
        params = {"status": "active", "limit": PAGE_SIZE, "offset": offset}
        try:
            resp = self._get(url, params=params)
            return resp.get("results", [])
        except MLApiError as e:
            log.error("ErrorDoc obteniendo página de items (offset=%d): %s", offset, e)
            return []

    def _obtener_detalle_batch(self, item_ids: list[str]) -> list[dict]:
        ids_str = ",".join(item_ids)
        url = f"{BASE_URL}/items"
        params = {"ids": ids_str, "attributes": "id,seller_sku,attributes,catalog_product_id"}
        try:
            raw = self._get_lista(url, params=params)
            return [entry["body"] for entry in raw if isinstance(entry, dict) and entry.get("code") == 200]
        except MLApiError as e:
            log.error("Error en batch GET /items?ids=%s: %s", ids_str[:50], e)
            return []

    @staticmethod
    def _extraer_eans(item: dict) -> set[str]:
        eans: set[str] = set()
        atributos = item.get("attributes")
        if isinstance(atributos, list):
            for attr in atributos:
                if not isinstance(attr, dict):
                    continue
                raw_id = attr.get("id")
                if not raw_id:
                    continue
                attr_id = str(raw_id).upper()
                if attr_id in EAN_ATTRIBUTE_IDS:
                    val = attr.get("value_name")
                    if not val:
                        values_list = attr.get("values")
                        if isinstance(values_list, list) and len(values_list) > 0:
                            primer_valor = values_list[0]
                            if isinstance(primer_valor, dict):
                                val = primer_valor.get("name")
                    if val:
                        eans.add(str(val).strip())
                        eans.add(str(val).strip().lstrip("0"))
        
        sku = item.get("seller_sku")
        if sku:
            eans.add(str(sku).strip())
            eans.add(str(sku).strip().lstrip("0"))
            
        return eans

    @staticmethod
    def _normalizar_sku(sku_raw: str) -> list[str]:
        limpio = sku_raw.strip()
        sin_ceros = limpio.lstrip("0") or "0"
        variantes = [limpio, sin_ceros]
        if limpio.isdigit() and len(limpio) <= 13:
            con_ceros = limpio.zfill(13)
            if con_ceros not in variantes:
                variantes.append(con_ceros)
        vistas: set[str] = set()
        resultado: list[str] = []
        for v in variantes:
            if v not in vistas and v:
                vistas.add(v)
                resultado.append(v)
        return resultado

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        resp = self._session.get(url, params=params, timeout=15)
        if not resp.ok:
            try:
                msg = resp.json().get("message", resp.text[:200])
            except Exception:
                msg = resp.text[:200]
            raise MLApiError(resp.status_code, msg, url)
        return resp.json()

    def _get_lista(self, url: str, params: Optional[dict] = None) -> list:
        resp = self._session.get(url, params=params, timeout=15)
        if not resp.ok:
            try:
                msg = resp.json().get("message", resp.text[:200])
            except Exception:
                msg = resp.text[:200]
            raise MLApiError(resp.status_code, msg, url)
        data = resp.json()
        if not isinstance(data, list):
            raise MLApiError(200, f"Se esperaba lista, llegó {type(data).__name__}", url)
        return data


def obtener_contexto_buy_box(resolver: MLSkuResolver, sku_bsale: str) -> Optional[dict]:
    item_id = resolver.resolver_sku(sku_bsale)
    if not item_id:
        return None

    try:
        item_data = resolver._get(
            f"https://api.mercadolibre.com/items/{item_id}",
            params={"attributes": "id,catalog_product_id,price,seller_id"},
        )
    except Exception as e:
        return None

    catalog_product_id = item_data.get("catalog_product_id")
    precio_propio = item_data.get("price")

    if not catalog_product_id:
        return {
            "item_id": item_id,
            "catalog_product_id": None,
            "en_catalogo": False,
            "precio_propio": precio_propio,
        }

    try:
        catalog_data = resolver._get(
            f"https://api.mercadolibre.com/products/{catalog_product_id}"
        )
    except Exception as e:
        return {
            "item_id": item_id,
            "catalog_product_id": catalog_product_id,
            "en_catalogo": True,
            "precio_propio": precio_propio,
            "error_buy_box": str(e),
        }

    buy_box_winner = catalog_data.get("buy_box_winner")
    winner_id = None
    precio_buy_box = None
    ganando = False

    if isinstance(buy_box_winner, dict):
        winner_id = buy_box_winner.get("item_id")
        precio_buy_box = buy_box_winner.get("price")
        ganando = (winner_id == item_id)

    # 🌞 PLAN B: Paginación profunda en el catálogo oficial
    if not precio_buy_box:
        try:
            mejor_precio = float('inf')
            mejor_item_id = None
            offset = 0

            while offset < 500:
                items_cat = resolver._get(
                    f"https://api.mercadolibre.com/products/{catalog_product_id}/items",
                    params={"status": "active", "limit": 50, "offset": offset}
                )

                lista_competidores = items_cat.get("results") or items_cat.get("items_with_buy_box") or []

                if not lista_competidores:
                    break

                for comp in lista_competidores:
                    comp_price = comp.get("price")
                    comp_item_id = comp.get("item_id") or comp.get("id")

                    if comp_price and comp_item_id != item_id:
                        comp_price_float = float(comp_price)
                        if comp_price_float < mejor_precio:
                            mejor_precio = comp_price_float
                            mejor_item_id = comp_item_id

                offset += 50
                if len(lista_competidores) < 50:
                    break

            if mejor_precio != float('inf'):
                precio_buy_box = mejor_precio
                winner_id = mejor_item_id

        except Exception as e:
            pass

    return {
        "item_id": item_id,
        "catalog_product_id": catalog_product_id,
        "en_catalogo": True,
        "precio_propio": precio_propio,
        "precio_buy_box": precio_buy_box,
        "ganando_buy_box": ganando,
        "buy_box_winner_id": winner_id
    }