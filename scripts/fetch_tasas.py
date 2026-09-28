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
import sys
import time
import urllib.error
import urllib.request

URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

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

    salida = {
        **punto_actual,
        # null en vez de 0 cuando un mercado no tiene ofertas, para que el
        # frontend distinga "sin datos" de "vale cero".
        "precios_usdt": {
            f: (round(p, 4) if p else None)
            for f, p in precios.items()
        },
        "historial": historial,
    }

    os.makedirs("data", exist_ok=True)
    with open(RUTA_SALIDA, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print("data/tasas.json actualizado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
