#!/usr/bin/env python3
"""Consulta el mercado P2P de Binance y escribe data/tasas.json.

Reemplaza el script embebido en el YAML del workflow, que interpolaba los
promedios directamente dentro del código Python (`ves = $VES_AVG`). Si algún
curl fallaba, la variable quedaba vacía, Python moría con SyntaxError y la
redirección `> data/tasas.json` dejaba el archivo en cero bytes: el sitio
entero se quedaba sin datos hasta la siguiente corrida.

Aquí, si no se puede obtener el precio del USDT en Venezuela (el denominador
de todas las tasas) el script sale con error sin tocar el archivo, y se
conserva el último JSON bueno.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
BCV_URL = "https://www.bcv.org.ve/"

# El servidor de bcv.org.ve manda la cadena de certificados incompleta (le
# falta el intermedio). Los navegadores lo toleran porque suelen tener ese
# intermedio cacheado, pero un runner limpio (como el de GitHub Actions) lo
# rechaza con CERTIFICATE_VERIFY_FAILED. Es un problema conocido del sitio,
# no nuestro. Decisión de Jorge (28-sep-2026): desactivar la verificación
# SOLO para esta petición puntual — es una lectura pública, sin credenciales
# ni datos sensibles de por medio. El resto del script sigue verificando TLS
# normal (Binance no se toca).
_BCV_SSL_CTX = ssl.create_default_context()
_BCV_SSL_CTX.check_hostname = False
_BCV_SSL_CTX.verify_mode = ssl.CERT_NONE

# El BCV publica su tasa una vez al día — a diferencia del paralelo (Binance),
# no hace falta scrapearla cada hora. Solo se consulta cerca de las 5am y las
# 6pm hora Venezuela (VET = UTC-4 fijo, sin horario de verano); el resto de
# las corridas horarias conserva el último valor bueno sin tocar bcv.org.ve.
HORAS_BCV_VE = {5, 18}

# Venezuela se consulta del lado SELL (lo que piden por vender USDT) y el resto
# del lado BUY, igual que hacía el workflow original.
MERCADOS = {
    "VES": "SELL",
    "COP": "BUY",
    "ARS": "BUY",
    "CLP": "BUY",
    "MXN": "BUY",
    "PEN": "BUY",
    "EUR": "BUY",
}

INTENTOS = 3
ESPERA = 5
RUTA_SALIDA = "data/tasas.json"
MAX_HISTORIAL = 24  # una corrida por hora -> últimas 24h


def consultar(fiat, trade_type):
    """Devuelve la lista de anuncios de USDT para un fiat, o [] si no hay."""
    payload = json.dumps({
        "fiat": fiat,
        "asset": "USDT",
        "tradeType": trade_type,
        "page": 1,
        "rows": 10,
    }).encode()

    for intento in range(1, INTENTOS + 1):
        req = urllib.request.Request(
            URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "tasas-latam/1.0 (+https://tasas.henkki.co)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            return data.get("data") or []
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            print(f"  {fiat}: intento {intento}/{INTENTOS} falló ({e})", file=sys.stderr)
            if intento < INTENTOS:
                time.sleep(ESPERA)

    return None  # None = no se pudo consultar; [] = se consultó y no hay ofertas


def promedio(anuncios):
    """Promedio de precio descartando el primer anuncio (suele ir promocionado)."""
    if not anuncios:
        return None
    precios = [float(a["adv"]["price"]) for a in anuncios[1:6] if "adv" in a]
    return sum(precios) / len(precios) if precios else None


def bcv_oficial():
    """Tasa oficial de referencia del BCV (USD y EUR), leída del HTML público
    de bcv.org.ve (sitio server-rendered, sin JS). Devuelve None si el sitio
    no responde o cambia de maquetado, para no tumbar el resto del script."""
    req = urllib.request.Request(
        BCV_URL,
        headers={"User-Agent": "tasas-latam/1.0 (+https://tasas.henkki.co)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_BCV_SSL_CTX) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"BCV: sin respuesta ({e})", file=sys.stderr)
        return None

    def extraer(bloque_id):
        m = re.search(
            rf'id="{bloque_id}".*?<strong class="strong-tb">\s*([\d.,]+)\s*</strong>',
            html, re.S,
        )
        # Formato BCV: coma decimal, punto de miles (si aparece).
        return float(m.group(1).replace(".", "").replace(",", ".")) if m else None

    usd = extraer("dolar")
    eur = extraer("euro")
    fecha_m = re.search(r'property="dc:date"[^>]*content="([^"]+)"', html)
    fecha_valor = fecha_m.group(1) if fecha_m else None

    if not usd or not eur:
        print("BCV: no se pudo extraer USD/EUR del HTML (¿cambió el maquetado?)",
              file=sys.stderr)
        return None

    return {"usd": round(usd, 4), "eur": round(eur, 4), "fecha_valor": fecha_valor}


def main():
    precios = {}
    for fiat, trade_type in MERCADOS.items():
        anuncios = consultar(fiat, trade_type)
        if anuncios is None:
            print(f"{fiat}: sin respuesta de Binance", file=sys.stderr)
            precios[fiat] = None
            continue
        precio = promedio(anuncios)
        precios[fiat] = precio
        estado = f"{precio:.4f}" if precio else "sin ofertas P2P"
        print(f"{fiat}: {estado}")

    ves = precios.get("VES")
    if not ves:
        # Sin el precio del bolívar no hay ninguna tasa que calcular. Salir sin
        # escribir preserva el último data/tasas.json bueno.
        print("ERROR: sin precio de USDT en VES; no se reescribe data/tasas.json",
              file=sys.stderr)
        return 1

    def tasa(local):
        return round(ves / local, 6) if local else None

    punto_actual = {
        "fecha": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ves_usdt": round(ves, 4),
        "tasas": {
            "USD": round(1 / ves, 8),
            "COP": tasa(precios.get("COP")),
            "ARS": tasa(precios.get("ARS")),
            "CLP": tasa(precios.get("CLP")),
            "MXN": tasa(precios.get("MXN")),
            "PEN": tasa(precios.get("PEN")),
            "EUR": tasa(precios.get("EUR")),
        },
    }

    # El histórico vive dentro del mismo JSON: se lee lo que ya había escrito
    # la corrida anterior y se le agrega el punto de ahora. Así el frontend
    # puede pintar la gráfica desde la primera carga, sin depender de que el
    # visitante deje la pestaña abierta varias horas.
    historial = []
    if os.path.exists(RUTA_SALIDA):
        try:
            with open(RUTA_SALIDA, "r", encoding="utf-8") as fh:
                historial = json.load(fh).get("historial", [])
        except (json.JSONDecodeError, OSError):
            historial = []
    historial.append(punto_actual)
    historial = historial[-MAX_HISTORIAL:]

    # Tasa oficial BCV: solo se scrapea dentro de la ventana horaria (~5am y
    # ~6pm hora Venezuela); en el resto de las corridas, o si bcv.org.ve no
    # responde, se conserva la última que sí se pudo leer (igual que el
    # historial), en vez de dejar el campo vacío por una caída puntual.
    hora_ve = (time.gmtime().tm_hour - 4) % 24
    bcv = bcv_oficial() if hora_ve in HORAS_BCV_VE else None
    if bcv is None and os.path.exists(RUTA_SALIDA):
        try:
            with open(RUTA_SALIDA, "r", encoding="utf-8") as fh:
                bcv = json.load(fh).get("bcv")
        except (json.JSONDecodeError, OSError):
            bcv = None

    salida = {
        **punto_actual,
        # null en vez de 0 cuando un mercado no tiene ofertas, para que el
        # frontend distinga "sin datos" de "vale cero".
        "precios_usdt": {
            f: (round(p, 4) if p else None)
            for f, p in precios.items()
        },
        "historial": historial,
        "bcv": bcv,
    }

    os.makedirs("data", exist_ok=True)
    with open(RUTA_SALIDA, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print("data/tasas.json actualizado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
