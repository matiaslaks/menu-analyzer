#!/usr/bin/env python3
"""
Rappi Colombia scraper.
Extrae men├║s de restaurantes en rappi.com.co para an├ílisis competitivo.
"""

import re
import time
import json
import threading
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

RAPPI_URL = "https://www.rappi.com.co"

CITIES = {
    "bogota":       "Bogot├í",
    "medellin":     "Medell├¡n",
    "cali":         "Cali",
    "barranquilla": "Barranquilla",
    "cartagena":    "Cartagena",
    "bucaramanga":  "Bucaramanga",
    "pereira":      "Pereira",
    "manizales":    "Manizales",
    "cucuta":       "C├║cuta",
    "ibague":       "Ibagu├⌐",
}

CATEGORY_MAP = {
    "entradas":    ["entrada", "entradas", "aperitivo", "para compartir", "starters", "picadas"],
    "principales": ["principal", "principales", "plato fuerte", "platos fuertes", "especialidad",
                    "especialidades", "almuerzo", "almuerzos", "ejecutivo"],
    "postres":     ["postre", "postres", "dulce", "dulces", "helado", "dessert", "torta"],
    "bebidas":     ["bebida", "bebidas", "jugo", "jugos", "gaseosa", "agua", "cerveza",
                    "licor", "vino", "drink", "drinks", "caf├⌐", "malteada", "batido"],
    "combos":      ["combo", "combos", "promoci├│n", "promo", "promos", "oferta", "especial",
                    "paquete", "menu del dia", "men├║ del d├¡a"],
    "adiciones":   ["adici├│n", "adiciones", "adicional", "adicionales", "extra", "extras",
                    "complemento", "acompa├▒amiento", "salsa", "topping"],
}

PROTEINS = ["pollo", "res", "cerdo", "carne", "pescado", "camar├│n", "langostino",
            "at├║n", "salm├│n", "tofu", "vegetariano", "vegano", "mixto", "pavo",
            "cordero", "costilla", "lomo", "pechuga", "chorizo", "chicharr├│n"]

MOST_ORDERED_KEYWORDS = [
    "m├ís pedido", "mas pedido", "most ordered", "lo m├ís pedido",
    "populares", "popular", "destacado", "destacados",
    "m├ís vendido", "mas vendido", "recomendado", "recomendados",
    "top ventas", "favorito", "favoritos",
]


def is_most_ordered_section(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in MOST_ORDERED_KEYWORDS)


# ΓöÇΓöÇ API parsing helpers ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

# Fields in Rappi's JSON that can indicate a popular item
POPULAR_API_FIELDS = {
    "tag", "tags", "badge", "badge_text", "label", "highlight",
    "is_popular", "is_featured", "is_recommended", "is_top",
    "most_ordered", "popular", "featured", "trending",
}
POPULAR_API_VALUES = {
    "popular", "m├ís pedido", "mas pedido", "most ordered", "top",
    "destacado", "recomendado", "favorito", "trending", "best seller",
    "more_sold", "most_sold", "top_seller",
}


def _is_popular_api_item(item_dict: dict) -> bool:
    """Check if a JSON object has popularity indicators."""
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
    """Extract price from various API field names."""
    for f in ["price", "precio", "value", "cost", "unit_price", "base_price", "real_price"]:
        val = item_dict.get(f)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def _get_api_name(item_dict: dict) -> str:
    """Extract name from various API field names."""
    for f in ["name", "nombre", "title", "product_name", "display_name"]:
        val = item_dict.get(f)
        if val and isinstance(val, str) and 2 < len(val) < 120:
            return val.strip()
    return ""


def _get_api_description(item_dict: dict) -> str:
    """Extract description from various API field names."""
    for f in ["description", "descripcion", "subtitle", "details", "short_description"]:
        val = item_dict.get(f)
        if val and isinstance(val, str):
            return val.strip()[:200]
    return ""


def _search_json_for_items(data, current_section: str = "", depth: int = 0):
    """
    Recursively search a JSON structure for menu items.
    Returns list of (item_dict, section_name, is_popular) tuples.
    """
    if depth > 10:
        return []

    results = []

    if isinstance(data, dict):
        # Try to detect a section name from this object
        section = current_section
        for k in ["name", "title", "section_name", "category_name", "label", "header"]:
            v = data.get(k)
            if v and isinstance(v, str) and 1 < len(v) < 80:
                section = v
                break

        # If this object itself looks like a menu item (has name + price), capture it
        name = _get_api_name(data)
        price = _get_api_price(data)
        if name and price > 0:
            is_pop = _is_popular_api_item(data) or is_most_ordered_section(section)
            results.append((data, section, is_pop))
            return results  # Don't recurse further into a menu item

        # Otherwise recurse into values
        for v in data.values():
            if isinstance(v, (dict, list)):
                results.extend(_search_json_for_items(v, section, depth + 1))

    elif isinstance(data, list) and data:
        # Check if this list looks like a menu items array
        first = data[0] if isinstance(data[0], dict) else None
        if first and _get_api_name(first) and _get_api_price(first) > 0:
            for item in data:
                if isinstance(item, dict):
                    is_pop = _is_popular_api_item(item) or is_most_ordered_section(current_section)
                    results.append((item, current_section, is_pop))
            return results

        # Otherwise recurse into list items
        for item in data:
            if isinstance(item, (dict, list)):
                results.extend(_search_json_for_items(item, current_section, depth + 1))

    return results


def parse_items_from_api_responses(api_responses: list, log=print) -> list:
    """Parse menu items from captured Rappi API responses."""
    all_raw = []
    for resp in api_responses:
        try:
            raw = _search_json_for_items(resp.get("data", {}))
            all_raw.extend(raw)
        except Exception as e:
            log(f"  ΓÜá Error parseando API response: {e}")

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
            name=name,
            price=price,
            description=description,
            proteins=proteins,
            raw_category=section,
            category=cat,
            is_most_ordered=is_pop,
        )
        menu_item.is_combo = menu_item.category == "combos"
        items.append(menu_item)
        if is_pop:
            popular_count += 1

    if popular_count > 0:
        log(f"  Γ£ô {popular_count} items populares encontrados via API")
    return items


# ΓöÇΓöÇ Data models ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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
            "name": self.name,
            "url": self.url,
            "city": self.city,
            "total_items": self.total_items,
            "avg_price": self.avg_price,
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
                {
                    "name": i.name, "price": i.price, "description": i.description,
                    "proteins": i.proteins,
                }
                for i in self.most_ordered_items
            ],
            "error": self.error,
        }


# ΓöÇΓöÇ Helpers ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

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
    # Colombian format: 12.900 or 12,900
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


# ΓöÇΓöÇ Browser helpers ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def wait(page: Page, ms: int = 1500):
    page.wait_for_timeout(ms)


def try_selector(page: Page, selectors: list, timeout: int = 3000):
    """Try multiple selectors, return first match or None."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el:
                return el
        except Exception:
            continue
    return None


def set_location(page: Page, city: str, log=print) -> bool:
    """Set delivery city on Rappi. Returns True if successful."""
    log(f"  Configurando ciudad: {city}...")

    # Wait for location prompt or already set
    location_selectors = [
        'input[placeholder*="direcci├│n" i]',
        'input[placeholder*="ciudad" i]',
        'input[placeholder*="Escribe" i]',
        'input[placeholder*="ingresa" i]',
        '[data-testid="address-input"]',
        'input[data-testid*="address"]',
        'input[data-testid*="location"]',
    ]

    inp = try_selector(page, location_selectors, timeout=6000)
    if not inp:
        log("  No se encontr├│ campo de ciudad (puede ya estar configurado)")
        return True

    inp.click()
    wait(page, 500)
    inp.fill(city)
    wait(page, 2000)

    # Click first autocomplete suggestion
    suggestion_selectors = [
        '[data-testid*="suggestion"]',
        '[data-testid*="address-result"]',
        'li[class*="suggestion" i]',
        'div[class*="autocomplete" i] li',
        'div[class*="Suggestion" i]',
        'div[class*="result" i] div',
    ]
    sugg = try_selector(page, suggestion_selectors, timeout=3000)
    if sugg:
        sugg.click()
        wait(page, 2000)
        log(f"  Γ£ô Ciudad configurada: {city}")
        return True

    # Fallback: press Enter
    inp.press('Enter')
    wait(page, 2000)
    log(f"  Γ£ô Ciudad ingresada (Enter): {city}")
    return True


def search_restaurant(page: Page, name: str, log=print) -> Optional[str]:
    """Search for a restaurant by name and return its URL."""
    log(f"  Buscando: {name}...")

    # Try search bar
    search_selectors = [
        'input[placeholder*="buscar" i]',
        'input[placeholder*="Buscar" i]',
        'input[placeholder*="restaurante" i]',
        'input[type="search"]',
        '[data-testid*="search"] input',
        'input[class*="search" i]',
    ]

    search_btn_selectors = [
        '[data-testid*="search-button"]',
        'button[aria-label*="buscar" i]',
        'button[class*="search" i]',
        'svg[class*="search" i]',
        'a[href*="search"]',
    ]

    # Try clicking search icon first
    btn = try_selector(page, search_btn_selectors, timeout=3000)
    if btn:
        btn.click()
        wait(page, 800)

    inp = try_selector(page, search_selectors, timeout=5000)
    if not inp:
        log("  ΓÜá No se encontr├│ barra de b├║squeda")
        return None

    inp.click()
    inp.fill(name)
    wait(page, 2500)

    # Look for restaurant results
    result_selectors = [
        '[data-testid*="store-card"]',
        '[data-testid*="restaurant-card"]',
        'div[class*="StoreCard" i]',
        'div[class*="store-card" i]',
        'a[href*="/restaurantes/"]',
        'div[class*="RestaurantCard" i]',
    ]

    # Try to find a result matching the name
    for sel in result_selectors:
        try:
            results = page.query_selector_all(sel)
            for r in results:
                text = (r.inner_text() or "").lower()
                if any(w.lower() in text for w in name.split() if len(w) > 3):
                    # Get the link
                    link = r.query_selector('a[href*="/restaurantes/"]')
                    if link:
                        href = link.get_attribute('href')
                        if href:
                            url = href if href.startswith('http') else RAPPI_URL + href
                            log(f"  Γ£ô Restaurante encontrado: {url}")
                            return url
                    # Maybe the element itself is a link
                    href = r.get_attribute('href')
                    if href and '/restaurantes/' in href:
                        url = href if href.startswith('http') else RAPPI_URL + href
                        log(f"  Γ£ô Restaurante encontrado: {url}")
                        return url
        except Exception:
            continue

    # If no match found, take the first result
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

    log(f"  Γ£ù No se encontraron resultados para: {name}")
    return None


def extract_menu(page: Page, restaurant_name: str, url: str, log=print) -> RestaurantMenu:
    """Navigate to restaurant page and extract full menu."""
    menu = RestaurantMenu(name=restaurant_name, url=url)

    # ΓöÇΓöÇ Strategy 1: Intercept Rappi API responses ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
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
            # Filter for Rappi API calls likely to contain menu/product data
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
    log(f"  Cargando men├║ de {restaurant_name} (intercepci├│n API activa)...")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        wait(page, 3000)

        # Scroll down to trigger lazy loading and additional API calls
        for _ in range(5):
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            wait(page, 800)
        page.evaluate("window.scrollTo(0, 0)")
        wait(page, 500)

    except Exception as e:
        menu.error = f"Error cargando p├ígina: {e}"
        log(f"  Γ£ù {menu.error}")
        page.remove_listener("response", on_response)
        return menu

    page.remove_listener("response", on_response)
    log(f"  Capturadas {len(api_responses)} respuestas de API de Rappi")

    # Try to extract from API responses first
    if api_responses:
        log("  Intentando extracci├│n via API...")
        items_from_api = parse_items_from_api_responses(api_responses, log)
        if len(items_from_api) >= 3:
            menu.items = items_from_api
            menu.raw_categories = list({i.raw_category for i in items_from_api if i.raw_category})
            popular = sum(1 for i in items_from_api if i.is_most_ordered)
            log(f"  Γ£ô {len(items_from_api)} items extra├¡dos via API ({popular} populares)")
            return menu
        log("  API no retorn├│ suficientes datos, usando extracci├│n HTML...")

    # ΓöÇΓöÇ Strategy 2: HTML scraping with popularity badge detection ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
    item_selectors = [
        '[data-testid*="product-card"]',
        '[data-testid*="item-card"]',
        'div[class*="ProductCard" i]',
        'div[class*="product-card" i]',
        'div[class*="MenuItem" i]',
        'div[class*="menu-item" i]',
        'li[class*="product" i]',
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

    # Strategy 3: Fallback ΓÇö extract all text with prices
    if len(items_found) < 3:
        log("  Usando estrategia de extracci├│n alternativa...")
        items_found = extract_items_fallback(page)

    # Assign categories + popularity based on section headers
    assign_categories(page, items_found, log)

    menu.items = items_found
    menu.raw_categories = list({i.raw_category for i in items_found if i.raw_category})

    popular = sum(1 for i in items_found if i.is_most_ordered)
    log(f"  Γ£ô {len(items_found)} items extra├¡dos de {restaurant_name} ({popular} populares)")
    return menu


def extract_item_from_card(card) -> Optional[MenuItem]:
    """Extract a MenuItem from a product card element."""
    try:
        text = card.inner_text() or ""
        if not text.strip():
            return None

        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if not lines:
            return None

        name = lines[0]

        # Find price in text
        price = 0.0
        price_text = ""
        price_pattern = re.compile(r'\$\s*[\d.,]+')
        for line in lines:
            match = price_pattern.search(line)
            if match:
                price_text = match.group()
                price = parse_price(price_text)
                break

        # Description: lines between name and price
        description = " ".join(
            l for l in lines[1:]
            if not price_pattern.search(l) and len(l) > 3
        )[:200]

        if len(name) < 2 or len(name) > 120:
            return None

        proteins = detect_proteins(name + " " + description)

        # Detect popularity badge in card HTML/text
        full_text_lower = text.lower()
        is_popular = is_most_ordered_section(full_text_lower)

        # Also check data attributes for popularity signals
        try:
            outer_html = card.evaluate("el => el.outerHTML") or ""
            outer_lower = outer_html.lower()
            if any(kw in outer_lower for kw in ["popular", "most_ordered", "top_seller", "m├ís pedido", "badge"]):
                is_popular = True
        except Exception:
            pass

        return MenuItem(
            name=name,
            price=price,
            description=description,
            proteins=proteins,
            is_most_ordered=is_popular,
        )
    except Exception:
        return None


def extract_items_fallback(page: Page) -> list:
    """Fallback: extract items by finding price patterns in page text."""
    items = []
    try:
        # Get all text nodes near prices
        data = page.evaluate("""() => {
            const results = [];
            const priceRe = /\\$\\s*[\\d.]{3,}/;

            // Walk all elements
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_ELEMENT
            );

            let node;
            while (node = walker.nextNode()) {
                const text = node.innerText || '';
                if (priceRe.test(text) && text.length < 500 && text.length > 5) {
                    const children = node.children;
                    if (children.length <= 5) {
                        results.push({
                            text: text.trim(),
                            tag: node.tagName,
                        });
                    }
                }
            }
            return results;
        }""")

        seen = set()
        for d in data:
            lines = [l.strip() for l in d['text'].split('\n') if l.strip()]
            if not lines:
                continue
            name = lines[0]
            if name in seen or len(name) < 3 or len(name) > 120:
                continue
            seen.add(name)

            price_match = re.search(r'\$\s*[\d.,]+', d['text'])
            price = parse_price(price_match.group()) if price_match else 0.0

            description = " ".join(
                l for l in lines[1:]
                if not re.search(r'\$\s*[\d.,]+', l) and len(l) > 3
            )[:200]

            proteins = detect_proteins(name + " " + description)
            items.append(MenuItem(name=name, price=price, description=description, proteins=proteins))

    except Exception as e:
        print(f"  Fallback extraction error: {e}")

    return items


def assign_categories(page: Page, items: list, log=print):
    """Try to assign categories to items based on page section headers."""
    try:
        data = page.evaluate("""() => {
            const sections = [];
            const headers = document.querySelectorAll('h2, h3, [class*="category" i], [class*="section-title" i]');
            headers.forEach(h => {
                const text = h.innerText?.trim();
                if (text && text.length > 1 && text.length < 80) {
                    const rect = h.getBoundingClientRect();
                    sections.push({ text, y: rect.top + window.scrollY });
                }
            });

            const products = [];
            const cards = document.querySelectorAll(
                '[data-testid*="product"], [data-testid*="item"], [class*="ProductCard" i], [class*="MenuItem" i]'
            );
            cards.forEach(c => {
                const text = c.innerText?.split('\\n')[0]?.trim();
                if (text) {
                    const rect = c.getBoundingClientRect();
                    products.push({ name: text, y: rect.top + window.scrollY });
                }
            });

            return { sections, products };
        }""")

        sections = sorted(data.get('sections', []), key=lambda x: x['y'])
        products = data.get('products', [])

        # Map each product to its nearest preceding section
        product_categories = {}
        for prod in products:
            best_section = None
            for sec in sections:
                if sec['y'] <= prod['y']:
                    best_section = sec['text']
                else:
                    break
            if best_section:
                product_categories[prod['name'].lower()] = best_section

        # Apply to items
        for item in items:
            key = item.name.lower()
            raw = product_categories.get(key, "")
            if raw:
                item.raw_category = raw
                item.category = classify_category(raw)
                item.is_combo = item.category == "combos"
                item.is_most_ordered = is_most_ordered_section(raw)
            else:
                # Classify by item name itself
                item.category = classify_category(item.name)
                item.is_combo = item.category == "combos"

        most_ordered_count = sum(1 for i in items if i.is_most_ordered)
        if most_ordered_count > 0:
            log(f"  Γ£ô {most_ordered_count} items identificados como 'm├ís pedidos'")

    except Exception as e:
        log(f"  ΓÜá No se pudieron asignar categor├¡as: {e}")
        # Classify by name as fallback
        for item in items:
            item.category = classify_category(item.name)
            item.is_combo = item.category == "combos"


# ΓöÇΓöÇ Main scraping function ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def scrape_restaurants(
    main_restaurant: str,
    competitors: list,
    city: str = "Bogot├í",
    log=print,
) -> list:
    """
    Scrape Rappi menus for main restaurant + competitors.
    Returns list of RestaurantMenu objects.
    """
    all_names = [main_restaurant] + competitors
    results = []

    with sync_playwright() as p:
        log("Iniciando navegador...")
        import os as _os
        _headless = _os.environ.get("HEADLESS", "true").lower() != "false"
        browser = p.chromium.launch(
            headless=_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ] + (["--start-maximized"] if not _headless else []),
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="es-CO",
        )
        page = context.new_page()

        try:
            # Open Rappi
            log("Abriendo rappi.com.co...")
            page.goto(RAPPI_URL, wait_until="domcontentloaded", timeout=30000)
            wait(page, 2000)

            # Set city
            set_location(page, city, log)
            wait(page, 1000)

            # Scrape each restaurant
            for name in all_names:
                log(f"\n{'ΓöÇ'*40}")
                log(f"Procesando: {name}")

                # Go back to main page to search
                page.goto(RAPPI_URL, wait_until="domcontentloaded", timeout=20000)
                wait(page, 1500)

                url = search_restaurant(page, name, log)
                if not url:
                    results.append(RestaurantMenu(
                        name=name,
                        city=city,
                        error=f"No se encontr├│ '{name}' en Rappi {city}",
                    ))
                    continue

                menu = extract_menu(page, name, url, log)
                menu.city = city
                results.append(menu)
                wait(page, 1000)  # Polite delay

        except Exception as e:
            log(f"Error general: {e}")
        finally:
            browser.close()
            log("\nNavegador cerrado.")

    return results
