import streamlit as st
import pandas as pd
import time
import requests
import urllib.parse
import re
import os
import concurrent.futures
import bcrypt
from dataclasses import dataclass

from repricer import (
    ML_APP_ID, ML_SECRET_KEY, BSALE_TOKEN, DIFERENCIAL_PRECIO, PAUSA_ML
)
from ml_sku_resolver import MLSkuResolver, obtener_contexto_buy_box

APP_USERNAME = os.environ["APP_USERNAME"]
APP_PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"]

def verificar_login():
    if st.session_state.get("autenticado"):
        return True

    st.set_page_config(page_title="Repricer by Avinari.cl", page_icon="🏢", layout="centered")
    st.markdown("<h2 style='text-align: center;'>🏢 Repricer by Avinari.cl</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888;'>Acceso restringido</p>", unsafe_allow_html=True)

    with st.form("login_form"):
        usuario = st.text_input("Usuario")
        clave = st.text_input("Contraseña", type="password")
        enviado = st.form_submit_button("Ingresar", type="primary", width="stretch")

    if enviado:
        clave_valida = bcrypt.checkpw(clave.encode("utf-8"), APP_PASSWORD_HASH.encode("utf-8"))
        if usuario == APP_USERNAME and clave_valida:
            st.session_state.autenticado = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    return False

if not verificar_login():
    st.stop()

if "resultados_escaneo" not in st.session_state:
    st.session_state.resultados_escaneo = None
if "token_ml" not in st.session_state:
    st.session_state.token_ml = None
if "stats_actuales" not in st.session_state:
    st.session_state.stats_actuales = {"analizados": 0, "subidas": 0, "bajadas": 0, "protegidos": 0}

TOKEN_BSALE = BSALE_TOKEN

# Sesión HTTP reutilizable: evita rehacer el handshake TLS en cada llamada a Bsale
BSALE_SESSION = requests.Session()
BSALE_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20))

@dataclass
class AppProducto:
    variant_id: int   
    sku: str
    nombre: str
    precio_actual: float
    costo: float
    stock: int
    precio_minimo: float

def calcular_margen_display(precio_venta: float, costo_base: float) -> str:
    if not precio_venta or precio_venta <= 0: return "-"
    meta_bruta = (costo_base * 1.07) * 1.19
    if precio_venta < 9980: envio = 790
    elif precio_venta < 19990: envio = 1000
    else: envio = 3100
    comision = precio_venta * 0.14
    ganancia_neta = precio_venta - comision - envio - meta_bruta
    return f"{(ganancia_neta / precio_venta) * 100:.1f}% (${int(ganancia_neta):,})"

@st.cache_data(ttl=3600)
def obtener_id_lista_ml():
    headers = {"access_token": TOKEN_BSALE, "Content-Type": "application/json"}
    try:
        r = BSALE_SESSION.get("https://api.bsale.io/v1/price_lists.json", headers=headers, timeout=15)
        if r.ok:
            listas = r.json().get("items", [])
            for lista in listas:
                if "07 MERCADO LIBRE" in lista.get("name", "").upper():
                    return int(lista["id"])
    except:
        pass
    return 7 

def bsale_actualizar_precio_lista_jit(variant_id: int, nuevo_precio_bruto: float, true_list_id: int) -> str:
    if variant_id == 0: return "Variante inválida"
    headers = {"access_token": TOKEN_BSALE, "Content-Type": "application/json"}
    base_url = "https://api.bsale.io/v1"
    
    try:
        url_search = f"{base_url}/price_lists/{true_list_id}/details.json"
        resp_search = BSALE_SESSION.get(url_search, headers=headers, params={"variantid": variant_id}, timeout=15)
        resp_search.raise_for_status()

        items = resp_search.json().get("items", [])
        if not items: return f"Variante no encontrada en la Lista (ID {true_list_id})"

        detail_id = int(items[0]["id"])
        precio_neto_api = float(nuevo_precio_bruto) / 1.19

        url_put = f"{base_url}/price_lists/{true_list_id}/details/{detail_id}.json"
        payload = {"id": detail_id, "variantValue": precio_neto_api}

        resp_put = BSALE_SESSION.put(url_put, headers=headers, json=payload, timeout=15)
        if not resp_put.ok:
            return f"Error HTTP {resp_put.status_code} | Bsale: {resp_put.text}"
            
        return "OK"
    except Exception as e:
        return f"Error de conexión: {str(e)}"

def obtener_meta_minima(costo: float) -> float:
    meta_bruta = (costo * 1.07) * 1.19
    p_min = (meta_bruta + 790) / 0.86
    if p_min >= 9980: p_min = (meta_bruta + 1000) / 0.86
    if p_min >= 19990: p_min = (meta_bruta + 3100) / 0.86
    return p_min

def _procesar_stock_item(s: dict, true_list_id: int):
    headers = {"access_token": TOKEN_BSALE, "Content-Type": "application/json"}
    base_url = "https://api.bsale.io/v1"

    stock_qty = float(s.get("quantity") or 0)
    if stock_qty <= 0: return None
    href = (s.get("variant") or {}).get("href", "")
    if not href: return None
    vid = int(href.rstrip("/").replace(".json", "").split("/")[-1])

    try:
        data = BSALE_SESSION.get(f"{base_url}/variants/{vid}.json", headers=headers, timeout=15).json()
        sku = (data.get("code") or "").strip()
        if not sku: return None

        nombre = (data.get("description") or "").strip()
        if nombre in [".", ""]:
            p_data = BSALE_SESSION.get(f"{base_url}/products/{data['product']['id']}.json", headers=headers, timeout=15).json()
            nombre = (p_data.get("name") or "").strip()

        cost_data = BSALE_SESSION.get(f"{base_url}/variants/{vid}/costs.json", headers=headers, timeout=15).json()
        costo = float(cost_data.get("averageCost") or cost_data.get("totalCost") or 1.0) if cost_data else 1.0

        pl_data = BSALE_SESSION.get(f"{base_url}/price_lists/{true_list_id}/details.json", headers=headers, params={"variantid": vid}, timeout=15).json()
        precio_actual_bsale = 0.0
        if pl_data.get("items"):
            det = pl_data["items"][0]
            precio_actual_bsale = float(det.get("variantValueWithTaxes") or det.get("variantValue") or 0)

        p_min = obtener_meta_minima(costo)
        return AppProducto(vid, sku, nombre, precio_actual_bsale, costo, int(stock_qty), p_min)
    except Exception:
        return None

def bsale_cargar_productos_directo(limite_escaneo: int, true_list_id: int) -> list[AppProducto]:
    headers = {"access_token": TOKEN_BSALE, "Content-Type": "application/json"}
    base_url = "https://api.bsale.io/v1"
    productos = []
    offset, limite_pagina = 0, 50

    while offset < limite_escaneo:
        limit_solicitado = min(limite_pagina, limite_escaneo - offset)
        try:
            r = BSALE_SESSION.get(f"{base_url}/stocks.json", headers=headers, params={"state": 1, "limit": limit_solicitado, "offset": offset}, timeout=20)
            items = r.json().get("items", [])
            if not items: break

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                resultados_pagina = executor.map(lambda s: _procesar_stock_item(s, true_list_id), items)
                productos.extend(p for p in resultados_pagina if p is not None)
        except Exception:
            break
        offset += limite_pagina
    return productos

def _cargar_un_sku(sku: str, true_list_id: int):
    headers = {"access_token": TOKEN_BSALE, "Content-Type": "application/json"}
    base_url = "https://api.bsale.io/v1"

    try:
        r_var = BSALE_SESSION.get(f"{base_url}/variants.json", headers=headers, params={"code": sku}, timeout=15)
        if not r_var.ok or not r_var.json().get("items"): return None
        vid = r_var.json()["items"][0]["id"]

        data = BSALE_SESSION.get(f"{base_url}/variants/{vid}.json", headers=headers, timeout=15).json()
        nombre = (data.get("description") or "").strip()
        if nombre in [".", ""]:
            p_data = BSALE_SESSION.get(f"{base_url}/products/{data['product']['id']}.json", headers=headers, timeout=15).json()
            nombre = (p_data.get("name") or "").strip()

        r_stock = BSALE_SESSION.get(f"{base_url}/stocks.json", headers=headers, params={"variantid": vid}, timeout=15)
        stock_qty = sum(float(s.get("quantity") or 0) for s in r_stock.json().get("items", []))

        cost_data = BSALE_SESSION.get(f"{base_url}/variants/{vid}/costs.json", headers=headers, timeout=15).json()
        costo = float(cost_data.get("averageCost") or cost_data.get("totalCost") or 1.0) if cost_data else 1.0

        pl_data = BSALE_SESSION.get(f"{base_url}/price_lists/{true_list_id}/details.json", headers=headers, params={"variantid": vid}, timeout=15).json()
        precio_actual_bsale = 0.0
        if pl_data.get("items"):
            det = pl_data["items"][0]
            precio_actual_bsale = float(det.get("variantValueWithTaxes") or det.get("variantValue") or 0)

        p_min = obtener_meta_minima(costo)
        return AppProducto(vid, sku, nombre, precio_actual_bsale, costo, int(stock_qty), p_min)
    except Exception:
        return None

def bsale_cargar_productos_por_sku(lista_skus: list, true_list_id: int) -> list[AppProducto]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        resultados = executor.map(lambda sku: _cargar_un_sku(sku, true_list_id), lista_skus)
        return [p for p in resultados if p is not None]

def ejecutar_analisis_mercado(productos, progress_bar, status_text):
    stats = {"analizados": 0, "subidas": 0, "bajadas": 0, "protegidos": 0}
    resultados = []
    resolver = MLSkuResolver(access_token=st.session_state.token_ml)
    total = len(productos)
    
    for i, p in enumerate(productos):
        status_text.text(f"Procesando SKU: {p.sku} - {p.nombre}")
        ctx = obtener_contexto_buy_box(resolver, p.sku)
        time.sleep(PAUSA_ML)
        
        accion, motivo, target = "⚪ Ignorado", "Sin vinculación", None
        precio_rival_display = "N/A"
        rival_nombre_display = "-"
        
        precio_bsale_display = f"${p.precio_actual:,.0f}"
        
        precio_ml = p.precio_actual
        if ctx and ctx.get("precio_propio"):
            precio_ml = ctx.get("precio_propio")
            
        precio_ml_display = f"${precio_ml:,.0f}"
        nuevo_precio_display = "-"
        
        nombre_codificado = urllib.parse.quote(p.nombre)
        url_dinamica = f"https://listado.mercadolibre.cl/{nombre_codificado}"
        
        if ctx and ctx.get("en_catalogo"):
            ganando = ctx.get("ganando_buy_box")
            precio_rival = ctx.get("precio_buy_box")
            
            if not precio_rival:
                motivo = "Sin competidores"
            elif ganando:
                precio_rival_display = f"${precio_rival:,.0f}" if precio_rival else "-"
                accion, motivo = "🟢 Liderando", "Eres el más barato"
            else:
                precio_rival_display = f"${precio_rival:,.0f}"
                rival_nombre_display = ctx.get("rival_nombre") or "Desconocido"
                target = round(precio_rival - DIFERENCIAL_PRECIO, 2)

                # REGLA FUEGO AMIGO
                if abs(precio_ml - precio_rival) <= 10:
                    accion, motivo = "⚪ Mantener", "Empate / Eres el más barato"
                    target = None
                    nuevo_precio_display = "-"
                # REGLA SUBIR PRECIO
                elif precio_ml < target:
                    accion, motivo, nuevo_precio_display = "🟢 SUBIR PRECIO", "Optimización de Margen", f"${target:,.0f}"
                    stats["subidas"] += 1
                elif precio_ml == target:
                    accion, motivo = "⚪ Mantener", "Precio Óptimo"
                else:
                    if target < p.precio_minimo:
                        accion, motivo, target = "🔴 Bloqueado (Piso)", f"Límite inferior (${p.precio_minimo:,.0f})", None
                        stats["protegidos"] += 1
                    else:
                        accion, motivo, nuevo_precio_display = "🔴 BAJAR PRECIO", "Recuperación de Posición", f"${target:,.0f}"
                        stats["bajadas"] += 1

        margen_actual_display = calcular_margen_display(precio_ml if ctx and ctx.get("en_catalogo") else p.precio_actual, p.costo)
        margen_nuevo_display = calcular_margen_display(target, p.costo) if target else "-"

        resultados.append({
            "Aprobar": False, 
            "SKU": p.sku, 
            "Producto": p.nombre, 
            "Enlace ML": url_dinamica,
            "Stock": p.stock, 
            "Precio Bsale": precio_bsale_display, 
            "Precio ML": precio_ml_display, 
            "Margen Actual": margen_actual_display, 
            "Rival Más Barato": precio_rival_display,
            "Vendedor Rival": rival_nombre_display,
            "Acción": accion,
            "Precio Sugerido": nuevo_precio_display, 
            "Precio Final": target,                  # Editable
            "Margen Nuevo": margen_nuevo_display,    # Se recalcula al editar
            "Detalle": motivo, 
            "_target_original": target,              
            "_variant_id": p.variant_id,
            "_costo": p.costo                        # Oculto, usado para recalcular margen
        })
        
        stats["analizados"] += 1
        m_analizados.metric("Inventario Analizado", f"{stats['analizados']} / {total}")
        m_oportunidades.metric("Oportunidades de Alza", stats["subidas"])
        m_ataques.metric("Alertas de Competencia", stats["bajadas"])
        m_protegidos.metric("Protección de Margen Mínimo", stats["protegidos"])
        progress_bar.progress((i + 1) / total)

    status_text.empty()
    st.success("Análisis de mercado finalizado. Esperando revisión.")
    return resultados, stats

st.set_page_config(page_title="Repricer by Avinari.cl", page_icon="🏢", layout="wide")

col_logo, col_titulo = st.columns([1, 5])
with col_logo:
    try: st.image("logo.png", width=120)
    except: st.markdown("<h2 style='color: #7d22b3; margin-top: 20px;'>🏢 AVINARI.CL</h2>", unsafe_allow_html=True)

with col_titulo:
    st.markdown("<h1 style='margin-bottom: 0px; padding-top: 10px;'>Repricer by Avinari.cl</h1>", unsafe_allow_html=True)

st.divider()

TRUE_LIST_ID = obtener_id_lista_ml()

with st.sidebar:
    try:
        col_sb1, col_sb2, col_sb3 = st.columns([1, 4, 1])
        with col_sb2: st.image("logo.png", width="stretch")
    except: pass

    if st.button("🔒 Cerrar sesión", width="stretch"):
        st.session_state.autenticado = False
        st.rerun()

    st.markdown(f"<p style='text-align: center; color: #888; font-size: 0.8rem; margin-top: -10px; margin-bottom: 20px; font-weight: 500;'>Control Center | v19.1 (Dynamic Margin)<br>Target: Lista Bsale ID {TRUE_LIST_ID}</p>", unsafe_allow_html=True)
    st.divider()
    
    st.header("Configuración del Sistema")
    limite = st.selectbox("Volumen de escaneo", options=[10, 50, 100, 500, 1000], index=0)
    
    st.divider()
    st.header("Filtros de Acción")
    opciones_accion = ["🟢 SUBIR PRECIO", "🔴 BAJAR PRECIO", "🔴 Bloqueado (Piso)", "🟢 Liderando", "⚪ Mantener", "⚪ Ignorado"]
    filtros_accion = st.multiselect(
        "Estados activos:",
        options=opciones_accion,
        default=opciones_accion
    )
    
    st.markdown("<br><br>" * 2, unsafe_allow_html=True)
    st.markdown(
        """
        <div style="background-color: #f8f9fa; padding: 15px; border-left: 4px solid #7d22b3; border-radius: 4px;">
            <p style="margin: 0; font-size: 0.8rem; color: #666; font-weight: bold; text-transform: uppercase;">Core Developer</p>
            <p style="margin: 5px 0 0 0; font-size: 1.05rem; color: #222; font-weight: bold;">Sumeet Samtani</p>
            <p style="margin: 0; font-size: 0.75rem; color: #888;">Lead Systems Architect</p>
        </div>
        """, unsafe_allow_html=True
    )

col1, col2, col3, col4 = st.columns(4)
m_analizados = col1.empty()
m_oportunidades = col2.empty()
m_ataques = col3.empty()
m_protegidos = col4.empty()

m_analizados.metric("Inventario Analizado", st.session_state.stats_actuales["analizados"])
m_oportunidades.metric("Oportunidades de Alza", st.session_state.stats_actuales["subidas"])
m_ataques.metric("Alertas de Competencia", st.session_state.stats_actuales["bajadas"])
m_protegidos.metric("Protección de Margen Mínimo", st.session_state.stats_actuales["protegidos"])

st.divider()

tab1, tab2 = st.tabs(["Ingreso por Lote de SKUs", "Barrido General de Catálogo"])

with tab1:
    skus_input = st.text_area("SKUs (Separados por espacio o salto de línea)", height=150)
    if st.button("1. Ejecutar Análisis de SKUs", type="primary", width="stretch"):
        if not skus_input.strip(): st.warning("Ingrese al menos un SKU para continuar.")
        else:
            lista_cruda = re.split(r'[,\n\t\s]+', skus_input)
            lista_limpia = [s.strip() for s in lista_cruda if s.strip()]
            
            with st.spinner("Validando credenciales con API Mercado Libre..."):
                r = requests.post("https://api.mercadolibre.com/oauth/token", data={"grant_type": "client_credentials", "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY}, timeout=15)
                st.session_state.token_ml = r.json().get("access_token")
            with st.spinner(f"Consultando SKUs en Bsale (Lista ID: {TRUE_LIST_ID})..."):
                productos = bsale_cargar_productos_por_sku(lista_limpia, TRUE_LIST_ID)
            if not productos: st.error("No se encontraron registros válidos para los SKUs ingresados.")
            else:
                prog = st.progress(0)
                stat = st.empty()
                res, sts = ejecutar_analisis_mercado(productos, prog, stat)
                st.session_state.resultados_escaneo = res
                st.session_state.stats_actuales = sts

with tab2:
    if st.button("1. Iniciar Barrido General", type="primary", width="stretch"):
        with st.spinner("Validando credenciales con API Mercado Libre..."):
            r = requests.post("https://api.mercadolibre.com/oauth/token", data={"grant_type": "client_credentials", "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY}, timeout=15)
            st.session_state.token_ml = r.json().get("access_token")
        with st.spinner(f"Extrayendo productos desde Bsale (Lista ID: {TRUE_LIST_ID})..."):
            productos = bsale_cargar_productos_directo(limite, TRUE_LIST_ID)
        if not productos: st.warning("No se detectaron productos en el barrido.")
        else:
            prog = st.progress(0)
            stat = st.empty()
            res, sts = ejecutar_analisis_mercado(productos, prog, stat)
            st.session_state.resultados_escaneo = res
            st.session_state.stats_actuales = sts

if st.session_state.resultados_escaneo is not None:
    st.divider()
    st.markdown("### Panel de Aprobación")
    
    df = pd.DataFrame(st.session_state.resultados_escaneo)
    df_mostrar = df[df["Acción"].isin(filtros_accion)] if filtros_accion else df

    if len(df_mostrar) < len(df):
        st.caption(f"Mostrando {len(df_mostrar)} de {len(df)} SKUs analizados (hay {len(df) - len(df_mostrar)} ocultos por el filtro 'Estados activos' de la barra lateral).")

    edited_df = st.data_editor(
        df_mostrar,
        width="stretch",
        hide_index=True,
        disabled=["SKU", "Producto", "Enlace ML", "Stock", "Precio Bsale", "Precio ML", "Margen Actual", "Rival Más Barato", "Vendedor Rival", "Acción", "Precio Sugerido", "Margen Nuevo", "Detalle"],
        column_config={
            "Aprobar": st.column_config.CheckboxColumn("Aprobar", default=False),
            "Enlace ML": st.column_config.LinkColumn("Enlace ML", display_text="Revisar Publicación"),
            "Precio Final": st.column_config.NumberColumn(
                "Precio Final ✏️",
                help="Escribe tu precio aquí. Presiona Enter para recalcular el Margen al instante.",
                format="$%d",
                min_value=0,
                step=10,
            ),
            "_target_original": None,
            "_variant_id": None,
            "_costo": None # Se oculta la columna costo interno
        }
    )

    # --- MOTOR DE RECALCULO EN VIVO ---
    cambios_para_rerun = False
    for idx, row in edited_df.iterrows():
        # Guardar cambios del checkbox de Aprobar
        if row["Aprobar"] != st.session_state.resultados_escaneo[idx]["Aprobar"]:
            st.session_state.resultados_escaneo[idx]["Aprobar"] = row["Aprobar"]
            
        # Detectar edición de Precio Final
        old_precio = st.session_state.resultados_escaneo[idx]["Precio Final"]
        new_precio = row["Precio Final"]
        
        if pd.notna(new_precio) and new_precio != old_precio:
            st.session_state.resultados_escaneo[idx]["Precio Final"] = new_precio
            # Recalculamos el margen usando la fórmula oficial
            nuevo_margen = calcular_margen_display(new_precio, row["_costo"])
            st.session_state.resultados_escaneo[idx]["Margen Nuevo"] = nuevo_margen
            cambios_para_rerun = True

    if cambios_para_rerun:
        st.rerun()
    # ----------------------------------

    if st.button("2. Aplicar Cambios en Bsale", type="primary", width="stretch"):
        productos_aprobados = edited_df[edited_df["Aprobar"] == True]
        
        if productos_aprobados.empty: 
            st.warning("Seleccione al menos un registro en la columna 'Aprobar' para continuar.")
        else:
            with st.spinner("Sincronizando precios con Bsale..."):
                exitos_bsale = 0
                errores_bsale = []
                log_cambios_exitosos = [] 
                
                for index, row in productos_aprobados.iterrows():
                    precio_final_inyectar = row["Precio Final"]
                    
                    if pd.notna(precio_final_inyectar) and pd.notna(row["_variant_id"]):
                        estrategia = row["Acción"]
                        if pd.notna(row["_target_original"]) and float(precio_final_inyectar) != float(row["_target_original"]):
                            estrategia = "Modificación Manual ✍️"

                        resultado = bsale_actualizar_precio_lista_jit(int(row["_variant_id"]), float(precio_final_inyectar), TRUE_LIST_ID)
                        
                        if resultado == "OK": 
                            exitos_bsale += 1
                            log_cambios_exitosos.append({
                                "SKU": row["SKU"],
                                "Producto": row["Producto"],
                                "Precio Anterior": row["Precio Bsale"],
                                "Precio Nuevo Inyectado": f"${precio_final_inyectar:,.0f}",
                                "Estrategia Aplicada": estrategia
                            })
                        else: 
                            errores_bsale.append(f"SKU {row['SKU']}: {resultado}")
                        time.sleep(0.3) 
                
                if errores_bsale:
                    st.error("Se encontraron excepciones durante la sincronización:")
                    for err in errores_bsale: st.code(err)
                else:
                    st.success(f"Sincronización exitosa. Se han actualizado {exitos_bsale} precios en Bsale (Lista ID: {TRUE_LIST_ID}).")
                    st.balloons()
                    
                    if log_cambios_exitosos:
                        st.divider()
                        st.markdown("### 📊 Log de Publicidad (SKUs Ganadores)")
                        st.info("Utilice este reporte para orientar las campañas de Mercado Ads a los SKUs donde acaba de ganar la Buy Box.")
                        
                        df_log = pd.DataFrame(log_cambios_exitosos)
                        st.dataframe(df_log, width="stretch", hide_index=True)
                        
                        csv_log = df_log.to_csv(index=False).encode('utf-8')
                        st.download_button(
                            label="📥 Exportar Log a Excel (CSV)",
                            data=csv_log,
                            file_name="skus_actualizados_ads.csv",
                            mime="text/csv",
                        )
                        
                st.session_state.resultados_escaneo = None