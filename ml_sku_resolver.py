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
MAX_ITEMS_INDICE_EAN = 10000  # tope de seguridad; cubre catálogos de varios miles de productos

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
        self._indice_ean: Optional[dict[str, str]] = None
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

        # El índice de EAN se construye una sola vez por sesión (no una vez por SKU),
        # así el costo del escaneo completo del catálogo se paga solo la primera vez
        # que se necesita, y no en cada SKU que no matchea por seller_sku.
        indice = self._obtener_indice_ean(seller_id)
        for variante in variantes:
            item_id = indice.get(variante)
            if item_id:
                log.info("Match por EAN (índice): %s → %s", variante, item_id)
                return item_id

        log.warning("No se encontró item_id para SKU '%s'.", sku_raw)
        return None

    def _obtener_seller_id(self) -> int:
        if self._seller_id is not None:
            return self._seller_id
        url = f"{BASE_URL}/users/me"
        resp = self._get(url)
        self._seller_id = int(resp["id"])
        log.info("seller_id obtenido: %d (nickname: %s)", self._seller_id, resp.get("nickname", "?"))
        return self._seller_id

    def _buscar_por_seller_sku(self, seller_id: int, sku: str) -> Optional[str]:
        # Sin filtro de estado: también deben encontrarse productos pausados (sin stock)
        # o en revisión, ya que igual sirven para consultar precio de la competencia
        # antes de reponer stock.
        url = f"{BASE_URL}/users/{seller_id}/items/search"
        params = {"seller_sku": sku, "limit": 1}
        try:
            resp = self._get(url, params=params)
            results = resp.get("results", [])
            return results[0] if results else None
        except MLApiError as e:
            log.debug("seller_sku query falló para '%s': %s", sku, e)
            return None

    def _obtener_indice_ean(self, seller_id: int) -> dict[str, str]:
        if self._indice_ean is not None:
            return self._indice_ean

        log.info("Construyendo índice de EAN del catálogo activo (una sola vez por sesión)...")
        indice: dict[str, str] = {}
        total_escaneado = 0
        scroll_id = None

        while total_escaneado < MAX_ITEMS_INDICE_EAN:
            item_ids, scroll_id = self._obtener_pagina_items_scroll(seller_id, scroll_id)
            if not item_ids:
                break
            total_escaneado += len(item_ids)

            for i in range(0, len(item_ids), BATCH_SIZE):
                batch = item_ids[i : i + BATCH_SIZE]
                items_detalle = self._obtener_detalle_batch(batch)
                for item in items_detalle:
                    for ean in self._extraer_eans(item):
                        indice.setdefault(ean, item["id"])
                time.sleep(BATCH_DELAY_SEC)

            if len(item_ids) < PAGE_SIZE or not scroll_id:
                break
            time.sleep(SCROLL_DELAY_SEC)

        log.info("Índice de EAN construido: %d valores mapeados (de %d items escaneados).", len(indice), total_escaneado)
        self._indice_ean = indice
        return indice

    def _obtener_pagina_items_scroll(self, seller_id: int, scroll_id: Optional[str]) -> tuple[list[str], Optional[str]]:
        # /items/search con offset solo permite escanear los primeros 1000 resultados
        # (Mercado Libre devuelve 400 más allá de eso). Para catálogos más grandes se
        # usa la paginación "scroll" oficial, que no tiene ese límite.
        # Sin filtro de "status": también deben indexarse productos pausados (sin stock)
        # o en revisión, para poder consultar precio de competencia antes de reponerlos.
        url = f"{BASE_URL}/users/{seller_id}/items/search"
        params = {"limit": PAGE_SIZE, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        try:
            resp = self._get(url, params=params)
            return resp.get("results", []), resp.get("scroll_id")
        except MLApiError as e:
            log.error("Error obteniendo página de items (scroll_id=%s): %s", scroll_id, e)
            return [], None

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
        try:
            resp = self._session.get(url, params=params, timeout=15)
        except requests.exceptions.RequestException as e:
            raise MLApiError(0, f"Error de red: {e}", url)
        if not resp.ok:
            try:
                msg = resp.json().get("message", resp.text[:200])
            except Exception:
                msg = resp.text[:200]
            raise MLApiError(resp.status_code, msg, url)
        return resp.json()

    def _get_lista(self, url: str, params: Optional[dict] = None) -> list:
        try:
            resp = self._session.get(url, params=params, timeout=15)
        except requests.exceptions.RequestException as e:
            raise MLApiError(0, f"Error de red: {e}", url)
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


ESTADO_LABELS = {
    "active": "Activa",
    "paused": "Pausada",
    "closed": "Cerrada",
    "under_review": "En revisión",
    "inactive": "Inactiva",
    "payment_required": "Pago requerido",
}

def _formatear_estado_publicacion(status: Optional[str], sub_status) -> str:
    etiqueta = ESTADO_LABELS.get(status, status or "Desconocido")
    if isinstance(sub_status, list) and "out_of_stock" in sub_status:
        etiqueta += " (sin stock)"
    return etiqueta

def obtener_contexto_buy_box(resolver: MLSkuResolver, sku_bsale: str) -> Optional[dict]:
    item_id = resolver.resolver_sku(sku_bsale)
    if not item_id:
        return None

    try:
        item_data = resolver._get(
            f"https://api.mercadolibre.com/items/{item_id}",
            params={"attributes": "id,catalog_product_id,price,seller_id,status,sub_status"},
        )
    except Exception:
        return None

    catalog_product_id = item_data.get("catalog_product_id")
    precio_propio = item_data.get("price")
    estado_publicacion = _formatear_estado_publicacion(item_data.get("status"), item_data.get("sub_status"))

    if not catalog_product_id:
        return {
            "item_id": item_id,
            "catalog_product_id": None,
            "en_catalogo": False,
            "precio_propio": precio_propio,
            "estado_publicacion": estado_publicacion,
        }

    # Se escanea en vivo la lista de competidores activos del catálogo y de ahí se
    # sacan precio Y vendedor del más barato EN LA MISMA consulta. Antes se usaba el
    # campo "buy_box_winner" de Mercado Libre para el precio (que puede venir con
    # caché vieja de su lado) y por separado se resolvía el vendedor - si el precio
    # cacheado ya no correspondía al vendedor más barato actual, mostraba un precio
    # y un vendedor de momentos distintos, que no calzaban entre sí.
    mejor_precio = None
    mejor_item_id = None
    mejor_seller_id = None
    mejor_es_internacional = False

    try:
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
                    if mejor_precio is None or comp_price_float < mejor_precio:
                        mejor_precio = comp_price_float
                        mejor_item_id = comp_item_id
                        mejor_seller_id = comp.get("seller_id")
                        # Ventas internacionales (Cross Border Trade): Mercado Libre a veces
                        # muestra en la página pública un nombre de tienda distinto al nickname
                        # real de la cuenta para este tipo de publicación.
                        mejor_es_internacional = "cbt_item" in (comp.get("tags") or [])

            offset += 50
            if len(lista_competidores) < 50:
                break

    except Exception:
        pass

    ganando = (
        precio_propio is not None
        and mejor_precio is not None
        and float(precio_propio) <= mejor_precio
    )

    # Nota: /items/{id} de un item ajeno devuelve 403 (access_denied) con un token de
    # app (client_credentials). Por eso el seller_id del rival sale de la misma
    # consulta al catálogo público de arriba, nunca del item directamente.
    # Se resuelve el nombre del rival aunque estemos ganando (util para saber a quien
    # se le esta ganando y si es una oferta internacional a tener en el radar).
    rival_nombre = None
    if mejor_seller_id:
        try:
            seller_data = resolver._get(
                f"https://api.mercadolibre.com/users/{mejor_seller_id}",
                params={"attributes": "nickname"},
            )
            rival_nombre = seller_data.get("nickname")
        except Exception:
            rival_nombre = None

    return {
        "item_id": item_id,
        "catalog_product_id": catalog_product_id,
        "en_catalogo": True,
        "precio_propio": precio_propio,
        "precio_buy_box": mejor_precio,
        "ganando_buy_box": ganando,
        "buy_box_winner_id": mejor_item_id,
        "rival_nombre": rival_nombre,
        "rival_internacional": mejor_es_internacional,
        "estado_publicacion": estado_publicacion,
    }