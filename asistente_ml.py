import requests

print("=== ASISTENTE SEGURO DE MERCADO LIBRE ===")
app_id = input("1. Pega tu App ID: ").strip()
secret = input("2. Pega tu Secret Key: ").strip()

link = f"https://auth.mercadolibre.cl/authorization?response_type=code&client_id={app_id}&redirect_uri=https://www.google.cl"

print("\n3. Presiona la tecla 'Ctrl' y haz clic en este link azul para abrir el navegador:")
print(link)
print("\n4. Mercado Libre te pedirá autorizar. Luego te enviará a Google.")
print("   Copia el link COMPLETO de arriba (el que dice google.cl/?code=...) y pégalo aquí abajo:")

url_google = input("\n-> Pega el link de Google aquí: ").strip()

try:
    # El asistente extrae el código de forma automática y segura
    codigo_tg = url_google.split("code=")[1].split("&")[0]
    
    url_token = "https://api.mercadolibre.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": secret,
        "code": codigo_tg,
        "redirect_uri": "https://www.google.cl"
    }
    
    print("\n⏳ Conectando con Mercado Libre...")
    resp = requests.post(url_token, data=data).json()
    
    if "access_token" in resp:
        print("\n✅ ¡ÉXITO! Aquí tienes tu token oficial de Mercado Libre. Cópialo:")
        print("-" * 50)
        print(resp["access_token"])
        print("-" * 50)
        print("Pégalo en la variable ML_TOKEN de tu repricer.py ¡y listo!")
    else:
        print("\n❌ Hubo un error de Mercado Libre:", resp)
        
except Exception as e:
    print("\n❌ Error al leer el link. Asegúrate de haber pegado el link completo de Google.")