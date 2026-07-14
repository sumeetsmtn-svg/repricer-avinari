import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

BSALE_TOKEN = os.environ["BSALE_TOKEN"]
HEADERS = {
    "access_token": BSALE_TOKEN,
    "Accept": "application/json"
}

print("=== RADIOGRAFÍA 1: LISTAS DE PRECIOS ===")
url_listas = "https://api.bsale.cl/v1/price_lists.json"
resp_listas = requests.get(url_listas, headers=HEADERS)
if "items" in resp_listas.json():
    for lista in resp_listas.json()["items"]:
        print(f"ID: {lista['id']} -> Nombre: {lista['name']}")

print("\n=== RADIOGRAFÍA 2: TIPO DE PRODUCTO ===")
url_producto = "https://api.bsale.cl/v1/products/4.json"
resp_producto = requests.get(url_producto, headers=HEADERS)
print(json.dumps(resp_producto.json(), indent=4))