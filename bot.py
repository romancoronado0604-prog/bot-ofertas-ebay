import base64
import json
import os
import re
import sys
import urllib.parse

import requests

APP_ID = os.environ["EBAY_APP_ID"]
CERT_ID = os.environ["EBAY_CERT_ID"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen.json"
LAPTOP_CATEGORY = "111422"  # PC Laptops & Netbooks (incluye MacBooks)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=30,
    )
    if not r.ok:
        print("Error Telegram:", r.status_code, r.text[:200])


def get_token():
    basic = base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )
    if not r.ok:
        print("Error al obtener token de eBay:", r.status_code, r.text[:300])
        sys.exit(1)
    return r.json()["access_token"]


def search(token, p):
    params = {
        "q": p["query"],
        "category_ids": LAPTOP_CATEGORY,
        "sort": "price",
        "limit": 100,
        "filter": (
            f"price:[{p['min_price']}..{p['max_price']}],"
            "priceCurrency:USD,"
            "buyingOptions:{FIXED_PRICE},"
            "deliveryCountry:US"
        ),
    }
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params=params,
        timeout=30,
    )
    if not r.ok:
        print("Error de búsqueda:", r.status_code, r.text[:300])
        return []
    return r.json().get("itemSummaries", [])


def shipping_cost(item):
    costs = []
    for o in item.get("shippingOptions", []) or []:
        try:
            costs.append(float(o["shippingCost"]["value"]))
        except (KeyError, TypeError, ValueError):
            pass
    return min(costs) if costs else None


def get_item_text(token, item_id):
    """Devuelve el texto de descripción y nota de condición del anuncio."""
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item/" + urllib.parse.quote(item_id, safe=""),
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        timeout=30,
    )
    if not r.ok:
        return None
    d = r.json()
    raw = " ".join(
        [
            d.get("conditionDescription", "") or "",
            d.get("shortDescription", "") or "",
            d.get("description", "") or "",
        ]
    )
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).lower()


def matches(title, p, global_exclude):
    t = title.lower()
    for pattern in p["must_match"]:
        if not re.search(pattern, t):
            return False
    for word in p.get("exclude", []) + global_exclude:
        if word.lower() in t:
            return False
    return True


def main():
    cfg = load_json("products.json", None)
    if cfg is None:
        print("No se encontró products.json")
        sys.exit(1)

    first_run = not os.path.exists(SEEN_FILE)
    seen = load_json(SEEN_FILE, {})
    min_pct = cfg["settings"]["min_feedback_percent"]
    min_score = cfg["settings"]["min_feedback_score"]

    token = get_token()
    new_alerts = 0

    for p in cfg["products"]:
        items = search(token, p)
        print(f"{p['name']}: {len(items)} resultados")

        for item in items:
            title = item.get("title", "")
            if not matches(title, p, cfg["global_exclude"]):
                continue

            condition = item.get("condition", "") or ""
            if "parts" in condition.lower():
                continue

            seller = item.get("seller", {}) or {}
            try:
                pct = float(seller.get("feedbackPercentage", 0))
            except ValueError:
                pct = 0
            score = int(seller.get("feedbackScore", 0) or 0)
            if pct < min_pct or score < min_score:
                continue

            try:
                price = float(item["price"]["value"])
            except (KeyError, ValueError):
                continue
            ship = shipping_cost(item)
            total = price + (ship or 0)
            if total > p["max_price"]:
                continue

            item_id = item["itemId"]
            if item_id in seen and total >= seen[item_id] - 1:
                continue  # ya avisado y sin bajada de precio

            # Revisión profunda: descripción completa del anuncio
            text = get_item_text(token, item_id)
            if text is None:
                continue  # sin poder verificar, no se avisa
            if any(re.search(rx, text) for rx in cfg["description_exclude_regex"]):
                seen[item_id] = 0  # descartado, no volver a revisarlo
                continue

            seen[item_id] = total
            new_alerts += 1
            ship_txt = (
                "gratis" if ship == 0
                else f"${ship:.2f}" if ship is not None
                else "por confirmar"
            )
            telegram(
                f"🔔 OFERTA: {p['name']}\n\n"
                f"{title}\n"
                f"Precio: ${price:.2f}\n"
                f"Envío a EE.UU.: {ship_txt}\n"
                f"Total aprox.: ${total:.2f} (tu límite: ${p['max_price']})\n"
                f"Condición: {condition or 'no indicada'}\n"
                f"Vendedor: {seller.get('username', '?')} "
                f"({pct}% positivo, {score} calificaciones)\n\n"
                f"{item.get('itemWebUrl', '')}"
            )

    if first_run:
        telegram(
            "✅ Bot activo. Voy a revisar eBay cada ~10 minutos "
            "y te aviso cuando aparezca una oferta dentro de tus límites."
        )

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    print(f"Alertas nuevas: {new_alerts}")


if __name__ == "__main__":
    main()
