#!/usr/bin/env python3
"""
Rappi Colombia scraper.
Extrae menús de restaurantes en rappi.com.co para análisis competitivo.
"""

import re
import time
import json
import shutil
import threading
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

RAPPI_URL = "https://www.rappi.com.co"

CITIES = {
    "bogota":       "Bogotá",
    "medellin":     "Medellín",
    "cali":         "Cali",
    "barranquilla": "Barranquilla",
    "cartagena":    "Cartagena",
    "bucaramanga":  "Bucaramanga",
    "pereira":      "Pereira",
    "manizales":    "Manizales",
    "cucuta":       "Cúcuta",
    "ibague":       "Ibagué",
}

CATEGORY_MAP = {
    "entradas":    ["entrada", "entradas", "aperitivo", "para compartir", "starters", "picadas"],
    "principales": ["principal", "principales", "plato fuerte", "platos fuertes", "especialidad",
                    "especialidades", "almuerzo", "almuerzos", "ejecutivo"],
    "postres":     ["postre", "postres", "dulce", "dulces", "helado", "dessert", "torta"],
    "bebidas":     ["bebida", "bebidas", "jugo", "jugos", "gaseosa", "agua", "cerveza",
                    "licor", "vino", "drink", "drinks", "café", "malteada", "batido"],
    "combos":      ["combo", "combos", "promoción", "promo", "promos", "oferta", "especial",
                    "paquete", "menu del dia", "menú del día"],
    "adiciones":   ["adición", "adiciones", "adicional", "adicionales", "extra", "extras",
                    "complemento", "acompañamiento", "salsa", "topping"],
}

PROTEINS = ["pollo", "res", "cerdo", "carne", "pescado", "camarón", "langostino",
            "atún", "salmón", "tofu", "vegetariano", "vegano", "mixto", "pavo",
            "cordero", "costilla", "lomo", "pechuga", "chorizo", "chicharrón"]

MOST_ORDERED_KEYWORDS = [
    "más pedido", "mas pedido", "most ordered", "lo más pedido",
    "populares", "popular", "destacado", "destacados",
    "más vendido", "mas vendido", "recomendado", "recomendados",
    "top ventas", "favorito", "favoritos",
]


def is_most_ordered_section(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in MOST_ORDERED_KEYWORDS)


POPULAR_API_FIELDS = {
    "tag", "tags", "badge", "badge_text", "label", "highlight",
    "is_popular", "is_featured", "is_recommended", "is_top",
    "most_ordered", "popular", "featured", "trending",
}
POPULAR_API_VALUES = {
    "popular", "más pedido", "mas pedido", "most ordered", "top",
    "destacado", "recomendado", "favorito", "trending", "best seller",
    "more_sold", "most_sold", "top_seller",
}


def _is_popular_api_item(item_dict: dict) -> bool:
    for field_name in POPULAR_API_FIELDS:
        val = item_dict.get(field_name)
        if val is True:
            return True
        if isinstance(val, str) and val.lower() in POPULAR_API_VALUES:
            return True
        if isinstance(val, list):
            for v in val:
                if isinstance(v, str) and v.lower() in POPULAR_API_VALUES:
                    return True
    return False


def _get_api_price(item_dict: dict) -> float:
    for f in ["price", "precio", "value", "cost", "unit_price", "base_price", "real_price"]:
        val = item_dict.get(f)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def _get_api_name(item_dict: dict) -> str:
    for f in ["name", "nombre", "title", "product_name", "display_name"]:
        val = item_dict.get(f)
        if val and isinstance(val, str) and 2 < len(val) < 120:
            return val.strip()
    return ""


def _get_api_description(item_dict: dict) -> str:
    for f in ["description", "descripcion", "subtitle", "details", "short_description"]:
        val = item_dict.get(f)
        if val and isinstance(val, str):
            return val.strip()[:200]
    return ""


def _search_json_for_items(data, current_section: str = "", depth: int = 0):
    if depth > 10:
        return []
    results = []
    if isinstance(data, dict):
        section = current_section
        for k in ["name", "title", "section_name", "category_name", "label", "header"]:
            v = data.get(k)
            if v and isinstance(v, str) and 1 < len(v) < 80:
                section = v
                break
        name = _get_api_name(data)
        price = _get_api_price(data)
        if name and price > 0:
            is_pop = _is_popular_api_item(data) or is_most_ordered_section(section)
            results.append((data, section, is_pop))
            return results
        for v in data.values():
            if isinstance(v, (dict, list)):
                results.extend(_search_json_for_items(v, section, depth + 1))
    elif isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else None
        if first and _get_api_name(first) and _get_api_price(first) > 0:
            for item in data:
                if isinstance(item, dict):
                    is_pop = _is_popular_api_item(item) or is_most_ordered_section(current_section)
                    results.append((item, current_section, is_pop))
            return results
        for item in data:
            if isinstance(item, (dict, list)):
                results.extend(_search_json_for_items(item, current_section, depth + 1))
    return results


def parse_items_from_api_responses(api_responses: list, log=print) -> list:
    all_raw = []
    for resp in api_responses:
        try:
            raw = _search_json_for_items(resp.get("data", {}))
            all_raw.extend(raw)
        except Exception as e:
            log(f"  ⚠ Error parseando API response: {e}")
    if not all_raw:
        return []
    seen = set()
    items = []
    popular_count = 0
    for item_dict, section, is_pop in all_raw:
        name = _get_api_name(item_dict)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        price = _get_api_price(item_dict)
        description = _get_api_description(item_dict)
        proteins = detect_proteins(name + " " + description)
        cat = classify_category(section) if section else classify_category(name)
        menu_item = MenuItem(
            name=name, price=price, description=description,
            proteins=proteins, raw_category=section, category=cat, is_most_ordered=is_pop,
        )
        menu_item.is_combo = menu_item.category == "combos"
        items.append(menu_item)
        if is_pop:
            popular_count += 1
    if popular_count > 0:
        log(f"  ✓ {popular_count} items populares encontrados via API")
    return items


@dataclass
class MenuItem:
    name: str
    price: float = 0.0
    description: str = ""
    category: str = "otros"
    raw_category: str = ""
    is_combo: bool = False
    is_most_ordered: bool = False
    proteins: list = field(default_factory=list)


@dataclass
class RestaurantMenu:
    name: str
    url: str = ""
    city: str = ""
    items: list = field(default_factory=list)
    raw_categories: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def total_items(self): return len(self.items)

    @property
    def combo_count(self): return sum(1 for i in self.items if i.is_combo)

    @property
    def most_ordered_items(self): return [i for i in self.items if i.is_most_ordered]

    @property
    def avg_price(self):
        prices = [i.price for i in self.items if i.price > 0]
        return round(sum(prices) / len(prices), 0) if prices else 0

    @property
    def by_category(self):
        result = {}
        for item in self.items:
            result.setdefault(item.category, []).append(item)
        return result

    @property
    def by_protein(self):
        result = {}
        for item in self.items:
            for p in item.proteins:
                result[p] = result.get(p, 0) + 1
        return result

    def to_dict(self):
        return {
            "name": self.name, "url": self.url, "city": self.city,
            "total_items": self.total_items, "avg_price": self.avg_price,
            "combo_count": self.combo_count,
            "by_category": {cat: len(items) for cat, items in self.by_category.items()},
            "by_protein": self.by_protein,
            "raw_categories": self.raw_categories,
            "items": [
                {
                    "name": i.name, "price": i.price, "description": i.description,
                    "category": i.category, "raw_category": i.raw_category,
                    "is_combo": i.is_combo, "is_most_ordered": i.is_most_ordered,
                    "proteins": i.proteins,
                }
                for i in self.items
            ],
            "most_ordered_items": [
                {"name": i.name, "price": i.price, "description": i.description, "proteins": i.proteins}
                for i in self.most_ordered_items
            ],
            "error": self.error,
        }


def classify_category(raw: str) -> str:
    raw_lower = raw.lower()
    for normalized, keywords in CATEGORY_MAP.items():
        if any(kw in raw_lower for kw in keywords):
            return normalized
    return "otros"


def detect_proteins(text: str) -> list:
    t = text.lower()
    return [p for p in PROTEINS if p in t]


def parse_price(text: str) -> float:
    if not text:
        return 0.0
    cleaned = re.sub(r'[^\d.,]', '', text)
    if not cleaned:
        return 0.0
    if '.' in cleaned:
        parts = cleaned.split('.')
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace('.', '')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def wait(page: Page, ms: int = 1500):
    page.wait_for_timeout(ms)


def try_selector(page: Page, selectors: list, timeout: int = 3000):
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el:
                return el
        except Exception:
            continue
    return None


def set_location(page: Page, city: str, log=print) -> bool:
    log(f"  Configurando ciudad: {city}...")
    location_selectors = [
        'input[placeholder*="dirección" i]', 'input[placeholder*="ciudad" i]',
        'input[placeholder*="Escribe" i]', 'input[placeholder*="ingresa" i]',
        '[data-testid="address-input"]', 'input[data-testid*="address"]',
        'input[data-testid*="location"]',
    ]
    inp = try_selector(page, location_selectors, timeout=6000)
    if not inp:
        log("  No se encontró campo de ciudad (puede ya estar configurado)")
        return True
    inp.click()
    wait(page, 500)
    inp.fill(city)
    wait(page, 2000)
    suggestion_selectors = [
        '[data-testid*="suggestion"]', '[data-testid*="address-result"]',
        'li[class*="suggestion" i]', 'div[class*="autocomplete" i] li',
        'div[class*="Suggestion" i]', 'div[class*="result" i] div',
    ]
    sugg = try_selector(page, suggestion_selectors, timeout=3000)
    if sugg:
        sugg.click()
        wait(page, 2000)
        log(f"  ✓ Ciudad configurada: {city}")
        return True
    inp.press('Enter')
    wait(page, 2000)
    log(f"  ✓ Ciudad ingresada (Enter): {city}")
    return True


def search_restaurant(page: Page, name: str, log=print) -> Optional[str]:
    log(f"  Buscando: {name}...")
    search_selectors = [
        'input[placeholder*="buscar" i]', 'input[placeholder*="Buscar" i]',
        'input[placeholder*="restaurante" i]', 'input[type="search"]',
        '[data-testid*="search"] input', 'input[class*="search" i]',
    ]
    search_btn_selectors = [
        '[data-testid*="search-button"]', 'button[aria-label*="buscar" i]',
        'button[class*="search" i]', 'svg[class*="search" i]', 'a[href*="search"]',
    ]
    btn = try_selector(page, search_btn_selectors, timeout=3000)
    if btn:
        btn.click()
        wait(page, 800)
    inp = try_selector(page, search_selectors, timeout=5000)
    if not inp:
        log("  ⚠ No se encontró barra de búsqueda")
        return None
    inp.click()
    inp.fill(name)
    wait(page, 2500)
    result_selectors = [
        '[data-testid*="store-card"]', '[data-testid*="restaurant-card"]',
        'div[class*="StoreCard" i]', 'div[class*="store-card" i]',
        'a[href*="/restaurantes/"]', 'div[class*="RestaurantCard" i]',
    ]
    for sel in result_selectors:
        try:
            results = page.query_selector_all(sel)
            for r in results:
                text = (r.inner_text() or "").lower()
                if any(w.lower() in text for w in name.split() if len(w) > 3):
                    link = r.query_selector('a[href*="/restaurantes/"]')
                    if link:
                        href = link.get_attribute('href')
                        if href:
                            url = href if href.startswith('http') else RAPPI_URL + href
                            log(f"  ✓ Restaurante encontrado: {url}")
                            return url
                    href = r.get_attribute('href')
                    if href and '/restaurantes/' in href:
                        url = href if href.startswith('http') else RAPPI_URL + href
                        log(f"  ✓ Restaurante encontrado: {url}")
                        return url
        except Exception:
            continue
    for sel in result_selectors:
        try:
            r = page.query_selector(sel)
            if r:
                link = r.query_selector('a[href*="/restaurantes/"]') or r
                href = link.get_attribute('href')
                if href and '/restaurantes/' in href:
                    url = href if href.startswith('http') else RAPPI_URL + href
                    log(f"  ~ Primer resultado (sin coincidencia exacta): {url}")
                    return url
        except Exception:
            continue
    log(f"  ✗ No se encontraron resultados para: {name}")
    return None


def extract_menu(page: Page, restaurant_name: str, url: str, log=print) -> RestaurantMenu:
    menu = RestaurantMenu(name=restaurant_name, url=url)
    api_responses = []
    api_lock = threading.Lock()

    def on_response(response):
        try:
            if response.status != 200:
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            resp_url = response.url.lower()
            api_keywords = ["restaurant", "store", "menu", "product", "catalog",
                            "item", "pax", "microservice", "aliclik", "dynamic"]
            if not any(kw in resp_url for kw in api_keywords):
                return
            try:
                data = response.json()
                with api_lock:
                    api_responses.append({"url": response.url, "data": data})
            except Exception:
                pass
        except Exception:
            pass

    page.on("response", on_response)
    log(f"  Cargando menú de {restaurant_name} (intercepción API activa)...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        wait(page, 3000)
        for _ in range(5):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            wait(page, 800)
        page.evaluate("window.scrollTo(0, 0)")
        wait(page, 500)
    except Exception as e:
        menu.error = f"Error cargando página: {e}"
        log(f"  ✗ {menu.error}")
        page.remove_listener("response", on_response)
        return menu

    page.remove_listener("response", on_response)
    log(f"  Capturadas {len(api_responses)} respuestas de API de Rappi")

    if api_responses:
        log("  Intentando extracción via API...")
        items_from_api = parse_items_from_api_responses(api_responses, log)
        if len(items_from_api) >= 3:
            menu.items = items_from_api
            menu.raw_categories = list({i.raw_category for i in items_from_api if i.raw_category})
            popular = sum(1 for i in items_from_api if i.is_most_ordered)
            log(f"  ✓ {len(items_from_api)} items extraídos via API ({popular} populares)")
            return menu
        log("  API no retornó suficientes datos, usando extracción HTML...")

    item_selectors = [
        '[data-testid*="product-card"]', '[data-testid*="item-card"]',
        'div[class*="ProductCard" i]', 'div[class*="product-card" i]',
        'div[class*="MenuItem" i]', 'div[class*="menu-item" i]', 'li[class*="product" i]',
    ]
    items_found = []
    for sel in item_selectors:
        try:
            cards = page.query_selector_all(sel)
            if len(cards) > 2:
                log(f"  Encontrados {len(cards)} productos con selector: {sel}")
                for card in cards:
                    item = extract_item_from_card(card)
                    if item:
                        items_found.append(item)
                break
        except Exception:
            continue

    if len(items_found) < 3:
        log("  Usando estrategia de extracción alternativa...")
        items_found = extract_items_fallback(page)

    assign_categories(page, items_found, log)
    menu.items = items_found
    menu.raw_categories = list({i.raw_category for i in items_found if i.raw_category})
    popular = sum(1 for i in items_found if i.is_most_ordered)
    log(f"  ✓ {len(items_found)} items extraídos de {restaurant_name} ({popular} populares)")
    return menu


def extract_item_from_card(card) -> Optional[MenuItem]:
    try:
        text = card.inner_text() or ""
        if not text.strip():
            return None
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if not lines:
            return None
        name = lines[0]
        price = 0.0
        price_pattern = re.compile(r'\$\s*[\d.,]+')
        for line in lines: