#!/usr/bin/env python3
"""
Menu Analyzer - Web server with drag & drop UI.

Usage:
    python server.py
"""

import base64
import json
from pathlib import Path
from typing import Generator

import anthropic
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import MenuExtraction, MODEL
from rappi_scraper import scrape_restaurants, CITIES

app = FastAPI(title="Menu Analyzer")
app.mount("/static", StaticFiles(directory="static"), name="static")

client = anthropic.Anthropic()

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# Tokens reservados para el thinking interno de Claude
THINKING_BUDGET = 8000
# Max tokens totales
MAX_TOKENS = 16000


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


def build_image_block(data: bytes, content_type: str) -> dict:
    b64 = base64.standard_b64encode(data).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": content_type, "data": b64},
    }


@app.post("/analyze")
def analyze(files: list[UploadFile] = File(...)):
    """Accept image uploads and return an SSE stream with analysis."""

    # Read all uploaded bytes before the generator starts
    image_payloads: list[tuple[bytes, str]] = []
    for upload in files:
        ct = upload.content_type or "image/jpeg"
        if ct not in ALLOWED_TYPES:
            continue
        image_payloads.append((upload.file.read(), ct))

    def event_stream() -> Generator[str, None, None]:
        # ── Step 1: structured extraction ───────────────────────────────────
        content = [build_image_block(d, ct) for d, ct in image_payloads]
        content.append({
            "type": "text",
            "text": (
                "Analyze this restaurant menu image carefully. "
                "Extract ALL menu items, their prices, descriptions, and categories. "
                "Note which items have a food photo accompanying them. "
                "If prices are not visible, set price to null. "
                "Identify the currency used. "
                "Provide observations about the menu's overall structure and quality."
            ),
        })

        try:
            response = client.messages.parse(
                model=MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
                output_format=MenuExtraction,
            )
            extraction: MenuExtraction = response.parsed_output
            yield f"event: extraction\ndata: {extraction.model_dump_json()}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(f'Error en extracción: {str(e)}')}\n\n"
            return

        # ── Step 2: streaming recommendations ───────────────────────────────
        menu_summary = (
            f"Restaurant: {extraction.restaurant_name or 'Unknown'}\n"
            f"Cuisine: {extraction.cuisine_type or 'Unknown'}\n"
            f"Total items: {extraction.total_items_found}\n"
            f"Price range: {extraction.price_range_min} - {extraction.price_range_max} "
            f"{extraction.currency or ''}\n"
            f"Items with photos: "
            f"{sum(1 for i in extraction.items if i.has_photo)}/{len(extraction.items)}\n"
            f"Language: {extraction.menu_language}\n\nMenu items:\n"
        ) + "\n".join(
            f"- {item.name} | {item.category or 'N/A'} | "
            f"{item.currency or ''}{item.price or 'N/A'} | "
            f"{'has photo' if item.has_photo else 'no photo'} | "
            f"Desc: {item.description or 'none'}"
            for item in extraction.items
        )

        rec_content = [build_image_block(d, ct) for d, ct in image_payloads]
        rec_content.append({
            "type": "text",
            "text": f"""You are an expert restaurant consultant with deep knowledge of menu engineering, pricing strategy, and food photography.

Here is the extracted menu data:
{menu_summary}

Please provide a comprehensive analysis in Spanish with the following sections:

## 1. 💰 Análisis de Pricing Competitivo
- Evaluación de la estrategia de precios actual
- Comparativa con estándares de mercado para este tipo de cocina
- Items con precios que parecen fuera de rango (muy caros o muy baratos)
- Recomendaciones específicas de ajuste de precios

## 2. 🎯 Ideas de Promociones
- 3-5 promociones concretas adaptadas al menú actual
- Happy hours, combos, menús del día o especiales sugeridos
- Estrategias para aumentar el ticket promedio

## 3. ✍️ Mejoras en Descripciones
- Evalúa las descripciones actuales
- Identifica los items con descripciones pobres o inexistentes
- Proporciona 3-5 ejemplos de cómo reescribir descripciones para hacerlas más atractivas
- Usa técnicas de copywriting gastronómico (ingredientes premium, técnicas de cocción, origen)

## 4. 📸 Feedback sobre Fotos de los Platos
- Evalúa la calidad y uso de fotografías en el menú
- Items que definitivamente necesitan foto y por qué
- Consejos específicos de fotografía gastronómica para este menú
- Recomendaciones sobre estilo visual (ángulos, iluminación, composición)

## 5. 🏆 Resumen Ejecutivo
- Top 3 cambios prioritarios con mayor impacto
- Potencial estimado de mejora en ventas
""",
        })

        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": rec_content}],
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            chunk = json.dumps(event.delta.text)
                            yield f"event: text\ndata: {chunk}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(f'Error en análisis IA: {str(e)}')}\n\n"
            return

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Rappi comparison ────────────────────────────────────────────────────────

class RappiRequest(BaseModel):
    main_restaurant: str
    competitors: list[str]
    city: str = "Bogotá"


@app.post("/rappi-compare")
def rappi_compare(req: RappiRequest):
    """Scrape Rappi and return SSE stream with progress + AI analysis."""

    def event_stream() -> Generator[str, None, None]:
        collected_logs = []

        def collect_log(msg: str):
            collected_logs.append(msg)

        yield f"event: log\ndata: {json.dumps('Iniciando scraping en Rappi...')}\n\n"

        # ── Scraping phase ───────────────────────────────────────────────────
        try:
            menus = scrape_restaurants(
                main_restaurant=req.main_restaurant,
                competitors=req.competitors,
                city=req.city,
                log=collect_log,
            )
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(f'Error en scraping: {str(e)}')}\n\n"
            return

        # Stream collected logs
        for msg in collected_logs:
            yield f"event: log\ndata: {json.dumps(msg)}\n\n"

        # Send scraped data
        menus_data = [m.to_dict() for m in menus]
        yield f"event: menus\ndata: {json.dumps(menus_data)}\n\n"

        # ── AI analysis phase ────────────────────────────────────────────────
        yield f"event: log\ndata: {json.dumps('Analizando con IA...')}\n\n"

        # Build comparison summary for Claude
        def format_most_ordered(items: list) -> str:
            if not items:
                return "  (No se detectó sección 'más pedidos' en Rappi)"
            lines = []
            for i in items:
                proteins_str = ", ".join(i.get("proteins", [])) or "no identificada"
                price_str = f"${i['price']:,.0f} COP" if i.get("price") else "precio N/A"
                desc = (i.get("description") or "")[:80]
                lines.append(f"  • {i['name']} | {price_str} | Proteína: {proteins_str} | {desc}")
            return "\n".join(lines)

        def menu_summary(m: dict) -> str:
            cats = m.get("by_category", {})
            proteins = m.get("by_protein", {})
            most_ordered = m.get("most_ordered_items", [])
            return f"""
Restaurante: {m['name']}
- Total items: {m['total_items']}
- Precio promedio: ${m['avg_price']:,.0f} COP
- Combos: {m['combo_count']}
- Entradas: {cats.get('entradas', 0)}
- Principales: {cats.get('principales', 0)}
- Postres: {cats.get('postres', 0)}
- Bebidas: {cats.get('bebidas', 0)}
- Adiciones: {cats.get('adiciones', 0)}
- Otros: {cats.get('otros', 0)}
- Platos por proteína: {', '.join(f'{p}: {n}' for p, n in proteins.items()) or 'N/A'}
- Categorías del menú: {', '.join(m.get('raw_categories', [])) or 'N/A'}
- Items más pedidos ({len(most_ordered)}):
{format_most_ordered(most_ordered)}
- Error: {m.get('error') or 'ninguno'}
""".strip()

        all_summaries = "\n\n".join(menu_summary(m) for m in menus_data)
        main_name = req.main_restaurant
        competitor_names = ", ".join(req.competitors)

        main_menu_data = next(
            (m for m in menus_data if m['name'].lower() == main_name.lower()),
            menus_data[0] if menus_data else {}
        )
        competitor_menus_data = [m for m in menus_data if m != main_menu_data]

        # Build full item lists for dish-by-dish comparison
        def format_items_list(m: dict, max_items: int = 60) -> str:
            items = m.get("items", [])[:max_items]
            if not items:
                return "  (sin items)"
            lines = []
            for it in items:
                price_str = f"${it['price']:,.0f}" if it.get("price") else "N/A"
                proteins = ", ".join(it.get("proteins", [])) or "—"
                desc = (it.get("description") or "")[:60]
                lines.append(f"  • {it['name']} | {price_str} COP | Proteína: {proteins} | {desc}")
            return "\n".join(lines)

        main_items_text = format_items_list(main_menu_data)
        competitor_items_text = "\n\n".join(
            f"### {m['name']}\n{format_items_list(m)}" for m in competitor_menus_data
        )

        prompt = f"""Eres un consultor experto en restaurantes con amplio conocimiento de Rappi Colombia, nutrición y estrategia de menús.

Se analizaron los siguientes restaurantes en Rappi {req.city}:

{all_summaries}

El restaurante PRINCIPAL es: **{main_name}**
Los COMPETIDORES son: {competitor_names}

---

LISTADO COMPLETO DE PLATOS — {main_name}:
{main_items_text}

LISTADO COMPLETO DE PLATOS — COMPETIDORES:
{competitor_items_text}

---

Por favor genera un análisis completo en español con estas secciones:

## 1. 📊 Tabla Comparativa General
Tabla con todos los restaurantes comparando: total items, precio promedio, combos, entradas, principales, postres, bebidas, adiciones, variedad de proteínas.

## 2. 🍽️ Comparación Plato a Plato
Para cada plato de **{main_name}**, busca el plato más similar en cada competidor.
Usa EXACTAMENTE estas columnas cortas (sin renombrarlas):

| Plato | Precio | Prot. | Kcal | Similar (competidor) | $ comp. | Dif. | Acción |
|---|---|---|---|---|---|---|---|

- **Plato**: nombre del plato de {main_name}
- **Precio**: precio COP (solo número, ej: 22.000)
- **Prot.**: proteína principal (1 palabra)
- **Kcal**: calorías estimadas (solo número, ej: 450)
- **Similar**: nombre corto del plato más parecido + (restaurante)
- **$ comp.**: precio del plato competidor
- **Dif.**: diferencia de precio (ej: +15% o -8%)
- **Acción**: máximo 6 palabras de recomendación

Incluye TODOS los platos. Si no hay equivalente: "Sin equivalente" en Similar.

## 3. 🔥 Más Pedidos de {main_name}
Tabla con columnas cortas:
| Plato | Precio | Prot. | Kcal | Nutrición | vs Mercado |
- Nutrición: ⭐ Excelente / ✅ Bueno / ⚠️ Regular / ❌ Mejorable
- vs Mercado: ↑ caro / ✓ justo / ↓ barato

## 4. 💰 Posicionamiento de Precios
- ¿Cómo está {main_name} vs competencia por categoría?
- Platos donde el precio es oportunidad de mejora (muy caro o muy barato vs mercado)
- Recomendaciones concretas de ajuste

## 5. 🎯 Oportunidades de Combos
- Combos que la competencia ofrece y {main_name} no
- Propuesta de 3 combos específicos usando los platos más pedidos

## 6. 🏆 Plan de Acción — Top 5 Prioridades
Acciones concretas ordenadas por impacto, con métricas de éxito.
"""

        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield f"event: text\ndata: {json.dumps(event.delta.text)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps(f'Error en análisis IA: {str(e)}')}\n\n"
            return

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/suggest-restaurants")
async def suggest_restaurants(q: str, city: str = "Bogotá"):
    """Search Rappi for restaurant name suggestions."""
    if len(q.strip()) < 2:
        return {"suggestions": []}

    from playwright.async_api import async_playwright

    suggestions = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="es-CO",
            )
            page = await context.new_page()
            await page.goto(f"https://www.rappi.com.co/restaurantes?query={q}", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)

            seen = set()
            elements = await page.query_selector_all('a[href*="/restaurantes/"]')
            for el in elements:
                try:
                    text = (await el.inner_text()).split("\n")[0].strip()
                    href = await el.get_attribute("href") or ""
                    if text and len(text) > 1 and "/restaurantes/" in href and text not in seen:
                        seen.add(text)
                        suggestions.append(text)
                        if len(suggestions) >= 6:
                            break
                except Exception:
                    continue

            await browser.close()
    except Exception:
        pass

    return {"suggestions": suggestions}


class PDFRequest(BaseModel):
    html: str
    filename: str = "reporte.pdf"


@app.post("/generate-pdf")
async def generate_pdf(req: PDFRequest):
    """Render HTML to PDF using Playwright and return the file."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 794, "height": 1123})
        await page.set_content(req.html, wait_until="networkidle")
        pdf_bytes = await page.pdf(
            format="A4",
            margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
            print_background=True,
        )
        await browser.close()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{req.filename}"'},
    )


if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
