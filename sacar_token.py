import os
import requests
from dotenv import load_dotenv

load_dotenv()

# 1. LLENA ESTOS DATOS (APP_ID y SECRET vienen del .env; CODE cambia cada vez)
APP_ID = os.environ["ML_APP_ID"]
SECRET = os.environ["ML_SECRET_KEY"]
CODE = "pega_aqui_el_codigo_de_un_solo_uso"

# 2. EL CANJE
url = "https://api.mercadolibre.com/oauth/token"
data = {
    "grant_type": "authorization_code",
    "client_id": APP_ID,
    "client_secret": SECRET,
    "code": CODE,
    "redirect_uri": "https://example.com"
}

print("Canjeando ticket en Mercado Libre...")
respuesta = requests.post(url, data=data)

try:
    print("\n✅ ¡ÉXITO! Aquí tienes tu token oficial:")
    print(respuesta.json()["access_token"])
except KeyError:
    print("\n❌ Hubo un error:", respuesta.json())