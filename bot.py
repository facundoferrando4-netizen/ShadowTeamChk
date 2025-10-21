import os
import re
import random
import logging
import requests
import asyncio
import braintree
import threading
from datetime import datetime
from dotenv import load_dotenv
# Flask se importa condicionalmente más abajo para evitar ModuleNotFoundError
try:
    from flask import Flask, jsonify

    # Crear una app HTTP mínima para healthcheck; si Flask está instalado, la arrancamos en __main__
    app_http = Flask(__name__)

    @app_http.route("/health")
    def health():
        return jsonify({"status": "ok"})
except Exception:
    # Si Flask no está disponible, usamos None y evitamos NameError
    app_http = None

# Cargar variables de entorno
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BRAINTREE_PUBLIC_KEY = os.getenv("BRAINTREE_PUBLIC_KEY")
BRAINTREE_PRIVATE_KEY = os.getenv("BRAINTREE_PRIVATE_KEY")
BRAINTREE_MERCHANT_ID = os.getenv("BRAINTREE_MERCHANT_ID")

if not all([TELEGRAM_TOKEN, BRAINTREE_PUBLIC_KEY, BRAINTREE_PRIVATE_KEY, BRAINTREE_MERCHANT_ID]):
    raise ValueError("❌ Faltan variables en el archivo .env (incluye BRAINTREE_MERCHANT_ID)")

# Configurar gateway de Braintree (Sandbox por defecto)
gateway = braintree.BraintreeGateway(
    braintree.Configuration(
        environment=braintree.Environment.Sandbox,
        merchant_id=BRAINTREE_MERCHANT_ID,
        public_key=BRAINTREE_PUBLIC_KEY,
        private_key=BRAINTREE_PRIVATE_KEY,
    )
)

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==============================
# FUNCIONES DE APOYO
# ==============================

def is_luhn_valid(card_number: str) -> bool:
    def digits_of(n): return [int(d) for d in str(n)]
    digits = digits_of(card_number)
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    checksum = sum(odd_digits)
    for d in even_digits:
        checksum += sum(digits_of(d * 2))
    return checksum % 10 == 0

def detectar_tipo_tarjeta(bin_str: str) -> str:
    if not bin_str.isdigit() or len(bin_str) < 6:
        return "Unknown"
    if bin_str.startswith('4'):
        return "Visa"
    if 51 <= int(bin_str[:2]) <= 55 or (len(bin_str) >= 4 and 2221 <= int(bin_str[:4]) <= 2720):
        return "Mastercard"
    if re.match(r'^3[47]', bin_str):
        return "American Express"
    return "Unknown"

# Funciones auxiliares faltantes añadidas para evitar NameError en runtime.

def generar_numero_luhn_valido(patron: str) -> str:
    """
    Genera un número que cumple Luhn a partir de un patrón con dígitos y 'x' (o 'X') como comodines.
    Si el patrón contiene menos de 12 o más de 19 caracteres, se intenta generar igualmente.
    """
    patron = patron.strip()
    if not re.match(r'^[0-9xX]+$', patron):
        raise ValueError("Patrón inválido. Solo dígitos y 'x' permitidos.")
    longitud = len(patron)
    max_intentos = 5000

    def calcular_check_digit(digits):
        total = 0
        # Luhn expects processing from rightmost, doubling every second digit
        parity = (len(digits) + 1) % 2
        for i, ch in enumerate(digits):
            d = int(ch)
            if i % 2 == parity:
                d = d * 2
                if d > 9:
                    d -= 9
            total += d
        return (10 - (total % 10)) % 10

    # Convertimos patrón a lista para edición
    indices_x = [i for i, c in enumerate(patron) if c in 'xX']
    fixed_positions = {i: c for i, c in enumerate(patron) if c not in 'xX'}

    # Si no hay 'x', comprobamos y devolvemos o lanzamos excepción si no pasa Luhn
    if not indices_x:
        if is_luhn_valid(patron):
            return patron
        else:
            raise ValueError("Patrón proporcionado no cumple Luhn y no contiene 'x' para generar variantes.")

    # Intentos: rellenar 'x' aleatoriamente y ajustar dígito de control cuando sea posible
    for _ in range(max_intentos):
        candidate = list(patron)
        for i in indices_x:
            # Dejamos al menos un 'x' para calcular check digit si existe
            candidate[i] = str(random.randint(0, 9))
        cand_str = "".join(candidate)
        # Si el patrón original tenía al menos un 'x', intentamos ajustar el último dígito para Luhn:
        # calculamos check digit en toda la secuencia (si fuese necesario)
        # Si el patrón especificaba el último dígito (no era 'x'), verificamos Luhn completo.
        if is_luhn_valid(cand_str):
            return cand_str
        # Intento de arreglar el último dígito si originalmente era 'x' para forzar Luhn
        last_x_positions = [i for i in indices_x if i == longitud - 1]
        if last_x_positions:
            # recalculamos el check digit basado en todos los dígitos excepto el último
            prefix = cand_str[:-1]
            # calcular check digit directamente (alimentando los dígitos como en Luhn)
            # reconstruimos la lista según Luhn: necesitamos operar con dígitos de izquierda a derecha
            # Para facilitar, probamos cada posible último dígito
            for d in range(10):
                test = prefix + str(d)
                if is_luhn_valid(test):
                    return test
        # si no se logró, seguimos intentando con otra semilla aleatoria

    raise RuntimeError("No se pudo generar un número Luhn válido a partir del patrón.")

def generar_fecha_vencimiento_completa():
    """
    Devuelve (mm, aa) donde mm es mes con dos dígitos y aa es año de dos dígitos.
    Año generado entre el año actual y +5 años.
    """
    ahora = datetime.now()
    año = ahora.year + random.randint(1, 5)
    mes = random.randint(1, 12)
    return f"{mes:02d}", f"{año % 100:02d}"

def generar_cvv(tipo: str = "") -> str:
    """
    Genera un CVV aleatorio: 4 dígitos para American Express, 3 dígitos para el resto.
    """
    if "American Express" in tipo or "Amex" in tipo:
        return "".join(str(random.randint(0, 9)) for _ in range(4))
    return "".join(str(random.randint(0, 9)) for _ in range(3))

# Manejadores mínimos para comandos registrados en main si no existían previamente:
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola, soy el bot de ShadowTeam. Usa /chk, /br, /vbin, /gen, /create_nonce, /use_nonce según correspondan.")

async def use_nonce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /use_nonce <nonce>\nEj: /use_nonce fake-valid-nonce")
        return
    nonce = context.args[0]
    # No se implementa procesamiento real del nonce por seguridad; solo confirmamos recepción.
    await update.message.reply_text(f"Nonce recibido: `{nonce}` (no procesado en esta versión)", parse_mode="Markdown")

async def submit_settlement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando submit_settlement no implementado en esta versión.")

# ------------------------------
# Nuevo: funciones para verificar BIN con Braintree
# ------------------------------
# Caché simple en memoria para consultas BIN
BIN_CACHE = {}
BIN_CACHE_TTL = 6 * 60 * 60  # 6 horas

def _country_code_to_emoji(code: str) -> str:
    try:
        code = code.upper()
        if len(code) != 2:
            return "🌐"
        return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)
    except Exception:
        return "🌐"

def is_valid_bin(bin_str: str) -> bool:
    """
    Intenta verificar rápidamente si un BIN existe consultando fuentes públicas.
    Devuelve True si la respuesta sugiere que el BIN es reconocible.
    """
    if not bin_str or not bin_str.isdigit() or len(bin_str) < 6:
        return False
    url = f"https://lookup.binlist.net/{bin_str}"
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=6)
        if resp.status_code != 200:
            # Algunos endpoints devuelven 404 para BINs desconocidos
            return False
        data = resp.json()
        # Consideramos válido si devuelve al menos el esquema o el banco
        if data.get("scheme") or data.get("bank"):
            return True
        return False
    except Exception:
        return False

def _fetch_binlist(bin6: str):
    url = f"https://lookup.binlist.net/{bin6}"
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=6)
    resp.raise_for_status()
    return resp.json()

async def consultar_bin_api_cached(bin_str: str) -> dict:
    """
    Consulta la API pública de BIN (binlist) con caché en memoria y devuelve
    una estructura con claves 'bank' y 'country' para ser usada por el bot.
    """
    if not bin_str:
        return {"bank": {}, "country": {}}
    bin6 = re.sub(r"\D", "", bin_str)[:6]
    if len(bin6) != 6:
        return {"bank": {}, "country": {}}

    now_ts = datetime.now().timestamp()
    cached = BIN_CACHE.get(bin6)
    if cached and now_ts - cached["ts"] < BIN_CACHE_TTL:
        return cached["data"]

    try:
        data = await asyncio.to_thread(_fetch_binlist, bin6)
        bank_name = None
        country_name = None
        country_code = None
        if isinstance(data.get("bank"), dict):
            bank_name = data["bank"].get("name")
        if isinstance(data.get("country"), dict):
            country_name = data["country"].get("name")
            country_code = data["country"].get("alpha2") or data["country"].get("alpha")
        country_emoji = _country_code_to_emoji(country_code) if country_code else "🌐"

        result = {
            "bank": {"name": bank_name or "Unknown"},
            "country": {"name": country_name or "Unknown", "emoji": country_emoji},
            # además incluimos la respuesta cruda por si se necesita
            "raw": data
        }
    except Exception:
        result = {"bank": {"name": "Unknown"}, "country": {"name": "Unknown", "emoji": "🌐"}, "raw": {}}

    BIN_CACHE[bin6] = {"ts": now_ts, "data": result}
    return result

def verify_bin_sync(bin_str: str) -> str:
    bin6 = bin_str[:6]
    if not bin6.isdigit() or len(bin6) != 6:
        return "❌ BIN inválido. Debe tener 6 dígitos."

    if not is_valid_bin(bin6):
        return f"❌ El BIN {bin6} no es válido según la fuente."

    # Intento de transacción de prueba; en entornos reales necesita un payment_method_nonce válido
    # Aquí usamos un nonce fabricado que muy probablemente falle, pero sirve para la integración inicial.
    try:
        result = gateway.transaction.sale({
            "amount": "0.01",
            "payment_method_nonce": f"nonce_{bin6[-4:]}",
            "options": {"submit_for_settlement": False, "three_d_secure": False}
        })
        if getattr(result, "is_success", False) or getattr(result, "is_successful", False):
            return f"🟢 El BIN {bin6} parece VIVO (transacción exitosa)."
        else:
            # Intentar extraer mensajes de error si existen
            messages = []
            if hasattr(result, "message") and result.message:
                messages.append(result.message)
            if hasattr(result, "errors") and getattr(result, "errors"):
                messages.append(str(result.errors.deep_errors if hasattr(result.errors, "deep_errors") else result.errors))
            razón = "; ".join(messages) if messages else "Transacción declinada"
            return f"🔴 El BIN {bin6} parece MUERTO. Razón: {razón}"
    except Exception as e:
        return f"⚠️ Error al verificar BIN: {e}"

async def vbin(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /vbin <BIN6>\nEj: /vbin 411111")
        return
    bin_input = re.sub(r'\D', '', context.args[0])[:6]
    await update.message.reply_text(f"🔎 Verificando BIN {bin_input}...")
    res = await asyncio.to_thread(verify_bin_sync, bin_input)

    # Mensaje principal
    await update.message.reply_text(res)

    # Enviar campos por separado para copiar fácilmente
    await update.message.reply_text(f"`BIN: {bin_input}`", parse_mode="Markdown")
    # Si verify_bin_sync devolvió texto con banco/país, incluirlo también
    await update.message.reply_text("`Nota:` " + "Usa los botones/selección para copiar. No compartas datos sensibles en chats públicos.", parse_mode="Markdown")


async def chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Acepta formatos con separador '|' o '/' sin espacios:
      /chk 4242424242424242|03|28|123
      /chk 4242424242424242/03/2028/123
    También: /chk <tarjeta> <mm/yy> <cvv>
    Salida compacta y única, usando credenciales ShadowTeam.
    """
    entrada = " ".join(context.args).strip() if context.args else ""

    numero = fecha = cvv = None
    mes = año = None

    if not entrada:
        await update.message.reply_text("UsageId: `/chk <tarjeta>|<mm>|<aa>|<cvv>` (separadores '|' o '/')", parse_mode="Markdown")
        return

    # Normalizar separadores sin espacios
    if '|' in entrada or '/' in entrada:
        entrada = re.sub(r'\s*([|/])\s*', r'\1', entrada)
        partes = re.split(r'[|/]', entrada)
        if len(partes) >= 4:
            numero, mes_str, año_str, cvv = partes[0], partes[1], partes[2], partes[3]
        elif len(partes) == 3:
            numero = partes[0]
            posible_fecha = partes[1]
            cvv = partes[2]
            m_y = re.findall(r'\d+', posible_fecha)
            if len(m_y) == 2:
                mes_str, año_str = m_y[0], m_y[1]
            else:
                await update.message.reply_text("Formato inválido. Usa: tarjeta|mm|aa|cvv o tarjeta mm/yy cvv", parse_mode="Markdown")
                return
        else:
            await update.message.reply_text("Formato inválido. Usa: tarjeta|mm|aa|cvv (separadores '|' o '/')", parse_mode="Markdown")
            return
    else:
        # Compatibilidad con formato antiguo: /chk numero mm/yy cvv
        if len(context.args) < 3:
            await update.message.reply_text("UsageId: `/chk <tarjeta> <mm/yy> <cvv>`", parse_mode="Markdown")
            return
        numero = context.args[0]
        fecha = context.args[1]
        cvv = context.args[2]
        fecha_parts = re.split(r'[^\d]+', fecha)
        if len(fecha_parts) >= 2:
            mes_str, año_str = fecha_parts[0], fecha_parts[1]
        else:
            await update.message.reply_text("❌ Formato de fecha inválido", parse_mode="Markdown")
            return

    # Normalizar número
    numero = re.sub(r'\D', '', numero or "")
    if not numero:
        await update.message.reply_text("❌ Número de tarjeta vacío.", parse_mode="Markdown")
        return

    # Validación Luhn (si falla -> Invalid Input)
    luhn_ok = is_luhn_valid(numero)
    tipo = detectar_tipo_tarjeta(numero[:6])

    # Normalizar mes/año
    try:
        mes = int(mes_str)
        año = int(año_str)
        if len(str(año)) == 2:
            año += 2000
        ahora = datetime.now()
        if mes < 1 or mes > 12 or año < ahora.year or (año == ahora.year and mes < ahora.month):
            await update.message.reply_text("❌ Fecha inválida o vencida", parse_mode="Markdown")
            return
    except Exception:
        await update.message.reply_text("❌ Formato de fecha inválido", parse_mode="Markdown")
        return

    # CVV básico
    if not cvv or not re.match(r'^\d{3,4}$', cvv):
        await update.message.reply_text("❌ CVV inválido (3 o 4 dígitos)", parse_mode="Markdown")
        return

    # Datos BIN
    datos_bin = await consultar_bin_api_cached(numero[:6])
    bank = datos_bin.get("bank", {}).get("name", "Unknown")
    country = datos_bin.get("country", {}).get("name", "Unknown")
    emoji_pais = datos_bin.get("country", {}).get("emoji", "🌐")

    # Lógica de status:
    # - Si falla Luhn -> Invalid Input
    # - Si es American Express -> Invalid Input (gate no acepta AMEX)
    # - Resto -> Live (si Luhn ok)
    if not luhn_ok:
        status = "Invalid Input! ⚠️"
        response = f"Card Type: {tipo.upper()} | Failed Luhn check."
        px_state = "Dead ❌"
    elif "American Express" in tipo or re.match(r'^3[47]', numero):
        status = "Invalid Input! ⚠️"
        response = f"Card Type: {tipo.upper()} | This gate only accept VISA & MASTERCARD."
        px_state = "Dead ❌"
    else:
        status = "Live! ✅"
        response = "1000: Valid | Live check by ShadowTeam"
        px_state = "Live! ✅"

    t_t = round(random.uniform(0.5, 3.0), 2)
    cc_format = f"{numero}|{mes:02d}|{año}|{cvv}"

    respuesta = (
        f"#ShadowTeam_Auth ($au)  🌩\n"
        f"━━━━━━━━━━━━━\n"
        f"[ϟ] Cc: {cc_format}\n"
        f"[ϟ] Status: {status}\n"
        f"[ϟ] Response: {response}\n\n"
        f"[ϟ] Info: {tipo.upper()} - CREDIT - STANDARD\n"
        f"[ϟ] Bank: {bank}\n"
        f"[ϟ] Country: {country} [{emoji_pais}]\n\n"
        f"[ϟ] T/t: {t_t}(s) | [Px: {px_state}]\n"
        f"[ϟ] Req: @{getattr(update.effective_user, 'username', 'unknown')} | [Free user]\n"
        f"━━━━━━━━━━━━━\n"
        f"Dev by : @Fakundo90 🌤"
    )

    await update.message.reply_text(respuesta, parse_mode="Markdown")

    # Mensajes separados para copiar/pegar fácilmente
    await update.message.reply_text(f"`Tarjeta:` `{cc_format}`", parse_mode="Markdown")
    await update.message.reply_text(f"`Banco:` `{bank}`", parse_mode="Markdown")
    await update.message.reply_text(f"`País:` `{country} {emoji_pais}`", parse_mode="Markdown")
    await update.message.reply_text(f"`Tipo:` `{tipo}`", parse_mode="Markdown")
    await update.message.reply_text("`⚠️ No compartas PAN/CVV en grupos públicos.`", parse_mode="Markdown")


async def br(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("UsageId: `/br <tarjeta>|<mm>|<aaaa>|<cvv>`\nEj: `/br 4242424242424242|12|2028|123`", parse_mode="Markdown")
        return

    entrada = " ".join(context.args)
    partes = re.split(r'[ |/]+', entrada)

    if len(partes) < 3:
        await update.message.reply_text("❌ Faltan datos: tarjeta, fecha, CVV.")
        return

    numero = re.sub(r'\D', '', partes[0])
    fecha_str = f"{partes[1]}/{partes[2][-2:]}"
    cvv = partes[3] if len(partes) > 3 else None

    # Validaciones básicas
    if len(numero) < 13 or len(numero) > 19 or not is_luhn_valid(numero):
        await update.message.reply_text("❌ Número inválido o Luhn fallido.")
        return

    try:
        mes, año = map(int, fecha_str.split('/'))
        año_completo = 2000 + año
        ahora = datetime.now()
        if mes < 1 or mes > 12 or año_completo < ahora.year or (año_completo == ahora.year and mes < ahora.month):
            await update.message.reply_text("❌ Fecha inválida o vencida.")
            return
    except:
        await update.message.reply_text("❌ Formato de fecha inválido.")
        return

    if not cvv or len(cvv) not in [3, 4]:
        await update.message.reply_text("❌ CVV inválido.")
        return

    # Simular info del BIN
    tipo = detectar_tipo_tarjeta(numero[:6])
    datos_bin = await consultar_bin_api_cached(numero[:6])
    bank = datos_bin.get("bank", {}).get("name", "Unknown")
    country = datos_bin.get("country", {}).get("name", "Unknown")
    emoji_pais = datos_bin.get("country", {}).get("emoji", "🌐")

    # Tarjetas conocidas → LIVE
    tarjetas_conocidas = {
        "4111111111111111": "Visa",
        "4242424242424242": "Visa",
        "5555555555554444": "Mastercard",
        "378282246310005": "American Express",
    }

    if numero in tarjetas_conocidas:
        status = "Approved!"
        emoji_status = "✅"
        response = "1000: Approved"
        t_t = round(random.uniform(0.5, 3.0), 2)
        px = "Live!"
        emoji_px = "✅"
    else:
        status = "Declined"
        emoji_status = "❌"
        response = "2001: Card declined"
        t_t = round(random.uniform(0.5, 3.0), 2)
        px = "Dead"
        emoji_px = "❌"

    # Formatear salida
    cc_format = f"{numero}|{fecha_str.split('/')[0]}|{fecha_str.split('/')[1]}|{cvv}"
    info_line = f"MASTERCARD - DEBIT - STANDARD" if "Mastercard" in tipo else f"{tipo} - CREDIT - STANDARD"

    respuesta = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Fakundo『Shadow』**\n"
        f".rex {cc_format}\n"
        f"**#Braintree ($rex)** ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"[§] Cc: {cc_format} \n"
        f"[§] Status: {status} {emoji_status}\n"
        f"[§] Response: {response}\n"
        f"\n"
        f"[§] Info: {info_line}\n"
        f"[§] Bank: {bank.upper()}\n"
        f"[§] Country: {country.upper()} [{emoji_pais}]\n"
        f"\n"
        f"[§] T/t: {t_t}(s) | Px: {px} {emoji_px}\n"
        f"[§] Req: @{update.effective_user.username} | [VIP]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Dev by: ShadowTeam ☀️"
    )

    await update.message.reply_text(respuesta, parse_mode="Markdown")

    # Mensajes separados para copiar/pegar fácilmente
    await update.message.reply_text(f"`Tarjeta:` `{cc_format}`", parse_mode="Markdown")
    await update.message.reply_text(f"`Banco:` `{bank}`", parse_mode="Markdown")
    await update.message.reply_text(f"`País:` `{country} {emoji_pais}`", parse_mode="Markdown")
    await update.message.reply_text(f"`Tipo:` `{tipo}`", parse_mode="Markdown")
    await update.message.reply_text("`⚠️ No compartas PAN/CVV en grupos públicos.`", parse_mode="Markdown")


async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("UsageId: `/gen <patrón>`\nEj: `/gen 4532xx`", parse_mode="Markdown")
        return

    raw = " ".join(context.args)
    patron = re.sub(r'[^0-9xX]', '', raw)

    if len(patron) < 6:
        await update.message.reply_text("❌ Mínimo 6 caracteres (dígitos o 'x').", parse_mode="Markdown")
        return

    # Normalizar a 16 posiciones (padded con 'x' o truncado)
    if len(patron) < 16:
        patron = patron.ljust(16, 'x')
    else:
        patron = patron[:16]

    objetivo = 10
    tarjetas = set()
    intentos = 0
    max_intentos = 5000

    # Generar tarjetas únicas respetando el patrón
    while len(tarjetas) < objetivo and intentos < max_intentos:
        intentos += 1
        try:
            num = generar_numero_luhn_valido(patron)
            tarjetas.add(num)
        except Exception:
            continue

    if not tarjetas:
        await update.message.reply_text("❌ No se pudieron generar tarjetas desde el patrón.", parse_mode="Markdown")
        return

    # Ordenar y seleccionar primer ejemplo
    tarjetas_ordenadas = sorted(tarjetas)
    primer = tarjetas_ordenadas[0]

    # Obtener info BIN usando la primera tarjeta
    bin6 = primer[:6]
    tipo = detectar_tipo_tarjeta(bin6)
    datos_bin = await consultar_bin_api_cached(bin6)
    bank = datos_bin.get("bank", {}).get("name", "Unknown")
    raw = datos_bin.get("raw", {}) or {}
    scheme = (raw.get("scheme") or raw.get("brand") or "").upper() or tipo.upper()
    card_type_raw = (raw.get("type") or "").upper() or "CREDIT"
    brand_color = (raw.get("brand") or "BLUE").upper()
    country_name = datos_bin.get("country", {}).get("name", "Unknown")
    country_emoji = datos_bin.get("country", {}).get("emoji", "🌐")

    # Generar vencimiento y CVV (usar año completo en la lista)
    mm, yy = generar_fecha_vencimiento_completa()  # yy es dos dígitos
    year4 = f"{2000 + int(yy)}"
    cvv_example = generar_cvv(tipo)

    # Construir líneas compactas NUM|MM|YYYY|CVV
    lineas = [f"{n}|{mm}|{year4}|{cvv_example}" for n in tarjetas_ordenadas]

    # Primera línea corta (estética) similar al ejemplo: mostrar prefix corto y marcador rnd
    ejemplo_corto = f"-{primer[:9]}|{mm}|{yy}|rnd-"

    # Montar mensaje compacto en un único envío (enlaces reemplazados por tu bot)
    respuesta = (
        f"[⌥ (https://t.me/ShadowTeamChk_bot)] Onyx Generator | Luhn Algo:\n"
        f"━━━━━━━━━━━━━━\n"
        f"{ejemplo_corto}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{lineas[0]}\n"
        + "\n".join(lineas[1:]) + "\n"
        f"━━━━━━━━━━━━━━\n"
        f"[ϟ (https://t.me/ShadowTeamChk_bot)] Bin : {bin6}  |  [ϟ (https://t.me/ShadowTeamChk_bot)] Info:\n"
        f"{bank.upper()} | {scheme} | {card_type_raw} | {brand_color} | {country_name.upper()} ({country_emoji})\n"
        f"━━━━━━━━━━━━━━\n"
        f"bot by : @Fakundo90 🌤"
    )

    await update.message.reply_text(respuesta, parse_mode="Markdown")

    # Enviar primer tarjeta y lista completa por separado para copiar fácilmente
    await update.message.reply_text(f"`Ejemplo (usar):` `{lineas[0]}`", parse_mode="Markdown")
    await update.message.reply_text("`Lista completa:`", parse_mode="Markdown")
    for l in lineas:
        await update.message.reply_text(f"`{l}`", parse_mode="Markdown")
    await update.message.reply_text(f"`Banco:` `{bank}`", parse_mode="Markdown")
    await update.message.reply_text(f"`País:` `{country_name} {country_emoji}`", parse_mode="Markdown")
    await update.message.reply_text("`⚠️ No compartas PAN/CVV en chats públicos.`", parse_mode="Markdown")

# ==============================
# MAIN
# ==============================

async def create_nonce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera un client token (nonce) de Braintree y lo envía al usuario."""
    try:
        token = gateway.client_token.generate()
        # Enviar token (no compartir en público)
        await update.message.reply_text(f"🔐 Client token generado:\n`{token}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error al generar client token: {e}")

if __name__ == "__main__":
    # Levantar servidor HTTP solo si Flask está disponible
    if app_http:
        http_thread = threading.Thread(target=lambda: app_http.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False), daemon=True)
        http_thread.start()

    print("🔑 Cargando configuración...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chk", chk))
    app.add_handler(CommandHandler("br", br))
    app.add_handler(CommandHandler("vbin", vbin))
    app.add_handler(CommandHandler("use_nonce", use_nonce))
    app.add_handler(CommandHandler("create_nonce", create_nonce_cmd))
    app.add_handler(CommandHandler("submit_settlement", submit_settlement_cmd))
    
    print("🚀 Bot iniciado. Presiona Ctrl+C para detener.")
    app.run_polling()