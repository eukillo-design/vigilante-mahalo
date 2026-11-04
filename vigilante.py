"""
Vigilante de visibilidad - Mahalo Poke Sevilla Bermejales.

Cada vez que se ejecuta (GitHub lo lanza cada 10 minutos):
  1. Mira si estamos dentro del horario de config.yaml. Si no, termina.
  2. Abre la página de cada tienda como lo haría un cliente.
  3. Decide si la tienda está VISIBLE, NO VISIBLE o si no se ha podido comprobar.
  4. Si algo ha cambiado desde la última vez, avisa por Telegram.
El estado anterior se guarda en estado.json.
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

BASE = Path(__file__).parent
ESTADO_PATH = BASE / "estado.json"
CAPTURAS = BASE / "capturas"
TZ = ZoneInfo("Europe/Madrid")
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

# Cuántas comprobaciones seguidas "sin poder comprobar" antes de avisar
FALLOS_ANTES_DE_AVISAR = 3

# Textos (sin tildes, en minúsculas) que indican cada estado
TEXTOS_CERRADO = [
    "cerrado", "cerrada", "closed", "no disponible", "unavailable",
    "temporalmente", "abre a las", "abre manana", "opens at", "available at",
    "disponible a las", "no acepta pedidos", "no esta aceptando",
    "solo pedidos programados", "programar pedido", "schedule order",
    "no realiza entregas", "fuera de su zona", "fuera de la zona",
]
TEXTOS_ABIERTO = [
    "abierto hasta", "open until", "anadir al pedido", "add to order",
    "anadir a la cesta", "add to cart", "ir a pagar", "anadir al carrito",
]


# ------------------------------------------------------------------ utilidades
def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower())


def cargar_config():
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cargar_estado():
    if ESTADO_PATH.exists():
        return json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
    return {"tiendas": {}}


def guardar_estado(estado):
    ESTADO_PATH.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def dentro_de_horario(config, ahora: datetime) -> bool:
    h = config["horario"]
    if DIAS[ahora.weekday()] not in [normalizar(d) for d in h["dias"]]:
        return False
    minuto = ahora.hour * 60 + ahora.minute
    for franja in h["franjas"]:
        ini, fin = franja.split("-")
        a = int(ini[:2]) * 60 + int(ini[3:])
        b = int(fin[:2]) * 60 + int(fin[3:])
        # margen: no se avisa en los 5 primeros minutos (apertura) ni al cierre
        if a + 5 <= minuto < b:
            return True
    return False


# ------------------------------------------------------------------ Telegram
def telegram_chat_id(estado):
    chat = os.environ.get("TELEGRAM_CHAT_ID") or estado.get("telegram_chat_id")
    if chat:
        return chat
    token = os.environ["TELEGRAM_TOKEN"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    for upd in reversed(r.get("result", [])):
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat:
            estado["telegram_chat_id"] = chat
            return chat
    raise SystemExit("No encuentro tu chat de Telegram: abre tu bot, pulsa Iniciar y escríbele 'hola'.")


def enviar_telegram(estado, texto, foto: Path | None = None):
    try:
        _enviar_telegram(estado, texto, foto)
    except BaseException as e:  # nunca romper la vigilancia por un fallo de Telegram
        print(f"AVISO Telegram: no se pudo enviar ({e})")


def _enviar_telegram(estado, texto, foto: Path | None = None):
    token = os.environ["TELEGRAM_TOKEN"]
    chat = telegram_chat_id(estado)
    if foto and foto.exists():
        with open(foto, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data={"chat_id": chat, "caption": texto[:1000]},
                          files={"photo": f}, timeout=30)
    else:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": texto}, timeout=30)


# ------------------------------------------------------------------ navegador
def aceptar_minimas_cookies(page):
    """Rechaza cookies no esenciales si aparece el banner; si no hay opción, lo quita de la vista."""
    try:  # banner de mahalopoke.es: Gestionar -> rechazar/guardar
        g = page.get_by_role("button", name=re.compile(r"^gestionar$", re.I)).first
        if g.is_visible(timeout=800):
            g.click()
            page.wait_for_timeout(1000)
            for t in [r"rechazar", r"guardar", r"confirmar"]:
                try:
                    b = page.get_by_role("button", name=re.compile(t, re.I)).first
                    if b.is_visible(timeout=800):
                        b.click()
                        page.wait_for_timeout(800)
                        break
                except Exception:
                    pass
    except Exception:
        pass
    try:  # si sigue algún aviso de cookies tapando la página, se oculta
        page.evaluate("""() => document.querySelectorAll('[class*=cookie i],[id*=cookie i],[class*=cky],[id*=usercentrics i]')
              .forEach(e => { if (getComputedStyle(e).position === 'fixed') e.style.display = 'none'; })""")
    except Exception:
        pass
    for txt in ["Denegar", "Rechazar", "Rechazar todo", "Rechazar todas", "Solo necesarias",
                "Reject all", "Decline", "Aceptar solo necesarias"]:
        try:
            b = page.get_by_role("button", name=re.compile(txt, re.I)).first
            if b.is_visible(timeout=800):
                b.click()
                return
        except Exception:
            pass


def clic_texto(page, patron, timeout=6000):
    loc = page.get_by_text(re.compile(patron, re.I)).first
    loc.wait_for(state="visible", timeout=timeout)
    loc.click()


def preparar_app_propia(page, tienda, direccion):
    """Web de Flipdish (mahalopoke.es): poner dirección y elegir el local."""
    page.wait_for_timeout(4000)
    marco = page
    for fr in page.frames:  # el pedido de Flipdish puede ir dentro de un iframe
        if "flipdish" in (fr.url or ""):
            marco = fr
    try:
        clic_texto(marco, r"a[ñn]adir nueva direcci[oó]n", 10000)
    except Exception:
        pass
    campo = marco.locator("input[type=text], input[type=search], input:not([type])").first
    campo.wait_for(state="visible", timeout=10000)
    campo.fill(direccion)
    page.wait_for_timeout(2500)
    try:  # primera sugerencia de dirección
        marco.locator("[role=option], li").filter(has_text=re.compile("Austria", re.I)).first.click(timeout=5000)
    except Exception:
        campo.press("ArrowDown")
        campo.press("Enter")
    page.wait_for_timeout(2500)
    for boton in [r"^confirmar", r"^guardar", r"^continuar", r"^usar esta direcci"]:
        try:
            clic_texto(marco, boton, 2500)
            page.wait_for_timeout(2000)
        except Exception:
            pass
    try:  # elegir el local si sale una lista de locales
        clic_texto(marco, tienda.get("local", "Bermejales"), 6000)
    except Exception:
        pass
    page.wait_for_timeout(4000)
    return marco


# Qué pulsar en cada plataforma para abrir el cuadro de dirección
ABRIR_DIRECCION = {
    "glovo": [r"^sevilla$", r"introduce tu direcci", r"a[ñn]adir direcci", r"direcci[oó]n de entrega"],
    "uber": [r"introduce la direcci", r"enter delivery address", r"a[ñn]adir direcci",
             r"enter address", r"introduce una direcci"],
    "justeat": [r"introduce tu direcci", r"a[ñn]adir direcci", r"comprobar", r"c[oó]digo postal",
                r"enter your address"],
}


def poner_direccion_glovo(page, direccion):
    """Glovo abre un cuadro '¿Dónde hay que hacer la entrega?' con el campo 'Buscar dirección'."""
    campo = page.get_by_placeholder(re.compile(r"buscar direcci", re.I)).first
    try:
        campo.wait_for(state="visible", timeout=6000)
    except Exception:
        try:
            clic_texto(page, r"^sevilla$", 3000)
            campo.wait_for(state="visible", timeout=5000)
        except Exception:
            return False
    campo.click()
    campo.press_sequentially(direccion, delay=60)
    page.wait_for_timeout(3000)
    try:
        page.get_by_text(re.compile(r"austria", re.I)).last.click(timeout=5000)
    except Exception:
        campo.press("ArrowDown")
        campo.press("Enter")
    page.wait_for_timeout(3000)
    for boton in [r"^confirmar", r"^guardar", r"^continuar", r"^aceptar"]:
        try:
            page.get_by_role("button", name=re.compile(boton, re.I)).first.click(timeout=1500)
            page.wait_for_timeout(2000)
        except Exception:
            pass
    page.wait_for_timeout(3000)
    return True


def poner_direccion(page, plataforma, direccion):
    """Intenta fijar la dirección de entrega como haría un cliente. No falla si no puede."""
    if plataforma == "glovo":
        return poner_direccion_glovo(page, direccion)
    for patron in ABRIR_DIRECCION.get(plataforma, []):
        try:
            clic_texto(page, patron, 2500)
            page.wait_for_timeout(1500)
            break
        except Exception:
            continue
    try:
        campo = page.locator(
            "input[placeholder*='irecci' i], input[placeholder*='ddress' i], "
            "input[placeholder*='postal' i], input[type=search], input[name*='addr' i]").first
        campo.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    campo.fill(direccion)
    page.wait_for_timeout(3000)
    try:
        page.locator("[role=option], [role=listbox] li, ul li").filter(
            has_text=re.compile("Austria", re.I)).first.click(timeout=5000)
    except Exception:
        campo.press("ArrowDown")
        campo.press("Enter")
    page.wait_for_timeout(2500)
    for boton in [r"^confirmar", r"^guardar", r"^continuar", r"^aceptar", r"^listo", r"^save",
                  r"^confirm", r"^done", r"^usar esta direcci"]:
        try:
            clic_texto(page, boton, 1500)
            page.wait_for_timeout(1500)
        except Exception:
            pass
    page.wait_for_timeout(3000)
    return True


def leer_texto(marco):
    try:
        return marco.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def clasificar(texto, tienda):
    t = normalizar(texto)
    if any(x in t for x in ["verificacion de seguridad", "you have been blocked", "unable to access",
                            "verify you are human", "oh, no!", "there's a problem"]):
        return "bloqueado", "la plataforma ha bloqueado la comprobación automática"
    if len(t) < 200:
        return "desconocido", "la página no ha cargado"
    if tienda["plataforma"] == "app_propia":
        local = normalizar(tienda.get("local", "bermejales"))
        if local not in t:
            return "desconocido", "no he llegado a la página del local"
        m = re.search(r"abierto hasta las? [0-9:apm ]+", t)
        if m:
            if "no realiza entregas" in t:
                return "desconocido", "la dirección queda fuera del radio"
            return "visible", m.group(0)
    # Sin dirección puesta, Uber/Glovo dicen "no disponible" aunque esté abierto:
    # eso no es un cierre, es que no he podido poner la dirección.
    if re.search(r"(introduce|enter|anade|anadir) (tu |la |una )?(direccion|delivery address|address)", t) \
            and not re.search(r"\d+\s*[-–]\s*\d+\s*min", t):
        return "desconocido", "no he podido poner la dirección de entrega"
    for frase in TEXTOS_CERRADO:
        if frase in t:
            return "no_visible", f"la página dice «{frase}»"
    if re.search(r"\d+\s*[-–]\s*\d+\s*min", t) or any(frase in t for frase in TEXTOS_ABIERTO):
        return "visible", "acepta pedidos"
    return "desconocido", "no reconozco el estado de la página"


def comprobar(browser, tienda, direccion, intento):
    ctx = browser.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
                              locale="es-ES", timezone_id="Europe/Madrid",
                              viewport={"width": 1366, "height": 900},
                              geolocation={"latitude": 37.358, "longitude": -5.986},
                              permissions=["geolocation"])
    page = ctx.new_page()
    captura = CAPTURAS / f"{re.sub(r'[^a-z0-9]+', '_', normalizar(tienda['nombre'] + '_' + tienda['plataforma']))}_{intento}.png"
    try:
        page.goto(tienda["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        aceptar_minimas_cookies(page)
        marco = page
        if tienda["plataforma"] == "app_propia":
            marco = preparar_app_propia(page, tienda, direccion)
        else:
            page.wait_for_timeout(3000)
            poner_direccion(page, tienda["plataforma"], direccion)
            aceptar_minimas_cookies(page)
        texto = leer_texto(marco)
        estado, motivo = clasificar(texto, tienda)
    except Exception as e:
        estado, motivo = "desconocido", f"error al abrir la página ({type(e).__name__})"
    try:
        page.screenshot(path=str(captura), full_page=False)
    except Exception:
        captura = None
    ctx.close()
    return estado, motivo, captura


# ------------------------------------------------------------------ principal
def main():
    forzar = "--forzar" in sys.argv     # comprueba aunque esté fuera de horario
    prueba = "--prueba" in sys.argv     # manda un resumen por Telegram siempre
    config = cargar_config()
    estado = cargar_estado()
    ahora = datetime.now(TZ)

    if "--en-horario" in sys.argv:  # usado por GitHub para no gastar tiempo fuera de horario
        dentro = dentro_de_horario(config, ahora)
        print("dentro" if dentro else "fuera")
        sys.exit(0 if dentro else 1)

    if not (forzar or dentro_de_horario(config, ahora)):
        print(f"{ahora:%H:%M} fuera de horario: no se comprueba nada.")
        return

    from playwright.sync_api import sync_playwright
    CAPTURAS.mkdir(exist_ok=True)
    resumen = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        for tienda in config["tiendas"]:
            clave = f"{tienda['plataforma']}|{tienda['nombre']}"
            est, motivo, foto = comprobar(browser, tienda, config["direccion"], 1)
            if est == "no_visible":  # segunda comprobación para evitar falsas alarmas
                time.sleep(20)
                est, motivo, foto = comprobar(browser, tienda, config["direccion"], 2)

            previo = estado["tiendas"].get(clave, {"estado": "visible", "fallos": 0})
            plataforma = {"app_propia": "App propia", "glovo": "Glovo",
                          "uber": "Uber Eats", "justeat": "Just Eat"}.get(tienda["plataforma"], tienda["plataforma"])
            hora = f"{ahora:%H:%M}"
            print(f"[{hora}] {plataforma} - {tienda['nombre']}: {est} ({motivo})")
            resumen.append(f"• {plataforma} – {tienda['nombre']}: {est} ({motivo})")

            if est in ("desconocido", "bloqueado"):
                previo["fallos"] = previo.get("fallos", 0) + 1
                if previo["fallos"] == FALLOS_ANTES_DE_AVISAR:
                    enviar_telegram(estado, f"❓ {tienda['nombre']} en {plataforma}: no he podido comprobarlo "
                                            f"{FALLOS_ANTES_DE_AVISAR} veces seguidas ({motivo}). Revísalo a mano.", foto)
            else:
                previo["fallos"] = 0
                if est != previo["estado"]:
                    if est == "no_visible":
                        enviar_telegram(estado, f"⚠️ {tienda['nombre']} en {plataforma} HA DEJADO DE ESTAR VISIBLE "
                                                f"({hora}). Motivo: {motivo}.", foto)
                    else:
                        enviar_telegram(estado, f"✅ {tienda['nombre']} en {plataforma} vuelve a estar visible ({hora}).")
                previo["estado"] = est
            previo["ultima"] = ahora.isoformat(timespec="minutes")
            estado["tiendas"][clave] = previo
        browser.close()

    if prueba:
        enviar_telegram(estado, "🧪 Prueba del vigilante Mahalo Bermejales\n" + "\n".join(resumen))
    guardar_estado(estado)


if __name__ == "__main__":
    main()
