import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["ML_ACCESS_TOKEN_TEST"]

# --- PRUEBA 1: Validar si la credencial realmente funciona ---
url_me = "https://api.mercadolibre.com/users/me"
headers_me = {"Authorization": f"Bearer {TOKEN}"}

print("1️⃣ Probando credencial en zona segura...")
r_me = requests.get(url_me, headers=headers_me)

if r_me.status_code == 200:
    print("✅ ¡ÉXITO! El token está impecable y tienes acceso a la cuenta.")
else:
    print(f"❌ Error en el token: {r_me.text}")

# --- PRUEBA 2: Búsqueda con camuflaje de navegador ---
url_search = "https://api.mercadolibre.com/sites/MLC/search?q=0043917029092"
headers_search = {
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" # El disfraz de Chrome
}

print("\n2️⃣ Intentando buscar el producto disfrazados de humano...")
r_search = requests.get(url_search, headers=headers_search)

if r_search.status_code == 200:
    print("✅ ¡ÉXITO! Era el filtro anti-bots. Con el disfraz de Chrome, pudimos pasar.")
else:
    print(f"❌ Sigue bloqueando la búsqueda: {r_search.text}")